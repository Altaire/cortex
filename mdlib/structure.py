"""Headings, sections and heading patterns.

Only headings written with # count (ATX); lines inside code blocks are never headings —
markdown-it parses them as code. A section is the heading line and everything up to the next
heading of the same or a higher level (fewer #), or to the end of the document.

A pattern names a heading by its level and text:
    "# Раздел 5. Методы"      exactly this text, level 1
    "# Раздел 5..."           level 1, the text starts so; the start ends on a word boundary:
                              "# Раздел 1..." does not find "Раздел 10"
    "# Раздел 5... > ## Выводы"   a path: "Выводы" inside the section "Раздел 5"
Spaces after # and repeated spaces do not count; letter case does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark")
_NEWLINES = re.compile(r"\r\n?")
_SPACES = re.compile(r"\s+")
_SEGMENT = re.compile(r"^(#{1,6})(?!#)\s*(.*)$")


class MdError(ValueError):
    """A document or a pattern does not allow the operation; the message says why."""


class NotFound(MdError):
    def __init__(self, path: "HeadingPath", depth: int, where: str = "") -> None:
        self.path = path
        self.depth = depth
        inside = f" внутри \"{HeadingPath(path.segments[:depth])}\"" if depth else ""
        super().__init__(f"раздел \"{path.segments[depth]}\"{inside} не найден{where}")


class Ambiguous(MdError):
    def __init__(self, path: "HeadingPath", depth: int, found: list["Heading"]) -> None:
        self.path = path
        self.found = found
        listed = ", ".join(f"\"{h}\" (строка {h.line + 1})" for h in found[:5])
        more = f" и ещё {len(found) - 5}" if len(found) > 5 else ""
        super().__init__(f"шаблон \"{path.segments[depth]}\" неоднозначен: {len(found)} совпадения — {listed}{more}; "
                         "уточни начало заголовка или укажи путь через \" > \"")


def normalize_newlines(text: str) -> str:
    return _NEWLINES.sub("\n", text)


def normalize_text(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True)
class Heading:
    level: int
    text: str        # as written, without the #s (and closing #s), spaces normalised
    line: int        # 0-based index of the heading line
    end: int         # 0-based index of the first line after its section

    def __str__(self) -> str:
        return f"{'#' * self.level} {self.text}"


def headings(text: str) -> list[Heading]:
    """ATX headings in document order, each with the end of its section."""
    text = normalize_newlines(text)
    tokens = _MD.parse(text)
    found: list[tuple[int, str, int]] = []
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.markup.startswith("#") and token.map:
            inline = tokens[i + 1] if i + 1 < len(tokens) and tokens[i + 1].type == "inline" else None
            found.append((int(token.tag[1:]), normalize_text(inline.content if inline else ""), token.map[0]))
    total = len(text[:-1].split("\n")) if text.endswith("\n") else len(text.split("\n")) if text else 0
    result: list[Heading] = []
    for k, (level, title, line) in enumerate(found):
        end = total
        for level2, _title2, line2 in found[k + 1:]:
            if level2 <= level:
                end = line2
                break
        result.append(Heading(level, title, line, end))
    return result


@dataclass(frozen=True)
class Segment:
    """One step of a pattern: level, text, and whether the text is only the beginning (...)."""
    level: int
    text: str
    prefix: bool

    def __str__(self) -> str:
        return f"{'#' * self.level} {self.text}{'...' if self.prefix else ''}"

    def matches(self, heading: Heading) -> bool:
        if heading.level != self.level:
            return False
        if not self.prefix:
            return heading.text == self.text
        if not heading.text.startswith(self.text):
            return False
        rest = heading.text[len(self.text):]
        return not rest or not (rest[0].isalnum() or rest[0] == "_")

    def compatible(self, other: "Segment") -> bool:
        """Can both name the same heading? (a promised head="# Раздел 4..." and a reference to
        "# Раздел 4. Методы")"""
        if self.level != other.level:
            return False
        probe_self = Heading(self.level, self.text, 0, 0)
        probe_other = Heading(other.level, other.text, 0, 0)
        return self.matches(probe_other) or other.matches(probe_self) or self.text == other.text


@dataclass(frozen=True)
class HeadingPath:
    segments: tuple[Segment, ...]

    def __str__(self) -> str:
        return " > ".join(str(s) for s in self.segments)

    @property
    def last(self) -> Segment:
        return self.segments[-1]

    def compatible(self, other: "HeadingPath") -> bool:
        """Can both paths name the same heading? Compared from the end: the shorter path is a
        tail of the longer one."""
        a, b = self.segments, other.segments
        n = min(len(a), len(b))
        return all(x.compatible(y) for x, y in zip(a[len(a) - n:], b[len(b) - n:]))


def parse_path(text: str) -> HeadingPath:
    """ "# Раздел 5... > ## Выводы" → HeadingPath; errors say what is wrong."""
    parts = [p.strip() for p in text.split(" > ")]
    segments: list[Segment] = []
    for part in parts:
        match = _SEGMENT.match(part)
        if not match:
            raise MdError(f"шаблон раздела \"{part}\" должен начинаться с # (1–6 знаков #)")
        body = normalize_text(match.group(2))
        prefix = body.endswith("...")
        if prefix:
            body = body[:-3].rstrip()
        if not body:
            raise MdError(f"в шаблоне \"{part}\" нет текста заголовка")
        segment = Segment(len(match.group(1)), body, prefix)
        if segments and segment.level <= segments[-1].level:
            raise MdError(f"в пути \"{text}\" уровень \"{segment}\" должен быть глубже, чем \"{segments[-1]}\"")
        segments.append(segment)
    return HeadingPath(tuple(segments))


def candidates(heads: list[Heading], path: HeadingPath) -> tuple[list[Heading], int]:
    """Headings the path may name, and how deep the path got: the parents must be unique."""
    lo, hi = 0, 1 << 60
    for depth, segment in enumerate(path.segments):
        found = [h for h in heads if lo <= h.line < hi and segment.matches(h)]
        if depth == len(path.segments) - 1 or len(found) != 1:
            return found, depth
        parent = found[0]
        lo, hi = parent.line + 1, parent.end
    return [], 0  # pragma: no cover


def locate(text_or_heads: str | list[Heading], path: HeadingPath) -> Heading:
    """The one heading the path names; NotFound or Ambiguous otherwise."""
    heads = headings(text_or_heads) if isinstance(text_or_heads, str) else text_or_heads
    found, depth = candidates(heads, path)
    if not found:
        raise NotFound(path, depth)
    if len(found) > 1:
        raise Ambiguous(path, depth, found)
    return found[0]
