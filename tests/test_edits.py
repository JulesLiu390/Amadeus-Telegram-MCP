"""编辑过的消息：同一个 message_id，内容要换成新的，并且标出是编辑。"""

import pytest

from telegram_agent_mcp.config import Config
from telegram_agent_mcp.context import ContextManager

CHAT = {"id": -100123, "title": "G", "type": "supergroup"}
USER = {"id": 42, "first_name": "Jules"}


def msg(text, mid=7, **extra):
    return {"message_id": mid, "date": 1700000000, "chat": CHAT, "from": USER, "text": text, **extra}


@pytest.mark.asyncio
async def test_an_edit_replaces_the_original_instead_of_being_dropped():
    ctx = ContextManager(Config(bot_token="999:x"))
    await ctx._handle_message(msg("明天几点开会"))
    await ctx._handle_message(msg("明天十点开会", edit_date=1700000060), edited=True)

    messages = ctx.get_context("-100123")["messages"]
    assert [m["content"] for m in messages] == ["明天十点开会"], "以前按 ID 去重，新内容被当成重复丢掉"
    assert messages[0]["edited"] is True
    assert messages[0]["edit_date"].startswith("2023-11-15T06:14:20")


@pytest.mark.asyncio
async def test_an_edit_of_a_message_no_longer_buffered_is_just_added():
    ctx = ContextManager(Config(bot_token="999:x"))
    await ctx._handle_message(msg("改过的旧消息", mid=3, edit_date=1700000060), edited=True)
    assert [m["content"] for m in ctx.get_context("-100123")["messages"]] == ["改过的旧消息"]


@pytest.mark.asyncio
async def test_plain_messages_carry_no_edit_fields():
    ctx = ContextManager(Config(bot_token="999:x"))
    await ctx._handle_message(msg("hi"))
    m = ctx.get_context("-100123")["messages"][0]
    assert "edited" not in m and "edit_date" not in m
