"""Conservative Markdown math delimiters for generated explanatory prose.

This only adds/converts delimiters; it never repairs a formula or interprets a
paper. Source quotations should normally bypass this module entirely. Code,
quoted text, links, URLs, paths and existing math are also kept opaque here.
"""
from __future__ import annotations

import re


# This is deliberately a small recognizer, not a general TeX parser. Unknown
# commands remain untouched instead of guessing whether they are paths/text.
_COMMANDS = set("""
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi
varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbb mathcal mathrm mathsf mathit mathbf boldsymbol operatorname text
frac dfrac tfrac sqrt overline underline hat widehat bar tilde widetilde vec
dot ddot sum prod int iint oint lim min max argmin argmax sup inf log ln exp
sin cos tan det diag tr Pr E Var Cov const in notin subset subseteq supset
supseteq le leq ge geq ne neq approx sim simeq equiv propto to mapsto rightarrow
leftarrow infty partial nabla ell hbar top bot perp cdot times otimes oplus
mid vert Vert lvert rvert lVert rVert dots ldots cdots vdots ddots pm mp
succ prec succeq preceq iff implies forall exists nexists land lor neg not
cup cap setminus emptyset
""".split())

# Non-mathematical Markdown is masked before looking for math. In particular,
# money is masked before dollar-delimited math, so "$10 and $20" is not math.
_QUOTES = re.compile(r"“[^”\n]*”|‘[^’\n]*’|(?<!\w)\"[^\"\n]+\"|(?<!\w)'[^'\n]+'(?!\w)")
_LINK = re.compile(r"!?\[[^\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)")
_OPAQUE = [
    re.compile(r"(?ms)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*(?:\n|$)|\Z)"),
    re.compile(r"(`+)[^\n]*?\1"),
    re.compile(r"(?m)^[ \t]{0,3}>[^\n]*(?:\n[ \t]{0,3}>[^\n]*)*"),
    _QUOTES,
    _LINK,
    re.compile(r"!?\[[^\]\n]*\]\[[^\]\n]*\]"),
    re.compile(r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*"),
    re.compile(r"<(?:https?://|mailto:)[^>\n]+>"),
    re.compile(r"(?:https?://|file://|mailto:)[^\s<>\"]+"),
    re.compile(r"(?<![\w\\])[A-Za-z]:[\\/][^\s<>\"|]+"),
    re.compile(r"\\\\[A-Za-z0-9][^\s<>\"|]+"),
    re.compile(r"(?<![\w\\])(?:[\w.-]+[\\/])+[\w.-]+\.(?:md|txt|py|json|pdf|csv|tex|html|exe|png|js)\b", re.I),
    re.compile(r"(?<![\w\\])[\w.-]+\.(?:md|txt|py|json|pdf|csv|tex|html|exe|png|js)\b", re.I),
    re.compile(r"\\\$"),
    re.compile(r"(?<![\\$])\$\d[\d,]*(?:\.\d+)?(?![\w$])"),
]
_MARKED = re.compile(
    r"\\\[(?P<block>[\s\S]*?)\\\]|\\\((?P<inline>[\s\S]*?)\\\)"
    r"|(?P<existing>\$\$[\s\S]*?\$\$|(?<![\\$])\$(?!\$)[^$\n]+\$(?!\$))"
)
_COMMAND = re.compile(r"\\([A-Za-z]+)")
_BARE_BASE = re.compile(r"[A-Za-z\u0370-\u03ff]")
_CJK = re.compile(r"[\u3400-\u9fff]")
_ARG_COUNTS = {name: 1 for name in """
mathbb mathcal mathrm mathsf mathit mathbf boldsymbol operatorname text sqrt
overline underline hat widehat bar tilde widetilde vec dot ddot
""".split()} | {name: 2 for name in ("frac", "dfrac", "tfrac")}
_SIZED_DELIMITER = re.compile(r"\\(left|right)\s*(?:\\[{}]|[()[\]|.]|\\(?:langle|rangle|lVert|rVert|lvert|rvert))")


def _group_end(text: str, start: int, opening: str = "{", closing: str = "}") -> int | None:
    if start >= len(text) or text[start] != opening:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opening and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif text[i] == closing and (i == 0 or text[i - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _delimited_end(text: str, start: int) -> int | None:
    r"""An escaped set or a paired \left/\right group, keeping every token."""
    if text.startswith(r"\{", start):
        depth = 0
        for match in re.finditer(r"\\[{}]", text[start:]):
            depth += 1 if match[0] == r"\{" else -1
            if depth == 0:
                return start + match.end()
        return None
    first = _SIZED_DELIMITER.match(text, start)
    if first is None or first[1] != "left":
        return None
    depth = 0
    for match in _SIZED_DELIMITER.finditer(text, start):
        depth += 1 if match[1] == "left" else -1
        if depth == 0:
            return match.end()
    return None


def _math_application_end(text: str, start: int) -> int | None:
    """Distinguish [mathematical expression](X_s) from a Markdown link."""
    close = _group_end(text, start, "[", "]")
    if close is None:
        return None
    end = _group_end(text, close, "(", ")")
    if end is None:
        return None
    label, target = text[start + 1:close - 1], text[close + 1:end - 1]
    if not re.search(r"\\[A-Za-z]+|[_^]", label) or not target.strip():
        return None
    # A URL/path/anchor, CJK label, ordinary English word or Markdown syntax
    # makes this an actual link. Only clearly mathematical labels qualify.
    if re.search(r"[/.:#]", target):
        return None
    for value in (label, target):
        if any(m[1] not in _COMMANDS for m in _COMMAND.finditer(value)):
            return None
        bare = _COMMAND.sub("", value)
        if re.search(r"[^A-Za-z0-9\u0370-\u03ff_{}()[\],+*/=<>|!\-\\ \t]", bare):
            return None
        if any(len(word) > 1 for word in re.findall(r"[A-Za-z]+", bare)):
            return None
    return end


def _command_end(text: str, start: int, *, known: bool = True, depth: int = 0) -> int | None:
    if depth > 16:
        return None
    match = _COMMAND.match(text, start)
    if match is None or known and match[1] not in _COMMANDS:
        return None
    end = match.end()
    if match[1] == "sqrt" and end < len(text) and text[end] == "[":
        end = _group_end(text, end, "[", "]") or end
    arguments = 0
    while end < len(text):
        # Spaces inside a macro invocation are allowed, never swallow the next
        # prose word just because a command ended before it.
        following = end
        while following < len(text) and text[following] in " \t":
            following += 1
        group = _group_end(text, following)
        if group is None:
            break
        end = group
        arguments += 1
    for _ in range(max(0, _ARG_COUNTS.get(match[1], 0) - arguments)):
        following = end
        while following < len(text) and text[following] in " \t":
            following += 1
        if following >= len(text):
            return None  # never emit an isolated accent/font/fraction macro
        group = _group_end(text, following)
        command = _command_end(text, following, depth=depth + 1)
        if group is not None:
            end = group
        elif command is not None:
            end = command
        elif re.fullmatch(r"[A-Za-z0-9\u0370-\u03ff]", text[following]):
            end = following + 1
            if end < len(text) and re.fullmatch(r"[A-Za-z\u0370-\u03ff]", text[end]):
                return None  # do not consume the first letter of an English word
        else:
            return None
    return end


def _scripts_end(text: str, end: int) -> int:
    while end < len(text):
        cursor = end
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        if cursor >= len(text) or text[cursor] not in "_^":
            break
        cursor += 1
        group = _group_end(text, cursor)
        command = _command_end(text, cursor)
        if group is not None:
            end = group
        elif command is not None:
            end = command
        else:
            # A long snake_case identifier is more likely prose/code; generated
            # multi-letter TeX indices can be explicit braces: x_{initial}.
            part = re.match(r"(?:[A-Za-z\u0370-\u03ff]\d*|\d+)(?![A-Za-z0-9_\u0370-\u03ff])", text[cursor:])
            if part is None:
                break
            end = cursor + part.end()
    return end


def _atom(text: str, start: int) -> tuple[int, bool] | None:
    application = _math_application_end(text, start)
    if application is not None:
        return _scripts_end(text, application), True
    delimited = _delimited_end(text, start)
    if delimited is not None:
        return _scripts_end(text, delimited), True
    command = _command_end(text, start)
    if command is not None:
        return _scripts_end(text, command), True
    if _BARE_BASE.match(text, start):
        end = _scripts_end(text, start + 1)
        if end < len(text) and re.fullmatch(r"[A-Za-z0-9_\u0370-\u03ff]", text[end]):
            return None  # an ordinary word, not a one-letter variable
        return end, end > start + 1 or bool(re.fullmatch(r"[\u0370-\u03ff]", text[start]))
    number = re.match(r"\d+(?:\.\d+)?", text[start:])
    if number is None:
        return None
    end = start + number.end()
    if end < len(text) and _BARE_BASE.match(text, end):
        factor = _atom(text, end)
        if factor is not None:
            return factor[0], True  # 2A_t is implicit multiplication, not a word
    return end, False


def _expression_end(text: str, start: int) -> int | None:
    """Join an unmistakable contiguous expression, without absorbing prose."""
    first = _atom(text, start)
    if first is None:
        return None
    end, certain = first
    while end < len(text):
        delimited = _delimited_end(text, end)
        if delimited is not None:
            end, certain = _scripts_end(text, delimited), True
            continue
        command = _command_end(text, end)
        if command is not None:
            end, certain = _scripts_end(text, command), True
            continue
        if text[end] in "([":
            opening = text[end]
            group = _group_end(text, end, opening, ")" if opening == "(" else "]")
            if group is not None:
                body = text[end:group]
                # Function arguments may contain TeX/indices/numbers. Never
                # absorb prose, quotes, URLs or inline Markdown into new math.
                if (not _CJK.search(body) and not re.search(r"[`\"'$]|://", body)
                        and all(m[1] in _COMMANDS or m[1] in ("left", "right", "langle", "rangle")
                                for m in _COMMAND.finditer(body))):
                    certain = certain or bool(re.search(r"[\\_^=\u0370-\u03ff]", body))
                    end = _scripts_end(text, group)
                    continue
        if text[end] in "=+-*/<>|":
            following = _atom(text, end + 1)
            if following is not None:
                end, _ = following
                certain = True
                continue
        if text[end].isdigit():
            following = _atom(text, end)
            if following is not None:
                end, _ = following
                continue
        if certain and text[end - 1] in ")]}":
            following = _atom(text, end)
            if following is not None:
                end, _ = following  # x^\top(B^{-1}-2A_t)x keeps its last factor
                continue
        break
    return end if certain else None


def _bare(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        end = None
        before = text[i - 1] if i else ""
        boundary = not (before and (re.fullmatch(r"[A-Za-z0-9_\u0370-\u03ff]", before) or before in "\\$"))
        if boundary:
            end = _expression_end(text, i)
            if end is None and text[i] == "\\":
                unknown = _command_end(text, i, known=False)
                if unknown is not None:
                    out.append(text[i:unknown])
                    i = unknown
                    continue
        if end is not None and not (end < len(text) and (re.fullmatch(r"[A-Za-z0-9_\u0370-\u03ff]", text[end]) or text[end] in "\\$")):
            out.append("$" + text[i:end] + "$")
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _format_segment(text: str) -> str:
    out = []
    cursor = 0
    for match in _MARKED.finditer(text):
        out.append(_bare(text[cursor:match.start()]))
        if match["existing"] is not None:
            out.append(match[0])
        elif match["block"] is not None:
            out.append("\n\n" + display(match["block"]) + "\n\n")
        else:
            out.append("$" + match["inline"].strip() + "$")
        cursor = match.end()
    out.append(_bare(text[cursor:]))
    return "".join(out)


def prose(text: str, *, preserve_quotes: bool = True) -> str:
    """Format prose; opt out of quote masking only for generated explanations.

    Even when ``preserve_quotes=False``, quote characters stay literal and code,
    blockquotes, links, paths and existing math remain opaque. Actual paper
    quotations should bypass this formatter, rather than opt out of masking.
    """
    if not text:
        return text
    # An outer construct owns its contents: math containing \text{"quoted"}
    # remains math, while a source quotation containing math remains verbatim.
    # At the same start, opaque syntax (notably money/code) takes precedence.
    patterns = [p for p in _OPAQUE if preserve_quotes or p is not _QUOTES]
    ranges = [(m.start(), 0, m.end()) for pattern in patterns for m in pattern.finditer(text)
              if not (pattern is _LINK and _math_application_end(text, m.start()) == m.end())]
    ranges += [(m.start(), 1, m.end()) for m in _MARKED.finditer(text)]
    out = []
    cursor = 0
    for start, kind, end in sorted(ranges, key=lambda r: (r[0], r[1], -r[2])):
        if start < cursor:
            continue
        out.extend((_bare(text[cursor:start]), text[start:end] if kind == 0 else _format_segment(text[start:end])))
        cursor = end
    out.append(_bare(text[cursor:]))
    return "".join(out)


def symbol(value: str) -> str:
    """Delimit a dedicated symbol field; send mixed Chinese explanations to prose."""
    value = value.strip()
    if not value or _CJK.search(value):
        return prose(value)
    if _MARKED.fullmatch(value):
        return _format_segment(value).strip()
    command = _COMMAND.fullmatch(value)
    if command is not None and command[1] in _ARG_COUNTS:
        return value
    # Never turn a path, URL, filename or an ordinary English word into math.
    if any(p.fullmatch(value) for p in _OPAQUE):
        return value
    if not (re.search(r"[\\_^{}=+*/<>\u0370-\u03ff]", value)
            or re.fullmatch(r"[A-Za-z]|[A-Z]{2,3}|\d+(?:\.\d+)?", value)):
        return prose(value)
    return "$" + value + "$"


def display(value: str) -> str:
    """Delimit one dedicated LaTeX equation as a standalone Markdown block."""
    value = value.strip()
    if not value:
        return ""
    for opening, closing in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if value.startswith(opening) and value.endswith(closing) and len(value) >= len(opening + closing):
            value = value[len(opening):-len(closing)].strip()
            break
    return "$$\n" + value + "\n$$" if value else ""
