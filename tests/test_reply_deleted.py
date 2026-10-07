"""引用的那条被删了，回复照样要发出去。"""

import json

import pytest

from telegram_agent_mcp.telegram_api import TelegramClient, _reply_parameters


def test_reply_parameters_allow_sending_when_the_target_is_gone():
    assert _reply_parameters(17193) == {"message_id": 17193, "allow_sending_without_reply": True}
    assert _reply_parameters(None) is None


@pytest.mark.asyncio
async def test_send_message_uses_reply_parameters_not_the_old_field():
    client = TelegramClient("999:x")
    seen = {}

    async def fake_call(method, **params):
        seen.update(method=method, **params)
        return {"message_id": 1}

    client._call = fake_call
    await client.send_message("-100123", "hi", reply_to_message_id=17193, message_thread_id=45)
    assert seen["reply_parameters"] == {"message_id": 17193, "allow_sending_without_reply": True}
    assert seen.get("reply_to_message_id") is None
    assert seen["message_thread_id"] == 45


class _Resp:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return {"ok": True, "result": {"message_id": 2}}


class _Session:
    def __init__(self):
        self.form = None

    def post(self, url, data=None, json=None):
        self.form = data
        return _Resp()


def _fields(form):
    return {opts["name"]: value for opts, _headers, value in form._fields}


@pytest.mark.parametrize("method, arg", [("send_photo", "aGk="), ("send_voice", "SUQz")])
@pytest.mark.asyncio
async def test_multipart_sends_also_survive_a_deleted_reply_target(method, arg):
    client = TelegramClient("999:x")
    session = _Session()

    async def ensure():
        return session

    client._ensure_session = ensure
    await getattr(client, method)("-100123", arg, reply_to_message_id=17193)
    fields = _fields(session.form)
    assert "reply_to_message_id" not in fields
    assert json.loads(fields["reply_parameters"]) == {"message_id": 17193, "allow_sending_without_reply": True}
