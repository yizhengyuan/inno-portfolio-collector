from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import HTTP
from pathlib import Path
from typing import BinaryIO


MAX_MULTIPART_HEADER_BYTES = 16 << 10


class MultipartError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UploadedFile:
    filename: str
    content_type: str
    path: Path
    size: int

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _content_type(value: str) -> tuple[str, dict[str, str]]:
    message = Message()
    message["content-type"] = value
    media_type = message.get_content_type().lower()
    params = {
        str(key).lower(): str(item)
        for key, item in message.get_params(header="content-type", failobj=[])[1:]
    }
    return media_type, params


def _upload_headers(raw: bytes) -> tuple[str, str]:
    try:
        headers = BytesHeaderParser(policy=HTTP).parsebytes(raw + b"\r\n")
    except Exception:
        raise MultipartError("invalid multipart headers") from None
    if len(headers) != 2 or set(name.lower() for name in headers.keys()) != {
        "content-disposition",
        "content-type",
    }:
        raise MultipartError("invalid multipart headers")
    disposition = headers.get_content_disposition()
    name = headers.get_param("name", header="content-disposition")
    filename = headers.get_filename()
    if (
        disposition != "form-data"
        or name != "file"
        or not isinstance(filename, str)
        or not filename
        or len(filename.encode("utf-8")) > 255
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise MultipartError("invalid upload filename")
    part_type = headers.get_content_type().lower()
    if part_type not in {"application/octet-stream", "application/zip"}:
        raise MultipartError("invalid upload content type")
    return filename, part_type


def parse_single_file_multipart(
    content_type: str,
    stream: BinaryIO,
    content_length: int,
    upload_root: Path,
    *,
    max_file_bytes: int,
) -> UploadedFile:
    media_type, params = _content_type(content_type)
    boundary_text = params.get("boundary", "")
    try:
        boundary = boundary_text.encode("ascii")
    except UnicodeEncodeError:
        raise MultipartError("invalid multipart boundary") from None
    if (
        media_type != "multipart/form-data"
        or not 1 <= len(boundary) <= 70
        or any(value < 33 or value > 126 for value in boundary)
    ):
        raise MultipartError("invalid multipart content type")

    consumed = 0
    opening = stream.readline(MAX_MULTIPART_HEADER_BYTES + 1)
    consumed += len(opening)
    if opening != b"--" + boundary + b"\r\n":
        raise MultipartError("invalid multipart body")
    header_lines: list[bytes] = []
    header_size = 0
    while True:
        line = stream.readline(MAX_MULTIPART_HEADER_BYTES + 1)
        consumed += len(line)
        if not line or len(line) > MAX_MULTIPART_HEADER_BYTES:
            raise MultipartError("invalid multipart headers")
        if line == b"\r\n":
            break
        header_size += len(line)
        if header_size > MAX_MULTIPART_HEADER_BYTES:
            raise MultipartError("invalid multipart headers")
        header_lines.append(line)
    filename, part_type = _upload_headers(b"".join(header_lines))

    trailer = b"\r\n--" + boundary + b"--\r\n"
    file_size = content_length - consumed - len(trailer)
    if file_size < 0 or file_size > max_file_bytes:
        raise MultipartError("uploaded file exceeded safe limit")

    root = Path(upload_root)
    try:
        if root.is_symlink():
            raise OSError
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_resolved = root.resolve(strict=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".upload-",
            suffix=".tmp",
            dir=root_resolved,
        )
        os.fchmod(descriptor, 0o600)
    except OSError:
        raise MultipartError("upload storage is unavailable") from None
    temporary = Path(temporary_name)
    remaining = file_size
    embedded_delimiters = (
        b"\r\n--" + boundary + b"\r\n",
        b"\r\n--" + boundary + b"--\r\n",
    )
    scan_tail = b""
    overlap = max(len(value) for value in embedded_delimiters) - 1
    try:
        with os.fdopen(descriptor, "wb") as output:
            while remaining:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    raise MultipartError("uploaded file was incomplete")
                scan = scan_tail + chunk
                if any(value in scan for value in embedded_delimiters):
                    raise MultipartError("exactly one uploaded file is required")
                scan_tail = scan[-overlap:]
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if stream.read(len(trailer)) != trailer:
            raise MultipartError("invalid multipart trailer")
        return UploadedFile(
            filename=filename,
            content_type=part_type,
            path=temporary,
            size=file_size,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
