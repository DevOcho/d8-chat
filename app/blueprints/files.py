# app/blueprints/files.py
import os
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.utils import secure_filename

from app.models import UploadedFile
from app.routes import login_required
from app.services import minio_service

files_bp = Blueprint("files", __name__)

# Configure allowed extensions and max size (e.g., 10MB)
ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "pdf",
    "txt",
    "py",
    "js",
    "css",
    "html",
}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@files_bp.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify(error="No file part"), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="No selected file"), 400

    if file and allowed_file(file.filename):
        # Secure the original filename
        original_filename = secure_filename(file.filename)

        # Generate a unique filename for storage
        file_ext = original_filename.rsplit(".", 1)[1].lower()
        stored_filename = f"{uuid.uuid4()}.{file_ext}"

        # Save the file temporarily to the server filesystem for processing
        temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, stored_filename)
        file.save(temp_path)

        # Get file size
        file_size = os.path.getsize(temp_path)
        if file_size > MAX_CONTENT_LENGTH:
            os.remove(temp_path)
            return jsonify(error="File exceeds maximum size limit"), 400

        # Upload from the temporary path to Minio
        success = minio_service.upload_file(
            object_name=stored_filename, file_path=temp_path, content_type=file.mimetype
        )

        # Clean up the temporary file
        os.remove(temp_path)

        if success:
            # Create a record in our database
            new_file = UploadedFile.create(
                uploader=g.user,
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=file.mimetype,
                file_size_bytes=file_size,
            )
            return (
                jsonify(file_id=new_file.id, message="File uploaded successfully"),
                201,
            )
        else:
            return jsonify(error="Failed to upload file to storage"), 500

    return jsonify(
        error="File type not allowed. Files must have an extension (e.g., .png, .jpg)."
    ), 400
