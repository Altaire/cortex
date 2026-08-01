"""The input stream: script lines and named blocks.

    === name ===        a block: the text up to "=== / ===" (anywhere in the stream)
    \\ text              a comment; summary lines that Cortex writes (\\ run 3 · …, \\ done: …)
                        are dropped on input and computed again; "\\ run N" is the run marker
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from cx.errors import CortexError

_OPEN = re.compile(r"^===\s+([^\s/][^\s]*)\s+===\s*$")
_CLOSE = re.compile(r"^===\s*/\s*===\s*$")
_ANY_DELIM = re.compile(r"^===.*===\s*$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
SUMMARY = re.compile(r"^\s*\\\s*(run\s+\d+|plan\b|status:|done:|launch:|running:|waiting:|decide:|message:|note:)")
_RUN = re.compile(r"^\s*\\\s*run\s+(\d+)")


@dataclass
class Block:
    name: str
    text: str
    line: int


@dataclass
class SourceLine:
    number: int          # 1-based, in the stream
    text: str
    kind: str            # code | comment | blank


@dataclass
class Stream:
    lines: list[SourceLine] = field(default_factory=list)
    blocks: dict[str, Block] = field(default_factory=dict)
    run_marker: int | None = None


def split_stream(text: str) -> Stream:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    stream = Stream()
    current: Block | None = None
    body: list[str] = []
    for number, raw in enumerate(text.split("\n"), 1):
        if current is not None:
            if _CLOSE.match(raw):
                current.text = "\n".join(body).strip("\n") + "\n" if "".join(body).strip() else ""
                stream.blocks[current.name] = current
                current, body = None, []
            elif _OPEN.match(raw):
                raise CortexError(f"блок {current.name} (строка {current.line}) не закрыт строкой «=== / ===» "
                                  f"до начала следующего блока", number)
            else:
                body.append(raw)
            continue
        opened = _OPEN.match(raw)
        if opened:
            name = opened.group(1)
            if not _NAME.match(name):
                raise CortexError(f"имя блока «{name}»: латинские буквы, цифры, _ . -", number)
            if name in stream.blocks:
                raise CortexError(f"блок {name} уже был в потоке (строка {stream.blocks[name].line})", number)
            current = Block(name, "", number)
            continue
        if _CLOSE.match(raw) or _ANY_DELIM.match(raw):
            raise CortexError(f"строка-разделитель без открытого блока: {raw.strip()}", number)
        stripped = raw.strip()
        if not stripped:
            stream.lines.append(SourceLine(number, raw, "blank"))
        elif stripped.startswith("\\"):
            marker = _RUN.match(raw)
            if marker:
                stream.run_marker = int(marker.group(1))
            if not SUMMARY.match(raw):
                stream.lines.append(SourceLine(number, raw.rstrip(), "comment"))
        else:
            stream.lines.append(SourceLine(number, raw.rstrip(), "code"))
    if current is not None:
        raise CortexError(f"блок {current.name} не закрыт строкой «=== / ===»", current.line)
    return stream
