"""Compose the FLV fragments into one timeline-accurate video with ffmpeg.

The recording is a set of fragments that each belong at an absolute offset on a
shared clock. We build a canvas of the full duration, overlay every screen-share
fragment at its offset, and delay/mix the audio fragments the same way. Gaps are
intentionally left as background; that is what the original recording contains.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import AcdlError, UnsupportedRecordingError
from .loudness import Loudness, OutputStats, measure, parse_output
from .probe import MediaInfo, find_ffmpeg, probe
from .timeline import Recording, Segment

BACKGROUND = "0x111318"
DEFAULT_LUFS = -16.0
DEFAULT_TRUE_PEAK = -1.5


@dataclass(frozen=True)
class Clip:
    path: Path
    offset_s: float
    duration_s: float
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class AudioReport:
    normalized: bool
    target_lufs: float
    target_true_peak_db: float
    input_lufs: float | None = None
    input_true_peak_db: float | None = None
    input_lra: float | None = None
    output_lufs: float | None = None
    output_true_peak_db: float | None = None
    normalization_type: str | None = None


@dataclass(frozen=True)
class ComposeResult:
    path: Path
    audio: AudioReport


def clip_segments(
    segments: list[Segment], start_s: float, end_s: float
) -> list[Clip]:
    """Clip *segments* to [start_s, end_s] and rebase their offsets to start_s."""
    clips: list[Clip] = []
    for segment in segments:
        begin = max(segment.start_s, start_s)
        finish = min(segment.end_s, end_s)
        if finish - begin <= 0.01:
            continue
        clips.append(
            Clip(
                path=segment.path,
                offset_s=begin - start_s,
                duration_s=finish - begin,
            )
        )
    return clips


def build_filtergraph(
    video: list[Clip],
    audio: list[Clip],
    *,
    width: int,
    height: int,
    fps: int,
    total_s: float,
    audio_filter: str | None = None,
) -> str:
    parts: list[str] = []

    if video:
        parts.append(
            f"color=c={BACKGROUND}:s={width}x{height}:r={fps}:d={total_s:.3f}[base]"
        )
        previous = "base"
        for index, clip in enumerate(video):
            chain = [f"fps={fps}", "setsar=1", "setpts=PTS-STARTPTS"]
            if clip.width and clip.height and (clip.width, clip.height) != (width, height):
                chain += [
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={BACKGROUND}",
                ]
            chain += [
                f"trim=duration={clip.duration_s:.3f}",
                f"setpts=PTS-STARTPTS+{clip.offset_s:.3f}/TB",
            ]
            parts.append(f"[{index}:v]{','.join(chain)}[v{index}]")
            parts.append(
                f"[{previous}][v{index}]overlay=eof_action=pass:shortest=0[o{index}]"
            )
            previous = f"o{index}"
        parts.append(f"[{previous}]fps={fps},format=yuv420p[vout]")

    audio_start = len(video)
    if audio:
        labels: list[str] = []
        for index, clip in enumerate(audio):
            chain = [
                "aresample=async=1:first_pts=0",
                "asetpts=PTS-STARTPTS",
                f"atrim=duration={clip.duration_s:.3f}",
            ]
            delay_ms = max(0, int(round(clip.offset_s * 1000)))
            if delay_ms:
                chain.append(f"adelay={delay_ms}|{delay_ms}")
            chain.append(f"apad=whole_dur={total_s:.3f}")
            parts.append(f"[{audio_start + index}:a]{','.join(chain)}[a{index}]")
            labels.append(f"[a{index}]")
        if len(audio) == 1:
            chain = f"{labels[0]}atrim=duration={total_s:.3f},asetpts=PTS-STARTPTS"
        else:
            chain = (
                f"{''.join(labels)}amix=inputs={len(audio)}:normalize=0:"
                f"dropout_transition=0:duration=longest,"
                f"atrim=duration={total_s:.3f},asetpts=PTS-STARTPTS"
            )
        if audio_filter:
            chain = f"{chain},{audio_filter}"
        parts.append(f"{chain}[aout]")
    else:
        parts.append(
            f"anullsrc=channel_layout=mono:sample_rate=44100,"
            f"atrim=duration={total_s:.3f}[aout]"
        )

    return ";".join(parts)


def compose(
    recording: Recording,
    output: Path,
    *,
    crf: int = 23,
    fps: int = 10,
    start_s: float = 0.0,
    end_s: float | None = None,
    audio_only: bool = False,
    normalize: bool = True,
    target_lufs: float = DEFAULT_LUFS,
    target_true_peak: float = DEFAULT_TRUE_PEAK,
    show_progress: bool = True,
) -> ComposeResult:
    ffmpeg = find_ffmpeg()
    total = recording.duration_s
    if start_s >= total:
        raise AcdlError(
            f"--from {_fmt(start_s)} is beyond the recording ({_fmt(total)})"
        )
    if not -45.0 <= target_lufs <= -5.0:
        raise AcdlError("--lufs must be between -45 and -5")
    if not -9.0 <= target_true_peak < 0.0:
        raise AcdlError("--true-peak must be between -9 and 0")
    start_s = max(0.0, start_s)
    end_s = total if end_s is None else min(end_s, total)
    if end_s - start_s <= 0.1:
        raise AcdlError("the selected --from/--to range is empty")

    video_clips = clip_segments(recording.video, start_s, end_s)
    audio_clips = clip_segments(recording.audio, start_s, end_s)
    duration = end_s - start_s

    if not video_clips and not audio_only and not audio_clips:
        raise UnsupportedRecordingError(
            "no audio or video segments fall inside the selected range"
        )
    if not video_clips and not audio_only:
        raise UnsupportedRecordingError(
            "this recording has no screen-share video inside the selected range "
            "(whiteboard-only or PDF-only lessons are stored as vector data, not "
            "video). Re-run with --audio-only to get the audio track."
        )

    width = height = 0
    if video_clips:
        video_clips = _enrich_sizes(video_clips, ffmpeg=ffmpeg)
        if not video_clips:
            raise UnsupportedRecordingError(
                "the screen-share fragments could not be decoded as video"
            )
        width = video_clips[0].width or 1280
        height = video_clips[0].height or 720
        width -= width % 2
        height -= height % 2

    audio_filter: str | None = None
    report = AudioReport(
        normalized=False,
        target_lufs=target_lufs,
        target_true_peak_db=target_true_peak,
    )
    if normalize and audio_clips:
        target = (
            f"loudnorm=I={target_lufs:.1f}:TP={target_true_peak:.1f}:LRA=11"
        )
        print("  measuring audio loudness ...")
        measurement_graph = build_filtergraph(
            [],
            audio_clips,
            width=2,
            height=2,
            fps=fps,
            total_s=duration,
            audio_filter=f"{target}:print_format=json",
        )
        stats: Loudness | None = measure(
            ffmpeg, [clip.path for clip in audio_clips], measurement_graph
        )
        if stats is None:
            print(
                "  warning: loudness measurement failed; using single-pass "
                "normalization",
                file=sys.stderr,
            )
            audio_filter = f"{target}:print_format=json"
            report = AudioReport(
                normalized=True,
                target_lufs=target_lufs,
                target_true_peak_db=target_true_peak,
            )
        else:
            print(
                f"  input loudness {stats.integrated_lufs:.1f} LUFS / "
                f"peak {stats.true_peak_db:+.1f} dBTP"
            )
            audio_filter = (
                f"{target}:"
                f"measured_I={stats.integrated_lufs:.2f}:"
                f"measured_TP={stats.true_peak_db:.2f}:"
                f"measured_LRA={stats.lra:.2f}:"
                f"measured_thresh={stats.threshold_lufs:.2f}:"
                f"offset={stats.offset:.2f}:linear=true:print_format=json"
            )
            report = AudioReport(
                normalized=True,
                target_lufs=target_lufs,
                target_true_peak_db=target_true_peak,
                input_lufs=stats.integrated_lufs,
                input_true_peak_db=stats.true_peak_db,
                input_lra=stats.lra,
            )
        audio_filter = f"{audio_filter},aresample=44100"

    graph = build_filtergraph(
        video_clips,
        audio_clips,
        width=width or 2,
        height=height or 2,
        fps=fps,
        total_s=duration,
        audio_filter=audio_filter,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_name(f"{output.stem}.part{output.suffix}")
    loglevel = "info" if audio_filter else "error"
    command = [ffmpeg, "-hide_banner", "-loglevel", loglevel, "-y", "-nostdin"]
    for clip in video_clips:
        command += ["-i", str(clip.path)]
    for clip in audio_clips:
        command += ["-i", str(clip.path)]
    command += ["-filter_complex", graph]

    if audio_only:
        command += ["-map", "[aout]", "-vn"]
    else:
        command += ["-map", "[vout]", "-map", "[aout]"]

    command += ["-t", f"{duration:.3f}"]
    if not audio_only:
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
        ]
    command += [
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-metadata",
        f"title={recording.title}",
        "-progress",
        "pipe:1",
        "-nostats",
        str(work),
    ]

    try:
        stderr = _run(command, duration_s=duration, show_progress=show_progress)
    except BaseException:
        work.unlink(missing_ok=True)
        raise

    if audio_filter:
        achieved: OutputStats | None = parse_output(stderr)
        if achieved is not None:
            report = replace(
                report,
                output_lufs=achieved.integrated_lufs,
                output_true_peak_db=achieved.true_peak_db,
                normalization_type=achieved.normalization_type,
            )

    if not work.is_file() or work.stat().st_size == 0:
        work.unlink(missing_ok=True)
        raise AcdlError(f"ffmpeg did not produce {output}")

    work.replace(output)
    info = probe(output, ffmpeg=ffmpeg)
    if info.duration_s and abs(info.duration_s - duration) > 2.0:
        print(
            f"warning: output duration {info.duration_s:.1f}s differs from expected "
            f"{duration:.1f}s",
            file=sys.stderr,
        )
    return ComposeResult(path=output, audio=report)


def _enrich_sizes(clips: list[Clip], *, ffmpeg: str) -> list[Clip]:
    enriched: list[Clip] = []
    for clip in clips:
        info: MediaInfo = probe(clip.path, ffmpeg=ffmpeg)
        if not info.has_video:
            continue
        enriched.append(
            Clip(
                path=clip.path,
                offset_s=clip.offset_s,
                duration_s=clip.duration_s,
                width=info.width,
                height=info.height,
            )
        )
    return enriched


def _run(command: list[str], *, duration_s: float, show_progress: bool) -> str:
    is_tty = sys.stderr.isatty()
    progress = _Progress(duration_s, enabled=show_progress and is_tty)
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if not line.startswith("out_time="):
                    continue
                seconds = _parse_hms(line.split("=", 1)[1].strip())
                if seconds is not None:
                    progress.update(seconds)
            returncode = process.wait()
        except KeyboardInterrupt:
            process.kill()
            raise
        progress.finish()
        errors.seek(0)
        text = errors.read()

        if returncode != 0:
            tail = "\n".join(text.splitlines()[-25:])
            raise AcdlError(f"ffmpeg failed (exit {returncode}):\n{tail}")
        return text


class _Progress:
    def __init__(self, duration_s: float, *, enabled: bool) -> None:
        self.duration_s = max(duration_s, 0.001)
        self.enabled = enabled
        self.last_emit = 0.0

    def update(self, seconds: float) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self.last_emit < 0.3:
            return
        self.last_emit = now
        percent = min(100.0, seconds * 100.0 / self.duration_s)
        sys.stderr.write(
            f"\r  encoding {percent:5.1f}%  {_fmt(seconds)} / {_fmt(self.duration_s)}"
        )
        sys.stderr.flush()

    def finish(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _parse_hms(value: str) -> float | None:
    if not value or value in ("N/A", "nan"):
        return None
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
