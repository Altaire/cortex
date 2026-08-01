"""Structure checks.

Document: heading levels go in order — inside # comes ##, inside ## comes ### (a level may not
be skipped). Agent result: starts with its head, holds exactly one heading of that level, has
every required path, no skipped levels, no service lines.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from mdlib.edit import strip_blank, to_lines
from mdlib.structure import Heading, HeadingPath, MdError, Segment, candidates, headings

_SERVICE = re.compile(r"^\s*===.*===\s*$")


@dataclass(frozen=True)
class Issue:
    key: tuple
    text: str

    def __str__(self) -> str:
        return self.text


def structure_issues(text_or_heads: str | list[Heading]) -> list[Issue]:
    """Skipped levels: a heading deeper than the previous heading + 1."""
    heads = headings(text_or_heads) if isinstance(text_or_heads, str) else text_or_heads
    issues: list[Issue] = []
    for prev, head in zip(heads, heads[1:]):
        if head.level > prev.level + 1:
            issues.append(Issue(("jump", prev.level, prev.text, head.level, head.text),
                                f"после \"{prev}\" идёт \"{head}\" без уровня {'#' * (prev.level + 1)}"))
    return issues


def issue_counter(issues: list[Issue]) -> Counter:
    return Counter(i.key for i in issues)


def capture_issue(lines: list[str], index: int, chunk: str) -> str | None:
    """Would the inserted text take over the subsections that follow the insertion point?"""
    chunk_heads = headings(chunk)
    if not chunk_heads:
        return None
    top = min(h.level for h in chunk_heads)
    for head in headings("\n".join(lines) + "\n"):
        if head.line >= index:
            if head.level > top:
                return (f"вставка поглотит подраздел \"{head}\": после вставленного "
                        f"\"{chunk_heads[0]}\" он станет его частью")
            return None
    return None


def chunk_issues(text: str, head: Segment | None, has: list[HeadingPath]) -> list[str]:
    """What is wrong with an agent's result (empty list: nothing)."""
    lines = to_lines(text)
    if not strip_blank(lines):
        return ["результат пуст"]
    problems: list[str] = []
    for number, line in enumerate(lines, 1):
        if _SERVICE.match(line) or "@@@" in line:
            problems.append(f"служебная строка {number}: {line.strip()[:60]}")
    heads = headings(text)
    if head is not None:
        first_line = next(i for i, line in enumerate(lines) if line.strip())
        if not heads or heads[0].line != first_line or not head.matches(heads[0]):
            problems.append(f"результат должен начинаться с заголовка \"{head}\"")
        else:
            extra = [h for h in heads[1:] if h.level <= head.level]
            if extra:
                problems.append(f"в результате должен быть один заголовок уровня {'#' * head.level}, "
                                f"лишний: \"{extra[0]}\"")
    problems.extend(str(i) for i in structure_issues(heads))
    for path in has:
        found, depth = candidates(heads, path)
        if not found:
            problems.append(f"нет \"{path}\"")
        elif len(found) > 1:
            problems.append(f"\"{path}\" встречается {len(found)} раза")
    return problems


__all__ = ["Issue", "structure_issues", "issue_counter", "capture_issue", "chunk_issues", "MdError"]
