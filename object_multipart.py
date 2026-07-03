"""Multipart form parsing for object execution, standard library only.

Browser upload forms post `multipart/form-data`. Text parts become plain
payload fields; file parts are collected under a `_files` mapping with the
content base64-encoded so the payload stays JSON-serializable across the
subprocess execution boundary:

    request["photo_caption"]                       # text field
    request["_files"]["photo"]["filename"]
    request["_files"]["photo"]["content_type"]
    request["_files"]["photo"]["size"]
    base64.b64decode(request["_files"]["photo"]["content_base64"])

Request bodies are already capped by DBBASIC_MAX_REQUEST_BYTES before parsing.
"""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from typing import Any

FILES_KEY = "_files"


class InvalidMultipartError(ValueError):
    """Raised when a multipart body cannot be parsed."""


def is_multipart_content_type(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() == "multipart/form-data"


def parse_multipart(body: bytes, content_type: str) -> dict[str, Any]:
    """Parse one multipart/form-data body into an object payload dict."""
    if not is_multipart_content_type(content_type):
        raise InvalidMultipartError("Content type is not multipart/form-data")
    if "boundary=" not in content_type:
        raise InvalidMultipartError("Multipart body is missing a boundary")

    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    try:
        message = BytesParser(policy=policy.HTTP).parsebytes(header + body)
    except Exception as exc:  # email parser errors are varied
        raise InvalidMultipartError(f"Could not parse multipart body: {exc}") from exc

    if not message.is_multipart():
        raise InvalidMultipartError("Multipart body has no parts")

    payload: dict[str, Any] = {}
    files: dict[str, dict[str, Any]] = {}

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="Content-Disposition")
        if not name:
            continue

        filename = part.get_filename()
        content = part.get_payload(decode=True)
        if content is None:
            content = b""

        if filename is not None:
            files[str(name)] = {
                "filename": str(filename),
                "content_type": part.get_content_type(),
                "size": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        else:
            try:
                payload[str(name)] = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidMultipartError(
                    f"Form field {name} is not valid UTF-8 text"
                ) from exc

    if files:
        payload[FILES_KEY] = files
    return payload
