"""Offline acceptance for multi-source research, portable files and experiment records."""
import hashlib
import io
import json
import math
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

import pytest

from agenthub.experiments import validate_records
from agenthub.papers import ResearchRequest, ResourceRules, resource_decision, estimate_calls
from agenthub_kernel.papers import sources
from agenthub_kernel.papers.portable import enrich
from tests.papers.conftest import Hub, waiting
from tools.demo.paper_fixtures import FixtureWeb, CORPUS


def payload(source):
    if source=='openalex':
        return {'results':[{'id':'https://openalex.org/W1','title':'A study','publication_year':2025,
            'doi':'https://doi.org/10.1234/Test','authorships':[{'author':{'display_name':'A. Author'}}],
            'abstract_inverted_index':{'A':[0],'study':[1]},'primary_location':{'landing_page_url':'https://example.org/a',
            'source':{'display_name':'Journal'}},'best_oa_location':{'pdf_url':'https://example.org/a.pdf'}}]}
    if source=='crossref':
        return {'message':{'items':[{'DOI':'10.1234/Test','title':['A study'],'author':[{'given':'A.','family':'Author'}],
            'published':{'date-parts':[[2025,1]]},'abstract':'<jats:p>A study</jats:p>',
            'link':[{'content-type':'application/pdf','URL':'https://example.org/tdm.pdf'}]}]}}
    return {'data':[{'paperId':'s1','title':'A study','authors':[{'name':'A. Author'}],'year':2025,
        'externalIds':{'DOI':'10.1234/Test'},'openAccessPdf':{'url':'https://example.org/a.pdf'}}]}


@pytest.mark.parametrize('source',['openalex','crossref','semantic_scholar'])
def test_source_query_and_metadata(source):
    url=sources.query_url(source,['example & query'],year_from=2024,year_to=2026,max_results=15)
    assert urlsplit(url).scheme=='https'
    rows=sources.parse_results(source,json.dumps(payload(source)).encode())
    assert len(rows)==1 and rows[0]['doi']=='10.1234/test'
    assert rows[0]['authors']==['A. Author'] and rows[0]['year']==2025
    assert rows[0]['source']==source
    if source=='crossref': assert rows[0]['pdf_url'] is None and rows[0]['abstract']=='A study'
    if source=='openalex': assert rows[0]['abstract']=='A study'
    with pytest.raises(ValueError):sources.parse_results(source,b'{}')
    with pytest.raises(ValueError):sources.parse_results(source,b'broken json')


def test_new_resources_are_explicit_and_old_hashes_stable():
    old=ResourceRules(send_to=('deepseek',))
    raw=old.as_dict()
    assert 'allow_public_fulltext' not in raw
    assert old.rules_hash()==hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    assert not resource_decision(old,'download','https://publisher.example/a.pdf')[0]
    public=ResourceRules(sources=('openalex',),send_to=('deepseek',),allow_public_fulltext=True)
    assert resource_decision(public,'download','https://publisher.example/a.pdf',resolve=lambda h:['93.184.216.34'])[0]
    assert not resource_decision(public,'download','https://evil.example/a',resolve=lambda h:['127.0.0.1'])[0]
    assert not resource_decision(public,'search','crossref')[0]
    assert public.rules_hash()!=old.rules_hash()
    with pytest.raises(ValueError): ResearchRequest(topic='x',sources=['arxiv','arxiv'])
    assert estimate_calls(ResearchRequest(topic='x',sources=['arxiv','openalex']),review=True)['expected'] > estimate_calls(ResearchRequest(topic='x'),review=True)['expected']


def test_default_planner_preserves_legacy_input_hash():
    from types import SimpleNamespace
    from agenthub_kernel.papers.pipeline import phase_plan, _h
    from agenthub_kernel.papers.templates import PLAN
    legacy={'topic':'x','years':'不限-不限','target_papers':5,'source':'arXiv (title/abstract keyword search)'}
    seen=[]
    def model(*args):
        seen.append(args)
        return 'waiting',None
    phase_plan(SimpleNamespace(request=ResearchRequest(topic='x'),model=model))
    assert seen[0][4] == _h([legacy,PLAN.sha256])


def test_portable_exports_preserve_material_and_are_reproducible():
    base={'review.md':'# 中文标题\n\n<script>alert(1)</script>\n$x_1=2$\n\n## 证据\n原文 [1]'.encode(),
          'evidence.json':b'[{"quote":"exact original"}]'}
    files=enrich(base)
    assert files==enrich(base) and files['review.md']==base['review.md']
    assert b'<script>' not in files['review.html'] and b'&lt;script&gt;' in files['review.html']
    assert b'\\documentclass' in files['review.tex']
    with zipfile.ZipFile(io.BytesIO(files['review.docx'])) as doc:
        for name in doc.namelist():
            if name.endswith(('.xml','.rels')): ET.fromstring(doc.read(name))
        assert '中文标题' in doc.read('word/document.xml').decode()
        assert '$x_1=2$' in doc.read('word/document.xml').decode()
    with zipfile.ZipFile(io.BytesIO(files['research-bundle.zip'])) as z:
        manifest=json.loads(z.read('MANIFEST.json'))
        for name,meta in manifest.items(): assert hashlib.sha256(z.read(name)).hexdigest()==meta['sha256']
        assert z.read('evidence.json')==base['evidence.json']
        assert not any('credentials' in n or n.endswith('.pdf') for n in z.namelist())


def record(name='baseline', samples=None, **changes):
    return {'id':name,'label':name,'dataset':'data-v1','protocol':'split-a; seed 42','metric':'error','direction':'min',
            'samples':samples or [3,4,5],'provenance':'reported','note':'measured locally',**changes}


@pytest.mark.parametrize('values',[[True],[math.inf],[math.nan],[],['1']])
def test_experiment_input_does_not_accept_nonmeasurements(values):
    bad=record();bad['samples']=values
    with pytest.raises(ValueError):validate_records([bad])


def test_completed_research_experiment_versions_exports_and_no_model_calls(hub):
    hub.prove()
    check=hub.api('POST','/api/papers/preflight',json=hub.body(target=1)).json()
    assert {'review.docx','review.html','review.tex','research-bundle.zip'} <= set(check['output']['files'])
    task=hub.create(target=1);v=hub.run(task)
    assert v['status']=='completed',v
    calls=hub.model_calls()
    data=[record(),record('improved',[1,2,3]),record('other-data',[8,9],dataset='other'),record('demo',[0.01],provenance='demo')]
    r=hub.api('PUT',f'/api/papers/{task}/experiments',json={'expected_version':0,'records':data})
    assert r.status_code==200,r.text
    result=r.json(); assert result['version']==1
    c={x['id']:x for x in result['comparisons']}
    assert c['improved']['delta']==-2 and c['improved']['improvement']==2
    assert c['other-data']['baseline_id']=='other-data' and c['demo']['baseline_id']=='demo'
    assert hub.model_calls()==calls
    assert hub.api('PUT',f'/api/papers/{task}/experiments',json={'expected_version':0,'records':data}).status_code==409
    assert hub.api('GET',f'/api/papers/{task}/experiments').json()['version']==1
    bundle=hub.api('GET',f'/api/papers/{task}/artifacts/research-bundle.zip')
    assert bundle.status_code==200 and bundle.headers['content-type']=='application/zip'
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        assert json.loads(z.read('experiments.json'))['version']==1
        assert 'experiment-samples.csv' in z.namelist()
    assert hub.api('GET',f'/api/papers/{task}/artifacts/review.docx').headers['content-type'].endswith('document')
    hub.restart()
    assert hub.api('GET',f'/api/papers/{task}/experiments').json()['records']==data


def test_experiment_routes_require_user_and_own_task(hub):
    hub.prove();task=hub.create(target=1)
    assert hub.api('PUT',f'/api/papers/{task}/experiments',json={'expected_version':0,'records':[]}).status_code==409
    assert hub.client.get(f'/api/papers/{task}/experiments').status_code==401
    assert hub.client.get(f'/api/papers/{task}/experiments',headers={'Authorization':'Bearer dev-agent-token'}).status_code==403
    assert hub.api('GET','/api/papers/not-owned/experiments').status_code==404


def test_multi_source_failure_is_audited_and_duplicates_retain_provenance(tmp_path):
    original=FixtureWeb()
    p=CORPUS[0]
    def web(url):
        if urlsplit(url).hostname=='api.openalex.org':
            value=payload('openalex');w=value['results'][0]
            w.update(title=p['title'],doi='https://doi.org/'+p['doi'],publication_year=p['year'])
            return 200,'application/json',json.dumps(value).encode()
        if urlsplit(url).hostname=='api.crossref.org':return 429,'application/json',b'{}'
        return original(url)
    h=Hub(tmp_path,web=web).open()
    try:
        h.prove();body=h.body(target=1);body['request']['sources']=['arxiv','openalex','crossref']
        response=h.api('POST','/api/papers',json=body);assert response.status_code==200,response.text
        task=response.json()['task_id'];v=h.run(task)
        assert v['status']=='completed',v
        statuses=v['outcome']['search_sources']
        assert any(x['query'].startswith('crossref:') and x['status']=='failed' for x in statuses)
        report=json.loads(h.api('GET',f'/api/papers/{task}/artifacts/research-report.json').content)
        assert report['source_results']
        assert any(m.get('source')=='openalex' for paper in report['papers'] for m in paper['merged'])
        assert len([paper for paper in report['papers'] if paper['source_id']==p['id']])==1
    finally:h.close()


def test_title_dedup_does_not_merge_distinct_known_dois_and_recalls_source_alias(hub):
    from sqlalchemy import select
    from agenthub_kernel.papers.db import PaperRow, ResearchTaskRow
    from agenthub_kernel.papers.pipeline import _upsert_papers
    hub.prove();task_id=hub.create(target=1)
    one=sources.parse_results('openalex',json.dumps(payload('openalex')).encode())[0]
    two={**one,'source_id':'https://openalex.org/W2','doi':'10.1234/other'}
    alias={**one,'source':'semantic_scholar','source_id':'s-alias'}
    with hub.client.app.state.db.session() as session:
        task=session.get(ResearchTaskRow,task_id)
        assert _upsert_papers(session,task,[one,two,alias],0)=={'entries':3,'new':2,'merged':1}
        session.commit()
    # A later query may omit the DOI/title metadata; its recorded alias still matches.
    with hub.client.app.state.db.session() as session:
        task=session.get(ResearchTaskRow,task_id)
        assert _upsert_papers(session,task,[{**alias,'doi':None,'title':'Changed metadata','authors':[]}],1)['new']==0
        assert len(list(session.scalars(select(PaperRow).where(PaperRow.task_id==task_id))))==2


def test_empty_exact_arxiv_queries_get_one_audited_broader_query(tmp_path):
    original=FixtureWeb();requests=[]
    def web(url):
        if urlsplit(url).hostname=='export.arxiv.org':
            query=parse_qs(urlsplit(url).query).get('search_query',[''])[0]
            requests.append(query)
            if query.count('all:')>1:
                return 200,'application/atom+xml',b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        return original(url)
    h=Hub(tmp_path,web=web).open()
    try:
        h.prove();task=h.create(target=1);view=h.run(task)
        assert view['status']=='completed',view
        broad=[r for r in view['outcome']['search_sources'] if r['query'].endswith(':broader')]
        assert len(broad)==1 and broad[0]['counts']['new']>0
        assert len([q for q in requests if q.count('all:')==1])==1
        calls=h.model_calls();fetches=len(h.fetcher.calls)
        h.restart();assert h.view(task)['phase']=='done'
        assert h.model_calls()==calls and len(h.fetcher.calls)==fetches
    finally:h.close()
