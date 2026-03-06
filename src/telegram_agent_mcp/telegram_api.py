"""Telegram Bot API async client."""

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
    ) -> dict:
        """Send a text message. Returns the sent Message object."""
        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
        )

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> bool:
        """Send a chat action (e.g. 'typing'). Returns True on success."""
        return await self._call("sendChatAction", chat_id=chat_id, action=action)

    # ── File APIs ───────────────────────────────────────────

    async def get_file(self, file_id: str) -> dict:
        """Get file info by file_id. Returns {file_id, file_unique_id, file_path, ...}."""
        return await self._call("getFile", file_id=file_id)

    def get_file_url(self, file_path: str) -> str:
        """Build a download URL for a file_path returned by getFile."""
        # base_url is https://api.telegram.org/bot<TOKEN>
        # file URL is  https://api.telegram.org/file/bot<TOKEN>/<file_path>
        return self.base_url.replace("/bot", "/file/bot", 1) + "/" + file_path
