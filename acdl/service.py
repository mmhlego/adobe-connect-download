"""Shared download/compose pipeline used by the CLI and the TUI.

The CLI and the TUI differ only in how they report progress. Everything that
talks to the network or ffmpeg lives here and emits events through a small
``Reporter`` protocol, so both front ends drive exactly the same code path.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import __version__
from .bundle import extract
from .chat import write_chat
from .compose import AudioReport, compose
from .download import download_bundle
from .errors import AcdlError
from .probe import ffmpeg_version
from .source import Source, resolve
from .timeline import Recording, load_recording
from .util import fmt_duration, sanitize_filename

STAGE_LABELS = {
  "queued": "Queued",
  "resolving": "Resolving",
  "downloading": "Downloading",
  "extracting": "Extracting",
  "measuring": "Measuring",
  "encoding": "Encoding",
  "finalizing": "Finalizing",
  "done": "Done",
  "skipped": "Skipped",
  "error": "Error",
  "cancelled": "Cancelled",
}


@dataclass(frozen=True)
class Progress:
  """A single progress sample for one recording."""

  fraction: float | None = None
  detail: str = ""
  eta_s: float | None = None


class Reporter(Protocol):
  """Receives pipeline events from a worker thread."""

  def title(self, title: str) -> None: ...
  def stage(self, stage: str) -> None: ...
  def progress(self, progress: Progress) -> None: ...
  def log(self, message: str) -> None: ...


class NullReporter:
  """Reporter that discards every event (useful for tests and scripts)."""

  def title(self, title: str) -> None:
    pass

  def stage(self, stage: str) -> None:
    pass

  def progress(self, progress: Progress) -> None:
    pass

  def log(self, message: str) -> None:
    pass


@dataclass
class Options:
  """Everything the pipeline needs to process one or more recordings."""

  output_dir: Path = Path("downloads")
  crf: int = 23
  fps: int = 10
  start: float | None = None
  end: float | None = None
  audio_only: bool = False
  normalize: bool = True
  lufs: float = -16.0
  chat: bool = False
  keep_raw: bool = False
  force: bool = False
  name: str | None = None
  insecure: bool = False


@dataclass(frozen=True)
class Result:
  path: Path
  title: str
  duration_s: float = 0.0
  skipped: bool = False
  audio: AudioReport | None = None


def process_url(
  url: str,
  options: Options,
  reporter: Reporter,
  ffmpeg: str,
) -> Result:
  """Resolve, download, compose and report one recording."""
  reporter.stage("resolving")
  src, session = resolve(url, insecure=options.insecure)
  title = options.name or src.title
  reporter.title(title)
  reporter.log(f"room {src.room} · recording {src.sco_id}")

  folder = options.output_dir.expanduser() / sanitize_filename(title)
  output = folder / ("recording.m4a" if options.audio_only else "recording.mp4")

  if output.exists() and not options.force:
    reporter.log(f"skipped (already exists): {output}")
    reporter.stage("skipped")
    return Result(path=output, title=title, skipped=True)

  raw_dir = folder / "raw"
  reporter.stage("downloading")
  zip_path = download_bundle(
    session,
    src,
    raw_dir / f"{src.sco_id}.zip",
    force=options.force,
    on_progress=lambda done, total, speed: _download_progress(
      reporter, done, total, speed
    ),
  )

  reporter.stage("extracting")
  bundle_dir = extract(zip_path, raw_dir / "extracted", force=options.force)
  recording = load_recording(bundle_dir, title=title)
  _log_recording(recording, reporter)

  result = compose(
    recording,
    output,
    crf=options.crf,
    fps=options.fps,
    start_s=options.start or 0.0,
    end_s=options.end,
    audio_only=options.audio_only,
    normalize=options.normalize,
    target_lufs=options.lufs,
    on_stage=reporter.stage,
    on_progress=lambda seconds, total: _compose_progress(reporter, seconds, total),
    on_log=reporter.log,
  )
  output = result.path

  if options.chat:
    count = write_chat(bundle_dir, folder / "chat.txt", title=title)
    if count:
      reporter.log(f"wrote chat.txt ({count} messages)")
    else:
      reporter.log("no chat messages found")

  reporter.stage("finalizing")
  _write_metadata(folder, src, recording, output, options, ffmpeg, result.audio)
  size_mb = output.stat().st_size / (1024 * 1024)
  reporter.log(f"wrote {output} ({size_mb:.1f} MB)")

  if options.keep_raw:
    reporter.log(f"kept raw assets in {raw_dir}")
  else:
    shutil.rmtree(raw_dir, ignore_errors=True)

  reporter.stage("done")
  return Result(
    path=output,
    title=title,
    duration_s=recording.duration_s,
    audio=result.audio,
  )


def compose_existing(
  source: Path,
  options: Options,
  reporter: Reporter,
  ffmpeg: str,
) -> Result:
  """Compose a ZIP or an already-extracted bundle without touching the network."""
  temporary: Path | None = None
  try:
    if source.is_dir():
      bundle_dir = source
      title = options.name or source.name
    elif source.suffix.lower() == ".zip":
      temporary = Path(tempfile.mkdtemp(prefix="acdl-compose-"))
      bundle_dir = extract(source, temporary, force=True)
      title = options.name or source.stem
    else:
      raise AcdlError("expected a .zip file or an extracted directory")

    reporter.stage("extracting")
    recording = load_recording(bundle_dir, title=title)
    reporter.title(title)
    _log_recording(recording, reporter)

    folder = options.output_dir.expanduser() / sanitize_filename(title)
    output = folder / ("recording.m4a" if options.audio_only else "recording.mp4")
    if output.exists() and not options.force:
      reporter.log(f"skipped (already exists): {output}")
      reporter.stage("skipped")
      return Result(path=output, title=title, skipped=True)

    result = compose(
      recording,
      output,
      crf=options.crf,
      fps=options.fps,
      start_s=options.start or 0.0,
      end_s=options.end,
      audio_only=options.audio_only,
      normalize=options.normalize,
      target_lufs=options.lufs,
      on_stage=reporter.stage,
      on_progress=lambda seconds, total: _compose_progress(reporter, seconds, total),
      on_log=reporter.log,
    )
    output = result.path

    if options.chat:
      count = write_chat(bundle_dir, folder / "chat.txt", title=title)
      if count:
        reporter.log(f"wrote chat.txt ({count} messages)")
      else:
        reporter.log("no chat messages found")

    reporter.stage("finalizing")
    _write_metadata(folder, None, recording, output, options, ffmpeg, result.audio)
    size_mb = output.stat().st_size / (1024 * 1024)
    reporter.log(f"wrote {output} ({size_mb:.1f} MB)")
    reporter.stage("done")
    return Result(
      path=output,
      title=title,
      duration_s=recording.duration_s,
      audio=result.audio,
    )
  finally:
    if temporary is not None:
      shutil.rmtree(temporary, ignore_errors=True)


def _download_progress(
  reporter: Reporter,
  done: int,
  total: int | None,
  speed: float,
) -> None:
  if total:
    fraction = min(1.0, done / total)
    percent = f"{fraction * 100:4.1f}%"
    size = f"{_mb(done)}/{_mb(total)} MB"
    eta = (total - done) / speed if speed > 0 else None
    detail = f"{size} ({percent})"
  else:
    fraction = None
    size = f"{_mb(done)} MB"
    eta = None
    detail = size
  if speed > 0:
    detail += f" · {_mb(speed)} MB/s"
  reporter.progress(Progress(fraction=fraction, detail=detail, eta_s=eta))


def _compose_progress(reporter: Reporter, seconds: float, total: float) -> None:
  fraction = min(1.0, seconds / total) if total > 0 else None
  reporter.progress(
    Progress(
      fraction=fraction,
      detail=f"{fmt_duration(seconds)} / {fmt_duration(total)}",
      eta_s=max(0.0, total - seconds),
    )
  )


def _log_recording(recording: Recording, reporter: Reporter) -> None:
  reporter.log(
    f"duration {fmt_duration(recording.duration_s)} · "
    f"{len(recording.video)} screen segment(s) · "
    f"{len(recording.audio)} audio segment(s)"
  )
  if recording.ignored:
    names = ", ".join(segment.name for segment in recording.ignored)
    reporter.log(f"ignoring non-media streams: {names}")


def _write_metadata(
  folder: Path,
  src: Source | None,
  recording: Recording,
  output: Path,
  options: Options,
  ffmpeg: str,
  audio: AudioReport | None,
) -> None:
  folder.mkdir(parents=True, exist_ok=True)
  payload = {
    "title": recording.title,
    "duration_s": round(recording.duration_s, 3),
    "source": (
      {
        "origin": src.origin,
        "room": src.room,
        "sco_id": src.sco_id,
      }
      if src
      else None
    ),
    "range": {"from_s": options.start or 0.0, "to_s": options.end},
    "streams": {
      "video": [_segment_json(segment) for segment in recording.video],
      "audio": [_segment_json(segment) for segment in recording.audio],
      "ignored": [_segment_json(segment) for segment in recording.ignored],
    },
    "output": output.name,
    "audio": _audio_json(audio),
    "tool": {"name": "acdl", "version": __version__},
    "ffmpeg": ffmpeg_version(ffmpeg),
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
  }
  path = folder / "metadata.json"
  path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
  )


def _segment_json(segment) -> dict[str, object]:
  return {
    "name": segment.name,
    "start_s": round(segment.start_s, 3),
    "end_s": round(segment.end_s, 3),
    "file": segment.path.name,
  }


def _audio_json(report: AudioReport | None) -> dict[str, object] | None:
  if report is None:
    return None
  return {
    "normalized": report.normalized,
    "target_lufs": report.target_lufs,
    "target_true_peak_db": report.target_true_peak_db,
    "input_lufs": _round(report.input_lufs),
    "input_true_peak_db": _round(report.input_true_peak_db),
    "input_lra": _round(report.input_lra),
    "output_lufs": _round(report.output_lufs),
    "output_true_peak_db": _round(report.output_true_peak_db),
    "normalization_type": report.normalization_type,
  }


def _round(value: float | None) -> float | None:
  return round(value, 2) if value is not None else None


def _mb(value: float) -> str:
  return f"{value / (1024 * 1024):.1f}"
