"""Small helpers shared by the CLI and tests."""

from __future__ import annotations

import re
import unicodedata

_INVALID = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def sanitize_filename(name: str, *, max_length: int = 120) -> str:
    """Make a filesystem-safe folder name while keeping Unicode readable."""
    name = unicodedata.normalize("NFC", name or "")
    name = _INVALID.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    if name in ("", ".", ".."):
        return "recording"
    return name


def parse_time(value: str | None) -> float | None:
    """Accept '90', '1:30' or '01:02:03.5' and return seconds."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time: {value!r}")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"invalid time: {value!r}") from exc
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    if seconds < 0:
        raise ValueError(f"invalid time: {value!r}")
    return seconds


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
