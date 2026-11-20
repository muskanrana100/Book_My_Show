"""
Validation helpers for movie trailers and poster images (Task 1).

Trailers: only YouTube URLs in explicitly supported formats, or a bare 11-character video ID,
are accepted. The ID is parsed and validated on the server. Anything else (javascript:, data:,
other domains, malformed URLs) is rejected, so no arbitrary URL can reach an iframe.

Posters: JPEG, PNG or WebP only, at most 5 MB, and the file content must really be an image
(checked with Pillow, not just the file name).
"""
import re
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import FileExtensionValidator

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hosts accepted for trailer URLs. Only these exact hostnames are allowed.
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

# Path prefixes that carry the video ID as their second segment (e.g. /embed/<id>).
YOUTUBE_PATH_PREFIXES = ("/embed/", "/shorts/", "/live/")

MAX_POSTER_BYTES = 5 * 1024 * 1024
ALLOWED_POSTER_FORMATS = {"JPEG", "PNG", "WEBP"}


def parse_youtube_id(value):
    """
    Return the 11-character YouTube video ID for a supported input, '' for an empty input,
    or None when the input is not a supported YouTube reference.

    Supported:
      - a bare video ID, e.g. dQw4w9WgXcQ
      - https://www.youtube.com/watch?v=<id>  (extra query parameters are ignored)
      - https://youtu.be/<id>
      - https://www.youtube.com/embed/<id>
      - https://www.youtube.com/shorts/<id> and /live/<id>
    """
    value = (value or "").strip()
    if not value:
        return ""
    if YOUTUBE_ID_RE.match(value):
        return value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    # Require an explicit http(s) scheme. This rejects javascript:, data:, and scheme-less input.
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    path = parsed.path or ""
    candidate = None
    if host.endswith("youtu.be"):
        segments = [s for s in path.split("/") if s]
        candidate = segments[0] if segments else None
    elif path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [None])[0]
    elif path.startswith(YOUTUBE_PATH_PREFIXES):
        segments = [s for s in path.split("/") if s]
        candidate = segments[1] if len(segments) >= 2 else None

    if candidate and YOUTUBE_ID_RE.match(candidate):
        return candidate
    return None


def validate_trailer_reference(value):
    """Model/form validator: the stored value must be empty or a valid 11-character ID."""
    if value and not YOUTUBE_ID_RE.match(value):
        raise ValidationError(
            "Enter a YouTube link (youtube.com/watch?v=..., youtu.be/...) or the 11-character video ID."
        )


def validate_poster_size(file):
    # Only new uploads are checked. A poster already stored (possibly missing from disk) is left alone,
    # so editing an existing movie in the admin does not fail on its old image.
    if not isinstance(file, UploadedFile):
        return
    if file.size and file.size > MAX_POSTER_BYTES:
        raise ValidationError("Poster images must be 5 MB or smaller.")


def validate_poster_content(file):
    """Checks the real image format with Pillow so renamed files are rejected (new uploads only)."""
    from PIL import Image, UnidentifiedImageError

    if not isinstance(file, UploadedFile):
        return

    position = file.tell() if hasattr(file, "tell") else None
    try:
        file.seek(0)
        with Image.open(file) as img:
            fmt = (img.format or "").upper()
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValidationError("The uploaded file is not a valid image.") from exc
    finally:
        if position is not None:
            file.seek(position)
    if fmt not in ALLOWED_POSTER_FORMATS:
        raise ValidationError("Poster images must be JPEG, PNG or WebP.")


POSTER_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "webp"]),
    validate_poster_size,
    validate_poster_content,
]
