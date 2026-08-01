"""The vocabulary of Cortex: Python primitives annotated with @word (fvm), loaded by the VM.

    markdown.py   insert replace delete move save — writes (right "write", the last word of a line)
                  join relevel — texts
    agent.py      agenttask — a job for an agent

Every primitive takes the run environment `env` first (bound by the VM, see cx/env.py); its
other inputs come from the stack and from key=value parameters (params=…), in the order of the
stack comment.
"""
