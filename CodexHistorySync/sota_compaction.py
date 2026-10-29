"""Versioned local compaction state, protected by the current Windows user's DPAPI."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

PREFIX = "codex-sota.compaction.v1."
MAX_SUMMARY_BYTES = 256 * 1024


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(raw: bytes, protect: bool) -> bytes:
    buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    entropy_bytes = PREFIX.encode("ascii")
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    entropy = _Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_byte)))
    output = _Blob()
    operation = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    try:
        # UI_FORBIDDEN: a background router must never open a Windows credential prompt.
        if not operation(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise ctypes.WinError()
        return ctypes.string_at(output.data, output.size)
    finally:
        if output.data:
            ctypes.windll.kernel32.LocalFree(output.data)


def protect_summary(summary: str) -> str:
    raw = summary.encode("utf-8")
    if not raw or len(raw) > MAX_SUMMARY_BYTES:
        raise ValueError("Compaction summary is empty or exceeds the local state limit")
    return PREFIX + base64.b64encode(_crypt(raw, True)).decode("ascii")


def restore_summary(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise ValueError("This bridge cannot decode another provider's encrypted compaction state")
    if len(value) > MAX_SUMMARY_BYTES * 2:
        raise ValueError("Local compaction state exceeds the adapter limit")
    try:
        raw = _crypt(base64.b64decode(value[len(PREFIX):], validate=True), False)
        if not raw or len(raw) > MAX_SUMMARY_BYTES:
            raise ValueError("Invalid summary size")
        return raw.decode("utf-8")
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError("Local compaction state is damaged or belongs to another Windows user") from error


def restore_payload(payload: dict, *, require_local: bool = False) -> dict:
    history = payload.get("input")
    if not isinstance(history, list):
        return payload
    restored = []
    changed = False
    for item in history:
        if isinstance(item, dict) and item.get("type") == "compaction":
            value = item.get("encrypted_content")
            if require_local or (isinstance(value, str) and value.startswith("codex-sota.compaction.")):
                summary = restore_summary(value)
                restored.append({"role": "user", "content": "Conversation handoff summary:\n\n" + summary})
                changed = True
                continue
        restored.append(item)
    return dict(payload, input=restored) if changed else payload
