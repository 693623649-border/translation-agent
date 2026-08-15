from __future__ import annotations

import errno
import json
import socket
import time
from pathlib import Path
from typing import Any, Mapping


MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def send_message(sock: socket.socket, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("local OCR protocol message is too large")
    sock.sendall(encoded)


def receive_message(sock: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while True:
        chunk = sock.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not chunk:
            raise ConnectionError("local OCR service closed the connection")
        chunks.extend(chunk)
        newline = chunks.find(b"\n")
        if newline >= 0:
            raw = bytes(chunks[:newline])
            break
        if len(chunks) > MAX_MESSAGE_BYTES:
            raise ValueError("local OCR protocol message exceeds the size limit")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("local OCR protocol payload must be a JSON object")
    return payload


def request(
    socket_path: Path,
    payload: Mapping[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    retry_errnos = {errno.EAGAIN, errno.EWOULDBLOCK}
    last_connect_error: BlockingIOError | None = None

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError(
                f"timed out communicating with local OCR service at {socket_path}"
            ) from last_connect_error
        return value

    while True:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(remaining())
            try:
                sock.connect(str(socket_path))
            except BlockingIOError as exc:
                if exc.errno not in retry_errnos:
                    raise
                last_connect_error = exc
                delay = min(0.01, remaining())
                time.sleep(delay)
                continue
            sock.settimeout(remaining())
            send_message(sock, payload)
            sock.settimeout(remaining())
            return receive_message(sock)
