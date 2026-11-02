"""Versioned experiment ledger and descriptive comparisons, no code execution."""
import csv
import io
import json
import statistics

from sqlalchemy import select
from agenthub.experiments import validate_records
from agenthub_kernel.papers.db import ExperimentRevisionRow
from agenthub_kernel.service import QuotaError, new_id


def view(session, task):
    row = session.scalar(select(ExperimentRevisionRow).where(ExperimentRevisionRow.task_id==task.id)
                         .order_by(ExperimentRevisionRow.version.desc()))
    records = row.records if row else []
    groups = {}
    for r in records:
        key = (r['dataset'],r['protocol'],r['metric'],r['direction'],r['provenance'])
        groups.setdefault(key,[]).append(r)
    comparisons=[]
    for key, group in groups.items():
        baseline=statistics.fmean(group[0]['samples'])
        for r in group:
            values=r['samples']; avg=statistics.fmean(values)
            change=avg-baseline
            comparisons.append({'id':r['id'],'label':r['label'],'dataset':key[0],'protocol':key[1],
                'metric':key[2],'direction':key[3],'provenance':key[4],'n':len(values),'mean':avg,
                'sample_stddev':statistics.stdev(values) if len(values)>1 else None,
                'baseline_id':group[0]['id'],'delta':change,
                'improvement':(-change if key[3]=='min' else change),
                'status':'baseline' if r is group[0] else 'descriptive_only'})
    return {'version':row.version if row else 0,'records':records,'comparisons':comparisons,
            'limitations':['按相同数据集、条件、指标、方向和数据来源分组；每组首项作为基线',
                '用户记录的数据未被系统独立验证；均值差不是显著性、因果或复现成功证明',
                '演示与用户记录不混合比较；记录不会自动写入论文结论']}


def save(session, task, expected_version, records, actor):
    if task.phase!='done': raise QuotaError('research_in_progress','请在研究结束后记录实验')
    current=view(session,task)
    if current['version']!=expected_version:
        raise QuotaError('experiments_changed','实验记录已被另一窗口更新，请重新打开')
    records=validate_records(records)
    session.add(ExperimentRevisionRow(id=new_id('exp'),task_id=task.id,version=expected_version+1,
                                      records=records,actor=actor))
    session.flush()
    return view(session,task)


def export_files(session, task):
    data=view(session,task)
    if not data['version']: return {}
    stream=io.StringIO(newline='')
    writer=csv.writer(stream)
    writer.writerow(['id','label','dataset','protocol','metric','direction','provenance','sample_index','value'])
    for r in data['records']:
        for i,value in enumerate(r['samples']):
            # Prefix spreadsheet formula-like text; raw exact strings remain in JSON.
            safe=lambda s: "'"+s if isinstance(s,str) and s.lstrip().startswith(('=','+','-','@')) else s
            writer.writerow([safe(r[k]) for k in ('id','label','dataset','protocol','metric','direction','provenance')]+[i+1,value])
    return {'experiments.json':json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'),
            'experiment-samples.csv':('\ufeff'+stream.getvalue()).encode('utf-8')}
