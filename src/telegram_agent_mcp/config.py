"""Configuration dataclass for telegram-agent-mcp."""

from dataclasses import dataclass


@dataclass
class Config:
    bot_token: str
    chat_ids: set[str] | None = None  # None = accept all chats
    user_ids: set[str] | None = None  # None = accept all users
    buffer_size: int = 100
    compress_every: int = 30
    log_level: str = "info"
    polling_timeout: int = 30  # long-polling timeout in seconds

    @property
    def api_base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def bot_id(self) -> str:
        """Extract numeric bot ID from token (the part before the colon)."""
        return self.bot_token.split(":")[0]

    def is_chat_monitored(self, chat_id: str) -> bool:
        """Check if a chat is in the monitor list. None means all."""
        if self.chat_ids is None:
            return True
        return chat_id in self.chat_ids

    def is_user_monitored(self, user_id: str) -> bool:
        """Check if a user is in the whitelist. None means all."""
        if self.user_ids is None:
            return True
        return user_id in self.user_ids
