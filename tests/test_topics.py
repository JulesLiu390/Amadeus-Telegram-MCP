"""论坛话题：一个群里的每个话题都当成一个独立的群。"""

import pytest

import telegram_agent_mcp.tools as tools_mod
from telegram_agent_mcp.config import Config
from telegram_agent_mcp.context import ContextManager
from telegram_agent_mcp.tools import register_tools
from telegram_agent_mcp.topics import (
    learned_topic_name,
    make_target,
    normalize_target,
    split_target,
    topic_thread_id,
)

FORUM = -1001234567890
CHAT = {"id": FORUM, "title": "GTA+", "type": "supergroup", "is_forum": True}
USER = {"id": 42, "first_name": "oxa", "username": "oxa"}


def topic_msg(thread, text, message_id=100):
    """话题里的一条普通消息：Telegram 会把话题的创建消息挂在 reply_to_message 上。"""
    return {
        "message_id": message_id, "date": 1700000000, "chat": CHAT, "from": USER,
        "text": text, "is_topic_message": True, "message_thread_id": thread,
        "reply_to_message": {
            "message_id": thread, "chat": CHAT, "from": USER,
            "message_thread_id": thread, "is_topic_message": True,
            "forum_topic_created": {"name": "Contentious Topics", "icon_color": 1},
        },
    }


def general_msg(text):
    return {"message_id": 7, "date": 1700000000, "chat": CHAT, "from": USER, "text": text}


# ── 纯函数 ──────────────────────────────────────────────────

def test_targets_round_trip():
    assert split_target("-1001234567890:45") == ("-1001234567890", 45)
    assert split_target("@grp:45") == ("@grp", 45)
    assert make_target("-1001234567890", 45) == "-1001234567890:45"
    # General / 普通群 / 私聊：就是 chat_id 本身，老配置不受影响
    assert split_target("-1001234567890") == ("-1001234567890", None)
    assert make_target("-1001234567890", None) == "-1001234567890"


@pytest.mark.parametrize("link, expected", [
    # 用户直接粘了话题链接——线上就是这么填的，结果一直等不到消息
    ("https://t.me/c/3809923344/13288", "-1003809923344:13288"),
    ("t.me/c/3809923344/13288/99999", "-1003809923344:13288"),   # 话题里某条消息的链接
    ("https://t.me/c/3809923344", "-1003809923344"),
    ("https://t.me/c/3809923344/13288?single", "-1003809923344:13288"),
    ("https://t.me/GlitchTestGroup", "@GlitchTestGroup"),
    ("t.me/GlitchTestGroup/45", "@GlitchTestGroup:45"),
    # 已经是规范写法、或者认不出来的，原样返回
    ("-1003809923344:13288", "-1003809923344:13288"),
    ("@GlitchTestGroup", "@GlitchTestGroup"),
    ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"),     # 邀请链接拿不到 ID
    ("https://t.me/joinchat/AbCdEf", "https://t.me/joinchat/AbCdEf"),
])
def test_pasted_telegram_links_become_canonical_targets(link, expected):
    assert normalize_target(link) == expected


def test_split_target_accepts_links_directly():
    assert split_target("https://t.me/c/3809923344/13288") == ("-1003809923344", 13288)


@pytest.mark.parametrize("raw", ["abc:", "x:0", "x:-3", "x:ab", ":5"])
def test_malformed_suffixes_are_not_topics(raw):
    assert split_target(raw) == (raw, None)


def test_only_forum_topics_count_not_reply_threads():
    assert topic_thread_id(topic_msg(45, "hi")) == 45
    assert topic_thread_id(general_msg("hi")) is None
    # 普通超级群的回复串也带 message_thread_id，但不是话题
    assert topic_thread_id({"message_thread_id": 9}) is None


def test_topic_names_are_learned_from_service_messages():
    created = {"message_id": 45, "message_thread_id": 45, "is_topic_message": True,
               "forum_topic_created": {"name": "Announcements"}}
    edited = {"message_id": 50, "message_thread_id": 45, "is_topic_message": True,
              "forum_topic_edited": {"name": "Notices"}}
    assert learned_topic_name(created) == (45, "Announcements")
    assert learned_topic_name(edited) == (45, "Notices")
    assert learned_topic_name(topic_msg(45, "hi")) == (45, "Contentious Topics")
    assert learned_topic_name(general_msg("hi")) is None


# ── 收消息 ──────────────────────────────────────────────────

def _ctx(**cfg):
    return ContextManager(Config(bot_token="999:x", **cfg))


@pytest.mark.asyncio
async def test_each_topic_gets_its_own_buffer_and_general_keeps_the_chat_id():
    ctx = _ctx()
    await ctx._handle_message(topic_msg(45, "在话题里"))
    await ctx._handle_message(topic_msg(60, "另一个话题", message_id=101))
    await ctx._handle_message(general_msg("在 General"))

    assert sorted(ctx.buffer_stats["active_chat_ids"]) == sorted(
        [f"{FORUM}:45", f"{FORUM}:60", str(FORUM)])
    assert [m["content"] for m in ctx.get_context(f"{FORUM}:45")["messages"]] == ["在话题里"]
    assert ctx.topic_name(f"{FORUM}:45") == "Contentious Topics"


@pytest.mark.asyncio
async def test_the_topic_root_is_not_rendered_as_a_fake_reply():
    ctx = _ctx()
    await ctx._handle_message(topic_msg(45, "hello"))
    content = ctx.get_context(f"{FORUM}:45")["messages"][0]["content"]
    assert "回复了" not in content


@pytest.mark.asyncio
async def test_a_topic_created_message_records_the_name_without_buffering_anything():
    ctx = _ctx()
    await ctx._handle_message({"message_id": 70, "date": 1700000000, "chat": CHAT, "from": USER,
                               "message_thread_id": 70, "is_topic_message": True,
                               "forum_topic_created": {"name": "Urgent"}})
    assert ctx.topic_name(f"{FORUM}:70") == "Urgent"
    assert ctx.buffer_stats["active_chat_ids"] == []


@pytest.mark.asyncio
async def test_whitelisting_one_topic_ignores_the_rest_of_the_group():
    ctx = _ctx(chat_ids={f"{FORUM}:45"})
    await ctx._handle_message(topic_msg(45, "要"))
    await ctx._handle_message(topic_msg(60, "不要", message_id=101))
    await ctx._handle_message(general_msg("也不要"))
    assert ctx.buffer_stats["active_chat_ids"] == [f"{FORUM}:45"]


# ── 发消息 / 列表 ───────────────────────────────────────────

class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeBot:
    def __init__(self):
        self.calls = []

    async def get_chat(self, cid):
        return CHAT

    async def get_chat_member_count(self, cid):
        return 95

    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        return True

    async def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        self.calls.append(("send_message", chat_id, message_thread_id))
        return {"message_id": 500}

    async def send_photo(self, chat_id, photo, reply_to_message_id=None, message_thread_id=None):
        self.calls.append(("send_photo", chat_id, message_thread_id))
        return {"message_id": 501}

    async def send_voice(self, chat_id, voice, reply_to_message_id=None, message_thread_id=None):
        self.calls.append(("send_voice", chat_id, message_thread_id))
        return {"message_id": 502}


@pytest.fixture
def setup(monkeypatch):
    async def no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(tools_mod.asyncio, "sleep", no_sleep)
    tools_mod._last_send.clear()
    tools_mod._sent_history.clear()
    bot, mcp = FakeBot(), FakeMcp()
    ctx = ContextManager(Config(bot_token="999:x"), bot)
    register_tools(mcp, ctx.config, bot, ctx)
    return bot, ctx, mcp.tools


@pytest.mark.asyncio
async def test_sending_to_a_topic_passes_the_thread_and_keeps_the_reply_in_that_topic(setup):
    bot, ctx, tools = setup
    r = await tools["send_message"](target=f"{FORUM}:45", content="收到")
    assert r["success"] is True
    assert bot.calls == [("send_message", str(FORUM), 45)]
    # 自己发的那条进的是话题的 buffer，不是 General
    assert ctx.get_context(f"{FORUM}:45")["message_count"] == 1
    assert ctx.get_context(str(FORUM))["message_count"] == 0


@pytest.mark.asyncio
async def test_sending_to_general_omits_the_thread(setup):
    bot, _ctx, tools = setup
    await tools["send_message"](target=str(FORUM), content="大家好")
    await tools["send_image"](target=f"{FORUM}:45", image="aGk=")
    assert bot.calls == [("send_message", str(FORUM), None), ("send_photo", str(FORUM), 45)]


@pytest.mark.asyncio
async def test_the_group_list_shows_each_topic_as_its_own_group(setup):
    _bot, ctx, tools = setup
    await ctx._handle_message(topic_msg(45, "a"))
    await ctx._handle_message(general_msg("b"))
    ctx._topic_names.pop(f"{FORUM}:60", None)
    await ctx._handle_message({**topic_msg(60, "c", message_id=102), "reply_to_message": None})

    groups = (await tools["get_group_list"]())["groups"]
    assert groups == [
        {"group_id": str(FORUM), "group_name": "GTA+ / General", "member_count": 95},
        {"group_id": f"{FORUM}:45", "group_name": "GTA+ / Contentious Topics", "member_count": 95},
        # 名字还没见过的话题，用编号顶上
        {"group_id": f"{FORUM}:60", "group_name": "GTA+ / 话题 #60", "member_count": 95},
    ]


@pytest.mark.asyncio
async def test_voice_goes_to_the_topic_and_is_remembered_as_voice(setup):
    """PetGPT 的 voice_send 调的是 ${server}__send_voice；以前 telegram-mcp 没有，
    ElevenLabs 合成完才发现发不出去。"""
    bot, ctx, tools = setup
    r = await tools["send_voice"](target=f"{FORUM}:45", audio="SUQz")
    assert r["success"] is True
    assert bot.calls == [("send_voice", str(FORUM), 45)]
    assert ctx.get_context(f"{FORUM}:45")["messages"][-1]["content"] == "[语音]"
