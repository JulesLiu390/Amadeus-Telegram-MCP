"""Message buffer & long-polling listener for Telegram message context."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from .config import Config
from .topics import learned_topic_name, make_target, topic_thread_id

# PetGPT 会校验图片魔数，只认这几种；别的图片格式改用缩略图
VIEWABLE_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}


def _thumbnail_id(obj: dict) -> str:
    """Bot API 新版字段叫 thumbnail，旧版叫 thumb。"""
    thumb = obj.get("thumbnail") or obj.get("thumb") or {}
    return thumb.get("file_id", "")

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
    # 编辑过的消息：Telegram 的编辑不换 message_id，下游按 ID 去重会把新内容当
    # 重复丢掉。带上这个标记，PetGPT 才知道「同一个 ID、内容变了」是一次编辑。
    edited: bool = False
    edit_date: str = ""  # ISO 8601，编辑发生的时间

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
        if self.edited:
            d["edited"] = True
            d["edit_date"] = self.edit_date
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

    def upsert(self, msg: Message) -> None:
        """编辑过的消息：原地换掉同 ID 的那条；不在缓冲里（太旧被挤掉了）就当新消息加。"""
        if msg.message_id and msg.message_id in self._seen_ids:
            for i, old in enumerate(self.messages):
                if old.message_id == msg.message_id:
                    self.messages[i] = msg
                    return
        self.add(msg)

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
        # 话题 target（"chat:thread"）→ 话题名。Bot API 没有列话题的接口，只能边收边记
        self._topic_names: dict[str, str] = {}

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
        """Get message context for a chat.

        Returns dict with 'target' key (unified with QQ MCP interface).
        """
        key = self._buffer_key(chat_id)
        buf = self._buffers.get(key)

        if buf is None:
            return {
                "target": chat_id,
                "compressed_summary": None,
                "message_count": 0,
                "messages": [],
            }

        return {
            "target": chat_id,
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

    def topic_name(self, target: str) -> str:
        """已知的话题名；没见过的话题返回空串。"""
        return self._topic_names.get(target, "")

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
                    edited = bool(update.get("edited_message") or update.get("edited_channel_post"))
                    if message:
                        await self._handle_message(message, edited=edited)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Polling error: %s", e)
                if self._running:
                    logger.info("Retrying in %.1fs...", retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry)

    # ── Alias Resolution ─────────────────────────────────

    async def _lazy_resolve_alias(self, numeric_id: str) -> bool:
        """Try to match a numeric chat_id to a configured @username alias."""
        if not self.config.chat_ids or not self.bot:
            return False
        # Only try if there are unresolved @usernames in config
        unresolved = [c for c in self.config.chat_ids
                      if c.startswith("@") and c not in self.config._chat_aliases]
        if not unresolved:
            return False
        try:
            chat_info = await self.bot.get_chat(numeric_id)
            username = chat_info.get("username", "")
            if username:
                at_username = f"@{username}"
                if at_username in self.config.chat_ids:
                    self.config._chat_aliases[at_username] = numeric_id
                    self.config.chat_ids.add(numeric_id)
                    logger.info("Poller lazy-resolved %s → %s", at_username, numeric_id)
                    return True
        except Exception as e:
            logger.debug("Lazy alias resolution failed for %s: %s", numeric_id, e)
        return False

    # ── User Alias Resolution ─────────────────────────────

    def _track_user_alias(self, sender_id: str, from_user: dict) -> None:
        """Always record @username → numeric_id mapping for private chat senders."""
        username = from_user.get("username", "")
        if not username:
            return
        at_username = f"@{username}"
        if at_username not in self.config._user_aliases:
            self.config._user_aliases[at_username] = sender_id
            if self.config.user_ids is not None:
                self.config.user_ids.add(sender_id)
            logger.info("Tracked user alias %s → %s", at_username, sender_id)

    # ── Message Handling ───────────────────────────────────

    async def _handle_message(self, message: dict, edited: bool = False) -> None:
        """Process a Telegram message update."""
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "")
        # 论坛群里每个话题是一个独立的 target；General 和普通群就是 chat_id 本身
        thread_id = topic_thread_id(message)
        target = make_target(chat_id, thread_id)

        # 话题名要在内容过滤之前记：创建/改名话题的是没有正文的服务消息
        learned = learned_topic_name(message)
        if learned:
            self._topic_names[make_target(chat_id, learned[0])] = learned[1]

        # "from" may be absent for channel posts; fall back to sender_chat
        from_user = message.get("from") or message.get("sender_chat") or {}
        sender_id = str(from_user.get("id", ""))

        if chat_type == "private":
            if not self.config.is_user_monitored(sender_id):
                # Try matching from.username against @username in user_ids
                self._track_user_alias(sender_id, from_user)
                if not self.config.is_user_monitored(sender_id):
                    return
            # Always track alias for resolved private chats (so tools can resolve @username)
            self._track_user_alias(sender_id, from_user)
        else:
            # 白名单可以写整个群（含所有话题），也可以只写某个话题 "chat:thread"
            if not (self.config.is_chat_monitored(chat_id)
                    or (thread_id and self.config.is_chat_monitored(target))):
                # Lazy alias resolution: resolve numeric ID → @username
                if not await self._lazy_resolve_alias(chat_id):
                    return

        is_self = sender_id == self.config.bot_id

        # Use @username as sender_id for display (if available)
        username = from_user.get("username", "")
        sender_id_display = f"@{username}" if username else sender_id

        content, is_at_me, image_urls = await self._parse_message(message)
        if not content.strip():
            return

        sender_name = self._get_sender_name(from_user)
        timestamp = self._format_timestamp(message.get("date", 0))
        message_id = str(message.get("message_id", ""))

        msg = Message(
            sender_id=sender_id_display,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            message_id=message_id,
            chat_id=target,
            is_at_me=is_at_me,
            is_self=is_self,
            image_urls=image_urls,
            edited=edited,
            edit_date=self._format_timestamp(message.get("edit_date", 0)) if edited else "",
        )

        key = self._buffer_key(target)
        buf = self._get_or_create_buffer(key)
        if edited:
            buf.upsert(msg)
        else:
            buf.add(msg)

        logger.debug(
            "Chat %s | %s: %s%s",
            target, sender_name, content[:50],
            " [@me]" if is_at_me else "",
        )

    # ── Message Parsing ────────────────────────────────────

    async def _file_url(self, file_id: str) -> str | None:
        """file_id → 可下载的 URL；拿不到（没有 bot、文件过大、API 报错）就是 None。"""
        if not file_id or not self.bot:
            return None
        try:
            file_path = (await self.bot.get_file(file_id)).get("file_path", "")
        except Exception as e:
            logger.warning("Failed to get file URL: %s", e)
            return None
        return self.bot.get_file_url(file_path) if file_path else None

    async def _parse_message(self, message: dict) -> tuple[str, bool, list[str]]:
        """Parse a Telegram Message object into text content.

        Returns (content_string, is_at_me, image_urls).
        """
        parts: list[str] = []
        is_at_me = False
        image_urls: list[str] = []

        # Reply reference — Telegram includes the full replied message
        reply = message.get("reply_to_message")
        # 论坛话题里的普通消息，reply_to_message 是「创建话题」那条服务消息——那不是
        # 回复，不跳过的话每条话题消息都会被标成「回复了 xxx 的「」」
        if reply and reply.get("forum_topic_created"):
            reply = None
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
            url = await self._file_url(largest.get("file_id", ""))
            if url:
                image_urls.append(url)
            # 有配文也要标：图片万一没到模型那里（下载失败、被裁掉），它至少知道
            # 这里有过一张图，不会回「只看到你发了这一句」
            parts.append(" [图片]" if content_text else "[图片]")

        # Sticker —— 静态贴纸本身就是 webp，直接给模型看；动态（.tgs）和
        # 视频（.webm）贴纸不是图片，用它的缩略图
        sticker = message.get("sticker")
        if sticker:
            if sticker.get("is_animated") or sticker.get("is_video"):
                url = await self._file_url(_thumbnail_id(sticker))
            else:
                url = await self._file_url(sticker.get("file_id", ""))
            if url:
                image_urls.append(url)
            parts.append(f"[贴纸{sticker.get('emoji', '')}]")

        # Video / GIF —— Telegram 的 GIF 其实是 mp4，看不了，给缩略图（第一帧）
        if message.get("animation"):
            url = await self._file_url(_thumbnail_id(message["animation"]))
            if url:
                image_urls.append(url)
            parts.append("[GIF]")
        elif message.get("video"):
            parts.append("[视频]")

        # Voice / Audio
        if message.get("voice"):
            parts.append("[语音]")
        if message.get("audio"):
            title = message["audio"].get("title", "?")
            parts.append(f"[音频: {title}]")

        # Document (but not GIF)。「作为文件发送」的图片也走这里——不当成图片的话
        # 模型就只看到一个文件名
        if message.get("document") and not message.get("animation"):
            doc = message["document"]
            filename = doc.get("file_name", "?")
            mime = (doc.get("mime_type") or "").lower()
            if mime in VIEWABLE_IMAGE_MIMES:
                url = await self._file_url(doc.get("file_id", ""))
            elif mime.startswith("image/"):
                # HEIC 之类模型/PetGPT 不收的格式，退到缩略图
                url = await self._file_url(_thumbnail_id(doc))
            else:
                url = None
            if url:
                image_urls.append(url)
                parts.append(f"[图片文件: {filename}]")
            else:
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
