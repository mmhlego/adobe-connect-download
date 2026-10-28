"""Read mainstream.xml and turn it into a timeline of stream segments.

Classic Adobe Connect recordings are not a single file. They are several FLV
fragments, each with a different start offset on a shared timeline. The offsets
are published as ``streamAdded`` / ``streamRemoved`` play events in
``mainstream.xml``; the FLV itself only knows its own local time.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AcdlError

VIDEO_PREFIXES = ("screenshare",)
AUDIO_PREFIXES = ("cameravoip", "voip", "telephony", "phone")


@dataclass(frozen=True)
class Segment:
  name: str
  kind: str  # "video" | "audio" | "data"
  start_ms: int
  end_ms: int
  path: Path
  stream_type: str = ""

  @property
  def start_s(self) -> float:
    return self.start_ms / 1000.0

  @property
  def end_s(self) -> float:
    return self.end_ms / 1000.0

  @property
  def duration_s(self) -> float:
    return max(0.0, (self.end_ms - self.start_ms) / 1000.0)


@dataclass
class Recording:
  title: str
  duration_ms: int
  bundle_dir: Path
  video: list[Segment] = field(default_factory=list)
  audio: list[Segment] = field(default_factory=list)
  ignored: list[Segment] = field(default_factory=list)

  @property
  def duration_s(self) -> float:
    return self.duration_ms / 1000.0


def load_recording(bundle_dir: Path, *, title: str = "recording") -> Recording:
  mainstream = bundle_dir / "mainstream.xml"
  if not mainstream.is_file():
    nested = list(bundle_dir.glob("*/mainstream.xml"))
    if nested:
      mainstream = nested[0]
      bundle_dir = mainstream.parent
  if not mainstream.is_file():
    raise AcdlError(f"mainstream.xml not found in {bundle_dir}")

  try:
    root = ET.parse(mainstream).getroot()
  except ET.ParseError as exc:
    raise AcdlError(f"could not parse {mainstream}: {exc}") from exc

  total_ms = _read_duration_ms(root)
  raw = _read_stream_events(root)

  segments: list[Segment] = []
  for name, (start, end, stream_type) in raw.items():
    path = bundle_dir / f"{name}.flv"
    if not path.is_file():
      continue
    end_ms = end if end is not None and end > start else total_ms
    kind = _classify(bundle_dir, name, stream_type)
    segments.append(
      Segment(
        name=name,
        kind=kind,
        start_ms=start,
        end_ms=min(end_ms, total_ms) if total_ms else end_ms,
        path=path,
        stream_type=stream_type,
      )
    )

  if not total_ms:
    total_ms = max((segment.end_ms for segment in segments), default=0)
  if total_ms <= 0:
    raise AcdlError("could not determine the recording duration")

  segments.sort(key=lambda segment: (segment.start_ms, segment.name))
  video = [segment for segment in segments if segment.kind == "video"]
  audio = [segment for segment in segments if segment.kind == "audio"]
  ignored = [segment for segment in segments if segment.kind == "data"]

  return Recording(
    title=title,
    duration_ms=total_ms,
    bundle_dir=bundle_dir,
    video=video,
    audio=audio,
    ignored=ignored,
  )


def _read_duration_ms(root: ET.Element) -> int:
  for message in root.findall("Message"):
    if message.findtext("Method") != "onMetaData":
      continue
    number = message.find(".//Number")
    if number is not None and number.text:
      try:
        return int(round(float(number.text) * 1000))
      except ValueError:
        return 0
  return 0


def _read_stream_events(root: ET.Element) -> dict[str, tuple[int, int | None, str]]:
  """Return name -> (start_ms, end_ms|None, stream_type)."""
  events: dict[str, dict] = {}
  for message in root.findall("Message"):
    if message.findtext("Method") != "playEvent":
      continue
    event = message.findtext("String")
    if event not in ("streamAdded", "streamRemoved"):
      continue
    obj = message.find("Array/Object")
    if obj is None:
      continue
    name = (obj.findtext("streamName") or "").strip().lstrip("/")
    if not name:
      continue
    try:
      message_time = int(message.get("time") or 0)
    except ValueError:
      message_time = 0
    entry = events.setdefault(
      name,
      {"start": None, "end": None, "type": (obj.findtext("streamType") or "")},
    )
    if event == "streamAdded":
      try:
        start = int(obj.findtext("startTime") or message_time)
      except ValueError:
        start = message_time
      if entry["start"] is None or start < entry["start"]:
        entry["start"] = start
    else:
      if message_time > 0:
        entry["end"] = message_time

  result: dict[str, tuple[int, int | None, str]] = {}
  for name, entry in events.items():
    if entry["start"] is None:
      continue
    result[name] = (entry["start"], entry["end"], entry["type"])
  return result


def _classify(bundle_dir: Path, name: str, stream_type: str) -> str:
  flag = _read_flag(bundle_dir / f"{name}.xml")
  if "video" in flag:
    return "video"
  if "audio" in flag:
    return "audio"
  if flag:
    # "data" (whiteboard, cursor, chat, ...) is not renderable media
    return "data"

  lowered = name.lower()
  if stream_type == "screenshare" or lowered.startswith(VIDEO_PREFIXES):
    return "video"
  if stream_type == "cameraVoip" or lowered.startswith(AUDIO_PREFIXES):
    return "audio"
  return "data"


def _read_flag(path: Path) -> str:
  if not path.is_file():
    return ""
  try:
    root = ET.parse(path).getroot()
  except ET.ParseError:
    return ""
  return (root.findtext("Flag") or "").strip().lower()
