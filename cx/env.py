"""One run of one script: what the words see.

Documents are read once and kept as working copies; a line's writes are staged and committed
only when the whole line went through: the structure of the new text is checked first (no new
skipped levels), then the patterns of the lines still to run (none may become ambiguous), then
the file is written. Dependencies between lines come from what the earlier lines that did not
complete in this run (pending) are going to do:
  • writes to one document go in the order of the lines;
  • a section is read only when no earlier pending line will create, replace, delete or move it;
  • a heading that is not there yet waits when an earlier pending line promises it (head=).
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from cx.errors import LineError, Suspend
from cx.values import BlockRef, DocRef, FilePath, FileRef, Pattern, Pos, ResultRef, SectionRef, Src, TextValue
from mdlib import (Ambiguous, HeadingPath, MdError, NotFound, capture_issue, delete_span, from_lines, headings,
                   insert_at, issue_counter, locate, move_span, replace_span, structure_issues, to_lines)


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def related(a: HeadingPath, b: HeadingPath) -> bool:
    return any(x.compatible(y) for x in a.segments for y in b.segments)


class Summary:
    ORDER = ("done", "launch", "running", "waiting", "decide", "message", "note")

    def __init__(self) -> None:
        self.items: dict[str, list[str]] = {kind: [] for kind in self.ORDER}

    def add(self, kind: str, text: str) -> None:
        self.items[kind].append(text)

    def count(self, kind: str) -> int:
        return len(self.items[kind])

    def lines(self) -> list[str]:
        return [f"\\ {kind + ':':<9} {text}" for kind in self.ORDER for text in self.items[kind]]


class Env:
    def start(self, *, store: Any, program: Any, state: dict[str, Any], dry: bool, summary: Summary,
              context_text: str) -> None:
        self.store = store
        self.program = program
        self.state = state
        self.dry = dry
        self.settings = program.settings
        self.summary = summary
        self.context_text = context_text
        self.context_hash = digest(context_text)
        self.now = time.time()
        self.docs: dict[str, list[str]] = {}
        self.changed: list[str] = []
        self.pending: list[Any] = []
        self.completed: set[int] = set()
        self.noted: set[str] = set()
        self.line: Any = None
        self.staged: list[tuple[str, Any, Any]] = []

    # ----- lines -----
    def begin_line(self, line: Any) -> None:
        self.line = line
        self.staged = []

    def end_line(self, completed: bool) -> None:
        if completed:
            self.completed.add(self.line.number)
        else:
            self.pending.append(self.line)
        self.line = None
        self.staged = []

    def wait(self, detail: str) -> Suspend:
        return Suspend("waiting", self.line.label, detail)

    # ----- dependencies on earlier pending lines -----
    def earlier_write(self, doc: str) -> Any:
        return next((ln for ln in self.pending if ln.write and ln.write.doc and ln.write.doc.name == doc), None)

    def promise(self, doc: str, path: HeadingPath) -> Any:
        for ln in self.pending:
            agent, write = ln.agent, ln.write
            if (agent and agent.spec.head and write and write.doc and write.doc.name == doc
                    and write.word in ("insert", "replace")
                    and any(seg.compatible(agent.spec.head) for seg in path.segments)):
                return ln
        return None

    def changer(self, doc: str, path: HeadingPath) -> Any:
        for ln in self.pending:
            write = ln.write
            if (write and write.doc and write.doc.name == doc and write.word in ("replace", "delete", "move")
                    and write.target is not None and related(write.target, path)):
                return ln
        return None

    # ----- texts -----
    def text_of(self, src: Src) -> str:
        if isinstance(src, TextValue):
            return src.text
        if isinstance(src, BlockRef):
            return self.block_text(src.name)
        if isinstance(src, ResultRef):
            return self.result_text(src.name)
        if isinstance(src, DocRef):
            line = self.earlier_write(src.name)
            if line is not None:
                raise self.wait(f"waits for line ⟨{line.number}⟩: a write to {src.name}")
            return from_lines(self.doc_lines(src))
        if isinstance(src, FileRef):
            text = self.store.read(self.store.root / src.path)
            if text is None:
                raise LineError(f"file {src.path} not found")
            return text
        if isinstance(src, SectionRef):
            return self.section_text(src)
        raise LineError(f"value {src!r} has no text")

    def block_text(self, name: str) -> str:
        block = self.program.blocks.get(name)
        if block is not None:
            return block.text
        text = self.store.read_block(name)
        if text is None:
            raise LineError(f"block {name} not found")
        return text

    def result_text(self, name: str) -> str:
        job = self.state["jobs"].get(name) or {}
        path = self.store.result(name)
        if job.get("done") and path.exists():
            return path.read_text(encoding="utf-8")
        producer = self.program.producers.get(name)
        if producer is not None and producer.number not in self.completed:
            raise self.wait(f"waits for {name}")
        if job.get("done"):
            raise LineError(f"result {name} is marked done, but {self.store.rel(path)} is missing")
        raise LineError(f"result {name} is not ready and no line of the script produces it")

    def section_text(self, ref: SectionRef) -> str:
        base = ref.base
        if isinstance(base, DocRef):
            changer = self.changer(base.name, ref.path)
            if changer is not None:
                raise self.wait(f"waits for line ⟨{changer.number}⟩: it changes \"{changer.write.target}\" in {base.name}")
            lines = self.doc_lines(base)
            head = self.heading(base, lines, ref.path)
            return from_lines(lines[head.line:head.end])
        text = self.text_of(base)
        try:
            head = locate(text, ref.path)
        except MdError as exc:
            raise LineError(f"{base.label()}: {exc}") from None
        return from_lines(to_lines(text)[head.line:head.end])

    # ----- documents -----
    def doc_lines(self, doc: DocRef) -> list[str]:
        if doc.name not in self.docs:
            text = self.store.read(self.store.root / doc.path)
            if text is None:
                if doc.name not in self.program.creates:
                    raise LineError(f"file {doc.path} not found (alias {doc.name})")
                text = ""
            self.docs[doc.name] = to_lines(text)
        return self.docs[doc.name]

    def heading(self, doc: DocRef, lines: list[str], path: HeadingPath):
        try:
            return locate(from_lines(lines), path)
        except NotFound as exc:
            promised = self.promise(doc.name, path)
            if promised is not None:
                raise self.wait(f"waits for section \"{path}\" from line ⟨{promised.number}⟩") from None
            raise LineError(f"{doc.name}: {exc}") from None
        except Ambiguous as exc:
            raise LineError(f"{doc.name}: {exc}") from None

    def index(self, doc: DocRef, lines: list[str], pos: Pos, side: str) -> int:
        if pos.kind == "start":
            return 0
        if pos.kind == "end":
            return len(lines)
        head = self.heading(doc, lines, pos.path)
        return head.end if side == "after" else head.line

    def in_order(self, doc: DocRef) -> None:
        line = self.earlier_write(doc.name)
        if line is not None:
            raise self.wait(f"waits for line ⟨{line.number}⟩: writes to {doc.name} go in line order")

    # ----- the words -----
    def insert(self, src: Src, doc: DocRef, pos: Pos, side: str) -> None:
        self.in_order(doc)
        text = self.text_of(src)
        lines = self.doc_lines(doc)
        index = self.index(doc, lines, pos, side)
        issue = capture_issue(lines, index, text)
        if issue:
            raise LineError(f"{doc.name}: {issue}")
        self.staged.append(("doc", doc, insert_at(lines, index, text)))

    def replace(self, src: Src, doc: DocRef, pattern: Pattern) -> None:
        self.in_order(doc)
        text = self.text_of(src)
        lines = self.doc_lines(doc)
        head = self.heading(doc, lines, pattern.path)
        agent = self.line.agent
        base = (self.state["jobs"].get(agent.result) or {}).get("base") if agent else None
        if base is not None and base != digest(from_lines(lines[head.line:head.end])):
            raise LineError(f"section \"{head}\" in {doc.name} changed after agent {agent.result} was launched: the "
                            f"replace was not done — restart the job (python cortex.py reset {self.store.script} "
                            f"{agent.result}) or decide otherwise")
        self.staged.append(("doc", doc, replace_span(lines, head.line, head.end, text)))

    def delete(self, doc: DocRef, pattern: Pattern) -> None:
        self.in_order(doc)
        lines = self.doc_lines(doc)
        head = self.heading(doc, lines, pattern.path)
        self.staged.append(("doc", doc, delete_span(lines, head.line, head.end)))

    def move(self, doc: DocRef, pattern: Pattern, pos: Pos, side: str) -> None:
        self.in_order(doc)
        lines = self.doc_lines(doc)
        head = self.heading(doc, lines, pattern.path)
        chunk = from_lines(lines[head.line:head.end])
        rest = delete_span(lines, head.line, head.end)
        try:
            index = self.index(doc, rest, pos, side)
        except LineError as exc:
            raise LineError(f"{exc} (the place is looked up without the section being moved)") from None
        issue = capture_issue(rest, index, chunk)
        if issue:
            raise LineError(f"{doc.name}: {issue}")
        self.staged.append(("doc", doc, move_span(lines, head.line, head.end, lambda _rest: index)))

    def save(self, src: Src, path: FilePath) -> None:
        self.staged.append(("file", path.path, self.text_of(src)))

    # ----- commit -----
    def commit(self) -> None:
        for kind, target, payload in self.staged:
            if kind != "doc":
                continue
            old_text, new_text = from_lines(self.doc_lines(target)), from_lines(payload)
            before, after = structure_issues(old_text), structure_issues(new_text)
            fresh = issue_counter(after) - issue_counter(before)
            if fresh:
                texts = list(dict.fromkeys(str(i) for i in after if i.key in fresh))
                raise LineError(f"structure of {target.name}: " + "; ".join(texts))
            if before and target.name not in self.noted:
                self.noted.add(target.name)
                self.summary.add("note", f"{target.name}: the document already has structure problems — "
                                         + "; ".join(str(i) for i in before[:2])
                                         + (f" and {len(before) - 2} more" if len(before) > 2 else ""))
            self._check_patterns(target, old_text, new_text)
        for kind, target, payload in self.staged:
            if kind == "doc":
                self.docs[target.name] = payload
                if not self.dry:
                    self.store.write(self.store.root / target.path, from_lines(payload))
                self.changed.append(target.path)
            else:
                if not self.dry:
                    self.store.write(self.store.root / target, payload)
                self.changed.append(target)

    def _check_patterns(self, doc: DocRef, old_text: str, new_text: str) -> None:
        """No pattern of a line still to run may become ambiguous because of this write."""
        old_heads, new_heads = headings(old_text), headings(new_text)
        for ln in self.program.lines:
            if ln is self.line or ln.number in self.completed:
                continue
            for name, path in ln.patterns:
                if name != doc.name:
                    continue
                try:
                    locate(new_heads, path)
                except Ambiguous:
                    try:
                        locate(old_heads, path)
                    except Ambiguous:
                        continue          # it was ambiguous already: that line reports it itself
                    except NotFound:
                        pass
                    raise LineError(f"after this write the pattern \"{path}\" of line ⟨{ln.number}⟩ would become ambiguous in "
                                    f"{doc.name}: make the heading or the pattern more specific") from None
                except NotFound:
                    continue
