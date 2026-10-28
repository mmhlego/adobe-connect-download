"""Unpacking and locating files inside the recording bundle."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from zipfile import ZipFile

from .errors import AcdlError

MARKER = ".acdl-extracted.json"


def extract(zip_path: Path, dest: Path, *, force: bool = False) -> Path:
  """Extract *zip_path* into *dest*, reusing a previous identical extraction."""
  dest.parent.mkdir(parents=True, exist_ok=True)
  stamp = {
    "name": zip_path.name,
    "size": zip_path.stat().st_size,
    "mtime": int(zip_path.stat().st_mtime),
  }
  marker = dest / MARKER

  if not force and marker.is_file():
    try:
      previous = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      previous = None
    if previous == stamp and find_mainstream(dest):
      print(f"  using cached extraction {dest}")
      return dest

  if dest.exists():
    shutil.rmtree(dest)
  dest.mkdir(parents=True)

  with ZipFile(zip_path) as archive:
    try:
      _safe_extract(archive, dest)
    except (zipfile.BadZipFile, EOFError, ValueError) as exc:
      raise AcdlError(
        f"the bundle {zip_path.name} is corrupt ({exc}); delete it and download again"
      ) from exc

  marker.write_text(json.dumps(stamp), encoding="utf-8")
  if not find_mainstream(dest):
    raise AcdlError(
      "the bundle does not contain mainstream.xml; this does not look like "
      "a classic Adobe Connect recording."
    )
  return dest


def _safe_extract(archive: ZipFile, dest: Path) -> None:
  root = dest.resolve()
  for info in archive.infolist():
    name = info.filename
    if not name or name.endswith("/") or name.startswith("__MACOSX/"):
      continue
    target = (root / name).resolve()
    if root != target and root not in target.parents:
      raise AcdlError(f"refusing to extract unsafe path from bundle: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, target.open("wb") as handle:
      shutil.copyfileobj(source, handle)


def find_mainstream(directory: Path) -> Path | None:
  direct = directory / "mainstream.xml"
  if direct.is_file():
    return direct
  for candidate in directory.glob("*/mainstream.xml"):
    if candidate.is_file():
      return candidate
  for candidate in directory.rglob("mainstream.xml"):
    if candidate.is_file():
      return candidate
  return None
