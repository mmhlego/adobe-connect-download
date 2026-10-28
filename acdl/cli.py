"""Command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .bundle import extract
from .chat import write_chat
from .compose import AudioReport, compose
from .download import download_bundle
from .errors import AcdlError, FfmpegMissingError
from .probe import ffmpeg_version, find_ffmpeg
from .source import Source, resolve
from .timeline import Recording, load_recording
from .util import fmt_duration, parse_time, sanitize_filename

COMMANDS = ("download", "dl", "batch", "compose")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize(argv))
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0

    try:
        ffmpeg = find_ffmpeg()
    except FfmpegMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        return args.handler(args, ffmpeg)
    except AcdlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — partial download kept for resume", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acdl",
        description=(
            "Download a classic Adobe Connect recording and compose its FLV "
            "fragments into one timeline-accurate MP4."
        ),
        epilog=(
            "Examples:\n"
            "  acdl 'https://host/room1234/?session=abc&proto=true'\n"
            "  acdl batch links.txt\n"
            "  acdl compose recording.zip\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"acdl {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    download = subparsers.add_parser(
        "download", aliases=["dl"], help="download one or more recordings"
    )
    download.add_argument("urls", nargs="+", metavar="URL")
    _add_options(download, network=True)
    download.set_defaults(handler=_run_download)

    batch = subparsers.add_parser("batch", help="download every URL in a text file")
    batch.add_argument("file", nargs="?", default="links.txt", metavar="FILE")
    _add_options(batch, network=True)
    batch.set_defaults(handler=_run_batch)

    offline = subparsers.add_parser(
        "compose", help="compose an already-downloaded ZIP or extracted bundle"
    )
    offline.add_argument("source", metavar="ZIP_OR_DIRECTORY")
    _add_options(offline, network=False)
    offline.set_defaults(handler=_run_compose)

    return parser


def _add_options(parser: argparse.ArgumentParser, *, network: bool) -> None:
    parser.add_argument(
        "-o",
        "--output-dir",
        default="downloads",
        metavar="DIR",
        help="where recordings are written (default: downloads)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=23,
        help="x264 quality, lower is better and larger (default: 23)",
    )
    parser.add_argument(
        "--fps", type=int, default=10, help="output frame rate (default: 10)"
    )
    parser.add_argument(
        "--from", dest="start", metavar="TIME", help="start time, e.g. 12:30"
    )
    parser.add_argument(
        "--to", dest="end", metavar="TIME", help="end time, e.g. 45:00"
    )
    parser.add_argument(
        "--audio-only", action="store_true", help="write an .m4a audio track only"
    )
    parser.add_argument(
        "--lufs",
        type=float,
        default=-16.0,
        metavar="N",
        help="loudness target for audio normalization (default: -16)",
    )
    parser.add_argument(
        "--no-normalize",
        dest="normalize",
        action="store_false",
        help="keep the original audio level instead of normalizing",
    )
    parser.add_argument(
        "--chat", action="store_true", help="also write chat.txt"
    )
    parser.add_argument(
        "--keep-raw", action="store_true", help="keep the downloaded ZIP and FLVs"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download and re-encode"
    )
    parser.add_argument(
        "--name", metavar="NAME", help="override the output folder name"
    )
    if network:
        parser.add_argument(
            "--insecure",
            action="store_true",
            help="skip TLS certificate verification (also done automatically as fallback)",
        )


def _run_download(args: argparse.Namespace, ffmpeg: str) -> int:
    if args.name and len(args.urls) > 1:
        print("error: --name can only be used with a single URL", file=sys.stderr)
        return 2
    return _process_many(args.urls, args, ffmpeg)


def _run_batch(args: argparse.Namespace, ffmpeg: str) -> int:
    if args.name:
        print("error: --name cannot be used with batch", file=sys.stderr)
        return 2
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"error: no such links file: {path}", file=sys.stderr)
        return 2
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in urls:
            urls.append(line)
    if not urls:
        print(f"error: {path} contains no links", file=sys.stderr)
        return 2
    return _process_many(urls, args, ffmpeg)


def _process_many(urls: list[str], args: argparse.Namespace, ffmpeg: str) -> int:
    failures = 0
    for index, url in enumerate(urls, start=1):
        if len(urls) > 1:
            print(f"\n[{index}/{len(urls)}] {url}")
        try:
            _process_url(url, args, ffmpeg)
        except AcdlError as exc:
            failures += 1
            print(f"error: {exc}", file=sys.stderr)
    if failures:
        print(
            f"{failures} of {len(urls)} recording(s) failed", file=sys.stderr
        )
        return 1
    return 0


def _process_url(url: str, args: argparse.Namespace, ffmpeg: str) -> Path:
    src, session = resolve(url, insecure=args.insecure)
    title = args.name or src.title
    folder = Path(args.output_dir).expanduser() / sanitize_filename(title)
    audio_only = bool(args.audio_only)
    output = folder / ("recording.m4a" if audio_only else "recording.mp4")

    print(f"  {title}")
    print(f"  room {src.room} · recording {src.sco_id}")

    if output.exists() and not args.force:
        print(f"  skipped (already exists): {output}")
        return output

    raw_dir = folder / "raw"
    zip_path = download_bundle(
        session, src, raw_dir / f"{src.sco_id}.zip", force=args.force
    )
    bundle_dir = extract(zip_path, raw_dir / "extracted", force=args.force)
    recording = load_recording(bundle_dir, title=title)
    _print_recording(recording)

    start, end = _selected_range(args)
    result = compose(
        recording,
        output,
        crf=args.crf,
        fps=args.fps,
        start_s=start or 0.0,
        end_s=end,
        audio_only=audio_only,
        normalize=args.normalize,
        target_lufs=args.lufs,
    )
    output = result.path
    _print_audio(result.audio)

    if args.chat:
        count = write_chat(bundle_dir, folder / "chat.txt", title=title)
        if count:
            print(f"  wrote chat.txt ({count} messages)")
        else:
            print("  no chat messages found")

    _write_metadata(folder, src, recording, output, args, start, end, result.audio, ffmpeg)
    _report_output(output)

    if args.keep_raw:
        print(f"  kept raw assets in {raw_dir}")
    else:
        shutil.rmtree(raw_dir, ignore_errors=True)
    return output


def _run_compose(args: argparse.Namespace, ffmpeg: str) -> int:
    source = Path(args.source).expanduser()
    if not source.exists():
        print(f"error: no such file or directory: {source}", file=sys.stderr)
        return 2

    temporary: Path | None = None
    try:
        if source.is_dir():
            bundle_dir = source
            title = args.name or source.name
        elif source.suffix.lower() == ".zip":
            temporary = Path(tempfile.mkdtemp(prefix="acdl-compose-"))
            bundle_dir = extract(source, temporary, force=True)
            title = args.name or source.stem
        else:
            print("error: expected a .zip file or an extracted directory", file=sys.stderr)
            return 2

        recording = load_recording(bundle_dir, title=title)
        _print_recording(recording)

        folder = Path(args.output_dir).expanduser() / sanitize_filename(title)
        audio_only = bool(args.audio_only)
        output = folder / ("recording.m4a" if audio_only else "recording.mp4")
        if output.exists() and not args.force:
            print(f"  skipped (already exists): {output}")
            return 0

        start, end = _selected_range(args)
        result = compose(
            recording,
            output,
            crf=args.crf,
            fps=args.fps,
            start_s=start or 0.0,
            end_s=end,
            audio_only=audio_only,
            normalize=args.normalize,
            target_lufs=args.lufs,
        )
        output = result.path
        _print_audio(result.audio)
        if args.chat:
            count = write_chat(bundle_dir, folder / "chat.txt", title=title)
            if count:
                print(f"  wrote chat.txt ({count} messages)")
        _write_metadata(
            folder, None, recording, output, args, start, end, result.audio, ffmpeg
        )
        _report_output(output)
        return 0
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _print_recording(recording: Recording) -> None:
    print(
        f"  duration {fmt_duration(recording.duration_s)} · "
        f"{len(recording.video)} screen segment(s) · "
        f"{len(recording.audio)} audio segment(s)"
    )
    if recording.ignored:
        names = ", ".join(segment.name for segment in recording.ignored)
        print(f"  ignoring non-media streams: {names}")


def _selected_range(args: argparse.Namespace) -> tuple[float | None, float | None]:
    try:
        start = parse_time(getattr(args, "start", None))
        end = parse_time(getattr(args, "end", None))
    except ValueError as exc:
        raise AcdlError(str(exc)) from exc
    if start is not None and end is not None and end <= start:
        raise AcdlError("--to must be greater than --from")
    return start, end


def _write_metadata(
    folder: Path,
    src: Source | None,
    recording: Recording,
    output: Path,
    args: argparse.Namespace,
    start: float | None,
    end: float | None,
    audio: AudioReport | None,
    ffmpeg: str,
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
        "range": {"from_s": start or 0.0, "to_s": end},
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


def _print_audio(report: AudioReport | None) -> None:
    if report is None:
        return
    if not report.normalized:
        print("  audio normalization disabled")
        return
    if report.output_lufs is None:
        print(f"  audio normalized to {report.target_lufs:.1f} LUFS")
        return
    peak = (
        f"{report.output_true_peak_db:+.1f}"
        if report.output_true_peak_db is not None
        else "?"
    )
    mode = f", {report.normalization_type}" if report.normalization_type else ""
    print(
        f"  audio normalized to {report.output_lufs:.1f} LUFS "
        f"(peak {peak} dBTP{mode})"
    )


def _report_output(output: Path) -> None:
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"  wrote {output} ({size_mb:.1f} MB)")


def _normalize(argv: list[str] | None) -> list[str]:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and not values[0].startswith("-") and values[0] not in COMMANDS:
        values.insert(0, "download")
    return values


if __name__ == "__main__":
    raise SystemExit(main())
