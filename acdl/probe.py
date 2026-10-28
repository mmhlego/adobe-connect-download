"""Locate ffmpeg/ffprobe and read basic media information."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import AcdlError, FfmpegMissingError

_INSTALL_HINT = "ffmpeg is required. Install it with:\n    brew install ffmpeg"


@dataclass(frozen=True)
class MediaInfo:
  width: int = 0
  height: int = 0
  duration_s: float = 0.0
  has_video: bool = False
  has_audio: bool = False
  video_codec: str = ""
  audio_codec: str = ""


def find_ffmpeg() -> str:
  override = os.environ.get("ACDL_FFMPEG")
  if override:
    if Path(override).is_file():
      return override
    raise FfmpegMissingError(f"ACDL_FFMPEG points at a missing file: {override}")
  found = shutil.which("ffmpeg")
  if not found:
    raise FfmpegMissingError(_INSTALL_HINT)
  return found


def find_ffprobe(ffmpeg: str | None = None) -> str | None:
  override = os.environ.get("ACDL_FFPROBE")
  if override and Path(override).is_file():
    return override
  if ffmpeg:
    sibling = Path(ffmpeg).with_name("ffprobe")
    if sibling.is_file():
      return str(sibling)
  return shutil.which("ffprobe")


def ffmpeg_version(ffmpeg: str) -> str:
  try:
    result = subprocess.run(
      [ffmpeg, "-version"], capture_output=True, text=True, timeout=20
    )
  except (OSError, subprocess.SubprocessError):
    return "unknown"
  first = (result.stdout or "").splitlines()
  return first[0].replace("ffmpeg version ", "").strip() if first else "unknown"


def probe(path: Path, *, ffmpeg: str | None = None) -> MediaInfo:
  ffprobe = find_ffprobe(ffmpeg)
  if ffprobe:
    info = _probe_with_ffprobe(ffprobe, path)
    if info is not None:
      return info
  ffmpeg = ffmpeg or find_ffmpeg()
  return _probe_with_ffmpeg(ffmpeg, path)


def _probe_with_ffprobe(ffprobe: str, path: Path) -> MediaInfo | None:
  command = [
    ffprobe,
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_format",
    "-show_streams",
    str(path),
  ]
  try:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
  except (OSError, subprocess.SubprocessError):
    return None
  if result.returncode != 0:
    return None
  try:
    payload = json.loads(result.stdout)
  except ValueError:
    return None

  video = next(
    (item for item in payload.get("streams", []) if item.get("codec_type") == "video"),
    None,
  )
  audio = next(
    (item for item in payload.get("streams", []) if item.get("codec_type") == "audio"),
    None,
  )
  duration = 0.0
  try:
    duration = float(payload.get("format", {}).get("duration") or 0.0)
  except (TypeError, ValueError):
    duration = 0.0
  return MediaInfo(
    width=int(video.get("width") or 0) if video else 0,
    height=int(video.get("height") or 0) if video else 0,
    duration_s=duration,
    has_video=video is not None,
    has_audio=audio is not None,
    video_codec=(video.get("codec_name") or "") if video else "",
    audio_codec=(audio.get("codec_name") or "") if audio else "",
  )


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?: Video: ([^,]+).*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\S+.*?: Audio: ([^,]+)")


def _probe_with_ffmpeg(ffmpeg: str, path: Path) -> MediaInfo:
  try:
    result = subprocess.run(
      [ffmpeg, "-hide_banner", "-i", str(path)],
      capture_output=True,
      text=True,
      timeout=60,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    raise AcdlError(f"could not run ffmpeg: {exc}") from exc

  text = result.stderr or ""
  if "Invalid data found" in text or "No such file" in text:
    raise AcdlError(f"ffmpeg could not read {path.name}")

  duration = 0.0
  match = _DURATION_RE.search(text)
  if match:
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

  video = _VIDEO_RE.search(text)
  audio = _AUDIO_RE.search(text)
  return MediaInfo(
    width=int(video.group(2)) if video else 0,
    height=int(video.group(3)) if video else 0,
    duration_s=duration,
    has_video=video is not None,
    has_audio=audio is not None,
    video_codec=video.group(1).strip() if video else "",
    audio_codec=audio.group(1).strip() if audio else "",
  )
