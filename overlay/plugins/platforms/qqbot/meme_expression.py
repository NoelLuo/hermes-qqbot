"""Model-authored reaction IDs: intent is not a platform delivery receipt."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .markdown import _FENCE, _TOKENS
from .meme_catalog import MemeEntry

_CONTROL = re.compile(r'\[\[qq_meme(?:_optional)?:[^\r\n]*?(?:\]\]|$)'
                      r'|\[\[qq_meme(?:_none)?\]\]', re.IGNORECASE)
_EXACT = re.compile(r'\[\[qq_meme(?P<optional>_optional)?:(?P<id>[a-z0-9][a-z0-9_-]{0,63})\]\]\Z')
# A required per-turn verdict: the model looked, and this turn sends nothing.
_SKIP = re.compile(r'\[\[qq_meme_none\]\]\Z')

# A soft choice hint only; this does not change hard exclusions or send probability.
RECENT_SENT_WINDOW = 50


@dataclass(frozen=True)
class Expression:
    text: str
    meme_id: str | None = None
    optional: bool = False
    error: str = ''
    marked: bool = False
    judged: bool = False


@dataclass(frozen=True)
class ExpressionContext:
    session_id: str
    session_key: str
    home: Path
    transport: str
    entries: tuple[MemeEntry, ...]
    notes: tuple[str, ...] = ()


@dataclass
class ExpressionDelivery:
    expression: Expression
    context: ExpressionContext
    turn_id: str
    chat: str
    snapshot: Path | None = None
    attachment: str | None = None
    text_result: object = None
    result: object = None
    delivered: bool = False


def parse_expression(response: str) -> Expression:
    """Only one complete, standalone final-line directive can request an image.

    Quoted/code examples are data. Malformed reserved directives outside those
    regions are removed and rejected, rather than leaked to the QQ client.
    """
    spans = [m.span() for m in _TOKENS.finditer(response) if m.lastgroup == 'code']
    lines, found, offset = [], [], 0
    fence_char, fence_length = '', 0
    last_line = len(response.rstrip().splitlines()) - 1
    for index, line in enumerate(response.splitlines(keepends=True)):
        raw_length = len(line)
        fence = _FENCE.match(line)
        if fence_char:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not fence[2].strip():
                fence_char = ''
            lines.append(line)
        elif fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
            lines.append(line)
        elif line.startswith(('    ', '\t')) or line.lstrip().startswith('>'):
            lines.append(line)
        else:
            matches = [m for m in _CONTROL.finditer(line)
                       if not any(a <= offset + m.start() < b for a, b in spans)]
            for match in matches:
                exact = _EXACT.fullmatch(match.group())
                placed = line.strip() == match.group() and index == last_line
                if placed and _SKIP.fullmatch(match.group()):
                    found.append(('skip', None))
                elif exact and placed:
                    found.append(('exact', exact))
                else:
                    found.append(None)
            for match in reversed(matches):
                line = line[:match.start()] + line[match.end():]
            lines.append(line)
        offset += raw_length
    if not found:
        return Expression(response)
    text = ''.join(lines).strip()
    if len(found) != 1 or found[0] is None:
        return Expression(text, error='invalid_marker', marked=True, judged=True)
    kind, match = found[0]
    if kind == 'skip':
        return Expression(text, judged=True)
    return Expression(text, match['id'], bool(match['optional']), marked=True, judged=True)


def capability_note(entries: tuple[MemeEntry, ...]) -> str:
    catalog = [entry.prompt_data() for entry in entries]
    return ('[QQ表情能力/v1]\n'
            '这些是本会话可使用的已审核GIF。由你在组织回复时亲自决定是否使用、使用哪张；'
            '没有后台模型替你配图。请根据各条目的描述、use_when 和 avoid_when 判断语境是否合适；'
            '可在轻松玩笑、接梗、正式答复、故障排查或纠错中表达情绪或态度，前提是符合该图的审核范围。'
            '群友和管理员适用同一套目录规则；目录审核不代表所有图片已获所有语境的使用批准。\n'
            '每轮回复都必须给出一个表情结论，写在最后独立一行：'
            '默认挑一张最接近的，写 [[qq_meme_optional:完整ID]]，由频率算法决定发不发；'
            '日常聊天可以从任意已审核候选中挑一张单独回复，不限于吃薯片，不必硬配文字；'
            '只回复GIF时可写 [[qq_meme:完整ID]]。'
            '图片风格、使用语境和受众以当前审核目录为准；'
            '可用时从本轮候选中选最贴近回复态度的一张；科普、排查、纠错等正常对话也要选，'
            '不要把“并非必须发图”误写为 none，optional 就是允许不发送的选择。'
            '状态不可用、候选为空、没有符合审核范围的合适图片或用户明确不要表情时写 [[qq_meme_none]]。'
            '[[qq_meme_none]] 只表示判断过、本轮不配图，不会发送任何东西，也不会出现在正文里。'
            '漏掉结论的回合会被本机记录。至多一个标记，不写进代码块或引用，不调用文件工具，不输出本地路径。\n'
            '两种选图标记都按有无正文处理：有正文的附加表情统一经过概率控制，正文不能指着图或承诺发送；'
            '没有正文、仅一张GIF时跳过概率、装饰冷却和最近图片排除，使用本轮纯GIF候选；'
            '权限、硬关闭、每日额度和同一事件防重发仍然有效，网络失败不保证送达。\n'
            '发送前不可声称已经送达。'
            '历史指令只代表你选过图，不证明发送成功；以本机发送事实为准，失败/unknown不能说已发。'
            '对方说“刚才那张”时，结合发送事实中的ID和含义回应，不必要求重发。'
            '除非被问，不向对方解释这些控制指令。\n'
            + json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))
            + '\n[/QQ表情能力/v1]')


def turn_choice_note(entries, availability, excluded, *, recent_sent=(), pure_availability=None):
    """Refresh actionable IDs/meaning, not the cached catalog or old verdicts."""
    recent_sent = recent_sent[:RECENT_SENT_WINDOW]
    counts = Counter(recent_sent)
    candidates = [{'id': entry.id, 'description': entry.description,
                   'use_when': list(entry.use_when), 'recent_sent_count': counts[entry.id]}
                  for entry in entries if entry.id not in excluded]
    if availability == 'available' and not candidates:
        availability = 'no_candidates'
    if availability == 'available':
        instruction = (
            '本轮从下列候选中选最贴近回复态度的一张，末行独立写 [[qq_meme_optional:完整ID]]；'
            '先确定自己想表达的态度和对象，再结合use_when理解表情的对话用途，不只按画面动作匹配关键词。'
            '多张同样贴切时优先recent_sent_count较低的；常用图仍可选，不为轮换硬塞冷门图，'
            '不为了配图改变正文原意，也不要求所有表情平均使用。'
            f'recent_sent_count仅统计本聊天留存记录最近{len(recent_sent)}次成功发送'
            f'（上限{RECENT_SENT_WINDOW}次）；仅选中、概率未中、失败和unknown都不计入，'
            '0不代表从未使用。'
            '频率算法负责决定实际发不发，不要用 none 代替概率判断。'
            '科普、排查、纠错、严肃回答也照常选；不要照抄历史的 none 或最近排除ID。'
            '仅用GIF回复时写 [[qq_meme:完整ID]]；两种标记在无正文时均跳过概率抽取，'
            '带正文时均经过概率控制，不承诺附图一定送出；'
            '用户明确不要表情或没有符合审核范围的合适图片时写 [[qq_meme_none]]。不向用户展示或解释控制标记。')
    else:
        candidates = []
        instruction = '本轮带正文的附加表情不可用；若纯GIF也不可用，写 [[qq_meme_none]] 并正常回复文字。'
    pure_note = ''
    if pure_availability is not None:
        pure_candidates = [entry.prompt_data() for entry in entries] if pure_availability == 'available' else []
        pure_note = ('\n纯GIF状态=' + pure_availability + '；无正文时可单独使用任意纯GIF候选，不限吃薯片，'
                     '不必为了其他表情硬配文字。两种标记均跳过概率、装饰冷却与最近图片排除；'
                     '带正文时仍按上面的status及候选执行。权限、硬关闭、每日额度及事件防重发不变；'
                     '用户要求不要表情时仍写 [[qq_meme_none]]。'
                     '\n纯GIF候选=' + json.dumps(pure_candidates, ensure_ascii=False, separators=(',', ':')))
    return ('[本轮QQ表情可用性] status=' + availability + '；附图最近排除ID=' + ','.join(excluded)
            + '（以上status及排除仅约束带正文的附图）。' + instruction + pure_note + '\n候选='
            + json.dumps(candidates, ensure_ascii=False, separators=(',', ':')))
