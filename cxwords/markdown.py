"""Words that work with Markdown: writes into documents and transformations of texts."""
from __future__ import annotations

from typing import Any

from cx.errors import LineError
from cx.values import TextValue
from fvm.annotations import word
from mdlib import MdError, join_texts, relevel as relevel_text


@word("insert", "( src:Src to:Doc after:Pos? before:Pos? -- )", caps=("write",), params=("to", "after", "before"))
def insert(env: Any, src: Any, to: Any, after: Any, before: Any) -> None:
    """Вставить текст в документ: после раздела (after=) или перед ним (before=); START — начало, END — конец."""
    env.insert(src, to, after or before, "after" if after is not None else "before")


@word("replace", "( src:Src to:Doc section:Pattern -- )", caps=("write",), params=("to", "section"))
def replace(env: Any, src: Any, to: Any, section: Any) -> None:
    """Заменить раздел целиком, вместе с заголовком."""
    env.replace(src, to, section)


@word("delete", "( to:Doc section:Pattern -- )", caps=("write",), params=("to", "section"))
def delete(env: Any, to: Any, section: Any) -> None:
    """Удалить раздел вместе с подразделами."""
    env.delete(to, section)


@word("move", "( to:Doc section:Pattern after:Pos? before:Pos? -- )", caps=("write",),
      params=("to", "section", "after", "before"))
def move(env: Any, to: Any, section: Any, after: Any, before: Any) -> None:
    """Переместить раздел: после другого раздела (after=) или перед ним (before=)."""
    env.move(to, section, after or before, "after" if after is not None else "before")


@word("save", "( src:Src to:FilePath -- )", caps=("write",), params=("to",))
def save(env: Any, src: Any, to: Any) -> None:
    """Записать текст в отдельный файл (файл перезаписывается)."""
    env.save(src, to)


@word("join", "( items:List -- Text )", caps=("read",))
def join(env: Any, items: list[Any]) -> TextValue:
    """Список текстов → один текст, части через пустую строку."""
    return TextValue(join_texts([env.text_of(item) for item in items]), "join")


@word("relevel", "( src:Src top:Int -- Text )", caps=("read",), params=("top",))
def relevel(env: Any, src: Any, top: int) -> TextValue:
    """Сдвинуть заголовки так, чтобы верхний был уровня top."""
    try:
        return TextValue(relevel_text(env.text_of(src), top), f"{src.label()} relevel")
    except MdError as exc:
        raise LineError(str(exc)) from None
