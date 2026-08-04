"""The document map: the heading tree with the words of each section, and warnings."""
from __future__ import annotations

from collections import Counter

from mdlib.check import structure_issues
from mdlib.edit import to_lines
from mdlib.structure import headings


def _words(lines: list[str]) -> int:
    return sum(len(line.split()) for line in lines)


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def outline(text: str, name: str) -> str:
    lines = to_lines(text)
    heads = headings(text)
    rows: list[tuple[str, str]] = []
    base = min((h.level for h in heads), default=1)
    for h in heads:
        rows.append(("  " * (h.level - base) + str(h), _num(_words(lines[h.line:h.end]))))
    width = max((len(label) for label, _n in rows), default=0)
    out = [f"{name} · {_num(_words(lines))} words · {len(heads)} headings"]
    out += [f"{label.ljust(width)}  {count:>7}" for label, count in rows]
    repeats = Counter((h.level, h.text) for h in heads)
    for (level, title), n in repeats.items():
        if n > 1:
            out.append(f"! \"{'#' * level} {title}\" — {n} times; refer to it with a path \" > \"")
    out += [f"! {issue}" for issue in structure_issues(heads)]
    return "\n".join(out)
