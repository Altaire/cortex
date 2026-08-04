"""Three kinds of trouble.

CortexError — the stream or a line cannot be compiled: nothing runs, STDERR names the line.
LineError   — one line cannot be done now (a heading is missing, the structure would break …):
              the line stays in the script with the status decide, the others go on.
Suspend     — a line waits for something (an agent, another line): it stays with its status.
"""
from __future__ import annotations


class CortexError(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}" if line else message)


class LineError(Exception):
    """A run-time problem of one line; the message goes to the summary (decide)."""


class Suspend(Exception):
    """The line cannot go on in this run.

    status: launch | running | waiting | decide;  label: what the summary line is about
    (an agent's result name or the line);  detail: the rest of the summary line."""

    def __init__(self, status: str, label: str, detail: str = "") -> None:
        self.status = status
        self.label = label
        self.detail = detail
        super().__init__(f"{status}: {label} {detail}".strip())
