"""Configuration dataclass for telegram-agent-mcp."""

from dataclasses import dataclass, field


@dataclass
class Config:
    bot_token: str
    chat_ids: set[str] | None = None  # None = accept all chats
    user_ids: set[str] | None = None  # None = accept all users
    buffer_size: int = 100
    compress_every: int = 30
    log_level: str = "info"
    polling_timeout: int = 30  # long-polling timeout in seconds
    _chat_aliases: dict[str, str] = field(default_factory=dict)  # @username -> numeric_id
    _user_aliases: dict[str, str] = field(default_factory=dict)  # @username -> numeric_user_id

    @property
    def api_base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def bot_id(self) -> str:
        """Extract numeric bot ID from token (the part before the colon)."""
        return self.bot_token.split(":")[0]

    def resolve_chat_id(self, raw: str) -> str:
        """Resolve @username to numeric ID if aliased, otherwise return raw."""
        return self._chat_aliases.get(raw, raw)

    def resolve_user_id(self, raw: str) -> str:
        """Resolve @username to numeric user ID if aliased, otherwise return raw."""
        return self._user_aliases.get(raw, raw)

    def is_chat_monitored(self, chat_id: str) -> bool:
        """Check if a chat is in the monitor list. None means all."""
        if self.chat_ids is None:
            return True
        if chat_id in self.chat_ids:
            return True
        # Check if numeric chat_id maps to an aliased @username
        for alias, resolved in self._chat_aliases.items():
            if resolved == chat_id and alias in self.chat_ids:
                return True
        return False

    def is_user_monitored(self, user_id: str) -> bool:
        """Check if a user is in the whitelist. None means all."""
        if self.user_ids is None:
            return True
        if user_id in self.user_ids:
            return True
        # Check if numeric user_id maps to an aliased @username
        for alias, resolved in self._user_aliases.items():
            if resolved == user_id and alias in self.user_ids:
                return True
        return False
