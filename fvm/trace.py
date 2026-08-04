"""Transparency: console, run records, traces, execution memory.

Every run (an MCP call, the start script) has
    console      [MCP <run>] lines: the template file, the filled program, the check, every
                 step as "stack ▸ the rest of the program", the result (FVM_CONSOLE)
    memory       values too big to print, by address #n:Type (fvm/render.py)
and, for an MCP call, a folder results/runs/<date>/<run-id>/:
    request.json   tool and arguments
    program.fs     the filled program (python -m fvm script … runs it again)
    program.log    the console lines of the run: the program shrinking step by step
    trace.jsonl    one line per word call: where in the .fs, inputs, outputs (as addresses), time
    memory.jsonl   the values behind the addresses
    response.md    the answer   (or error.txt with the chain of Forth words)
The start script writes its trace to results/trace/boot-<time>.jsonl.

FVM_CONSOLE: off | calls (template, program, check, result) | steps (default: + every step)
             | log (every step into program.log only, nothing printed: Cortex keeps STDOUT clean)
FVM_TRACE:   off | calls (default)          FVM_RUNS=0: no run folders for MCP calls
FVM_MEMORY_VALUE_KB=256: bigger values go to memory.jsonl only as the beginning of their JSON
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any

_counter = itertools.count(1)
_print_lock = threading.Lock()   # lines of parallel calls never mix inside one line


def _default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return repr(value)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_default)


class Run:
    """One execution: its console lines, trace, memory and (MCP calls) its folder."""

    def __init__(self, out_dir: Path, kind: str, name: str, trace_level: str, console: str, save: bool,
                 memory: Any, request: dict[str, Any] | None = None, run_dir: Path | None = None) -> None:
        self.kind = kind
        self.name = name
        self.trace_level = trace_level
        self.console = console
        self.save = save
        self.memory = memory
        now = time.time()
        stamp = time.strftime("%H%M%S", time.localtime(now))
        number = next(_counter)
        self.short = f"{stamp}-{number:04d}"
        self.id = f"{stamp}-{int(now * 1000) % 1000:03d}-{number:04d}-{name.replace('/', '_')}"
        self.prefix = f"[{kind.upper()} {self.short}]"
        self._steps = itertools.count(1)
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        self.log: list[str] = []
        self._stream = None
        self.dir: Path | None = None
        if run_dir is not None:              # the caller chose the folder (Cortex: .cortex/history/…)
            self.dir = Path(run_dir)
            self.dir.mkdir(parents=True, exist_ok=True)
        elif save and kind == "boot":
            folder = out_dir / "trace"
            folder.mkdir(parents=True, exist_ok=True)
            self._stream = open(folder / f"boot-{time.strftime('%Y%m%d-%H%M%S')}.jsonl", "a", encoding="utf-8")
        elif save:
            self.dir = out_dir / "runs" / time.strftime("%Y-%m-%d") / self.id
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "request.json").write_text(
                json.dumps({"kind": kind, "name": name, "run": self.id, **(request or {})},
                           ensure_ascii=False, indent=1, default=_default) + "\n", encoding="utf-8")

    @property
    def tracing(self) -> bool:
        return self.trace_level != "off" and self.save

    @property
    def stepping(self) -> bool:
        """Print / log every step?"""
        return self.console in ("steps", "log") or (self.dir is not None and self.console != "off")

    def next_step(self) -> int:
        return next(self._steps)

    def ref(self, value: Any) -> Any:
        """A value in the trace: small ones as they are, others as their memory address."""
        from fvm.render import is_small
        return value if is_small(value) else self.memory.label(value)

    def say(self, *lines: str, level: str = "calls") -> None:
        """Console lines of this run (and into program.log)."""
        with self._lock:
            self.log.extend(lines)
        if self.console in ("off", "log") or (level == "steps" and self.console != "steps"):
            return
        with _print_lock:
            for line in lines:
                print(f"{self.prefix} {line}", flush=True)

    def record(self, entry: dict[str, Any]) -> None:
        if not self.tracing:
            return
        entry = {"run": self.id, **entry}
        with self._lock:
            if self._stream is not None:
                self._stream.write(json.dumps(entry, ensure_ascii=False, default=_default) + "\n")
                self._stream.flush()
            else:
                self.records.append(entry)

    def write(self, name: str, text: str) -> None:
        if self.dir is not None:
            (self.dir / name).write_text(text, encoding="utf-8")

    def finish(self, response: str | None = None, error: BaseException | None = None) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self.dir is None:
            return
        if self.records:
            with open(self.dir / "trace.jsonl", "w", encoding="utf-8") as handle:
                for entry in sorted(self.records, key=lambda e: e["step"]):
                    handle.write(json.dumps(entry, ensure_ascii=False, default=_default) + "\n")
        if self.log:
            self.write("program.log", "\n".join(self.log) + "\n")
        rows = self.memory.dump()
        if rows:
            self.write("memory.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        if response is not None:
            self.write("response.md", response)
        if error is not None:
            self.write("error.txt", describe_error(error))


def describe_error(error: BaseException) -> str:
    """The error, the chain of Forth words it went through, and the Python traceback."""
    lines = [f"{type(error).__name__}: {error}", ""]
    chain = getattr(error, "__fvm_stack__", [])
    if chain:
        lines.append("Forth words (innermost first):")
        lines.extend(f"  {frame}" for frame in chain)
        lines.append("")
    lines.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))
    return "\n".join(lines)


def load_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render_tree(records: list[dict[str, Any]]) -> str:
    """A trace as an indented tree: word, time, inputs → outputs."""
    lines = []
    for entry in sorted(records, key=lambda e: (e.get("vm", ""), e["step"])):
        ins = ", ".join(str(v) for v in entry.get("in", []))
        outs = ", ".join(str(v) for v in entry.get("out", []))
        status = f"ERROR {entry['error']}" if entry.get("error") else f"{ins} → {outs}".strip()
        lines.append(f"{'  ' * entry['depth']}{entry['word']:<{max(1, 40 - 2 * entry['depth'])}} "
                     f"{entry['ms']:>8.1f} ms  {status}   [{entry.get('vm', '')} {entry.get('src', '')}]")
    return "\n".join(lines)
