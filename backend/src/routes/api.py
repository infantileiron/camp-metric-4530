import os,json
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Metric,Fact,Snapshot,Audit,Delivery
from src.schemas.business import Login,MetricInput,FactInput,RunInput,NoteInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.repository import save_metric,save_fact
from src.services import delivery as deliveries
from src.controllers import business as ensure
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/metrics')
def metrics(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Metric).order_by(Metric.code,Metric.version).all()]
@router.post('/metrics')
def create_metric(data:MetricInput,u=Depends(editor),s=Depends(db)):
 r=save_metric(s,data);audit(s,u,'建立口径 '+r.code+' v'+str(r.version));return dump(r)
@router.put('/metrics/{i}')
def edit_metric(i:int,data:MetricInput,u=Depends(editor),s=Depends(db)):
 r=save_metric(s,data,i);audit(s,u,'修订口径 '+str(i));return dump(r)
@router.get('/facts')
def facts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Fact).order_by(Fact.day.desc(),Fact.class_code).all()]
@router.post('/facts')
def create_fact(data:FactInput,u=Depends(editor),s=Depends(db)):
 r=save_fact(s,data);audit(s,u,'录入事实 '+str(r.id));return dump(r)
@router.put('/facts/{i}')
def edit_fact(i:int,data:FactInput,u=Depends(editor),s=Depends(db)):
 r=save_fact(s,data,i);audit(s,u,'修订事实 '+str(i));return dump(r)
@router.get('/snapshots')
def snapshots(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Snapshot).order_by(Snapshot.id.desc()).all()]
@router.post('/snapshots')
def run(data:RunInput,u=Depends(editor),s=Depends(db)):
 m=ensure.metric(s,data.metric_id);baseline=None
 if data.baseline_id:
  b=ensure.snapshot(s,data.baseline_id)
  if b.state!='done':raise HTTPException(409,'仅成功快照可作为比较基线')
  if b.result['metric']['code']!=m.code:raise HTTPException(409,'只能比较同一指标代码的口径版本')
  baseline=dict(id=b.id,date_from=b.date_from,date_to=b.date_to,result={k:b.result[k] for k in ['metric','value','facts_checksum']})
 values=data.model_dump(mode='json');rows=[dump(f) for f in s.query(Fact).filter(Fact.day>=values['date_from'],Fact.day<=values['date_to']).order_by(Fact.day,Fact.class_code).all()]
 frozen={'metric':dump(m),'facts':rows,'baseline':baseline,'date_from':values['date_from'],'date_to':values['date_to']}
 r=Snapshot(**values,result={'snapshot':frozen});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('camp:snapshots',r.id)
 except redis.RedisError:r.state='failed';r.error='队列不可用，请恢复后重新试算';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交试算 '+str(r.id));return dump(r)
@router.get('/snapshots/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure.snapshot(s,i))
@router.put('/snapshots/{i}/note')
def note(i:int,data:NoteInput,u=Depends(editor),s=Depends(db)):
 r=ensure.snapshot(s,i)
 if r.state!='done' or r.approval=='approved':raise HTTPException(409,'仅成功且未签核快照可编辑意见')
 r.note=data.note;s.commit();audit(s,u,'起草签核意见 '+str(i));return dump(r)
@router.post('/snapshots/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 for attempt in range(4):
  r=ensure.snapshot(s,i)
  if r.state!='done' or r.approval=='approved':raise HTTPException(409,'仅成功且未签核快照可签核')
  if r.result['value'] is None:raise HTTPException(409,'零分母undefined快照不能签核，请修正口径')
  if len(r.note)<8:raise HTTPException(409,'先填写至少8字业务解释')
  m=ensure.metric(s,r.metric_id)
  if any(getattr(m,k)!=v for k,v in r.result['metric'].items() if k not in ['locked']):raise HTTPException(409,'口径在试算后已变更，请用当前版本重新试算再签核')
  approved_at=deliveries.now_iso()
  try:
   r.approval='approved';r.approved_by=u.email
   deliveries.enqueue(s,r,u.email,approved_at)
   m.locked=True
   audit(s,u,'签核冻结并登记待报送 '+str(i))
   s.commit()
   return dump(r)
  except IntegrityError:
   # 并发签核同一指标日期窗抢到同一版本号，或同一快照被重复签核：
   # 整体回滚（签核标记不落库）后重新读状态再试。
   s.rollback()
   if attempt==3:raise HTTPException(409,'并发签核冲突，请刷新后重试')
 return dump(r)
@router.get('/snapshots/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure.snapshot(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功快照可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="camp-metric-{i}.json"'})
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
@router.get('/deliveries')
def deliveries_list(u=Depends(current),s=Depends(db)):
 items=[]
 for r in s.query(Delivery).order_by(Delivery.id.desc()).all():
  item=dump(r);item.pop('payload',None);items.append(item)
 return {'receiver_configured':bool(deliveries.receiver_url()),'items':items}
@router.post('/deliveries/{i}/retry')
def deliveries_retry(i:int,u=Depends(editor),s=Depends(db)):
 r=s.get(Delivery,i)
 if not r:raise HTTPException(404,'报送记录不存在')
 if r.status=='confirmed':raise HTTPException(409,'接收端已确认，无需补发')
 if r.snapshot_id and ensure.snapshot(s,r.snapshot_id).approval!='approved':raise HTTPException(409,'仅已签核快照可补发')
 r=deliveries.retry(s,r,u);s.commit();item=dump(r);item.pop('payload',None);return item
@router.post('/deliveries/dispatch')
def deliveries_dispatch(u=Depends(editor),s=Depends(db)):
 if not deliveries.receiver_url():raise HTTPException(503,'未配置接收端地址 RECEIVER_URL')
 try:results=deliveries.dispatch(s)
 except deliveries.DeliveryError as exc:raise HTTPException(503,str(exc))
 return {'results':[{'id':i,'status':st} for i,st in results]}
@router.get('/deliveries/{i}/reconcile')
def deliveries_reconcile(i:int,u=Depends(current),s=Depends(db)):
 r=s.get(Delivery,i)
 if not r:raise HTTPException(404,'报送记录不存在')
 try:return deliveries.reconcile(r)
 except deliveries.DeliveryError as exc:raise HTTPException(503,str(exc))
@router.get('/deliveries/{i}/package')
def deliveries_package(i:int,u=Depends(current),s=Depends(db)):
 r=s.get(Delivery,i)
 if not r:raise HTTPException(404,'报送记录不存在')
 return Response(json.dumps(r.payload,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="camp-metric-package-{r.package_id[:12]}.json"'})
