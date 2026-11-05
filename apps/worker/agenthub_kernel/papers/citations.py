"""Exact spans in approved material, unique relocation and bounded source patches."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from agenthub.citations import CitationIssue, CitationLocation
from agenthub_kernel.papers.parse import normalize
from agenthub_kernel.papers.validate import FormatError


class CitationError(FormatError):
    def __init__(self, issues: list[CitationIssue], candidate: dict):
        self.issues = issues
        self.candidate = candidate
        names = '; '.join(f"{i['unit_id']}[{i['source_index']}] {i['chunk_id']} {i['code']}" for i in issues)
        super().__init__(f"source span not found: {len(issues)} source error(s): {names}")


class ReferenceLimitError(FormatError):
    """Keep a structurally valid draft; only its source lists need selection."""

    def __init__(self, issues, candidate):
        self.issues, self.candidate = issues, candidate
        detail = ", ".join(f"{i['unit_id']}: {i['count']} > {i['limit']}" for i in issues)
        super().__init__("too many sources per unit (limit 12): " + detail)


def apply_source_selection(out, seed, issues):
    """Select existing references only. Never edit prose, math or dependencies."""
    if not isinstance(out, dict) or set(out) != {'selections'} or not isinstance(out['selections'], list):
        raise FormatError('source selection must contain selections only')
    limits = {i['unit_id']: i['limit'] for i in issues}
    fixed, seen = copy.deepcopy(seed), set()
    units = {u['id']: u for u in fixed['units']}
    for selection in out['selections']:
        if not isinstance(selection, dict) or set(selection) != {'unit_id', 'source_indices'}:
            raise FormatError('source selection may name existing source indices only')
        uid, indices = selection['unit_id'], selection['source_indices']
        if not isinstance(uid, str) or uid not in limits or uid in seen:
            raise FormatError('source selection changed an unapproved or repeated unit')
        refs = units[uid]['sources']
        if (not isinstance(indices, list) or not 1 <= len(indices) <= limits[uid]
                or any(type(i) is not int or not 0 <= i < len(refs) for i in indices)
                or len(set(indices)) != len(indices)):
            raise FormatError('source selection needs 1..12 distinct in-range indices')
        units[uid]['sources'] = [refs[i] for i in sorted(indices)]
        seen.add(uid)
    if seen != set(limits):
        raise FormatError('source selection must address every over-limit unit')
    return fixed


def merge_audit(*histories):
    """Merge backend-generated histories, never a field supplied by the model."""
    return list({json.dumps(x, sort_keys=True): x for entries in histories for x in entries}.values())


def source_aliases(chunks):
    """Per-document sequence keeps IDs stable when a repair sends a subset."""
    return {f'S{getattr(c, "seq", i)+1:02d}': c.id for i, c in enumerate(chunks)}


def normalized_offsets(text):
    chars, offsets = [], []
    for match in re.finditer(r'\s+|[^\s]', text):
        value = match.group()
        if value == '\u00ad':
            continue
        value = ' ' if value.isspace() else value
        if value == ' ' and chars and chars[-1] == ' ':
            offsets[-1] = (offsets[-1][0], match.end())
            continue
        chars.append(value)
        offsets.append((match.start(), match.end()))
    if chars and chars[0] == ' ':
        chars.pop(0); offsets.pop(0)
    if chars and chars[-1] == ' ':
        chars.pop(); offsets.pop()
    joined = ''.join(chars)
    lowered = joined.lower()
    expanded = [span for ch, span in zip(joined, offsets) for _ in ch.lower()]
    assert len(expanded) == len(lowered)
    return lowered, expanded


def exact_spans(quote, text):
    wanted = normalize(quote)
    if len(wanted) < 12 or len(quote) > 24000:
        return []
    normalized, offsets = normalized_offsets(text)
    found, start = [], 0
    while (index := normalized.find(wanted, start)) >= 0:
        found.append((offsets[index][0], offsets[index+len(wanted)-1][1]))
        start = index+1
        if len(found) == 2:  # two matches already establish ambiguity
            break
    return found


def resolve(ref, chunks):
    if not isinstance(ref, dict):
        return None, 'invalid_reference'
    source = {c.id: c for c in chunks}
    aliases = source_aliases(chunks)
    declared = ref.get('chunk_id')
    if ref.get('source_id') is not None:
        short = aliases.get(ref['source_id']) if isinstance(ref['source_id'], str) else None
        if short is None or declared is not None and declared != short:
            return None, 'unknown_source'
        declared = short
    original = source.get(declared) if isinstance(declared, str) else None
    quote = ref.get('quote')
    if original is None:
        return None, 'unknown_source'
    if not isinstance(quote, str) or len(normalize(quote)) < 12 or len(quote) > 24000:
        return None, 'invalid_quote'
    matches = exact_spans(quote, original.text)
    if len(matches) > 1:
        return None, 'ambiguous_span'
    target = original
    method = 'exact'
    if not matches:
        # Never consult the database or additional files. Relocation cannot
        # expand the material sent in the approved request or cross documents.
        document = getattr(original, 'document_id', None)
        candidates = []
        for other in chunks:
            if other.id == original.id or getattr(other, 'document_id', None) != document:
                continue
            candidates.extend((other, span) for span in exact_spans(quote, other.text))
            if len(candidates) > 1:
                return None, 'ambiguous_source'
        if not candidates:
            return None, 'span_absent'
        target, span = candidates[0]
        matches, method = [span], 'relocated_exact'
    start, end = matches[0]
    actual = target.text[start:end]
    location: CitationLocation = {'method': method, 'declared_chunk_id': declared,
        'chunk_sha256': hashlib.sha256(target.text.encode()).hexdigest(),
        'quote_sha256': hashlib.sha256(actual.encode()).hexdigest(),
        'chunk_start': start, 'chunk_end': end}
    # Do not trust provenance or page numbers supplied by the model.
    return {'chunk_id': target.id, 'quote': actual, 'pdf_page': target.page,
            'source_location': location}, None


def resolve_units(out, chunks):
    candidate = copy.deepcopy(out)
    issues, audit = [], []
    for unit in candidate['units']:
        for index, ref in enumerate(unit.get('sources', [])):
            fixed, error = resolve(ref, chunks)
            if error:
                issues.append({'unit_id': unit['id'], 'source_index': index,
                    'chunk_id': str(ref.get('chunk_id') or ref.get('source_id') or '') if isinstance(ref, dict) else '',
                    'code': error, 'message': 'Copy one exact span from a single supplied source; do not guess.'})
            else:
                unit['sources'][index] = fixed
                if fixed['source_location']['method'] == 'relocated_exact':
                    audit.append({'unit_id': unit['id'], 'source_index': index, **fixed})
    candidate['citation_audit'] = audit
    if issues:
        raise CitationError(issues, candidate)
    return candidate


def apply_source_patch(out, seed, issues):
    """Only named reference slots can change; prose, math and dependencies stay."""
    if not isinstance(out, dict) or set(out) != {'replacements'} or not isinstance(out['replacements'], list):
        raise FormatError('citation repair must contain replacements only')
    allowed = {(i['unit_id'], i['source_index']) for i in issues}
    fixed, seen = copy.deepcopy(seed), set()
    target = {u['id']: u for u in fixed['units']}
    for row in out['replacements']:
        if not isinstance(row, dict) or set(row) != {'unit_id', 'source_index', 'source'}:
            raise FormatError('citation repair may edit reference slots only')
        if not isinstance(row['unit_id'], str) or type(row['source_index']) is not int:
            raise FormatError('citation repair needs a unit id and integer source index')
        key = (row['unit_id'], row['source_index'])
        if key not in allowed or key in seen:
            raise FormatError('citation repair changed an unapproved or repeated source slot')
        ref = row['source']
        if not isinstance(ref, dict) or set(ref) - {'chunk_id', 'source_id', 'quote'}:
            raise FormatError('citation repair source needs only source id and quote')
        target[key[0]]['sources'][key[1]] = copy.deepcopy(ref)
        seen.add(key)
    if seen != allowed:
        raise FormatError('citation repair must address every failed source slot')
    return fixed
