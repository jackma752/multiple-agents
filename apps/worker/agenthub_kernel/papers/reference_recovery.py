"""Pure, lossless source-list recovery; never marks a research step successful.

The caller supplies ONLY material in the original approved requests and must
run the full study/patch validator afterwards. Never choose which evidence to
drop: unresolvable references and over-limit lists remain visible failures.
"""
from __future__ import annotations

import copy
import hashlib

from agenthub_kernel.papers.citations import normalized_offsets, resolve, source_aliases
from agenthub_kernel.papers.parse import normalize
from agenthub_kernel.papers.validate import FormatError


def _short_reference(ref, chunks):
    """Expand one unique short quote in its declared chunk, never relocate it."""
    if not isinstance(ref, dict):
        return None
    quote = ref.get('quote')
    if not isinstance(quote, str) or not 1 <= len(normalize(quote)) < 12:
        return None
    declared = ref.get('chunk_id')
    if 'source_id' in ref:
        alias = source_aliases(chunks).get(ref['source_id']) if isinstance(ref['source_id'], str) else None
        if alias is None or declared is not None and declared != alias:
            return None
        declared = alias
    original = next((c for c in chunks if c.id == declared), None)
    if original is None:
        return None
    # The normal resolver deliberately rejects short anchors. Locate one here
    # without accepting it as evidence; only an expanded, fully resolved quote
    # can be returned. Ambiguous anchors are never guessed.
    wanted = normalize(quote)
    text, offsets = normalized_offsets(original.text)
    index = text.find(wanted)
    if index < 0 or text.find(wanted, index+1) >= 0:
        return None
    start, end = offsets[index][0], offsets[index+len(wanted)-1][1]
    margin = 24
    while margin <= 24000:
        lo, hi = max(0, start-margin), min(len(original.text), end+margin)
        expanded = original.text[lo:hi]
        if len(expanded) > 24000:
            return None
        fixed, error = resolve({'chunk_id': original.id, 'quote': expanded}, chunks)
        if not error:
            return fixed, {'action': 'expanded_short_exact', 'original_span': [start, end],
                'original_quote_sha256': hashlib.sha256(quote.encode()).hexdigest(),
                'expanded_span': [lo, hi]}
        if lo == 0 and hi == len(original.text):
            return None
        margin *= 2
    return None


def recover_reference_lists(candidate, chunks, *, limit=12):
    """Return a separate candidate, unresolved issues and source coverage audit.

    All non-source unit fields and the caller's missing/mathematics state are
    preserved exactly. A merged quotation contains EVERY valid original span
    in the same approved chunk, including intervening original text.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError('reference limit must be positive')
    fixed = copy.deepcopy(candidate)
    if not isinstance(fixed, dict) or not isinstance(fixed.get('units'), list):
        raise FormatError('recovery requires structured teaching units')
    by_id = {c.id: c for c in chunks}
    issues, audit = [], []
    for unit in fixed['units']:
        if not isinstance(unit, dict) or not isinstance(unit.get('sources', []), list):
            raise FormatError('recovery requires source lists')
        original_refs = unit.get('sources', [])
        groups, order, invalid = {}, [], {}
        for index, ref in enumerate(original_refs):
            canonical, error = resolve(ref, chunks)
            extension = _short_reference(ref, chunks) if error == 'invalid_quote' else None
            if extension:
                canonical, action = extension
                error = None
                audit.append({'unit_id': unit['id'], 'original_source_index': index, **action})
            if error:
                key = ('invalid', index)
                order.append(key)
                invalid[key] = copy.deepcopy(ref)
                issues.append({'unit_id': unit['id'], 'original_source_index': index, 'code': error})
                continue
            key = ('chunk', canonical['chunk_id'])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((index, canonical))
        refs = []
        for key in order:
            if key[0] == 'invalid':
                refs.append(invalid[key])
                continue
            entries = groups[key]
            if len(entries) == 1:
                refs.append(entries[0][1])
                continue
            chunk = by_id[key[1]]
            spans = [r['source_location'] for _, r in entries]
            lo, hi = min(s['chunk_start'] for s in spans), max(s['chunk_end'] for s in spans)
            merged, error = resolve({'chunk_id': chunk.id, 'quote': chunk.text[lo:hi]}, chunks)
            if error:
                refs.extend(r for _, r in entries)  # Never erase valid evidence to meet the limit.
                issues.append({'unit_id': unit['id'], 'code': 'merge_not_verifiable'})
                continue
            refs.append(merged)
            audit.append({'action': 'merged_same_chunk', 'unit_id': unit['id'], 'chunk_id': chunk.id,
                'original_source_indices': [i for i, _ in entries],
                'covered_spans': [[s['chunk_start'], s['chunk_end']] for s in spans],
                'merged_span': [lo, hi]})
        unit['sources'] = refs
        if len(refs) > limit:
            issues.append({'unit_id': unit['id'], 'code': 'source_limit_remaining', 'count': len(refs), 'limit': limit})
    return {'candidate': fixed, 'issues': issues, 'audit': audit,
            'model_calls': 0, 'reviewed': False}
