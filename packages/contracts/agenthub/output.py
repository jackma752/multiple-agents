"""Output preference for a call (T-3, minimal).

The user may ask for a ``concise`` or ``detailed`` answer. The Hub adds a
fixed, visible instruction (context part ``output_hint``) just before the
user's message; the approval shows and binds it like any other part.

It is a SOFT target: no adapter enforces an output limit, so a longer answer
is not truncated and is never reported as cut short. Model or reasoning
effort switching is not done here (it needs per-adapter capability checks).
"""

OUTPUT_STYLES = ("", "concise", "detailed")

OUTPUT_HINTS = {
    "concise": ("Output preference: be concise. Lead with the result, then only what was verified and any "
                "blockers. Do not restate the request or the context above."),
    "detailed": ("Output preference: be thorough. Give the result, the steps or reasoning that matter, what "
                 "was verified and how, and any open risks."),
}
