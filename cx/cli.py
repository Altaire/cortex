"""Command line of Cortex.

    python cortex.py < script              run (the same: python cortex.py run < script)
    python cortex.py plan < script         forecast: what a run would do; nothing is written
    python cortex.py status                the state of every script, without a run
    python cortex.py outline FILE…         the map of a document
    python cortex.py reset SCRIPT RESULT   an agent's job starts over (launch, attempts from zero)
    python cortex.py help [WORD]           this help; a word: its stack comment and parameters
--root DIR — the workspace (default: the current folder).
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import TextIO

from cx.errors import CortexError, LineError
from cx.store import Store

COMMANDS = ("run", "plan", "status", "outline", "reset", "help")

HELP = """\
Cortex executes scripts that build and edit Markdown documents; some steps are done by agents.
Cortex never starts agents itself: it does everything it has the data for and prints the rest
of the script with a summary — which agents to start, what waits, where a decision is needed.

Commands
  python cortex.py < file                 run; the output is the rest of the script (also in .cortex/scripts/)
  python cortex.py plan < file            forecast: nothing is executed or written
  python cortex.py status                 the state of every script, without a run
  python cortex.py outline doc.md         the map of a document: headings, words, warnings
  python cortex.py reset SCRIPT RESULT    restart an agent's job (launch, attempts from zero)
  python cortex.py help [word]            this help, or one word
  --root DIR                              the workspace (default: the current folder)

Stream
  script chapters                          the script name (if missing, Cortex picks one and adds it)
  "doc.md" alias maindoc1                  a name for an existing file
  "new.md" create book                     the same; the file is created empty if missing
  set attempts=3 timeout=30m autocommit=off
  === task1 === … === / ===                a text block; the block "context" goes into every agent's instructions
  \\ comment                                summary lines (\\ run, \\ done: …) are ignored on input

A line: objects, words, parameters
  task1  text0  maindoc1                   names of blocks, results, documents
  "text"                                   text; inside an agent's input list — a file path
  maindoc1"# Chapter 5..."                 a section of a document, block or result
  "# Chapter 5..." / "# Chapter 5. Methods"  heading start (up to a word boundary) / exact heading
  "# Chapter 5... > ## Summary"            a path: a subsection inside a section
  [task2, task1.out, maindoc1"# …"]        a list: the task first, then the agent's inputs
  {task2.out head="# …" has=["# … > ## …"]}  what the agent's result must be (only after agenttask)
  START  END                               the beginning and the end of a document (after=START, before=END)

Rules
  the stack lives inside a line and is empty at its end; a line may end with agenttask — the result
  keeps its name; at most one write per line, and it is the last word; a line is done entirely or
  stays in the script with a status.

Summary statuses
  done — done; launch — start an agent: "Follow the instructions in <file>";
  running — the agent is working; waiting — waits for another line or result;
  decide — the coordinator must decide; message — a message from an agent; note — for information.

Words
"""


def _help(vm, word: str | None) -> str:
    if word is not None:
        w = vm.words.get(word)
        if w is None:
            return f"no word {word}; words: {', '.join(sorted(vm.words))}\n"
        params = " ".join(p + "=" for p in w.params if p != "spec") or ("{…}" if "spec" in w.params else "—")
        doc = "\n  ".join(w.doc.splitlines())
        return f"{w.name} {w.sig}\n  parameters: {params}\n  rights: {', '.join(sorted(w.caps))}\n  {doc}\n"
    rows = []
    for name in ("insert", "replace", "delete", "move", "save", "join", "relevel", "agenttask"):
        w = vm.words.get(name)
        if w is None:
            continue
        params = " ".join(p + "=" for p in w.params if p != "spec")
        if "spec" in w.params:
            params = "{…}"
        first = w.doc.splitlines()[0] if w.doc else ""
        rows.append(f"  {name:<10} {params:<26} {first}")
    return HELP + "\n".join(rows) + "\n"


def _status(root: Path) -> str:
    from cx.frontend import CompileErrors, read_declarations
    from cx.stream import split_stream
    folder = root / ".cortex" / "scripts"
    files = sorted(folder.glob("*.cortex")) if folder.is_dir() else []
    if not files:
        return "no scripts (.cortex/scripts is empty)\n"
    out: list[str] = []
    now = time.time()
    for path in files:
        name = path.stem
        text = path.read_text(encoding="utf-8")
        try:
            actions = len(read_declarations(split_stream(text)).actions)
        except (CortexError, CompileErrors) as exc:
            out.append(f"{name:<18} ? the file cannot be parsed: {exc}")
            continue
        last: dict[str, int] = {}
        for kind in re.findall(r"^\\\s*(launch|running|waiting|decide):", text, re.MULTILINE):
            last[kind] = last.get(kind, 0) + 1
        store = Store(root, name)
        state = store.load_state()
        ready, running = [], []
        for result, job in sorted(state["jobs"].items()):
            if job.get("done"):
                continue
            if store.message(result).exists():
                ready.append(result)
            elif store.status(result).exists():
                minutes = int((now - store.status(result).stat().st_mtime) // 60)
                running.append(f"{result} ({minutes} min)")
        state_word = "done" if actions == 0 else "active"
        parts = [f"{name:<18} {state_word:<7} run {state['run']}, {actions} lines"]
        if last:
            parts.append("last run: " + ", ".join(f"{k} {v}" for k, v in last.items()))
        if running:
            parts.append("working: " + ", ".join(running))
        if ready:
            parts.append("ready for a run: " + ", ".join(ready))
        out.append(" · ".join(parts))
    return "\n".join(out) + "\n"


def main(argv: list[str], stdin: TextIO | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    out = out or sys.stdout
    err = err or sys.stderr
    for stream in (out, err):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = list(argv)
    root = Path.cwd()
    if "--root" in args:
        k = args.index("--root")
        if k + 1 >= len(args):
            print("cortex: --root DIR", file=err)
            return 2
        root = Path(args[k + 1])
        del args[k:k + 2]
    if args and args[0] not in COMMANDS:
        print(f"cortex: unknown command {args[0]} (known: {', '.join(COMMANDS)})", file=err)
        return 2
    command = args.pop(0) if args else "run"
    if command in ("run", "plan"):
        if args:
            print(f"cortex: {command} reads the script from STDIN: python cortex.py {command} < file", file=err)
            return 2
        if stdin.isatty():
            print("cortex: give the script on STDIN: python cortex.py < file (help: python cortex.py help)", file=err)
            return 2
        data = stdin.buffer.read().decode("utf-8") if hasattr(stdin, "buffer") else stdin.read()
        from cx.runner import execute
        return execute(data, root, command, out, err)
    if command == "status":
        out.write(_status(root))
        return 0
    if command == "outline":
        if not args:
            print("cortex: outline FILE…", file=err)
            return 2
        from mdlib import outline
        for name in args:
            path = root / name
            if not path.exists():
                print(f"cortex: no file {name}", file=err)
                return 1
            out.write(outline(path.read_text(encoding="utf-8"), name) + "\n")
        return 0
    if command == "reset":
        if len(args) != 2:
            print("cortex: reset SCRIPT RESULT", file=err)
            return 2
        from cx.agents import reset
        store = Store(root, args[0])
        try:
            with store.lock():
                state = store.load_state()
                message = reset(store, state, args[1])
                store.save_state(state)
        except (CortexError, LineError) as exc:
            print(f"cortex: {exc}", file=err)
            return 1
        out.write(message + "\n")
        return 0
    from cx.env import Env
    from cx.runner import make_vm
    out.write(_help(make_vm(Env()), args[0] if args else None))
    return 0
