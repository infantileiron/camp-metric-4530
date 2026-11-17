import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import Snapshot
from src.services.analysis import calculate
from src.services import delivery
r=redis.Redis.from_url(os.environ['REDIS_URL'])
with Session() as recovery:
 for pending in recovery.query(Snapshot).filter(Snapshot.state.in_(['queued','running'])).all():
  pending.state='queued';r.rpush('camp:snapshots',pending.id)
 recovery.commit()
DISPATCH_EVERY=float(os.getenv('DELIVERY_INTERVAL_SECONDS','10'))
last_dispatch=0.0
while True:
 Path('/tmp/heartbeat').touch()
 try:
  if delivery.receiver_url() and time.time()-last_dispatch>=DISPATCH_EVERY:
   last_dispatch=time.time()
   with Session() as outbox:
    try:delivery.dispatch(outbox,limit=5,on_progress=lambda:Path('/tmp/heartbeat').touch())
    except Exception:logging.exception('delivery dispatch')
  item=r.blpop('camp:snapshots',timeout=3)
  if not item:continue
  with Session() as s:
   row=s.get(Snapshot,int(item[1]))
   if not row or row.state!='queued':continue
   snapshot=row.result['snapshot'];row.state='running';s.commit()
   try:
    row.result={**calculate(snapshot),'snapshot':snapshot};row.state='done'
    Path('/artifacts/'+str(row.id)+'.json').write_text(json.dumps(row.result,ensure_ascii=False))
   except Exception as e:row.state='failed';row.error=str(e)
   s.commit()
 except Exception:logging.exception('worker');time.sleep(2)
