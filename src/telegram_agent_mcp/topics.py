"""Forum topics (话题): treat every topic in a forum supergroup as its own target.

A forum supergroup has one chat_id but many topics; each message carries a
``message_thread_id`` saying which topic it belongs to. To the agent each topic
is an independent conversation, so targets are written as::

    -1001234567890        a normal group, or the General topic of a forum
    -1001234567890:45     topic 45 of that forum

The ``chat:thread`` form matches Telegram's own topic links: ``t.me/c/1234567890/45``
is topic 45 of chat ``-1001234567890``.

General stays the bare chat_id on purpose: messages there carry no
``message_thread_id``, sending to it must omit the parameter, and every target
configured before topics were supported keeps meaning what it meant.

The Bot API cannot list a forum's topics, so names are learned from the service
messages that create or rename a topic, which also arrive as ``reply_to_message``
on ordinary messages posted in that topic.
"""

from __future__ import annotations

import re

TOPIC_SEP = ":"
GENERAL_TOPIC_NAME = "General"


# t.me/c/<内部id>[/<话题>[/<消息>]] —— 私有群（含论坛）的链接
_PRIVATE_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/c/(\d+)(?:/(\d+))?(?:/\d+)?/?(?:[?#].*)?$", re.I)
# t.me/<用户名>[/<话题>[/<消息>]] —— 公开群的链接；+xxx / joinchat 这类邀请链接不在此列
_PUBLIC_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,})(?:/(\d+))?(?:/\d+)?/?(?:[?#].*)?$", re.I)


def normalize_target(target: str) -> str:
    """把用户直接粘贴的 Telegram 链接换成规范的 target，其它写法原样返回。

    ::

        https://t.me/c/3809923344/13288   → -1003809923344:13288
        https://t.me/c/3809923344         → -1003809923344
        t.me/somegroup/45                 → @somegroup:45

    私有群链接里的数字是去掉 -100 前缀的内部 ID。注意 ``t.me/c/<id>/<n>`` 在
    论坛群里是话题链接，在普通群里却是某条消息的链接——两者长得一样，这里一律
    当话题处理，因为只有前者能被当作监听目标。
    """
    raw = str(target).strip()
    m = _PRIVATE_LINK.match(raw)
    if m:
        return make_target(f"-100{m.group(1)}", int(m.group(2)) if m.group(2) else None)
    m = _PUBLIC_LINK.match(raw)
    if m and m.group(1).lower() not in {"c", "joinchat", "addstickers", "share", "proxy"}:
        return make_target(f"@{m.group(1)}", int(m.group(2)) if m.group(2) else None)
    return raw


def split_target(target: str) -> tuple[str, int | None]:
    """``"-100123:45"`` → ``("-100123", 45)``; anything without a valid thread → ``(target, None)``.

    Telegram links are accepted too and normalized first, see :func:`normalize_target`.
    """
    target = normalize_target(target)
    chat, sep, thread = target.rpartition(TOPIC_SEP)
    if sep and chat and thread.isdigit() and int(thread) > 0:
        return chat, int(thread)
    return target, None


def make_target(chat_id: str, thread_id: int | None) -> str:
    """Inverse of :func:`split_target`. No thread (or General) → the bare chat_id."""
    return f"{chat_id}{TOPIC_SEP}{thread_id}" if thread_id else str(chat_id)


def topic_thread_id(message: dict) -> int | None:
    """The forum topic a message belongs to, or None for General / non-forum chats.

    Only ``is_topic_message`` counts: ordinary supergroups also set
    ``message_thread_id`` on reply threads, and those are not topics.
    """
    if not message.get("is_topic_message"):
        return None
    thread = message.get("message_thread_id")
    return int(thread) if thread else None


def learned_topic_name(message: dict) -> tuple[int, str] | None:
    """``(thread_id, name)`` if this message reveals a topic's name, else None."""
    for key in ("forum_topic_created", "forum_topic_edited"):
        info = message.get(key)
        if info and info.get("name"):
            thread = message.get("message_thread_id") or message.get("message_id")
            if thread:
                return int(thread), info["name"]
    # A message posted in a topic (not replying to anything) carries the
    # topic's creation message as reply_to_message.
    parent = message.get("reply_to_message") or {}
    created = parent.get("forum_topic_created")
    thread = message.get("message_thread_id")
    if message.get("is_topic_message") and created and created.get("name") and thread:
        return int(thread), created["name"]
    return None
