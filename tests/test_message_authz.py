# tests/test_message_authz.py
"""Every route that takes a message, parent, quote, attachment or poll id must
check the caller can see it. Each of these was an open hole: a member of one
conversation could read, react to, quote, attach from or post into another
(a private channel, or a DM they aren't in) just by guessing ids."""

import pytest

from app.blueprints.api_v1 import generate_api_token
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    MessageAttachment,
    Poll,
    PollOption,
    Reaction,
    UploadedFile,
    User,
    UserConversationStatus,
    Vote,
    Workspace,
)

SECRET = "TOPSECRET-dm-content-42"


@pytest.fixture
def world(app):
    """User 1 (the logged-in client) is in #general; alice and bob share a DM
    and alice owns a private channel and a file user 1 must not reach."""
    workspace = Workspace.get(name="DevOcho")
    me = User.get_by_id(1)
    alice = User.create(id=2, username="alice", email="a@x.com")
    bob = User.create(id=3, username="bob", email="b@x.com")
    general = Channel.get(name="general")
    ChannelMember.get_or_create(user=me, channel=general)
    ChannelMember.get_or_create(user=alice, channel=general)
    general_conv = Conversation.get(conversation_id_str=f"channel_{general.id}")

    dm = Conversation.create(conversation_id_str="dm_2_3", type="dm")
    UserConversationStatus.create(user=alice, conversation=dm)
    UserConversationStatus.create(user=bob, conversation=dm)
    secret = Message.create(user=alice, conversation=dm, content=SECRET)
    secret_file = UploadedFile.create(
        uploader=alice,
        original_filename="payroll.pdf",
        stored_filename="victim-object.pdf",
        mime_type="application/pdf",
        file_size_bytes=10,
    )
    MessageAttachment.create(message=secret, attachment=secret_file)
    poll = Poll.create(message=secret, question="secret poll?")
    option = PollOption.create(poll=poll, text="yes")

    announcements = Channel.get(name="announcements")
    announcements.posting_restricted_to_admins = True
    announcements.save()
    ChannelMember.get_or_create(user=me, channel=announcements)

    private = Channel.create(workspace=workspace, name="exec-only", is_private=True)
    ChannelMember.create(user=alice, channel=private, role="admin")
    private_conv = Conversation.create(
        conversation_id_str=f"channel_{private.id}", type="channel"
    )
    return {
        "me": me,
        "alice": alice,
        "general": general_conv.conversation_id_str,
        "general_conv": general_conv,
        "announcements": f"channel_{announcements.id}",
        "private": private,
        "private_conv": private_conv,
        "secret": secret,
        "secret_file": secret_file,
        "poll": poll,
        "option": option,
    }


def _bearer(app, user_id=1):
    with app.app_context():
        return {"Authorization": f"Bearer d8_sec_{generate_api_token(user_id)}"}


def _my_latest():
    return Message.select().where(Message.user == 1).order_by(Message.id.desc()).first()


# --- Markdown -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "````\n\n<img src=x onerror=alert(document.domain)>\n\n```",
        "~~~~\n<img src=x onerror=alert(1)>\n~~~",
        "```\n```<script>alert(1)</script>```\n```",
    ],
)
def test_mismatched_code_fences_cannot_inject_html(app, payload):
    with app.test_request_context("/"):
        html = app.jinja_env.filters["markdown"](payload)
    assert "<img" not in html
    assert "<script" not in html


def test_real_code_blocks_still_highlight(app):
    with app.test_request_context("/"):
        html = app.jinja_env.filters["markdown"]("```python\nx = '<b>'\n```")
    assert 'class="codehilite"' in html
    assert "&lt;b&gt;" in html


# --- Reading by id --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/chat/message/{id}",
        "/chat/thread/{id}",
        "/chat/message/{id}/reply",
        "/chat/input/thread/{id}",
        "/chat/message/{id}/forward",
        "/chat/message/{id}/context",
    ],
)
def test_foreign_message_routes_are_forbidden(logged_in_client, world, path):
    res = logged_in_client.get(path.format(id=world["secret"].id))
    assert res.status_code == 403
    assert SECRET not in res.get_data(as_text=True)


def test_thread_quote_reply_composer_is_forbidden(logged_in_client, world):
    reply = Message.create(
        user=world["alice"],
        conversation=world["secret"].conversation,
        content="in thread",
        parent_message=world["secret"],
        reply_type="thread",
    )
    res = logged_in_client.get(f"/chat/message/{reply.id}/load_for_thread_reply")
    assert res.status_code == 403


def test_removed_member_cannot_jump_into_channel(logged_in_client, world):
    """A read-state row survives removal from a channel; it must not grant
    access."""
    private_msg = Message.create(
        user=world["alice"], conversation=world["private_conv"], content=SECRET
    )
    UserConversationStatus.create(user=world["me"], conversation=world["private_conv"])
    res = logged_in_client.get(f"/chat/message/{private_msg.id}/context")
    assert res.status_code == 403


def test_member_can_still_view_their_messages(logged_in_client, world):
    mine = Message.create(
        user=world["me"], conversation=world["general_conv"], content="hello"
    )
    assert logged_in_client.get(f"/chat/message/{mine.id}").status_code == 200
    assert logged_in_client.get(f"/chat/thread/{mine.id}").status_code == 200


# --- Sending with foreign references -------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        lambda w: {"quoted_message_id": w["secret"].id},
        lambda w: {"parent_message_id": w["secret"].id, "reply_type": "quote"},
        lambda w: {"parent_message_id": w["secret"].id, "reply_type": "thread"},
        lambda w: {"attachment_file_ids": str(w["secret_file"].id)},
    ],
)
def test_web_send_rejects_foreign_references(logged_in_client, world, extra):
    data = {"chat_message": "hi", **extra(world)}
    res = logged_in_client.post(
        f"/chat/conversations/{world['general']}/messages", data=data
    )
    assert res.status_code == 400
    assert _my_latest() is None


def test_web_send_rejects_unknown_reply_type(logged_in_client, world):
    parent = Message.create(
        user=world["me"], conversation=world["general_conv"], content="p"
    )
    res = logged_in_client.post(
        f"/chat/conversations/{world['general']}/messages",
        data={
            "chat_message": "hi",
            "parent_message_id": parent.id,
            "reply_type": "bogus",
        },
    )
    assert res.status_code == 400


def test_web_send_accepts_own_upload_and_local_quote(logged_in_client, world):
    mine = UploadedFile.create(
        uploader=world["me"],
        original_filename="mine.png",
        stored_filename="mine.png",
        mime_type="image/png",
        file_size_bytes=10,
    )
    local = Message.create(
        user=world["alice"], conversation=world["general_conv"], content="local"
    )
    res = logged_in_client.post(
        f"/chat/conversations/{world['general']}/messages",
        data={
            "chat_message": "hi",
            "quoted_message_id": local.id,
            "attachment_file_ids": str(mine.id),
        },
    )
    assert res.status_code == 200
    sent = _my_latest()
    assert sent.quoted_message_id == local.id
    assert [f.id for f in sent.attachments] == [mine.id]


def test_thread_reply_to_a_reply_joins_the_thread(logged_in_client, world):
    top = Message.create(
        user=world["alice"], conversation=world["general_conv"], content="top"
    )
    reply = Message.create(
        user=world["alice"],
        conversation=world["general_conv"],
        content="r1",
        parent_message=top,
        reply_type="thread",
    )
    res = logged_in_client.post(
        f"/chat/conversations/{world['general']}/messages",
        data={
            "chat_message": "r2",
            "parent_message_id": reply.id,
            "reply_type": "thread",
        },
    )
    assert res.status_code == 200
    assert _my_latest().parent_message_id == top.id


def test_api_send_rejects_foreign_quote_and_attachment(client, world, app):
    res = client.post(
        f"/api/v1/conversations/{world['general']}/messages",
        headers=_bearer(app),
        json={
            "content": "x",
            "quoted_message_id": world["secret"].id,
            "attachment_file_ids": str(world["secret_file"].id),
        },
    )
    assert res.status_code == 400
    assert _my_latest() is None
    proxy = client.get(
        f"/api/v1/files/{world['secret_file'].id}/content", headers=_bearer(app)
    )
    assert proxy.status_code in (403, 404)


def test_api_send_accepts_attachment_id_list(client, world, app):
    mine = UploadedFile.create(
        uploader=world["me"],
        original_filename="a.png",
        stored_filename="a.png",
        mime_type="image/png",
        file_size_bytes=10,
    )
    res = client.post(
        f"/api/v1/conversations/{world['general']}/messages",
        headers=_bearer(app),
        json={"content": "x", "attachment_file_ids": [mine.id]},
    )
    assert res.status_code == 201
    assert res.get_json()["attachments"][0]["file_id"] == mine.id


def test_api_send_respects_admin_only_posting(client, world, app):
    res = client.post(
        f"/api/v1/conversations/{world['announcements']}/messages",
        headers=_bearer(app),
        json={"content": "Fake announcement"},
    )
    assert res.status_code == 403
    assert _my_latest() is None


def test_forward_respects_admin_only_posting(logged_in_client, world):
    mine = Message.create(
        user=world["me"], conversation=world["general_conv"], content="fwd me"
    )
    res = logged_in_client.post(
        f"/chat/message/{mine.id}/forward",
        data={"conversation_id_str": world["announcements"]},
    )
    assert res.status_code == 403


# --- Polls and reactions --------------------------------------------------------


def test_poll_cannot_be_created_in_foreign_dm(logged_in_client, world):
    res = logged_in_client.post(
        "/chat/poll/create",
        data={
            "question": "q",
            "options[]": ["a", "b"],
            "conversation_id_str": "dm_2_3",
        },
    )
    assert res.status_code == 403
    assert _my_latest() is None


def test_poll_in_unknown_conversation_is_404(logged_in_client, world):
    res = logged_in_client.post(
        "/chat/poll/create",
        data={
            "question": "q",
            "options[]": ["a", "b"],
            "conversation_id_str": "dm_8_9",
        },
    )
    assert res.status_code == 404


def test_cannot_vote_on_foreign_poll(logged_in_client, client, world, app):
    res = logged_in_client.post(f"/chat/poll/option/{world['option'].id}/vote")
    assert res.status_code == 403
    res = client.post(
        f"/api/v1/polls/{world['poll'].id}/vote",
        headers=_bearer(app),
        json={"option_id": world["option"].id},
    )
    assert res.status_code == 403
    assert Vote.select().count() == 0


def test_cannot_react_to_foreign_message(logged_in_client, client, world, app):
    res = logged_in_client.post(
        f"/chat/message/{world['secret'].id}/react", data={"emoji": "x"}
    )
    assert res.status_code == 403
    res = client.post(
        f"/api/v1/messages/{world['secret'].id}/reactions",
        headers=_bearer(app),
        json={"emoji": "x"},
    )
    assert res.status_code == 403
    assert Reaction.select().count() == 0


# --- Deleting -------------------------------------------------------------------


def test_delete_keeps_files_another_message_still_uses(logged_in_client, world, mocker):
    deleted = mocker.patch("app.services.minio_service.delete_file")
    mine = UploadedFile.create(
        uploader=world["me"],
        original_filename="shared.png",
        stored_filename="shared.png",
        mime_type="image/png",
        file_size_bytes=10,
    )
    original = Message.create(
        user=world["me"], conversation=world["general_conv"], content="o"
    )
    forward = Message.create(
        user=world["alice"], conversation=world["general_conv"], content="f"
    )
    MessageAttachment.create(message=original, attachment=mine)
    MessageAttachment.create(message=forward, attachment=mine)

    assert logged_in_client.delete(f"/chat/message/{original.id}").status_code == 204
    deleted.assert_not_called()
    assert UploadedFile.get_or_none(id=mine.id) is not None


def test_delete_removes_an_unshared_upload(logged_in_client, world, mocker):
    deleted = mocker.patch("app.services.minio_service.delete_file")
    mine = UploadedFile.create(
        uploader=world["me"],
        original_filename="solo.png",
        stored_filename="solo.png",
        mime_type="image/png",
        file_size_bytes=10,
    )
    msg = Message.create(
        user=world["me"], conversation=world["general_conv"], content="o"
    )
    MessageAttachment.create(message=msg, attachment=mine)

    assert logged_in_client.delete(f"/chat/message/{msg.id}").status_code == 204
    deleted.assert_called_once_with("solo.png")
    assert UploadedFile.get_or_none(id=mine.id) is None
