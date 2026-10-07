"""get_group_list / get_friend_list —— PetGPT 的「Fetch from MCP」按钮靠这两个工具。"""

import pytest

from telegram_agent_mcp.config import Config
from telegram_agent_mcp.tools import _split_known_chats, register_tools


# ── _split_known_chats ─────────────────────────────────────

def test_groups_and_private_chats_use_the_qq_mcp_shape():
    groups, friends = _split_known_chats([
        {"chat_id": "-100", "title": "Amadeus", "type": "supergroup", "member_count": 7},
        {"chat_id": "-200", "title": "Old", "type": "group"},
        {"chat_id": "42", "title": "Daisy", "type": "private"},
    ])
    assert groups == [
        {"group_id": "-100", "group_name": "Amadeus", "member_count": 7},
        {"group_id": "-200", "group_name": "Old", "member_count": 0},
    ]
    assert friends == [{"user_id": "42", "nickname": "Daisy"}]


def test_channels_and_unresolvable_chats_are_left_out():
    groups, friends = _split_known_chats([
        {"chat_id": "-300", "title": "News", "type": "channel"},
        {"chat_id": "-400", "title": "", "type": "unknown"},
    ])
    assert groups == [] and friends == []


# ── 通过 register_tools 端到端 ──────────────────────────────

class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeBot:
    CHATS = {
        "-100": {"id": -100, "title": "Amadeus", "type": "supergroup"},
        "42": {"id": 42, "first_name": "Daisy", "type": "private"},
        "-300": {"id": -300, "title": "News", "type": "channel"},
    }

    async def get_chat(self, cid):
        if cid not in self.CHATS:
            raise RuntimeError("chat not found")
        return self.CHATS[cid]

    async def get_chat_member_count(self, cid):
        return 7


class FakeCtx:
    def __init__(self, active):
        self.buffer_stats = {"active_chat_ids": active}


def _tools(config, active):
    mcp = FakeMcp()
    register_tools(mcp, config, FakeBot(), FakeCtx(active))
    return mcp.tools


@pytest.mark.asyncio
async def test_lists_cover_the_whitelist_and_chats_seen_at_runtime():
    config = Config(bot_token="x", chat_ids={"@GlitchTestGroup"})
    config._chat_aliases["@GlitchTestGroup"] = "-100"
    # buffer 里也有 -100：白名单的 @别名和数字 ID 是同一个群，不能列两遍
    tools = _tools(config, ["-100", "42", "-300", "-999"])

    groups = (await tools["get_group_list"]())["groups"]
    friends = (await tools["get_friend_list"]())["friends"]

    assert groups == [{"group_id": "-100", "group_name": "Amadeus", "member_count": 7}]
    assert friends == [{"user_id": "42", "nickname": "Daisy"}]


@pytest.mark.asyncio
async def test_nothing_known_yet_gives_empty_lists_not_an_error():
    tools = _tools(Config(bot_token="x"), [])
    assert await tools["get_group_list"]() == {"groups": []}
    assert await tools["get_friend_list"]() == {"friends": []}
