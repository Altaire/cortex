"""Annotations that put Python code into the VM: @word for primitives, @artifact for types.

    @word("toc/section", "( req:TocRequest id:Str -- TocSection | TocError )", caps=("read",))
    def section(kb, req, id): ...

    @artifact
    @dataclass
    class TocSection: ...

The VM imports every module of the words/ tree and loads what is annotated (VM.register_module).
Leading parameters of a primitive named after a context value ("kb" — the knowledge base, "server" — the server module)
are bound when the word is loaded; a parameter named "instance" receives the running VM
instance. The remaining parameters are taken from the stack, in the order of the stack comment.
"""
from __future__ import annotations

from typing import Any, Callable

WORD_ATTR = "__fvm_word__"
ARTIFACT_ATTR = "__fvm_artifact__"


def word(name: str, sig: str, *, exec: str = "inline", caps: tuple[str, ...] = ("pure",),
         trace: bool = True, doc: str | None = None, params: tuple[str, ...] = ()) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a function as the primitive `name` with the stack comment `sig`.

    exec: inline (in the instance's task) | thread (worker thread, for blocking work) | async.
    caps: rights the word needs — pure, read, write, llm, io, spawn, admin.
    params: inputs of the stack comment given by name (key=value) instead of from the stack;
            in the stack comment they follow the inputs taken from the stack.
    """
    def mark(function: Callable[..., Any]) -> Callable[..., Any]:
        marks = getattr(function, WORD_ATTR, [])
        marks.append({"name": name, "sig": sig, "exec": exec, "caps": tuple(caps), "trace": trace, "doc": doc,
                      "params": tuple(params)})
        setattr(function, WORD_ATTR, marks)  # one function may serve several words
        return function
    return mark


def artifact(cls: type | None = None, *, name: str | None = None, parent: str | None = None) -> Any:
    """Mark a class as a VM type (its name is used in stack comments)."""
    def mark(target: type) -> type:
        setattr(target, ARTIFACT_ATTR, {"name": name or target.__name__, "parent": parent})
        return target
    return mark(cls) if cls is not None else mark
