"""Exceptions with messages meant for humans."""


class AcdlError(Exception):
    """Base class for expected failures."""


class SessionExpiredError(AcdlError):
    """The session token is missing, expired, or no longer grants access."""


class UnsupportedRecordingError(AcdlError):
    """The recording exists but this tool cannot process it."""


class FfmpegMissingError(AcdlError):
    """ffmpeg/ffprobe are required but could not be found."""
