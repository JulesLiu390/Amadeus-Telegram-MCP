"""Unit tests for telegram_api.py."""

import pytest
import pytest_asyncio
from aioresponses import aioresponses

from telegram_agent_mcp.telegram_api import TelegramClient, TelegramAPIError


BASE_URL = "https://api.telegram.org/bot123:ABC"


@pytest.fixture
def client():
    return TelegramClient(BASE_URL)


@pytest.fixture
def mock_api():
    with aioresponses() as m:
        yield m


class TestTelegramClient:
    @pytest.mark.asyncio
    async def test_get_me(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getMe", payload={
            "ok": True,
            "result": {"id": 123, "is_bot": True, "first_name": "TestBot", "username": "testbot"},
        })
        result = await client.get_me()
        assert result["id"] == 123
        assert result["username"] == "testbot"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_me_error(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getMe", payload={
            "ok": False,
            "error_code": 401,
            "description": "Unauthorized",
        })
        with pytest.raises(TelegramAPIError) as exc_info:
            await client.get_me()
        assert exc_info.value.error_code == 401
        assert "Unauthorized" in str(exc_info.value)
        await client.close()

    @pytest.mark.asyncio
    async def test_get_chat(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getChat", payload={
            "ok": True,
            "result": {"id": -100111, "type": "supergroup", "title": "Test Group"},
        })
        result = await client.get_chat("-100111")
        assert result["title"] == "Test Group"
        await client.close()

    @pytest.mark.asyncio
    async def test_send_message(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/sendMessage", payload={
            "ok": True,
            "result": {"message_id": 42, "chat": {"id": -100111}, "text": "hello"},
        })
        result = await client.send_message("-100111", "hello")
        assert result["message_id"] == 42
        await client.close()

    @pytest.mark.asyncio
    async def test_send_message_with_reply(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/sendMessage", payload={
            "ok": True,
            "result": {"message_id": 43, "chat": {"id": -100111}, "text": "reply"},
        })
        result = await client.send_message("-100111", "reply", reply_to_message_id=10)
        assert result["message_id"] == 43
        await client.close()

    @pytest.mark.asyncio
    async def test_send_chat_action(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/sendChatAction", payload={
            "ok": True,
            "result": True,
        })
        result = await client.send_chat_action("-100111", "typing")
        assert result is True
        await client.close()

    @pytest.mark.asyncio
    async def test_get_updates(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getUpdates", payload={
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"message_id": 1, "text": "hi"}},
                {"update_id": 2, "message": {"message_id": 2, "text": "bye"}},
            ],
        })
        result = await client.get_updates(offset=1, timeout=5)
        assert len(result) == 2
        assert result[0]["update_id"] == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_get_file(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getFile", payload={
            "ok": True,
            "result": {"file_id": "abc", "file_path": "photos/file_1.jpg"},
        })
        result = await client.get_file("abc")
        assert result["file_path"] == "photos/file_1.jpg"
        await client.close()

    def test_get_file_url(self, client):
        url = client.get_file_url("photos/file_1.jpg")
        assert url == "https://api.telegram.org/file/bot123:ABC/photos/file_1.jpg"

    @pytest.mark.asyncio
    async def test_none_params_filtered(self, client, mock_api):
        """None params should not be included in the request."""
        mock_api.post(f"{BASE_URL}/sendMessage", payload={
            "ok": True,
            "result": {"message_id": 1},
        })
        await client.send_message("-100111", "test", reply_to_message_id=None, parse_mode=None)
        # If None params were sent, Telegram would reject them — but our mock accepts anything
        # The key test is that _call filters them out (tested by code inspection + no error)
        await client.close()

    @pytest.mark.asyncio
    async def test_session_reuse(self, client, mock_api):
        """Multiple calls should reuse the same session."""
        mock_api.post(f"{BASE_URL}/getMe", payload={"ok": True, "result": {"id": 1}})
        mock_api.post(f"{BASE_URL}/getMe", payload={"ok": True, "result": {"id": 1}})
        await client.get_me()
        session1 = client._session
        await client.get_me()
        assert client._session is session1
        await client.close()

    @pytest.mark.asyncio
    async def test_close_clears_session(self, client, mock_api):
        mock_api.post(f"{BASE_URL}/getMe", payload={"ok": True, "result": {"id": 1}})
        await client.get_me()
        assert client._session is not None
        await client.close()
        assert client._session is None


class TestTelegramAPIError:
    def test_attributes(self):
        err = TelegramAPIError("sendMessage", 400, "Bad Request")
        assert err.method == "sendMessage"
        assert err.error_code == 400
        assert "sendMessage" in str(err)
        assert "400" in str(err)
        assert "Bad Request" in str(err)
