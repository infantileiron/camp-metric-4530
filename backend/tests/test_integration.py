"""Full HTTP integration: signing → outbox → real receiver → reconcile.

Run: cd backend && python3 -m pytest tests/test_integration.py -q
The receiver runs as a separate local process on SQLite; the backend uses its
FastAPI app with SQLite so Docker/PostgreSQL/Redis are not required.
"""
import os
import socket
import subprocess
import sys
import time

os.environ.setdefault('JWT_SECRET', 'integration-secret')

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'backend'))
sys.path.insert(0, os.path.join(ROOT, 'receiver'))

os.environ['DATABASE_URL'] = 'sqlite+pysqlite:///:memory:'

from src.config.database import Base, db as db_dependency  # noqa: E402
from src.main import app  # noqa: E402
from src.models.entities import Delivery, Fact, Metric, Snapshot, User  # noqa: E402
from src.services.analysis import calculate  # noqa: E402
from src.utils.security import hash_password  # noqa: E402


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture()
def receiver_process(tmp_path):
    port = _free_port()
    env = dict(os.environ, RECEIVER_DB=str(tmp_path / 'receiver.db'), RECEIVER_PORT=str(port),
               RECEIVER_TOKEN='integration-token')
    process = subprocess.Popen([sys.executable, os.path.join(ROOT, 'receiver', 'server.py')],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        raise RuntimeError('receiver failed to start')
    env['RECEIVER_URL'] = f'http://127.0.0.1:{port}'
    yield env, port
    process.terminate()
    process.wait(timeout=5)


@pytest.fixture()
def client():
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        with Session() as session:
            yield session

    app.dependency_overrides[db_dependency] = override
    session = Session()
    seed(session)
    with TestClient(app) as test_client:
        test_client.session = session
        yield test_client
    app.dependency_overrides.clear()
    session.close()


def seed(s):
    user = User(email='admin@demo.local', role='admin', password=hash_password('Demo123!'))
    metric = Metric(code='completion', version=2, name='净实到结课达成率', numerator='completed',
                    denominator='attended - refunds', numerator_mode='全部', denominator_mode='全部', note='')
    s.add_all([user, metric])
    s.commit()
    facts = [
        dict(id=1, class_code='QH-01', day='2026-11-15', mode='线上', enrolled=30, attended=25,
             completed=20, refunds=1, note=''),
        dict(id=2, class_code='QH-02', day='2026-11-15', mode='线下', enrolled=28, attended=22,
             completed=18, refunds=2, note=''),
    ]
    frozen = {'metric': {c.name: getattr(metric, c.name) for c in metric.__table__.columns},
              'facts': facts, 'baseline': None, 'date_from': '2026-11-15', 'date_to': '2026-11-15'}
    result = {**calculate(frozen), 'snapshot': frozen}
    snapshot = Snapshot(metric_id=metric.id, date_from='2026-11-15', date_to='2026-11-15',
                        state='done', result=result, note='净实到口径已核对，同意签核冻结', approval='draft')
    s.add(snapshot)
    s.commit()
    return user, metric, snapshot


def login(client):
    response = client.post('/auth/login', json={'email': 'admin@demo.local', 'password': 'Demo123!'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_sign_offline_then_later_dispatch_and_reconcile(client, receiver_process):
    env, port = receiver_process
    headers = login(client)
    snapshot_id = client.session.query(Snapshot).one().id

    # 1. Receiver not configured yet: signing must still succeed and leave a pending record.
    os.environ.pop('RECEIVER_URL', None)
    response = client.post(f'/snapshots/{snapshot_id}/approve', headers=headers)
    assert response.status_code == 200
    deliveries = client.get('/deliveries', headers=headers).json()
    assert deliveries['receiver_configured'] is False
    assert len(deliveries['items']) == 1 and deliveries['items'][0]['status'] == 'pending'
    delivery_id = deliveries['items'][0]['id']

    # 2. Dispatch to a down receiver: stays failed, no data loss.
    os.environ['RECEIVER_URL'] = 'http://127.0.0.1:9'
    response = client.post('/deliveries/dispatch', headers=headers)
    assert response.status_code in (200, 503)
    assert client.get('/deliveries', headers=headers).json()['items'][0]['status'] in ('failed', 'pending')

    # 3. Start address correct: dispatch confirms and the receiver accounted it once.
    os.environ['RECEIVER_URL'] = env['RECEIVER_URL']
    os.environ['RECEIVER_TOKEN'] = 'integration-token'
    response = client.post('/deliveries/dispatch', headers=headers)
    assert response.status_code == 200
    assert response.json()['results'] == [{'id': delivery_id, 'status': 'confirmed'}]
    listed = client.get('/deliveries', headers=headers).json()['items'][0]
    assert 'payload' not in listed and listed['ack']['accounting'] == '已入账'

    # 4. Retrying the same dispatch must not duplicate accounting on the receiver.
    response = client.post('/deliveries/dispatch', headers=headers)
    assert response.status_code == 200
    assert response.json()['results'] == []
    with socket.create_connection(('127.0.0.1', port), timeout=2):
        pass
    import json
    import urllib.request
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/accounts',
                                     headers={'Authorization': 'Bearer integration-token'})
    with urllib.request.urlopen(request, timeout=5) as reply:
        accounts = json.loads(reply.read())
    assert len(accounts) == 1
    assert accounts[0]['revision'] == 1 and accounts[0]['numerator'] == 38

    # 5. Reconciliation basis is visible on both sides.
    reconcile = client.get(f'/deliveries/{delivery_id}/reconcile', headers=headers).json()
    assert reconcile['in_sync'] is True
    assert reconcile['receiver']['groups'] == [
        {'class_code': 'QH-01', 'numerator': 20, 'denominator': 24, 'value': 20 / 24},
        {'class_code': 'QH-02', 'numerator': 18, 'denominator': 20, 'value': 18 / 20},
    ]

    # 6. Original snapshot and its download remain available.
    assert client.get(f'/snapshots/{snapshot_id}', headers=headers).json()['approval'] == 'approved'
    export = client.get(f'/snapshots/{snapshot_id}/export', headers=headers)
    assert export.status_code == 200 and export.headers['content-disposition'].endswith('.json"')
    package = client.get(f'/deliveries/{delivery_id}/package', headers=headers)
    assert package.status_code == 200
