"""End-to-end tests for the receiver's verification and accounting rules.

Run from repository root:  python3 -m pytest receiver/test_receiver.py -q
Standard library + pytest only; no Docker/PostgreSQL/Redis required.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, 'backend')
RECEIVER = os.path.join(ROOT, 'receiver')
sys.path.insert(0, RECEIVER)
sys.path.insert(0, BACKEND)

import store  # noqa: E402
import verify  # noqa: E402


def _fact(code, day, enrolled, attended, completed, refunds, mode='线上'):
    return {'class_code': code, 'day': day, 'mode': mode, 'enrolled': enrolled,
            'attended': attended, 'completed': completed, 'refunds': refunds, 'note': ''}


def _package(facts, revision=1, package_id=None, numerator='completed', denominator='attended - refunds',
             code='completion', package_checksum_mode='auto'):
    summary_num = sum(f['completed'] for f in facts)
    summary_den = sum(f['attended'] - f['refunds'] for f in facts)
    groups = []
    for class_code in sorted({f['class_code'] for f in facts}):
        rows = [f for f in facts if f['class_code'] == class_code]
        n = sum(r['completed'] for r in rows)
        d = sum(r['attended'] - r['refunds'] for r in rows)
        groups.append({'class_code': class_code, 'numerator': n, 'denominator': d,
                       'value': None if d == 0 else n / d})
    package = {
        'schema': 'camp-metric-package-v1',
        'package_id': package_id or ('a' * 31 + str(revision % 10)),
        'snapshot_id': 10 + revision,
        'metric': {'id': 1, 'code': code, 'version': 1, 'name': '结课达成率',
                   'numerator': numerator, 'denominator': denominator,
                   'numerator_mode': '全部', 'denominator_mode': '全部'},
        'date_from': min(f['day'] for f in facts),
        'date_to': max(f['day'] for f in facts),
        'revision': revision,
        'approved_at': '2026-11-16T00:00:00+00:00',
        'approved_by': 'admin@demo.local',
        'summary': {'value': summary_num / summary_den if summary_den else None,
                    'numerator': summary_num, 'denominator': summary_den,
                    'rows': len(facts), 'numerator_rows': len(facts),
                    'denominator_rows': len(facts),
                    'facts_checksum': verify.facts_checksum(facts),
                    'undefined_reason': '' if summary_den else '外层聚合分母为零'},
        'groups': groups,
        'facts': facts,
        'method': 'test',
        'analysis_version': 'camp-aggregate-ratio-v1',
    }
    package['checksum'] = verify.compute_checksum(package)
    if package_checksum_mode == 'tamper':
        package['summary']['numerator'] = 999
        package['checksum'] = verify.compute_checksum(package)
    return package


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('RECEIVER_DB', str(tmp_path / 'receiver.db'))
    store.connect().close()
    return tmp_path / 'receiver.db'


FACTS = [
    _fact('QH-01', '2026-11-15', 30, 25, 20, 1),
    _fact('QH-02', '2026-11-15', 28, 22, 18, 2),
]


def test_valid_package_is_recomputed_and_accounted(db):
    package = _package(FACTS)
    status, body = store.ingest(package)
    assert status == 201 and body['accounted'] is True
    assert body['summary']['numerator'] == 38
    assert body['summary']['denominator'] == 44
    account = store.account_for('completion', '2026-11-15', '2026-11-15')
    assert account['revision'] == 1
    assert [g['class_code'] for g in account['groups']] == ['QH-01', 'QH-02']
    assert store.list_receipts()[0]['accounting'] == '已入账'


def test_duplicate_delivery_does_not_account_twice(db):
    package = _package(FACTS)
    assert store.ingest(package)[0] == 201
    status, body = store.ingest(package)  # lost ack / retry with identical content
    assert status == 200
    assert body['status'] == 'duplicate' and body['outcome'] == 'already_accounted'
    assert body['accounted'] is True
    receipts = store.list_receipts()
    assert len(receipts) == 1
    assert store.stats()['accounts'] == 1


def test_same_package_id_different_content_rejected(db):
    package = _package(FACTS)
    assert store.ingest(package)[0] == 201
    altered = _package(FACTS, package_id=package['package_id'])
    altered['facts'][0]['completed'] = 21
    altered['summary']['numerator'] = 39
    altered['checksum'] = verify.compute_checksum(altered)
    status, body = store.ingest(altered)
    assert status == 409 and body['status'] == 'conflict' and body['accounted'] is False
    assert store.stats()['conflicts'] == 1
    account = store.account_for('completion', '2026-11-15', '2026-11-15')
    assert account['revision'] == 1
    stored = store.get_receipt(package['package_id'])
    assert stored['payload']['summary']['numerator'] == 38


def test_tampered_total_rejected_and_original_payload_kept(db):
    package = _package(FACTS, package_checksum_mode='tamper')
    status, body = store.ingest(package)
    assert status == 422 and body['accounted'] is False
    assert any(d['field'] == 'numerator' for d in body['differences'])
    receipt = store.get_receipt(package['package_id'])
    assert receipt['payload']['summary']['numerator'] == 999
    assert store.stats()['accounts'] == 0


def test_tampered_fact_row_breaks_checksum(db):
    package = _package(FACTS)
    package['facts'][0]['attended'] = 26
    status, body = store.ingest(package)
    assert status == 422
    assert any('checksum' in e for e in body['errors'])
    assert store.stats()['accounts'] == 0


def test_newer_revision_replaces_older(db):
    assert store.ingest(_package(FACTS, revision=1))[0] == 201
    newer_facts = [_fact('QH-01', '2026-11-15', 30, 26, 21, 1), _fact('QH-02', '2026-11-15', 28, 22, 18, 2)]
    status, body = store.ingest(_package(newer_facts, revision=2))
    assert status == 201 and body['status'] == 'replaced'
    account = store.account_for('completion', '2026-11-15', '2026-11-15')
    assert account['revision'] == 2 and account['numerator'] == 39
    assert [g['numerator'] for g in account['groups']] == [21, 18]


def test_out_of_order_old_revision_does_not_overwrite(db):
    assert store.ingest(_package(FACTS, revision=2))[0] == 201
    status, body = store.ingest(_package(FACTS, revision=1))
    assert status == 409 and body['status'] == 'stale'
    assert body['accounted'] is False
    assert store.account_for('completion', '2026-11-15', '2026-11-15')['revision'] == 2
    # later retry of the newest revision is still an idempotent no-op
    assert store.ingest(_package(FACTS, revision=2))[0] == 200


def test_same_revision_different_package_rejected(db):
    first = _package(FACTS, revision=1)
    assert store.ingest(first)[0] == 201
    other = _package(FACTS, revision=1, package_id='b' * 32)
    status, body = store.ingest(other)
    assert status == 409 and body['outcome'] == 'same_revision_different_package'
    assert body['accounted'] is False
    assert store.account_for('completion', '2026-11-15', '2026-11-15')['package_id'] == first['package_id']


def test_unknown_metric_field_rejected(db):
    package = _package(FACTS)
    package['metric']['numerator'] = '__import__("os").system("id")'
    package['checksum'] = verify.compute_checksum(package)
    status, body = store.ingest(package)
    assert status == 422 and body['accounted'] is False


def test_zero_denominator_keeps_null_not_zero(db):
    facts = [_fact('QH-01', '2026-11-15', 0, 0, 0, 0)]
    package = _package(facts)
    status, body = store.ingest(package)
    assert status == 201
    assert body['summary']['value'] is None
    assert body['summary']['denominator'] == 0


# --- HTTP-level behaviour: concurrency, lost ack, process restart ---------

def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    env = dict(os.environ, RECEIVER_DB=str(tmp_path / 'http.db'), RECEIVER_PORT=str(port),
               RECEIVER_HOST='127.0.0.1', RECEIVER_TOKEN='test-token')
    proc = subprocess.Popen([sys.executable, os.path.join(RECEIVER, 'server.py')], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError('receiver did not start')
    yield port, env, proc
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=5)


def _start(port, env):
    proc = subprocess.Popen([sys.executable, os.path.join(RECEIVER, 'server.py')], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError('receiver did not start')


def _post(port, package, token='test-token'):
    body = json.dumps(package, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/api/receipts', data=body, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                 'Idempotency-Key': package['package_id']})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_concurrent_retries_account_once(server):
    port, _, _ = server
    package = _package(FACTS)
    results = []

    def worker():
        results.append(_post(port, package))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert all(status in (200, 201) for status, _ in results)
    assert sum(1 for _, body in results if body['outcome'] != 'already_accounted') <= 1
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/accounts',
                                     headers={'Authorization': 'Bearer test-token'})
    with urllib.request.urlopen(request, timeout=5) as response:
        accounts = json.loads(response.read())
    assert len(accounts) == 1


def test_http_lost_ack_then_restart_is_idempotent(server):
    port, env, proc = server
    package = _package(FACTS)
    assert _post(port, package)[0] == 201
    # restart the receiver process against the same database
    proc.terminate()
    proc.wait(timeout=5)
    proc = _start(port, env)
    try:
        status, body = _post(port, package)
        assert status == 200 and body['outcome'] == 'already_accounted'
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/accounts',
                                         headers={'Authorization': 'Bearer test-token'})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert len(json.loads(response.read())) == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_http_unauthorized_rejected(db, server):
    port, _, _ = server
    status, body = _post(port, _package(FACTS), token='wrong')
    assert status == 401 and body['accounted'] is False
