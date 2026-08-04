"""run / plan: one pass over the lines of a script.

Each line runs in its own fvm instance (its own stack). A line that went through is committed
and leaves the script; a line that waits (Suspend) or failed (LineError) stays as written, with
its status in the summary. The output — declarations, the summary, the remaining lines — goes
to STDOUT and to .cortex/scripts/<script>.cortex; the input and the output of the run, the
program.log of the VM (the program shrinking step by step), the trace and the memory go to
.cortex/history/<script>/NNN/. plan does the same in memory and writes nothing.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import nullcontext
from pathlib import Path
from typing import Any, TextIO

import cx.values as values
from cx.env import Env, Summary, digest
from cx.errors import CortexError, LineError, Suspend
from cx.frontend import CompileErrors, Compiler, Program, default_script_name, read_declarations
from cx.store import Store, write_atomic
from cx.stream import split_stream
from fvm.render import Memory
from fvm.trace import Run
from fvm.vm import VM, ForthError, Instance
from mdlib import MdError

ROOT = Path(__file__).resolve().parent.parent
LINE_REF = re.compile(r"⟨(\d+)⟩")   # a line of the input; shown as its number in the output


def make_vm(env: Env) -> VM:
    vm = VM(ROOT / "cxwords", ROOT / "scripts", ROOT / ".fvm", context={"env": env},
            trace_level="calls", save_runs=False, console="off")
    vm.register_module(values)
    vm.load()
    return vm


def describe_done(line: Any) -> str:
    write = line.write
    if write is None:
        return f"{line.obj} is ready"
    if write.word == "insert":
        return f"{line.obj} → {write.doc.name} {write.side} {write.pos.describe()}"
    if write.word == "replace":
        return f"{line.obj} → {write.doc.name} replaces \"{write.target}\""
    if write.word == "delete":
        return f"{write.doc.name}: deleted \"{write.target}\""
    if write.word == "move":
        return f"{write.doc.name}: \"{write.target}\" moved {write.side} {write.pos.describe()}"
    return f"{line.obj} → \"{write.path}\""


async def run_lines(vm: VM, env: Env, program: Program, run: Run, summary: Summary, err: TextIO) -> None:
    for line in program.lines:
        env.begin_line(line)
        run.say(f"line {line.number}: {line.text.strip()}")
        completed = False
        try:
            await Instance(vm, "line", run).run_code(line.code)
            env.commit()
            completed = True
            summary.add("done", describe_done(line))
        except Suspend as stop:
            text = f"{stop.label} {stop.detail}" if stop.status == "launch" else f"{stop.label} ({stop.detail})"
            summary.add(stop.status, text)
            run.say(f"  → {stop.status}: " + LINE_REF.sub(r"\1", text))
        except (LineError, MdError, ForthError) as exc:
            summary.add("decide", f"{line.label} ({exc})")
            run.say("  → decide: " + LINE_REF.sub(r"\1", str(exc)))
        except Exception as exc:  # a bug: the line stays, the run goes on
            summary.add("decide", f"{line.label} (internal error {type(exc).__name__}: {exc})")
            traceback.print_exc(file=err)
        finally:
            env.end_line(completed)


def render(program: Program, env: Env, summary: Summary, run_number: int, dry: bool) -> str:
    out: list[str] = []
    if program.script_auto:
        out.append(f"script {program.script}")
    out += program.decl_texts
    out.append("")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if dry:
        if program.run_marker is not None:
            out.append(f"\\ run {program.run_marker}")
        out.append(f"\\ plan · {stamp} · forecast: nothing was executed or written, the lines below are as given")
        lines = program.lines
    else:
        out.append(f"\\ run {run_number} · {stamp}")
        lines = [ln for ln in program.lines if ln.number not in env.completed]
        if not lines:
            out.append("\\ status: done")
    notes = summary.lines()
    body: list[str] = []
    numbers: dict[int, int] = {}
    start = len(out) + len(notes) + (1 if lines or program.trailing else 0)
    for line in lines:
        body += line.comments
        numbers[line.number] = start + len(body) + 1
        body.append(line.text)
    out += [LINE_REF.sub(lambda m: str(numbers.get(int(m.group(1)), m.group(1))), note) for note in notes]
    if lines or program.trailing:
        out.append("")
    out += body + program.trailing
    return "\n".join(out).rstrip("\n") + "\n"


def autocommit(root: Path, paths: list[str], message: str, err: TextIO) -> None:
    git = shutil.which("git")
    if git is None:
        print("cortex: autocommit — git not found", file=err)
        return
    inside = subprocess.run([git, "-C", str(root), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if inside.returncode != 0:
        print("cortex: autocommit — the workspace is not in a git repository", file=err)
        return
    specs = list(dict.fromkeys(paths)) + [".cortex", ":(exclude).cortex/lock"]
    add = subprocess.run([git, "-C", str(root), "add", "--", *specs], capture_output=True, text=True)
    commit = subprocess.run([git, "-C", str(root), "commit", "-q", "-m", message], capture_output=True, text=True)
    if add.returncode or commit.returncode:
        print(f"cortex: autocommit failed: {(add.stderr or commit.stderr or commit.stdout).strip()}", file=err)
    else:
        print(f"cortex: autocommit — {message}", file=err)


def execute(text: str, root: Path, mode: str, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    dry = mode == "plan"
    try:
        stream = split_stream(text)
        decls = read_declarations(stream)
    except CompileErrors as exc:
        return _report(exc.errors, err)
    except CortexError as exc:
        return _report([exc], err)
    script = decls.script or default_script_name()
    store = Store(root, script, dry)
    commit_args: tuple[list[str], str] | None = None
    try:
        with (nullcontext() if dry else store.lock()):
            state = store.load_state()
            last = state["run"]
            if not dry and last and stream.run_marker is None:
                raise CortexError(f"script {script} has already run (run {last}); continue with "
                                  f"{store.rel(store.script_file)} — or give the script another name")
            if not dry and stream.run_marker is not None and stream.run_marker < last:
                raise CortexError(f"this is the output of run {stream.run_marker}, but the last run is {last}; "
                                  f"continue with {store.rel(store.script_file)}")
            missing = [CortexError(f"file {doc.path} not found (alias {name}); for a new file use "
                                   f"\"{doc.path}\" create {name}")
                       for name, doc in decls.aliases.items()
                       if name not in decls.creates and not (store.root / doc.path).exists()]
            env = Env()
            vm = make_vm(env)
            done = {name for name, job in state["jobs"].items() if job.get("done")}
            try:
                program = Compiler(vm, decls, stream, store.stored_blocks(), done).program(script, decls.script is None)
            except CompileErrors as exc:
                return _report(exc.errors + missing, err)
            if missing:
                return _report(missing, err)
            run_number = last + 1
            if not dry:
                for name, block in program.blocks.items():
                    if store.read_block(name) != block.text:
                        store.write(store.block(name), block.text)
                for name in sorted(program.creates):
                    path = store.root / program.aliases[name].path
                    if not path.exists():
                        write_atomic(path, "")
            summary = Summary()
            context = program.blocks["context"].text if "context" in program.blocks else store.read_block("context") or ""
            context_hash = digest(context)
            if state.get("context_hash") not in (None, context_hash):
                stale = {r: j for r, j in state["jobs"].items() if j.get("context") not in (None, context_hash)}
                done_old = sorted(r for r, j in stale.items() if j.get("done"))
                started_old = sorted(r for r, j in stale.items() if not j.get("done") and store.status(r).exists())
                parts = ([f"done with the old version: {', '.join(done_old)}"] if done_old else []) + \
                        ([f"agents work with the old version: {', '.join(started_old)}"] if started_old else [])
                if parts:
                    summary.add("note", "context changed; " + "; ".join(parts))
            state["context_hash"] = context_hash
            env.start(store=store, program=program, state=state, dry=dry, summary=summary, context_text=context)
            history = None if dry else store.history_dir(run_number)
            run = Run(store.base, "cortex", script, "calls", "log" if not dry else "off", not dry,
                      Memory(vm.types), run_dir=history)
            try:
                asyncio.run(run_lines(vm, env, program, run, summary, err))
            finally:
                run.finish()
            output = render(program, env, summary, run_number, dry)
            if not dry:
                write_atomic(store.script_file, output)
                write_atomic(history / "in.cortex", text)
                write_atomic(history / "out.cortex", output)
                state["run"] = run_number
                store.save_state(state)
                if program.settings["autocommit"] and env.changed:
                    counts = ", ".join(f"{k} {summary.count(k)}" for k in ("done", "launch", "waiting", "decide")
                                       if summary.count(k))
                    commit_args = (env.changed, f"cortex {script} run {run_number}: {counts or 'no changes'}")
    except CompileErrors as exc:
        return _report(exc.errors, err)
    except CortexError as exc:
        return _report([exc], err)
    out.write(output)
    out.flush()
    if dry:
        print("cortex: forecast — nothing was executed or written", file=err)
    else:
        print(f"cortex: saved    {store.rel(store.script_file)}", file=err)
        print(f"cortex: history  {store.rel(history)}/", file=err)
        print(f"cortex: next     python cortex.py < {store.rel(store.script_file)}", file=err)
        if commit_args is not None:
            autocommit(store.root, *commit_args, err)
    return 0


def _report(errors: list[CortexError], err: TextIO) -> int:
    for exc in errors:
        print(f"cortex: error: {exc}", file=err)
    print("cortex: nothing was executed", file=err)
    return 1
