"""EBU R128 loudness measurement helpers built on ffmpeg's loudnorm filter."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float
    true_peak_db: float
    lra: float
    threshold_lufs: float
    offset: float


@dataclass(frozen=True)
class OutputStats:
    integrated_lufs: float | None
    true_peak_db: float | None
    normalization_type: str | None


def _last_json(text: str) -> dict | None:
    index = text.rfind('"input_i"')
    if index < 0:
        return None
    start = text.rfind("{", 0, index)
    end = text.find("}", index)
    if start < 0 or end < 0:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _number(payload: dict, key: str) -> float | None:
    value = payload.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_measurement(text: str) -> Loudness | None:
    """Read the input_* fields from a loudnorm `print_format=json` block."""
    payload = _last_json(text)
    if payload is None:
        return None
    integrated = _number(payload, "input_i")
    peak = _number(payload, "input_tp")
    lra = _number(payload, "input_lra")
    threshold = _number(payload, "input_thresh")
    offset = _number(payload, "target_offset")
    if integrated is None or peak is None:
        return None
    return Loudness(
        integrated_lufs=integrated,
        true_peak_db=peak,
        lra=lra if lra is not None else 0.0,
        threshold_lufs=threshold if threshold is not None else -70.0,
        offset=offset if offset is not None else 0.0,
    )


def parse_output(text: str) -> OutputStats | None:
    """Read the output_i / output_tp fields reported after encoding."""
    payload = _last_json(text)
    if payload is None:
        return None
    kind = payload.get("normalization_type")
    return OutputStats(
        integrated_lufs=_number(payload, "output_i"),
        true_peak_db=_number(payload, "output_tp"),
        normalization_type=kind if isinstance(kind, str) else None,
    )


def measure(
    ffmpeg: str, inputs: list[Path], graph: str
) -> Loudness | None:
    """Run an audio-only graph ending in [aout] and return its loudness."""
    if not inputs:
        return None
    command = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-y"]
    for path in inputs:
        command += ["-i", str(path)]
    command += ["-filter_complex", graph, "-map", "[aout]", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_measurement(result.stderr or "")
