"""Minimal QQ wire-format repair for bold next to Unicode punctuation.

QQ receives raw Markdown, so changing a local Markdown parser cannot fix its
rendering. Add ordinary spaces only at blocked outer delimiter boundaries. Never
normalize full-width characters or put invisible characters into message text.
"""
from __future__ import annotations

import re
import unicodedata


_FENCE = re.compile(r"^(?: {0,3}>[ \t]?)* {0,3}(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})([^\r\n]*)")
_TOKENS = re.compile(
    r"(?P<escape>\\.)"
    r"|(?P<code>(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<destination>\]\((?:\\.|[^()\\\n]|\([^()\n]*\))*\))"
    r"|(?P<url>https?://[^\s<>]+)"
    r"|(?P<tag><[^>\n]+>)"
    r"|(?P<strong>(?<!\*)\*{2,3}(?!\*)|(?<!_)_{2,3}(?!_))"
)
_REFERENCE = re.compile(r"^ {0,3}\[[^]\n]+\]:")


def _punctuation(char: str) -> bool:
    # CommonMark punctuation includes symbols, not just full-width/CJK marks.
    return bool(char) and unicodedata.category(char)[0] in {"P", "S"}


def _blocked_boundary(inner: str, outer: str) -> bool:
    return _punctuation(inner) and bool(outer) and not outer.isspace() and not _punctuation(outer)


def _normalize_line(line: str, code_spans: list[tuple[int, int]]) -> str:
    openers: dict[str, tuple[int, int]] = {}
    spaces: set[int] = set()
    for token in _TOKENS.finditer(line):
        if token.lastgroup != "strong" or any(left <= token.start() < right for left, right in code_spans):
            continue
        marker = token.group()
        start, end = token.span()
        before = line[start - 1] if start else ""
        after = line[end] if end < len(line) else ""
        if marker in openers and before and not before.isspace():
            left, inner_start = openers.pop(marker)
            if _blocked_boundary(line[inner_start], line[left - 1] if left else ""):
                spaces.add(left)
            if _blocked_boundary(before, after):
                spaces.add(end)
        elif after and not after.isspace():
            openers[marker] = (start, end)
    if not spaces:
        return line
    parts, previous = [], 0
    for position in sorted(spaces):
        parts.extend((line[previous:position], " "))
        previous = position
    parts.append(line[previous:])
    return "".join(parts)


def normalize_qq_markdown(content: str) -> str:
    """Repair paired **/__ (and triple) spans, leaving code/URLs/escapes intact.

    This is a conservative inline-bold pre-pass, not a Markdown parser: unmatched
    or cross-line delimiters are left alone. Run before chunking; repeated calls
    are idempotent. Inline code may still appear *inside* a repaired bold span.
    """
    lines = []
    # Code spans may contain newlines; protect them before splitting into lines.
    code_spans = [token.span() for token in _TOKENS.finditer(content) if token.lastgroup == "code"]
    offset = 0
    fence_char, fence_length = "", 0
    for line in content.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if fence_char:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not fence[2].strip():
                fence_char = ""
            lines.append(line)
        elif fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
            lines.append(line)
        elif line.startswith(("    ", "\t")) or _REFERENCE.match(line):
            lines.append(line)
        else:
            local_spans = [(left - offset, right - offset) for left, right in code_spans
                           if left < offset + len(line) and right > offset]
            lines.append(_normalize_line(line, local_spans))
        offset += len(line)
    return "".join(lines)
