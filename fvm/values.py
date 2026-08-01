"""Value types of the VM itself: responses, tasks, channels."""
from __future__ import annotations

import asyncio
from typing import Any


class Response(str):
    """The text answer of a tool."""


class Task:
    """A background instance started by spawn; join waits for its result."""

    def __init__(self, name: str, future: "asyncio.Future[Any]") -> None:
        self.name = name
        self.future = future

    def __repr__(self) -> str:
        return f"Task({self.name})"


class Chan:
    """A channel between instances of one event loop: send / receive."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    def __repr__(self) -> str:
        return f"Chan({self.queue.qsize()})"
