"""Unit tests for context.py — Message, MessageBuffer, ContextManager parsing."""

import time

import pytest
import pytest_asyncio

from telegram_agent_mcp.config import Config
from telegram_agent_mcp.context import Message, MessageBuffer, ContextManager


# ── Message ────────────────────────────────────────────────

class TestMessage:
    def test_to_dict_basic(self):
        m = Message(
            sender_id="42", sender_name="Alice", content="hello",
            timestamp="2026-01-01T00:00:00+08:00", message_id="1",
            chat_id="-100",
        )
        d = m.to_dict()
        assert d["sender_id"] == "42"
        assert d["sender_name"] == "Alice"
        assert d["content"] == "hello"
        assert d["chat_id"] == "-100"
        assert d["is_at_me"] is False
        assert d["is_self"] is False
        assert "image_urls" not in d

    def test_to_dict_with_images(self):
        m = Message(
            sender_id="1", sender_name="B", content="pic",
            timestamp="t", message_id="2",
            image_urls=["https://example.com/img.jpg"],
        )
        d = m.to_dict()
        assert d["image_urls"] == ["https://example.com/img.jpg"]


# ── MessageBuffer ──────────────────────────────────────────

class TestMessageBuffer:
    def _make_msg(self, msg_id: str, content: str = "hi") -> Message:
        return Message(
            sender_id="1", sender_name="A", content=content,
            timestamp="t", message_id=msg_id,
        )

    def test_add_and_count(self):
        buf = MessageBuffer(maxlen=10)
        buf.add(self._make_msg("1"))
        buf.add(self._make_msg("2"))
        assert buf.count == 2

    def test_dedup(self):
        buf = MessageBuffer(maxlen=10)
        buf.add(self._make_msg("1"))
        buf.add(self._make_msg("1"))  # duplicate
        assert buf.count == 1

    def test_sliding_window(self):
        buf = MessageBuffer(maxlen=3)
        for i in range(5):
            buf.add(self._make_msg(str(i), f"msg{i}"))
        assert buf.count == 3
        contents = [m.content for m in buf.messages]
        assert contents == ["msg2", "msg3", "msg4"]

    def test_get_recent(self):
        buf = MessageBuffer(maxlen=10)
        for i in range(5):
            buf.add(self._make_msg(str(i), f"msg{i}"))
        recent = buf.get_recent(limit=2)
        assert len(recent) == 2
        assert recent[0]["content"] == "msg3"
        assert recent[1]["content"] == "msg4"

    def test_get_since(self):
        buf = MessageBuffer(maxlen=10)
        t0 = time.time()
        buf.add(self._make_msg("old", "old"))
        # Manually set received_at to past
        buf.messages[-1].received_at = t0 - 10
        buf.add(self._make_msg("new", "new"))
        result = buf.get_since(t0 - 1)
        assert len(result) == 1
        assert result[0].content == "new"

    def test_apply_summary(self):
        buf = MessageBuffer()
        buf.apply_summary("summary1")
        assert buf.compressed_summary == "summary1"
        buf.apply_summary("summary2")
        assert buf.compressed_summary == "summary1\nsummary2"

    def test_compress_pending_flag(self):
        buf = MessageBuffer(maxlen=100, compress_every=3)
        assert buf._compress_pending is False
        for i in range(3):
            buf.add(self._make_msg(str(i)))
        assert buf._compress_pending is True

    def test_seen_ids_trimming(self):
        buf = MessageBuffer(maxlen=5)
        # Add more messages than 2*maxlen to trigger trim
        for i in range(15):
            buf.add(self._make_msg(str(i)))
        # _seen_ids should be trimmed to only IDs still in messages
        assert len(buf._seen_ids) <= 10


# ── ContextManager ─────────────────────────────────────────

class TestContextManager:
    def test_get_context_empty(self, config):
        ctx = ContextManager(config)
        result = ctx.get_context("-100111")
        assert result["target"] == "-100111"
        assert result["message_count"] == 0
        assert result["messages"] == []
        assert result["compressed_summary"] is None

    def test_add_message_and_get_context(self, config):
        ctx = ContextManager(config)
        msg = Message(
            sender_id="42", sender_name="Bob", content="hello",
            timestamp="t", message_id="1", chat_id="-100111",
        )
        ctx.add_message("-100111", msg)
        result = ctx.get_context("-100111", limit=10)
        assert result["message_count"] == 1
        assert result["messages"][0]["content"] == "hello"

    def test_buffer_stats(self, config):
        ctx = ContextManager(config)
        ctx.add_message("-100111", Message(
            sender_id="1", sender_name="A", content="a",
            timestamp="t", message_id="1",
        ))
        ctx.add_message("-100222", Message(
            sender_id="2", sender_name="B", content="b",
            timestamp="t", message_id="2",
        ))
        stats = ctx.buffer_stats
        assert stats["total_messages_buffered"] == 2
        assert stats["chats_tracked"] == 2
        assert sorted(stats["active_chat_ids"]) == ["-100111", "-100222"]

    def test_get_messages_since(self, config):
        ctx = ContextManager(config)
        t0 = time.time()
        ctx.add_message("-100111", Message(
            sender_id="1", sender_name="A", content="msg",
            timestamp="t", message_id="1",
        ))
        result = ctx.get_messages_since("-100111", t0 - 1)
        assert len(result) == 1

    def test_get_messages_since_empty(self, config):
        ctx = ContextManager(config)
        assert ctx.get_messages_since("-999", 0) == []


# ── ContextManager._get_sender_name ───────────────────────

class TestGetSenderName:
    def test_first_last(self):
        assert ContextManager._get_sender_name({"first_name": "John", "last_name": "Doe"}) == "John Doe"

    def test_first_only(self):
        assert ContextManager._get_sender_name({"first_name": "Alice"}) == "Alice"

    def test_username_fallback(self):
        assert ContextManager._get_sender_name({"username": "bob_bot"}) == "bob_bot"

    def test_id_fallback(self):
        assert ContextManager._get_sender_name({"id": 42}) == "42"

    def test_empty(self):
        assert ContextManager._get_sender_name({}) == "?"


# ── ContextManager._format_timestamp ──────────────────────

class TestFormatTimestamp:
    def test_valid_timestamp(self):
        ts = ContextManager._format_timestamp(1700000000)
        assert "2023" in ts  # 2023-11-14

    def test_zero_timestamp(self):
        ts = ContextManager._format_timestamp(0)
        assert "T" in ts  # ISO format


# ── ContextManager._process_entities ──────────────────────

class TestProcessEntities:
    def _make_ctx(self):
        config = Config(bot_token="123456789:AAA")
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        return ctx

    def test_no_entities(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("hello world", [])
        assert text == "hello world"
        assert at_me is False

    def test_mention_bot(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("@testbot hi", [
            {"type": "mention", "offset": 0, "length": 8}
        ])
        assert at_me is True

    def test_mention_other(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("@someone hi", [
            {"type": "mention", "offset": 0, "length": 8}
        ])
        assert at_me is False

    def test_text_mention_bot(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("Bot hi", [
            {"type": "text_mention", "offset": 0, "length": 3, "user": {"id": 123456789}}
        ])
        assert at_me is True

    def test_bot_command_with_bot_name(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("/start@testbot", [
            {"type": "bot_command", "offset": 0, "length": 14}
        ])
        assert at_me is True

    def test_bot_command_without_bot_name(self):
        ctx = self._make_ctx()
        text, at_me = ctx._process_entities("/start", [
            {"type": "bot_command", "offset": 0, "length": 6}
        ])
        assert at_me is False


# ── ContextManager._parse_message ─────────────────────────

class TestParseMessage:
    def _make_ctx(self):
        config = Config(bot_token="123456789:AAA")
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        return ctx

    @pytest.mark.asyncio
    async def test_plain_text(self):
        ctx = self._make_ctx()
        content, at_me, imgs = await ctx._parse_message({
            "text": "hello world",
            "chat": {"id": 1},
            "from": {"id": 42, "first_name": "A"},
        })
        assert content == "hello world"
        assert at_me is False
        assert imgs == []

    @pytest.mark.asyncio
    async def test_reply(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "text": "yes",
            "reply_to_message": {
                "from": {"id": 10, "first_name": "Bob"},
                "text": "original message",
            },
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "回复了 Bob(10)" in content
        assert "original message" in content
        assert "yes" in content

    @pytest.mark.asyncio
    async def test_reply_truncation(self):
        ctx = self._make_ctx()
        long_text = "x" * 100
        content, _, _ = await ctx._parse_message({
            "text": "reply",
            "reply_to_message": {
                "from": {"id": 10, "first_name": "B"},
                "text": long_text,
            },
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "…" in content

    @pytest.mark.asyncio
    async def test_forward_from_user(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "text": "forwarded text",
            "forward_from": {"id": 50, "first_name": "Charlie"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "转发自 Charlie" in content

    @pytest.mark.asyncio
    async def test_forward_from_chat(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "text": "channel post",
            "forward_from_chat": {"id": -100, "title": "News Channel"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "转发自 News Channel" in content

    @pytest.mark.asyncio
    async def test_sticker(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "sticker": {"emoji": "😀", "file_id": "abc"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[贴纸😀]" in content

    @pytest.mark.asyncio
    async def test_video(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "video": {"file_id": "v1"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[视频]" in content

    @pytest.mark.asyncio
    async def test_animation_is_gif(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "animation": {"file_id": "a1"},
            "document": {"file_name": "anim.gif"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[GIF]" in content
        assert "[文件:" not in content  # animation should suppress document

    @pytest.mark.asyncio
    async def test_voice(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "voice": {"file_id": "v1"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[语音]" in content

    @pytest.mark.asyncio
    async def test_document(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "document": {"file_name": "report.pdf", "file_id": "d1"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[文件: report.pdf]" in content

    @pytest.mark.asyncio
    async def test_location(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "location": {"latitude": 39.9, "longitude": 116.4},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[位置: 39.9,116.4]" in content

    @pytest.mark.asyncio
    async def test_contact(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "contact": {"first_name": "Dave", "phone_number": "+1234567890"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "联系人" in content
        assert "Dave" in content

    @pytest.mark.asyncio
    async def test_poll(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "poll": {"question": "Best language?", "options": []},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[投票: Best language?]" in content

    @pytest.mark.asyncio
    async def test_new_members(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "new_chat_members": [
                {"id": 1, "first_name": "Eve"},
                {"id": 2, "first_name": "Frank"},
            ],
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "Eve" in content
        assert "Frank" in content
        assert "加入了群聊" in content

    @pytest.mark.asyncio
    async def test_left_member(self):
        ctx = self._make_ctx()
        content, _, _ = await ctx._parse_message({
            "left_chat_member": {"id": 3, "first_name": "Grace"},
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "Grace" in content
        assert "离开了群聊" in content

    @pytest.mark.asyncio
    async def test_photo_without_bot(self):
        """Photo without bot client should not crash."""
        ctx = self._make_ctx()
        ctx.bot = None
        content, _, imgs = await ctx._parse_message({
            "photo": [
                {"file_id": "sm", "file_size": 100},
                {"file_id": "lg", "file_size": 5000},
            ],
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "[图片]" in content
        assert imgs == []

    @pytest.mark.asyncio
    async def test_caption_with_photo(self):
        ctx = self._make_ctx()
        ctx.bot = None
        content, _, _ = await ctx._parse_message({
            "caption": "nice photo!",
            "photo": [{"file_id": "p1", "file_size": 1000}],
            "chat": {"id": 1},
            "from": {"id": 42},
        })
        assert "nice photo!" in content
        # Should NOT add [图片] when caption is present
        assert "[图片]" not in content


# ── ContextManager._handle_message ────────────────────────

class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_monitored_group_message(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": -100111, "type": "supergroup"},
            "from": {"id": 42, "first_name": "Alice"},
            "text": "hello group",
            "date": 1700000000,
        })
        assert ctx._buffers["-100111"].count == 1

    @pytest.mark.asyncio
    async def test_unmonitored_chat_ignored(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": -100999, "type": "supergroup"},
            "from": {"id": 42, "first_name": "Alice"},
            "text": "ignored",
            "date": 1700000000,
        })
        assert "-100999" not in ctx._buffers

    @pytest.mark.asyncio
    async def test_private_user_whitelist(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        # User 999 is whitelisted
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 999, "first_name": "Admin"},
            "text": "hi bot",
            "date": 1700000000,
        })
        assert ctx._buffers["999"].count == 1

    @pytest.mark.asyncio
    async def test_private_user_not_whitelisted(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        # User 888 is NOT whitelisted
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": 888, "type": "private"},
            "from": {"id": 888, "first_name": "Stranger"},
            "text": "hi",
            "date": 1700000000,
        })
        assert "888" not in ctx._buffers

    @pytest.mark.asyncio
    async def test_empty_message_ignored(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": -100111, "type": "supergroup"},
            "from": {"id": 42, "first_name": "A"},
            "date": 1700000000,
            # No text, no media — empty content
        })
        assert "-100111" not in ctx._buffers

    @pytest.mark.asyncio
    async def test_self_message_marked(self, config):
        ctx = ContextManager(config)
        ctx._bot_username = "testbot"
        await ctx._handle_message({
            "message_id": 1,
            "chat": {"id": -100111, "type": "supergroup"},
            "from": {"id": 123456789, "first_name": "Bot"},
            "text": "bot reply",
            "date": 1700000000,
        })
        buf = ctx._buffers.get("-100111")
        assert buf is not None
        assert buf.messages[0].is_self is True
