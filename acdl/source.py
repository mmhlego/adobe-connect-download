"""Turn a recording URL into a downloadable source description."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from .errors import AcdlError, SessionExpiredError, UnsupportedRecordingError
from .httpclient import describe_response, get, looks_like_login, make_session

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCO_RE = re.compile(r"sco-id(?:=|%3D)(\d+)", re.IGNORECASE)
_HTML_CONTENT_RE = re.compile(r"hasHTMLContent=(true|false)", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    origin: str
    room: str
    session: str
    sco_id: str
    title: str
    has_html_content: bool

    @property
    def zip_url(self) -> str:
        return (
            f"{self.origin}/{self.room}/output/{self.sco_id}.zip"
            f"?download=zip&session={quote(self.session, safe='')}"
        )

    @property
    def proto_url(self) -> str:
        return f"{self.origin}/{self.room}/?{urlencode({'session': self.session, 'proto': 'true'})}"

    @property
    def player_url(self) -> str:
        params = {
            "launcher": "false",
            "fcsContent": "true",
            "pbMode": "normal",
            "session": self.session,
        }
        return f"{self.origin}/{self.room}/?{urlencode(params)}"


def parse_url(url: str) -> Source:
    """Split a recording URL into its static parts (no network access)."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AcdlError(f"not an http(s) URL: {url!r}")
    if not parsed.netloc:
        raise AcdlError(f"URL has no host: {url!r}")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise AcdlError(
            "URL has no meeting path; expected something like "
            "https://host/room1234/?session=...&proto=true"
        )
    room = parts[-1]
    if room.lower() == "output" and len(parts) > 1:
        room = parts[-2]

    query = parse_qs(parsed.query)
    session = (query.get("session") or [""])[0].strip()
    if not session:
        raise AcdlError(
            "URL has no session= parameter. Open the recording in your browser, "
            "copy the full address (it should contain session=...) and try again."
        )

    origin = f"{parsed.scheme}://{parsed.netloc}"
    return Source(
        origin=origin,
        room=room,
        session=session,
        sco_id="",
        title=room,
        has_html_content=False,
    )


def resolve(url: str, *, insecure: bool = False, timeout: float = 45) -> tuple[Source, requests.Session]:
    """Parse *url*, fetch the launcher page and fill in sco-id / title.

    Returns the source together with the HTTP session used to fetch it, so the
    cookie set by the launcher page can be reused for the download.
    """
    parsed = parse_url(url)
    session = make_session(insecure=insecure)

    try:
        response = get(session, parsed.proto_url, timeout=timeout)
    except requests.RequestException as exc:
        raise AcdlError(f"could not reach {parsed.proto_url}: {exc}") from exc

    text = response.text
    if response.status_code >= 400:
        raise AcdlError(
            f"the recording page returned {describe_response(response)}; "
            "the session may have expired."
        )
    if looks_like_login(text):
        raise SessionExpiredError(
            "the session was rejected (login or 'Request Not Processed' page). "
            "Open the recording in your browser and copy a fresh link."
        )

    sco_match = _SCO_RE.search(text)
    if not sco_match:
        raise UnsupportedRecordingError(
            "could not find the recording id (sco-id) in the page. "
            "Make sure the URL ends with /?session=...&proto=true."
        )
    sco_id = sco_match.group(1)

    title = _extract_title(text)
    if not title:
        title = _fetch_player_title(session, parsed, timeout=timeout) or parsed.room

    has_html = False
    html_match = _HTML_CONTENT_RE.search(text)
    if html_match:
        has_html = html_match.group(1).lower() == "true"

    if has_html:
        raise UnsupportedRecordingError(
            "this is an HTML5 recording (hasHTMLContent=true). This tool handles "
            "classic Flash recordings only. The recording page may still offer a "
            "ZIP download you can use with 'acdl compose'."
        )

    return (
        Source(
            origin=parsed.origin,
            room=parsed.room,
            session=parsed.session,
            sco_id=sco_id,
            title=title,
            has_html_content=has_html,
        ),
        session,
    )


def _extract_title(text: str) -> str:
    match = _TITLE_RE.search(text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()


def _fetch_player_title(
    session: requests.Session, parsed: Source, *, timeout: float
) -> str:
    params = {
        "launcher": "false",
        "fcsContent": "true",
        "pbMode": "normal",
        "session": parsed.session,
    }
    url = f"{parsed.origin}/{parsed.room}/?{urlencode(params)}"
    try:
        response = get(session, url, timeout=timeout)
    except requests.RequestException:
        return ""
    return _extract_title(response.text)
