"""Execution memory and the program as text: what the console and program.log show.

Execution is shown as rewriting of the program: at every step the state is printed as
"stack ▸ the rest of the program". A call of a colon word is replaced by its body; a local
name ({ req }), once bound, is replaced everywhere in the rest by its value. Small values are
written as literals (42, " A0008", none); others live in the memory of the run under an
address — #3 (with its type where it appears: #3:ChapterRequest) — and memory.jsonl of the
run holds them.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from fvm.words import Call, Each, If, Lit, LocalGet, LocalSet, Loop, Match, Parallel, Tick

SMALL_STR = 40       # strings up to this length are written inline
SMALL_LIST = 6       # lists of up to this many small scalars are written inline
# memory.jsonl keeps a value whole up to this size; bigger ones (e.g. full search results,
# which are saved in search_results/ anyway) only as the beginning of their JSON.
MEMORY_VALUE_BYTES = int(os.environ.get("FVM_MEMORY_VALUE_KB", "256")) * 1024
PREVIEW_CHARS = 2000


def forth_literal(value: Any) -> str:
    """A Python value as Forth source: " text", 42, true, none, [ … ]; a value with a
    forth() method writes itself (Cortex references)."""
    if hasattr(value, "forth"):
        return value.forth()
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if not value:
            return '""'
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'" {escaped}"'
    if isinstance(value, (list, tuple)):
        return "[ " + " ".join(forth_literal(v) for v in value) + " ]" if value else "[ ]"
    raise ValueError(f"значение {value!r} нельзя записать литералом Forth")


def _scalar(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and len(value) <= SMALL_STR and "\n" not in value


def is_small(value: Any) -> bool:
    """Written as a literal (not stored in memory)?"""
    if _scalar(value):
        return True
    return type(value) is list and len(value) <= SMALL_LIST and all(_scalar(v) for v in value)


class Memory:
    """Values of one run that are too big to print: address #n, type, the value itself."""

    def __init__(self, types: Any) -> None:
        self.types = types
        self.slots: list[tuple[str, str, Any]] = []
        self._by_object: dict[int, str] = {}
        self._lock = threading.Lock()

    def address(self, value: Any) -> str:
        """#n — the same object always gets the same address within the run."""
        with self._lock:
            known = self._by_object.get(id(value))
            if known is not None:
                return known
            address = f"#{len(self.slots) + 1}"
            self.slots.append((address, str(self.types.of_value(value)), value))  # keeps it alive: ids stay unique
            self._by_object[id(value)] = address
            return address

    def show(self, value: Any) -> str:
        """In the program: a literal or #n."""
        return forth_literal(value) if is_small(value) else self.address(value)

    def label(self, value: Any) -> str:
        """Where a value appears (a result, a binding): a literal or #n:Type."""
        return forth_literal(value) if is_small(value) else f"{self.address(value)}:{self.types.of_value(value)}"

    def dump(self) -> list[dict[str, Any]]:
        """Rows of memory.jsonl: address, type, size (items / characters), value (or its beginning)."""
        from fvm.trace import canonical
        rows = []
        for address, type_name, value in self.slots:
            size = len(value) if isinstance(value, (str, list, dict, tuple)) else None
            text = canonical(value)
            row: dict[str, Any] = {"address": address, "type": type_name, "size": size}
            if len(text.encode("utf-8")) <= MEMORY_VALUE_BYTES:
                row["value"] = json.loads(text)
            else:
                row.update(truncated=True, json_chars=len(text), preview=text[:PREVIEW_CHARS])
            rows.append(row)
        return rows


def render_code(code: list[Any], lookup: Any, memory: Memory) -> list[str]:
    """Instructions back to Forth tokens; bound local names → their values (literal or #address)."""
    out: list[str] = []
    for ins in code:
        kind = type(ins)
        if kind is Call:
            out.append(ins.name)
        elif kind is Lit:  # source code: always written as the literal it is
            out.append(forth_literal(ins.value))
        elif kind is LocalGet:
            found, value = lookup(ins.name)
            out.append(memory.show(value) if found else ins.name)
        elif kind is LocalSet:
            out.append("{ " + " ".join(ins.names) + " }")
        elif kind is If:
            out += ["IF", *render_code(ins.then, lookup, memory)]
            if ins.otherwise:
                out += ["ELSE", *render_code(ins.otherwise, lookup, memory)]
            out.append("THEN")
        elif kind is Match:
            out.append("MATCH")
            for branch_type, body in ins.branches:
                out += [str(branch_type), "->", *render_code(body, lookup, memory)]
            out.append("END")
        elif kind is Each:
            out += ["PARALLEL-EACH" if ins.parallel else "EACH", *render_code(ins.body, lookup, memory), "NEXT"]
        elif kind is Parallel:
            out.append("PARALLEL")
            for i, body in enumerate(ins.branches):
                if i:
                    out.append("|")
                out += render_code(body, lookup, memory)
            out.append("END")
        elif kind is Loop:
            out += ["BEGIN", *render_code(ins.body, lookup, memory), "UNTIL"]
        elif kind is Tick:
            out += ["[']", ins.name]
    return out
