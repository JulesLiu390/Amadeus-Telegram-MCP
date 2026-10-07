"""Telegram Bot API async client."""

import base64
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class TelegramAPIError(Exception):
    """Raised when Telegram Bot API returns ok=false."""

    def __init__(self, method: str, error_code: int, description: str):
        self.method = method
        self.error_code = error_code
        super().__init__(f"Telegram {method} failed ({error_code}): {description}")


def _reply_parameters(reply_to_message_id: int | None) -> dict | None:
    """引用回复的参数。被引用的那条已经被删掉时照样发，只是不挂引用。

    老写法 reply_to_message_id 遇到被删的消息会让整条发送失败
    （"message to be replied not found"）——群友发完就撤回、或者管理员删了，
    bot 那条回复就发不出去了。
    """
    if reply_to_message_id is None:
        return None
    return {"message_id": int(reply_to_message_id), "allow_sending_without_reply": True}


class TelegramClient:
    """Async client for Telegram Bot API."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, **params: Any) -> Any:
        """Call a Telegram Bot API method and return the result field."""
        session = await self._ensure_session()

        url = f"{self.base_url}/{method}"
        payload = {k: v for k, v in params.items() if v is not None}

        logger.debug("Telegram call: %s %s", method, payload)

        async with session.post(url, json=payload) as resp:
            result = await resp.json()

        if not result.get("ok", False):
            raise TelegramAPIError(
                method,
                result.get("error_code", -1),
                result.get("description", "Unknown error"),
            )

        return result.get("result")

    # ── Query APIs ──────────────────────────────────────────

    async def get_me(self) -> dict:
        """Get bot info. Returns {id, is_bot, first_name, username, ...}."""
        return await self._call("getMe")

    async def get_chat(self, chat_id: str) -> dict:
        """Get info about a chat (group, supergroup, channel, or private)."""
        return await self._call("getChat", chat_id=chat_id)

    async def get_chat_member_count(self, chat_id: str) -> int:
        """Number of members in a group / supergroup / channel."""
        return await self._call("getChatMemberCount", chat_id=chat_id)

    async def get_updates(
        self, offset: int | None = None, timeout: int = 30, allowed_updates: list[str] | None = None,
    ) -> list[dict]:
        """Long-poll for new updates. Returns list of Update objects."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        return await self._call("getUpdates", **params)

    # ── Send APIs ───────────────────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        """Send a text message. Returns the sent Message object.

        message_thread_id targets a forum topic; None means General / a normal chat.
        """
        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_parameters=_reply_parameters(reply_to_message_id),
            parse_mode=parse_mode,
            message_thread_id=message_thread_id,
        )

    async def send_chat_action(
        self, chat_id: str, action: str = "typing", message_thread_id: int | None = None
    ) -> bool:
        """Send a chat action (e.g. 'typing'). Returns True on success."""
        return await self._call(
            "sendChatAction", chat_id=chat_id, action=action, message_thread_id=message_thread_id
        )

    async def send_photo(
        self,
        chat_id: str,
        photo_base64: str,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        """Send a photo via multipart upload from base64 data. Returns the sent Message."""
        session = await self._ensure_session()
        url = f"{self.base_url}/sendPhoto"

        photo_bytes = base64.b64decode(photo_base64)
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("photo", photo_bytes, filename="image.jpg", content_type="image/jpeg")
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_parameters", json.dumps(_reply_parameters(reply_to_message_id)))
        if message_thread_id is not None:
            data.add_field("message_thread_id", str(message_thread_id))

        async with session.post(url, data=data) as resp:
            result = await resp.json()

        if not result.get("ok", False):
            raise TelegramAPIError(
                "sendPhoto",
                result.get("error_code", -1),
                result.get("description", "Unknown error"),
            )
        return result.get("result")

    async def send_voice(
        self,
        chat_id: str,
        voice_base64: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        """Send a voice note via multipart upload from base64 audio. Returns the sent Message.

        Telegram plays MP3 / M4A / OGG-OPUS as a voice note; the name and MIME type
        follow the actual bytes so OGG uploads aren't mislabelled as MP3.
        """
        session = await self._ensure_session()
        url = f"{self.base_url}/sendVoice"

        audio = base64.b64decode(voice_base64)
        if audio[:4] == b"OggS":
            filename, mime = "voice.ogg", "audio/ogg"
        elif audio[4:8] == b"ftyp":
            filename, mime = "voice.m4a", "audio/mp4"
        else:
            filename, mime = "voice.mp3", "audio/mpeg"
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("voice", audio, filename=filename, content_type=mime)
        if reply_to_message_id is not None:
            data.add_field("reply_parameters", json.dumps(_reply_parameters(reply_to_message_id)))
        if message_thread_id is not None:
            data.add_field("message_thread_id", str(message_thread_id))

        async with session.post(url, data=data) as resp:
            result = await resp.json()

        if not result.get("ok", False):
            raise TelegramAPIError(
                "sendVoice",
                result.get("error_code", -1),
                result.get("description", "Unknown error"),
            )
        return result.get("result")

    # ── File APIs ───────────────────────────────────────────

    async def get_file(self, file_id: str) -> dict:
        """Get file info by file_id. Returns {file_id, file_unique_id, file_path, ...}."""
        return await self._call("getFile", file_id=file_id)

    def get_file_url(self, file_path: str) -> str:
        """Build a download URL for a file_path returned by getFile."""
        # base_url is https://api.telegram.org/bot<TOKEN>
        # file URL is  https://api.telegram.org/file/bot<TOKEN>/<file_path>
        return self.base_url.replace("/bot", "/file/bot", 1) + "/" + file_path
