"""
Tests for the magic-byte upload validator.

Audit item: the upload pipeline used to trust the client-supplied filename
extension and Content-Type header. After this change, every upload is sniffed
with libmagic, the bytes are matched against an explicit allowlist, and the
sniffed MIME (not the client one) is what we persist.
"""

import io

import pytest

from app.models import UploadedFile
from app.services.upload_validation import (
    ALLOWED_EXTENSIONS,
    AVATAR_EXTENSIONS,
    ValidationError,
    validate_upload,
)

# Real magic-byte payloads. ``b"dummy"`` doesn't sniff as anything useful, so
# fixtures need actual minimal headers for each format.
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f"
    b"\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
    b"\xff\xd9"
)
TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<<>>endobj\nxref\n0 1\n0000000000 65535 f \ntrailer<<>>\n%%EOF\n"
)
TINY_ZIP = b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
WINDOWS_EXE_HEADER = (
    b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 200
)


def _write(tmp_path, name: str, payload: bytes) -> str:
    """Drop bytes into a temp file and return its absolute path."""
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


# --- Direct unit tests on validate_upload --------------------------------


class TestSniffMatches:
    def test_real_png_accepted(self, tmp_path):
        result = validate_upload(_write(tmp_path, "x.png", TINY_PNG), "logo.png")
        assert result.extension == "png"
        assert result.sniffed_mime == "image/png"

    def test_real_jpeg_accepted(self, tmp_path):
        result = validate_upload(_write(tmp_path, "x.jpg", TINY_JPEG), "photo.jpg")
        assert result.sniffed_mime == "image/jpeg"

    def test_real_pdf_accepted(self, tmp_path):
        result = validate_upload(_write(tmp_path, "x.pdf", TINY_PDF), "doc.pdf")
        assert result.sniffed_mime == "application/pdf"

    def test_text_file_accepted(self, tmp_path):
        result = validate_upload(
            _write(tmp_path, "x.txt", b"hello world\n"), "notes.txt"
        )
        assert result.sniffed_mime.startswith("text/")


class TestSniffRejects:
    def test_renamed_executable(self, tmp_path):
        """The single most important assertion: an MZ-header binary uploaded
        as ``foo.png`` must be rejected."""
        path = _write(tmp_path, "evil.png", WINDOWS_EXE_HEADER)
        with pytest.raises(ValidationError, match="do not match"):
            validate_upload(path, "evil.png")

    def test_html_disguised_as_png(self, tmp_path):
        path = _write(tmp_path, "x.png", b"<html><script>alert(1)</script></html>")
        with pytest.raises(ValidationError, match="do not match"):
            validate_upload(path, "x.png")

    def test_zip_disguised_as_pdf(self, tmp_path):
        path = _write(tmp_path, "x.pdf", TINY_ZIP)
        with pytest.raises(ValidationError, match="do not match"):
            validate_upload(path, "x.pdf")

    def test_binary_disguised_as_text(self, tmp_path):
        """A real PNG uploaded as ``.txt`` should fail the text/* check."""
        path = _write(tmp_path, "x.txt", TINY_PNG)
        with pytest.raises(ValidationError, match="do not match"):
            validate_upload(path, "x.txt")


class TestExtensionAllowlist:
    @pytest.mark.parametrize("blocked_ext", ["html", "js", "css", "ts", "exe", "sh"])
    def test_dropped_extension_rejected(self, tmp_path, blocked_ext):
        """Extensions removed from the allowlist must be refused even when
        the bytes look fine for that extension."""
        path = _write(tmp_path, f"x.{blocked_ext}", b"alert(1);\n")
        with pytest.raises(ValidationError, match="not accepted"):
            validate_upload(path, f"x.{blocked_ext}")

    def test_no_extension_rejected(self, tmp_path):
        path = _write(tmp_path, "noext", b"hello")
        with pytest.raises(ValidationError, match="must have an extension"):
            validate_upload(path, "noext")

    def test_html_js_css_ts_not_in_allowlist(self):
        for ext in ("html", "js", "css", "ts"):
            assert ext not in ALLOWED_EXTENSIONS

    def test_avatar_subset_is_image_only(self):
        for ext in AVATAR_EXTENSIONS:
            assert ext in {"png", "jpg", "jpeg", "gif"}
        assert "pdf" not in AVATAR_EXTENSIONS
        assert "zip" not in AVATAR_EXTENSIONS


class TestAvatarSubset:
    def test_png_accepted_for_avatar(self, tmp_path):
        result = validate_upload(
            _write(tmp_path, "a.png", TINY_PNG),
            "a.png",
            allowed_extensions=AVATAR_EXTENSIONS,
        )
        assert result.sniffed_mime == "image/png"

    def test_pdf_rejected_for_avatar(self, tmp_path):
        path = _write(tmp_path, "a.pdf", TINY_PDF)
        with pytest.raises(ValidationError, match="not accepted"):
            validate_upload(path, "a.pdf", allowed_extensions=AVATAR_EXTENSIONS)


# --- HTTP-level tests for the actual upload endpoints --------------------


class TestApiUploadEndpoint:
    def _login(self, client):
        from app.models import User

        user = User.get_by_id(1)
        user.set_password("password123")
        user.save()
        res = client.post(
            "/api/v1/auth/login",
            json={"username": "testuser", "password": "password123"},
        )
        return res.get_json()["api_token"]

    def test_renamed_exe_rejected_with_400(self, client, mocker):
        """End-to-end: the upload endpoint refuses an exe-as-png and never
        reaches MinIO or the DB."""
        upload_mock = mocker.patch(
            "app.blueprints.api_v1.minio_service.upload_file", return_value=True
        )
        token = self._login(client)

        res = client.post(
            "/api/v1/files/upload",
            data={"file": (io.BytesIO(WINDOWS_EXE_HEADER), "evil.png")},
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert res.status_code == 400
        assert "do not match" in res.get_json()["error"]
        upload_mock.assert_not_called()
        assert UploadedFile.select().count() == 0

    def test_lying_content_type_does_not_persist(self, client, mocker):
        """Even when the multipart Content-Type lies, the persisted MIME is
        whatever libmagic actually sniffed from the bytes."""
        mocker.patch(
            "app.blueprints.api_v1.minio_service.upload_file", return_value=True
        )
        token = self._login(client)

        # Real PNG bytes, but the multipart Content-Type claims it's something
        # else. We should ignore that header and persist image/png.
        res = client.post(
            "/api/v1/files/upload",
            data={
                "file": (
                    io.BytesIO(TINY_PNG),
                    "x.png",
                    "application/x-malware-pretending-to-be-png",
                )
            },
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert res.status_code == 201
        assert res.get_json()["mime_type"] == "image/png"
        assert UploadedFile.get().mime_type == "image/png"

    def test_dropped_html_extension_rejected(self, client):
        """HTML uploads were on the allowlist before; now they're refused
        outright with no chance to slip through via lying bytes."""
        token = self._login(client)

        res = client.post(
            "/api/v1/files/upload",
            data={"file": (io.BytesIO(b"<h1>hi</h1>"), "page.html")},
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert res.status_code == 400
        assert "not allowed" in res.get_json()["error"]


class TestWebUploadEndpoint:
    def test_renamed_exe_rejected_with_400(self, logged_in_client, mocker):
        upload_mock = mocker.patch(
            "app.blueprints.files.minio_service.upload_file", return_value=True
        )

        res = logged_in_client.post(
            "/files/upload",
            data={"file": (io.BytesIO(WINDOWS_EXE_HEADER), "evil.png")},
            content_type="multipart/form-data",
        )

        assert res.status_code == 400
        upload_mock.assert_not_called()
        assert UploadedFile.select().count() == 0

    def test_lying_content_type_persists_sniffed_mime(self, logged_in_client, mocker):
        mocker.patch(
            "app.blueprints.files.minio_service.upload_file", return_value=True
        )

        res = logged_in_client.post(
            "/files/upload",
            data={
                "file": (
                    io.BytesIO(TINY_PNG),
                    "x.png",
                    "application/x-evil",
                )
            },
            content_type="multipart/form-data",
        )

        assert res.status_code == 201
        assert UploadedFile.get().mime_type == "image/png"
