"""Resumable download of the recording ZIP bundle.

Note on resuming: this endpoint builds the ZIP on demand, so the bytes can
differ between two requests. Blindly appending a Range response to an earlier
partial file produces a corrupt archive. We therefore re-fetch a small overlap
before the resume point, compare it with what is already on disk, and restart
from zero if the server produced a different archive. The finished file is also
CRC-checked before it is accepted.
"""

from __future__ import annotations

import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import requests

from .errors import AcdlError, SessionExpiredError
from .httpclient import describe_response, get, looks_like_login
from .source import Source

CHUNK = 256 * 1024
OVERLAP = 64 * 1024
MAX_ATTEMPTS = 5


class _Progress:
  def __init__(
    self,
    *,
    show: bool = True,
    on_progress: Callable[[int, int | None, float], None] | None = None,
  ) -> None:
    self.show = show and sys.stderr.isatty()
    self.on_progress = on_progress
    self.last_emit = 0.0
    self.start = time.monotonic()
    self.total: int | None = None

  def reset(self) -> None:
    self.last_emit = 0.0
    self.start = time.monotonic()

  def update(self, done: int, total: int | None, *, force: bool = False) -> None:
    self.total = total
    now = time.monotonic()
    if not force and now - self.last_emit < 0.15:
      return
    self.last_emit = now
    elapsed = max(now - self.start, 1e-6)
    speed = done / elapsed
    if self.on_progress is not None:
      self.on_progress(done, total, speed)
      return
    if not self.show:
      return
    if total:
      percent = min(100.0, done * 100.0 / total)
      sys.stderr.write(
        f"\r  downloading {_mb(done)}/{_mb(total)} MB "
        f"({percent:4.1f}%) {_mb(speed)} MB/s"
      )
    else:
      sys.stderr.write(f"\r  downloading {_mb(done)} MB {_mb(speed)} MB/s")
    sys.stderr.flush()

  def done(self) -> None:
    if self.show and self.on_progress is None:
      sys.stderr.write("\n")
      sys.stderr.flush()


def _mb(value: float) -> str:
  return f"{value / (1024 * 1024):.1f}"


def download_bundle(
  session: requests.Session,
  src: Source,
  dest: Path,
  *,
  force: bool = False,
  show_progress: bool = True,
  timeout: float = 60,
  resume: bool = True,
  on_progress: Callable[[int, int | None, float], None] | None = None,
  on_log: Callable[[str], None] | None = None,
) -> Path:
  """Download the bundle to *dest*, resuming only when the archive matches."""
  dest.parent.mkdir(parents=True, exist_ok=True)

  if dest.exists() and not force:
    problem = _validate(dest)
    if problem is None:
      _emit(on_log, f"using cached bundle {dest}")
      return dest
    _emit(on_log, f"cached bundle is corrupt ({problem}); downloading again")
    dest.unlink()

  partial = dest.with_name(dest.name + ".part")
  if force or not resume:
    partial.unlink(missing_ok=True)

  progress = _Progress(show=show_progress, on_progress=on_progress)
  last_error: Exception | None = None

  for attempt in range(1, MAX_ATTEMPTS + 1):
    progress.reset()
    try:
      complete = _transfer(session, src, partial, timeout=timeout, progress=progress)
    except SessionExpiredError:
      progress.done()
      raise
    except (requests.RequestException, OSError) as exc:
      progress.done()
      last_error = exc
      if attempt < MAX_ATTEMPTS:
        _sleep(attempt, on_log=on_log)
      continue

    progress.done()

    if complete:
      problem = _validate(partial)
      if problem is None:
        partial.replace(dest)
        return dest
      _emit(on_log, f"bundle was corrupt ({problem}); restarting download")
      partial.unlink(missing_ok=True)
      last_error = AcdlError(problem)
      continue

    if attempt < MAX_ATTEMPTS:
      _sleep(attempt, on_log=on_log)

  raise AcdlError(f"download failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _transfer(
  session: requests.Session,
  src: Source,
  partial: Path,
  *,
  timeout: float,
  progress: _Progress,
) -> bool:
  """One download attempt. Returns True when the file is fully written."""
  # Two passes at most: the second starts from zero if the resume point did
  # not match the archive the server just generated.
  for _ in range(2):
    offset = partial.stat().st_size if partial.exists() else 0
    overlap = min(offset, OVERLAP)
    start = offset - overlap
    headers = {"Range": f"bytes={start}-"} if offset else {}
    headers["Accept-Encoding"] = "identity"

    try:
      response = get(
        session, src.zip_url, timeout=timeout, stream=True, headers=headers
      )
    except requests.RequestException:
      raise

    try:
      if response.status_code == 416 and offset:
        if _validate(partial) is None:
          return True
        partial.unlink(missing_ok=True)
        continue

      if response.status_code >= 400:
        raise AcdlError(f"bundle download failed: {describe_response(response)}")

      content_type = (response.headers.get("Content-Type") or "").lower()
      if "text/html" in content_type or "text/xml" in content_type:
        snippet = response.text[:8000]
        if looks_like_login(snippet):
          raise SessionExpiredError(
            "the server returned a login page instead of the bundle; "
            "the session has expired. Grab a fresh recording link."
          )
        raise AcdlError(
          "the server returned an unexpected page instead of a ZIP bundle "
          f"({describe_response(response)})."
        )

      total = _total_size(response, start)

      if response.status_code == 206 and offset:
        iterator = response.iter_content(CHUNK)
        head = _read_exact(iterator, overlap)
        if head is None or not _tail_matches(partial, head[:overlap]):
          # The server rebuilt the archive; the old prefix is unusable.
          partial.unlink(missing_ok=True)
          continue

        progress.update(offset, total, force=True)
        with partial.open("r+b") as handle:
          handle.seek(offset)
          written = offset
          if len(head) > overlap:
            handle.write(head[overlap:])
            written += len(head) - overlap
          written = _drain(iterator, handle, progress, written, total)
        return True

      # No usable resume (fresh download, or the server ignored Range).
      total = _total_size(response, 0)
      progress.update(0, total, force=True)
      with partial.open("wb") as handle:
        written = _drain(response.iter_content(CHUNK), handle, progress, 0, total)
      return True
    finally:
      response.close()

  return False


def _read_exact(iterator, size: int) -> bytes | None:
  if size <= 0:
    return b""
  buffer = bytearray()
  while len(buffer) < size:
    try:
      chunk = next(iterator)
    except StopIteration:
      return None
    if not chunk:
      continue
    buffer.extend(chunk)
  return bytes(buffer)


def _tail_matches(path: Path, head: bytes) -> bool:
  size = len(head)
  if size == 0:
    return True
  try:
    with path.open("rb") as handle:
      handle.seek(-size, 2)
      return handle.read(size) == head
  except OSError:
    return False


def _drain(
  iterator, handle, progress: _Progress, written: int, total: int | None
) -> int:
  for chunk in iterator:
    if not chunk:
      continue
    handle.write(chunk)
    written += len(chunk)
    progress.update(written, total)
  return written


def _total_size(response: requests.Response, offset: int) -> int | None:
  content_range = response.headers.get("Content-Range", "")
  if "/" in content_range:
    tail = content_range.rsplit("/", 1)[-1].strip()
    if tail.isdigit():
      return int(tail)
  length = response.headers.get("Content-Length")
  if length and length.isdigit():
    return offset + int(length)
  return None


def _validate(path: Path) -> str | None:
  """Return a human-readable problem, or None when the archive is intact."""
  if not path.exists():
    return "download is missing"
  try:
    with zipfile.ZipFile(path) as archive:
      if not archive.namelist():
        return "archive is empty"
      bad = archive.testzip()
      if bad is not None:
        return f"checksum mismatch in {bad}"
  except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
    return str(exc) or "invalid ZIP archive"
  return None


def _sleep(attempt: int, *, on_log: Callable[[str], None] | None = None) -> None:
  delay = min(2 ** (attempt - 1), 10)
  _emit(on_log, f"retrying in {delay}s ...")
  time.sleep(delay)


def _emit(on_log: Callable[[str], None] | None, message: str) -> None:
  if on_log is not None:
    on_log(message)
    return
  print(f"  {message}")
