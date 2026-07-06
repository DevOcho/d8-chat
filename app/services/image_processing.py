"""Image re-encoding helpers shared by the upload blueprints.

Avatars are always re-encoded through Pillow so that any bytes embedded after
the image data (polyglots, EXIF payloads, trailing scripts) are stripped. The
naive way to do that — ``Image.open(...).thumbnail(...).save(..., "PNG")`` —
also silently collapses an animated GIF to its first frame, which is why users
who set an animated GIF avatar saw a still image.

``reencode_avatar`` keeps the payload-stripping guarantee while preserving
animation: animated inputs are resized frame-by-frame and written back out as a
GIF; everything else becomes a PNG as before.
"""

from __future__ import annotations

from PIL import Image, ImageOps, ImageSequence


def reencode_avatar(temp_path: str, size: tuple[int, int]) -> tuple[str, str]:
    """Resize the image at ``temp_path`` in place, preserving animation.

    Returns ``(mime_type, extension)`` describing the re-encoded file so the
    caller can store it under the right object name and content type. Animated
    GIFs stay GIFs (``image/gif``); every other input is flattened to a PNG
    (``image/png``). Raises whatever Pillow raises if the bytes can't be
    decoded — callers already treat that as a 400.
    """
    with Image.open(temp_path) as img:
        if getattr(img, "is_animated", False):
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(img):
                # Iterating composites each frame to full size, so resizing the
                # RGBA copy independently is safe even for diff-based GIFs.
                resized = frame.convert("RGBA")
                resized.thumbnail(size, Image.Resampling.LANCZOS)
                frames.append(resized)
                durations.append(frame.info.get("duration", 100))

            frames[0].save(
                temp_path,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                loop=img.info.get("loop", 0),
                duration=durations,
                disposal=2,
                optimize=True,
            )
            return "image/gif", "gif"

        oriented = ImageOps.exif_transpose(img)
        oriented.thumbnail(size, Image.Resampling.LANCZOS)
        oriented.save(temp_path, format="PNG", optimize=True)
        return "image/png", "png"
