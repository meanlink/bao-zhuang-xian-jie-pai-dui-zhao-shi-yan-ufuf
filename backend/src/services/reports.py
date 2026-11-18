import time, os, csv, io, math, hashlib, json
from fastapi import HTTPException
from sqlalchemy import select
from src.models import Job, Report, Delivery, Dataset
from src.config.settings import ARTIFACT_DIR
from src.services.business import analyze
# 外发防倒推：同一批次只放行一种口径（总体或分班全套），每个公开组至少 5 段。
MIN_GROUP_SEGMENTS = 5
SCOPE_LABELS = {'overall': '总体', 'by_shift': '分班全套'}
class ReleaseRejected(Exception):
    """口径不满足外发条件：本次拒发，计划保留并按下个周期重试。"""
def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True, scope=data.scope, last_status='pending', last_reason='')
    db.add(r); db.flush()
    return r
def _group_counts(records):
    counts={}
    seen=set()
    for r in records:
        try:
            arm,run,shift=str(r['arm']),str(r['run']),str(r['shift'])
            seconds=float(r['seconds'])
            if arm not in ('A','B') or not math.isfinite(seconds) or seconds<=0 or run in seen: continue
        except (KeyError,TypeError,ValueError): continue
        seen.add(run)
        counts.setdefault(shift,{'A':0,'B':0})[arm]+=1
    return counts
def _release_payload(records, params, scope):
    """按口径生成外发内容；内部分析结果不改动。组不足 5 段时拒绝。"""
    confidence=(params or {}).get('confidence',.95)
    counts=_group_counts(records)
    if scope=='overall':
        total={'A':sum(c['A'] for c in counts.values()),'B':sum(c['B'] for c in counts.values())}
        bad=[f'{arm} 组 {n} 段' for arm,n in total.items() if n<MIN_GROUP_SEGMENTS]
        if bad: raise ReleaseRejected('总体口径下 '+'、'.join(bad)+f'，不足 {MIN_GROUP_SEGMENTS} 段，暂缓发布')
        try: return {'scope':'overall','result':analyze(records,{'confidence':confidence,'shift':'全部'})}
        except ValueError as e: raise ReleaseRejected(f'总体口径无法生成简报：{e}')
    shifts=sorted(counts)
    if not shifts: raise ReleaseRejected('批次内没有可分班的有效片段，暂缓发布')
    bad=[f'{s}·{arm} 组 {counts[s][arm]} 段' for s in shifts for arm in ('A','B') if counts[s][arm]<MIN_GROUP_SEGMENTS]
    if bad: raise ReleaseRejected('分班口径要求每个公开组至少 '+str(MIN_GROUP_SEGMENTS)+' 段：'+'、'.join(bad)+' 不足，可改走总体口径或暂缓发布')
    groups=[]
    for s in shifts:
        try: result=analyze(records,{'confidence':confidence,'shift':s})
        except ValueError as e: raise ReleaseRejected(f'班次 {s} 无法生成简报：{e}')
        groups.append({'shift':s,'result':result})
    return {'scope':'by_shift','groups':groups}
def _csv_bytes(payload):
    out=io.StringIO()
    out.write('﻿')
    writer=csv.writer(out)
    def safe(v):
        if isinstance(v,(int,float)) and not isinstance(v,bool): return v
        text=str(v)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    if payload['scope']=='overall':
        table=payload['result'].get('table',{})
        cols=table.get('columns',[])
        writer.writerow([safe(c['label']) for c in cols])
        for row in table.get('rows',[]): writer.writerow([safe(row.get(c['key'],'')) for c in cols])
    else:
        cols=[('shift','班次'),('arm','实验组'),('n','片段数'),('mean','均值/秒'),('sd','样本标准差/秒')]
        writer.writerow([label for _,label in cols])
        for g in payload['groups']:
            for row in g['result'].get('table',{}).get('rows',[]):
                full={'shift':g['shift'],**row}
                writer.writerow([safe(full.get(key,'')) for key,_ in cols])
    return out.getvalue().encode('utf-8')
def _finalize(db, report, dataset, job, delivery, payload):
    data=_csv_bytes(payload)
    os.makedirs(ARTIFACT_DIR,exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR,delivery.filename),'wb') as f:f.write(data)
    checksum=hashlib.sha256(data).hexdigest()
    with open(os.path.join(ARTIFACT_DIR,delivery.filename+'.json'),'w') as f: json.dump({'job':job.name,'params':job.params,'release':payload,'checksum':checksum},f,ensure_ascii=False,allow_nan=False)
    delivery.checksum=checksum
    delivery.status='delivered'
    if not dataset.released_scope: dataset.released_scope=delivery.scope # 首份可下载简报锁定全批口径，停用也不解锁
    report.last_status='delivered'
    report.last_reason=''
    report.next_run=time.time()+report.interval_seconds
def _reject(db, report, reason):
    report.last_status='rejected'
    report.last_reason=reason[:490]
    report.next_run=time.time()+report.interval_seconds
    return None
def deliver(db, report):
    j=db.get(Job,report.job_id)
    if not j or not j.approved: return None
    ds=db.get(Dataset,j.dataset_id)
    # 发布中断后恢复原申请：续用同一条归档记录，重试不新增归档。
    pending=db.scalar(select(Delivery).where(Delivery.report_id==report.id,Delivery.status=='publishing'))
    if pending:
        payload=_release_payload(ds.records,j.params,pending.scope)
        _finalize(db,report,ds,j,pending,payload)
        return pending
    if ds.released_scope and ds.released_scope!=report.scope:
        return _reject(db,report,f'本批次已按「{SCOPE_LABELS[ds.released_scope]}」口径外发，混发可被倒推出未公开班次数据，拒绝以「{SCOPE_LABELS[report.scope]}」口径发布')
    try:
        payload=_release_payload(ds.records,j.params,report.scope)
    except ReleaseRejected as e:
        return _reject(db,report,str(e))
    delivery=Delivery(report_id=report.id, created_at=time.time(), checksum='', filename='', destination=report.destination, status='publishing', scope=report.scope)
    db.add(delivery); db.flush()
    delivery.filename=f'report-{report.id}-delivery-{delivery.id}.csv'
    db.commit() # 归档记录先落库，中断后按原申请恢复
    _finalize(db,report,ds,j,delivery,payload)
    return delivery
