"""
Server-side validation for uploaded files.

The bare extension allowlist that used to gate uploads accepted three classes
of malicious requests:

1. **Renamed binaries** — ``evil.exe`` → ``evil.png`` slips through because we
   only checked the suffix, never the bytes.
2. **Lying ``Content-Type`` headers** — multipart clients can claim any MIME
   type they want, and we used to persist that string straight to the DB and
   serve it back from MinIO.
3. **Browser-renderable formats hosted same-origin** — ``.html``/``.js``/``.css``
   uploads turn the upload bucket into an XSS hosting platform when fetched
   from the same origin as the chat UI.

This module fixes all three: it sniffs the actual content type with libmagic
and matches it against an explicit per-extension allowlist. Callers should
treat ``ValidationError`` as "reject the upload" and persist the returned
``sniffed_mime`` rather than the client-supplied one. ``.html``/``.js``/``.css``/
``.ts`` are no longer in the upload allowlist; share code via fenced-code
markdown messages instead.

libmagic versions vary (esp. on text/source files) so we accept a small set
of equivalent MIME strings per extension and fall back to a "starts-with
text/" check for plain-text formats.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import magic

# Extensions that map to a single binary type. The set value is the collection
# of libmagic MIME strings considered acceptable for that extension.
EXTENSION_MIME_MAP: dict[str, frozenset[str]] = {
    "png": frozenset({"image/png"}),
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "gif": frozenset({"image/gif"}),
    "pdf": frozenset({"application/pdf"}),
    "zip": frozenset({"application/zip", "application/x-zip-compressed"}),
}

# Extensions for plain-text content. libmagic's reported subtype varies a lot
# here (``text/x-python`` on one box, ``text/plain`` on another for the same
# file), so we accept any ``text/*``. Source files masquerading as something
# binary still get caught.
TEXT_EXTENSIONS: frozenset[str] = frozenset({"txt", "md", "py"})

# The complete set of extensions accepted on upload. Everything callers see
# should derive from this constant — don't maintain parallel lists in the
# upload blueprints.
ALLOWED_EXTENSIONS: frozenset[str] = (
    frozenset(EXTENSION_MIME_MAP.keys()) | TEXT_EXTENSIONS
)

# Subset that may be supplied for an avatar. Avatars must be raster images;
# the upload pipeline re-encodes everything to PNG via Pillow so we don't
# need the rest of ALLOWED_EXTENSIONS here.
AVATAR_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif"})


class ValidationError(Exception):
    """Raised when an uploaded file fails server-side content validation."""


@dataclass(frozen=True)
class ValidatedUpload:
    extension: str
    sniffed_mime: str


def _extract_extension(filename: str) -> str | None:
    """Return the lowercase extension after the final dot, or None."""
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[1].lower()


def _sniff_mime(file_path: str) -> str:
    """Wrap ``magic.from_file`` so we can stub it in tests if needed."""
    return magic.from_file(file_path, mime=True)


def validate_upload(
    file_path: str,
    filename: str,
    *,
    allowed_extensions: frozenset[str] = ALLOWED_EXTENSIONS,
) -> ValidatedUpload:
    """
    Validate that the bytes at ``file_path`` match the declared filename.

    Returns the matched extension and the libmagic-reported MIME on success.
    Raises ``ValidationError`` with a human-readable message otherwise; the
    blueprint should map that to a 400 response and remove the temp file.

    Pass a narrower ``allowed_extensions`` (e.g. ``AVATAR_EXTENSIONS``) when
    the endpoint accepts a smaller subset.
    """
    extension = _extract_extension(filename)
    if extension is None:
        raise ValidationError("File must have an extension.")
    if extension not in allowed_extensions:
        raise ValidationError(f"Files of type .{extension} are not accepted.")

    if not os.path.exists(file_path):
        raise ValidationError("Uploaded file is missing on the server.")

    try:
        sniffed = _sniff_mime(file_path)
    except Exception as exc:  # libmagic failures shouldn't 500 the request
        raise ValidationError(f"Could not determine file type: {exc}") from exc

    if extension in EXTENSION_MIME_MAP:
        if sniffed not in EXTENSION_MIME_MAP[extension]:
            raise ValidationError(
                f"File contents (detected as {sniffed}) do not match extension .{extension}."
            )
    elif extension in TEXT_EXTENSIONS:
        if not sniffed.startswith("text/"):
            raise ValidationError(
                f"File contents (detected as {sniffed}) do not match extension .{extension}."
            )
    else:
        # ``allowed_extensions`` accepted it but neither map covers it — treat
        # as a misconfiguration rather than silently letting the upload through.
        raise ValidationError(f"No content rule configured for extension .{extension}.")

    return ValidatedUpload(extension=extension, sniffed_mime=sniffed)
