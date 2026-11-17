"""SQLite persistence for the receiver.

Idempotency and accounting are committed in one IMMEDIATE transaction.  A
unique receipt key makes a repeated delivery (lost acknowledgement, restart or
concurrent retry) a no-op, while the accounting row is only advanced by a
strictly newer revision for the same metric code and date window.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import verify

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    package_id TEXT PRIMARY KEY,
    snapshot_id INTEGER,
    metric_code TEXT,
    date_from TEXT,
    date_to TEXT,
    revision INTEGER,
    provided_checksum TEXT,
    computed_checksum TEXT,
    status TEXT NOT NULL,
    outcome TEXT NOT NULL,
    accounted INTEGER NOT NULL,
    http_status INTEGER NOT NULL,
    detail TEXT,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    verification TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    metric_code TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    revision INTEGER NOT NULL,
    package_id TEXT NOT NULL,
    snapshot_id INTEGER,
    computed_checksum TEXT,
    approved_at TEXT,
    approved_by TEXT,
    value REAL,
    numerator REAL,
    denominator REAL,
    rows INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (metric_code, date_from, date_to)
);
CREATE TABLE IF NOT EXISTS account_groups (
    metric_code TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    class_code TEXT NOT NULL,
    numerator REAL,
    denominator REAL,
    value REAL,
    PRIMARY KEY (metric_code, date_from, date_to, class_code)
);
CREATE TABLE IF NOT EXISTS receipt_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    incoming_checksum TEXT,
    stored_checksum TEXT,
    received_at TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS receipts_received ON receipts(received_at DESC);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_path():
    return os.environ.get('RECEIVER_DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'receiver.db'))


def connect():
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA busy_timeout=10000')
    connection.executescript(SCHEMA)
    return connection


def _account_key(package):
    return (package.get('metric', {}).get('code'), package.get('date_from'), package.get('date_to'))


def _accounting_label(accounted):
    return '已入账' if accounted else '未入账'


def _receipt_summary(row, accounted=None):
    accounted = row['accounted'] if accounted is None else accounted
    verification = json.loads(row['verification'] or '{}')
    return {
        'status': row['status'],
        'outcome': row['outcome'],
        'accounted': bool(accounted),
        'accounting': _accounting_label(accounted),
        'package_id': row['package_id'],
        'snapshot_id': row['snapshot_id'],
        'metric_code': row['metric_code'],
        'date_from': row['date_from'],
        'date_to': row['date_to'],
        'revision': row['revision'],
        'checksum': row['computed_checksum'],
        'provided_checksum': row['provided_checksum'],
        'received_at': row['received_at'],
        'detail': row['detail'],
        'summary': verification.get('summary'),
        'differences': verification.get('differences') or [],
        'errors': verification.get('errors') or [],
    }


def _record_receipt(c, package, status, outcome, accounted, http_status, detail, verification):
    key = _account_key(package)
    c.execute(
        'INSERT INTO receipts (package_id, snapshot_id, metric_code, date_from, date_to, revision,'
        ' provided_checksum, computed_checksum, status, outcome, accounted, http_status, detail,'
        ' received_at, payload, verification) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            package.get('package_id'), package.get('snapshot_id'), key[0], key[1], key[2],
            package.get('revision'), package.get('checksum'), verify.compute_checksum(package),
            status, outcome, 1 if accounted else 0, http_status, detail,
            now_iso(), json.dumps(package, ensure_ascii=False, sort_keys=True),
            json.dumps(verification, ensure_ascii=False, sort_keys=True),
        ),
    )


def ingest(package):
    """Validate, de-duplicate and account one package.  Returns (http_status, body)."""
    if not isinstance(package, dict):
        return 400, {'status': 'invalid', 'outcome': 'malformed', 'accounted': False,
                     'accounting': '未入账', 'detail': '请求体必须是JSON对象'}
    package_id = package.get('package_id')
    if not isinstance(package_id, str) or not package_id:
        return 400, {'status': 'invalid', 'outcome': 'missing_package_id', 'accounted': False,
                     'accounting': '未入账', 'detail': '缺少package_id，无法幂等处理'}

    computed = verify.compute_checksum(package)
    connection = connect()
    try:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute(
            'SELECT * FROM receipts WHERE package_id=?', (package_id,)
        ).fetchone()
        if existing:
            connection.execute('COMMIT')
            if existing['computed_checksum'] != computed:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute(
                    'INSERT INTO receipt_conflicts (package_id, incoming_checksum, stored_checksum, received_at, detail)'
                    ' VALUES (?,?,?,?,?)',
                    (package_id, computed, existing['computed_checksum'], now_iso(),
                     '同一package_id收到不同内容，拒收且不改变已入账数据'),
                )
                connection.execute('COMMIT')
                body = _receipt_summary(existing, accounted=False)
                body.update({
                    'status': 'conflict',
                    'outcome': 'same_package_different_content',
                    'accounted': False,
                    'accounting': '未入账',
                    'incoming_checksum': computed,
                    'detail': '同一报送包ID收到不同内容，整体拒收；原入账内容未被覆盖',
                })
                return 409, body
            body = _receipt_summary(existing, accounted=bool(existing['accounted']))
            if existing['accounted']:
                current = connection.execute(
                    'SELECT revision FROM accounts WHERE metric_code=? AND date_from=? AND date_to=?',
                    (existing['metric_code'], existing['date_from'], existing['date_to']),
                ).fetchone()
                if current and current['revision'] > existing['revision']:
                    body.update({
                        'status': 'duplicate',
                        'outcome': 'already_accounted_superseded',
                        'detail': f'该包已入账，但当前已被更新版本 v{current["revision"]} 替换；本次未重复入账',
                        'superseded_by_revision': current['revision'],
                    })
                    return 200, body
                body.update({
                    'status': 'duplicate',
                    'outcome': 'already_accounted',
                    'detail': '相同内容已入账，本次为幂等重试，未重复入账',
                })
                return 200, body
            return existing['http_status'], body

        verification = verify.verify(package)
        if not verification['ok']:
            status = 'invalid'
            outcome = 'integrity_failed'
            detail = '; '.join(verification['errors'])[:1000]
            _record_receipt(connection, package, status, outcome, False, 422, detail, verification)
            connection.execute('COMMIT')
            return 422, {
                'status': status, 'outcome': outcome, 'accounted': False, 'accounting': '未入账',
                'package_id': package_id, 'snapshot_id': package.get('snapshot_id'),
                'metric_code': _account_key(package)[0], 'date_from': package.get('date_from'),
                'date_to': package.get('date_to'), 'revision': package.get('revision'),
                'checksum': verification.get('checksum'), 'received_at': now_iso(),
                'summary': verification.get('summary'), 'groups': verification.get('groups'),
                'differences': verification.get('differences'), 'errors': verification['errors'],
                'detail': detail,
            }

        key = _account_key(package)
        revision = package['revision']
        account = connection.execute(
            'SELECT * FROM accounts WHERE metric_code=? AND date_from=? AND date_to=?', key
        ).fetchone()

        if account and revision < account['revision']:
            detail = f'旧版本 v{revision} 不会覆盖已入账的 v{account["revision"]}'
            _record_receipt(connection, package, 'stale', 'older_revision_ignored', False, 409, detail, verification)
            connection.execute('COMMIT')
            return 409, {
                'status': 'stale', 'outcome': 'older_revision_ignored', 'accounted': False,
                'accounting': '未入账', 'package_id': package_id,
                'snapshot_id': package.get('snapshot_id'), 'metric_code': key[0],
                'date_from': key[1], 'date_to': key[2], 'revision': revision,
                'receiver_revision': account['revision'], 'checksum': verification['checksum'],
                'summary': verification['summary'], 'groups': verification['groups'],
                'differences': verification['differences'], 'received_at': now_iso(),
                'detail': detail,
            }
        if account and revision == account['revision'] and account['package_id'] != package_id:
            detail = f'版本 v{revision} 已被另一个报送包占用；同版本异包拒收，请确认后补发更高版本'
            _record_receipt(connection, package, 'conflict', 'same_revision_different_package', False, 409, detail, verification)
            connection.execute('COMMIT')
            return 409, {
                'status': 'conflict', 'outcome': 'same_revision_different_package', 'accounted': False,
                'accounting': '未入账', 'package_id': package_id,
                'snapshot_id': package.get('snapshot_id'), 'metric_code': key[0],
                'date_from': key[1], 'date_to': key[2], 'revision': revision,
                'receiver_revision': account['revision'], 'checksum': verification['checksum'],
                'summary': verification['summary'], 'groups': verification['groups'],
                'differences': verification['differences'], 'received_at': now_iso(),
                'detail': detail,
            }

        summary = verification['summary']
        groups = verification['groups']
        replaced = bool(account and revision > account['revision'])
        metric = package['metric']
        connection.execute(
            'DELETE FROM account_groups WHERE metric_code=? AND date_from=? AND date_to=?', key
        )
        for group in groups:
            connection.execute(
                'INSERT INTO account_groups (metric_code, date_from, date_to, class_code, numerator, denominator, value)'
                ' VALUES (?,?,?,?,?,?,?)',
                (key[0], key[1], key[2], group['class_code'], group['numerator'], group['denominator'], group['value']),
            )
        connection.execute(
            'INSERT INTO accounts (metric_code, date_from, date_to, revision, package_id, snapshot_id,'
            ' computed_checksum, approved_at, approved_by, value, numerator, denominator, rows, updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
            ' ON CONFLICT(metric_code, date_from, date_to) DO UPDATE SET'
            ' revision=excluded.revision, package_id=excluded.package_id, snapshot_id=excluded.snapshot_id,'
            ' computed_checksum=excluded.computed_checksum, approved_at=excluded.approved_at,'
            ' approved_by=excluded.approved_by, value=excluded.value, numerator=excluded.numerator,'
            ' denominator=excluded.denominator, rows=excluded.rows, updated_at=excluded.updated_at',
            (key[0], key[1], key[2], revision, package_id, package.get('snapshot_id'),
             verification['checksum'], package.get('approved_at'), package.get('approved_by'),
             summary['value'], summary['numerator'], summary['denominator'], summary['rows'], now_iso()),
        )
        outcome = 'replaced_previous_revision' if replaced else 'accounted'
        status = 'replaced' if replaced else 'accepted'
        detail = (f'已用 v{revision} 替换 v{account["revision"]}' if replaced else f'已入账 v{revision}')
        _record_receipt(connection, package, status, outcome, True, 201, detail, verification)
        connection.execute('COMMIT')
        return 201, {
            'status': status, 'outcome': outcome, 'accounted': True, 'accounting': '已入账',
            'package_id': package_id, 'snapshot_id': package.get('snapshot_id'),
            'metric_code': key[0], 'metric_version': metric.get('version'),
            'date_from': key[1], 'date_to': key[2], 'revision': revision,
            'receiver_revision': revision, 'checksum': verification['checksum'],
            'summary': summary, 'groups': groups, 'differences': [],
            'received_at': now_iso(), 'detail': detail,
        }
    except Exception:
        connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()


def _row_to_account(row):
    return {key: row[key] for key in row.keys()}


def list_receipts(limit=100):
    connection = connect()
    try:
        rows = connection.execute(
            'SELECT * FROM receipts ORDER BY received_at DESC LIMIT ?', (int(limit),)
        ).fetchall()
        return [
            {
                'package_id': row['package_id'], 'snapshot_id': row['snapshot_id'],
                'metric_code': row['metric_code'], 'date_from': row['date_from'],
                'date_to': row['date_to'], 'revision': row['revision'],
                'status': row['status'], 'outcome': row['outcome'],
                'accounted': bool(row['accounted']), 'accounting': _accounting_label(row['accounted']),
                'detail': row['detail'], 'checksum': row['computed_checksum'],
                'provided_checksum': row['provided_checksum'], 'received_at': row['received_at'],
            }
            for row in rows
        ]
    finally:
        connection.close()


def get_receipt(package_id):
    connection = connect()
    try:
        row = connection.execute('SELECT * FROM receipts WHERE package_id=?', (package_id,)).fetchone()
        if not row:
            return None
        body = _receipt_summary(row)
        body['payload'] = json.loads(row['payload'])
        body['verification'] = json.loads(row['verification'] or '{}')
        body['conflicts'] = [
            dict(conflict)
            for conflict in connection.execute(
                'SELECT * FROM receipt_conflicts WHERE package_id=? ORDER BY id DESC', (package_id,)
            ).fetchall()
        ]
        body['account'] = account_for(
            row['metric_code'], row['date_from'], row['date_to'], connection=connection
        )
        return body
    finally:
        connection.close()


def list_accounts():
    connection = connect()
    try:
        rows = connection.execute(
            'SELECT * FROM accounts ORDER BY metric_code, date_from, date_to'
        ).fetchall()
        return [_row_to_account(row) for row in rows]
    finally:
        connection.close()


def account_for(metric_code, date_from, date_to, connection=None):
    owns = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            'SELECT * FROM accounts WHERE metric_code=? AND date_from=? AND date_to=?',
            (metric_code, date_from, date_to),
        ).fetchone()
        if not row:
            return None
        account = _row_to_account(row)
        account['groups'] = [
            _row_to_account(group)
            for group in connection.execute(
                'SELECT class_code, numerator, denominator, value FROM account_groups'
                ' WHERE metric_code=? AND date_from=? AND date_to=? ORDER BY class_code',
                (metric_code, date_from, date_to),
            ).fetchall()
        ]
        return account
    finally:
        if owns:
            connection.close()


def stats():
    connection = connect()
    try:
        return {
            'receipts': connection.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],
            'accounts': connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],
            'conflicts': connection.execute('SELECT COUNT(*) FROM receipt_conflicts').fetchone()[0],
            'db': db_path(),
        }
    finally:
        connection.close()
