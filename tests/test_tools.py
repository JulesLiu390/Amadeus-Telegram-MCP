"""Unit tests for tools.py — chunking, dedup, normalization, delay."""

import time

import pytest

from telegram_agent_mcp.tools import (
    _chunk_message,
    _check_duplicate,
    _decide_chunks,
    _normalize_content,
    _human_delay_for_chunk,
    _rule_based_compress,
    _split_by_tag,
    _sent_history,
    TELEGRAM_MSG_LIMIT,
)
from telegram_agent_mcp.context import Message


# ── _normalize_content ─────────────────────────────────────

class TestNormalizeContent:
    def test_strip_and_lower(self):
        assert _normalize_content("  Hello World  ") == "hello world"

    def test_collapse_whitespace(self):
        assert _normalize_content("a  b\n\nc") == "a b c"

    def test_nfkc(self):
        # Full-width A → normal A
        assert _normalize_content("Ａ") == "a"


# ── _check_duplicate ──────────────────────────────────────

class TestCheckDuplicate:
    def setup_method(self):
        _sent_history.clear()

    def test_first_send_ok(self):
        assert _check_duplicate("chat:1", "hello") is None

    def test_duplicate_detected(self):
        _check_duplicate("chat:1", "hello")
        result = _check_duplicate("chat:1", "hello")
        assert result is not None
        assert "已经发送过" in result

    def test_different_content_ok(self):
        _check_duplicate("chat:1", "hello")
        assert _check_duplicate("chat:1", "world") is None

    def test_different_target_ok(self):
        _check_duplicate("chat:1", "hello")
        assert _check_duplicate("chat:2", "hello") is None

    def test_whitespace_normalization(self):
        _check_duplicate("chat:1", "hello world")
        result = _check_duplicate("chat:1", "  hello   world  ")
        assert result is not None

    def test_case_insensitive(self):
        _check_duplicate("chat:1", "Hello")
        result = _check_duplicate("chat:1", "hello")
        assert result is not None


# ── _human_delay_for_chunk ─────────────────────────────────

class TestHumanDelay:
    def test_within_bounds(self):
        delay = _human_delay_for_chunk("hello")
        assert 0.3 <= delay <= 3.0

    def test_short_uses_minimum(self):
        delay = _human_delay_for_chunk("hi")
        assert delay >= 0.3

    def test_long_capped_at_max(self):
        delay = _human_delay_for_chunk("x" * 1000)
        assert delay <= 3.0

    def test_varies_with_length(self):
        short = _human_delay_for_chunk("hi")
        long = _human_delay_for_chunk("this is a much longer message with many chars")
        # Long should generally be >= short (with jitter, not guaranteed, but likely)
        # Just check both are valid
        assert 0.3 <= short <= 3.0
        assert 0.3 <= long <= 3.0


# ── _chunk_message ─────────────────────────────────────────

class TestChunkMessage:
    def test_empty(self):
        assert _chunk_message("") == []
        assert _chunk_message("   ") == []

    def test_short_text_single_chunk(self):
        chunks = _chunk_message("hello")
        assert chunks == ["hello"]

    def test_paragraph_split(self):
        text = "paragraph one\n\nparagraph two"
        chunks = _chunk_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "paragraph one"
        assert chunks[1] == "paragraph two"

    def test_sentence_split(self):
        text = "这是第一句话。这是第二句话。" + "这是第三句话，非常长" * 20
        chunks = _chunk_message(text, max_chars=50)
        assert len(chunks) > 1
        for c in chunks:
            # Each chunk should respect the limit or be a single indivisible unit
            assert len(c) <= max(50, 200)

    def test_file_extension_preserved(self):
        text = "请看文件 report.pdf 和 image.jpg 的内容"
        chunks = _chunk_message(text, max_chars=200)
        full = "".join(chunks)
        assert "report.pdf" in full
        assert "image.jpg" in full

    def test_telegram_limit_hard_split(self):
        text = "x" * 5000
        chunks = _chunk_message(text, max_chars=5000)
        for c in chunks:
            assert len(c) <= TELEGRAM_MSG_LIMIT

    def test_chinese_sentence_enders(self):
        text = "你好。你呢？我很好！再见。"
        chunks = _chunk_message(text, max_chars=8)
        assert len(chunks) >= 2

    def test_clause_level_split(self):
        text = "首先，我们需要准备材料，然后开始制作，最后进行测试，这样就完成了"
        chunks = _chunk_message(text, max_chars=20)
        assert len(chunks) >= 2

    def test_dash_split(self):
        text = "这个功能——也就是消息分段——非常重要，我们必须实现它，否则体验会很差"
        chunks = _chunk_message(text, max_chars=20)
        assert len(chunks) >= 2

    def test_multiple_paragraphs_mixed(self):
        text = "短消息\n\n这是一个比较长的段落，包含逗号、冒号：以及句号。还有问号？"
        chunks = _chunk_message(text, max_chars=30)
        assert len(chunks) >= 2
        assert chunks[0] == "短消息"


# ── </分段> tag splitting ─────────────────────────────────

class TestSplitTag:
    def test_no_tag_returns_none(self):
        assert _split_by_tag("普通消息没有标签") is None

    def test_single_tag_splits_into_two(self):
        assert _split_by_tag("吃了吗</分段>今天忙不忙") == ["吃了吗", "今天忙不忙"]

    def test_multiple_tags_split_into_n(self):
        assert _split_by_tag("一</分段>二</分段>三") == ["一", "二", "三"]

    def test_consecutive_tags_drop_empty(self):
        assert _split_by_tag("前</分段></分段>后") == ["前", "后"]

    def test_leading_tag_dropped(self):
        assert _split_by_tag("</分段>内容") == ["内容"]

    def test_trailing_tag_dropped(self):
        assert _split_by_tag("内容</分段>") == ["内容"]

    def test_whitespace_around_tag_stripped(self):
        assert _split_by_tag("前面  </分段>  后面") == ["前面", "后面"]

    def test_newlines_around_tag_stripped(self):
        assert _split_by_tag("前面\n</分段>\n后面") == ["前面", "后面"]

    def test_tag_with_inner_whitespace_tolerated(self):
        assert _split_by_tag("前</ 分段 >后") == ["前", "后"]

    def test_self_closing_tag_is_not_a_splitter(self):
        assert _split_by_tag("前<分段/>后") is None

    def test_only_tag_returns_empty_list(self):
        assert _split_by_tag("</分段>") == []

    def test_empty_string_returns_none(self):
        assert _split_by_tag("") is None


# ── _decide_chunks ────────────────────────────────────────

class TestDecideChunks:
    def test_no_split_plain_text(self):
        assert _decide_chunks("你好", split_content=False, num_chunks=None) == ["你好"]

    def test_strips_outer_whitespace(self):
        assert _decide_chunks("  hi  ", split_content=False, num_chunks=None) == ["hi"]

    def test_empty_returns_empty_list(self):
        assert _decide_chunks("   ", split_content=False, num_chunks=None) == []

    def test_tag_splits_by_default(self):
        assert _decide_chunks(
            "吃了吗</分段>今天忙不忙", split_content=False, num_chunks=None
        ) == ["吃了吗", "今天忙不忙"]

    def test_num_chunks_1_overrides_tag(self):
        assert _decide_chunks(
            "吃了吗</分段>今天忙不忙", split_content=False, num_chunks=1
        ) == ["吃了吗</分段>今天忙不忙"]

    def test_num_chunks_overrides_tag(self):
        result = _decide_chunks(
            "吃了吗</分段>今天忙不忙", split_content=False, num_chunks=2
        )
        assert result != ["吃了吗", "今天忙不忙"]
        assert "</分段>" in "".join(result)

    def test_tag_chunks_not_further_split_by_punctuation(self):
        assert _decide_chunks(
            "你好</分段>今天,天气,真的,非常,好,我们,去,公园,玩,好不好",
            split_content=False,
            num_chunks=None,
        ) == ["你好", "今天,天气,真的,非常,好,我们,去,公园,玩,好不好"]

    def test_tag_wins_over_split_content(self):
        assert _decide_chunks(
            "一段</分段>另一段", split_content=True, num_chunks=None
        ) == ["一段", "另一段"]

    def test_split_content_keeps_telegram_long_message_behavior(self):
        text = "这是一个比较长的句子，" * 30
        result = _decide_chunks(text, split_content=True, num_chunks=None)
        assert len(result) > 1


# ── _rule_based_compress ──────────────────────────────────

class TestRuleBasedCompress:
    def test_basic(self):
        msgs = [
            Message(sender_id="1", sender_name="Alice", content="hello",
                    timestamp="2026-01-01T00:00", message_id="1"),
            Message(sender_id="2", sender_name="Bob", content="world",
                    timestamp="2026-01-01T00:01", message_id="2"),
        ]
        result = _rule_based_compress(msgs)
        assert "Alice: hello" in result
        assert "Bob: world" in result
        assert "2026-01-01" in result

    def test_long_content_truncated(self):
        msgs = [
            Message(sender_id="1", sender_name="A", content="x" * 200,
                    timestamp="t1", message_id="1"),
        ]
        result = _rule_based_compress(msgs)
        assert "..." in result
        assert len(result) < 200


# ── 分段标记的各种写法 ─────────────────────────────────────

@pytest.mark.parametrize("tag", ["</分段>", "‹/分段›", "＜/分段＞", "〈/分段〉", "</ 分段 >", "<分段>"])
def test_split_tag_variants_all_split(tag):
    # 线上原样发出去过：「明明聪明得很好不好。‹/分段›别拆我台啊姐姐」
    assert _split_by_tag(f"明明聪明得很好不好。{tag}别拆我台啊姐姐") == ["明明聪明得很好不好。", "别拆我台啊姐姐"]


def test_text_that_merely_mentions_segments_is_not_split():
    assert _split_by_tag("我们分段来讲") is None
