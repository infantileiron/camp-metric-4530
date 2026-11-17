"""Backend tests for approval→outbox→dispatch and receiver compatibility.

Run:  cd backend && python3 -m pytest tests -q
Uses SQLite so it does not need PostgreSQL/Redis; the receiver is exercised
in-process through its own store module.
"""
import json
import os
import sys

os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:///:memory:')

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'backend'))
sys.path.insert(0, os.path.join(ROOT, 'receiver'))

from src.config.database import Base
from src.models.entities import User, Metric, Snapshot, Delivery
from src.routes.api import approve
from src.services import delivery, packages
from src.services.analysis import calculate
from src.services.business import dump

import store as receiver_store


@pytest.fixture()
def s():
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def receiver_db(tmp_path, monkeypatch):
    monkeypatch.setenv('RECEIVER_DB', str(tmp_path / 'receiver.db'))
    receiver_store.connect().close()
    return tmp_path / 'receiver.db'


def make_snapshot(s, note='净实到口径已核对，同意签核冻结'):
    metric = Metric(code='completion', version=2, name='净实到结课达成率', numerator='completed',
                    denominator='attended - refunds', numerator_mode='全部', denominator_mode='全部', note='')
    s.add(metric)
    s.commit()
    facts = [
        dict(id=1, class_code='QH-01', day='2026-11-15', mode='线上', enrolled=30, attended=25,
             completed=20, refunds=1, note=''),
        dict(id=2, class_code='QH-02', day='2026-11-15', mode='线下', enrolled=28, attended=22,
             completed=18, refunds=2, note=''),
    ]
    frozen = {'metric': dump(metric), 'facts': facts, 'baseline': None,
              'date_from': '2026-11-15', 'date_to': '2026-11-15'}
    result = {**calculate(frozen), 'snapshot': frozen}
    snapshot = Snapshot(metric_id=metric.id, date_from='2026-11-15', date_to='2026-11-15',
                        state='done', result=result, note=note, approval='draft')
    s.add(snapshot)
    s.commit()
    return metric, snapshot


def approve_as(s, snapshot, email='admin@demo.local', role='admin'):
    user = User(email=email, role=role)
    s.add(user)
    s.commit()
    return approve(snapshot.id, u=user, s=s)


def test_approval_leaves_pending_delivery_even_if_receiver_down(s, monkeypatch):
    monkeypatch.delenv('RECEIVER_URL', raising=False)
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    row = s.query(Delivery).one()
    assert row.status == 'pending' and row.snapshot_id == snapshot.id
    assert row.metric_code == 'completion' and row.revision == 1
    assert row.payload['checksum'] == packages.compute_checksum(row.payload)
    assert s.get(Snapshot, snapshot.id).approval == 'approved'


def test_dispatch_confirms_and_retry_after_lost_ack(s, receiver_db, monkeypatch):
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    row = s.query(Delivery).one()

    # Simulate "receiver accounted but our acknowledgement was lost": deliver
    # straight to the receiver, then let the sender retry with no ack.
    status, body = receiver_store.ingest(row.payload)
    assert status == 201 and body['accounted'] is True

    class LostAck:
        def send(self, request, timeout=5):
            raise OSError('connection reset after processing')

    delivery.dispatch_once(s, http_client=LostAck())
    row = s.query(Delivery).one()
    assert row.status == 'failed' and row.attempts == 1

    delivery.dispatch_once(s, http_client=_ReceiverClient())
    row = s.query(Delivery).one()
    assert row.status == 'confirmed'
    assert row.ack['outcome'] == 'already_accounted'
    assert receiver_store.stats()['accounts'] == 1


class _ReceiverClient:
    """In-process HTTP stand-in that calls the receiver store directly.

    Like urllib, a non-2xx response surfaces as HTTPError; the sender reads
    the receiver's JSON verdict from it.
    """

    def send(self, request, timeout=5):
        from urllib.error import HTTPError
        import io

        package = json.loads(request.data.decode())
        status, body = receiver_store.ingest(package)
        if status >= 400:
            raise HTTPError(request.full_url, status, 'receiver verdict', {},
                            io.BytesIO(json.dumps(body).encode()))
        return _Response(status, body)


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sender_package_passes_receiver_verification(s, receiver_db):
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    row = s.query(Delivery).one()
    status, body = receiver_store.ingest(row.payload)
    assert status == 201, body
    assert body['summary']['numerator'] == 38
    assert body['summary']['denominator'] == 44
    assert body['summary']['value'] == pytest.approx(38 / 44)
    assert [g['class_code'] for g in body['groups']] == ['QH-01', 'QH-02']


def test_newer_approval_replaces_and_old_revision_is_superseded(s, receiver_db, monkeypatch):
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    metric, first = make_snapshot(s, note='第一次签核，净实到口径')
    approve_as(s, first)
    second = Snapshot(metric_id=metric.id, date_from='2026-11-15', date_to='2026-11-15',
                      state='done', result=first.result, note='第二次签核，事实修订后重算', approval='draft')
    s.add(second)
    s.commit()
    approve_as(s, second, email='admin2@demo.local')

    rows = s.query(Delivery).order_by(Delivery.revision).all()
    assert [r.revision for r in rows] == [1, 2]

    # Old package arrives after the new one: must not overwrite.
    receiver_store.ingest(rows[1].payload)
    status, body = receiver_store.ingest(rows[0].payload)
    assert status == 409 and body['status'] == 'stale'

    delivery.dispatch_once(s, http_client=_ReceiverClient())
    # Newest first: v2 confirms and locally supersedes v1 before v1 is sent.
    assert s.query(Delivery).filter_by(revision=2).one().status == 'confirmed'
    assert s.query(Delivery).filter_by(revision=1).one().status == 'superseded'
    assert s.query(Delivery).filter_by(revision=1).one().attempts == 0
    assert receiver_store.account_for('completion', '2026-11-15', '2026-11-15')['revision'] == 2
    assert receiver_store.stats()['receipts'] == 2  # v2 send + the explicit stale test below

    # An in-flight v1 that hits the wire after v2 is still refused by the receiver.
    assert receiver_store.ingest(rows[0].payload)[0] == 409
    assert receiver_store.account_for('completion', '2026-11-15', '2026-11-15')['revision'] == 2


def test_reapproval_and_retry_never_duplicate_receiver_account(s, receiver_db, monkeypatch):
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    row = s.query(Delivery).one()
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    for _ in range(3):
        delivery.dispatch_once(s, http_client=_ReceiverClient())
    assert s.query(Delivery).one().status == 'confirmed'
    assert receiver_store.stats()['receipts'] == 1
    assert receiver_store.stats()['accounts'] == 1


def test_retry_confirmed_delivery_is_refused(s, receiver_db, monkeypatch):
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    delivery.dispatch_once(s, http_client=_ReceiverClient())
    row = s.query(Delivery).one()
    assert delivery.retry(s, row, User(email='analyst@demo.local', role='analyst')) is row
    assert row.status == 'confirmed'


def test_receiver_rejects_tampered_ack_as_failure(s, receiver_db, monkeypatch):
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')

    class Tamperer(_ReceiverClient):
        def send(self, request, timeout=5):
            package = json.loads(request.data.decode())
            package['summary']['numerator'] = 999
            package['checksum'] = packages.compute_checksum(package)
            status, body = receiver_store.ingest(package)
            return _Response(status, body)

    delivery.dispatch_once(s, http_client=Tamperer())
    row = s.query(Delivery).one()
    assert row.status == 'rejected'
    assert receiver_store.stats()['accounts'] == 0


def test_crash_leftover_sending_row_is_recovered(s, receiver_db, monkeypatch):
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    metric, snapshot = make_snapshot(s)
    approve_as(s, snapshot)
    row = s.query(Delivery).one()
    # Simulate a crash between claiming the row and finishing the send.
    row.status = 'sending'
    row.updated_at = '2000-01-01T00:00:00+00:00'
    s.commit()
    assert delivery.recover(s) == 1
    assert s.query(Delivery).one().status == 'pending'
    delivery.dispatch_once(s, http_client=_ReceiverClient())
    assert s.query(Delivery).one().status == 'confirmed'
    assert receiver_store.stats()['accounts'] == 1


def test_duplicate_after_replacement_reports_superseded_but_not_reaccounted(s, receiver_db, monkeypatch):
    monkeypatch.setenv('RECEIVER_URL', 'http://receiver.invalid')
    metric, first = make_snapshot(s)
    approve_as(s, first)
    second = Snapshot(metric_id=metric.id, date_from='2026-11-15', date_to='2026-11-15',
                      state='done', result=first.result, note='第二次签核，事实修订后重算', approval='draft')
    s.add(second)
    s.commit()
    approve_as(s, second, email='admin2@demo.local')
    rows = s.query(Delivery).order_by(Delivery.revision).all()

    receiver_store.ingest(rows[0].payload)
    receiver_store.ingest(rows[1].payload)
    status, body = receiver_store.ingest(rows[0].payload)
    assert status == 200 and body['outcome'] == 'already_accounted_superseded'
    assert body['superseded_by_revision'] == 2
    assert receiver_store.stats()['accounts'] == 1
