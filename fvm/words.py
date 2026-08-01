"""Words, signatures, MCP tool declarations and the instructions of compiled code."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fvm.types import T, parse


@dataclass
class Signature:
    """A stack comment: ( query:Str limit:Int -- Response ). Names are optional (Str Int -- …);
    MCP tools take the names and types of their arguments from the stack comment."""
    inputs: list[T]
    outputs: list[T]
    text: str
    input_names: list[str | None] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Signature":
        body = text.strip()
        if body.startswith("("):
            body = body[1:]
        if body.endswith(")"):
            body = body[:-1]
        if "--" not in body:
            raise ValueError(f"В стековом комментарии нет «--»: ( {body.strip()} )")
        left, right = body.split("--", 1)
        names, inputs = _items(left)
        _out_names, outputs = _items(right)
        return cls(inputs, outputs, "( " + " ".join(body.split()) + " )", names)

    def __str__(self) -> str:
        return self.text


def _items(part: str) -> tuple[list[str | None], list[T]]:
    """Names and types of one side of a stack comment: "name:Type" or "Type";
    "A | B" and "A|B" are one union."""
    tokens = part.split()
    merged: list[str] = []
    for token in tokens:
        if merged and (token.startswith("|") or merged[-1].endswith("|")):
            merged[-1] += token
        else:
            merged.append(token)
    names: list[str | None] = []
    types: list[T] = []
    for item in merged:
        name, _, type_text = item.rpartition(":") if ":" in item else ("", "", item)
        names.append(name or None)
        types.append(parse(type_text))
    return names, types


@dataclass
class Word:
    """A primitive (Python function) or a colon definition (compiled Forth code)."""
    name: str
    sig: Signature
    kind: str                                   # "primitive" | "colon"
    doc: str = ""
    source: str = ""                            # file:line of the definition
    text: str = ""                              # source text of a colon definition (for see)
    fn: Callable[..., Any] | None = None        # primitive
    exec: str = "inline"                        # primitive: inline | thread | async
    caps: frozenset[str] = frozenset({"pure"})  # primitive: what it may do
    pass_instance: bool = False                 # primitive receives the running Instance first
    trace: bool = True                          # write calls of this word to the trace
    params: tuple[str, ...] = ()                # inputs given by name (key=value), after the stack ones
    code: list[Any] = field(default_factory=list)  # colon: instructions


@dataclass(frozen=True)
class WordRef:
    """A word as a value on the stack: ['] name."""
    name: str


@dataclass
class ToolArg:
    name: str
    type: T | None = None
    default: Any = None
    required: bool = True


@dataclass
class Tool:
    """An MCP tool: a template of its call (scripts/mcp/*.fs) — TOOL header and the program.
    A call fills the placeholders (argument names) with the values and runs the program."""
    name: str
    args: list[ToolArg]
    description_parts: list[tuple[str, Any]]    # ("str", text) | ("word", name) — joined at load
    source: str                                 # file:line of TOOL
    code: list[Any] = field(default_factory=list)
    file: str = ""
    text: str = ""                              # the program as written in the template
    description: str = ""


# ---------------------------------------------------------------------------
# Instructions of compiled code. `src` is file:line of the token, for errors and traces.
# ---------------------------------------------------------------------------
@dataclass
class Call:
    name: str
    src: str
    word: Word | None = None   # resolved when all files are loaded


@dataclass
class Lit:
    value: Any
    src: str


@dataclass
class LocalGet:
    name: str
    src: str


@dataclass
class LocalSet:
    names: list[str]
    src: str


@dataclass
class If:
    then: list[Any]
    otherwise: list[Any]
    src: str


@dataclass
class Match:
    branches: list[tuple[T, list[Any]]]
    src: str


@dataclass
class Each:
    body: list[Any]
    parallel: bool
    src: str


@dataclass
class Parallel:
    branches: list[list[Any]]
    src: str


@dataclass
class Loop:
    body: list[Any]
    src: str


@dataclass
class Tick:
    name: str
    src: str
