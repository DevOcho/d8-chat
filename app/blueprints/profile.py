# app/blueprints/profile.py
import json
import os
import uuid

from flask import Blueprint, g, make_response, render_template, request
from PIL import Image
from werkzeug.utils import secure_filename

from app.chat_manager import chat_manager
from app.models import UploadedFile, User
from app.routes import AVATAR_SIZE, login_required
from app.services import minio_service

profile_bp = Blueprint("profile", __name__)


@profile_bp.route("/profile")
@login_required
def profile():
    """Renders the profile details partial for the offcanvas panel."""
    html = render_template(
        "partials/profile_details.html", user=g.user, theme=g.user.theme
    )
    response = make_response(html)
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


@profile_bp.route("/profile/address/view", methods=["GET"])
@login_required
def get_address_display():
    """Returns the read-only address display partial."""
    return render_template("partials/address_display.html", user=g.user)


@profile_bp.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    if "avatar" not in request.files:
        return "No file part", 400
    file = request.files["avatar"]
    if file.filename == "":
        return "No selected file", 400
    allowed_extensions = {"png", "jpg", "jpeg", "gif"}
    if (
        "." not in file.filename
        or file.filename.rsplit(".", 1)[1].lower() not in allowed_extensions
    ):
        return "File type not allowed", 400

    old_avatar_file = g.user.avatar
    original_filename = secure_filename(file.filename)
    stored_filename = f"{uuid.uuid4()}.png"
    temp_dir = os.path.join(g.app.instance_path, "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, stored_filename)

    try:
        file.save(temp_path)
        with Image.open(temp_path) as img:
            img.thumbnail(AVATAR_SIZE)
            img.save(temp_path, format="PNG")
        file_size = os.path.getsize(temp_path)
        success = minio_service.upload_file(
            object_name=stored_filename, file_path=temp_path, content_type="image/png"
        )

        if success:
            new_file = UploadedFile.create(
                uploader=g.user,
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type="image/png",
                file_size_bytes=file_size,
            )
            g.user.avatar = new_file
            g.user.save()
            if old_avatar_file:
                try:
                    minio_service.delete_file(old_avatar_file.stored_filename)
                    old_avatar_file.delete_instance()
                except Exception as e:
                    print(f"Error during old avatar cleanup: {e}")

            payload = {
                "type": "avatar_update",
                "user_id": g.user.id,
                "avatar_url": g.user.avatar_url,
            }
            chat_manager.broadcast_to_all(payload)

            profile_header_html = render_template(
                "partials/profile_header.html", user=g.user
            )
            sidebar_button_html = render_template(
                "partials/_sidebar_profile_button.html"
            )
            sidebar_oob_swap = f'<div hx-swap-oob="outerHTML:#sidebar-profile-button">{sidebar_button_html}</div>'
            return make_response(profile_header_html + sidebar_oob_swap)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return render_template("partials/profile_header.html", user=g.user)


@profile_bp.route("/profile/address/edit", methods=["GET"])
@login_required
def get_address_form():
    """Returns the address editing form partial."""
    return render_template("partials/address_form.html", user=g.user)


@profile_bp.route("/profile/address", methods=["PUT"])
@login_required
def update_address():
    """Processes the address form submission."""
    user = g.user
    user.country = request.form.get("country")
    user.city = request.form.get("city")
    user.timezone = request.form.get("timezone")
    user.save()
    display_html = render_template("partials/address_display.html", user=user)
    header_html_content = render_template("partials/profile_header.html", user=user)
    header_oob_swap = f'<div id="profile-header-card" hx-swap-oob="outerHTML">{header_html_content}</div>'
    return make_response(display_html + header_oob_swap)


@profile_bp.route("/profile/status", methods=["PUT"])
@login_required
def update_presence_status():
    """Updates the user's presence status and broadcasts the change as a JSON event."""
    new_status = request.form.get("status")
    if new_status not in ["online", "away", "busy"]:
        return "Invalid status", 400

    user = g.user
    user.presence_status = new_status
    user.save()
    presence_class_map = {
        "online": "presence-online",
        "away": "presence-away",
        "busy": "presence-busy",
    }
    status_class = presence_class_map.get(new_status)
    payload = {
        "type": "presence_update",
        "user_id": user.id,
        "status_class": status_class,
    }
    chat_manager.broadcast_to_all(payload)

    profile_header_html = render_template("partials/profile_header.html", user=g.user)
    sidebar_button_html = render_template("partials/_sidebar_profile_button.html")
    sidebar_oob_swap = f'<div hx-swap-oob="outerHTML:#sidebar-profile-button">{sidebar_button_html}</div>'
    return make_response(profile_header_html + sidebar_oob_swap)


@profile_bp.route("/profile/theme", methods=["PUT"])
@login_required
def update_theme():
    """Updates the user's theme preference."""
    new_theme = request.form.get("theme")
    if new_theme in ["light", "dark", "system"]:
        user = g.user
        user.theme = new_theme
        user.save()
        response = make_response("")
        response.headers["HX-Refresh"] = "true"
        return response
    return "Invalid theme", 400


@profile_bp.route("/profile/notification_sound", methods=["PUT"])
@login_required
def update_notification_sound():
    """Updates the user's notification sound preference."""
    new_sound = request.form.get("sound")
    allowed_sounds = ["d8-notification.mp3", "slack-notification.mp3"]
    if new_sound and new_sound in allowed_sounds:
        user = g.user
        user.notification_sound = new_sound
        user.save()
        response = make_response("")
        response.headers["HX-Trigger"] = json.dumps(
            {"update-sound-preference": new_sound}
        )
        return response
    return "Invalid sound choice", 400


@profile_bp.route("/chat/user/preference/wysiwyg", methods=["PUT"])
@login_required
def set_wysiwyg_preference():
    """Updates the user's preference for the WYSIWYG editor."""
    enabled_str = request.form.get("wysiwyg_enabled", "false")
    enabled = enabled_str.lower() == "true"
    if g.user.wysiwyg_enabled != enabled:
        user = User.get_by_id(g.user.id)
        user.wysiwyg_enabled = enabled
        user.save()
        g.user.wysiwyg_enabled = enabled
    return "", 204
