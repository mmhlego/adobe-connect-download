"""Textual interface: paste links, watch a stable queue, see progress and ETA."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from rich.progress_bar import ProgressBar
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
  Button,
  DataTable,
  Footer,
  Header,
  RichLog,
  Static,
  TextArea,
)

from ..errors import AcdlError, FfmpegMissingError
from ..probe import find_ffmpeg
from ..service import STAGE_LABELS, Options, Progress, Result, process_url
from ..util import fmt_duration

COLUMNS = (
  ("#", 3),
  ("Title", 26),
  ("Step", 12),
  ("Progress", 20),
  ("Detail", 24),
  ("Elapsed", 8),
  ("ETA", 8),
  ("Status", 9),
)


@dataclass
class QueueItem:
  number: int
  url: str
  title: str
  stage: str = "queued"
  status: str = "queued"
  fraction: float = 0.0
  detail: str = ""
  eta_s: float | None = None
  started: float | None = None
  elapsed_s: float = 0.0
  error: str = ""

  @property
  def key(self) -> str:
    return f"item-{self.number}"


class AcdlApp(App):
  """Queue-based downloader."""

  TITLE = "acdl"
  SUB_TITLE = "Adobe Connect recording downloader"

  CSS = """
  Screen { layout: vertical; }
  #urls { height: 6; border: round $primary; }
  #buttons { height: auto; padding: 0 1; }
  #buttons Button { margin-right: 1; }
  #queue { height: 1fr; border: round $primary; }
  #log { height: 9; border: round $primary; }
  #dep { padding: 2 4; }
  """

  BINDINGS = [
    Binding("ctrl+q", "quit", "Quit"),
    Binding("ctrl+n", "add_links", "Add links"),
    Binding("ctrl+l", "load_links", "Load links.txt"),
    Binding("ctrl+r", "start_queue", "Start"),
    Binding("escape", "stop_queue", "Stop"),
  ]

  def __init__(self, *, ffmpeg: str | None, error: str | None = None) -> None:
    super().__init__()
    self.ffmpeg = ffmpeg
    self.ffmpeg_error = error or ""
    self.items: list[QueueItem] = []
    self._seen: set[str] = set()
    self._processing = False
    self._stop_after_current = False
    self._batch: list[QueueItem] = []
    self._table: DataTable | None = None

  # ------------------------------------------------------------------ layout

  def compose(self) -> ComposeResult:
    yield Header(show_clock=True)
    if self.ffmpeg is None:
      yield Static(self._dependency_text(), id="dep", markup=True)
    else:
      yield TextArea(
        id="urls",
        placeholder="Paste recording URLs here, one per line, then choose Add to queue.",
      )
      with Horizontal(id="buttons"):
        yield Button("Add to queue", id="add", variant="primary")
        yield Button("Load links.txt", id="load")
        yield Button("Start", id="start", variant="success")
        yield Button("Stop", id="stop", variant="error", disabled=True)
      yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)
      yield RichLog(id="log", markup=True, wrap=True)
    yield Footer()

  def on_mount(self) -> None:
    if self.ffmpeg is None:
      return
    table = self.query_one("#queue", DataTable)
    for label, width in COLUMNS:
      table.add_column(label, width=width, key=label.lower())
    self._table = table
    self.set_interval(1.0, self._tick)
    self.query_one("#urls", TextArea).focus()
    self._log("Ready. Paste URLs or load links.txt, then press Start.")
    self._log("Output goes to ./downloads. Ctrl+Q quits.")

  # ----------------------------------------------------------------- actions

  def action_add_links(self) -> None:
    if self.ffmpeg is None:
      return
    self.query_one("#urls", TextArea).focus()

  def action_load_links(self) -> None:
    self._load_links()

  def action_start_queue(self) -> None:
    self._start()

  def action_stop_queue(self) -> None:
    self._stop()

  @on(Button.Pressed, "#add")
  def _add_pressed(self) -> None:
    self._enqueue_from_input()

  @on(Button.Pressed, "#load")
  def _load_pressed(self) -> None:
    self._load_links()

  @on(Button.Pressed, "#start")
  def _start_pressed(self) -> None:
    self._start()

  @on(Button.Pressed, "#stop")
  def _stop_pressed(self) -> None:
    self._stop()

  def _enqueue_from_input(self) -> None:
    if self.ffmpeg is None:
      return
    input_widget = self.query_one("#urls", TextArea)
    added = self._enqueue(input_widget.text)
    if added:
      input_widget.clear()
      self._log(f"Added {added} link(s) to the queue.")
    else:
      self._log("No new links found in the input box.")

  def _load_links(self) -> None:
    if self.ffmpeg is None:
      return
    path = Path.cwd() / "links.txt"
    if not path.is_file():
      self._log(f"No links.txt in {Path.cwd()}.")
      return
    try:
      text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
      self._log(f"Could not read {path}: {exc}")
      return
    added = self._enqueue(text)
    self._log(f"Added {added} link(s) from {path.name}.")

  def _enqueue(self, text: str) -> int:
    added = 0
    for raw in text.splitlines():
      url = raw.strip()
      if not url or url.startswith("#"):
        continue
      if not url.startswith(("http://", "https://")):
        self._log(f"Skipped invalid URL: {_short(url, 60)}")
        continue
      if url in self._seen:
        self._log(f"Already queued: {_short(url, 60)}")
        continue
      self._seen.add(url)
      item = QueueItem(
        number=len(self.items) + 1,
        url=url,
        title=_guess_title(url),
      )
      self.items.append(item)
      if self._table is not None:
        self._table.add_row(*self._cells(item), key=item.key)
      added += 1
    return added

  def _start(self) -> None:
    if self.ffmpeg is None or self._processing:
      return
    self._batch = [
      item for item in self.items if item.status in ("queued", "error", "cancelled")
    ]
    if not self._batch:
      self._log("Nothing to do: add a link first.")
      return
    self._processing = True
    self._stop_after_current = False
    self.query_one("#start", Button).disabled = True
    self.query_one("#stop", Button).disabled = False
    self._log(f"Starting {len(self._batch)} recording(s).")
    self._run_queue()

  def _stop(self) -> None:
    if not self._processing:
      return
    self._stop_after_current = True
    self._log("Stopping after the current recording…")

  # ------------------------------------------------------------------ worker

  @work(thread=True, exclusive=True)
  def _run_queue(self) -> None:
    for item in self._batch:
      if self._stop_after_current:
        self._call(self._mark_cancelled, item)
        continue
      self._call(self._mark_running, item)
      reporter = _TuiReporter(self, item)
      try:
        result = process_url(item.url, self.options, reporter, self.ffmpeg or "")
      except AcdlError as exc:
        self._call(self._mark_error, item, str(exc))
      except Exception as exc:  # noqa: BLE001 - surfaced in the queue row
        self._call(self._mark_error, item, f"{type(exc).__name__}: {exc}")
      else:
        self._call(self._mark_done, item, result)
    self._call(self._finish_queue)

  @property
  def options(self) -> Options:
    return Options()

  def _call(self, callback, *args) -> None:
    try:
      self.call_from_thread(callback, *args)
    except RuntimeError:
      pass

  # ------------------------------------------------------------ item updates

  def _cells(self, item: QueueItem) -> list:
    return [
      str(item.number),
      _short(item.title, 26),
      STAGE_LABELS.get(item.stage, item.stage),
      _bar(item.fraction),
      _short(item.detail, 24),
      _elapsed(item.elapsed_s),
      _eta(item.eta_s),
      item.status,
    ]

  def _apply(self, item: QueueItem) -> None:
    if self._table is None:
      return
    try:
      self._table.update_cell(item.key, "title", _short(item.title, 26))
      self._table.update_cell(
        item.key, "step", STAGE_LABELS.get(item.stage, item.stage)
      )
      self._table.update_cell(item.key, "progress", _bar(item.fraction))
      self._table.update_cell(item.key, "detail", _short(item.detail, 24))
      self._table.update_cell(item.key, "elapsed", _elapsed(item.elapsed_s))
      self._table.update_cell(item.key, "eta", _eta(item.eta_s))
      self._table.update_cell(item.key, "status", item.status)
    except Exception:  # noqa: BLE001 - the app may be shutting down
      pass

  def _mark_running(self, item: QueueItem) -> None:
    item.started = time.monotonic()
    item.elapsed_s = 0.0
    item.status = "running"
    item.stage = "resolving"
    item.fraction = 0.0
    item.detail = ""
    item.eta_s = None
    self._apply(item)

  def _mark_done(self, item: QueueItem, result: Result) -> None:
    if item.started is not None:
      item.elapsed_s = time.monotonic() - item.started
    item.status = "skipped" if result.skipped else "done"
    item.stage = item.status
    item.fraction = 1.0
    item.detail = "" if result.skipped else fmt_duration(result.duration_s)
    item.eta_s = 0.0
    self._apply(item)
    self._log(f"[{item.number}] {item.title}: {item.status} → {result.path}")

  def _mark_error(self, item: QueueItem, message: str) -> None:
    if item.started is not None:
      item.elapsed_s = time.monotonic() - item.started
    item.status = "error"
    item.stage = "error"
    item.detail = _short(message, 24)
    item.eta_s = None
    self._apply(item)
    self._log(f"[{item.number}] error: {message}")

  def _mark_cancelled(self, item: QueueItem) -> None:
    item.status = "cancelled"
    item.stage = "cancelled"
    item.eta_s = None
    self._apply(item)
    self._log(f"[{item.number}] cancelled.")

  def _finish_queue(self) -> None:
    self._processing = False
    self._batch = []
    try:
      self.query_one("#start", Button).disabled = False
      self.query_one("#stop", Button).disabled = True
    except Exception:  # noqa: BLE001 - the app may be shutting down
      pass
    self._log("Queue finished.")

  def _tick(self) -> None:
    now = time.monotonic()
    for item in self.items:
      if item.status == "running" and item.started is not None:
        item.elapsed_s = now - item.started
        if self._table is not None:
          try:
            self._table.update_cell(item.key, "elapsed", _elapsed(item.elapsed_s))
            self._table.update_cell(item.key, "eta", _eta(item.eta_s))
          except Exception:  # noqa: BLE001 - the app may be shutting down
            pass

  # reporter callbacks (always invoked on the UI thread)

  def _on_title(self, item: QueueItem, title: str) -> None:
    item.title = title
    self._apply(item)

  def _on_stage(self, item: QueueItem, stage: str) -> None:
    item.stage = stage
    if stage == "done":
      item.fraction = 1.0
    self._apply(item)

  def _on_progress(self, item: QueueItem, progress: Progress) -> None:
    if progress.fraction is not None:
      item.fraction = progress.fraction
    item.detail = progress.detail
    item.eta_s = progress.eta_s
    self._apply(item)

  def _on_log(self, item: QueueItem, message: str) -> None:
    self._log(f"[{item.number}] {message}")

  # ------------------------------------------------------------------- misc

  def _log(self, message: str) -> None:
    try:
      self.query_one("#log", RichLog).write(message)
    except Exception:  # noqa: BLE001 - the log widget may not exist yet
      pass

  def _dependency_text(self) -> str:
    return (
      "[bold red]ffmpeg was not found[/]\n\n"
      "acdl needs [bold]ffmpeg[/] and [bold]ffprobe[/] on your PATH.\n\n"
      "[bold]macOS[/]\n"
      "  brew install ffmpeg\n\n"
      "[bold]Windows[/]\n"
      "  winget install Gyan.FFmpeg\n"
      "  (or) choco install ffmpeg\n\n"
      "[bold]Linux[/]\n"
      "  sudo apt install ffmpeg      # Debian / Ubuntu\n"
      "  sudo dnf install ffmpeg      # Fedora\n\n"
      "Install it, then reopen acdl.\n"
      "If ffmpeg lives elsewhere, set [bold]ACDL_FFMPEG[/] to the binary path.\n\n"
      f"[dim]{self.ffmpeg_error}[/]"
    )


class _TuiReporter:
  """Bridges service events from the worker thread onto the UI thread."""

  def __init__(self, app: AcdlApp, item: QueueItem) -> None:
    self.app = app
    self.item = item
    self.last_emit = 0.0

  def title(self, title: str) -> None:
    self.app._call(self.app._on_title, self.item, title)

  def stage(self, stage: str) -> None:
    self.app._call(self.app._on_stage, self.item, stage)

  def progress(self, progress: Progress) -> None:
    now = time.monotonic()
    if now - self.last_emit < 0.12:
      return
    self.last_emit = now
    self.app._call(self.app._on_progress, self.item, progress)

  def log(self, message: str) -> None:
    self.app._call(self.app._on_log, self.item, message)


def _bar(fraction: float | None) -> ProgressBar:
  completed = 0.0 if fraction is None else min(max(fraction, 0.0), 1.0)
  return ProgressBar(total=1.0, completed=completed, width=16)


def _short(text: str, width: int) -> str:
  if len(text) <= width:
    return text
  return text[: max(0, width - 1)] + "…"


def _elapsed(seconds: float) -> str:
  return fmt_duration(seconds)


def _eta(seconds: float | None) -> str:
  if seconds is None:
    return "—"
  return fmt_duration(seconds)


def _guess_title(url: str) -> str:
  parsed = urlparse(url)
  parts = [part for part in parsed.path.split("/") if part]
  room = parts[-1] if parts else ""
  if room.lower() == "output" and len(parts) > 1:
    room = parts[-2]
  host = parsed.netloc or url
  return f"{host}/{room}" if room else host


def run_tui() -> int:
  try:
    ffmpeg = find_ffmpeg()
  except FfmpegMissingError as exc:
    ffmpeg = None
    error = str(exc)
  else:
    error = None
  AcdlApp(ffmpeg=ffmpeg, error=error).run()
  return 0
