"""Persistent Hub chat contracts.

Local history is not provider session resume. A turn continues a provider
thread only when it explicitly asks for ``context_mode=native_resume``; the
thread id is then bound into the approved snapshot.
"""

from dataclasses import asdict, dataclass
import hashlib
import json

from agenthub.contracts import CONTEXT_MODES, INDEPENDENT


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

    def __post_init__(self) -> None:
        ids = self.handoff_message_ids
        if (not isinstance(ids, tuple) or len(ids) > MAX_HANDOFF_MESSAGES
                or len(set(ids)) != len(ids)
                or not all(type(i) is int and i > 0 for i in ids)):
            raise ValueError(f"handoff_message_ids: up to {MAX_HANDOFF_MESSAGES} distinct message ids")
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
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
