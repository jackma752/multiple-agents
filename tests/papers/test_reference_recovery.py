"""Lossless CPU-only recovery: retain statements and all original source spans."""
import copy
from types import SimpleNamespace

import pytest

from agenthub_kernel.papers.citations import resolve
from agenthub_kernel.papers.reference_recovery import recover_reference_lists
from agenthub_kernel.papers.guidance import study_result


def chunk(text, seq=0):
    return SimpleNamespace(id=f'chunk-{seq}', seq=seq, page=seq+1, document_id='approved-document', text=text)


def candidate(refs):
    return {'units': [{'id':'u1','text':'Keep this explanation exactly.', 'latex':r'x_{n+1}=Tx_n',
                       'depends_on':['unchanged'], 'sources':refs}],
            'math_state':'incomplete','missing':[{'reason':'Needs mathematical review.'}]}


def test_merge_preserves_every_span_and_never_changes_mathematics():
    c=chunk('First approved source phrase. Some intervening text. Final approved source phrase.')
    source=candidate([{'chunk_id':c.id,'quote':'First approved source phrase.'},
                      {'chunk_id':c.id,'quote':'Final approved source phrase.'}])
    before=copy.deepcopy(source)
    recovered=recover_reference_lists(source,[c])
    assert source==before and not recovered['issues'] and recovered['model_calls']==0
    fixed=recovered['candidate']; refs=fixed['units'][0]['sources']
    assert len(refs)==1 and refs[0]['quote']==c.text
    assert {k:v for k,v in fixed['units'][0].items() if k!='sources'}=={k:v for k,v in before['units'][0].items() if k!='sources'}
    assert fixed['math_state']==before['math_state'] and fixed['missing']==before['missing']
    for audit in recovered['audit']:
        lo,hi=audit['merged_span']
        assert all(lo<=start<=end<=hi for start,end in audit['covered_spans'])


def test_unique_short_quote_is_extended_only_inside_declared_material():
    c=chunk('A longer approved context before theta42 and after the unique symbol.')
    ref={'source_id':'S01','quote':'theta42'}
    recovered=recover_reference_lists(candidate([ref]),[c])
    assert not recovered['issues'] and recovered['audit'][0]['action']=='expanded_short_exact'
    fixed=recovered['candidate']['units'][0]['sources'][0]
    assert 'theta42' in fixed['quote'] and fixed['quote'] in c.text and resolve(fixed,[c])[1] is None


@pytest.mark.parametrize('ref,text', [
    ({'chunk_id':'chunk-0','quote':'theta'},'An approved theta occurs here and another theta occurs later.'),
    ({'chunk_id':'chunk-0','quote':'absent'},'This approved text does not contain the requested symbol.'),
    ({'source_id':'S99','quote':'theta'},'This theta belongs to a different approved source.'),
    ({'chunk_id':'chunk-0','source_id':'S99','quote':'theta'},'This theta belongs to a different source.'),
])
def test_unverifiable_or_ambiguous_short_quotes_remain_errors(ref,text):
    recovered=recover_reference_lists(candidate([ref]),[chunk(text)])
    assert recovered['issues'] and recovered['candidate']['units'][0]['sources']==[ref]


def test_cannot_discard_evidence_or_merge_across_chunks_to_fit_the_limit():
    chunks=[chunk(f'Unique approved quotation number {i} with enough context.',i) for i in range(13)]
    refs=[{'chunk_id':c.id,'quote':c.text} for c in chunks]
    recovered=recover_reference_lists(candidate(refs),chunks)
    assert len(recovered['candidate']['units'][0]['sources'])==13
    assert any(i['code']=='source_limit_remaining' for i in recovered['issues'])
    assert all(resolve(r,chunks)[1] is None for r in recovered['candidate']['units'][0]['sources'])


def test_unmergeable_long_span_retains_valid_original_references():
    text='First approved phrase. '+('middle '*4000)+' Final approved phrase.'
    c=chunk(text)
    refs=[{'chunk_id':c.id,'quote':'First approved phrase.'},{'chunk_id':c.id,'quote':'Final approved phrase.'}]
    recovered=recover_reference_lists(candidate(refs),[c])
    assert len(recovered['candidate']['units'][0]['sources'])==2
    assert any(i['code']=='merge_not_verifiable' for i in recovered['issues'])


def test_full_study_validation_runs_again_after_lossless_recovery():
    from tests.papers.test_guidance import sample
    c, raw=sample()
    raw['units'][0]['sources']=[{'chunk_id':c.id,'quote':c.text[i:i+40]} for i in range(20)]
    raw['units'][4]['sources']=[{'chunk_id':c.id,'quote':'Theorem'}]
    recovered=recover_reference_lists(raw,[c])
    assert not recovered['issues']
    checked=study_result(recovered['candidate'],[c])
    assert len(checked['units'])==len(raw['units']) and len(checked['units'][0]['sources'])==1
    assert checked['units'][0]['text']==raw['units'][0]['text']
