import os
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from app.models import UploadedFile
from app.routes import login_required
from app.services import minio_service
from app.services.upload_validation import (
    ALLOWED_EXTENSIONS,
    ValidationError,
    validate_upload,
)

files_bp = Blueprint("files", __name__)

# Bump limit to 50MB
MAX_CONTENT_LENGTH = 50 * 1024 * 1024


def optimize_if_image(file_path, mime_type):
    """Resizes and compresses large images to save bandwidth and storage."""
    if not mime_type.startswith("image/"):
        return
    if "gif" in mime_type.lower():
        return  # Do not process GIFs, Pillow can break animations

    try:
        with Image.open(file_path) as img:
            # Auto-orient based on camera EXIF data
            img = ImageOps.exif_transpose(img)
            # Resize preserving aspect ratio (max 1920x1920)
            img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            # Save back to the same path, optimizing
            img.save(file_path, optimize=True, quality=85)
    except Exception as e:
        current_app.logger.warning(f"Image optimization skipped/failed: {e}")


@files_bp.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify(error="No file part"), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="No selected file"), 400

    original_filename = secure_filename(file.filename)
    if "." not in original_filename:
        return jsonify(error="File must have an extension."), 400

    file_ext = original_filename.rsplit(".", 1)[1].lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        return jsonify(
            error="File type not allowed. Files must have an extension (e.g., .png, .jpg)."
        ), 400

    stored_filename = f"{uuid.uuid4()}.{file_ext}"
    temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, stored_filename)
    file.save(temp_path)

    try:
        # Sniff actual content type and reject if it doesn't match the
        # extension. We persist the sniffed MIME, never the client-supplied one.
        try:
            validated = validate_upload(temp_path, original_filename)
        except ValidationError as exc:
            return jsonify(error=str(exc)), 400

        # Re-encode images to strip embedded payloads / EXIF / oversize.
        optimize_if_image(temp_path, validated.sniffed_mime)

        file_size = os.path.getsize(temp_path)
        if file_size > MAX_CONTENT_LENGTH:
            return jsonify(error="File exceeds maximum size limit"), 400

        success = minio_service.upload_file(
            object_name=stored_filename,
            file_path=temp_path,
            content_type=validated.sniffed_mime,
        )
        if not success:
            return jsonify(error="Failed to upload file to storage"), 500

        new_file = UploadedFile.create(
            uploader=g.user,
            original_filename=original_filename,
            stored_filename=stored_filename,
            mime_type=validated.sniffed_mime,
            file_size_bytes=file_size,
        )
        return (
            jsonify(file_id=new_file.id, message="File uploaded successfully"),
            201,
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
