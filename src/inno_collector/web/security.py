from __future__ import annotations

import hmac


LOOPBACK_HOST = "127.0.0.1"
SESSION_HEADER = "X-Inno-Session"
MAX_REQUEST_BODY_BYTES = 4 << 20
MAX_UPLOAD_BYTES = 512 << 20
MAX_UPLOAD_FILE_BYTES = 500 << 20
MAX_RESPONSE_BYTES = 8 << 20
MAX_DOWNLOAD_BYTES = 512 << 20


class SecurityError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validate_bind_host(host: str) -> str:
    if host != LOOPBACK_HOST:
        raise ValueError("Web server must bind to 127.0.0.1")
    return host


def validate_host_header(host_header: str, port: int) -> None:
    if host_header != f"{LOOPBACK_HOST}:{port}":
        raise SecurityError(
            421,
            "misdirected_request",
            "This request is not for the local application.",
        )


def validate_write_headers(
    *,
    content_type: str,
    origin: str,
    token: str,
    expected_origin: str,
    expected_token: str,
) -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise SecurityError(
            415,
            "unsupported_media_type",
            "Writes require application/json.",
        )
    validate_write_identity(
        origin=origin,
        token=token,
        expected_origin=expected_origin,
        expected_token=expected_token,
    )


def validate_write_identity(
    *,
    origin: str,
    token: str,
    expected_origin: str,
    expected_token: str,
) -> None:
    if not origin or not hmac.compare_digest(origin, expected_origin):
        raise SecurityError(403, "invalid_origin", "Request origin was rejected.")
    if not token or not hmac.compare_digest(token, expected_token):
        raise SecurityError(403, "invalid_session", "Session token was rejected.")


def security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
