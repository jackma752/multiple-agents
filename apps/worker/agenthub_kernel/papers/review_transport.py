"""Bound research inputs without truncating teaching or approved material.

Logical input/checkpoint hashes remain unchanged: this is a reversible wire
representation. Previously successful reviews must never be repeated merely
because their JSON/citation representation can now be more compact.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from agenthub.chat import MAX_MESSAGE_CHARS
from agenthub.papers import IndexedReviewSource
from agenthub_kernel.papers.citations import resolve
from agenthub_kernel.papers.templates import Template, _register, render
from agenthub_kernel.papers.validate import FormatError


class PaperInputTooLarge(FormatError):
    def __init__(self, original_chars, sent_chars, limit):
        self.original_chars, self.sent_chars, self.limit = original_chars, sent_chars, limit
        super().__init__(f'Paper request: {sent_chars} characters after lossless packing; limit {limit}. No new invocation created.')


def indexed_review(data):
    """Replace only duplicate quotation bodies by exact spans in supplied chunks."""
    fixed = copy.deepcopy(data)
    if 'source_format' in fixed or not isinstance(fixed.get('chunks'),list) or not isinstance(fixed.get('units'),list):
        raise FormatError('invalid review material')
    chunks, aliases = {}, set()
    for c in fixed['chunks']:
        if (not isinstance(c,dict) or not isinstance(c.get('text'),str)
                or not isinstance(c.get('source_id'),str) or not isinstance(c.get('chunk_id'),str)
                or c['chunk_id'] in chunks or c['source_id'] in aliases):
            raise FormatError('invalid or repeated review source')
        chunks[c['chunk_id']] = c
        aliases.add(c['source_id'])
    for unit in fixed['units']:
        for index, ref in enumerate(unit.get('sources', [])):
            if not isinstance(ref,dict) or set(ref) != {'chunk_id','quote','pdf_page'}:
                raise FormatError('indexed review requires canonical verified sources')
            c = chunks.get(ref['chunk_id'])
            if c is None or ref['pdf_page'] != c.get('pdf_page'):
                raise FormatError('review citation missing from supplied material')
            chunk = SimpleNamespace(id=c['chunk_id'],seq=0,page=c['pdf_page'],text=c['text'])
            canonical,error = resolve(ref,[chunk])
            if error or canonical['quote'] != ref['quote']:
                raise FormatError('review quote is not an exact unique span in its supplied chunk')
            location = canonical['source_location']
            source: IndexedReviewSource = {'source_id':c['source_id'],
                'span':[location['chunk_start'],location['chunk_end']], 'anchor':ref['quote'][:48]}
            unit['sources'][index] = source
    fixed['source_format'] = 'indexed_spans_v1'
    return fixed


def render_for_model(name, template, data, *, language='zh', limit=MAX_MESSAGE_CHARS):
    """Try normal JSON, compact JSON, then reversible citation indexing."""
    original = render(template,data,language=language)
    if len(original) <= limit:
        return template,original
    compact = render(template,data,language=language,compact=True)
    if len(compact) <= limit:
        return template,compact
    if name.startswith('study_review:'):
        indexed_template = _indexed_template(template)
        compact = render(indexed_template,indexed_review(data),language=language,compact=True)
        if len(compact) <= limit:
            return indexed_template,compact
    raise PaperInputTooLarge(len(original),len(compact),limit)


def _indexed_template(template):
    # Keep the original review requirements/output schema. Add an explicit,
    # versioned explanation of the reversible citation encoding only.
    return _register(Template('research.study-review-indexed.v1',1,template.stage,template.inputs,template.output,
        template.evidence_rule + ' Source format indexed_spans_v1: each unit source names a supplied source_id '
        'and span=[start,end], zero-based Unicode character offsets into that chunk text (end exclusive). '
        'The anchor is only the first 48 characters for navigation; the full quotation is text[start:end]. '
        'The backend already verified every exact span. Read the full supplied material, not just the anchors; '
        'no source text or teaching content was omitted. Do not treat citation indexing as mathematical review.',
        template.missing_rule,template.task))
