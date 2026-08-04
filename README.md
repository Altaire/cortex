# Cortex

**Cortex is a small, deterministic executor for plans that build and edit Markdown documents with the help of LLM agents.**

You describe the work as a short script: "insert this text after Chapter 1", "let an agent write Chapter 3 and put it after Chapter 2", "when Chapter 3 is ready, let another agent write Chapter 4 from it". Cortex runs everything it can run right now. Everything it cannot run yet stays in the script, with a status that says why.

Cortex never calls an LLM itself. It only does the deterministic work: finding sections, inserting and replacing text, checking structure and tracking state. The **coordinator** does the rest. The coordinator is a person or an LLM such as Claude Code: it starts the agents Cortex asks for and feeds the script back in.

## Why

Writing a large document with several agents is mostly bookkeeping:

- which parts are done, which are being written, which wait for others;
- where exactly each new piece goes;
- what to do when an agent's output is broken;
- how to resume after the session is gone.

An LLM coordinator is bad at keeping all of that in its context, and loses it when the context is compacted. Cortex keeps all of it in plain files instead:

- **The plan lives in a file, not in the coordinator's memory.** Every run prints the *remaining* script, so the next step is always visible.
- **Agents' results never pass through the coordinator's context.** An agent writes its result to a file, and Cortex inserts it into the document.
- **Edits are exact.** Sections are addressed by heading (`"# Chapter 3..."`). Cortex refuses edits that would break the heading structure or make a reference ambiguous.
- **Work survives restarts.** The state is in `.cortex/`, and any session can continue with one command.

## The pipeline

```
          ┌───────────── script (stdin) ─────────────┐
          ▼                                          │
     cortex.py ── does everything it can now ──► document.md
          │
          ├─► stdout: the remaining script + a summary
          │          (done / launch / running / waiting / decide)
          └─► .cortex/: agent instructions, state, history
                │
   coordinator reads "launch: X → prompts/X.md" and starts an agent:
   "Follow the instructions in .cortex/work/<script>/prompts/X.md"
                │
   the agent writes status → result → message (STATUS: done)
                │
          └──────── coordinator runs cortex.py again ───────┘
                    until the summary says "status: done"
```

## Install

```bash
pip install -r requirements.txt     # markdown-it-py
python cortex.py help
```

Python 3.9+.

## Example

`book.md` before the run:

```markdown
# Chapter 1. Introduction

Intro.

# Chapter 2. Methods

Methods.
```

`plan.cortex` holds the script lines and the named text blocks in one stream:

```
script book
"book.md" alias main

note insert to=main after="# Chapter 1..."
task3 agenttask {task3.out head="# Chapter 3..." has=["# Chapter 3... > ## Summary"]} insert to=main after="# Chapter 2..."
[task4, task3.out] agenttask {task4.out head="# Chapter 4..."} insert to=main before=END

=== context ===
Write concisely, in a neutral academic tone.
=== / ===
=== note ===
A short note added to the introduction.
=== / ===
=== task3 ===
Read chapter 2 and write chapter 3 (Results).
=== / ===
=== task4 ===
Using chapter 3, write chapter 4 (Discussion).
=== / ===
```

What the lines mean:

- `"book.md" alias main` gives the file a name.
- `note insert …` inserts the block `note` after the section whose heading starts with "Chapter 1".
- `task3 agenttask {…} insert …` asks an agent to do `task3`. The result must start with a `# Chapter 3…` heading and contain `## Summary`. When the result is ready, Cortex inserts it after Chapter 2.
- `[task4, task3.out] agenttask …` gives the agent the task `task4` plus the result of `task3` as input. This line waits until that result exists.
- The `context` block is added to every agent's instructions.

### Run 1: the script comes in on standard input

```bash
python cortex.py < plan.cortex
```

stdout (the remaining script; the same text is saved to `.cortex/scripts/book.cortex`):

```
script book
"book.md" alias main

\ run 1 · 2026-11-01 03:11
\ done:     note → main after "# Chapter 1..."
\ launch:   task3.out → .cortex/work/book/prompts/task3.out.md
\ waiting:  task4.out (waits for task3.out)

task3 agenttask {task3.out head="# Chapter 3..." has=["# Chapter 3... > ## Summary"]} insert to=main after="# Chapter 2..."
[task4, task3.out] agenttask {task4.out head="# Chapter 4..."} insert to=main before=END
```

stderr:

```
cortex: saved    .cortex/scripts/book.cortex
cortex: history  .cortex/history/book/001/
cortex: next     python cortex.py < .cortex/scripts/book.cortex
```

How to read run 1:

- `done:` — the note is already in `book.md`, and its line has left the script.
- `launch:` — start an agent and tell it: *"Follow the instructions in `.cortex/work/book/prompts/task3.out.md`"*. The instructions contain the context, the task and the inputs, plus the files to write: a start mark, the result, and a message whose first line is `STATUS: done` or `STATUS: failed`.
- `waiting:` — `task4.out` needs `task3.out`, which does not exist yet.

The instructions Cortex wrote for the agent (`prompts/task4.out.md` from the next run):

```markdown
# Agent job: task4.out

Workspace: `/path/to/workspace`. All paths below are relative to it. Change only the files of steps 1, 3 and 4.

## Shared context

Write concisely, in a neutral academic tone.

## Task

Using chapter 3, write chapter 4 (Discussion).

## Inputs

- `.cortex/work/book/results/task3.out.md` — result task3.out

## Steps

1. First of all, create the file `.cortex/work/book/status/task4.out` (any content, for example the start time).
2. Read the inputs and do the task.
3. Write the result to `.cortex/work/book/results/task4.out.md` — only the text for the document, nothing addressed to the coordinator. Requirements:
   - it starts with a `#` heading whose text starts with "Chapter 4" (for example: `# Chapter 4 …`); this is the only `#` heading in the result;
   - heading levels do not skip: inside `#` comes `##`, inside `##` comes `###`;
   - no lines like `=== … ===` and no `@@@`.
4. As the very last step, write a message to the coordinator to `.cortex/work/book/messages/task4.out.md`:
   the first line is `STATUS: done` or `STATUS: failed`;
   then: what was done, what problems there were, what you suggest changing in the plan.
5. Do not change any other files: only Cortex edits the documents.
```

Lines that start with `\` are comments. Cortex writes the summary as comments and ignores them on input.

### Run 2: the agent has finished, so feed the saved script back

```bash
python cortex.py < .cortex/scripts/book.cortex
```

```
\ run 2 · 2026-11-01 03:11
\ done:     task3.out → main after "# Chapter 2..."
\ launch:   task4.out → .cortex/work/book/prompts/task4.out.md
\ message:  task3.out → .cortex/work/book/messages/task3.out.md

[task4, task3.out] agenttask {task4.out head="# Chapter 4..."} insert to=main before=END
```

Cortex first checked the result. It has the right heading and the required `## Summary`, and the heading levels skip nothing. Then Cortex inserted it. `message:` points to the agent's note for the coordinator, for example a proposal to change the plan.

### Run 3: the second agent has finished

```
\ run 3 · 2026-11-01 03:11
\ status: done
\ done:     task4.out → main before END
```

`python cortex.py outline book.md` now prints the map of the document:

```
book.md · 31 words · 5 headings
# Chapter 1. Introduction       12
# Chapter 2. Methods             5
# Chapter 3. Results             9
  ## Summary                     4
# Chapter 4. Discussion          5
```

## When something goes wrong

| Situation | What Cortex does |
| --- | --- |
| The agent's result breaks the contract (no required heading, a skipped level, service lines) | Moves the attempt to `attempts/` with the reason and shows `launch` again ("attempt 2 of 3: reason"). The reason is added to the agent's instructions. |
| The attempts are used up | Shows `decide` with the reason. Fix the task, or run `python cortex.py reset <script> <result>`. |
| The agent started but sent no message within the timeout | Shows `decide`: check the agent or reset the job. |
| The task text or an input changed | Archives the old result and shows `launch` again. |
| An insert would skip a heading level, or would swallow the following subsections | Leaves the line in the script with `decide` and does not change the document. |
| A new heading would make another line's reference ambiguous | Refuses the write with `decide`. |
| A line refers to a section that an earlier line will create | The line waits. |
| The script has an error (unknown word or name, a missing parameter, two writes on one line…) | Lists every error with its line number and runs nothing. |
| An old or original version of the script is fed again | Refuses it, so that nothing is inserted twice. |

Errors name the line and suggest a fix. For example:

```
cortex: error: line 3: unknown word "insеrt": Cyrillic letters (е) — did you mean insert?
cortex: error: line 4: unknown name or word "mian" — did you mean main?
cortex: nothing was executed
```

A failed attempt shows up in the summary like this:

```
\ launch:   bad.out → .cortex/work/t/prompts/bad.out.md (attempt 2 of 2: the agent reported a failure: No data.)
\ waiting:  line 10 · jump (waits for line 9: writes to d go in line order)
```

## Language in one screen

```
script NAME                               name of the script
"path.md" alias NAME                      an existing file
"path.md" create NAME                     a file, created empty if missing
set attempts=3 timeout=30m autocommit=off
=== NAME === … === / ===                  a text block (block "context" goes to every agent)

NAME                                      a block, an agent's result or a document
"text"                                    literal text (inside an agent's input list: a file path)
NAME"# Heading..."                        a section of a document, block or result
"# Chapter 3..."                          heading that starts with "Chapter 3" (up to a word boundary)
"# Chapter 3. Results"                    exact heading
"# Chapter 3... > ## Summary"             a subsection inside a section
[task, input1, input2]                    the task for an agent, then its inputs
{result head="# …" has=["# … > ## …"]}    what the agent's result must look like
START  END                                beginning and end of a document
```

| Word | What it does |
| --- | --- |
| `insert to= after=/before=` | insert text |
| `replace to= section=` | replace a section, including its heading |
| `delete to= section=` | delete a section |
| `move to= section= after=/before=` | move a section |
| `save to="file"` | write text to a separate file |
| `join` | a list of texts → one text |
| `relevel top=N` | shift the headings so that the top one is level N |
| `agenttask {…}` | a job for an agent; the line waits until the result is ready |

Rules: one line is one unit of work, done entirely or left in the script. A line has at most one write, and it is the last word. Writes to one document happen in line order.

## Commands

| Command | What it does |
| --- | --- |
| `python cortex.py < file` | run |
| `python cortex.py plan < file` | forecast only: nothing is executed or written |
| `python cortex.py status` | the state of every script, without running it |
| `python cortex.py outline doc.md` | the heading map of a document |
| `python cortex.py reset SCRIPT RESULT` | restart an agent's job |
| `python cortex.py help [word]` | help |
| `--root DIR` | the workspace (default: the current folder) |

## Files

```
.cortex/
  scripts/<script>.cortex      the current script, the one to feed to the next run
  history/<script>/NNN/        input, output, step log, trace of every run
  work/<script>/               blocks, prompts, inputs, results, status, messages, attempts, state.json
```

## Code layout

| Folder | Contents |
| --- | --- |
| `fvm/` | a small Forth-style VM: words, typed stack comments, load-time checks, traces |
| `mdlib/` | Markdown on markdown-it-py: headings, sections, patterns, edits, structure checks, outline |
| `cx/` | stream, lexer, compiler (script line → VM code), `.cortex` store, run environment, agent jobs, CLI |
| `cxwords/` | the words of the language |

There are no tests yet.
