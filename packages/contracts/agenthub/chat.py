"""Persistent Hub chat contracts.

Local history is not provider session resume. A turn continues a provider
thread only when it explicitly asks for ``context_mode=native_resume``; the
thread id is then bound into the approved snapshot.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Optional

from agenthub.contracts import CONTEXT_MODES, INDEPENDENT
from agenthub.files import MAX_REFS, parse_ref
from agenthub.output import OUTPUT_STYLES


CONTEXT_MODE = INDEPENDENT
MAX_MESSAGE_CHARS = 100_000
#: Manual handoff: at most this many earlier replies, and this much of their text.
MAX_HANDOFF_MESSAGES = 3
MAX_HANDOFF_CHARS = 20_000


@dataclass(frozen=True)
class ConversationTurn:
    text: str
    agent_id: str
    client_request_id: str
    execution_mode: str = "strict"
    context_mode: str = INDEPENDENT
    #: Earlier assistant replies of this conversation to pass on, chosen by the user.
    handoff_message_ids: tuple = ()
    #: Files (or line ranges) the Hub reads and sends with this message
    #: (``agenthub.files`` syntax), chosen by the user.
    file_refs: tuple = ()
    #: A stage summary sent with this message instead of history (``agenthub.summary``):
    #: one of this conversation's, or of the conversation it was compressed from.
    summary_id: Optional[int] = None
    #: Send this conversation's recent messages with this one (summary drafting).
    include_history: bool = False
    #: Output preference added as a visible hint (``agenthub.output``); "" = none.
    output_style: str = ""

    def __post_init__(self) -> None:
        if self.output_style not in OUTPUT_STYLES:
            raise ValueError("output_style must be '', 'concise' or 'detailed'")
        if self.summary_id is not None and (type(self.summary_id) is not int or self.summary_id < 1):
            raise ValueError("summary_id must be a positive integer")
        if type(self.include_history) is not bool:
            raise ValueError("include_history must be a boolean")
        ids = self.handoff_message_ids
        if (not isinstance(ids, tuple) or len(ids) > MAX_HANDOFF_MESSAGES
                or len(set(ids)) != len(ids)
                or not all(type(i) is int and i > 0 for i in ids)):
            raise ValueError(f"handoff_message_ids: up to {MAX_HANDOFF_MESSAGES} distinct message ids")
        if not isinstance(self.file_refs, tuple) or len(self.file_refs) > MAX_REFS:
            raise ValueError(f"file_refs: up to {MAX_REFS} references")
        for spec in self.file_refs:
            parse_ref(spec)
        for value, maximum in (
            (self.text, MAX_MESSAGE_CHARS),
            (self.agent_id, 200),
            (self.client_request_id, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("turn fields must be nonblank strings within size limits")
        if self.execution_mode not in {"strict", "supervised"}:
            raise ValueError("execution_mode must be strict or supervised")
        if self.context_mode not in CONTEXT_MODES:
            raise ValueError("context_mode must be independent or native_resume")

    def request_hash(self) -> str:
        """Deduplication hash, separate from the policy-bound approval hash."""
        data = asdict(self)
        if data["context_mode"] == INDEPENDENT:
            # Keys stored before context_mode existed must still replay.
            del data["context_mode"]
        if data["handoff_message_ids"]:
            data["handoff_message_ids"] = list(data["handoff_message_ids"])
        else:
            del data["handoff_message_ids"]  # likewise for keys stored before handoff existed
        if data["file_refs"]:
            data["file_refs"] = list(data["file_refs"])
        else:
            del data["file_refs"]
        for key in ("summary_id", "include_history", "output_style"):  # likewise, absent unless used
            if not data[key]:
                del data[key]
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
