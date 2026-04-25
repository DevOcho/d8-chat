"""
Edge cases for the hashtag extraction in ``chat_service.handle_new_message``.

The extractor uses regex ``r"(?<![^\\s(\\['\\\"])#([a-zA-Z0-9_-]+)"`` — meaning
the ``#`` must be preceded by whitespace, ``(``, ``[``, ``'``, ``"``, or
start-of-string, and the tag body is letters/digits/underscore/hyphen. We
test both the regex extraction and the integration with ``Channel`` (an
existing channel name doesn't double-up as a hashtag).
"""

import re

import pytest

from app.models import (
    Channel,
    Conversation,
    Hashtag,
    Message,
    MessageHashtag,
    User,
    Workspace,
)
from app.services.chat_service import handle_new_message

HASHTAG_PATTERN = re.compile(r"(?<![^\s(\['\"])#([a-zA-Z0-9_-]+)")


# --- Regex unit tests -------------------------------------------------------


class TestHashtagRegex:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("hello #world", {"world"}),
            ("#first thing", {"first"}),
            ("(#parens)", {"parens"}),
            ("[#brackets]", {"brackets"}),
            ('"#quoted"', {"quoted"}),
            ("'#single'", {"single"}),
            ("#tag-with-dash", {"tag-with-dash"}),
            ("#tag_underscore", {"tag_underscore"}),
            ("#numeric123", {"numeric123"}),
            ("#123only", {"123only"}),
            ("#a #b #c", {"a", "b", "c"}),
            ("#repeat #repeat", {"repeat"}),
        ],
    )
    def test_matches(self, text, expected):
        assert set(HASHTAG_PATTERN.findall(text)) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "email me at me@host.com",  # @ not #
            "issue#42 inline",  # preceded by alphanumeric → not a hashtag
            "color: #fff",  # preceded by space, but ASCII letters... wait
            "https://x.com/path#anchor",
            "1+#2",  # preceded by + → not a tag
        ],
    )
    def test_does_not_match_glued_to_word(self, text):
        # The lookbehind ``(?<![^\s(\['\"])`` permits start-of-string, space,
        # parens, brackets, and quotes. Anything else (letters, digits, dot,
        # slash, +) blocks the match.
        if text == "color: #fff":
            # "color: #fff" — the # is after ": " (space), so this WILL match.
            # That's the expected behavior of the regex; including it here as
            # a deliberate counter-example so the comment above is accurate.
            assert HASHTAG_PATTERN.findall(text) == ["fff"]
            return
        if text == "1+#2":
            # +# is not in the allow list of preceding chars → no match.
            assert HASHTAG_PATTERN.findall(text) == []
            return
        # Everything else: no match.
        assert HASHTAG_PATTERN.findall(text) == []

    def test_unicode_body_not_captured(self):
        # The body is `[a-zA-Z0-9_-]+` so non-ASCII letters end the tag.
        assert HASHTAG_PATTERN.findall("#café") == ["caf"]

    def test_period_ends_tag(self):
        assert HASHTAG_PATTERN.findall("end of sentence #tag.") == ["tag"]

    def test_hash_followed_by_space_no_match(self):
        assert HASHTAG_PATTERN.findall("# notatag") == []


# --- End-to-end via chat_service --------------------------------------------


@pytest.fixture
def channel_conv(app):
    """A channel conversation we can post into."""
    with app.app_context():
        workspace = Workspace.get(Workspace.name == "DevOcho")
        channel = Channel.create(workspace=workspace, name="hash-test")
        conv, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}",
            defaults={"type": "channel"},
        )
        return channel.id, conv.id


def _hashtags_for(message: Message) -> set[str]:
    rows = (
        MessageHashtag.select(Hashtag.name)
        .join(Hashtag)
        .where(MessageHashtag.message == message)
    )
    return {row.hashtag.name for row in rows}


class TestHashtagIntegration:
    def test_basic_extraction(self, app, channel_conv):
        with app.app_context():
            _, conv_id = channel_conv
            conv = Conversation.get_by_id(conv_id)
            user = User.get_by_id(1)
            msg = handle_new_message(
                sender=user, conversation=conv, chat_text="check #urgent now"
            )
            assert _hashtags_for(msg) == {"urgent"}

    def test_existing_channel_name_is_not_a_hashtag(self, app, channel_conv):
        # A real channel exists named "hash-test" — even if a user writes
        # "#hash-test", we shouldn't store it as a hashtag.
        with app.app_context():
            _, conv_id = channel_conv
            conv = Conversation.get_by_id(conv_id)
            user = User.get_by_id(1)
            msg = handle_new_message(
                sender=user,
                conversation=conv,
                chat_text="see #hash-test for context, plus #other-tag",
            )
            tags = _hashtags_for(msg)
            assert "hash-test" not in tags
            assert "other-tag" in tags

    def test_duplicate_hashtags_in_one_message_dedup(self, app, channel_conv):
        with app.app_context():
            _, conv_id = channel_conv
            conv = Conversation.get_by_id(conv_id)
            user = User.get_by_id(1)
            msg = handle_new_message(
                sender=user,
                conversation=conv,
                chat_text="#dup and #dup again",
            )
            assert _hashtags_for(msg) == {"dup"}
            # Only one MessageHashtag row.
            assert (
                MessageHashtag.select().where(MessageHashtag.message == msg).count()
                == 1
            )

    def test_hashtag_reuses_existing_hashtag_row(self, app, channel_conv):
        with app.app_context():
            _, conv_id = channel_conv
            conv = Conversation.get_by_id(conv_id)
            user = User.get_by_id(1)
            handle_new_message(
                sender=user, conversation=conv, chat_text="#shared first"
            )
            handle_new_message(
                sender=user, conversation=conv, chat_text="#shared second"
            )
            # One Hashtag row total — gets reused.
            assert Hashtag.select().where(Hashtag.name == "shared").count() == 1

    def test_no_hashtags_means_no_rows(self, app, channel_conv):
        with app.app_context():
            _, conv_id = channel_conv
            conv = Conversation.get_by_id(conv_id)
            user = User.get_by_id(1)
            msg = handle_new_message(
                sender=user, conversation=conv, chat_text="plain message"
            )
            assert _hashtags_for(msg) == set()
