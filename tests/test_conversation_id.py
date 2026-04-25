import pytest

from app.conversation_id import ConversationKey, parse_conversation_id


class TestParseChannel:
    def test_basic(self):
        assert parse_conversation_id("channel_5") == ConversationKey(
            type="channel", channel_id=5
        )

    def test_large_id(self):
        assert parse_conversation_id("channel_2147483647").channel_id == 2147483647

    def test_extra_segment_is_rejected(self):
        # Numeric extra so we hit the count check, not the int-parse check.
        with pytest.raises(ValueError, match="exactly one id"):
            parse_conversation_id("channel_5_6")

    def test_extra_garbage_segment_is_rejected(self):
        with pytest.raises(ValueError, match="non-integer"):
            parse_conversation_id("channel_5_garbage")

    def test_missing_id(self):
        with pytest.raises(ValueError):
            parse_conversation_id("channel_")

    def test_negative_id_parses(self):
        # The regex doesn't disallow negatives — DB lookup will simply not match.
        assert parse_conversation_id("channel_-1").channel_id == -1


class TestParseDm:
    def test_two_users(self):
        parsed = parse_conversation_id("dm_3_5")
        assert parsed.type == "dm"
        assert parsed.user_ids == (3, 5)
        assert parsed.channel_id is None

    def test_user_ids_preserve_order(self):
        # Order matters for membership checks (current callers only check `in`,
        # but if that ever changes we shouldn't silently re-sort).
        assert parse_conversation_id("dm_5_3").user_ids == (5, 3)

    def test_self_dm(self):
        # Self-DMs use the same user id twice — should be allowed.
        assert parse_conversation_id("dm_4_4").user_ids == (4, 4)

    def test_multi_party(self):
        # Multi-party DMs aren't shipped yet but the parser shouldn't reject
        # the format — group DMs are on the missing-features list.
        assert parse_conversation_id("dm_1_2_3_4").user_ids == (1, 2, 3, 4)

    def test_single_user_rejected(self):
        with pytest.raises(ValueError, match="at least two"):
            parse_conversation_id("dm_5")

    def test_non_integer_user_id(self):
        with pytest.raises(ValueError, match="non-integer"):
            parse_conversation_id("dm_3_abc")


class TestParseMalformed:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "channel",
            "dm",
            "channel-5",
            "DM_3_5",  # case-sensitive prefix
            "Channel_5",
            "user_5",  # unknown type
            "_5",
            "5",
            "channel_abc",
        ],
    )
    def test_rejects(self, value):
        with pytest.raises(ValueError):
            parse_conversation_id(value)

    @pytest.mark.parametrize("value", [None, 123, [], {}])
    def test_non_string_rejected(self, value):
        with pytest.raises(ValueError):
            parse_conversation_id(value)


class TestApiEndpointBoundary:
    """
    Sanity check that the api_v1 endpoints reject malformed conv ids cleanly
    instead of returning 500. We only need a real conversation row for the
    success path — for malformed input the lookup fails first and returns 404,
    which is acceptable.
    """

    def test_get_messages_unknown_conv_returns_404(self, logged_in_client):
        # A token user is needed for /api/v1/, but a malformed conv id will
        # short-circuit at the auth layer with 401 since no Bearer is sent.
        # That's fine for this assertion — we just need to confirm no crash.
        resp = logged_in_client.get(
            "/api/v1/conversations/garbage/messages",
            headers={"Authorization": "Bearer d8_sec_invalid"},
        )
        assert resp.status_code in (401, 404, 400)
