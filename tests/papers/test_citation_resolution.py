"""Exact source resolution and reference-only repair; no model calls."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from agenthub_kernel.papers.citations import (
    CitationError, apply_source_patch, exact_spans, normalized_offsets, resolve, source_aliases,
)
from agenthub_kernel.papers.guidance import study_result
from agenthub_kernel.papers.parse import normalize, _page_text
from agenthub_kernel.papers.validate import FormatError
from tools.demo.guidance_stub import teaching


def sources():
    return [SimpleNamespace(id='c1', seq=0, document_id='d1', page=1,
                text='The complete metric space has a contraction with a unique fixed point.'),
            SimpleNamespace(id='c2', seq=1, document_id='d1', page=2,
                text='Every geometric tail tends to zero when the contraction factor is below one.')]


def test_unique_exact_relocation_is_audited_and_uses_original_source_text():
    cs = sources()
    quote = cs[1].text.lower()
    fixed, error = resolve({'chunk_id': 'c1', 'quote': quote, 'pdf_page': 999,
                             'source_location': {'method': 'fake'}}, cs)
    assert error is None and fixed['chunk_id'] == 'c2' and fixed['pdf_page'] == 2
    assert fixed['quote'] == cs[1].text
    loc = fixed['source_location']
    assert loc['method'] == 'relocated_exact' and loc['declared_chunk_id'] == 'c1'
    assert cs[1].text[loc['chunk_start']:loc['chunk_end']] == fixed['quote']
    assert loc['chunk_sha256'] == hashlib.sha256(cs[1].text.encode()).hexdigest()


@pytest.mark.parametrize('mutation,code', [
    ('unknown', 'unknown_source'), ('cross_document', 'span_absent'),
    ('duplicate', 'ambiguous_source'), ('paraphrase', 'span_absent'), ('short', 'invalid_quote'),
])
def test_resolution_never_guesses_or_expands_source_scope(mutation, code):
    cs = sources(); quote = cs[1].text
    ref = {'chunk_id': 'c1', 'quote': quote}
    if mutation == 'unknown': ref['chunk_id'] = 'not-sent'
    if mutation == 'cross_document': cs[1].document_id = 'different-document'
    if mutation == 'duplicate': cs.append(SimpleNamespace(**{**vars(cs[1]), 'id': 'c3'}))
    if mutation == 'paraphrase': ref['quote'] = 'Every geometric tail converges to zero with suitable conditions.'
    if mutation == 'short': ref['quote'] = 'tail'
    assert resolve(ref, cs) == (None, code)


def test_short_sources_are_request_scoped_and_conflicts_are_rejected():
    cs = sources()
    fixed, error = resolve({'source_id': 'S02', 'quote': cs[1].text}, cs)
    assert error is None and fixed['chunk_id'] == 'c2'
    assert resolve({'source_id': 'S02', 'chunk_id': 'c1', 'quote': cs[1].text}, cs)[1] == 'unknown_source'
    assert resolve({'source_id': 'S03', 'quote': cs[1].text}, cs)[1] == 'unknown_source'
    assert source_aliases([cs[1]]) == {'S02': 'c2'}


@pytest.mark.parametrize('text', [' A  \tB\nC\u00ad D ', 'İΣ ΟΣ and text\nwith spaces.', ' x \u00ad  y '])
def test_normalization_offsets_match_the_existing_exact_rule(text):
    value, offsets = normalized_offsets(text)
    assert value == normalize(text)
    assert len(value) == len(offsets)
    assert all(0 <= a < b <= len(text) for a, b in offsets)


def test_duplicate_spans_within_a_chunk_are_not_assigned_an_invented_position():
    c = sources()[0]; quote = c.text; c.text += '\n' + quote
    assert len(exact_spans(quote, c.text)) == 2
    assert resolve({'chunk_id': c.id, 'quote': quote}, [c])[1] == 'ambiguous_span'


def test_collect_all_errors_and_apply_only_named_reference_slots():
    cs = sources(); out = teaching({'chunks': [{'chunk_id': 'c1', 'text': cs[0].text}]})
    out['units'][0]['sources'][0]['quote'] = 'Absent first quotation in supplied source.'
    out['units'][4]['sources'][0]['quote'] = 'Absent second quotation in supplied source.'
    snapshot = copy.deepcopy(out)
    with pytest.raises(CitationError) as caught:
        study_result(out, cs)
    exc = caught.value
    assert [x['unit_id'] for x in exc.issues] == ['problem1', 'setting1']
    assert out == snapshot
    patch = {'replacements': [{'unit_id': x['unit_id'], 'source_index': x['source_index'],
            'source': {'source_id': 'S01', 'quote': cs[0].text}} for x in exc.issues]}
    merged = apply_source_patch(patch, exc.candidate, exc.issues)
    fixed = study_result(merged, cs)
    assert fixed['math_state'] == 'complete'
    for before, after in zip(out['units'], fixed['units']):
        assert {k: v for k, v in before.items() if k != 'sources'} == {k: v for k, v in after.items() if k != 'sources'}
    bad = copy.deepcopy(patch); bad['replacements'][0]['text'] = 'Changed prose'
    with pytest.raises(FormatError): apply_source_patch(bad, exc.candidate, exc.issues)
    bad = copy.deepcopy(patch); bad['replacements'].pop()
    with pytest.raises(FormatError): apply_source_patch(bad, exc.candidate, exc.issues)


def test_math_patch_keeps_backend_history_and_ignores_model_audit():
    from agenthub_kernel.papers.guidance import patch_result

    cs = sources()
    out = teaching({'chunks': [{'chunk_id': cs[0].id, 'text': cs[0].text}]})
    out['units'][0]['sources'][0]['quote'] = cs[1].text
    study = study_result(out, cs)
    assert len(study['citation_audit']) == 1
    replacement = copy.deepcopy(study['units'][8])
    replacement['text'] += ' Authored explanation refinement.'
    patched = patch_result({'replacements': [replacement], 'math_state': 'complete', 'missing': [],
                            'citation_audit': [{'forged': True}]}, study, cs, {replacement['id']})
    assert patched['citation_audit'] == study['citation_audit']


class Page:
    mediabox = SimpleNamespace(left=0, bottom=0, width=595, height=842)
    def __init__(self, fragments, complex_transform=False):
        self.fragments = fragments; self.complex_transform = complex_transform
    def extract_text(self, visitor_text):
        for text, size, x, y in self.fragments:
            visitor_text(text, [1, .1 if self.complex_transform else 0, 0, 1, 0, 0],
                         [1, 0, 0, 1, x, y], {}, size)
        return ''.join(x[0] for x in self.fragments)


def test_pdf_footnote_order_preserves_body_subscripts_and_the_entire_note():
    fragments = [('For each statement s', 11, 70, 120), ('i', 8, 280, 118),
        ('2', 6, 83, 105), ('https://example.invalid\n', 9, 87, 101),
        ('An authored explanatory footnote.\n', 9, 70, 90),
        (' in S, the verification rule applies to every statement in the original material.', 11, 306, 760)]
    page = Page(fragments)
    ordered, audit = _page_text(page, 3)
    assert 'For each statement si\n in S' in ordered
    assert ordered.index('[Footnotes]') > ordered.index('verification rule')
    assert 'https://example.invalid' in ordered and 'An authored explanatory footnote.' in ordered
    assert audit['page'] == 3 and len(audit['moved_ranges']) == 1
    original = ''.join(x[0] for x in fragments)
    span = audit['moved_ranges'][0]
    assert original[span['raw_start']:span['raw_end']] == ''.join(x[0] for x in fragments[2:5])
    unchanged, note = _page_text(Page(fragments, True), 3)
    assert note is None and '[Footnotes]' not in unchanged


def test_small_mathematical_number_without_a_note_block_is_not_moved():
    page = Page([('The expression has exponent ', 11, 70, 120), ('2', 6, 260, 105),
                 (' and remains a mathematical expression in the body text.', 11, 270, 105)])
    text, audit = _page_text(page, 1)
    assert audit is None and 'exponent 2' in text


def test_parse_audit_survives_restart_and_belongs_only_to_its_document(hub):
    from agenthub_kernel.papers.db import PaperRow, ResearchTaskRow
    from agenthub_kernel.papers.pipeline import _store_document

    hub.prove(agents=('claude-code',))
    tid = hub.create(target=1, review=None)
    calls_before = hub.model_calls()
    details = {'extractor': 'authored-order-v1', 'pdf_sha256': 'a'*64,
               'pages': [{'page': 3, 'raw_sha256': 'b'*64, 'ordered_sha256': 'c'*64,
                          'moved_ranges': [{'raw_start': 20, 'raw_end': 60}]}]}
    with hub.client.app.state.db.session() as session:
        task = session.get(ResearchTaskRow, tid)
        for index in (0, 1):
            paper = PaperRow(id=f'paper-audit-{index}', task_id=tid, source='arxiv',
                             source_id=f'audit-{index}', title=f'Authored paper {index}')
            session.add(paper)
            session.flush()
            doc = {'rel_path': f'authored-{index}.pdf', 'sha256': 'a'*64,
                   'size': 100, 'parse_status': 'ok', 'extractor': 'authored-order-v1'}
            if index:
                doc['parse_provenance'] = copy.deepcopy(details)
            row = _store_document(session, task, paper.id, doc, origin='upload', origin_ref='authored.pdf')
            paper.document_id = row.id
    hub.restart()
    docs = {p['paper_id']: p['document'] for p in hub.view(tid)['papers']}
    assert docs['paper-audit-0']['parse_provenance'] is None
    assert docs['paper-audit-1']['parse_provenance'] == details
    assert hub.model_calls() == calls_before
