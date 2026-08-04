"""agenttask: the state of an agent's job, its instruction, the check of its result.

A job is named by its result R; its files are in .cortex/work/<script>/:
    prompts/R.md      the instruction (written by Cortex)
    status/R          the agent creates it first; its time is the start of the job
    results/R.md      the result
    messages/R.md     written last; the first line is STATUS: done or STATUS: failed
States:
    no status file                          → launch (the coordinator starts an agent)
    status, no message, within the timeout  → running
    status, no message, after the timeout   → decide (check the agent or reset the job)
    STATUS: done and the result passes      → the result goes on along the line
    STATUS: failed or the result fails      → the attempt goes to attempts/R/N/ with the reason;
                                              launch again (attempt N+1 of M) or decide
    the task text or an input changed       → the old files go to attempts/, launch again
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cx.env import digest
from cx.errors import LineError, Suspend
from cx.values import BlockRef, DocRef, FileRef, ResultRef, ResultSpec, SectionRef, Src, TextValue
from mdlib import MdError, chunk_issues, from_lines, locate

_STATUS = re.compile(r"^\s*STATUS:\s*(done|failed)\b", re.IGNORECASE)


@dataclass
class Prepared:
    path: str          # relative to the workspace: what the agent reads
    desc: str
    text: str
    target: Path | None = None   # a file Cortex writes for the agent (sections, texts)


def _prepare(env: Any, result: str, k: int, src: Src) -> Prepared:
    store = env.store
    text = env.text_of(src)                  # waits / fails here if the input is not ready
    if isinstance(src, BlockRef):
        return Prepared(store.rel(store.block(src.name)), f"block {src.name}", text)
    if isinstance(src, ResultRef):
        return Prepared(store.rel(store.result(src.name)), f"result {src.name}", text)
    if isinstance(src, DocRef):
        return Prepared(src.path, f"the whole document {src.name}", text)
    if isinstance(src, FileRef):
        return Prepared(src.path, "file", text)
    target = store.inputs(result) / f"{k}.md"
    desc = f"section \"{src.path}\" of {src.base.label()}" if isinstance(src, SectionRef) else "text"
    return Prepared(store.rel(target), desc, text, target)


def _message(text: str) -> tuple[str | None, str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            match = _STATUS.match(line)
            return (match.group(1).lower() if match else None), "\n".join(lines[i + 1:]).strip()
    return None, ""


def _short(text: str, limit: int = 200) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def archive(env: Any, result: str, job: dict[str, Any], reason: str) -> bool:
    """Move the files of the current attempt to attempts/R/N/ (with reason.txt)."""
    store = env.store
    files = [p for p in (store.status(result), store.result(result), store.message(result)) if p.exists()]
    if not files:
        return False
    number = job.get("archived", 0) + 1
    job["archived"] = number
    if not store.dry:
        folder = store.attempts(result) / str(number)
        folder.mkdir(parents=True, exist_ok=True)
        for path in files:
            path.replace(folder / path.name)
        (folder / "reason.txt").write_text(reason + "\n", encoding="utf-8")
    return True


def _head_text(spec: ResultSpec) -> str:
    head = spec.head
    level = "#" * head.level
    if head.prefix:
        return (f"a `{level}` heading whose text starts with \"{head.text}\" "
                f"(for example: `{level} {head.text} …`)")
    return f"the heading `{level} {head.text}`"


def build_prompt(env: Any, result: str, task_text: str, inputs: list[Prepared], spec: ResultSpec,
                 reason: str | None) -> str:
    store = env.store
    out = [f"# Agent job: {result}", "",
           f"Workspace: `{store.root}`. All paths below are relative to it. Change only the files of steps 1, 3 and 4.", "",
           "## Shared context", "", env.context_text.strip() or "(none)", "",
           "## Task", "", task_text.strip(), "",
           "## Inputs", ""]
    out += [f"- `{p.path}` — {p.desc}" for p in inputs] or ["No inputs."]
    out += ["", "## Steps", "",
            f"1. First of all, create the file `{store.rel(store.status(result))}` "
            "(any content, for example the start time).",
            "2. Read the inputs and do the task.",
            f"3. Write the result to `{store.rel(store.result(result))}` — only the text for the document, "
            "nothing addressed to the coordinator. Requirements:"]
    if spec.head is not None:
        out.append(f"   - it starts with {_head_text(spec)}; this is the only "
                   f"`{'#' * spec.head.level}` heading in the result;")
    if spec.has:
        out.append("   - it contains the sections: " + ", ".join(f"`{p}`" for p in spec.has) + ";")
    out += ["   - heading levels do not skip: inside `#` comes `##`, inside `##` comes `###`;",
            "   - no lines like `=== … ===` and no `@@@`.",
            f"4. As the very last step, write a message to the coordinator to `{store.rel(store.message(result))}`:",
            "   the first line is `STATUS: done` or `STATUS: failed`;",
            "   then: what was done, what problems there were, what you suggest changing in the plan.",
            "5. Do not change any other files: only Cortex edits the documents."]
    if reason:
        out += ["", "## The previous attempt was rejected", "", reason]
    return "\n".join(out) + "\n"


def _issue(env: Any, result: str, job: dict[str, Any], task_text: str, inputs: list[Prepared],
           spec: ResultSpec) -> None:
    """Write the inputs and the instruction; remember the start of the replaced section."""
    store = env.store
    if not store.dry:
        for folder in (store.status(result).parent, store.result(result).parent, store.message(result).parent):
            folder.mkdir(parents=True, exist_ok=True)
    for p in inputs:
        if p.target is not None:
            store.write(p.target, p.text)
    store.write(store.prompt(result), build_prompt(env, result, task_text, inputs, spec, job.get("last_failure")))
    job["issued"] = env.now
    job["context"] = env.context_hash      # the version of the context the agent gets
    write = env.line.write
    job["base"] = None
    if write is not None and write.word == "replace" and write.doc is not None and write.target is not None:
        try:
            lines = env.doc_lines(write.doc)
            head = locate(from_lines(lines), write.target)
            job["base"] = digest(from_lines(lines[head.line:head.end]))
        except (MdError, LineError):
            pass


def _launch(env: Any, result: str, job: dict[str, Any], task_text: str, inputs: list[Prepared],
            spec: ResultSpec, why: str | None = None) -> Suspend:
    attempts = env.settings["attempts"]
    if job["failures"] >= attempts:
        return Suspend("decide", result, f"failed {job['failures']} of {attempts}: {job.get('last_failure')} — "
                                         f"fix the task or run: python cortex.py reset {env.store.script} {result}")
    _issue(env, result, job, task_text, inputs, spec)
    detail = f"→ {env.store.rel(env.store.prompt(result))}"
    if job["failures"]:
        detail += f" (attempt {job['failures'] + 1} of {attempts}: {_short(job.get('last_failure') or '', 120)})"
    elif why:
        detail += f" ({why})"
    return Suspend("launch", result, detail)


def run_agent(env: Any, job_value: Any, spec: ResultSpec) -> TextValue:
    if isinstance(job_value, list):
        if not job_value:
            raise LineError("agenttask: empty list")
        task, inputs = job_value[0], job_value[1:]
    else:
        task, inputs = job_value, []
    result = spec.name
    store = env.store
    task_text = env.text_of(task)
    prepared = [_prepare(env, result, k, src) for k, src in enumerate(inputs, 1)]
    version = digest(task_text, *(p.text for p in prepared), str(spec.head), *(str(p) for p in spec.has))
    job = env.state["jobs"].setdefault(result, {"failures": 0, "archived": 0})
    job.setdefault("failures", 0)
    why = None
    if job.get("hash") not in (None, version):
        if archive(env, result, job, "the task or an input changed"):
            why = f"the task or an input changed; the previous attempt is in {store.rel(store.attempts(result))}/{job['archived']}"
        else:
            why = "the task or an input changed"
        job.update(failures=0, done=False, last_failure=None, base=None, reported=False)
    job["hash"] = version

    message = store.read(store.message(result))
    if message is not None:
        status, body = _message(message)
        if status == "done":
            text = store.read(store.result(result))
            problems = ([f"no result file {store.rel(store.result(result))}"] if text is None
                        else chunk_issues(text, spec.head, list(spec.has)))
            if not problems:
                if not job.get("done"):
                    job["done"] = True
                    job.setdefault("context", env.context_hash)
                if body and not job.get("reported"):
                    env.summary.add("message", f"{result} → {store.rel(store.message(result))}")
                    job["reported"] = True
                return TextValue(text, result)
            reason = "; ".join(problems)
        elif status == "failed":
            reason = "the agent reported a failure: " + _short(body or "no details")
        else:
            reason = "the message has no first line STATUS: done or STATUS: failed"
        archive(env, result, job, reason)
        job["failures"] += 1
        job["last_failure"] = reason
        job["done"] = False
        raise _launch(env, result, job, task_text, prepared, spec)

    status_path = store.status(result)
    if status_path.exists():
        elapsed = env.now - status_path.stat().st_mtime
        minutes = max(0, int(elapsed // 60))
        if elapsed <= env.settings["timeout"]:
            raise Suspend("running", result, f"{minutes} min")
        raise Suspend("decide", result, f"started {minutes} min ago, no message — check the agent or "
                                        f"restart: python cortex.py reset {store.script} {result}")
    raise _launch(env, result, job, task_text, prepared, spec, why)


def reset(env_store: Any, state: dict[str, Any], result: str) -> str:
    """python cortex.py reset SCRIPT RESULT — the job starts over: launch, attempts from zero."""
    job = state["jobs"].get(result)
    if job is None:
        raise LineError(f"script {env_store.script} has no job {result}")

    class _Env:
        store = env_store
    moved = archive(_Env, result, job, "reset by the coordinator")
    job.update(failures=0, done=False, last_failure=None, base=None, reported=False, hash=None)
    return f"{result}: " + ("the attempt files were moved to attempts/, " if moved else "") + "the next run will show launch"
