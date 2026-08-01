"""Types of the VM for Cortex lines (registered before the words are loaded).

Everything that has text is a Src: a block of the stream, an agent's result, a document
(alias), a section of any of them, a computed text. Words take a Src and ask the run
environment for its text only when they run — so a line can wait for a result that is not
there yet. forth() writes a value back the way the line was written (program.log).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fvm.annotations import artifact
from mdlib import HeadingPath, Segment


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@artifact(name="Src")
class Src:
    """Something with text."""

    def label(self) -> str:
        return self.forth()

    def forth(self) -> str:  # pragma: no cover - overridden
        return "?"


@artifact(name="Block", parent="Src")
@dataclass(frozen=True)
class BlockRef(Src):
    name: str

    def forth(self) -> str:
        return self.name


@artifact(name="Result", parent="Src")
@dataclass(frozen=True)
class ResultRef(Src):
    name: str

    def forth(self) -> str:
        return self.name


@artifact(name="Doc", parent="Src")
@dataclass(frozen=True)
class DocRef(Src):
    name: str
    path: str            # relative to the workspace, posix

    def forth(self) -> str:
        return self.name


@artifact(name="File", parent="Src")
@dataclass(frozen=True)
class FileRef(Src):
    """A file named by its path (a "string" inside an input list)."""
    path: str

    def forth(self) -> str:
        return _quote(self.path)


@artifact(name="Section", parent="Src")
@dataclass(frozen=True)
class SectionRef(Src):
    base: Src
    path: HeadingPath

    def forth(self) -> str:
        return f"{self.base.forth()}{_quote(str(self.path))}"


@artifact(name="Text", parent="Src")
@dataclass(frozen=True)
class TextValue(Src):
    text: str
    origin: str = "текст"

    def forth(self) -> str:
        if len(self.text) <= 40 and "\n" not in self.text:
            return _quote(self.text)
        return f"‹{self.origin}: {len(self.text)} симв.›"

    def label(self) -> str:
        return self.origin


@artifact(name="Pos")
@dataclass(frozen=True)
class Pos:
    """Where to insert: kind start | end | path (then `path` is the heading pattern)."""
    kind: str
    path: HeadingPath | None = None

    def forth(self) -> str:
        return {"start": "START", "end": "END"}.get(self.kind) or _quote(str(self.path))

    def describe(self) -> str:
        return {"start": "START", "end": "END"}.get(self.kind) or f"\"{self.path}\""


@artifact(name="Pattern")
@dataclass(frozen=True)
class Pattern:
    path: HeadingPath

    def forth(self) -> str:
        return _quote(str(self.path))


@artifact(name="FilePath")
@dataclass(frozen=True)
class FilePath:
    path: str

    def forth(self) -> str:
        return _quote(self.path)


@artifact(name="ResultSpec")
@dataclass(frozen=True)
class ResultSpec:
    """{name head="# …" has=["# … > ## …", …]} — what an agent's result must be."""
    name: str
    head: Segment | None = None
    has: tuple[HeadingPath, ...] = field(default_factory=tuple)

    def forth(self) -> str:
        parts = [self.name]
        if self.head is not None:
            parts.append(f"head={_quote(str(self.head))}")
        if self.has:
            parts.append("has=[" + ", ".join(_quote(str(p)) for p in self.has) + "]")
        return "{" + " ".join(parts) + "}"
