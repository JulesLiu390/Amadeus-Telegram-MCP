"""Shared fixtures for telegram-agent-mcp tests."""

import pytest

from telegram_agent_mcp.config import Config


@pytest.fixture
def config():
    """Default test config."""
    return Config(
        bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        chat_ids={"-100111", "-100222"},
        user_ids={"999"},
        buffer_size=50,
        compress_every=10,
    )


@pytest.fixture
def config_all():
    """Config with no whitelist (monitor all)."""
    return Config(bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
