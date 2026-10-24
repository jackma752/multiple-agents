"""Pre-send context preview (T-1d): what one call sends, part by part.

The Hub composes a call's input (``InvocationSnapshot.task_input``) from
segments - the user's text, handoff frames and contents, separators, and
from T-1f file snippets - and records each segment's length, estimate and
SHA-256 in ``InvocationSnapshot.context_preview``. The parts are an exact
split of ``task_input``: in order they concatenate to it, so their lengths
sum to the text the adapter actually sends, and each part's hash pins which
text belongs to it. :func:`check_preview` enforces that on every snapshot,
and the preview is part of ``payload_hash``: a preview that changes after
approval voids the approval. A hash only identifies a part; the text itself
is still what is sent.

What the preview does NOT claim:

* **Estimates, not counts.** ``estimate`` is :func:`estimate_visible_tokens`
  (``chars_v1``) per part; no tokenizer is installed. The call's visible
  estimate is the sum of the part estimates (upper-leaning).
* **Provider-side context is unknown.** Base instructions, tool definitions,
  environment details and, for ``native_resume``, the thread's history are
  added by the provider and are invisible to the Hub. They are listed as
  ``provider_hidden`` with status ``unknown`` and are never folded into the
  estimate. Earlier real runs under the same adapter configuration may be
  attached as ``observed`` - a historical observation that included that
  run's own (short) input, never this call's prediction.
* **Adapter text.** An adapter that declares it sends ``task_input``
  verbatim (``context_profile()["adds_prompt_text"] is False``) adds no
  text: ``adapter_text`` is ``{"chars": 0}``. Otherwise it is ``unknown``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional

from agenthub.budget import estimate_visible_tokens

PREVIEW_VERSION = 1
ESTIMATOR = "chars_v1"

USER_INPUT = "user_input"
HANDOFF_FRAME = "handoff_frame"      # the Hub's fixed lines around a handed-over reply
HANDOFF_CONTENT = "handoff_content"  # the handed-over reply itself
SEPARATOR = "separator"              # blank lines between blocks
FILE_FRAME = "file_frame"            # the Hub's fixed lines around a file snippet
FILE_SNIPPET = "file_snippet"        # text the Hub read from a referenced file (agenthub.files)
PART_KINDS = frozenset({USER_INPUT, HANDOFF_FRAME, HANDOFF_CONTENT, SEPARATOR, FILE_FRAME, FILE_SNIPPET})
#: Keys a part may carry besides its numbers: where its text came from. File
#: parts carry the root-relative path, the line range and the whole file's hash.
SOURCE_KEYS = frozenset({"message_id", "agent_id", "path", "start_line", "end_line", "file_sha256"})

#: What the provider adds on its own side (labels only; sizes unknown).
PROVIDER_FIXED = "instructions_and_tools"
THREAD_HISTORY = "thread_history"


def compose(segments: Iterable[tuple], *, context_mode: str = "independent",
            profile: Optional[dict] = None) -> tuple[str, dict[str, Any]]:
    """The text to send and its preview, from ``(kind, text[, source])`` segments.

    Empty segments are dropped; ``source`` is a dict of :data:`SOURCE_KEYS`.
    """
    texts, parts = [], []
    for segment in segments:
        kind, text = segment[0], segment[1]
        source = segment[2] if len(segment) > 2 else None
        if kind not in PART_KINDS:
            raise ValueError(f"unknown context part {kind!r}")
        if not isinstance(text, str):
            raise ValueError("context part text must be a string")
        if not text:
            continue
        part: dict[str, Any] = {"kind": kind, "chars": len(text), "estimate": estimate_visible_tokens(text),
                                "sha256": _sha256(text)}
        for key, value in (source or {}).items():
            if key not in SOURCE_KEYS:
                raise ValueError(f"unknown context part source {key!r}")
            part[key] = value
        texts.append(text)
        parts.append(part)
    text = "".join(texts)
    preview = {
        "version": PREVIEW_VERSION, "estimator": ESTIMATOR, "parts": parts,
        "chars": len(text), "estimate": sum(p["estimate"] for p in parts),
        "adapter_text": _adapter_text(profile),
        "provider_hidden": _provider_hidden(context_mode, profile),
    }
    return text, preview


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _adapter_text(profile: Optional[dict]) -> dict[str, Any]:
    if isinstance(profile, dict) and profile.get("adds_prompt_text") is False:
        return {"chars": 0}
    return {"status": "unknown"}


def _provider_hidden(context_mode: str, profile: Optional[dict]) -> dict[str, Any]:
    includes = [PROVIDER_FIXED] + ([THREAD_HISTORY] if context_mode == "native_resume" else [])
    observed = (profile or {}).get("observed") if isinstance(profile, dict) else None
    hidden: dict[str, Any] = {"status": "unknown", "includes": includes}
    if isinstance(observed, list) and observed:
        hidden["observed"] = [dict(o) for o in observed if isinstance(o, dict)]
    return hidden


def check_preview(task_input: str, preview: Any) -> None:
    """Raise ValueError unless ``preview`` is an exact, estimated split of ``task_input``."""
    if not isinstance(preview, dict) or preview.get("version") != PREVIEW_VERSION \
            or preview.get("estimator") != ESTIMATOR:
        raise ValueError("context_preview: unsupported version or estimator")
    parts = preview.get("parts")
    if not isinstance(parts, list):
        raise ValueError("context_preview: parts must be a list")
    offset = 0
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in PART_KINDS:
            raise ValueError("context_preview: unknown part")
        if set(part) - {"kind", "chars", "estimate", "sha256"} - SOURCE_KEYS:
            raise ValueError("context_preview: unknown part field")
        chars = part.get("chars")
        if not isinstance(chars, int) or isinstance(chars, bool) or chars < 1:
            raise ValueError("context_preview: part length must be a positive integer")
        span = task_input[offset:offset + chars]
        if (len(span) != chars or part.get("estimate") != estimate_visible_tokens(span)
                or part.get("sha256") != _sha256(span)):
            raise ValueError("context_preview: part does not match the text sent")
        offset += chars
    if offset != len(task_input) or preview.get("chars") != len(task_input):
        raise ValueError("context_preview: parts do not add up to the text sent")
    if preview.get("estimate") != sum(p["estimate"] for p in parts):
        raise ValueError("context_preview: estimate is not the sum of its parts")
    hidden = preview.get("provider_hidden")
    if not isinstance(hidden, dict) or hidden.get("status") != "unknown":
        raise ValueError("context_preview: provider-side context must be marked unknown")


def part_texts(task_input: str, preview: dict) -> list[str]:
    """Each part's text, sliced from the text sent (for checks and display)."""
    texts, offset = [], 0
    for part in preview["parts"]:
        texts.append(task_input[offset:offset + part["chars"]])
        offset += part["chars"]
    return texts


def visible_estimate(snapshot: dict) -> int:
    """The Hub-visible estimate of a stored snapshot: its preview's, or the whole text's."""
    preview = (snapshot or {}).get("context_preview")
    if isinstance(preview, dict) and isinstance(preview.get("estimate"), int):
        return preview["estimate"]
    return estimate_visible_tokens((snapshot or {}).get("task_input", ""))
