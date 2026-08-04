"""Lexer and compiler: Forth source text → words, MCP call templates, script code.

Syntax (classic Forth, with a few structured words):

    \\ comment to the end of the line        ( comment )   — after ": name" it is the signature
    : name ( In -- Out )  body ;             a definition; "\\" lines right above it are its doc
    " text"   ""   42   -1.5   true false none   [ 1 " a" ]      literals
    { a b }                                  pop into local names (b = top); a name pushes it back
    : name ( a:A b:B -- … )  … a … b … ;     named inputs the body uses are bound as locals on
                                             entry (as if it began with { a b }); a body that
                                             begins with { … } or never names them works on the stack
    IF … ELSE … THEN                         on Bool
    MATCH  Type -> …  Type -> …  END         on the type of the top value (it stays on the stack)
    EACH … NEXT    PARALLEL-EACH … NEXT      map a list: the body gets one item, leaves one value
    PARALLEL … | … END                       independent branches, results in branch order
    BEGIN … UNTIL                            loop while the body leaves false
    ['] name                                 the word itself as a value

A template of an MCP call (scripts/mcp/*.fs) is a TOOL header and the program under it:

    TOOL name                                the MCP tool
      ARG name Type [default]                default: a literal or a word ( -- value );
                                             no default = required
      DESCRIPTION " text" word " text"       pieces joined into one description
    END-TOOL
    chunk_ids chunk_offset … chapter/request  the program: the argument names are placeholders,
    …                                        filled with the values of the call; it must leave
                                             one Response on the stack
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fvm.types import parse as parse_type
from fvm.words import (
    Call, Each, If, Lit, LocalGet, LocalSet, Loop, Match, Parallel, Signature, Tick, Tool, ToolArg, Word,
)

_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_LITERAL_WORDS = {"true": True, "false": False, "none": None}
KEYWORDS = {":", ";", "IF", "ELSE", "THEN", "MATCH", "->", "END", "EACH", "PARALLEL-EACH", "NEXT",
            "PARALLEL", "|", "BEGIN", "UNTIL", "{", "}", "[']", "[", "]", "TOOL", "ARG", "DESCRIPTION",
            "END-TOOL"}


class ForthSyntaxError(ValueError):
    pass


@dataclass
class Token:
    kind: str      # word | str | num | comment | line
    text: str
    value: Any
    file: str
    line: int

    @property
    def src(self) -> str:
        return f"{self.file}:{self.line}"


def tokenize(text: str, file: str) -> list[Token]:
    tokens: list[Token] = []
    i, line, n = 0, 1, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        word = text[i:j]
        if word == "\\":                                   # comment to the end of the line
            end = text.find("\n", j)
            end = n if end < 0 else end
            tokens.append(Token("line", text[j:end].strip(), None, file, line))
            i = end
        elif word == "(":                                  # ( comment ) / ( stack comment )
            end = text.find(")", j)
            if end < 0:
                raise ForthSyntaxError(f"{file}:{line}: comment without a closing )")
            body = text[j:end]
            tokens.append(Token("comment", "( " + " ".join(body.split()) + " )", body, file, line))
            line += body.count("\n")
            i = end + 1
        elif word == '"':                                  # " text" — the space after " separates
            k = j + 1 if j < n and text[j] in " \t" else j
            chars: list[str] = []
            start_line = line
            while k < n and text[k] != '"':
                if text[k] == "\\" and k + 1 < n:
                    chars.append({"n": "\n", "t": "\t"}.get(text[k + 1], text[k + 1]))
                    k += 2
                    continue
                if text[k] == "\n":
                    line += 1
                chars.append(text[k])
                k += 1
            if k >= n:
                raise ForthSyntaxError(f"{file}:{start_line}: string without a closing quote")
            tokens.append(Token("str", '"', "".join(chars), file, start_line))
            i = k + 1
        elif word == '""':
            tokens.append(Token("str", '""', "", file, line))
            i = j
        elif _NUMBER_RE.match(word):
            tokens.append(Token("num", word, float(word) if "." in word else int(word), file, line))
            i = j
        else:
            tokens.append(Token("word", word, None, file, line))
            i = j
    return tokens


@dataclass
class Unit:
    """What one source file defines."""
    words: list[Word]
    tools: list[Tool]
    script: list[Any]          # top-level code (scripts only)


class Compiler:
    def __init__(self, text: str, file: str) -> None:
        self.text = text
        self.file = file
        self.tokens = tokenize(text, file)
        self.pos = 0
        self.scopes: list[set[str]] = []

    # ----- helpers -----
    def _peek(self, offset: int = 0) -> Token | None:
        k = self.pos + offset
        return self.tokens[k] if k < len(self.tokens) else None

    def _next(self, what: str) -> Token:
        token = self._peek()
        if token is None:
            raise ForthSyntaxError(f"{self.file}: unexpected end of file, expected {what}")
        self.pos += 1
        return token

    def _error(self, token: Token, message: str) -> ForthSyntaxError:
        return ForthSyntaxError(f"{token.src}: {message}")

    def _is(self, token: Token | None, *words: str) -> bool:
        return token is not None and token.kind == "word" and token.text in words

    # ----- file level -----
    def compile(self, allow_script: bool) -> Unit:
        words: list[Word] = []
        tools: list[Tool] = []
        script: list[Any] = []
        doc: list[Token] = []   # "\\" lines; the run right above ":" is the word's doc
        while self._peek() is not None:
            token = self._peek()
            if token.kind == "line":
                if doc and doc[-1].line != token.line - 1:
                    doc = []          # an empty line ends a comment block
                doc.append(token)
                self.pos += 1
                continue
            if token.kind == "comment":
                doc = []
                self.pos += 1
                continue
            if self._is(token, ":"):
                above = "\n".join(t.text for t in doc) if doc and doc[-1].line == token.line - 1 else ""
                words.append(self._definition(above))
            elif self._is(token, "TOOL"):
                raise self._error(token, "TOOL — only in a call template (scripts/mcp/*.fs, compile_template)")
            elif allow_script:
                self.scopes = [set()]
                script.extend(self._sequence(stops=(":", "TOOL"), top=True))
            else:
                raise self._error(token, f"outside a definition in a library file: {token.text!r} "
                                         "(top-level code is allowed only in scripts)")
            doc = []
        return Unit(words, tools, script)

    def _definition(self, doc: str) -> Word:
        start = self._next(":")
        name_token = self._next("a word name")
        if name_token.kind != "word" or name_token.text in KEYWORDS:
            raise self._error(name_token, f"a word name expected after \":\", not {name_token.text!r}")
        sig_token = self._next("a stack comment")
        if sig_token.kind != "comment":
            raise self._error(sig_token, f"word {name_token.text} has no stack comment ( in -- out )")
        try:
            sig = Signature.parse(sig_token.value)
        except ValueError as exc:
            raise self._error(sig_token, str(exc)) from None
        self.scopes = [set()]
        auto = self._auto_locals(sig)
        body = self._sequence(stops=(";",))
        if auto:
            body = [LocalSet(auto, sig_token.src), *body]
        end = self._next(";")
        lines = self.text.splitlines()[start.line - 1:end.line]
        return Word(name=name_token.text, sig=sig, kind="colon", doc=doc, source=start.src,
                    text="\n".join(lines), code=body)

    def _auto_locals(self, sig: Signature) -> list[str]:
        """The input names of the stack comment, when the body uses them and does not bind
        the inputs itself (does not begin with { … }); they then become the locals."""
        names = sig.input_names
        if not names or None in names:
            return []
        k = self.pos
        while k < len(self.tokens) and self.tokens[k].kind in ("line", "comment"):
            k += 1
        if k < len(self.tokens) and self._is(self.tokens[k], "{"):
            return []
        while k < len(self.tokens) and not self._is(self.tokens[k], ";"):
            if self.tokens[k].kind == "word" and self.tokens[k].text in names:
                self.scopes[0].update(names)
                return list(names)
            k += 1
        return []

    def template(self) -> Tool:
        """A template of an MCP call: the TOOL header, then the program with the arguments as
        placeholders (compiled as local names bound before the program runs)."""
        while self._peek() is not None and self._peek().kind in ("line", "comment"):
            self.pos += 1
        start = self._peek()
        if not self._is(start, "TOOL"):
            raise ForthSyntaxError(f"{self.file}: a call template starts with TOOL name … END-TOOL")
        self.pos += 1
        name = self._next("a tool name").text
        args: list[ToolArg] = []
        parts: list[tuple[str, Any]] = []
        while True:
            token = self._next("END-TOOL")
            if self._is(token, "END-TOOL"):
                break
            if self._is(token, "ARG"):
                arg_name = self._next("an argument name")
                arg_type = self._next("an argument type")
                try:
                    arg = ToolArg(arg_name.text, parse_type(arg_type.text))
                except ValueError as exc:
                    raise self._error(arg_type, str(exc)) from None
                default = self._peek()
                if default is not None and default.line == token.line and default.kind != "line":
                    self.pos += 1
                    arg.required = False
                    if default.kind in ("str", "num"):
                        arg.default = ("lit", default.value)
                    elif default.text in _LITERAL_WORDS:
                        arg.default = ("lit", _LITERAL_WORDS[default.text])
                    else:
                        arg.default = ("word", default.text)
                args.append(arg)
            elif self._is(token, "DESCRIPTION"):
                while self._peek() is not None and not self._is(self._peek(), "ARG", "END-TOOL"):
                    piece = self._next("a description")
                    if piece.kind == "line":
                        continue
                    parts.append(("str", piece.value) if piece.kind == "str" else ("word", piece.text))
            elif token.kind == "line":
                continue
            else:
                raise self._error(token, f"ARG, DESCRIPTION or END-TOOL expected in TOOL, not {token.text!r}")
        body_start = self._peek()
        self.scopes = [{arg.name for arg in args}]
        code = self._sequence(stops=(":", "TOOL"), top=True)
        if self._peek() is not None:
            raise self._error(self._peek(), "nothing may follow the program in a call template")
        text = "\n".join(self.text.splitlines()[body_start.line - 1:]).strip() if body_start else ""
        return Tool(name=name, args=args, description_parts=parts, source=start.src, code=code,
                    file=self.file, text=text)

    # ----- code -----
    def _local(self, name: str) -> bool:
        return any(name in scope for scope in self.scopes)

    def _sequence(self, stops: tuple[str, ...], match_mode: bool = False, top: bool = False) -> list[Any]:
        code: list[Any] = []
        while True:
            token = self._peek()
            if token is None:
                if top:
                    return code
                raise ForthSyntaxError(f"{self.file}: unexpected end of file, expected {' / '.join(stops)}")
            if token.kind in ("line", "comment"):
                self.pos += 1
                continue
            if token.kind == "word" and token.text in stops:
                return code
            if match_mode and self._is(self._peek(1), "->"):
                return code
            self.pos += 1
            code.append(self._instruction(token))

    def _instruction(self, token: Token) -> Any:
        if token.kind in ("str", "num"):
            return Lit(token.value, token.src)
        word = token.text
        if word in _LITERAL_WORDS:
            return Lit(_LITERAL_WORDS[word], token.src)
        if word == "[":
            return Lit(self._list_literal(token), token.src)
        if word == "IF":
            then = self._sequence(stops=("ELSE", "THEN"))
            otherwise: list[Any] = []
            if self._is(self._next("THEN"), "ELSE"):
                otherwise = self._sequence(stops=("THEN",))
                self._next("THEN")
            return If(then, otherwise, token.src)
        if word == "MATCH":
            branches = []
            while True:
                head = self._next("a MATCH branch or END")
                if self._is(head, "END"):
                    break
                if head.kind in ("line", "comment"):
                    continue
                arrow = self._next("->")
                if not self._is(arrow, "->"):
                    raise self._error(head, f"\"Type ->\" expected in MATCH, not {head.text!r}")
                try:
                    branch_type = parse_type(head.text)
                except ValueError as exc:
                    raise self._error(head, str(exc)) from None
                branches.append((branch_type, self._sequence(stops=("END",), match_mode=True)))
            if not branches:
                raise self._error(token, "MATCH without branches")
            return Match(branches, token.src)
        if word in ("EACH", "PARALLEL-EACH"):
            self.scopes.append(set())
            body = self._sequence(stops=("NEXT",))
            self.scopes.pop()
            self._next("NEXT")
            return Each(body, word == "PARALLEL-EACH", token.src)
        if word == "PARALLEL":
            branches = []
            while True:
                self.scopes.append(set())
                branches.append(self._sequence(stops=("|", "END")))
                self.scopes.pop()
                if self._is(self._next("END"), "END"):
                    break
            return Parallel(branches, token.src)
        if word == "BEGIN":
            body = self._sequence(stops=("UNTIL",))
            self._next("UNTIL")
            return Loop(body, token.src)
        if word == "{":
            names = []
            while True:
                name = self._next("}")
                if self._is(name, "}"):
                    break
                if name.kind != "word" or name.text in KEYWORDS:
                    raise self._error(name, f"invalid local name {name.text!r}")
                names.append(name.text)
            self.scopes[-1].update(names)
            return LocalSet(names, token.src)
        if word == "[']":
            target = self._next("a word name after [']")
            return Tick(target.text, token.src)
        if word in KEYWORDS:
            raise self._error(token, f"{word!r} is out of place here")
        if self._local(word):
            return LocalGet(word, token.src)
        return Call(word, token.src)

    def _list_literal(self, start: Token) -> list[Any]:
        items: list[Any] = []
        while True:
            token = self._next("]")
            if self._is(token, "]"):
                return items
            if token.kind in ("str", "num"):
                items.append(token.value)
            elif token.text in _LITERAL_WORDS:
                items.append(_LITERAL_WORDS[token.text])
            elif self._is(token, "["):
                items.append(self._list_literal(token))
            elif token.kind in ("line", "comment"):
                continue
            else:
                raise self._error(token, f"only literals are allowed in [ … ], not {token.text!r}")


def compile_source(text: str, file: str, allow_script: bool = False) -> Unit:
    return Compiler(text, file).compile(allow_script)


def compile_template(text: str, file: str) -> Tool:
    return Compiler(text, file).template()
