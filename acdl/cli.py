"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import AcdlError, FfmpegMissingError
from .probe import find_ffmpeg
from .service import Options, Progress, Result, compose_existing, process_url
from .util import parse_time

COMMANDS = ("download", "dl", "batch", "compose", "tui")


class TextReporter:
  """Reporter that prints the original CLI output."""

  def __init__(self) -> None:
    self._progress_line = False

  def title(self, title: str) -> None:
    self.log(title)

  def stage(self, stage: str) -> None:
    pass

  def progress(self, progress: Progress) -> None:
    if not progress.detail:
      return
    sys.stderr.write(f"\r  {progress.detail}")
    sys.stderr.flush()
    self._progress_line = True

  def log(self, message: str) -> None:
    self._end_progress()
    print(f"  {message}")

  def finish(self) -> None:
    self._end_progress()

  def _end_progress(self) -> None:
    if self._progress_line:
      sys.stderr.write("\n")
      sys.stderr.flush()
      self._progress_line = False


def main(argv: list[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(_normalize(argv))
  if getattr(args, "command", None) is None:
    if _interactive():
      return _run_tui()
    parser.print_help()
    return 0

  if args.command == "tui":
    return _run_tui()

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
      "  acdl                                   open the interactive TUI\n"
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

  tui = subparsers.add_parser("tui", help="open the interactive interface")
  tui.set_defaults(handler=None)

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
  parser.add_argument("--to", dest="end", metavar="TIME", help="end time, e.g. 45:00")
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
  parser.add_argument("--chat", action="store_true", help="also write chat.txt")
  parser.add_argument(
    "--keep-raw", action="store_true", help="keep the downloaded ZIP and FLVs"
  )
  parser.add_argument("--force", action="store_true", help="re-download and re-encode")
  parser.add_argument("--name", metavar="NAME", help="override the output folder name")
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
  reporter = TextReporter()
  options = _options(args)
  failures = 0
  for index, url in enumerate(urls, start=1):
    if len(urls) > 1:
      reporter.log(f"[{index}/{len(urls)}] {url}")
    try:
      process_url(url, options, reporter, ffmpeg)
    except AcdlError as exc:
      failures += 1
      reporter.finish()
      print(f"error: {exc}", file=sys.stderr)
  reporter.finish()
  if failures:
    print(f"{failures} of {len(urls)} recording(s) failed", file=sys.stderr)
    return 1
  return 0


def _run_compose(args: argparse.Namespace, ffmpeg: str) -> int:
  source = Path(args.source).expanduser()
  if not source.exists():
    print(f"error: no such file or directory: {source}", file=sys.stderr)
    return 2

  reporter = TextReporter()
  try:
    result: Result = compose_existing(source, _options(args), reporter, ffmpeg)
  finally:
    reporter.finish()
  return 0 if result.path.exists() else 1


def _options(args: argparse.Namespace) -> Options:
  try:
    start = parse_time(getattr(args, "start", None))
    end = parse_time(getattr(args, "end", None))
  except ValueError as exc:
    raise AcdlError(str(exc)) from exc
  if start is not None and end is not None and end <= start:
    raise AcdlError("--to must be greater than --from")
  return Options(
    output_dir=Path(getattr(args, "output_dir", "downloads")),
    crf=getattr(args, "crf", 23),
    fps=getattr(args, "fps", 10),
    start=start,
    end=end,
    audio_only=bool(getattr(args, "audio_only", False)),
    normalize=bool(getattr(args, "normalize", True)),
    lufs=getattr(args, "lufs", -16.0),
    chat=bool(getattr(args, "chat", False)),
    keep_raw=bool(getattr(args, "keep_raw", False)),
    force=bool(getattr(args, "force", False)),
    name=getattr(args, "name", None),
    insecure=bool(getattr(args, "insecure", False)),
  )


def _run_tui() -> int:
  if not _interactive():
    print("error: the interactive interface needs a terminal", file=sys.stderr)
    return 2
  try:
    import textual  # noqa: F401
  except ImportError:
    print(
      "error: the interactive interface needs the 'textual' package.\n"
      "  install it with: pip install 'acdl[tui]'",
      file=sys.stderr,
    )
    return 2
  from .tui.app import run_tui

  return run_tui()


def _interactive() -> bool:
  return sys.stdin.isatty() and sys.stdout.isatty()


def _normalize(argv: list[str] | None) -> list[str]:
  values = list(sys.argv[1:] if argv is None else argv)
  if values and not values[0].startswith("-") and values[0] not in COMMANDS:
    values.insert(0, "download")
  return values


if __name__ == "__main__":
  raise SystemExit(main())
