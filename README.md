# Cortex

Исполнитель скриптов работы с Markdown-документами, часть шагов которых делают LLM-агенты.
Cortex агентов не запускает и к LLM не обращается: он выполняет всё, для чего есть данные,
и печатает остаток скрипта со сводкой — каких агентов запустить, кого ждать, где нужно решение
координатора. Спецификация — документ «Cortex — спецификация v1».

```bash
pip install -r requirements.txt
python cortex.py help
python cortex.py < plan.cortex
python cortex.py < .cortex/scripts/<скрипт>.cortex
```

## Пример

```
script chapters
"doc.md" alias maindoc1

text0 insert to=maindoc1 after="# Раздел 2..."
task1 agenttask {task1.out head="# Раздел 4..." has=["# Раздел 4... > ## Выводы"]} insert to=maindoc1 after="# Раздел 3..."
[task2, task1.out, maindoc1"# Раздел 3..."] agenttask {task2.out head="# Раздел 5..."} insert to=maindoc1 before=END

=== context ===
Пиши кратко.
=== / ===
=== text0 ===
Дополнение к обзору.
=== / ===
=== task1 ===
Проанализируй раздел 3 и напиши раздел 4.
=== / ===
=== task2 ===
Проанализируй раздел 4 и напиши раздел 5.
=== / ===
```

Первый прогон вставит `text0`, выдаст инструкцию для `task1.out` (`launch`) и оставит `task2.out`
ждать. Координатор запускает агента одной строкой — «Выполни инструкцию из
`.cortex/work/chapters/prompts/task1.out.md`» — и после его работы снова подаёт сохранённый скрипт.

## Команды

| Команда | Что делает |
| --- | --- |
| `python cortex.py < файл` | прогон; вывод = остаток скрипта, он же в `.cortex/scripts/` |
| `python cortex.py plan < файл` | прогноз без выполнения и без записи |
| `python cortex.py status` | состояние всех скриптов без прогона |
| `python cortex.py outline doc.md` | карта документа |
| `python cortex.py reset СКРИПТ РЕЗУЛЬТАТ` | задание агента — заново |
| `python cortex.py help [слово]` | справка |

## Устройство

| Папка | Что там |
| --- | --- |
| `fvm/` | копия ядра Forth-VM: словарь, типы стековых комментариев, проверка, трасса |
| `mdlib/` | Markdown на markdown-it-py: заголовки, разделы, шаблоны, правки, проверки, карта |
| `cx/` | поток, лексер, фронтенд (строка → код fvm), хранилище `.cortex`, окружение прогона, задания агентам, CLI |
| `cxwords/` | слова языка: `insert` `replace` `delete` `move` `save` `join` `relevel` `agenttask` |

Тестов пока нет.
