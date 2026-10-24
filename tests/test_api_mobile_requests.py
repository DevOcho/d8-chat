# tests/test_api_mobile_requests.py
"""The mobile team's requests: last-message previews on the conversation
lists, forward paging of history, image dimensions on attachments, and the
updated message in reaction (and edit) responses."""

import datetime
import io

import pytest
from PIL import Image

from app.blueprints.api_v1 import generate_api_token
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    MessageAttachment,
    UploadedFile,
    User,
    UserConversationStatus,
    utc_now,
)


@pytest.fixture
def auth(app):
    with app.app_context():
        return {"Authorization": f"Bearer d8_sec_{generate_api_token(1)}"}


@pytest.fixture
def general(app):
    me = User.get_by_id(1)
    other = User.create(id=2, username="other", email="o@x.com", display_name="Other")
    channel = Channel.get(name="general")
    ChannelMember.create(user=me, channel=channel)
    ChannelMember.create(user=other, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    return {"me": me, "other": other, "channel": channel, "conv": conv}


def _at(minutes_ago):
    return utc_now() - datetime.timedelta(minutes=minutes_ago)


# --- Last-message previews ---------------------------------------------------------


def test_channels_carry_the_last_message_preview(client, auth, general):
    Message.create(
        user=general["me"],
        conversation=general["conv"],
        content="old",
        created_at=_at(10),
    )
    last = Message.create(
        user=general["other"],
        conversation=general["conv"],
        content="Deploy   is\ndone :tada:",
        created_at=_at(5),
    )
    # A later thread reply isn't the timeline's last message.
    Message.create(
        user=general["me"],
        conversation=general["conv"],
        content="in a thread",
        parent_message=last,
        reply_type="thread",
        created_at=_at(1),
    )
    channels = client.get("/api/v1/channels", headers=auth).get_json()["channels"]
    general_row = next(c for c in channels if c["id"] == general["channel"].id)
    assert general_row["last_message"]["id"] == last.id
    assert general_row["last_message"]["content_preview"] == "Deploy is done 🎉"
    assert general_row["last_message"]["user"]["username"] == "other"
    assert general_row["last_message"]["has_attachments"] is False
    assert general_row["last_activity_at"] == last.created_at.isoformat()


def test_empty_channel_has_no_last_message(client, auth, general):
    channels = client.get("/api/v1/channels", headers=auth).get_json()["channels"]
    row = next(c for c in channels if c["id"] == general["channel"].id)
    assert row["last_message"] is None
    assert row["last_activity_at"] is None


def test_preview_is_truncated_and_flags_attachments(client, auth, general):
    msg = Message.create(
        user=general["other"], conversation=general["conv"], content="x" * 300
    )
    upload = UploadedFile.create(
        uploader=general["other"],
        original_filename="a.png",
        stored_filename="a.png",
        mime_type="image/png",
        file_size_bytes=1,
    )
    MessageAttachment.create(message=msg, attachment=upload)
    channels = client.get("/api/v1/channels", headers=auth).get_json()["channels"]
    preview = next(c for c in channels if c["id"] == general["channel"].id)[
        "last_message"
    ]
    assert len(preview["content_preview"]) == 140
    assert preview["content_preview"].endswith("…")
    assert preview["has_attachments"] is True


def test_system_message_and_deleted_placeholder_previews(client, auth, general):
    Message.create(
        user=None,
        conversation=general["conv"],
        content="Other joined.",
        created_at=_at(3),
    )
    channels = client.get("/api/v1/channels", headers=auth).get_json()["channels"]
    preview = next(c for c in channels if c["id"] == general["channel"].id)[
        "last_message"
    ]
    assert preview["user"] is None
    assert preview["content_preview"] == "Other joined."

    Message.create(
        user=general["other"], conversation=general["conv"], content="", is_deleted=True
    )
    channels = client.get("/api/v1/channels", headers=auth).get_json()["channels"]
    preview = next(c for c in channels if c["id"] == general["channel"].id)[
        "last_message"
    ]
    assert preview["content_preview"] == "This message was deleted."


def test_dms_carry_the_last_message_and_stay_most_recent_first(client, auth, general):
    third = User.create(id=3, username="third", email="t@x.com")
    for partner, minutes in ((general["other"], 30), (third, 2)):
        conv = Conversation.create(conversation_id_str=f"dm_1_{partner.id}", type="dm")
        UserConversationStatus.create(user=general["me"], conversation=conv)
        Message.create(
            user=partner,
            conversation=conv,
            content=f"hi from {partner.username}",
            created_at=_at(minutes),
        )
    dms = client.get("/api/v1/dms", headers=auth).get_json()["dms"]
    assert [d["other_user"]["username"] for d in dms] == ["third", "other"]
    assert dms[0]["last_message"]["content_preview"] == "hi from third"
    assert dms[0]["last_activity_at"] is not None


# --- History paging -------------------------------------------------------------------


def _history(client, auth, conv, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages?{query}",
        headers=auth,
    )


def test_after_message_id_pages_forward_oldest_first(client, auth, general):
    msgs = [
        Message.create(
            user=general["other"],
            conversation=general["conv"],
            content=f"m{i}",
            created_at=_at(100 - i),
        )
        for i in range(40)
    ]
    res = _history(client, auth, general["conv"], after_message_id=msgs[4].id)
    ids = [m["id"] for m in res.get_json()["messages"]]
    assert ids == [m.id for m in msgs[5:35]]


def test_after_message_id_skips_thread_replies(client, auth, general):
    parent = Message.create(
        user=general["other"], conversation=general["conv"], content="p"
    )
    Message.create(
        user=general["other"],
        conversation=general["conv"],
        content="r",
        parent_message=parent,
        reply_type="thread",
    )
    later = Message.create(
        user=general["other"], conversation=general["conv"], content="later"
    )
    res = _history(client, auth, general["conv"], after_message_id=parent.id)
    assert [m["id"] for m in res.get_json()["messages"]] == [later.id]


def test_only_one_cursor_is_allowed(client, auth, general):
    res = _history(
        client, auth, general["conv"], after_message_id=1, before_message_id=5
    )
    assert res.status_code == 400


def test_around_message_id_does_not_return_another_conversations_message(
    client, auth, general
):
    private_dm = Conversation.create(conversation_id_str="dm_2_9", type="dm")
    secret = Message.create(
        user=general["other"], conversation=private_dm, content="secret"
    )
    res = _history(client, auth, general["conv"], around_message_id=secret.id)
    assert res.status_code == 200
    assert all(m["content"] != "secret" for m in res.get_json()["messages"])


# --- Image dimensions --------------------------------------------------------------------


def _png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    buf.seek(0)
    return buf


def test_upload_records_image_dimensions(client, auth, mocker):
    mocker.patch("app.blueprints.api_v1.minio_service.upload_file", return_value=True)
    res = client.post(
        "/api/v1/files/upload",
        data={"file": (_png(320, 200), "pic.png")},
        content_type="multipart/form-data",
        headers=auth,
    )
    assert res.status_code == 201
    body = res.get_json()
    assert (body["width"], body["height"]) == (320, 200)
    stored = UploadedFile.get_by_id(body["file_id"])
    assert (stored.width, stored.height) == (320, 200)


def test_large_image_dimensions_are_as_stored(client, auth, mocker):
    mocker.patch("app.blueprints.api_v1.minio_service.upload_file", return_value=True)
    res = client.post(
        "/api/v1/files/upload",
        data={"file": (_png(3840, 1920), "big.png")},
        content_type="multipart/form-data",
        headers=auth,
    )
    body = res.get_json()
    # optimize_if_image fits it inside 1920x1920 before it's measured.
    assert (body["width"], body["height"]) == (1920, 960)


def test_web_upload_records_dimensions_too(logged_in_client, mocker):
    mocker.patch("app.blueprints.files.minio_service.upload_file", return_value=True)
    res = logged_in_client.post(
        "/files/upload",
        data={"file": (_png(64, 48), "pic.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 201
    stored = UploadedFile.get_by_id(res.get_json()["file_id"])
    assert (stored.width, stored.height) == (64, 48)


def test_attachments_carry_dimensions(client, auth, general):
    upload = UploadedFile.create(
        uploader=general["other"],
        original_filename="a.png",
        stored_filename="a.png",
        mime_type="image/png",
        file_size_bytes=1,
        width=640,
        height=480,
    )
    msg = Message.create(
        user=general["other"], conversation=general["conv"], content="pic"
    )
    MessageAttachment.create(message=msg, attachment=upload)
    res = _history(client, auth, general["conv"])
    attachment = res.get_json()["messages"][-1]["attachments"][0]
    assert (attachment["width"], attachment["height"]) == (640, 480)


# --- Responses carry the updated message ---------------------------------------------------


def test_reaction_response_includes_the_updated_message(client, auth, general):
    msg = Message.create(
        user=general["other"], conversation=general["conv"], content="hi"
    )
    res = client.post(
        f"/api/v1/messages/{msg.id}/reactions", headers=auth, json={"emoji": "👍"}
    )
    body = res.get_json()
    assert body["message_id"] == msg.id
    assert body["message"]["id"] == msg.id
    assert body["message"]["reactions"][0]["emoji"] == "👍"
    assert body["message"]["reactions"][0]["users"] == [1]


def test_edit_response_is_the_updated_message(client, auth, general):
    msg = Message.create(user=general["me"], conversation=general["conv"], content="hi")
    res = client.patch(
        f"/api/v1/messages/{msg.id}", headers=auth, json={"content": "hello"}
    )
    body = res.get_json()
    assert body["id"] == msg.id
    assert body["content"] == "hello"
    assert body["is_edited"] is True
