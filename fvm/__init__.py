"""Forth VM — a copy of the core of the knowledge-base server's VM, used by Cortex.

    types.py        type expressions of stack comments, registry, run-time types
    words.py        words, signatures, compiled instructions
    compiler.py     lexer and compiler of .fs files (library definitions)
    checker.py      load-time check of stack effects
    annotations.py  @word / @artifact: how Python primitives and types enter the VM
    vm.py           the VM: dictionary, loading of a words/ tree, instances, interpreter
    render.py       execution memory and the program as text (program.log)
    trace.py        traces and run records
    session.py      client sessions (kept for vm.py; not used by Cortex)

Changes against the original: @word(params=…) — inputs given by name; Run(run_dir=…) and
console mode "log"; forth_literal() lets a value with forth() write itself. The MCP bridge
and the server CLI are not part of this copy.
"""
