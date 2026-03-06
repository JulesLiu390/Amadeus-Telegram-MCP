"""Unit tests for config.py."""

from telegram_agent_mcp.config import Config


class TestConfig:
    def test_api_base_url(self, config):
        assert config.api_base_url == "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz"

    def test_bot_id(self, config):
        assert config.bot_id == "123456789"

    def test_is_chat_monitored_whitelist(self, config):
        assert config.is_chat_monitored("-100111") is True
        assert config.is_chat_monitored("-100222") is True
        assert config.is_chat_monitored("-100999") is False

    def test_is_chat_monitored_all(self, config_all):
        assert config_all.is_chat_monitored("-100111") is True
        assert config_all.is_chat_monitored("anything") is True

    def test_is_user_monitored_whitelist(self, config):
        assert config.is_user_monitored("999") is True
        assert config.is_user_monitored("888") is False

    def test_is_user_monitored_all(self, config_all):
        assert config_all.is_user_monitored("anyone") is True

    def test_defaults(self):
        c = Config(bot_token="111:AAA")
        assert c.buffer_size == 100
        assert c.compress_every == 30
        assert c.polling_timeout == 30
        assert c.log_level == "info"
        assert c.chat_ids is None
        assert c.user_ids is None
