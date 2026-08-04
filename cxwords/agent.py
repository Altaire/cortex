"""The word of agent jobs."""
from __future__ import annotations

from typing import Any

from cx.agents import run_agent
from cx.values import TextValue
from fvm.annotations import word


@word("agenttask", "( job:Src|List spec:ResultSpec -- Text )", caps=("agent",), params=("spec",))
def agenttask(env: Any, job: Any, spec: Any) -> TextValue:
    """A job for an agent: a task or [task, inputs…] → a result {name head= has=}.
    Until the result is ready the line waits (launch, running, decide)."""
    return run_agent(env, job, spec)
