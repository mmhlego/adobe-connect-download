"""Export the chat log that Adobe Connect stores beside the recording."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .util import fmt_duration


@dataclass(frozen=True)
class ChatMessage:
    position_ms: int
    text: str
    sender: str


def collect_chat(bundle_dir: Path) -> list[ChatMessage]:
    messages = _from_ftchat(bundle_dir)
    if messages:
        return messages
    return _from_sco_metadata(bundle_dir)


def write_chat(bundle_dir: Path, output: Path, *, title: str) -> int:
    messages = collect_chat(bundle_dir)
    if not messages:
        return 0
    lines = [f"Chat log — {title}", ""]
    for message in messages:
        sender = message.sender or "guest"
        lines.append(
            f"[{fmt_duration(message.position_ms / 1000.0)}] {sender}: {message.text}"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(messages)


def _from_ftchat(bundle_dir: Path) -> list[ChatMessage]:
    path = bundle_dir / "ftchat0.xml"
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []

    messages: list[ChatMessage] = []
    for message in root.findall("Message"):
        if message.findtext("String") != "sendMessage6":
            continue
        payload = next(
            (obj for obj in message.findall("Object") if obj.find("text") is not None),
            None,
        )
        if payload is None:
            continue
        text = (payload.findtext("text") or "").strip()
        if not text:
            continue
        sender = (payload.findtext("fromName") or "").strip()
        try:
            position = int(message.get("time") or 0)
        except ValueError:
            position = 0
        messages.append(ChatMessage(position_ms=position, text=text, sender=sender))

    messages.sort(key=lambda item: item.position_ms)
    return messages


def _from_sco_metadata(bundle_dir: Path) -> list[ChatMessage]:
    path = bundle_dir / "sco_metadata.xml"
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []

    messages: list[ChatMessage] = []
    for section in root.findall("section"):
        if section.get("type") != "chat":
            continue
        text = (section.findtext("content") or "").strip()
        if not text:
            continue
        try:
            position = int(section.get("position") or 0)
        except ValueError:
            position = 0
        messages.append(ChatMessage(position_ms=position, text=text, sender=""))

    messages.sort(key=lambda item: item.position_ms)
    return messages
