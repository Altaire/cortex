"""Types of the Forth VM: names in stack comments, checked at load time and at run time.

A type is written as in stack comments:
    Int  Str  Bool  Float  None  Any  Dict      built-in
    List  List[Str]                             lists (List = List[Any])
    Chunk|ChunkNotFound                         union
    Int?                                        optional = Int|None
    a  b  x                                     type variables (one lowercase letter):
                                                dup ( a -- a a ) keeps the type of what it copies
Domain types (Response, Chapter, ...) are Python classes annotated with @artifact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BUILTINS = ("Any", "Int", "Float", "Str", "Bool", "None", "List", "Dict", "Word")


@dataclass(frozen=True)
class T:
    """A type expression: a name, List[elem], a union, or a variable."""
    name: str                        # "Int", "List", "|" for unions, "'" for variables
    args: tuple["T", ...] = ()

    def __str__(self) -> str:
        if self.name == "|":
            return "|".join(str(a) for a in self.args)
        if self.name == "'":
            return self.args[0].name
        if self.name == "List" and self.args:
            return f"List[{self.args[0]}]"
        return self.name


ANY = T("Any")
NONE = T("None")


def var(letter: str) -> T:
    return T("'", (T(letter),))


def is_var(t: T) -> bool:
    return t.name == "'"


def union(*types: T) -> T:
    """Flattened union without duplicates; one member gives the member itself."""
    members: list[T] = []
    for t in types:
        for m in (t.args if t.name == "|" else (t,)):
            if m not in members:
                members.append(m)
    if ANY in members:
        return ANY
    return members[0] if len(members) == 1 else T("|", tuple(members))


_TOKEN_RE = re.compile(r"\s*(\w+|\[|\]|\||\?)")


def parse(text: str) -> T:
    """Parse "List[Str]?", "Chunk|ChunkNotFound", "a" ... into a T."""
    tokens = _TOKEN_RE.findall(text)
    if "".join(tokens) != re.sub(r"\s+", "", text):
        raise ValueError(f"Неверная запись типа: {text!r}")
    pos = 0

    def parse_union() -> T:
        nonlocal pos
        members = [parse_one()]
        while pos < len(tokens) and tokens[pos] == "|":
            pos += 1
            members.append(parse_one())
        return union(*members)

    def parse_one() -> T:
        nonlocal pos
        if pos >= len(tokens) or not re.fullmatch(r"\w+", tokens[pos]):
            raise ValueError(f"Неверная запись типа: {text!r}")
        name = tokens[pos]
        pos += 1
        if len(name) == 1 and name.islower():
            result = var(name)
        elif pos < len(tokens) and tokens[pos] == "[":
            pos += 1
            elem = parse_union()
            if pos >= len(tokens) or tokens[pos] != "]":
                raise ValueError(f"Нет закрывающей ] в типе {text!r}")
            pos += 1
            result = T(name, (elem,))
        else:
            result = T(name)
        if pos < len(tokens) and tokens[pos] == "?":
            pos += 1
            result = union(result, NONE)
        return result

    result = parse_union()
    if pos != len(tokens):
        raise ValueError(f"Лишнее в записи типа: {text!r}")
    return result


class TypeRegistry:
    """Known type names: built-ins and domain classes (with their parents)."""

    def __init__(self) -> None:
        self.classes: dict[str, type] = {}
        self.parents: dict[str, str] = {}

    def register(self, cls: type, name: str | None = None, parent: str | None = None) -> None:
        name = name or cls.__name__
        self.classes[name] = cls
        if parent:
            self.parents[name] = parent
        cls.__fvm_type__ = name  # type: ignore[attr-defined]

    def known(self, name: str) -> bool:
        return name in BUILTINS or name in self.classes

    def check_known(self, t: T, where: str) -> None:
        if t.name in ("|",):
            for a in t.args:
                self.check_known(a, where)
        elif is_var(t):
            return
        else:
            if not self.known(t.name):
                raise ValueError(f"{where}: неизвестный тип {t.name}")
            for a in t.args:
                self.check_known(a, where)

    def is_subtype(self, name: str, parent: str) -> bool:
        while name:
            if name == parent:
                return True
            name = self.parents.get(name, self._builtin_parent(name))
        return False

    def _builtin_parent(self, name: str) -> str:
        cls = self.classes.get(name)
        if cls is None:
            return ""
        for base in cls.__mro__[1:]:  # Response(str) is a Str, Chapter(dict) a Dict, Outcome(tuple) ...
            if base is str:
                return "Str"
            if base is dict:
                return "Dict"
            if base is list:
                return "List"
        return ""

    def fits(self, actual: T, expected: T) -> bool:
        """Can a value of type `actual` be used where `expected` is required?"""
        if expected == ANY or actual == ANY or is_var(expected) or is_var(actual):
            return True
        if actual.name == "|":
            return all(self.fits(a, expected) for a in actual.args)
        if expected.name == "|":
            return any(self.fits(actual, e) for e in expected.args)
        if actual.name == "List" and expected.name == "List":
            if not expected.args or not actual.args:
                return True
            return self.fits(actual.args[0], expected.args[0])
        if expected.name == "List" or actual.name == "List":
            return self.is_subtype(actual.name, expected.name)
        return self.is_subtype(actual.name, expected.name)

    def of_value(self, value: Any) -> T:
        """The run-time type of a value."""
        name = getattr(type(value), "__fvm_type__", None)
        if name:
            return T(name)
        if value is None:
            return NONE
        if isinstance(value, bool):
            return T("Bool")
        if isinstance(value, int):
            return T("Int")
        if isinstance(value, float):
            return T("Float")
        if isinstance(value, str):
            return T("Str")
        if isinstance(value, list):  # the element type: union over the first items (enough for checks and traces)
            return T("List", (union(*(self.of_value(v) for v in value[:20])),)) if value else T("List")
        if isinstance(value, dict):
            return T("Dict")
        from fvm.words import WordRef
        if isinstance(value, WordRef):
            return T("Word")
        return ANY

    def matches(self, value: Any, t: T) -> bool:
        """MATCH at run time: does the value belong to the type of the branch?"""
        return self.fits(self.of_value(value), t)


def python_annotation(t: T) -> Any:
    """Python annotation for an MCP argument type (FastMCP builds the JSON schema from it)."""
    if t.name == "|":
        members = [python_annotation(a) for a in t.args]
        result = members[0]
        for m in members[1:]:
            result = result | m
        return result
    simple = {"Int": int, "Str": str, "Bool": bool, "Float": float, "None": type(None), "Dict": dict, "Any": Any}
    if t.name in simple:
        return simple[t.name]
    if t.name == "List":
        return list[python_annotation(t.args[0])] if t.args else list
    raise ValueError(f"Тип {t} нельзя использовать в аргументе MCP-инструмента")
