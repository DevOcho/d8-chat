"""0011_uploaded_file_dimensions.py

Adds nullable ``width`` and ``height`` to ``uploadedfile``.

Image uploads now record their pixel size so the web and mobile clients can
reserve an image's box before it loads, instead of the timeline jumping as
each image arrives. Files uploaded before this migration stay NULL (clients
fall back to a fixed box); non-images are always NULL.

Fresh DBs initialized via ``init_db.py`` already have the columns because the
``UploadedFile`` model was updated in lockstep; ``IF NOT EXISTS`` makes this
migration a no-op there.
"""

# pylint: disable=C0103

from db_bootstrap import db


def migrate():
    """Add the width and height columns."""
    db.execute_sql("ALTER TABLE uploadedfile ADD COLUMN IF NOT EXISTS width INTEGER")
    db.execute_sql("ALTER TABLE uploadedfile ADD COLUMN IF NOT EXISTS height INTEGER")


def rollback():
    """Drop the width and height columns."""
    db.execute_sql("ALTER TABLE uploadedfile DROP COLUMN IF EXISTS width")
    db.execute_sql("ALTER TABLE uploadedfile DROP COLUMN IF EXISTS height")
