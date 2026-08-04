"""The .cortex folder of a workspace.

    .cortex/
      scripts/<script>.cortex     the current version of a script (what its last run printed)
      history/<script>/NNN/       in.cortex, out.cortex, program.log, trace.jsonl, memory.jsonl
      work/<script>/
        blocks/<name>.md          texts of the blocks of the stream
        prompts/<result>.md       instructions for agents
        inputs/<result>/<k>.md    sections and texts given to an agent as inputs
        results/<result>.md       agents' results
        status/<result>           start marks (the start time is the file's time)
        messages/<result>.md      agents' messages to the coordinator
        attempts/<result>/<N>/    rejected or outdated attempts, with reason.txt
        state.json                the last run number, the jobs, the version of the context
      lock                        one run at a time
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cx.errors import CortexError


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Store:
    def __init__(self, root: Path, script: str, dry: bool = False) -> None:
        self.root = Path(root).resolve()
        self.base = self.root / ".cortex"
        self.script = script
        self.work = self.base / "work" / script
        self.dry = dry

    # ----- paths -----
    def rel(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    @property
    def script_file(self) -> Path:
        return self.base / "scripts" / f"{self.script}.cortex"

    def history_dir(self, run: int) -> Path:
        return self.base / "history" / self.script / f"{run:03d}"

    def block(self, name: str) -> Path:
        return self.work / "blocks" / f"{name}.md"

    def prompt(self, result: str) -> Path:
        return self.work / "prompts" / f"{result}.md"

    def inputs(self, result: str) -> Path:
        return self.work / "inputs" / result

    def result(self, result: str) -> Path:
        return self.work / "results" / f"{result}.md"

    def status(self, result: str) -> Path:
        return self.work / "status" / result

    def message(self, result: str) -> Path:
        return self.work / "messages" / f"{result}.md"

    def attempts(self, result: str) -> Path:
        return self.work / "attempts" / result

    # ----- files -----
    def write(self, path: Path, text: str) -> None:
        if not self.dry:
            write_atomic(path, text)

    @staticmethod
    def read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def stored_blocks(self) -> set[str]:
        folder = self.work / "blocks"
        return {p.stem for p in folder.glob("*.md")} if folder.is_dir() else set()

    def read_block(self, name: str) -> str | None:
        return self.read(self.block(name))

    # ----- state -----
    @property
    def state_file(self) -> Path:
        return self.work / "state.json"

    def load_state(self) -> dict[str, Any]:
        text = self.read(self.state_file)
        if text is None:
            return {"script": self.script, "run": 0, "jobs": {}, "context_hash": None}
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CortexError(f"{self.rel(self.state_file)} is corrupted: {exc}") from None
        state.setdefault("jobs", {})
        state.setdefault("run", 0)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.write(self.state_file, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    # ----- lock -----
    @contextmanager
    def lock(self) -> Iterator[None]:
        path = self.base / "lock"
        self.base.mkdir(parents=True, exist_ok=True)
        fd = None
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                info = (self.read(path) or "").split()
                pid = int(info[0]) if info and info[0].isdigit() else None
                if pid is not None and pid != os.getpid() and _alive(pid):
                    raise CortexError(f"another Cortex run is in progress (pid {pid}); if not, delete {self.rel(path)}")
                path.unlink(missing_ok=True)
        if fd is None:
            raise CortexError(f"could not take the lock {self.rel(path)}")
        os.write(fd, f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')} {self.script}\n".encode())
        os.close(fd)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
