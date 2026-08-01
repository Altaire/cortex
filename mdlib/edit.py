"""Edits of a document as a list of lines (no line ends; the text ends with one newline).

Every insertion is separated from its neighbours by exactly one empty line; the positions are
always section boundaries (a heading line, the end of a section, the start or the end of the
document), never the middle of a code block.
"""
from __future__ import annotations

import re

from mdlib.structure import MdError, headings, normalize_newlines

_ATX = re.compile(r"^( {0,3})(#{1,6})(?=\s|$)")


def to_lines(text: str) -> list[str]:
    text = normalize_newlines(text)
    if not text:
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def from_lines(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def strip_blank(lines: list[str]) -> list[str]:
    """Without empty lines at the beginning and at the end."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _join(head: list[str], middle: list[str], tail: list[str]) -> list[str]:
    head = list(head)
    while head and not head[-1].strip():
        head.pop()
    tail = list(tail)
    while tail and not tail[0].strip():
        tail.pop(0)
    out: list[str] = []
    for part in (head, middle, tail):
        if not part:
            continue
        if out:
            out.append("")
        out.extend(part)
    return out


def insert_at(lines: list[str], index: int, chunk: str) -> list[str]:
    return _join(lines[:index], strip_blank(to_lines(chunk)), lines[index:])


def replace_span(lines: list[str], start: int, end: int, chunk: str) -> list[str]:
    return _join(lines[:start], strip_blank(to_lines(chunk)), lines[end:])


def delete_span(lines: list[str], start: int, end: int) -> list[str]:
    return _join(lines[:start], [], lines[end:])


def move_span(lines: list[str], start: int, end: int, locate_target) -> list[str]:
    """Cut lines[start:end] and insert them where locate_target(lines without them) points."""
    chunk = strip_blank(lines[start:end])
    rest = delete_span(lines, start, end)
    index = locate_target(rest)
    return _join(rest[:index], chunk, rest[index:])


def relevel(text: str, top: int) -> str:
    """Shift every heading so that the highest one gets level `top`."""
    if not 1 <= top <= 6:
        raise MdError(f"relevel: уровень {top} вне 1–6")
    heads = headings(text)
    if not heads:
        return text
    delta = top - min(h.level for h in heads)
    if delta == 0:
        return text
    deepest = max(h.level for h in heads) + delta
    if deepest > 6:
        raise MdError(f"relevel top={top}: самый глубокий заголовок получил бы уровень {deepest} (больше 6)")
    lines = to_lines(text)
    for h in heads:
        lines[h.line] = _ATX.sub(lambda m: m.group(1) + "#" * (len(m.group(2)) + delta), lines[h.line], count=1)
    return from_lines(lines)


def join_texts(texts: list[str]) -> str:
    out: list[str] = []
    for text in texts:
        part = strip_blank(to_lines(text))
        if part:
            if out:
                out.append("")
            out.extend(part)
    return from_lines(out)
