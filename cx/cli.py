"""Command line of Cortex.

    python cortex.py < script              run (the same: python cortex.py run < script)
    python cortex.py plan < script         forecast: what a run would do; nothing is written
    python cortex.py status                the state of every script, without a run
    python cortex.py outline FILE…         the map of a document
    python cortex.py reset SCRIPT RESULT   an agent's job starts over (launch, attempts from zero)
    python cortex.py help [WORD]           this help; a word: its stack comment and parameters
--root DIR — the workspace (default: the current folder).
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import TextIO

from cx.errors import CortexError, LineError
from cx.store import Store

COMMANDS = ("run", "plan", "status", "outline", "reset", "help")

HELP = """\
Cortex — исполнитель скриптов работы с Markdown-документами, часть шагов которых делают агенты.
Сам Cortex агентов не запускает: он выполняет всё, для чего есть данные, и печатает остаток
скрипта со сводкой — кого из агентов запустить, кого ждать, где нужно решение.

Запуск
  python cortex.py < файл                 прогон; вывод = остаток скрипта (он же в .cortex/scripts/)
  python cortex.py plan < файл            прогноз без выполнения и без записи
  python cortex.py status                 состояние всех скриптов без прогона
  python cortex.py outline doc.md         карта документа: заголовки, слова, предупреждения
  python cortex.py reset СКРИПТ РЕЗУЛЬТАТ задание агента — заново (launch, попытки с нуля)
  python cortex.py help [слово]           эта справка или описание слова
  --root ПАПКА                            рабочая папка (по умолчанию текущая)

Поток
  script chapters                          имя скрипта (нет — Cortex назначит и допишет)
  "doc.md" alias maindoc1                  имя существующего файла
  "new.md" create book                     то же, файл создаётся пустым, если его нет
  set attempts=3 timeout=30m autocommit=off
  === task1 === … === / ===                блок текста; блок context попадает в каждую инструкцию агенту
  \\ комментарий                            строки сводки (\\ run, \\ done: …) на входе игнорируются

Строка: объекты, слова, параметры
  task1  text0  maindoc1                   имена блоков, результатов, документов
  "текст"                                  текст; в списке входов агента — путь к файлу
  maindoc1"# Раздел 5..."                  раздел документа, блока или результата
  "# Раздел 5..." / "# Раздел 5. Методы"   начало заголовка (до границы слова) / точный заголовок
  "# Раздел 5... > ## Выводы"              путь: подраздел внутри раздела
  [task2, task1.out, maindoc1"# …"]        список: первым — задание, дальше — входы агента
  {task2.out head="# …" has=["# … > ## …"]} описание результата агента (только после agenttask)
  START  END                               начало и конец документа (after=START, before=END)

Правила
  стек живёт внутри строки и в конце пуст; строка может закончиться на agenttask — результат
  сохранится под своим именем; в строке не больше одной записи, и она последняя; строка
  выполняется целиком или остаётся в скрипте со статусом.

Статусы в сводке
  done — выполнено; launch — запусти агента: «Выполни инструкцию из <файл>»;
  running — агент работает; waiting — ждёт другую строку или результат;
  decide — нужно решение координатора; message — сообщение агента; note — к сведению.

Слова
"""


def _help(vm, word: str | None) -> str:
    if word is not None:
        w = vm.words.get(word)
        if w is None:
            return f"нет слова {word}; слова: {', '.join(sorted(vm.words))}\n"
        params = " ".join(p + "=" for p in w.params if p != "spec") or ("{…}" if "spec" in w.params else "—")
        doc = "\n  ".join(w.doc.splitlines())
        return f"{w.name} {w.sig}\n  параметры: {params}\n  права: {', '.join(sorted(w.caps))}\n  {doc}\n"
    rows = []
    for name in ("insert", "replace", "delete", "move", "save", "join", "relevel", "agenttask"):
        w = vm.words.get(name)
        if w is None:
            continue
        params = " ".join(p + "=" for p in w.params if p != "spec")
        if "spec" in w.params:
            params = "{…}"
        first = w.doc.splitlines()[0] if w.doc else ""
        rows.append(f"  {name:<10} {params:<26} {first}")
    return HELP + "\n".join(rows) + "\n"


def _status(root: Path) -> str:
    from cx.frontend import CompileErrors, read_declarations
    from cx.stream import split_stream
    folder = root / ".cortex" / "scripts"
    files = sorted(folder.glob("*.cortex")) if folder.is_dir() else []
    if not files:
        return "скриптов нет (.cortex/scripts пуст)\n"
    out: list[str] = []
    now = time.time()
    for path in files:
        name = path.stem
        text = path.read_text(encoding="utf-8")
        try:
            actions = len(read_declarations(split_stream(text)).actions)
        except (CortexError, CompileErrors) as exc:
            out.append(f"{name:<18} ? файл не разбирается: {exc}")
            continue
        last: dict[str, int] = {}
        for kind in re.findall(r"^\\\s*(launch|running|waiting|decide):", text, re.MULTILINE):
            last[kind] = last.get(kind, 0) + 1
        store = Store(root, name)
        state = store.load_state()
        ready, running = [], []
        for result, job in sorted(state["jobs"].items()):
            if job.get("done"):
                continue
            if store.message(result).exists():
                ready.append(result)
            elif store.status(result).exists():
                minutes = int((now - store.status(result).stat().st_mtime) // 60)
                running.append(f"{result} ({minutes} мин)")
        state_word = "done" if actions == 0 else "active"
        parts = [f"{name:<18} {state_word:<7} прогон {state['run']}, строк {actions}"]
        if last:
            parts.append("по прогону: " + ", ".join(f"{k} {v}" for k, v in last.items()))
        if running:
            parts.append("работают: " + ", ".join(running))
        if ready:
            parts.append("готовы к прогону: " + ", ".join(ready))
        out.append(" · ".join(parts))
    return "\n".join(out) + "\n"


def main(argv: list[str], stdin: TextIO | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    out = out or sys.stdout
    err = err or sys.stderr
    for stream in (out, err):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = list(argv)
    root = Path.cwd()
    if "--root" in args:
        k = args.index("--root")
        if k + 1 >= len(args):
            print("cortex: --root ПАПКА", file=err)
            return 2
        root = Path(args[k + 1])
        del args[k:k + 2]
    if args and args[0] not in COMMANDS:
        print(f"cortex: неизвестная команда {args[0]} (есть: {', '.join(COMMANDS)})", file=err)
        return 2
    command = args.pop(0) if args else "run"
    if command in ("run", "plan"):
        if args:
            print(f"cortex: {command} читает скрипт из STDIN: python cortex.py {command} < файл", file=err)
            return 2
        if stdin.isatty():
            print("cortex: подай скрипт на STDIN: python cortex.py < файл (справка: python cortex.py help)", file=err)
            return 2
        data = stdin.buffer.read().decode("utf-8") if hasattr(stdin, "buffer") else stdin.read()
        from cx.runner import execute
        return execute(data, root, command, out, err)
    if command == "status":
        out.write(_status(root))
        return 0
    if command == "outline":
        if not args:
            print("cortex: outline ФАЙЛ…", file=err)
            return 2
        from mdlib import outline
        for name in args:
            path = root / name
            if not path.exists():
                print(f"cortex: нет файла {name}", file=err)
                return 1
            out.write(outline(path.read_text(encoding="utf-8"), name) + "\n")
        return 0
    if command == "reset":
        if len(args) != 2:
            print("cortex: reset СКРИПТ РЕЗУЛЬТАТ", file=err)
            return 2
        from cx.agents import reset
        store = Store(root, args[0])
        try:
            with store.lock():
                state = store.load_state()
                message = reset(store, state, args[1])
                store.save_state(state)
        except (CortexError, LineError) as exc:
            print(f"cortex: {exc}", file=err)
            return 1
        out.write(message + "\n")
        return 0
    from cx.env import Env
    from cx.runner import make_vm
    out.write(_help(make_vm(Env()), args[0] if args else None))
    return 0
