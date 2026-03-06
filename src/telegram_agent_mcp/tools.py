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

logger = logging.getLogger(__name__)

# Rate limiter state: chat_id -> last_send_timestamp
_last_send: dict[str, float] = {}
RATE_LIMIT_SECONDS = 3.0
CST = timezone(timedelta(hours=8))

# ── Duplicate send detection ────────────────────────────
_DEDUP_WINDOW_SECONDS = 60.0
_sent_history: dict[str, deque[tuple[str, float]]] = {}


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


def register_tools(
    mcp: Any, config: Config, bot: TelegramClient, ctx: ContextManager
) -> None:
    """Register all MCP tools on the FastMCP server instance."""

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

        # Collect chat IDs to resolve: from whitelist config or from active buffers
        chat_ids_to_resolve: set[str] = set()
        if config.chat_ids:
            chat_ids_to_resolve.update(config.chat_ids)
        # Also include chats discovered at runtime (from buffer)
        chat_ids_to_resolve.update(ctx.buffer_stats.get("active_chat_ids", []))

        monitored_chats: list[dict] = []
        for cid in chat_ids_to_resolve:
            try:
                chat_info = await bot.get_chat(cid)
                monitored_chats.append({
                    "chat_id": cid,
                    "title": chat_info.get("title", chat_info.get("first_name", "")),
                    "type": chat_info.get("type", ""),
                })
            except Exception:
                monitored_chats.append({"chat_id": cid, "title": "", "type": "unknown"})

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
    async def get_recent_context(
        chat_id: str,
        limit: int = 200,
    ) -> dict:
        """Get recent message context for a monitored Telegram chat.

        Returns all buffered messages (real-time only, no history backfill).
        Use compress_context to manually compress when needed.

        Args:
            chat_id: Telegram chat ID (group, supergroup, or private chat).
            limit: Number of recent messages to return (default 200).
        """
        if not config.is_chat_monitored(chat_id):
            return {"error": f"Chat {chat_id} is not monitored"}

        limit = max(1, limit)
        result = ctx.get_context(chat_id, limit)

        try:
            chat_info = await bot.get_chat(chat_id)
            result["chat_title"] = chat_info.get("title", chat_info.get("first_name", ""))
            result["chat_type"] = chat_info.get("type", "")
        except Exception:
            result["chat_title"] = ""
            result["chat_type"] = ""

        return result

    @mcp.tool()
    async def batch_get_recent_context(
        chat_ids: list[str],
        limit: int = 50,
    ) -> dict:
        """Batch query recent message context for multiple Telegram chats.

        Args:
            chat_ids: List of chat ID strings.
            limit: Number of recent messages per chat (default 50).
        """
        limit = max(1, min(limit, 200))

        results: list[dict] = []
        for cid in chat_ids:
            if not config.is_chat_monitored(cid):
                results.append({"chat_id": cid, "error": f"Chat {cid} is not monitored"})
                continue

            result = ctx.get_context(cid, limit)

            try:
                chat_info = await bot.get_chat(cid)
                result["chat_title"] = chat_info.get("title", chat_info.get("first_name", ""))
                result["chat_type"] = chat_info.get("type", "")
            except Exception:
                result["chat_title"] = ""
                result["chat_type"] = ""

            results.append(result)

        return {"results": results, "count": len(results)}

    @mcp.tool()
    async def send_message(
        chat_id: str,
        content: str,
        reply_to: int | None = None,
        split_content: bool = True,
        num_chunks: int | None = None,
    ) -> dict:
        """Send a message to a monitored Telegram chat.

        Args:
            chat_id: Telegram chat ID.
            content: Text message content.
            reply_to: Optional message ID to reply to.
            split_content: Whether to split long messages into multiple chunks
                with typing delay (default True). Set to False to send as a
                single message without splitting.
            num_chunks: If set, split the message into exactly this many chunks
                using natural punctuation boundaries. Overrides split_content.
        """
        if not config.is_chat_monitored(chat_id):
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

        # Split into chunks
        stripped = content.strip()
        if num_chunks is not None and num_chunks >= 2 and stripped:
            fine_chunks = _chunk_message(content)
            if len(fine_chunks) <= num_chunks:
                chunks = fine_chunks
            else:
                chunks = []
                per_group = len(fine_chunks) / num_chunks
                for i in range(num_chunks):
                    start = round(i * per_group)
                    end = round((i + 1) * per_group)
                    chunks.append("\n".join(fine_chunks[start:end]))
        elif split_content and len(stripped) > 100:
            chunks = _chunk_message(content)
        else:
            if len(stripped) > TELEGRAM_MSG_LIMIT:
                chunks = _chunk_message(stripped, max_chars=TELEGRAM_MSG_LIMIT)
            else:
                chunks = [stripped] if stripped else []
        if not chunks:
            return {"success": False, "error": "Empty message content"}

        sent_ids: list[int] = []
        first_reply_to = reply_to
        t0 = time.time()

        try:
            for i, chunk_text in enumerate(chunks):
                chunk_text = chunk_text.rstrip("。.")
                if not chunk_text:
                    continue

                # Send typing indicator
                try:
                    await bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass

                rto = first_reply_to if i == 0 else None
                result = await bot.send_message(chat_id, chunk_text, reply_to_message_id=rto)

                msg_id = result.get("message_id", 0)
                sent_ids.append(msg_id)

                bot_msg = Message(
                    sender_id=config.bot_id,
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
            "chat_id": chat_id,
            "timestamp": datetime.now(CST).isoformat(),
            "recent_messages": recent_lines,
        }

    @mcp.tool()
    async def compress_context(
        chat_id: str,
        ctx_mcp: Context,
    ) -> dict:
        """Compress all buffered messages for a chat into a summary.

        This replaces raw messages with a compressed summary, freeing up the buffer.

        Args:
            chat_id: Telegram chat ID.
        """
        if not config.is_chat_monitored(chat_id):
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
