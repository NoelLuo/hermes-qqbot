"""QQ outbound bold must survive CommonMark's punctuation flanking rules."""
from html import unescape
from unittest.mock import AsyncMock

import pytest
from markdown_it import MarkdownIt

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.keyboards import InlineKeyboard


@pytest.fixture
def adapter():
    return QQAdapter(PlatformConfig(enabled=True, extra={"markdown_support": True}))


@pytest.mark.parametrize("source,strong", [
    ("- **玩法：**小队制回合战术 RPG", "玩法："),
    ("- **设定剧情：**寻找失踪故人的旅程", "设定剧情："),
    ("前文**“中文引号”**后文", "“中文引号”"),
    ("前文**‘单引号’**后文", "‘单引号’"),
    ("前文**「直角引号」**后文", "「直角引号」"),
    ("前文**『双直角引号』**后文", "『双直角引号』"),
    ('前文**"English quotes"**后文', '"English quotes"'),
    ("前文**'single quotes'**后文", "'single quotes'"),
    ("前文**（括号）**后文", "（括号）"),
    ("前文**《书名》**后文", "《书名》"),
    ("前文**【标签】**后文", "【标签】"),
    ("**结论。**后文", "结论。"),
    ("前文**“只修左边**。", "“只修左边"),
    ("**只修右边”**后文", "只修右边”"),
    ("**玩法:**正文", "玩法:"),
    ("前文__“下划线粗体”__后文", "“下划线粗体”"),
    ("前文***“粗斜体”***后文", "“粗斜体”"),
    ("前文**“包含 `code` 的引号”**后文", "“包含 <code>code</code> 的引号”"),
])
def test_bold_punctuation_renders_and_is_idempotent(adapter, source, strong):
    formatted = adapter.format_message(source)
    html = unescape(MarkdownIt().render(formatted))
    assert f"<strong>{strong}</strong>" in html or f"<strong><em>{strong}</em></strong>" in html
    assert "**" not in html and "__" not in html
    assert adapter.format_message(formatted) == formatted
    assert formatted.replace(" ", "") == source.replace(" ", "")


@pytest.mark.parametrize("source", [
    "玩法能钻研，**世界观够怪，剧情还得有东西**。",
    "**标签：** 正文与 **正常粗体**。",
    "**“整行引用”**",
    "前文**正常粗体**后文",
    r"\*\*“不应加粗”\*\*后文",
    r"\**“不完整转义”**后文",
    "`前文**“行内代码”**后文`",
    "``前文**“含 ` 的代码”**后文``",
    "`跨行代码\n前文**“代码内容”**后文\n结束`",
    "- ```markdown\n前文**“列表内围栏”**后文",
    "> ```markdown\n> 前文**“引用内围栏”**后文\n> ```",
    "[ref]: relative/**“literal”**tail",
    "[链接](relative/**“literal”**tail)",
    '<span title="前文**“属性”**后文">text</span>',
    "```markdown\n前文**“围栏代码”**后文\n```",
    "~~~markdown\n前文**“波浪围栏”**后文\n~~~",
    "```markdown\n前文**“未闭合围栏”**后文",
    "    前文**“缩进代码”**后文",
    "https://example.invalid/**“literal”**tail",
    "[链接](https://example.invalid/**“literal”**tail)",
    "**\n不完整段落\n**正文",
    "**未闭合的“正文",
    "    code\n\n普通正文",
])
def test_code_escapes_urls_and_valid_markdown_are_unchanged(adapter, source):
    assert adapter.format_message(source) == source


def test_multiple_labels_do_not_swallow_each_other(adapter):
    source = '- **玩法：**正文\n- **“设定”**正文\n\n**正常**与**“第二处”**后文'
    html = MarkdownIt().render(adapter.format_message(source))
    assert html.count("<strong>") == 4
    assert "**" not in html


def test_multiline_code_does_not_block_neighboring_prose(adapter):
    source = "前文**“引号”**后文\n`代码\n**“不改”**内容\n结束`\n**结论：**正文"
    formatted = adapter.format_message(source)
    assert '`代码\n**“不改”**内容\n结束`' in formatted
    html = MarkdownIt().render(formatted)
    assert "<strong>“引号”</strong>" in html
    assert "<strong>结论：</strong>" in html


def test_plain_text_mode_does_not_insert_spacing():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"markdown_support": False}))
    assert adapter.format_message("前文**“引号”**后文") == "前文“引号”后文"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,keyboard", [("group", False), ("c2c", False), ("group", True), ("c2c", True)])
async def test_real_outbound_path_normalizes_before_post(adapter, chat_type, keyboard):
    adapter._ensure_connected = AsyncMock(return_value=True)
    adapter._chat_type_map["target"] = chat_type
    adapter._post_message = AsyncMock(return_value=SendResult(success=True, message_id="receipt"))
    source = "前文**“引号”**后文\n- **玩法：**正文"
    if keyboard:
        await adapter.send_with_keyboard("target", source, InlineKeyboard())
    else:
        await adapter.send("target", source)
    path, body = adapter._post_message.call_args.args
    assert path == f"/v2/{'groups' if chat_type == 'group' else 'users'}/target/messages"
    assert body["msg_type"] == 2
    html = MarkdownIt().render(body["markdown"]["content"])
    assert "<strong>“引号”</strong>" in html
    assert "<strong>玩法：</strong>" in html
    assert "**" not in html


@pytest.mark.asyncio
async def test_formatting_precedes_chunking(adapter):
    adapter._ensure_connected = AsyncMock(return_value=True)
    adapter.MAX_MESSAGE_LENGTH = 80
    adapter._send_chunk = AsyncMock(return_value=SendResult(success=True, message_id="receipt"))
    source = ("- **“标签”**正文\n" * 20).strip()
    await adapter.send("target", source)
    assert adapter._send_chunk.await_count > 1
    for call in adapter._send_chunk.call_args_list:
        chunk = call.args[1]
        assert len(chunk) <= adapter.MAX_MESSAGE_LENGTH
        assert "**" not in MarkdownIt().render(chunk)
