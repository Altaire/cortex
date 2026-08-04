"""The Forth VM: dictionary of words, loading of the words/ tree, instances, interpreter.

One VM per process holds the shared, read-only-after-load dictionary. Words live in folders
by meaning (words/<vocabulary>/): Python primitives annotated with @word and Forth
definitions in .fs files next to them. Every execution runs in an Instance: its own stack,
locals, trace, rights and (later) client session. Instances are asyncio tasks; primitives
declared exec="thread" run in a worker thread so the event loop is never blocked.

An MCP call takes the template of its tool (scripts/mcp/*.fs: TOOL header + program), fills
the placeholders (argument names) with the values of the call, checks them and runs the
program in a fresh instance, printing the program shrinking step by step (fvm/render.py).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import importlib
import inspect
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from fvm.annotations import ARTIFACT_ATTR, WORD_ATTR
from fvm.checker import Checker
from fvm.compiler import compile_source, compile_template
from fvm.render import Memory, forth_literal, render_code
from fvm.session import Session, Sessions
from fvm.trace import Run, describe_error
from fvm.types import TypeRegistry
from fvm.values import Chan, Response, Task
from fvm.words import (
    Call, Each, If, Lit, LocalGet, LocalSet, Loop, Match, Parallel, Signature, Tick, Tool, Word, WordRef,
)

ALL_CAPS = None   # an instance without restrictions (the server's own scripts)


class ForthError(RuntimeError):
    """A run-time error of the VM itself (stack underflow, no MATCH branch, rights, ...)."""


class Exit(Exception):
    """Stop the start script with an exit code (a primitive already printed why)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"exit {code}")
        self.code = code


class Frame:
    """Local names of one activation; EACH / PARALLEL bodies get a child frame."""

    __slots__ = ("values", "parent")

    def __init__(self, parent: "Frame | None" = None, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values or {})
        self.parent = parent

    def lookup(self, name: str) -> tuple[bool, Any]:
        frame: Frame | None = self
        while frame is not None:
            if name in frame.values:
                return True, frame.values[name]
            frame = frame.parent
        return False, None

    def get(self, name: str) -> Any:
        found, value = self.lookup(name)
        if not found:
            raise ForthError(f"local name {name} is not set")
        return value

    def child(self) -> "Frame":
        return Frame(self)


@dataclass
class Ctl:
    """One level of "where we are": a code list with the position being executed, its locals
    and its stack — or an EACH in progress. The chain of levels is the rest of the program."""
    code: list[Any]
    frame: Frame
    stack: list[Any]
    pc: int = -1
    each: Each | None = None          # an EACH level: the loop around the body being executed
    rest: list[Any] = field(default_factory=list)
    done: int = 0


# The levels of the running code of this task (asyncio tasks of PARALLEL get their own copy).
_control: contextvars.ContextVar[tuple[Ctl, ...]] = contextvars.ContextVar("fvm_control", default=())


class Instance:
    _ids = itertools.count(1)

    def __init__(self, vm: "VM", name: str, run: Run, caps: frozenset[str] | None = ALL_CAPS,
                 session: Session | None = None, max_steps: int | None = None) -> None:
        self.vm = vm
        self.name = f"{name}-{next(self._ids)}"
        self.run = run
        self.caps = caps
        self.session = session
        self.max_steps = max_steps
        self.steps = 0
        self.shown = 0                    # steps printed to the console / program.log
        self.stack: list[Any] = []

    # ----- entry points -----
    async def call_word(self, word: Word, inputs: Iterable[Any] = ()) -> list[Any]:
        self.stack.extend(inputs)
        await self._call(word, self.stack, f"<{self.run.kind}>", 0, Frame())
        return self.stack

    async def run_code(self, code: list[Any], frame: Frame | None = None) -> list[Any]:
        await self._code(code, frame or Frame(), self.stack, 0)
        return self.stack

    # ----- the program as text -----
    def program(self) -> str:
        """The state as one line: the stack, then ▸ and the rest of the program, innermost first."""
        levels = _control.get()
        memory = self.run.memory
        rest: list[str] = []
        for ctl in reversed(levels):
            if ctl.each is not None:
                body = " ".join(render_code(ctl.each.body, ctl.frame.lookup, memory))
                rest.append(f"‹EACH done {ctl.done}, left {forth_list(ctl.rest, memory)}: {body} NEXT›")
            else:
                rest += render_code(ctl.code[ctl.pc + 1:], ctl.frame.lookup, memory)
        stack = levels[-1].stack if levels else self.stack
        shown_stack = " ".join(memory.show(v) for v in stack)
        return f"{shown_stack} ▸ {' '.join(rest)}".strip() if rest else f"{shown_stack} ▸ (end)".lstrip()

    def _step(self, text: str) -> None:
        """One step of the run: what happened, then the program that is left."""
        if not self.run.stepping:
            return
        self.shown += 1
        self.run.say(f"{self.shown:>3}. {text}", f"     {self.program()}", level="steps")

    # ----- interpreter -----
    async def _code(self, code: list[Any], frame: Frame, stack: list[Any], depth: int) -> None:
        ctl = Ctl(code, frame, stack)
        token = _control.set(_control.get() + (ctl,))
        try:
            for index, ins in enumerate(code):
                ctl.pc = index
                await self._one(ins, frame, stack, depth)
        finally:
            _control.reset(token)

    async def _one(self, ins: Any, frame: Frame, stack: list[Any], depth: int) -> None:
        kind = type(ins)
        if kind is Call:
            await self._call(ins.word, stack, ins.src, depth, frame)
        elif kind is Lit:
            stack.append(list(ins.value) if isinstance(ins.value, list) else ins.value)
        elif kind is LocalGet:
            stack.append(frame.get(ins.name))
        elif kind is LocalSet:
            n = len(ins.names)
            self._need(stack, n, f"{{ {' '.join(ins.names)} }}", ins.src)
            values = stack[len(stack) - n:]
            del stack[len(stack) - n:]
            frame.values.update(zip(ins.names, values))
            if self.run.stepping:
                memory = self.run.memory
                self._step("{ " + " ".join(ins.names) + " } ← "
                           + "  ".join(f"{name} = {memory.label(v)}" for name, v in zip(ins.names, values)))
        elif kind is If:
            self._need(stack, 1, "IF", ins.src)
            flag = stack.pop()
            if not isinstance(flag, bool):
                raise ForthError(f"{ins.src}: IF needs a Bool, the top is {self.vm.types.of_value(flag)}")
            await self._code(ins.then if flag else ins.otherwise, frame, stack, depth)
        elif kind is Match:
            self._need(stack, 1, "MATCH", ins.src)
            top = stack[-1]
            for branch_type, body in ins.branches:
                if self.vm.types.matches(top, branch_type):
                    await self._code(body, frame, stack, depth)
                    break
            else:
                raise ForthError(f"{ins.src}: MATCH has no branch for type {self.vm.types.of_value(top)}")
        elif kind is Each:
            self._need(stack, 1, "EACH", ins.src)
            items = stack.pop()
            if not isinstance(items, list):
                raise ForthError(f"{ins.src}: EACH needs a List, the top is {self.vm.types.of_value(items)}")
            if ins.parallel:
                results = await asyncio.gather(*(self._item(ins, item, frame, depth, None) for item in items))
            else:
                loop = Ctl([], frame, stack, each=ins, rest=list(items))
                token = _control.set(_control.get() + (loop,))
                try:
                    results = []
                    while loop.rest:
                        item = loop.rest.pop(0)
                        results.append(await self._item(ins, item, frame, depth, loop))
                        loop.done += 1
                finally:
                    _control.reset(token)
            stack.append(list(results))
        elif kind is Parallel:
            outputs = await asyncio.gather(*(self._branch(body, frame, depth) for body in ins.branches))
            for out in outputs:
                stack.extend(out)
        elif kind is Loop:
            while True:
                await self._code(ins.body, frame, stack, depth)
                self._need(stack, 1, "UNTIL", ins.src)
                if stack.pop() is True:
                    break
        elif kind is Tick:
            stack.append(WordRef(ins.name))
        else:  # pragma: no cover
            raise ForthError(f"unknown instruction {ins!r}")

    async def _item(self, ins: Each, item: Any, frame: Frame, depth: int, loop: Ctl | None) -> Any:
        stack = [item]
        if loop is not None and self.run.stepping:
            ctl = Ctl(ins.body, frame.child(), stack)
            token = _control.set(_control.get() + (ctl,))
            try:
                self._step(f"EACH {loop.done + 1}/{loop.done + 1 + len(loop.rest)} ← {self.run.memory.label(item)}")
            finally:
                _control.reset(token)
            await self._code(ins.body, ctl.frame, stack, depth)
        else:
            await self._code(ins.body, frame.child(), stack, depth)
        if len(stack) != 1:
            raise ForthError(f"{ins.src}: the body of EACH left {len(stack)} values instead of one")
        return stack[0]

    async def _branch(self, body: list[Any], frame: Frame, depth: int) -> list[Any]:
        stack: list[Any] = []
        await self._code(body, frame.child(), stack, depth)
        return stack

    def _need(self, stack: list[Any], n: int, what: str, src: str) -> None:
        if len(stack) < n:
            raise ForthError(f"{src}: {what}: {len(stack)} values on the stack, {n} needed")

    async def _call(self, word: Word, stack: list[Any], src: str, depth: int, frame: Frame) -> None:
        self.steps += 1
        if self.max_steps is not None and self.steps > self.max_steps:
            raise ForthError(f"{src}: the instance step limit is exceeded ({self.max_steps})")
        if self.caps is not None and not word.caps <= self.caps:
            raise ForthError(f"{src}: word {word.name} needs rights {sorted(word.caps - self.caps)}")
        n_in = len(word.sig.inputs)
        self._need(stack, n_in, word.name, src)
        tracing = self.run.tracing and word.trace
        step = self.run.next_step() if tracing else 0
        started = time.perf_counter()
        inputs = stack[len(stack) - n_in:] if tracing else []
        try:
            if word.kind == "primitive":
                await self._primitive(word, stack, src)
                if self.run.stepping:
                    outputs = stack[len(stack) - len(word.sig.outputs):] if word.sig.outputs else []
                    ms = (time.perf_counter() - started) * 1000
                    result = " ".join(self.run.memory.label(v) for v in outputs)
                    self._step(f"{word.name}  {ms:.1f} ms" + (f" → {result}" if result else ""))
            else:
                base = len(stack) - n_in
                body_frame = Frame()
                if self.run.stepping:  # the word is replaced by its body: the program rewrites itself
                    ctl = Ctl(word.code, body_frame, stack)
                    token = _control.set(_control.get() + (ctl,))
                    try:
                        self._step(f"{word.name} → expanded ({word.source})")
                    finally:
                        _control.reset(token)
                await self._code(word.code, body_frame, stack, depth + 1)
                if len(stack) != base + len(word.sig.outputs):
                    raise ForthError(f"{src}: word {word.name} {word.sig} left "
                                     f"{len(stack) - base} values instead of {len(word.sig.outputs)}")
        except BaseException as exc:
            frames = getattr(exc, "__fvm_stack__", None)
            if frames is None:
                frames = []
                try:
                    exc.__fvm_stack__ = frames  # type: ignore[attr-defined]
                except AttributeError:  # pragma: no cover
                    pass
            frames.append(f"{word.name}  ← {src}")
            if tracing:
                self._record(word, src, depth, step, started, inputs, None, exc)
            raise
        if tracing:
            outputs = stack[len(stack) - len(word.sig.outputs):] if word.sig.outputs else []
            self._record(word, src, depth, step, started, inputs, outputs, None)

    async def _primitive(self, word: Word, stack: list[Any], src: str) -> None:
        n_in = len(word.sig.inputs)
        args = stack[len(stack) - n_in:]
        for value, expected in zip(args, word.sig.inputs):
            if not self.vm.types.fits(self.vm.types.of_value(value), expected):
                raise ForthError(f"{src}: {word.name} {word.sig}: got {self.vm.types.of_value(value)} "
                                 f"instead of {expected}")
        del stack[len(stack) - n_in:]
        if word.pass_instance:
            args = [self, *args]
        if word.exec == "thread":
            result = await asyncio.to_thread(word.fn, *args)
        elif word.exec == "async":
            result = await word.fn(*args)
        else:
            result = word.fn(*args)
        n_out = len(word.sig.outputs)
        if n_out == 1:
            stack.append(result)
        elif n_out > 1:
            stack.extend(result)

    def _record(self, word: Word, src: str, depth: int, step: int, started: float,
                inputs: list[Any], outputs: list[Any] | None, error: BaseException | None) -> None:
        entry: dict[str, Any] = {
            "vm": self.name, "step": step, "depth": depth, "word": word.name, "src": src,
            "in": [self.run.ref(v) for v in inputs],
            "ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if error is None:
            entry["out"] = [self.run.ref(v) for v in outputs or []]
        else:
            entry["error"] = f"{type(error).__name__}: {error}"
        self.run.record(entry)


def forth_list(values: list[Any], memory: Any) -> str:
    return "[ " + " ".join(memory.show(v) for v in values) + " ]" if values else "[ ]"


class VM:
    def __init__(self, words_dir: Path, scripts_dir: Path, out_dir: Path, context: dict[str, Any] | None = None,
                 trace_level: str | None = None, save_runs: bool | None = None, console: str | None = None) -> None:
        self.words_dir = Path(words_dir)
        self.scripts_dir = Path(scripts_dir)
        self.out_dir = Path(out_dir)
        self.context = dict(context or {})   # values bound to primitives' leading parameters: kb, server
        self.trace_level = (trace_level or os.environ.get("FVM_TRACE", "calls")).strip().lower()
        self.console = (console or os.environ.get("FVM_CONSOLE", "steps")).strip().lower()
        self.save_runs = (os.environ.get("FVM_RUNS", "1").strip().lower() not in {"0", "false", "no", "off"}
                          if save_runs is None else save_runs)
        self.types = TypeRegistry()
        self.words: dict[str, Word] = {}
        self.tools: list[Tool] = []
        self.sessions = Sessions()
        for cls in (Response, Task, Chan):
            self.types.register(cls)
        self.types.parents["Response"] = "Str"

    # ----- dictionary -----
    def primitive(self, name: str, sig: str, fn: Callable[..., Any], *, exec: str = "inline",
                  caps: Iterable[str] = ("pure",), doc: str | None = None, pass_instance: bool = False,
                  trace: bool = True, source: Any = None, params: Iterable[str] = ()) -> Word:
        """Add a primitive (normally via @word and register_module)."""
        if exec not in ("inline", "thread", "async"):
            raise ValueError(f"{name}: exec must be inline, thread or async")
        target = source or getattr(fn, "func", fn)  # the annotated function behind a partial
        word = Word(name=name, sig=Signature.parse(sig), kind="primitive", fn=fn, exec=exec,
                    caps=frozenset(caps), doc=(doc if doc is not None else (target.__doc__ or "")).strip(),
                    source=f"{getattr(target, '__module__', '?')}.{getattr(target, '__qualname__', '?')}",
                    pass_instance=pass_instance, trace=trace, params=tuple(params))
        names = word.sig.input_names
        for param in word.params:
            if param not in names:
                raise ValueError(f"{name}: parameter {param} is not in the stack comment {word.sig}")
        if word.params and names[len(names) - len(word.params):] != list(word.params):
            raise ValueError(f"{name}: parameters {word.params} must be the last inputs of {word.sig}, in order")
        self._add(word)
        return word

    def register_module(self, module: Any) -> int:
        """Load the annotated content of a Python module: @artifact types, then @word primitives."""
        self._register_types(module)
        return self._register_words(module)

    @staticmethod
    def _own(module: Any) -> list[Any]:
        return [obj for obj in vars(module).values() if getattr(obj, "__module__", None) == module.__name__]

    def _register_types(self, module: Any) -> None:
        for obj in self._own(module):
            mark = getattr(obj, ARTIFACT_ATTR, None) if isinstance(obj, type) else None
            if mark is not None:
                self.types.register(obj, mark["name"], mark["parent"])

    def _register_words(self, module: Any) -> int:
        count = 0
        for obj in self._own(module):
            for mark in (getattr(obj, WORD_ATTR, None) or []) if callable(obj) and not isinstance(obj, type) else []:
                fn, pass_instance = self._bind(obj, mark["name"])
                self.primitive(mark["name"], mark["sig"], fn, exec=mark["exec"], caps=mark["caps"],
                               doc=mark["doc"] if mark["doc"] is not None else (obj.__doc__ or ""),
                               pass_instance=pass_instance, trace=mark["trace"], source=obj,
                               params=mark.get("params", ()))
                count += 1
        return count

    def _bind(self, function: Callable[..., Any], name: str) -> tuple[Callable[..., Any], bool]:
        """Bind leading parameters named after context values (kb, server); "instance" → pass the instance."""
        params = list(inspect.signature(function).parameters)
        bound: list[Any] = []
        while params and params[0] in self.context:
            bound.append(self.context[params.pop(0)])
        pass_instance = bool(params) and params[0] == "instance"
        if any(p in self.context or p == "instance" for p in params[1 if pass_instance else 0:]):
            raise ValueError(f"{name}: context parameters (kb, server) and instance must come first")
        return (functools.partial(function, *bound) if bound else function), pass_instance

    def _add(self, word: Word) -> None:
        if word.name in self.words:
            raise ValueError(f"{word.source}: word {word.name} is already defined in {self.words[word.name].source}")
        for t in (*word.sig.inputs, *word.sig.outputs):
            self.types.check_known(t, f"{word.source}: {word.name}")
        self.words[word.name] = word

    def word(self, name: str) -> Word:
        try:
            return self.words[name]
        except KeyError:
            raise ForthError(f"word {name} is not defined") from None

    # ----- loading -----
    def load(self, import_python: bool = True) -> None:
        """Load the words/ tree: every Python module (annotated primitives and types), then every
        .fs file; link calls, check stack effects; then the call templates of the MCP tools
        (scripts/mcp/*.fs, in file name order = the order of the tools)."""
        if import_python:
            package = self.words_dir.name   # its parent folder is on sys.path (the project root)
            modules = [importlib.import_module(".".join((package, *path.relative_to(self.words_dir).with_suffix("").parts)))
                       for path in sorted(self.words_dir.rglob("*.py")) if path.name != "__init__.py"]
            for module in modules:          # all types first: words of one module use types of another
                self._register_types(module)
            for module in modules:
                self._register_words(module)
        for path in sorted(self.words_dir.rglob("*.fs")):
            unit = compile_source(path.read_text(encoding="utf-8"), self._rel(path))
            for word in unit.words:
                self._add(word)
        for word in self.words.values():
            if word.kind == "colon":
                self._link(word.code)
        self._compute_caps()
        checker = Checker(self.types)
        for word in self.words.values():
            if word.kind == "colon":
                checker.check_word(word)
        for path in sorted((self.scripts_dir / "mcp").glob("*.fs")):
            tool = compile_template(path.read_text(encoding="utf-8"), self._rel(path))
            if any(t.name == tool.name for t in self.tools):
                raise ValueError(f"{tool.source}: tool {tool.name} is already declared")
            self._link(tool.code)
            out = checker.check_script(tool.code, tool.file, locals_types={a.name: a.type for a in tool.args})
            if [str(t) for t in out] != ["Response"]:
                raise ValueError(f"{tool.file}: the template program must leave ( Response ), it leaves "
                                 f"( {' '.join(str(t) for t in out)} )")
            self._prepare_tool(tool)
            self.tools.append(tool)

    def compile_script(self, text: str, file: str, stack: list[Any] | None = None) -> list[Any]:
        """Top-level code of a script (definitions in it are added to the dictionary).
        `stack` — values already on the stack of the instance that will run it (REPL)."""
        unit = compile_source(text, file, allow_script=True)
        for word in unit.words:
            self._add(word)
            self._link(word.code)
        self._link(unit.script)
        checker = Checker(self.types)
        for word in unit.words:
            checker.check_word(word)
        checker.check_script(unit.script, file, [self.types.of_value(v) for v in stack or []])
        return unit.script

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.words_dir.parent).as_posix()   # words/… and scripts/… alike
        except ValueError:
            return path.as_posix()

    def _link(self, code: list[Any]) -> None:
        for ins in code:
            if isinstance(ins, Call):
                if ins.name not in self.words:
                    raise ValueError(f"{ins.src}: word {ins.name} is not defined")
                ins.word = self.words[ins.name]
            elif isinstance(ins, Tick):
                if ins.name not in self.words:
                    raise ValueError(f"{ins.src}: word {ins.name} is not defined")
            elif isinstance(ins, If):
                self._link(ins.then)
                self._link(ins.otherwise)
            elif isinstance(ins, Match):
                for _t, body in ins.branches:
                    self._link(body)
            elif isinstance(ins, (Each, Loop)):
                self._link(ins.body)
            elif isinstance(ins, Parallel):
                for body in ins.branches:
                    self._link(body)

    def _compute_caps(self) -> None:
        """Rights of a colon word = union of the rights of every word it calls."""
        done: dict[str, frozenset[str]] = {}

        def calls(code: list[Any]) -> Iterable[Word]:
            for ins in code:
                if isinstance(ins, Call):
                    yield ins.word
                elif isinstance(ins, If):
                    yield from calls(ins.then)
                    yield from calls(ins.otherwise)
                elif isinstance(ins, Match):
                    for _t, body in ins.branches:
                        yield from calls(body)
                elif isinstance(ins, (Each, Loop)):
                    yield from calls(ins.body)
                elif isinstance(ins, Parallel):
                    for body in ins.branches:
                        yield from calls(body)

        def caps_of(word: Word, visiting: set[str]) -> frozenset[str]:
            if word.kind == "primitive":
                return word.caps
            if word.name in done:
                return done[word.name]
            if word.name in visiting:
                return frozenset()
            visiting.add(word.name)
            result = frozenset().union(*(caps_of(w, visiting) for w in calls(word.code))) or frozenset({"pure"})
            visiting.discard(word.name)
            done[word.name] = result
            return result

        for word in self.words.values():
            if word.kind == "colon":
                word.caps = caps_of(word, set())

    def _prepare_tool(self, tool: Tool) -> None:
        """Description and default values of a template (words ( -- value ) are run now)."""
        pieces = []
        for kind, value in tool.description_parts:
            pieces.append(value if kind == "str" else str(self.evaluate(value)))
        tool.description = "".join(pieces)
        for arg in tool.args:
            if not arg.required and arg.default[0] == "word":
                arg.default = ("lit", self.evaluate(arg.default[1]))

    def evaluate(self, name: str) -> Any:
        """Run a word ( -- value ) at load time and return the value."""
        word = self.word(name)
        if word.sig.inputs or len(word.sig.outputs) != 1:
            raise ValueError(f"{name}: a load-time value needs a word ( -- value ), not {word.sig}")
        stack = run_sync(lambda: self.run_word(name, kind="load"))
        return stack[-1]

    # ----- running -----
    def new_run(self, kind: str, name: str, request: dict[str, Any] | None = None) -> Run:
        save = self.save_runs if kind == "tool" else kind == "boot"
        console = self.console if kind in ("tool", "boot", "script") else "off"
        return Run(self.out_dir, kind, name, self.trace_level, console, save, Memory(self.types), request)

    async def run_word(self, name: str, inputs: Iterable[Any] = (), kind: str = "word",
                       caps: frozenset[str] | None = ALL_CAPS, session: Session | None = None) -> list[Any]:
        run = self.new_run(kind, name)
        instance = Instance(self, kind, run, caps, session)
        try:
            return await instance.call_word(self.word(name), inputs)
        finally:
            run.finish()

    def program_lines(self, code: list[Any], lookup: Callable[[str], tuple[bool, Any]], memory: Memory,
                      literal: bool = False) -> list[str]:
        """A program as text, one line per source line; bound names → values (literal: always as
        literals, so the text can be run again; otherwise big values as #addresses)."""
        show = (lambda v: forth_literal(v)) if literal else memory.show
        shown = type("Shown", (), {"show": staticmethod(show)})()
        lines: dict[int, list[str]] = {}
        for ins in code:
            line = int(str(ins.src).rsplit(":", 1)[-1]) if ":" in str(ins.src) else 0
            lines.setdefault(line, []).extend(render_code([ins], lookup, shown))
        return [" ".join(tokens) for _line, tokens in sorted(lines.items())]

    def check_arguments(self, tool: Tool, arguments: dict[str, Any]) -> None:
        """The values of a call against the types of the template's arguments."""
        for arg in tool.args:
            if arg.name not in arguments:
                raise ValueError(f"argument {arg.name} is missing")
            actual = self.types.of_value(arguments[arg.name])
            if not self.types.fits(actual, arg.type):
                raise ValueError(f"argument {arg.name}: expected {arg.type}, got {actual}")

    def tool_program(self, tool: Tool, arguments: dict[str, Any]) -> str:
        """The filled program of one call as a runnable script (program.fs of the run)."""
        lines = self.program_lines(tool.code, lambda name: (name in arguments, arguments.get(name)),
                                   Memory(self.types), literal=True)
        return f"\\ MCP {tool.name} — filled template {tool.file}\n" + "\n".join(lines) + "\n"

    async def run_tool(self, tool: Tool, arguments: dict[str, Any], session: Session | None = None) -> str:
        """One MCP call: the template of the tool, filled with the values of the call, checked and
        run in a fresh instance; the answer is the top of the stack."""
        run = self.new_run("tool", tool.name, {"args": arguments, "session": session.id if session else None})
        started = time.perf_counter()
        try:
            run.say(f"{tool.name}: template {tool.file}")
            self.check_arguments(tool, arguments)
            program = self.tool_program(tool, arguments)
            run.write("program.fs", program)
            run.say("program:", *("    " + line for line in program.splitlines()[1:]))
            run.say("check: ok — " + ", ".join(f"{a.name}:{a.type}" for a in tool.args)
                    + " -- Response (stack effects checked at load time)")
            instance = Instance(self, "req", run, ALL_CAPS, session)
            frame = Frame(values=arguments)
            if run.stepping:
                run.say("  0. start: parameters filled in",
                        "     ▸ " + " ".join(self.program_lines(tool.code, frame.lookup, run.memory)), level="steps")
            stack = await instance.run_code(tool.code, frame)
        except BaseException as exc:
            run.say(f"ERROR: {type(exc).__name__}: {exc}")
            run.finish(error=exc)
            raise
        response = str(stack[-1])
        run.say(f"done in {(time.perf_counter() - started) * 1000:.0f} ms: answer of {len(response)} characters")
        run.finish(response=response)
        return response

    async def run_script(self, text: str, file: str, kind: str = "script",
                         caps: frozenset[str] | None = ALL_CAPS) -> list[Any]:
        code = self.compile_script(text, file)
        run = self.new_run(kind, Path(file).stem)
        instance = Instance(self, kind, run, caps)
        run.say(f"script {file}:", *("    " + line for line in self.program_lines(code, Frame().lookup, run.memory)))
        try:
            return await instance.run_code(code)
        except BaseException as exc:
            if kind == "boot" and not isinstance(exc, Exit):
                print(describe_error(exc), flush=True)
            raise
        finally:
            run.finish()

    def boot(self, script: str = "boot.fs") -> int:
        """Run the start script (prepare the corpus, start the search, serve MCP); exit code."""
        path = self.scripts_dir / script
        try:
            asyncio.run(self.run_script(path.read_text(encoding="utf-8"), self._rel(path), kind="boot"))
        except Exit as exc:
            return exc.code
        return 0

    # ----- tasks between instances -----
    def spawn(self, parent: Instance, ref: WordRef) -> Task:
        instance = Instance(self, ref.name.replace("/", "_"), parent.run, parent.caps, parent.session)
        future = asyncio.ensure_future(instance.call_word(self.word(ref.name)))
        return Task(instance.name, future)


def run_sync(factory: Callable[[], Any]) -> Any:
    """Run a coroutine to completion from synchronous code, also when this thread already
    has a running event loop (then in a helper thread with its own loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()
