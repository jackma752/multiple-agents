"""How a task's status is derived from the calls made for it.

``tasks.status`` used to be written once at creation and never touched again, so a
task whose call had already succeeded still read ``draft``. That is not just a
display problem: the column is queryable, so any later report, filter or dashboard
built on it would have been wrong.

This module is the ONE rule. It is a pure function of the task's invocation
statuses, so the stored row, the read model and the tests cannot disagree.

The result is a *summary*, not a replacement for per-invocation states: a task
with one success and one failure reports ``failed`` (something needs attention),
while each call keeps its own status for the console and the API.

Priority, first match wins - the list is ordered from "needs a human now" to
"finished":

===================  ==========================================================
``unknown``           a call's true state could not be confirmed (needs a human)
``running``           a call is starting or running
``queued``            an approved call is waiting for the scheduler
``awaiting_approval`` a call is waiting for the user
``failed``            a call failed or timed out
``canceled``          a call was canceled or rejected
``succeeded``         every call succeeded
``draft``             no call reached the adapter yet (none, or only draft/invalid)
===================  ==========================================================
"""

from __future__ import annotations

from typing import Any, Iterable

from agenthub.contracts import InvocationStatus

#: Ordered priority list; the first group that matches decides the summary.
PRIORITY: tuple[tuple[str, frozenset[str]], ...] = (
    ("unknown", frozenset({InvocationStatus.UNKNOWN.value})),
    (
        "running",
        frozenset({InvocationStatus.STARTING.value, InvocationStatus.RUNNING.value}),
    ),
    ("queued", frozenset({InvocationStatus.QUEUED.value})),
    ("awaiting_approval", frozenset({InvocationStatus.AWAITING_APPROVAL.value})),
    (
        "failed",
        frozenset({InvocationStatus.FAILED.value, InvocationStatus.TIMED_OUT.value}),
    ),
    (
        "canceled",
        frozenset({InvocationStatus.CANCELED.value, InvocationStatus.REJECTED.value}),
    ),
    ("succeeded", frozenset({InvocationStatus.SUCCEEDED.value})),
)

#: Nothing has been asked of an adapter yet.
DEFAULT_STATUS = InvocationStatus.DRAFT.value


def derive_task_status(statuses: Iterable[Any]) -> str:
    """Summarise a task's invocation statuses into one task status."""
    present = {str(s or "") for s in statuses}
    for name, members in PRIORITY:
        if present & members:
            return name
    return DEFAULT_STATUS


def explain() -> str:
    """The rule as text, for docs and diagnostics."""
    return " > ".join(name for name, _ in PRIORITY) + f" > {DEFAULT_STATUS}"
