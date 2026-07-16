from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from inno_collector.web.downloads import (
    DownloadGoneError,
    DownloadRegistrationError,
    DownloadRegistry,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class DownloadRegistryTests(unittest.TestCase):
    def _registry(
        self,
        temporary: str,
        **options: object,
    ) -> tuple[DownloadRegistry, Path, Path, Path]:
        base = Path(temporary)
        delivery_root = base / "temporary-deliveries"
        vault_root = base / "vault"
        exporter_runtime = base / "exporter-runtime"
        registry = DownloadRegistry(
            delivery_root,
            vault_root=vault_root,
            exporter_runtime_root=exporter_runtime,
            **options,
        )
        return registry, delivery_root, vault_root, exporter_runtime

    def test_register_records_safe_metadata_and_uses_opaque_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(temporary)
            package = root / "customer-delivery.zip"
            package.write_bytes(b"safe package")

            record = registry.register(package)

            self.assertGreaterEqual(len(record.id), 32)
            self.assertNotIn("customer", record.id)
            self.assertNotIn(str(package), repr(record.as_dict()))
            self.assertEqual(record.filename, "customer-delivery.zip")
            self.assertEqual(record.content_type, "application/zip")
            self.assertEqual(record.size, len(b"safe package"))
            self.assertEqual(
                record.sha256,
                hashlib.sha256(b"safe package").hexdigest(),
            )
            self.assertEqual(registry.get(record.id), record)

    def test_read_requires_registration_then_complete_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(temporary)
            package = root / "delivery.inno-update"
            package.write_bytes(b"payload")

            with self.assertRaises(DownloadGoneError):
                registry.read("not-a-real-download")

            record = registry.register(
                package,
                filename="English-safe-name.inno-update",
                content_type="application/octet-stream",
            )
            claim = registry.claim(record.id)
            self.assertEqual(claim.path, package)
            self.assertEqual(claim.filename, record.filename)
            self.assertEqual(claim.content_type, record.content_type)
            self.assertEqual(claim.size, record.size)
            self.assertEqual(claim.sha256, record.sha256)
            with self.assertRaises(DownloadGoneError):
                registry.claim(record.id)
            self.assertEqual(registry.read(record.id), b"payload")

            registry.complete(record.id, success=False)
            self.assertTrue(package.exists())
            self.assertEqual(registry.read(record.id), b"payload")

            registry.complete(record.id)

            self.assertFalse(package.exists())
            with self.assertRaises(DownloadGoneError):
                registry.get(record.id)

    def test_rejects_outside_symlink_directory_and_non_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(temporary)
            outside = Path(temporary) / "outside.zip"
            outside.write_bytes(b"outside")
            link = root / "linked.zip"
            link.symlink_to(outside)
            directory = root / "directory"
            directory.mkdir()

            for candidate in (outside, link, directory):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(DownloadRegistrationError) as raised:
                        registry.register(candidate)
                    self.assertNotIn(str(candidate), str(raised.exception))

            linked_directory = root / "linked-directory"
            linked_directory.symlink_to(Path(temporary), target_is_directory=True)
            with self.assertRaises(DownloadRegistrationError):
                registry.register(linked_directory / "outside.zip")

            vault_file = Path(temporary) / "vault" / "private.zip"
            vault_file.parent.mkdir()
            vault_file.write_bytes(b"private")
            hard_link = root / "hard-linked.zip"
            hard_link.hardlink_to(vault_file)
            with self.assertRaises(DownloadRegistrationError):
                registry.register(hard_link)

    def test_read_rejects_replaced_or_modified_registered_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(temporary)
            package = root / "delivery.zip"
            package.write_bytes(b"original")
            record = registry.register(package)

            package.write_bytes(b"modified")
            with self.assertRaises(DownloadGoneError) as raised:
                registry.read(record.id)
            self.assertEqual(str(raised.exception), "download is unavailable")

            second = root / "second.zip"
            second.write_bytes(b"second")
            second_record = registry.register(second)
            second.unlink()
            second.symlink_to(Path(temporary) / "missing.zip")
            with self.assertRaises(DownloadGoneError):
                registry.read(second_record.id)

    def test_delivery_root_must_be_disjoint_from_vault_and_exporter_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "vault"
            runtime = base / "runtime"

            unsafe_roots = (
                vault,
                vault / "temporary-deliveries",
                runtime,
                runtime / "temporary-deliveries",
            )
            for delivery_root in unsafe_roots:
                with self.subTest(delivery_root=delivery_root.name):
                    with self.assertRaises(ValueError) as raised:
                        DownloadRegistry(
                            delivery_root,
                            vault_root=vault,
                            exporter_runtime_root=runtime,
                        )
                    self.assertNotIn(str(delivery_root), str(raised.exception))

    def test_expiry_and_count_limits_remove_entries_and_temporary_files(self) -> None:
        clock = ManualClock()
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(
                temporary,
                max_entries=2,
                ttl_seconds=10,
                clock=clock,
            )
            files = []
            records = []
            for index in range(3):
                path = root / f"delivery-{index}.zip"
                path.write_bytes(str(index).encode())
                files.append(path)
                records.append(registry.register(path))

            self.assertFalse(files[0].exists())
            with self.assertRaises(DownloadGoneError):
                registry.get(records[0].id)
            self.assertTrue(files[-1].exists())

            clock.value += 11
            self.assertEqual(registry.cleanup(), 2)
            self.assertFalse(files[1].exists())
            self.assertFalse(files[2].exists())
            with self.assertRaises(DownloadGoneError):
                registry.get(records[-1].id)

    def test_claim_protects_active_download_until_completion_callback(self) -> None:
        clock = ManualClock()
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(
                temporary,
                max_entries=1,
                ttl_seconds=10,
                clock=clock,
            )
            active = root / "active.zip"
            active.write_bytes(b"active")
            record = registry.register(active)
            registry.claim(record.id)

            clock.value += 11
            self.assertEqual(registry.cleanup(), 0)
            self.assertTrue(active.exists())

            waiting = root / "waiting.zip"
            waiting.write_bytes(b"waiting")
            with self.assertRaises(DownloadRegistrationError) as raised:
                registry.register(waiting)
            self.assertEqual(str(raised.exception), "download registry is busy")

            registry.complete(record.id, success=False)
            self.assertFalse(active.exists())

    def test_rejects_unsafe_filename_and_mime_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(temporary)
            package = root / "delivery.zip"
            package.write_bytes(b"payload")

            for filename in ("../escape.zip", "folder/file.zip", "bad\nname.zip"):
                with self.subTest(filename=filename):
                    with self.assertRaises(DownloadRegistrationError) as raised:
                        registry.register(package, filename=filename)
                    self.assertEqual(
                        str(raised.exception),
                        "download metadata is invalid",
                    )

            with self.assertRaises(DownloadRegistrationError):
                registry.register(
                    package,
                    content_type="application/zip\r\nX-Evil: true",
                )

    def test_parallel_registration_read_and_completion_is_thread_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, root, _, _ = self._registry(
                temporary,
                max_entries=50,
            )
            gate = threading.Barrier(8)

            def round_trip(index: int) -> str:
                package = root / f"parallel-{index}.zip"
                body = f"payload-{index}".encode()
                package.write_bytes(body)
                gate.wait(timeout=2)
                record = registry.register(package)
                self.assertEqual(registry.read(record.id), body)
                registry.complete(record.id)
                return record.id

            with ThreadPoolExecutor(max_workers=8) as executor:
                ids = list(executor.map(round_trip, range(8)))

            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(registry.count, 0)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
