"""Tokens of one Cortex line.

    name            a word, a declared name, START / END, a number
    "text"          a string (escapes \\" \\\\ \\n \\t); no space needed after the quote
    name"pattern"   a section of a document, block or result
    key=value       a parameter of the word before it; value: "string", name, name"pattern", [list]
    [a, b, c]       a list (commas are spaces)
    {name k=v …}    the description of an agent's result
    \\ …             a comment to the end of the line
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cx.errors import CortexError

_STOP = set(' \t"[]{},=\\')
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


@dataclass
class Tok:
    kind: str            # name | str | sec | kv | list | spec
    value: Any
    col: int

    def __repr__(self) -> str:
        return f"{self.kind}:{self.value!r}"


class _Lexer:
    def __init__(self, text: str, line: int) -> None:
        self.s = text
        self.n = len(text)
        self.line = line

    def error(self, message: str, col: int) -> CortexError:
        return CortexError(f"{message} (позиция {col + 1})", self.line)

    def tokens(self) -> list[Tok]:
        out: list[Tok] = []
        i = 0
        while True:
            i = self.skip(i)
            if i >= self.n or self.s[i] == "\\":
                return out
            tok, i = self.item(i)
            out.append(tok)

    def skip(self, i: int) -> int:
        while i < self.n and (self.s[i].isspace() or self.s[i] == ","):
            i += 1
        return i

    def item(self, i: int) -> tuple[Tok, int]:
        c = self.s[i]
        if c == '"':
            value, j = self.string(i)
            return Tok("str", value, i), j
        if c == "[":
            return self.sequence(i, "]", "list")
        if c == "{":
            return self.sequence(i, "}", "spec")
        if c in "]}":
            raise self.error(f"лишняя «{c}»", i)
        if c == "=":
            raise self.error("«=» без имени параметра перед ним", i)
        j = i
        while j < self.n and self.s[j] not in _STOP and not self.s[j].isspace():
            j += 1
        word = self.s[i:j]
        if j < self.n and self.s[j] == "=":
            value, k = self.value(j + 1)
            return Tok("kv", (word, value), i), k
        if j < self.n and self.s[j] == '"':
            pattern, k = self.string(j)
            return Tok("sec", (word, pattern), i), k
        return Tok("name", word, i), j

    def value(self, i: int) -> tuple[Tok, int]:
        if i >= self.n or self.s[i].isspace() or self.s[i] in ",\\":
            raise self.error("после «=» нет значения", i)
        if self.s[i] == "{":
            raise self.error("значение параметра не может быть {…}", i)
        return self.item(i)

    def sequence(self, i: int, close: str, kind: str) -> tuple[Tok, int]:
        items: list[Tok] = []
        j = i + 1
        while True:
            j = self.skip(j)
            if j >= self.n or self.s[j] == "\\":
                raise self.error(f"нет закрывающей «{close}»", i)
            if self.s[j] == close:
                return Tok(kind, items, i), j + 1
            if self.s[j] in "[{":
                raise self.error("вложенные [ ] и { } не поддерживаются", j)
            tok, j = self.item(j)
            items.append(tok)

    def string(self, i: int) -> tuple[str, int]:
        chars: list[str] = []
        j = i + 1
        while j < self.n:
            c = self.s[j]
            if c == "\\" and j + 1 < self.n:
                chars.append(_ESCAPES.get(self.s[j + 1], "\\" + self.s[j + 1]))
                j += 2
                continue
            if c == '"':
                return "".join(chars), j + 1
            chars.append(c)
            j += 1
        raise self.error("строка без закрывающей кавычки", i)


def lex(text: str, line: int) -> list[Tok]:
    return _Lexer(text, line).tokens()
