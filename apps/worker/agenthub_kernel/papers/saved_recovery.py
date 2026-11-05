"""Restore an original paid reply into its checkpoint without model execution.

No adapter, scheduler or file importer is accepted here. Client input may name
an original reply and correct failed quotations, never supply teaching text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from sqlalchemy import func, select

from agenthub.contracts import InvocationSnapshot
from agenthub_kernel.db import ApprovalRow, ChatMessageRow, InvocationRow, StageGrantRow, StageRunRow, TaskRow
from agenthub_kernel.papers.citations import CitationError, ReferenceLimitError, resolve, source_aliases
from agenthub_kernel.papers.db import PaperChunkRow, PaperDocumentRow, PaperRow, PaperStepRow
from agenthub_kernel.papers.guidance import patch_result, study_result, unit_payload
from agenthub_kernel.papers.pipeline import _h
from agenthub_kernel.papers.reference_recovery import recover_reference_lists
from agenthub_kernel.papers.service import owned_task
from agenthub_kernel.papers.validate import FormatError, parse_json
from agenthub_kernel.service import QuotaError


def _refuse(code, message):
    raise QuotaError('recovery_' + code, message)


def _declared(ref, aliases):
    if not isinstance(ref, dict):return None
    cid = ref.get('chunk_id')
    if cid is not None and not isinstance(cid, str):return None
    if ref.get('source_id') is not None:
        sid = ref['source_id']
        alias = aliases.get(sid) if isinstance(sid,str) else None
        if alias is None or cid is not None and cid != alias:return None
        cid = alias
    return cid


def _request(session, task, step, iid):
    """Read ONLY the request actually bound to this step's consumed approval."""
    if iid not in (step.invocation_ids or []):
        _refuse('invocation_mismatch', '该回复不属于此步骤')
    inv = session.get(InvocationRow, iid)
    root = session.get(TaskRow, inv.task_id) if inv else None
    grant = session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == iid))
    approval = session.get(ApprovalRow, grant.approval_id) if grant else None
    if (inv is None or inv.status != 'succeeded' or root is None or root.root_task_id != task.root_task_id
            or approval is None or approval.status != 'consumed' or approval.payload_hash != inv.payload_hash):
        _refuse('unverified_reply', '只恢复同一研究任务树中已有审批且成功返回的回复')
    try:
        assert InvocationSnapshot(**inv.snapshot).payload_hash() == inv.payload_hash
    except (AssertionError, TypeError, ValueError):
        _refuse('snapshot_changed', '原审批输入已改变，不能恢复')
    prompt = session.scalar(select(ChatMessageRow.text).where(ChatMessageRow.invocation_id == iid, ChatMessageRow.role == 'user'))
    reply = session.scalar(select(ChatMessageRow.text).where(ChatMessageRow.invocation_id == iid, ChatMessageRow.role == 'assistant'))
    if not prompt or reply is None or prompt not in inv.snapshot['task_input']:
        _refuse('message_changed', '原发送内容或回复缺失，不能恢复')
    try:
        data, end = json.JSONDecoder().raw_decode(prompt.split('<<<DATA (untrusted material)\n', 1)[1])
        # Older generic retries appended a format-error instruction after the
        # closing delimiter. The entire prompt is still snapshot/hash-bound.
        assert prompt.split('<<<DATA (untrusted material)\n', 1)[1][end:].lstrip().startswith('DATA>>>')
        assert isinstance(data, dict)
    except (ValueError, IndexError, AssertionError):
        _refuse('request_invalid', '无法验证原发送材料')
    return inv, prompt, reply, data


def _base(session, task, step):
    if step.name == 'study':
        return None
    match = re.fullmatch(rf'v{task.input_version}:study_patch:{re.escape(step.target)}:r(\d+):[0-9a-f]{{16}}', step.key)
    if not match:
        _refuse('checkpoint_invalid', '修订断点格式不匹配')
    n = int(match[1])
    prefix = (f'v{task.input_version}:study:{step.target}:' if n == 0 else
              f'v{task.input_version}:study_patch:{step.target}:r{n-1}:')
    rows = list(session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == task.id,
        PaperStepRow.target == step.target, PaperStepRow.status == 'succeeded', PaperStepRow.id < step.id,
        PaperStepRow.key.startswith(prefix))))
    if len(rows) != 1 or not isinstance(rows[0].result, dict) or not rows[0].result.get('units'):
        _refuse('base_unavailable', '原始讲解断点不唯一或缺失，不能猜测基础版本')
    return rows[0]


def _chunks(session, task, step, data, paper):
    """Material sent in this original reply and its successful base lineage."""
    payloads = [data]
    base = _base(session, task, step)
    parent = base
    lineage = []
    while parent is not None:
        lineage.append([parent.id, parent.input_hash, parent.result])
        for iid in parent.invocation_ids or []:
            inv = session.get(InvocationRow, iid)
            if inv is None or inv.status != 'succeeded':
                continue
            _, _, _, sent = _request(session, task, parent, iid)
            payloads.append(sent)
        parent = _base(session, task, parent)
        if len(lineage) > 4:
            _refuse('lineage_invalid', '恢复链超出受支持的修订轮数')
    material = {}
    for sent in payloads:
        for c in sent.get('chunks', []):
            cid = c.get('chunk_id')
            if not isinstance(cid, str) or not isinstance(c.get('text'), str):
                _refuse('material_invalid', '原材料格式错误')
            if cid in material and material[cid] != c:
                _refuse('material_changed', '同一来源的已批准文本不一致')
            material[cid] = c
    rows = list(session.scalars(select(PaperChunkRow).where(PaperChunkRow.id.in_(material)).order_by(PaperChunkRow.seq)))
    if not rows or len(rows) != len(material):
        _refuse('material_missing', '已批准来源缺失')
    for c in rows:
        old = material[c.id]
        if (c.document_id != paper.document_id or c.text != old['text'] or c.page != old.get('pdf_page')
                or hashlib.sha256(c.text.encode()).hexdigest() != c.sha256):
            _refuse('material_changed', '来源不属于原论文或内容已改变')
    return base, rows, lineage


def _prepare(session, task, step, iid, corrections=()):
    inv, prompt, reply, data = _request(session, task, step, iid)
    from agenthub_kernel.papers.guidance import PATCH, STUDY
    template = PATCH if step.name == 'study_patch' else STUDY
    if not prompt.startswith('AGENTHUB-TEMPLATE: ' + template.id + '\n'):
        _refuse('template_mismatch', '只能恢复原始讲解或修订回复，不能把其它回复当正文')
    paper = session.get(PaperRow, step.target)
    if paper is None or paper.task_id != task.id or not paper.included:
        _refuse('paper_mismatch', '论文不属于本研究的纳入范围')
    base, chunks, lineage = _chunks(session, task, step, data, paper)
    if step.name == 'study_patch':
        sent = {k:v for k,v in data.items() if k != 'output_constraints'}
        # output_constraints were added after the stable step input was hashed.
        if _h([sent, PATCH.sha256]) != step.input_hash:
            _refuse('input_changed', '原修订请求与断点输入哈希不符')
        by_id = {u['id']:u for u in base.result['units']}
        supplied = data.get('units', [])
        if (not supplied or any(u.get('id') not in by_id or
                unit_payload([by_id[u['id']]])[0] != u for u in supplied)):
            _refuse('base_changed', '基础讲解与原修订请求不符')
        def validator(raw):
            return patch_result(raw, base.result, chunks, set(data['editable_ids']), data['allow_additions'],
                                preserve_material_gaps=bool(data.get('material_constraint')))
    else:
        from agenthub_kernel.papers.continuation import _study_input
        digest, _ = _study_input(session, task, paper)
        if digest != step.input_hash or step.key != f'v{task.input_version}:study:{paper.id}:{digest[:16]}':
            _refuse('input_changed', '原讲解请求与当前断点不符')
        validator = lambda raw: study_result(raw, chunks)
    raw = parse_json(reply)
    try:
        candidate = validator(raw)
    except (CitationError, ReferenceLimitError) as exc:
        candidate = exc.candidate
    candidate = copy.deepcopy(candidate)
    aliases = source_aliases(chunks)
    by_chunk = {c.id:c for c in chunks}
    invalid = {}
    for u in candidate['units']:
        for index, ref in enumerate(u.get('sources', [])):
            _, error = resolve(ref, chunks)
            if error:
                invalid[(u['id'], index)] = ref
    applied = []
    seen = set()
    for fix in corrections:
        key = (fix['unit_id'], fix['source_index'])
        if key not in invalid or key in seen:
            _refuse('correction_scope', '只能修正已有失败引文，不能更改已有效引用或重复修正')
        ref = invalid[key]
        declared = _declared(ref, aliases)
        if fix['chunk_id'] != declared or declared not in by_chunk:
            _refuse('correction_scope', '只能使用原引用声明的已批准来源')
        resolved, error = resolve({'chunk_id':declared, 'quote':fix['quote']}, [by_chunk[declared]])
        if error or resolved['chunk_id'] != declared:
            _refuse('correction_invalid', '修正必须是原 chunk 内唯一可定位的精确原文')
        unit = next(u for u in candidate['units'] if u['id'] == key[0])
        unit['sources'][key[1]] = resolved
        applied.append({**fix, 'old_source':copy.deepcopy(ref)})
        seen.add(key)
    recovered = recover_reference_lists(candidate, chunks)
    try:
        checked = study_result(recovered['candidate'], chunks)
        valid, issues = True, []
    except (CitationError, ReferenceLimitError) as exc:
        checked, valid, issues = recovered['candidate'], False, recovered['issues']
        if not issues:
            issues = list(exc.issues)
    fingerprint = _h([step.id, step.key, step.input_hash, step.status, step.result, step.invocation_ids,
        inv.payload_hash, hashlib.sha256(reply.encode()).hexdigest(), lineage,
        [[c.id, c.sha256, c.page] for c in chunks]])
    # Show original failed slots, whose indices remain stable before merging.
    slots = []
    for (uid, index), ref in invalid.items():
        if not any(i.get('unit_id') == uid and i.get('original_source_index') == index for i in issues):
            continue
        cid = _declared(ref, aliases)
        chunk = by_chunk.get(cid)
        slots.append({'unit_id':uid, 'source_index':index, 'chunk_id':cid,
            'quote':ref.get('quote', '') if isinstance(ref, dict) else '',
            'approved_text':chunk.text if chunk else '', 'pdf_page':chunk.page if chunk else None})
    offer = {'step_id':step.id, 'invocation_id':iid, 'fingerprint':fingerprint, 'units':len(checked['units']),
        'valid':valid, 'issues':issues, 'failed_sources':slots, 'model_calls':0,
        'references':sum(len(u.get('sources', [])) for u in checked['units'])}
    doc = session.get(PaperDocumentRow,paper.document_id)
    offer['coverage'] = {'chunk_ids':[c.id for c in chunks], 'pdf_pages':sorted({c.page for c in chunks if c.page}),
        'selected':len(chunks), 'total':session.scalar(select(func.count()).select_from(PaperChunkRow).where(PaperChunkRow.document_id==paper.document_id)),
        'source_hash':doc.sha256}
    return offer, checked, recovered['audit'], applied, paper


def _eligible(task, step):
    return (step.task_id == task.id and step.kind == 'model' and step.name in ('study', 'study_patch')
            and step.status != 'succeeded' and step.key.startswith(f'v{task.input_version}:{step.name}:'))


def preview(session, task):
    candidates, unavailable = [], []
    if task.phase != 'read':
        return {'candidates':[], 'unavailable':[], 'model_calls':0}
    steps = session.scalars(select(PaperStepRow).where(PaperStepRow.task_id == task.id).order_by(PaperStepRow.id))
    for step in steps:
        if not _eligible(task, step):
            continue
        for iid in step.invocation_ids or []:
            inv = session.get(InvocationRow, iid)
            if inv is None or inv.status != 'succeeded':
                continue
            try:
                candidates.append(_prepare(session, task, step, iid)[0])
            except (QuotaError, FormatError) as exc:
                unavailable.append({'step_id':step.id, 'invocation_id':iid,
                    'code':exc.code if isinstance(exc, QuotaError) else 'format_invalid',
                    'message':exc.message if isinstance(exc, QuotaError) else str(exc)[:300]})
    candidates.sort(key=lambda x:(x['valid'], x['units']), reverse=True)
    return {'candidates':candidates, 'unavailable':unavailable, 'model_calls':0}


def apply(session, user_id, task_id, body, now):
    task = owned_task(session, task_id, user_id)
    request_hash = _h(body)
    key = 'recover_saved:' + body['client_request_id']
    previous = session.scalar(select(PaperStepRow).where(PaperStepRow.task_id == task.id, PaperStepRow.key == key))
    if previous:
        if previous.input_hash != request_hash:
            _refuse('idempotency_conflict', '同一恢复请求标识不能用于不同内容')
        return {'receipt_id':previous.id, 'duplicate':True, 'model_calls':0}
    stage = session.get(StageRunRow, task.stage_id) if task.stage_id else None
    if stage is None or stage.status not in ('paused', 'blocked', 'stopped') or task.phase != 'read':
        _refuse('stage_in_progress', '只能在阅读阶段暂停或阻塞时恢复；不会替你暂停或恢复阶段')
    active = session.scalar(select(InvocationRow.id).join(TaskRow, TaskRow.id == InvocationRow.task_id).where(
        TaskRow.root_task_id == task.root_task_id,
        InvocationRow.status.in_(('queued', 'starting', 'running', 'unknown'))))
    if active:
        _refuse('invocation_active', '研究仍有排队、活动或未知调用，先核对它们')
    step = session.get(PaperStepRow, body['step_id'])
    if step is None or not _eligible(task, step):
        _refuse('checkpoint_invalid', '原步骤不再是可恢复的未完成讲解断点')
    try:
        offer, checked, audit, corrections, paper = _prepare(session, task, step, body['invocation_id'], body.get('corrections', []))
    except FormatError as exc:
        _refuse('format_invalid', str(exc)[:300])
    if offer['fingerprint'] != body['fingerprint']:
        _refuse('stale', '断点或原材料已改变，请重新预览')
    if not offer['valid']:
        _refuse('validation_failed', '仍有引用或结构错误，原步骤未改变；请先处理预览中的问题')
    before = {k:copy.deepcopy(getattr(step, k)) for k in ('status','result','error','finished_at')}
    receipt = PaperStepRow(task_id=task.id, key=key, kind='tool', name='recover_saved', target=step.target,
        input_hash=request_hash, status='succeeded', attempts=1, invocation_ids=[], started_at=now, finished_at=now,
        result={'step_id':step.id, 'source_invocation':body['invocation_id'], 'fingerprint':offer['fingerprint'],
            'before':before, 'corrections':corrections, 'reference_audit':audit, 'restored_units':offer['units'],
            'math_reviewed':False, 'model_calls':0, 'recovered_by':user_id})
    # JSON timestamps in the audit must stay serializable.
    receipt.result['before']['finished_at'] = before['finished_at'].isoformat() if before['finished_at'] else None
    session.add(receipt)
    session.flush()
    checked = {**checked, '_invocation_id':body['invocation_id'],
        '_offline_recovery':{'receipt_id':receipt.id, 'reviewed':False, 'model_calls':0}}
    step.status, step.result, step.error, step.finished_at = 'succeeded', checked, None, now
    studies = dict((task.outcome or {}).get('studies') or {})
    old = studies.get(paper.id, {})
    studies[paper.id] = {**old, **{k:v for k,v in checked.items() if not k.startswith('_')},
        'paper_id':paper.id, 'title':paper.title, 'finished':False, 'depth':'partial',
        'review_status':'needs_review', 'issues':old.get('issues', []),
        'coverage':offer['coverage'],
        'recovery':{'receipt_id':receipt.id, 'source_invocation':body['invocation_id'], 'math_reviewed':False}}
    task.outcome = {**(task.outcome or {}), 'studies':studies}
    paper.read_status = 'pending'
    from agenthub_kernel.autopilot import _event
    _event(session, stage, 'paper_saved_reply_recovered', invocation_id=body['invocation_id'],
        step_id=step.id, receipt_id=receipt.id, units=offer['units'], model_calls=0)
    return {'receipt_id':receipt.id, 'step_id':step.id, 'units':offer['units'], 'duplicate':False,
        'model_calls':0, 'math_reviewed':False, 'stage_status':stage.status}
