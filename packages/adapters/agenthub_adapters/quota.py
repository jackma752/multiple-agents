"""Normalize provider *error* evidence for P4.6. Never inspect successful replies.

Only explicit quota/rate-limit evidence becomes a wait. Reset dates must be
machine-readable; natural-language clock times are deliberately not guessed.
Returned metadata contains no original error text, prompts or credentials.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

_LIMIT = re.compile(r"rate[_ -]?limit|usage[_ -]?limit|quota[_ -]?(?:exceeded|exhausted)|"
                    r"(?:exceeded|exhausted).{0,20}quota|too many requests|"
                    r"you(?:'ve| have) (?:hit|reached) your limit", re.I)


def quota_error(error: Any, *, at: Optional[dt.datetime] = None) -> Optional[dict]:
    at = at or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    fields, strings = {}, []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, dict):
            for key, val in value.items():
                fields[str(key).lower().replace("-", "_")] = val
                walk(val, depth + 1)
        elif isinstance(value, list):
            for val in value[:20]:
                walk(val, depth + 1)
        elif isinstance(value, str):
            strings.append(value[:2000])

    walk(error)
    status = fields.get("status_code", fields.get("http_status", fields.get("code")))
    if status not in (429, "429") and not any(_LIMIT.search(s) for s in strings):
        return None
    reset = None
    for key in ("resets_at", "reset_at", "resetat", "resetsat", "reset_time"):
        value = fields.get(key)
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reset = dt.datetime.fromtimestamp(value / 1000 if value > 1e12 else value, dt.timezone.utc)
            elif isinstance(value, str):
                reset = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if reset.tzinfo is None:
                    reset = None  # a timezone-less clock time is not reliable
            if reset is not None:
                reset = reset.astimezone(dt.timezone.utc).replace(tzinfo=None)
                break
        except (ValueError, OverflowError, OSError):
            reset = None
    if reset is None:
        for key in ("retry_after", "retry_after_seconds", "retryafterseconds"):
            try:
                seconds = float(fields[key])
                if 0 <= seconds <= 31 * 86400:
                    reset = at + dt.timedelta(seconds=seconds)
                    break
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
    return {"kind": "provider_quota", "reset_at": reset.isoformat() + "Z" if reset else None}
