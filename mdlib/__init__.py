"""Markdown library of Cortex (no VM inside): headings, sections, heading patterns, edits,
structure checks, the document map. Built on markdown-it-py.

    structure.py  headings with their sections; patterns "# Раздел 5..." and paths " > "
    edit.py       lines of a document; insert / replace / delete / move; relevel; join
    check.py      structure issues of a document and of an agent's result
    outline.py    the document map: heading tree, words per section, warnings
"""
from mdlib.structure import (Ambiguous, Heading, MdError, NotFound, Segment, HeadingPath, headings, locate,
                             normalize_text, parse_path)
from mdlib.edit import (delete_span, from_lines, insert_at, join_texts, move_span, relevel, replace_span,
                        strip_blank, to_lines)
from mdlib.check import Issue, capture_issue, chunk_issues, issue_counter, structure_issues
from mdlib.outline import outline

__all__ = [
    "Ambiguous", "Heading", "MdError", "NotFound", "Segment", "HeadingPath", "headings", "locate",
    "normalize_text", "parse_path", "delete_span", "from_lines", "insert_at", "join_texts", "move_span",
    "relevel", "replace_span", "strip_blank", "to_lines", "Issue", "capture_issue", "chunk_issues",
    "issue_counter", "structure_issues", "outline",
]
