"""Declarations, the table of names, and Cortex lines → fvm code.

A line is sugar over the stack VM: objects (names, "text", name"pattern", [lists]) become
literals pushed onto the stack; a word takes its key=value parameters — pushed in the order of
its stack comment — and is called. Checked here, before anything runs:
  • every name is known (a block, a result, an alias); a misspelt word gets a suggestion,
    a word with a Cyrillic look-alike letter is named as such;
  • the stack is empty at the end of a line, or the line ends with agenttask (its result keeps
    its name for later lines);
  • at most one write per line (a word with the right "write"), and it is the last word;
  • at most one agenttask per line, its object written right before it (a name or a list).
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from cx.errors import CortexError
from cx.lexer import Tok, lex
from cx.stream import Block, SourceLine, Stream
from cx.values import (BlockRef, DocRef, FilePath, FileRef, Pattern, Pos, ResultRef, ResultSpec, SectionRef, Src,
                       TextValue)
from fvm.checker import Checker, StackEffectError, picture
from fvm.types import NONE
from fvm.words import Call, Lit
from mdlib import HeadingPath, MdError, parse_path

DECL_WORDS = ("script", "alias", "create", "set")
RESERVED = ("START", "END")
DEFAULTS: dict[str, Any] = {"attempts": 3, "timeout": 30 * 60, "autocommit": False}
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_DURATION = re.compile(r"^(\d+)(s|m|h)?$")
_CONFUSABLE = str.maketrans("аеорсухкАЕОРСУХКМТВН", "aeopcyxkAEOPCYXKMTBH")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@dataclass
class AgentStep:
    result: str
    task: Src
    inputs: list[Src]
    spec: ResultSpec


@dataclass
class WriteStep:
    word: str                         # insert | replace | delete | move | save
    doc: DocRef | None = None         # to= of a document
    target: HeadingPath | None = None # section=
    pos: Pos | None = None            # after= / before=
    side: str | None = None           # after | before
    path: str | None = None           # save to="…"


@dataclass
class Line:
    number: int
    text: str
    comments: list[str]
    code: list[Any]
    agent: AgentStep | None
    write: WriteStep | None
    label: str
    obj: str                          # what the line works on, for the summary
    patterns: list[tuple[str, HeadingPath]] = field(default_factory=list)  # (document, pattern) it names


@dataclass
class Program:
    script: str
    script_auto: bool
    decl_texts: list[str]             # declaration lines as written (with their comments)
    aliases: dict[str, DocRef]
    creates: set[str]
    settings: dict[str, Any]
    blocks: dict[str, Block]          # blocks of this stream
    lines: list[Line]
    trailing: list[str]               # comments after the last line
    run_marker: int | None
    producers: dict[str, Line] = field(default_factory=dict)


def check_rel_path(path: str, line: int | None) -> str:
    """A path inside the workspace, written with / ; not into .cortex."""
    if not path or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
        raise CortexError(f"путь «{path}» должен быть относительным (от рабочей папки)", line)
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if ".." in parts:
        raise CortexError(f"путь «{path}» выходит за рабочую папку", line)
    if parts and parts[0] == ".cortex":
        raise CortexError(f"путь «{path}» ведёт в .cortex — туда пишет только Cortex", line)
    return PurePosixPath(*parts).as_posix()


def parse_duration(text: str, line: int | None) -> int:
    match = _DURATION.match(text)
    if not match:
        raise CortexError(f"длительность «{text}»: число и s, m или h (30m, 2h, 90s)", line)
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2) or "m"]


# ----------------------------------------------------------------------------------------
# declarations
# ----------------------------------------------------------------------------------------
@dataclass
class Declarations:
    script: str | None = None
    texts: list[str] = field(default_factory=list)
    aliases: dict[str, DocRef] = field(default_factory=dict)
    creates: set[str] = field(default_factory=set)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    actions: list[tuple[SourceLine, list[Tok], list[str]]] = field(default_factory=list)
    trailing: list[str] = field(default_factory=list)


def read_declarations(stream: Stream) -> Declarations:
    """Sort the code lines: declarations (script, alias, create, set) and action lines; comment
    lines go with the line under them."""
    decls = Declarations()
    comments: list[str] = []
    errors: list[CortexError] = []
    for sl in stream.lines:
        if sl.kind == "comment":
            comments.append(sl.text)
            continue
        if sl.kind != "code":
            continue
        try:
            toks = lex(sl.text, sl.number)
            if not toks:
                comments.append(sl.text)
                continue
            if _declaration(decls, toks, sl.number):
                decls.texts += comments + [sl.text]
            else:
                decls.actions.append((sl, toks, comments))
        except CortexError as exc:
            errors.append(exc)
        comments = []
    decls.trailing = comments
    if errors:
        raise CompileErrors(errors)
    return decls


def _name_tok(tok: Tok, what: str, line: int) -> str:
    if tok.kind != "name" or not NAME_RE.match(tok.value):
        raise CortexError(f"{what}: ожидалось имя (латинские буквы, цифры, _ . -)", line)
    return tok.value


def _declaration(decls: Declarations, toks: list[Tok], line: int) -> bool:
    words = [t.value if t.kind == "name" else None for t in toks]
    if words[0] == "script":
        if len(toks) != 2:
            raise CortexError("объявление: script имя", line)
        if decls.script is not None:
            raise CortexError(f"скрипт уже назван: {decls.script}", line)
        decls.script = _name_tok(toks[1], "script", line)
        return True
    if "alias" in words or "create" in words:
        kind = "alias" if "alias" in words else "create"
        rest = [t for t in toks if not (t.kind == "name" and t.value == kind)]
        if len(toks) != 3 or len(rest) != 2 or rest[0].kind != "str":
            raise CortexError(f"объявление: \"путь\" {kind} имя", line)
        path = check_rel_path(rest[0].value, line)
        name = _name_tok(rest[1], kind, line)
        if name in decls.aliases:
            raise CortexError(f"имя {name} уже объявлено", line)
        decls.aliases[name] = DocRef(name, path)
        if kind == "create":
            decls.creates.add(name)
        return True
    if words[0] == "set":
        for tok in toks[1:]:
            if tok.kind != "kv":
                raise CortexError("set: только параметры ключ=значение", line)
            key, value = tok.value
            text = value.value if value.kind in ("name", "str") else None
            if text is None:
                raise CortexError(f"set {key}=: нужно простое значение", line)
            if key == "attempts":
                if not text.isdigit() or int(text) < 1:
                    raise CortexError("set attempts=: целое число от 1", line)
                decls.settings["attempts"] = int(text)
            elif key == "timeout":
                decls.settings["timeout"] = parse_duration(text, line)
            elif key == "autocommit":
                if text not in ("on", "off", "true", "false"):
                    raise CortexError("set autocommit=: on или off", line)
                decls.settings["autocommit"] = text in ("on", "true")
            else:
                raise CortexError(f"set: неизвестная настройка {key} (есть: attempts, timeout, autocommit)", line)
        return True
    return False


class CompileErrors(Exception):
    def __init__(self, errors: list[CortexError]) -> None:
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


# ----------------------------------------------------------------------------------------
# lines
# ----------------------------------------------------------------------------------------
class Compiler:
    def __init__(self, vm: Any, decls: Declarations, stream: Stream, stored_blocks: set[str],
                 done_results: set[str]) -> None:
        self.vm = vm
        self.decls = decls
        self.stream = stream
        self.names: dict[str, Src] = {}
        self.errors: list[CortexError] = []
        self._table(stored_blocks, done_results)

    # ----- names -----
    def _declare(self, name: str, ref: Src, line: int | None) -> None:
        if name in self.vm.words or name in DECL_WORDS:
            raise CortexError(f"имя {name} совпадает со словом языка", line)
        if name in RESERVED:
            raise CortexError(f"имя {name} зарезервировано", line)
        known = self.names.get(name)
        if known is not None and known != ref:
            kind = {"Block": "блок", "Result": "результат", "Doc": "документ"}[self.vm.types.of_value(known).name]
            raise CortexError(f"имя {name} уже занято ({kind})", line)
        self.names[name] = ref

    def _table(self, stored_blocks: set[str], done_results: set[str]) -> None:
        for name, doc in self.decls.aliases.items():
            self._guard(lambda: self._declare(name, doc, None))
        for name in sorted(set(self.stream.blocks) | stored_blocks):
            line = self.stream.blocks[name].line if name in self.stream.blocks else None
            self._guard(lambda: self._declare(name, BlockRef(name), line))
        self.results: dict[str, int] = {}
        for sl, toks, _comments in self.decls.actions:
            for k, tok in enumerate(toks):
                if tok.kind == "name" and tok.value == "agenttask":
                    name = self._result_name(toks, k)
                    if name is None:
                        continue
                    if name in self.results:
                        self.errors.append(CortexError(f"результат {name} уже создаёт строка {self.results[name]}",
                                                       sl.number))
                        continue
                    self.results[name] = sl.number
                    self._guard(lambda: self._declare(name, ResultRef(name), sl.number))
        for name in sorted(done_results - set(self.results)):
            if name not in self.names:
                self.names[name] = ResultRef(name)

    def _guard(self, fn) -> None:
        try:
            fn()
        except CortexError as exc:
            self.errors.append(exc)

    @staticmethod
    def _result_name(toks: list[Tok], k: int) -> str | None:
        if k + 1 < len(toks) and toks[k + 1].kind == "spec":
            items = toks[k + 1].value
            if items and items[0].kind == "name":
                return items[0].value
        prev = toks[k - 1] if k > 0 else None
        if prev is not None and prev.kind == "name":
            return f"{prev.value}.out"
        if prev is not None and prev.kind == "list" and prev.value and prev.value[0].kind == "name":
            return f"{prev.value[0].value}.out"
        return None

    def _unknown(self, word: str, line: int) -> CortexError:
        known = set(self.names) | set(self.vm.words) | set(DECL_WORDS) | set(RESERVED)
        if _CYRILLIC.search(word):
            latin = word.translate(_CONFUSABLE)
            if latin != word and latin in known:
                letters = ", ".join(sorted({c for c in word if _CYRILLIC.match(c)}))
                return CortexError(f"неизвестное слово «{word}»: кириллические буквы ({letters}) — "
                                   f"может быть, {latin}?", line)
        close = difflib.get_close_matches(word, sorted(known), n=3, cutoff=0.7)
        hint = f" — может быть, {', '.join(close)}?" if close else ""
        return CortexError(f"неизвестное имя или слово «{word}»{hint}", line)

    def resolve(self, word: str, line: int) -> Src:
        if word in RESERVED:
            raise CortexError(f"{word} — только значение after=/before=", line)
        ref = self.names.get(word)
        if ref is None:
            raise self._unknown(word, line)
        return ref

    # ----- compile -----
    def program(self, script: str, script_auto: bool) -> Program:
        lines: list[Line] = []
        producers: dict[str, Line] = {}
        for sl, toks, comments in self.decls.actions:
            try:
                line = self.line(sl, toks, comments)
            except CortexError as exc:
                self.errors.append(exc)
                continue
            except MdError as exc:
                self.errors.append(CortexError(str(exc), sl.number))
                continue
            lines.append(line)
            if line.agent is not None:
                producers[line.agent.result] = line
        if self.errors:
            raise CompileErrors(sorted(self.errors, key=lambda e: e.line or 0))
        return Program(script=script, script_auto=script_auto, decl_texts=self.decls.texts,
                       aliases=self.decls.aliases, creates=self.decls.creates, settings=self.decls.settings,
                       blocks=self.stream.blocks, lines=lines, trailing=self.decls.trailing,
                       run_marker=self.stream.run_marker, producers=producers)

    def line(self, sl: SourceLine, toks: list[Tok], comments: list[str]) -> Line:
        n = sl.number
        src = f"строка {n}"
        code: list[Any] = []
        agent: AgentStep | None = None
        write: WriteStep | None = None
        writes = agents = 0
        prev: Any = None
        first: Any = None
        patterns: list[tuple[str, HeadingPath]] = []
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok.kind == "name" and tok.value in self.vm.words:
                word = self.vm.words[tok.value]
                j = i + 1
                given: dict[str, Tok] = {}
                spec_tok: Tok | None = None
                while j < len(toks) and toks[j].kind in ("kv", "spec"):
                    extra = toks[j]
                    if extra.kind == "spec":
                        if "spec" not in word.params:
                            raise CortexError(f"{{…}} — описание результата, только после agenttask", n)
                        spec_tok = extra
                    else:
                        key = extra.value[0]
                        if key not in word.params or key == "spec":
                            allowed = ", ".join(p + "=" for p in word.params if p != "spec") or "нет"
                            raise CortexError(f"у слова {word.name} нет параметра {key}= (есть: {allowed})", n)
                        if key in given:
                            raise CortexError(f"параметр {key}= указан дважды", n)
                        given[key] = extra.value[1]
                    j += 1
                values = self.params(word, given, spec_tok, prev, n)
                code.extend(Lit(v, src) for v in values)
                code.append(Call(word.name, src))
                bound = dict(zip(word.params, values))
                if word.name == "agenttask":
                    agents += 1
                    if prev is None or not isinstance(prev, (list, BlockRef, ResultRef, SectionRef, DocRef, FileRef)):
                        raise CortexError("agenttask берёт задание по имени или список [задание, входы…] "
                                          "прямо перед собой", n)
                    task, inputs = (prev[0], list(prev[1:])) if isinstance(prev, list) else (prev, [])
                    if isinstance(prev, list) and not prev:
                        raise CortexError("agenttask: пустой список", n)
                    spec: ResultSpec = bound["spec"]
                    agent = AgentStep(spec.name, task, inputs, spec)
                if "write" in word.caps:
                    writes += 1
                    write = self._write_step(word.name, bound)
                    if write.doc is not None:
                        for key in ("after", "before"):
                            pos = bound.get(key)
                            if isinstance(pos, Pos) and pos.path is not None:
                                patterns.append((write.doc.name, pos.path))
                        if write.target is not None:
                            patterns.append((write.doc.name, write.target))
                prev = None
                i = j
                continue
            if tok.kind == "kv":
                raise CortexError(f"параметр {tok.value[0]}= без слова перед ним", n)
            if tok.kind == "spec":
                raise CortexError("{…} без agenttask перед ним", n)
            value = self.obj(tok, n)
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, SectionRef) and isinstance(item.base, DocRef):
                    patterns.append((item.base.name, item.path))
            code.append(Lit(value, src))
            prev = value
            first = value if first is None else first
            i += 1
        if not code:
            raise CortexError("пустая строка кода", n)
        if agents > 1:
            raise CortexError("одно задание агенту на строку", n)
        if writes > 1:
            raise CortexError("в строке больше одной записи (insert, replace, delete, move, save)", n)
        if writes == 1 and not (isinstance(code[-1], Call) and "write" in self.vm.words[code[-1].name].caps):
            raise CortexError("запись (insert, replace, delete, move, save) должна быть последним словом строки", n)
        for ins in code:
            if isinstance(ins, Call):
                ins.word = self.vm.words[ins.name]
        try:
            left = Checker(self.vm.types).check_script(code, src)
        except StackEffectError as exc:
            raise CortexError(str(exc)) from None
        ends_with_agent = isinstance(code[-1], Call) and code[-1].name == "agenttask"
        if left and not (ends_with_agent and len(left) == 1):
            raise CortexError(f"в конце строки на стеке остаётся {picture(left)}: строка должна заканчиваться "
                              "записью (insert, replace, delete, move, save) или заданием агенту", n)
        label = agent.result if agent else self._label(first) if first is not None else write.word if write else "?"
        return Line(number=n, text=sl.text, comments=comments, code=code, agent=agent, write=write,
                    label=label if agent else f"строка ⟨{n}⟩ · {label}", obj=label, patterns=patterns)

    @staticmethod
    def _label(value: Any) -> str:
        if isinstance(value, list):
            shown = ", ".join(item.label() for item in value[:3])
            return f"[{shown}{', …' if len(value) > 3 else ''}]"
        return value.label()

    @staticmethod
    def _write_step(word: str, bound: dict[str, Any]) -> WriteStep:
        step = WriteStep(word)
        to = bound.get("to")
        if isinstance(to, DocRef):
            step.doc = to
        elif isinstance(to, FilePath):
            step.path = to.path
        section = bound.get("section")
        if isinstance(section, Pattern):
            step.target = section.path
        for key in ("after", "before"):
            if bound.get(key) is not None:
                step.pos, step.side = bound[key], key
        return step

    # ----- objects -----
    def obj(self, tok: Tok, line: int) -> Any:
        if tok.kind == "str":
            return TextValue(tok.value, "текст")
        if tok.kind == "name":
            if tok.value.isdigit():
                raise CortexError(f"число {tok.value} — только значение параметра", line)
            return self.resolve(tok.value, line)
        if tok.kind == "sec":
            return self.section(tok, line)
        if tok.kind == "list":
            return [self.item(t, line) for t in tok.value]
        raise CortexError(f"неожиданное {tok.value!r}", line)  # pragma: no cover

    def item(self, tok: Tok, line: int) -> Src:
        if tok.kind == "name":
            if tok.value in self.vm.words:
                raise CortexError(f"в списке — имена, а не слова ({tok.value})", line)
            return self.resolve(tok.value, line)
        if tok.kind == "str":
            return FileRef(check_rel_path(tok.value, line))
        if tok.kind == "sec":
            return self.section(tok, line)
        raise CortexError("в списке допустимы имена, \"пути к файлам\" и разделы имя\"шаблон\"", line)

    def section(self, tok: Tok, line: int) -> SectionRef:
        name, pattern = tok.value
        base = self.resolve(name, line)
        try:
            return SectionRef(base, parse_path(pattern))
        except MdError as exc:
            raise CortexError(str(exc), line) from None

    # ----- parameters -----
    def params(self, word: Any, given: dict[str, Tok], spec_tok: Tok | None, prev: Any, line: int) -> list[Any]:
        names = word.sig.input_names
        values: list[Any] = []
        for param in word.params:
            t = word.sig.inputs[names.index(param)]
            members = [m for m in (t.args if t.name == "|" else (t,)) if m != NONE]
            optional = t.name == "|" and NONE in t.args
            base = members[0].name if len(members) == 1 else str(t)
            if base == "ResultSpec":
                values.append(self.spec(spec_tok, prev, line))
                continue
            if param not in given:
                if optional:
                    values.append(None)
                    continue
                raise CortexError(f"слову {word.name} нужен параметр {param}=", line)
            values.append(self.convert(base, given[param], word.name, param, line))
        if word.name in ("insert", "move"):
            chosen = [p for p in ("after", "before") if p in given]
            if len(chosen) != 1:
                raise CortexError(f"{word.name}: нужен ровно один из after= или before=", line)
        return values

    def convert(self, base: str, tok: Tok, word: str, param: str, line: int) -> Any:
        try:
            if base == "Doc":
                ref = self.names.get(tok.value) if tok.kind == "name" else None
                if tok.kind == "name" and ref is None:
                    raise self._unknown(tok.value, line)
                if not isinstance(ref, DocRef):
                    raise CortexError(f"{word} {param}=: нужно имя документа из alias или create", line)
                return ref
            if base == "Pos":
                if tok.kind == "name" and tok.value in RESERVED:
                    return Pos("start" if tok.value == "START" else "end")
                if tok.kind == "str":
                    return Pos("path", parse_path(tok.value))
                raise CortexError(f"{word} {param}=: \"# заголовок…\", START или END", line)
            if base == "Pattern":
                if tok.kind != "str":
                    raise CortexError(f"{word} {param}=: нужен \"# заголовок…\"", line)
                return Pattern(parse_path(tok.value))
            if base == "Int":
                if tok.kind != "name" or not tok.value.isdigit():
                    raise CortexError(f"{word} {param}=: нужно целое число", line)
                return int(tok.value)
            if base == "FilePath":
                if tok.kind != "str":
                    raise CortexError(f"{word} {param}=: нужен \"путь/к/файлу\"", line)
                return FilePath(check_rel_path(tok.value, line))
        except MdError as exc:
            raise CortexError(f"{word} {param}=: {exc}", line) from None
        raise CortexError(f"{word} {param}=: тип {base} не поддерживается", line)  # pragma: no cover

    def spec(self, tok: Tok | None, prev: Any, line: int) -> ResultSpec:
        items = tok.value if tok is not None else []
        name: str | None = None
        head = None
        has: list[HeadingPath] = []
        for k, item in enumerate(items):
            if item.kind == "name" and k == 0:
                name = item.value
                continue
            if item.kind != "kv":
                raise CortexError("{…}: имя результата, затем head= и has=", line)
            key, value = item.value
            try:
                if key == "head":
                    if value.kind != "str":
                        raise CortexError("head=: нужен \"# заголовок…\"", line)
                    path = parse_path(value.value)
                    if len(path.segments) != 1:
                        raise CortexError("head=: один заголовок, без \" > \"", line)
                    head = path.segments[0]
                elif key == "has":
                    raw = value.value if value.kind == "list" else [value]
                    for one in raw:
                        if one.kind != "str":
                            raise CortexError("has=: \"# путь > ## …\" или их список", line)
                        has.append(parse_path(one.value))
                else:
                    raise CortexError(f"{{…}}: неизвестный параметр {key}= (есть head=, has=)", line)
            except MdError as exc:
                raise CortexError(f"{key}=: {exc}", line) from None
        if name is None:
            base = prev[0] if isinstance(prev, list) and prev else prev
            if isinstance(base, (BlockRef, ResultRef)):
                name = f"{base.name}.out"
            else:
                raise CortexError("у результата нет имени: agenttask {имя …}", line)
        if not NAME_RE.match(name):
            raise CortexError(f"имя результата «{name}»: латинские буквы, цифры, _ . -", line)
        if head is not None:
            for path in has:
                if not path.segments[0].compatible(head):
                    raise CortexError(f"has=: путь \"{path}\" должен начинаться с head \"{head}\"", line)
        return ResultSpec(name, head, tuple(has))


def default_script_name() -> str:
    return time.strftime("script-%Y%m%d-%H%M%S")
