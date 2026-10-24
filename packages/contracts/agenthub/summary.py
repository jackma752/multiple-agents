"""Stage summaries and "compress and continue" (T-2, minimal).

A stage summary is what a fresh call needs instead of the whole history:
goal, constraints, what is done, open issues, next steps, and the file
references that matter (``agenthub.files`` syntax). The user writes or edits
it; every save is a new version, and a version never changes.

A call may attach one summary (``ConversationTurn.summary_id``). The Hub
frames it into the call's input as ``summary_frame`` / ``summary_content``
parts of the context preview, and its file references are read and sent as
snippets, exactly like the call's own references. The approval therefore
binds the summary text (its hash) and the snippets. Nothing in a summary
grants anything: "approved" written in a summary is text, not an approval.

A draft can be produced by an ordinary call that includes the conversation's
recent history (``include_history``): it goes through the same approval and
budget as any call, and its reply is parsed back into the fields
(:func:`parse_summary_text`) for the user to edit and save. Provider-native
compaction is not used.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from agenthub.files import MAX_REFS, parse_ref

#: (field, heading) in the order they are written and read back.
SECTIONS = (("goal", "Goal"), ("constraints", "Constraints"), ("done", "Done"),
            ("open_issues", "Open issues"), ("next_steps", "Next steps"))
FILES_HEADING = "Files"
MAX_FIELD_CHARS = 8_000
MAX_SUMMARY_CHARS = 20_000
#: History a drafting call may carry (most recent messages that fit).
MAX_HISTORY_CHARS = 30_000

#: The fixed request a drafting call sends with the history.
SUMMARY_INSTRUCTION = (
    "Write a stage summary of the conversation history in this message so that work can continue in a fresh "
    "session without it. Use exactly these headings, each on its own line followed by its content: "
    "Goal:, Constraints:, Done:, Open issues:, Next steps:, Files:. Under Files:, list only file "
    "references that are needed, one per line as '- path' or '- path:L10-20'. Keep decisions, "
    "constraints and anything marked unverified or refused. Do not use tools."
)


@dataclass(frozen=True)
class StageSummary:
    goal: str
    constraints: str = ""
    done: str = ""
    open_issues: str = ""
    next_steps: str = ""
    file_refs: tuple = ()

    def __post_init__(self) -> None:
        if isinstance(self.file_refs, list):
            object.__setattr__(self, "file_refs", tuple(self.file_refs))
        values = [getattr(self, name) for name, _ in SECTIONS]
        if not all(isinstance(v, str) for v in values):
            raise ValueError("summary fields must be strings")
        if not self.goal.strip():
            raise ValueError("a summary needs a goal")
        if any(len(v) > MAX_FIELD_CHARS for v in values) or sum(map(len, values)) > MAX_SUMMARY_CHARS:
            raise ValueError(f"summary fields: at most {MAX_FIELD_CHARS} characters each, "
                             f"{MAX_SUMMARY_CHARS} in total")
        if not isinstance(self.file_refs, tuple) or len(self.file_refs) > MAX_REFS:
            raise ValueError(f"summary file_refs: up to {MAX_REFS} references")
        for spec in self.file_refs:
            parse_ref(spec)

    def body(self) -> str:
        """The summary as sent: its non-empty sections under fixed headings."""
        blocks = [f"{heading}:\n{getattr(self, name).strip()}" for name, heading in SECTIONS
                  if getattr(self, name).strip()]
        if self.file_refs:
            blocks.append(f"{FILES_HEADING}:\n" + "\n".join(f"- {spec}" for spec in self.file_refs)
                          + "\n(their selected lines follow below)")
        return "\n\n".join(blocks)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["file_refs"] = list(self.file_refs)
        return data


def summary_frames(version: int, summary_id: int) -> tuple[str, str]:
    return (f"--- Stage summary v{version} (Hub summary {summary_id}) ---\n", "\n--- End of stage summary ---")


_HEADING = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*(goal|constraints|done|open issues|next steps|files)\s*:?\s*(?:\*\*)?\s*:?\s*(.*)$",
                      re.IGNORECASE)
_KEYS = {heading.casefold(): name for name, heading in SECTIONS}
_KEYS[FILES_HEADING.casefold()] = "file_refs"


def parse_summary_text(text: str) -> dict[str, Any]:
    """Fields read back from a drafted reply (headings as in SUMMARY_INSTRUCTION).

    Text before the first heading is ignored; file lines that are not valid
    references are dropped. The result is a draft for the user to edit; it
    is not validated as a :class:`StageSummary` here.
    """
    fields: dict[str, list[str]] = {}
    current = None
    for line in (text or "").splitlines():
        match = _HEADING.match(line)
        if match:
            current = _KEYS[match.group(1).casefold()]
            fields.setdefault(current, [])
            if match.group(2).strip():
                fields[current].append(match.group(2).strip())
        elif current is not None:
            fields[current].append(line)
    out: dict[str, Any] = {name: "\n".join(fields.get(name, [])).strip() for name, _ in SECTIONS}
    refs = []
    for line in fields.get("file_refs", []):
        spec = line.strip().lstrip("-*").strip().strip("`")
        if not spec or spec.startswith("("):
            continue
        try:
            parse_ref(spec)
        except ValueError:
            continue
        if spec not in refs:
            refs.append(spec)
    out["file_refs"] = refs[:MAX_REFS]
    return out
