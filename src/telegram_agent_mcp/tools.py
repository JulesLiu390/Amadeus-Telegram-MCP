"""MCP Tools definitions for Telegram."""

import asyncio
import hashlib
import logging
import random
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any

from mcp.server.fastmcp import Context
from mcp.types import SamplingMessage, TextContent

from .config import Config
from .context import ContextManager, Message
from .telegram_api import TelegramClient
from .topics import GENERAL_TOPIC_NAME, make_target, split_target

logger = logging.getLogger(__name__)

# Rate limiter state: chat_id -> last_send_timestamp
_last_send: dict[str, float] = {}
RATE_LIMIT_SECONDS = 3.0
CST = timezone(timedelta(hours=8))

# ── Duplicate send detection ────────────────────────────
_DEDUP_WINDOW_SECONDS = 60.0
_sent_history: dict[str, deque[tuple[str, float]]] = {}

# ── </分段> tag-based message splitting ────────────────
# 不只认 ASCII 的 </分段>：模型常照着 PetGPT 套在消息外面的 ‹…› 分隔符写成 ‹/分段›，
# 线上原样发出去过「明明聪明得很好不好。‹/分段›别拆我台啊姐姐」。全角、书名号同理。
_SPLIT_TAG_RE = re.compile(r"[<‹〈＜《«]\s*[/／]?\s*分\s*段\s*[>›〉＞》»]")


def _split_by_tag(text: str) -> list[str] | None:
    """Split text on </分段> tags.

    Returns non-empty stripped segments when the tag is present. Returns None
    when no tag is found so callers can fall through to other split strategies.
    """
    if not text or not _SPLIT_TAG_RE.search(text):
        return None
    parts = _SPLIT_TAG_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _normalize_content(text: str) -> str:
    """Normalize text for dedup comparison."""
    text = unicodedata.normalize("NFKC", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _check_duplicate(target_key: str, content: str) -> str | None:
    """Check if content was sent to this target within the dedup window."""
    now = time.time()
    h = hashlib.md5(_normalize_content(content).encode()).hexdigest()

    history = _sent_history.get(target_key)
    if history is None:
        history = deque(maxlen=50)
        _sent_history[target_key] = history

    while history and now - history[0][1] > _DEDUP_WINDOW_SECONDS:
        history.popleft()

    for entry_hash, entry_time in history:
        if entry_hash == h:
            ago = int(now - entry_time)
            return (
                f"⚠️ 这条消息你在 {ago} 秒前已经发送过完全相同的内容，未重复发送。"
                f"如果确实需要重发，请稍作修改后重试。"
            )

    history.append((h, now))
    return None


# Chunking config — Telegram allows up to 4096 chars per message
CHUNK_MAX_CHARS = 200
HUMAN_DELAY_MS_PER_CHAR = 80
HUMAN_DELAY_MIN_MS = 300
HUMAN_DELAY_MAX_MS = 3000
TELEGRAM_MSG_LIMIT = 4096

_start_time: float = time.time()


def _human_delay_for_chunk(chunk: str) -> float:
    """Calculate a human-like delay (in seconds) based on chunk length."""
    base = len(chunk) * HUMAN_DELAY_MS_PER_CHAR
    jitter = random.uniform(0.7, 1.3)
    ms = max(HUMAN_DELAY_MIN_MS, min(int(base * jitter), HUMAN_DELAY_MAX_MS))
    return ms / 1000.0


def _chunk_message(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Split a long message into natural chunks for sequential sending.

    1. Always split on \\n\\n (paragraph boundary).
    2. If a paragraph <= max_chars, keep it whole.
    3. If a paragraph > max_chars:
       a. Split by sentence-enders, group so each chunk stays near max_chars.
       b. If still too long, split by clause delimiters.
    4. Final safety: hard-split at TELEGRAM_MSG_LIMIT.
    """
    text = text.strip()
    if not text:
        return []

    _PLACEHOLDER = "\x00"
    _ext_re = re.compile(
        r'\.(?:md|jpeg|jpg|png|py|js|ts|json|html|css|txt|csv|pdf|zip|gif|svg|mp3|mp4|wav)\b',
        re.IGNORECASE,
    )
    text = _ext_re.sub(lambda m: _PLACEHOLDER + m.group(0)[1:], text)

    paragraphs = re.split(r'\n\n+', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    _sentence_re = re.compile(
        r'(?<=(?<!\d)[.])'
        r'|(?<=[!?。！？~\n])'
    )
    _clause_re = re.compile(
        r'[，,、：:；;]'
        r'|(?:——|--)'
    )

    def _group_parts(parts: list[str], limit: int) -> list[str]:
        groups: list[str] = []
        buf = ''
        for p in parts:
            candidate = (buf + p) if buf else p
            if len(candidate) <= limit:
                buf = candidate
            else:
                if buf:
                    groups.append(buf)
                buf = p
        if buf:
            groups.append(buf)
        return groups

    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append(para)
            continue

        sentences = [s.strip() for s in _sentence_re.split(para) if s.strip()]
        grouped = _group_parts(sentences, max_chars)

        for chunk in grouped:
            if len(chunk) <= max_chars:
                chunks.append(chunk)
            else:
                clauses = [c.strip() for c in _clause_re.split(chunk) if c.strip()]
                grouped2 = _group_parts(clauses, max_chars)
                chunks.extend(grouped2)

    # Safety: hard-split any chunk exceeding Telegram's 4096 limit
    safe_chunks: list[str] = []
    for c in chunks:
        while len(c) > TELEGRAM_MSG_LIMIT:
            safe_chunks.append(c[:TELEGRAM_MSG_LIMIT])
            c = c[TELEGRAM_MSG_LIMIT:]
        if c:
            safe_chunks.append(c)

    return [c.replace(_PLACEHOLDER, ".") for c in safe_chunks if c]


def _decide_chunks(
    content: str,
    split_content: bool,
    num_chunks: int | None,
) -> list[str]:
    """Decide how to split outgoing message content into chunks.

    Priority:
      1. num_chunks == 1        -> single message, tag kept as literal text
      2. num_chunks >= 2        -> punctuation split, then merge toward N chunks
      3. </分段> tag in content -> exact manual split, tag stripped
      4. split_content & >100   -> Telegram-style long-message auto split
      5. over Telegram limit    -> hard split for API safety
      6. default                -> single message
    """
    stripped = content.strip()

    if num_chunks is not None and num_chunks == 1:
        return [stripped] if stripped else []

    if num_chunks is not None and num_chunks >= 2 and stripped:
        fine_chunks = _chunk_message(content)
        if len(fine_chunks) <= num_chunks:
            return fine_chunks
        chunks: list[str] = []
        per_group = len(fine_chunks) / num_chunks
        for i in range(num_chunks):
            start = round(i * per_group)
            end = round((i + 1) * per_group)
            chunks.append("\n".join(fine_chunks[start:end]))
        return chunks

    tag_chunks = _split_by_tag(content)
    if tag_chunks is not None:
        return tag_chunks

    if split_content and len(stripped) > 100:
        return _chunk_message(content)

    if len(stripped) > TELEGRAM_MSG_LIMIT:
        return _chunk_message(stripped, max_chars=TELEGRAM_MSG_LIMIT)

    return [stripped] if stripped else []


# ── 会话列表 ──────────────────────────────────────────────

GROUP_CHAT_TYPES = {"group", "supergroup"}


def _split_known_chats(chats: list[dict]) -> tuple[list[dict], list[dict]]:
    """把已知会话分成群和私聊，输出格式和 qq-mcp 的 get_group_list / get_friend_list 一致。

    前端（PetGPT 的 Watch Targets）按 group_id/group_name 和 user_id/nickname 渲染，
    两个 MCP 格式对齐它就不用分别处理。频道（channel）两边都不放：bot 在频道里
    只能发不能聊，不是能「监听」的对象。
    """
    groups: list[dict] = []
    friends: list[dict] = []
    for c in chats:
        if c.get("type") in GROUP_CHAT_TYPES:
            groups.append({
                "group_id": c["chat_id"],
                "group_name": c.get("title", ""),
                "member_count": c.get("member_count", 0),
            })
        elif c.get("type") == "private":
            friends.append({"user_id": c["chat_id"], "nickname": c.get("title", "")})
    return groups, friends


def register_tools(
    mcp: Any, config: Config, bot: TelegramClient, ctx: ContextManager
) -> None:
    """Register all MCP tools on the FastMCP server instance."""

    async def _resolve_target(target: str) -> str:
        """Resolve @username to numeric ID; a ``:thread`` topic suffix is kept as is."""
        chat, thread = split_target(target)
        return make_target(await _resolve_chat(chat), thread)

    async def _resolve_chat(target: str) -> str:
        """Resolve @username to numeric ID with lazy caching."""
        # Try chat alias first (groups/channels)
        resolved = config.resolve_chat_id(target)
        if resolved != target:
            return resolved
        # Try user alias (private chats)
        resolved = config.resolve_user_id(target)
        if resolved != target:
            return resolved
        # Not yet resolved — try lazy resolution via getChat (works for public groups, not users)
        if target.startswith("@"):
            try:
                chat_info = await bot.get_chat(target)
                numeric_id = str(chat_info.get("id", ""))
                if numeric_id:
                    config._chat_aliases[target] = numeric_id
                    if config.chat_ids:
                        config.chat_ids.add(numeric_id)
                    logger.info("Lazy-resolved alias %s → %s", target, numeric_id)
                    return numeric_id
            except Exception as e:
                logger.warning("Failed to lazy-resolve %s: %s", target, e)
        return target

    async def _known_chats(with_member_count: bool = False) -> list[dict]:
        """bot 能知道的全部会话：白名单里配的 + 运行以来收到过消息的。

        Bot API 没有「列出我所在的群」这种接口，只能这样凑——所以 bot 刚启动、
        群里还没人说过话时，没写进白名单的群是看不见的。
        """
        # 白名单里的 @username 先换成数字 ID，免得和 buffer 里的同一个群重复
        ids: set[str] = set()
        if config.chat_ids:
            for cid in config.chat_ids:
                chat, thread = split_target(cid)
                ids.add(make_target(config.resolve_chat_id(chat), thread))
        ids.update(ctx.buffer_stats.get("active_chat_ids", []))

        # 论坛群的每个话题单独成一项，标题写成「群名 / 话题名」。同一个群的
        # getChat / 成员数只查一次
        infos: dict[str, dict | None] = {}
        counts: dict[str, int] = {}
        chats: list[dict] = []
        for target in sorted(ids):
            cid, thread = split_target(target)
            if cid not in infos:
                try:
                    infos[cid] = await bot.get_chat(cid)
                except Exception:
                    infos[cid] = None
            info = infos[cid]
            if info is None:
                chats.append({"chat_id": target, "title": "", "type": "unknown"})
                continue
            title = info.get("title", info.get("first_name", ""))
            if thread:
                title = f"{title} / {ctx.topic_name(target) or f'话题 #{thread}'}"
            elif info.get("is_forum"):
                title = f"{title} / {GENERAL_TOPIC_NAME}"
            chat = {"chat_id": target, "title": title, "type": info.get("type", "")}
            if with_member_count and chat["type"] in GROUP_CHAT_TYPES:
                if cid not in counts:
                    try:
                        counts[cid] = await bot.get_chat_member_count(cid)
                    except Exception:
                        counts[cid] = 0
                chat["member_count"] = counts[cid]
            chats.append(chat)
        return chats

    def _is_target_monitored(chat_id: str) -> bool:
        """Check if a target is monitored as either a chat or a user.

        A topic counts if its whole group is monitored or the topic itself is listed.
        """
        chat, thread = split_target(chat_id)
        if thread and config.is_chat_monitored(chat_id):
            return True
        return config.is_chat_monitored(chat) or config.is_user_monitored(chat)

    @mcp.tool()
    async def check_status() -> dict:
        """Check Telegram bot login status and connection info."""
        try:
            me = await bot.get_me()
        except Exception as e:
            return {
                "bot_running": False,
                "error": str(e),
            }

        monitored_chats = await _known_chats()

        return {
            "bot_running": True,
            "bot_id": str(me.get("id", "")),
            "bot_username": me.get("username", ""),
            "bot_name": me.get("first_name", ""),
            "uptime_seconds": int(time.time() - _start_time),
            "monitored_chats": monitored_chats,
            "monitor_all": config.chat_ids is None,
            "buffer_stats": ctx.buffer_stats,
        }

    @mcp.tool()
    async def get_group_list() -> dict:
        """List the Telegram groups the bot knows about.

        Telegram's Bot API cannot enumerate a bot's groups, so this covers groups in the
        configured whitelist plus any group that has sent a message since the bot started.
        A group the bot was added to but that has been silent since startup will not appear.

        Each topic of a forum group is listed as its own group, with group_id
        "chat_id:thread_id" and name "Group / Topic"; the forum's General topic is the
        bare chat_id. Topics are only known once someone has posted in them.
        """
        groups, _ = _split_known_chats(await _known_chats(with_member_count=True))
        return {"groups": groups}

    @mcp.tool()
    async def get_friend_list() -> dict:
        """List private chats the bot knows about (users who have messaged it since startup).

        Same Bot API limitation as get_group_list: a user only appears after messaging the bot.
        """
        _, friends = _split_known_chats(await _known_chats())
        return {"friends": friends}

    @mcp.tool()
    async def get_recent_context(
        target: str,
        target_type: str = "group",
        limit: int = 200,
    ) -> dict:
        """Get recent message context for a monitored Telegram chat.

        Returns all buffered messages (real-time only, no history backfill).
        Use compress_context to manually compress when needed.

        Args:
            target: Telegram chat ID (group, supergroup, or private chat), or
                "chat_id:thread_id" for one topic of a forum group.
            target_type: 'group' or 'private' (for interface compatibility).
            limit: Number of recent messages to return (default 200).
        """
        chat_id = await _resolve_target(target)
        if not _is_target_monitored(chat_id):
            return {"error": f"Chat {chat_id} is not monitored"}

        limit = max(1, limit)
        result = ctx.get_context(chat_id, limit)
        result["target"] = target  # preserve original @username

        try:
            chat_info = await bot.get_chat(chat_id)
            chat_type = chat_info.get("type", "")
            title = chat_info.get("title", chat_info.get("first_name", ""))
            if chat_type == "private":
                result["friend_name"] = title
            else:
                result["group_name"] = title
        except Exception:
            pass

        return result

    @mcp.tool()
    async def batch_get_recent_context(
        targets: list[dict],
        limit: int = 50,
    ) -> dict:
        """Batch query recent message context for multiple Telegram chats.

        Args:
            targets: List of objects with 'target' (chat ID) and 'target_type' ('group' or 'private').
            limit: Number of recent messages per chat (default 50).
        """
        limit = max(1, min(limit, 200))

        results: list[dict] = []
        for entry in targets:
            raw_id = entry.get("target", "") if isinstance(entry, dict) else str(entry)
            chat_id = await _resolve_target(raw_id)

            if not _is_target_monitored(chat_id):
                results.append({"target": raw_id, "error": f"Chat {raw_id} is not monitored"})
                continue

            result = ctx.get_context(chat_id, limit)
            result["target"] = raw_id  # preserve original @username

            try:
                chat_info = await bot.get_chat(chat_id)
                chat_type = chat_info.get("type", "")
                title = chat_info.get("title", chat_info.get("first_name", ""))
                if chat_type == "private":
                    result["friend_name"] = title
                else:
                    result["group_name"] = title
            except Exception:
                pass

            results.append(result)

        return {"results": results, "count": len(results)}

    @mcp.tool()
    async def send_message(
        target: str,
        content: str,
        target_type: str = "group",
        reply_to: int | None = None,
        split_content: bool = True,
        num_chunks: int | None = None,
    ) -> dict:
        """Send a message to a monitored Telegram chat.

        Preferred way to send multiple messages: insert `</分段>` in the content
        at each desired split point. Each segment becomes its own message and
        the tag itself is stripped.

        Args:
            target: Telegram chat ID, or "chat_id:thread_id" for a forum topic.
            content: Text message content. May contain `</分段>` markers to
                specify exact split points between messages.
            target_type: 'group' or 'private' (for interface compatibility).
            reply_to: Optional message ID to reply to.
            split_content: If True (and content has no `</分段>` tag), split
                long messages into multiple chunks with typing delay.
            num_chunks: If set, split the message into exactly this many chunks
                using natural punctuation boundaries. Overrides the `</分段>`
                tag. Set to 1 to force a single message.
        """
        chat_id = await _resolve_target(target)
        if not _is_target_monitored(chat_id):
            return {"success": False, "error": f"Chat {chat_id} is not monitored"}

        # Rate limit
        now = time.time()
        last = _last_send.get(chat_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - (now - last)
            return {"success": False, "error": f"Rate limited. Try again in {wait:.1f}s"}
        _last_send[chat_id] = now

        # Duplicate detection
        dup_warning = _check_duplicate(chat_id, content)
        if dup_warning:
            return {"success": False, "error": dup_warning}

        chunks = _decide_chunks(content, split_content, num_chunks)
        if not chunks:
            return {"success": False, "error": "Empty message content"}

        sent_ids: list[int] = []
        first_reply_to = reply_to
        # chat_id 可能是 "群:话题"，簿记都按它算；只有真正调 API 时才拆开
        api_chat, thread_id = split_target(chat_id)
        t0 = time.time()

        try:
            for i, chunk_text in enumerate(chunks):
                chunk_text = chunk_text.rstrip("。.")
                if not chunk_text:
                    continue

                # Send typing indicator
                try:
                    await bot.send_chat_action(api_chat, "typing", message_thread_id=thread_id)
                except Exception:
                    pass

                rto = first_reply_to if i == 0 else None
                result = await bot.send_message(
                    api_chat, chunk_text, reply_to_message_id=rto, message_thread_id=thread_id
                )

                msg_id = result.get("message_id", 0)
                sent_ids.append(msg_id)

                bot_sender_id = f"@{ctx._bot_username}" if ctx._bot_username else config.bot_id
                bot_msg = Message(
                    sender_id=bot_sender_id,
                    sender_name="bot",
                    content=chunk_text,
                    timestamp=datetime.now(CST).isoformat(),
                    message_id=str(msg_id),
                    chat_id=chat_id,
                    is_self=True,
                )
                ctx.add_message(chat_id, bot_msg)

                if i < len(chunks) - 1:
                    delay = _human_delay_for_chunk(chunk_text)
                    await asyncio.sleep(delay)

        except Exception as e:
            _last_send[chat_id] = last
            if sent_ids:
                return {
                    "success": False,
                    "error": f"Partial send ({len(sent_ids)}/{len(chunks)} chunks): {e}",
                    "message_ids": sent_ids,
                }
            return {"success": False, "error": str(e)}

        await asyncio.sleep(0.5)

        recent_msgs = ctx.get_messages_since(chat_id, t0)
        recent_lines: list[str] = []
        for m in recent_msgs:
            tag = "[bot(self)]" if m.is_self else f"[{m.sender_name}]"
            recent_lines.append(f"{tag} {m.content}")

        return {
            "success": True,
            "message_ids": sent_ids,
            "chunks": len(chunks),
            "target": target,
            "timestamp": datetime.now(CST).isoformat(),
            "recent_messages": recent_lines,
        }

    @mcp.tool()
    async def send_image(
        target: str,
        image: str,
        target_type: str = "group",
        reply_to: int | None = None,
    ) -> dict:
        """Send an image to a monitored Telegram chat.

        Args:
            target: Telegram chat ID, or "chat_id:thread_id" for a forum topic.
            image: Base64-encoded image data (raw base64, no prefix).
            target_type: 'group' or 'private' (for interface compatibility).
            reply_to: Optional message ID to reply to.
        """
        chat_id = await _resolve_target(target)
        if not _is_target_monitored(chat_id):
            return {"success": False, "error": f"Chat {chat_id} is not monitored"}

        # Rate limit
        now = time.time()
        last = _last_send.get(chat_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - (now - last)
            return {"success": False, "error": f"Rate limited. Try again in {wait:.1f}s"}
        _last_send[chat_id] = now

        api_chat, thread_id = split_target(chat_id)
        try:
            await bot.send_chat_action(api_chat, "upload_photo", message_thread_id=thread_id)
        except Exception:
            pass

        try:
            result = await bot.send_photo(
                api_chat,
                image,
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
            )
        except Exception as e:
            _last_send[chat_id] = last
            return {"success": False, "error": str(e)}

        msg_id = result.get("message_id", 0)

        bot_sender_id = f"@{ctx._bot_username}" if ctx._bot_username else config.bot_id
        bot_msg = Message(
            sender_id=bot_sender_id,
            sender_name="bot",
            content="[图片]",
            timestamp=datetime.now(CST).isoformat(),
            message_id=str(msg_id),
            chat_id=chat_id,
            is_self=True,
        )
        ctx.add_message(chat_id, bot_msg)

        return {
            "success": True,
            "message_id": str(msg_id),
            "target": target,
            "target_type": target_type,
            "timestamp": datetime.now(CST).isoformat(),
        }

    @mcp.tool()
    async def send_voice(
        target: str,
        audio: str,
        target_type: str = "group",
        reply_to: int | None = None,
    ) -> dict:
        """Send a voice message to a monitored Telegram chat (same contract as qq-mcp's send_voice).

        Args:
            target: Telegram chat ID, or "chat_id:thread_id" for a forum topic.
            audio: Base64-encoded audio (no prefix). MP3, M4A and OGG/OPUS are accepted
                by Telegram as voice notes; PetGPT's ElevenLabs TTS produces MP3.
            target_type: 'group' or 'private' (for interface compatibility).
            reply_to: Optional message ID to reply to.
        """
        chat_id = await _resolve_target(target)
        if not _is_target_monitored(chat_id):
            return {"success": False, "error": f"Chat {chat_id} is not monitored"}

        now = time.time()
        last = _last_send.get(chat_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - (now - last)
            return {"success": False, "error": f"Rate limited. Try again in {wait:.1f}s"}
        _last_send[chat_id] = now

        api_chat, thread_id = split_target(chat_id)
        try:
            await bot.send_chat_action(api_chat, "record_voice", message_thread_id=thread_id)
        except Exception:
            pass

        try:
            result = await bot.send_voice(
                api_chat, audio, reply_to_message_id=reply_to, message_thread_id=thread_id,
            )
        except Exception as e:
            _last_send[chat_id] = last
            return {"success": False, "error": str(e)}

        msg_id = result.get("message_id", 0)
        bot_sender_id = f"@{ctx._bot_username}" if ctx._bot_username else config.bot_id
        ctx.add_message(chat_id, Message(
            sender_id=bot_sender_id,
            sender_name="bot",
            content="[语音]",
            timestamp=datetime.now(CST).isoformat(),
            message_id=str(msg_id),
            chat_id=chat_id,
            is_self=True,
        ))
        return {
            "success": True,
            "message_id": str(msg_id),
            "target": target,
            "target_type": target_type,
            "timestamp": datetime.now(CST).isoformat(),
        }

    @mcp.tool()
    async def compress_context(
        target: str,
        ctx_mcp: Context,
        target_type: str = "group",
    ) -> dict:
        """Compress all buffered messages for a chat into a summary.

        This replaces raw messages with a compressed summary, freeing up the buffer.

        Args:
            target: Telegram chat ID, or "chat_id:thread_id" for a forum topic.
            target_type: 'group' or 'private' (for interface compatibility).
        """
        chat_id = await _resolve_target(target)
        if not _is_target_monitored(chat_id):
            return {"error": f"Chat {chat_id} is not monitored"}

        key = ctx._buffer_key(chat_id)
        buf = ctx._buffers.get(key)
        if buf is None or len(buf.messages) == 0:
            return {
                "success": True,
                "compressed": 0,
                "message": "No messages to compress",
                "compressed_summary": buf.compressed_summary if buf else None,
            }

        all_msgs = list(buf.messages)
        buf.messages.clear()
        buf._compress_pending = False
        buf._msg_since_compress = 0

        try:
            summary = await _llm_compress(ctx_mcp, all_msgs)
            method = "llm"
        except Exception as e:
            logger.warning("LLM compression failed, using rule-based: %s", e)
            summary = _rule_based_compress(all_msgs)
            method = "rule-based"

        buf.apply_summary(summary)
        logger.info("%s compressed %d messages for chat %s", method, len(all_msgs), chat_id)

        return {
            "success": True,
            "compressed": len(all_msgs),
            "method": method,
            "compressed_summary": buf.compressed_summary,
        }


async def _llm_compress(ctx_mcp: Context, messages: list) -> str:
    """Use the client's LLM (via MCP sampling) to compress messages into a summary."""
    lines = []
    for m in messages:
        lines.append(f"[{m.timestamp}] {m.sender_name}: {m.content}")
    chat_log = "\n".join(lines)

    result = await ctx_mcp.session.create_message(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=(
                        "请将以下聊天记录压缩为一段简洁的中文摘要，保留关键信息（话题、观点、重要发言者）。"
                        "摘要应在 300 字以内，不要使用列表格式，用自然段落描述。\n\n"
                        f"聊天记录：\n{chat_log}"
                    ),
                ),
            )
        ],
        max_tokens=8192,
        system_prompt="你是一个聊天记录摘要助手。只输出摘要内容，不要添加任何前缀或解释。",
    )

    if hasattr(result, "content"):
        content = result.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, TextContent):
            return content.text.strip()
        if isinstance(content, list):
            parts = []
            for c in content:
                if hasattr(c, "text"):
                    parts.append(c.text)
            return " ".join(parts).strip()
    return str(result).strip()


def _rule_based_compress(messages: list) -> str:
    """Fallback: rule-based compression when LLM is unavailable."""
    lines = []
    for m in messages:
        content = m.content[:80] + "..." if len(m.content) > 80 else m.content
        lines.append(f"{m.sender_name}: {content}")
    summary_block = " | ".join(lines)
    ts_range = f"[{messages[0].timestamp} ~ {messages[-1].timestamp}]"
    return f"{ts_range} {summary_block}"
