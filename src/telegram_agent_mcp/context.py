"""Message buffer & long-polling listener for Telegram message context."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from .config import Config

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


@dataclass
class Message:
    """Standardized message format."""

    sender_id: str
    sender_name: str
    content: str
    timestamp: str  # ISO 8601
    message_id: str
    chat_id: str = ""
    is_at_me: bool = False
    is_self: bool = False
    image_urls: list[str] = field(default_factory=list)
    received_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = {
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "message_id": str(self.message_id),
            "chat_id": self.chat_id,
            "is_at_me": self.is_at_me,
            "is_self": self.is_self,
        }
        if self.image_urls:
            d["image_urls"] = self.image_urls
        return d


class MessageBuffer:
    """Per-chat sliding window message buffer with compression."""

    def __init__(self, maxlen: int = 100, compress_every: int = 30):
        self.messages: deque[Message] = deque(maxlen=maxlen)
        self._seen_ids: set[str] = set()
        self.compressed_summary: str | None = None
        self._msg_since_compress: int = 0
        self._compress_every = compress_every
        self._compress_pending = False

    def add(self, msg: Message) -> None:
        """Add a message with dedup by message_id."""
        if msg.message_id and msg.message_id in self._seen_ids:
            return
        if msg.message_id:
            self._seen_ids.add(msg.message_id)
            max_ids = (self.messages.maxlen or 100) * 2
            if len(self._seen_ids) > max_ids:
                self._seen_ids = {m.message_id for m in self.messages if m.message_id}
        self.messages.append(msg)
        self._msg_since_compress += 1

        if self._msg_since_compress >= self._compress_every:
            self._compress_pending = True

    def apply_summary(self, new_summary: str) -> None:
        """Append a compressed summary block."""
        if self.compressed_summary:
            self.compressed_summary = self.compressed_summary + "\n" + new_summary
        else:
            self.compressed_summary = new_summary
        logger.debug("Summary updated. Length: %d", len(self.compressed_summary))

    def get_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent `limit` messages as dicts."""
        msgs = list(self.messages)
        return [m.to_dict() for m in msgs[-limit:]]

    def get_since(self, since: float) -> list[Message]:
        """Return messages with received_at >= since."""
        return [m for m in self.messages if m.received_at >= since]

    @property
    def count(self) -> int:
        return len(self.messages)


class ContextManager:
    """Manages message buffers and the Telegram long-polling listener."""

    def __init__(self, config: Config, bot=None):
        self.config = config
        self.bot = bot  # TelegramClient
        self._bot_username: str = ""
        self._buffers: dict[str, MessageBuffer] = {}
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._update_offset: int | None = None

    def _buffer_key(self, chat_id: str) -> str:
        return str(chat_id)

    def _get_or_create_buffer(self, key: str) -> MessageBuffer:
        if key not in self._buffers:
            self._buffers[key] = MessageBuffer(
                maxlen=self.config.buffer_size,
                compress_every=self.config.compress_every,
            )
        return self._buffers[key]

    # ── Public API ──────────────────────────────────────────

    def start(self, bot_username: str = "") -> None:
        """Start the background long-polling listener task."""
        if self._poll_task is not None:
            return
        self._bot_username = bot_username.lower().lstrip("@")
        self._running = True
        self._poll_task = asyncio.get_event_loop().create_task(self._poll_loop())
        logger.info("Long-polling listener started")

    async def stop(self) -> None:
        """Stop the long-polling listener."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Long-polling listener stopped")

    def get_context(self, chat_id: str, limit: int = 20) -> dict:
        """Get message context for a chat."""
        key = self._buffer_key(chat_id)
        buf = self._buffers.get(key)

        if buf is None:
            return {
                "chat_id": chat_id,
                "compressed_summary": None,
                "message_count": 0,
                "messages": [],
            }

        return {
            "chat_id": chat_id,
            "compressed_summary": buf.compressed_summary,
            "message_count": buf.count,
            "messages": buf.get_recent(limit),
        }

    def add_message(self, chat_id: str, msg: Message) -> None:
        """Directly add a message to the buffer for a chat."""
        key = self._buffer_key(chat_id)
        buf = self._get_or_create_buffer(key)
        buf.add(msg)

    def get_messages_since(self, chat_id: str, since: float) -> list[Message]:
        """Return messages received after `since` for a chat."""
        key = self._buffer_key(chat_id)
        buf = self._buffers.get(key)
        if buf is None:
            return []
        return buf.get_since(since)

    @property
    def buffer_stats(self) -> dict:
        """Summary stats for check_status."""
        total = sum(b.count for b in self._buffers.values())
        return {
            "total_messages_buffered": total,
            "chats_tracked": len(self._buffers),
            "active_chat_ids": list(self._buffers.keys()),
        }

    # ── Long-Polling Loop ──────────────────────────────────

    async def _poll_loop(self) -> None:
        """Reconnecting long-polling listener loop."""
        retry_delay = 1.0
        max_retry = 30.0

        while self._running:
            try:
                updates = await self.bot.get_updates(
                    offset=self._update_offset,
                    timeout=self.config.polling_timeout,
                    allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post"],
                )
                retry_delay = 1.0

                for update in updates:
                    update_id = update.get("update_id", 0)
                    self._update_offset = update_id + 1

                    # Handle all message-like update types
                    message = (
                        update.get("message")
                        or update.get("edited_message")
                        or update.get("channel_post")
                        or update.get("edited_channel_post")
                    )
                    if message:
                        await self._handle_message(message)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Polling error: %s", e)
                if self._running:
                    logger.info("Retrying in %.1fs...", retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry)

    # ── Message Handling ───────────────────────────────────

    async def _handle_message(self, message: dict) -> None:
        """Process a Telegram message update."""
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "")

        # "from" may be absent for channel posts; fall back to sender_chat
        from_user = message.get("from") or message.get("sender_chat") or {}
        sender_id = str(from_user.get("id", ""))

        if chat_type == "private":
            if not self.config.is_user_monitored(sender_id):
                return
        else:
            if not self.config.is_chat_monitored(chat_id):
                return

        is_self = sender_id == self.config.bot_id

        content, is_at_me, image_urls = await self._parse_message(message)
        if not content.strip():
            return

        sender_name = self._get_sender_name(from_user)
        timestamp = self._format_timestamp(message.get("date", 0))
        message_id = str(message.get("message_id", ""))

        msg = Message(
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            message_id=message_id,
            chat_id=chat_id,
            is_at_me=is_at_me,
            is_self=is_self,
            image_urls=image_urls,
        )

        key = self._buffer_key(chat_id)
        buf = self._get_or_create_buffer(key)
        buf.add(msg)

        logger.debug(
            "Chat %s | %s: %s%s",
            chat_id, sender_name, content[:50],
            " [@me]" if is_at_me else "",
        )

    # ── Message Parsing ────────────────────────────────────

    async def _parse_message(self, message: dict) -> tuple[str, bool, list[str]]:
        """Parse a Telegram Message object into text content.

        Returns (content_string, is_at_me, image_urls).
        """
        parts: list[str] = []
        is_at_me = False
        image_urls: list[str] = []

        # Reply reference — Telegram includes the full replied message
        reply = message.get("reply_to_message")
        if reply:
            reply_sender = self._get_sender_name(reply.get("from", {}))
            reply_sender_id = str(reply.get("from", {}).get("id", "?"))
            reply_text = reply.get("text", reply.get("caption", ""))
            quote = reply_text[:50]
            if len(reply_text) > 50:
                quote += "…"
            parts.append(f"[回复了 {reply_sender}({reply_sender_id}) 的「{quote}」] ")

        # Forward info
        forward_from = message.get("forward_from")
        forward_from_chat = message.get("forward_from_chat")
        if forward_from:
            fwd_name = self._get_sender_name(forward_from)
            parts.append(f"[转发自 {fwd_name}] ")
        elif forward_from_chat:
            fwd_title = forward_from_chat.get("title", "?")
            parts.append(f"[转发自 {fwd_title}] ")

        # Main text content
        text = message.get("text", "")
        caption = message.get("caption", "")
        content_text = text or caption

        if content_text:
            entities = message.get("entities", message.get("caption_entities", []))
            processed_text, at_me = self._process_entities(content_text, entities)
            if at_me:
                is_at_me = True
            parts.append(processed_text)

        # Photo
        if message.get("photo"):
            photos = message["photo"]
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            file_id = largest.get("file_id", "")
            if file_id and self.bot:
                try:
                    file_info = await self.bot.get_file(file_id)
                    file_path = file_info.get("file_path", "")
                    if file_path:
                        image_urls.append(self.bot.get_file_url(file_path))
                except Exception as e:
                    logger.warning("Failed to get file URL: %s", e)
            if not text and not caption:
                parts.append("[图片]")

        # Sticker
        if message.get("sticker"):
            emoji = message["sticker"].get("emoji", "")
            parts.append(f"[贴纸{emoji}]")

        # Video / GIF
        if message.get("animation"):
            parts.append("[GIF]")
        elif message.get("video"):
            parts.append("[视频]")

        # Voice / Audio
        if message.get("voice"):
            parts.append("[语音]")
        if message.get("audio"):
            title = message["audio"].get("title", "?")
            parts.append(f"[音频: {title}]")

        # Document (but not GIF)
        if message.get("document") and not message.get("animation"):
            filename = message["document"].get("file_name", "?")
            parts.append(f"[文件: {filename}]")

        # Location
        if message.get("location"):
            loc = message["location"]
            parts.append(f"[位置: {loc.get('latitude', '?')},{loc.get('longitude', '?')}]")

        # Contact
        if message.get("contact"):
            contact = message["contact"]
            parts.append(f"[联系人: {contact.get('first_name', '')} {contact.get('phone_number', '')}]")

        # Poll
        if message.get("poll"):
            poll = message["poll"]
            parts.append(f"[投票: {poll.get('question', '?')}]")

        # Member changes
        new_members = message.get("new_chat_members", [])
        if new_members:
            names = [self._get_sender_name(m) for m in new_members]
            parts.append(f"[{', '.join(names)} 加入了群聊]")

        left_member = message.get("left_chat_member")
        if left_member:
            parts.append(f"[{self._get_sender_name(left_member)} 离开了群聊]")

        content = "".join(parts).strip()
        return content, is_at_me, image_urls

    def _process_entities(self, text: str, entities: list[dict]) -> tuple[str, bool]:
        """Process Telegram message entities for @mention detection.

        Returns (text, is_at_me).
        """
        if not entities:
            return text, False

        is_at_me = False

        for entity in entities:
            etype = entity.get("type", "")
            offset = entity.get("offset", 0)
            length = entity.get("length", 0)
            mention_text = text[offset:offset + length]

            if etype == "mention":
                username = mention_text.lstrip("@").lower()
                if username == self._bot_username:
                    is_at_me = True

            elif etype == "text_mention":
                user = entity.get("user", {})
                if str(user.get("id", "")) == self.config.bot_id:
                    is_at_me = True

            elif etype == "bot_command":
                if "@" in mention_text:
                    cmd_bot = mention_text.split("@", 1)[1].lower()
                    if cmd_bot == self._bot_username:
                        is_at_me = True

        return text, is_at_me

    # ── Helpers ────────────────────────────────────────────

    @staticmethod
    def _get_sender_name(user: dict) -> str:
        """Extract display name from a Telegram User or Chat object."""
        # Channel/group chats use "title" instead of first/last name
        title = user.get("title", "")
        if title:
            return title
        first = user.get("first_name", "")
        last = user.get("last_name", "")
        name = f"{first} {last}".strip()
        return name or user.get("username", str(user.get("id", "?")))

    @staticmethod
    def _format_timestamp(unix_ts: int) -> str:
        """Convert Unix timestamp to ISO 8601 string in CST."""
        if unix_ts <= 0:
            return datetime.now(CST).isoformat()
        return datetime.fromtimestamp(unix_ts, tz=CST).isoformat()
