"""Small requests wrapper with TLS fallback.

Several on-premise Adobe Connect servers (including the one this tool was
written against) present an incomplete certificate chain. Requests refuses the
connection while browsers/curl often accept it. We retry once without
verification and warn loudly.
"""

from __future__ import annotations

import sys

import requests
import urllib3

from . import __version__

USER_AGENT = f"acdl/{__version__} (Adobe Connect recording downloader)"
_warned_insecure = False


def make_session(*, insecure: bool = False) -> requests.Session:
  session = requests.Session()
  session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
  if insecure:
    _disable_warnings()
  session.verify = not insecure
  return session


def get(
  session: requests.Session,
  url: str,
  *,
  timeout: float = 60,
  stream: bool = False,
  headers: dict[str, str] | None = None,
) -> requests.Response:
  """GET, retrying unverified once if the TLS chain is rejected."""
  global _warned_insecure
  try:
    return session.get(url, timeout=timeout, stream=stream, headers=headers)
  except requests.exceptions.SSLError:
    if not session.verify:
      raise
    if not _warned_insecure:
      print(
        "warning: the server's TLS certificate could not be verified; "
        "retrying without verification (pass --insecure to silence this)",
        file=sys.stderr,
      )
      _warned_insecure = True
    _disable_warnings()
    session.verify = False
    return session.get(url, timeout=timeout, stream=stream, headers=headers)


def _disable_warnings() -> None:
  urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def looks_like_login(text: str) -> bool:
  """Heuristic for Adobe Connect login / access-denied pages."""
  head = text[:8000].lower()
  markers = (
    "adobe connect central login",
    "request not processed",
    "meeting-passcode",
    'name="login"',
  )
  return any(marker in head for marker in markers)


def describe_response(response: requests.Response) -> str:
  if response.status_code == 401:
    return "HTTP 401 (authentication required)"
  if response.status_code == 403:
    return "HTTP 403 (access denied)"
  if response.status_code == 404:
    return "HTTP 404 (not found)"
  return f"HTTP {response.status_code}"
