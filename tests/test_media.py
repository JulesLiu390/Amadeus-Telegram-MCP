"""图片、贴纸、GIF、以文件形式发的图片——都要让模型看得到。"""

import pytest

from telegram_agent_mcp.config import Config
from telegram_agent_mcp.context import ContextManager

CHAT = {"id": -100123, "title": "G", "type": "supergroup"}
USER = {"id": 42, "first_name": "Jules"}


class FakeBot:
    def __init__(self, fail=()):
        self.fail = set(fail)

    async def get_file(self, file_id):
        if file_id in self.fail:
            raise RuntimeError("file is too big")
        return {"file_path": f"files/{file_id}"}

    def get_file_url(self, path):
        return f"https://api.telegram.org/file/bot<t>/{path}"


async def parse(extra, bot=None):
    ctx = ContextManager(Config(bot_token="999:x"), bot or FakeBot())
    msg = {"message_id": 1, "date": 1700000000, "chat": CHAT, "from": USER, **extra}
    content, _at, urls = await ctx._parse_message(msg)
    return content, [u.rsplit("/", 1)[-1] for u in urls]


@pytest.mark.asyncio
async def test_photo_uses_the_largest_size():
    content, urls = await parse({"caption": "能看见这个图吗？", "photo": [
        {"file_id": "small", "file_size": 1000}, {"file_id": "big", "file_size": 30000}]})
    assert urls == ["big"]
    assert "能看见这个图吗" in content


@pytest.mark.asyncio
async def test_static_sticker_is_shown_as_the_sticker_itself():
    content, urls = await parse({"sticker": {"file_id": "stk", "emoji": "😵‍💫",
                                             "is_animated": False, "is_video": False,
                                             "thumbnail": {"file_id": "stk_thumb"}}})
    assert urls == ["stk"]
    assert content == "[贴纸😵‍💫]"


@pytest.mark.parametrize("kind", ["is_animated", "is_video"])
@pytest.mark.asyncio
async def test_animated_and_video_stickers_fall_back_to_the_thumbnail(kind):
    _content, urls = await parse({"sticker": {"file_id": "tgs", "emoji": "😂", kind: True,
                                              "thumbnail": {"file_id": "tgs_thumb"}}})
    assert urls == ["tgs_thumb"]


@pytest.mark.asyncio
async def test_old_api_thumb_field_also_works():
    _c, urls = await parse({"sticker": {"file_id": "tgs", "is_animated": True, "thumb": {"file_id": "old_thumb"}}})
    assert urls == ["old_thumb"]


@pytest.mark.asyncio
async def test_gif_shows_its_thumbnail():
    content, urls = await parse({"animation": {"file_id": "mp4", "thumbnail": {"file_id": "gif_thumb"}},
                                 "document": {"file_id": "mp4", "mime_type": "video/mp4"}})
    assert urls == ["gif_thumb"]
    assert content == "[GIF]"


@pytest.mark.asyncio
async def test_an_image_sent_as_a_file_is_still_an_image():
    content, urls = await parse({"document": {"file_id": "png", "file_name": "shot.png", "mime_type": "image/png"}})
    assert urls == ["png"]
    assert content == "[图片文件: shot.png]"


@pytest.mark.asyncio
async def test_heic_files_use_the_thumbnail():
    _c, urls = await parse({"document": {"file_id": "heic", "file_name": "a.heic", "mime_type": "image/heic",
                                         "thumbnail": {"file_id": "heic_thumb"}}})
    assert urls == ["heic_thumb"]


@pytest.mark.asyncio
async def test_non_image_files_stay_files():
    content, urls = await parse({"document": {"file_id": "pdf", "file_name": "a.pdf", "mime_type": "application/pdf"}})
    assert urls == [] and content == "[文件: a.pdf]"


@pytest.mark.asyncio
async def test_a_get_file_failure_degrades_to_text_instead_of_crashing():
    content, urls = await parse({"document": {"file_id": "huge", "file_name": "big.png", "mime_type": "image/png"}},
                                bot=FakeBot(fail={"huge"}))
    assert urls == [] and content == "[文件: big.png]"
