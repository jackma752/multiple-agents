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


@dataclass(frozen=True)
class ConversationTurn:
    text: str
    agent_id: str
    client_request_id: str
    execution_mode: str = "strict"
    context_mode: str = INDEPENDENT

    def __post_init__(self) -> None:
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
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
