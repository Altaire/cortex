"""Load-time check of colon definitions: the stack of types is simulated through the body.

Every call must find values of fitting types on the stack; at the end exactly the outputs of
the stack comment must remain. Errors name file:line and show the stack picture.
"""
from __future__ import annotations

from fvm.types import ANY, T, TypeRegistry, is_var, union
from fvm.words import Call, Each, If, Lit, LocalGet, LocalSet, Loop, Match, Parallel, Tick, Word


class StackEffectError(ValueError):
    pass


def picture(stack: list[T]) -> str:
    return "( " + " ".join(str(t) for t in stack) + " )" if stack else "( empty )"


class Checker:
    def __init__(self, types: TypeRegistry) -> None:
        self.types = types

    def check_word(self, word: Word) -> None:
        stack = list(word.sig.inputs)
        local_types: dict[str, T] = {}
        stack = self._code(word.code, stack, local_types, word.name)
        expected = word.sig.outputs
        if len(stack) != len(expected) or not all(self.types.fits(a, e) for a, e in zip(stack, expected)):
            raise StackEffectError(
                f"{word.source}: word {word.name} {word.sig} leaves {picture(stack)} at the end, "
                f"it must leave {picture(expected)}")

    def check_script(self, code: list, name: str, initial: list[T] | None = None,
                     locals_types: dict[str, T] | None = None) -> list[T]:
        """Top-level code; `initial` — types already on the stack (REPL between lines);
        `locals_types` — names bound before it runs (the arguments of a call template)."""
        return self._code(code, list(initial or []), dict(locals_types or {}), name)

    # ----- simulation -----
    def _code(self, code: list, stack: list[T], local_types: dict[str, T], owner: str) -> list[T]:
        for ins in code:
            stack = self._one(ins, stack, local_types, owner)
        return stack

    def _one(self, ins, stack: list[T], local_types: dict[str, T], owner: str) -> list[T]:
        if isinstance(ins, Lit):
            return stack + [self._literal_type(ins.value)]
        if isinstance(ins, Tick):
            return stack + [T("Word")]
        if isinstance(ins, LocalGet):
            return stack + [local_types.get(ins.name, ANY)]
        if isinstance(ins, LocalSet):
            if len(stack) < len(ins.names):
                raise StackEffectError(f"{ins.src}: {{ {' '.join(ins.names)} }} — the stack is {picture(stack)}, "
                                       f"{len(ins.names)} values needed")
            taken = stack[len(stack) - len(ins.names):]
            local_types.update(zip(ins.names, taken))
            return stack[:len(stack) - len(ins.names)]
        if isinstance(ins, Call):
            return self._call(ins, stack)
        if isinstance(ins, If):
            if not stack or not self.types.fits(stack[-1], T("Bool")):
                raise StackEffectError(f"{ins.src}: IF needs a Bool on top, the stack is {picture(stack)}")
            base = stack[:-1]
            return self._join(ins.src, "IF/ELSE",
                              [self._code(ins.then, list(base), local_types, owner),
                               self._code(ins.otherwise, list(base), local_types, owner)])
        if isinstance(ins, Match):
            if not stack:
                raise StackEffectError(f"{ins.src}: MATCH on an empty stack")
            results = []
            for branch_type, body in ins.branches:
                results.append(self._code(body, stack[:-1] + [branch_type], local_types, owner))
            return self._join(ins.src, "MATCH branches", results)
        if isinstance(ins, Each):
            if not stack or not self.types.fits(stack[-1], T("List")):
                raise StackEffectError(f"{ins.src}: EACH needs a List on top, the stack is {picture(stack)}")
            item = stack[-1].args[0] if stack[-1].name == "List" and stack[-1].args else ANY
            out = self._code(ins.body, [item], dict(local_types), owner)
            if len(out) != 1:
                raise StackEffectError(f"{ins.src}: the body of EACH must leave one value, "
                                       f"it leaves {picture(out)}")
            return stack[:-1] + [T("List", (out[0],))]
        if isinstance(ins, Parallel):
            result = list(stack)
            for body in ins.branches:
                result += self._code(body, [], dict(local_types), owner)
            return result
        if isinstance(ins, Loop):
            out = self._code(ins.body, list(stack), local_types, owner)
            if not out or not self.types.fits(out[-1], T("Bool")):
                raise StackEffectError(f"{ins.src}: the body of BEGIN … UNTIL must leave a Bool")
            return out[:-1]
        raise StackEffectError(f"unknown instruction {ins!r}")

    def _call(self, ins: Call, stack: list[T]) -> list[T]:
        word = ins.word
        sig = word.sig
        n = len(sig.inputs)
        if len(stack) < n:
            raise StackEffectError(f"{ins.src}: {word.name} needs {sig}, the stack is {picture(stack)} — "
                                   f"not enough values")
        given = stack[len(stack) - n:]
        bindings: dict[str, T] = {}
        for actual, expected in zip(given, sig.inputs):
            if is_var(expected):
                letter = expected.args[0].name
                bindings.setdefault(letter, actual)
                continue
            if not self.types.fits(actual, expected):
                raise StackEffectError(f"{ins.src}: {word.name} needs {sig}, the stack is {picture(stack)}")
        outputs = [bindings.get(t.args[0].name, ANY) if is_var(t) else t for t in sig.outputs]
        return stack[:len(stack) - n] + outputs

    def _join(self, src: str, what: str, results: list[list[T]]) -> list[T]:
        lengths = {len(r) for r in results}
        if len(lengths) != 1:
            raise StackEffectError(f"{src}: {what} leave stacks of different depth: "
                                   + " / ".join(picture(r) for r in results))
        return [union(*column) for column in zip(*results)]

    def _literal_type(self, value) -> T:
        if isinstance(value, list):
            return T("List", (union(*[self._literal_type(v) for v in value]),)) if value else T("List")
        return self.types.of_value(value)
