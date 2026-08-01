"""Client sessions — the path for scripts sent by MCP clients (not used yet).

A session will hold words defined by one client (namespace user/<id>/), and later its own
stack between calls. Instances already carry a `session`; built-in tools run without one.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fvm.words import Word


@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    words: dict[str, Word] = field(default_factory=dict)   # user/<id>/name → Word

    @property
    def namespace(self) -> str:
        return f"user/{self.id}/"


class Sessions:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session:
        with self._lock:
            return self._sessions.setdefault(session_id, Session(session_id))
