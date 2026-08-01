"""Cortex: a deterministic executor of plans for Markdown documents written partly by LLM agents.

    errors.py    errors of the stream (nothing runs), of one line (the line stays), Suspend
    values.py    VM types: references (blocks, results, documents, sections), texts, patterns
    stream.py    the input stream: lines, blocks === name ===, comments, the run marker
    lexer.py     tokens of one line: names, "strings", name"pattern", key=value, [lists], {…}
    frontend.py  declarations, the table of names, lines → fvm code + what each line reads/writes
    store.py     the .cortex folder: scripts, history, work/<script>/…, state, lock
    env.py       one run: documents, staged writes, dependencies between lines, commit
    agents.py    agenttask: states of a job, the agent's instruction, the check of its result
    runner.py    run / plan: lines in order, the remaining script, the summary
    cli.py       python cortex.py run | plan | status | outline | help | reset
"""
