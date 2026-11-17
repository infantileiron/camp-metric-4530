"""Delivery outbox: created with approval, retried by the worker.

Rules:
* A successful approval inserts exactly one delivery row in the same
  transaction as ``approval='approved'``.  If the insert or commit fails the
  approval fails too, so a signed snapshot always has a pending record.
* Receiver availability never affects signing: nothing here talks to the
  receiver.  The worker polls pending rows.
* A newer signed snapshot for the same metric code and date window replaces the
  older revision.  An older revision can never overwrite a newer one.
"""
from datetime import datetime, timezone


from src.models.entities import Delivery
from src.services import packages
from src.services.business import audit


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def receiver_timeout(default=5.0):
    try:
        return float(__import__('os').getenv('RECEIVER_TIMEOUT', default))
    except (TypeError, ValueError):
        return default


def receiver_url():
    """Return the configured receiver base URL, or None when unconfigured."""
    import os
    value = (os.getenv('RECEIVER_URL') or '').strip().rstrip('/')
    return value or None


def revision_for(s, metric_code, date_from, date_to):
    """Next revision for a metric code + date window, independent of order."""
    latest = (
        s.query(Delivery.revision)
        .filter_by(metric_code=metric_code, date_from=date_from, date_to=date_to)
        .order_by(Delivery.revision.desc())
        .first()
    )
    return (latest[0] if latest else 0) + 1


def enqueue(s, snapshot, actor, approved_at):
    """Insert the pending delivery for a just-signed snapshot (same transaction)."""
    code = snapshot.result['metric']['code']
    revision = revision_for(s, code, snapshot.date_from, snapshot.date_to)
    package = packages.build_package(snapshot, revision, approved_at, actor)
    delivery = Delivery(
        package_id=package['package_id'],
        snapshot_id=snapshot.id,
        metric_code=code,
        metric_version=package['metric'].get('version'),
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
        revision=revision,
        approved_at=approved_at,
        approved_by=actor,
        checksum=package['checksum'],
        payload=package,
        status='pending',
        attempts=0,
        last_error='',
        created_at=approved_at,
        updated_at=approved_at,
    )
    s.add(delivery)
    # Flush so a competing approval loses the unique (code, window, revision)
    # race inside the same transaction; the caller rolls back and retries.
    s.flush()
    return delivery


def _supersede(s, delivery):
    """Mark strictly older revisions for the same window as superseded locally."""
    s.query(Delivery).filter(
        Delivery.status.in_(['pending', 'failed']),
        Delivery.metric_code == delivery.metric_code,
        Delivery.date_from == delivery.date_from,
        Delivery.date_to == delivery.date_to,
        Delivery.revision < delivery.revision,
    ).update({Delivery.status: 'superseded', Delivery.updated_at: now_iso()}, synchronize_session=False)
    s.expire_all()


def send(s, delivery, http_client=None):
    """One send attempt.  Returns the acknowledgement dict on success."""
    import json
    import os
    from urllib import error, request

    url = (receiver_url() or '') + '/api/receipts'
    body = json.dumps(delivery.payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Idempotency-Key': delivery.package_id}
    token = os.getenv('RECEIVER_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = request.Request(url, data=body, headers=headers, method='POST')
    try:
        if http_client is not None:
            with http_client.send(req, timeout=receiver_timeout()) as response:
                raw = response.read()
        else:
            with request.urlopen(req, timeout=receiver_timeout()) as response:
                raw = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:4000]
        try:
            verdict = json.loads(detail)
        except ValueError:
            verdict = None
        if isinstance(verdict, dict) and verdict.get('status'):
            # The receiver processed the package and gave a definitive verdict
            # (stale/conflict/invalid).  This is not a transport failure.
            return verdict
        raise DeliveryError(f'接收端拒绝 HTTP {exc.code}: {detail[:800]}', exc.code)
    except (error.URLError, TimeoutError, OSError) as exc:
        raise DeliveryError(f'接收端不可达: {exc}', None)
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise DeliveryError('接收端回执无法解析', None)


class DeliveryError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def apply_ack(s, delivery, ack):
    """Persist a receiver acknowledgement; only accounted acks confirm."""
    if not isinstance(ack, dict):
        raise DeliveryError('接收端回执格式不正确')
    if ack.get('status') in ('accepted', 'replaced', 'duplicate') and ack.get('accounted') is not False:
        superseded_by = ack.get('superseded_by_revision')
        if ack.get('outcome') == 'already_accounted_superseded' and isinstance(superseded_by, int) and superseded_by > delivery.revision:
            # The receiver had already accounted this package earlier, but a
            # newer revision now owns the window; keep the historical record
            # without pretending it is the current account.
            delivery.status = 'superseded'
            delivery.outcome = 'already_accounted_superseded'
            delivery.ack = ack
            delivery.last_error = str(ack.get('detail') or '')[:1000]
            delivery.updated_at = now_iso()
            return
        delivery.status = 'confirmed'
        delivery.outcome = str(ack.get('outcome') or 'accounted')[:120]
        delivery.ack = ack
        delivery.last_error = ''
        delivery.confirmed_at = now_iso()
        delivery.updated_at = delivery.confirmed_at
        _supersede(s, delivery)
        return
    delivery.status = 'superseded' if ack.get('status') == 'stale' else 'rejected'
    delivery.outcome = str(ack.get('outcome') or ack.get('status') or 'rejected')[:120]
    delivery.last_error = str(ack.get('detail') or ack.get('outcome') or '接收端拒收')[:1000]
    delivery.ack = ack
    delivery.updated_at = now_iso()


def recover(s, stale_seconds=120):
    """Reset in-flight rows left behind by a crash so they are retried.

    Confirmed rows are untouched, so a restart can never cause a second
    accounting: the receiver deduplicates by package_id anyway.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    reset = (
        s.query(Delivery)
        .filter(Delivery.status == 'sending', Delivery.updated_at < cutoff)
        .update({Delivery.status: 'pending'}, synchronize_session=False)
    )
    s.commit()
    s.expire_all()
    return reset


def dispatch_once(s, http_client=None, limit=20, on_progress=None):
    """Send all due pending/failed deliveries, newest approval first.

    Sending newest first means a superseded older revision is marked locally
    and never hits the wire, so the receiver only ever sees monotonic revisions.
    Safe to call concurrently.
    """
    recover(s)
    due = (
        s.query(Delivery)
        .filter(Delivery.status.in_(['pending', 'failed']))
        .order_by(Delivery.id.desc())
        .limit(limit)
        .all()
    )
    results = []
    for delivery in due:
        # Claim the row first so overlapping worker/UI dispatchers cannot send
        # the same package twice.  The receiver is idempotent anyway, but this
        # keeps the local attempt ledger honest.
        claimed = (
            s.query(Delivery)
            .filter(Delivery.id == delivery.id, Delivery.status.in_(['pending', 'failed']))
            .update({Delivery.status: 'sending', Delivery.updated_at: now_iso()}, synchronize_session=False)
        )
        s.commit()
        if not claimed:
            continue
        s.refresh(delivery)
        delivery.attempts = (delivery.attempts or 0) + 1
        try:
            ack = send(s, delivery, http_client)
            apply_ack(s, delivery, ack)
        except DeliveryError as exc:
            delivery.last_error = str(exc)[:1000]
            # A receiver-side conflict/stale verdict is final; a transport
            # error stays retryable.
            if exc.status_code in (401, 409, 422):
                delivery.status = 'rejected'
            else:
                delivery.status = 'failed'
            delivery.updated_at = now_iso()
        s.commit()
        results.append((delivery.id, delivery.status))
        if on_progress is not None:
            try:
                on_progress()
            except Exception:
                pass
    return results


def dispatch(s, http_client=None, limit=20, on_progress=None):
    """One dispatch pass over all due deliveries."""
    return dispatch_once(s, http_client=http_client, limit=limit, on_progress=on_progress)


def retry(s, delivery, actor):
    """Manual resend requested from the UI.  Confirmed rows are never resent."""
    if delivery.status == 'confirmed':
        return delivery
    delivery.status = 'pending'
    delivery.last_error = ''
    delivery.updated_at = now_iso()
    audit(s, actor, '补发报送 ' + delivery.package_id[:12])
    return delivery


def _receiver_get(path):
    """GET JSON from the receiver.  Used only for read-only reconciliation."""
    import json
    import os
    from urllib import error, request

    base = receiver_url()
    if not base:
        raise DeliveryError('未配置接收端地址 RECEIVER_URL')
    headers = {'Accept': 'application/json'}
    token = os.getenv('RECEIVER_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = request.Request(base + path, headers=headers, method='GET')
    try:
        with request.urlopen(req, timeout=receiver_timeout()) as response:
            return json.loads(response.read().decode('utf-8'))
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise DeliveryError(f'接收端查询失败 HTTP {exc.code}', exc.code)
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DeliveryError(f'接收端不可达: {exc}', None)


def _same(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    try:
        return abs(float(left) - float(right)) <= 1e-9 * max(1.0, abs(float(left)), abs(float(right)))
    except (TypeError, ValueError):
        return left == right


def reconcile(delivery):
    """Compare the sent package with what the receiver currently has on file.

    Returns both sides, explicit differences and the acknowledgement basis.
    A confirmed delivery whose account differs is flagged, never auto-corrected.
    """
    from urllib.parse import quote
    package = delivery.payload or {}
    summary = package.get('summary') or {}
    groups = package.get('groups') or []
    body = {
        'delivery': {
            'id': delivery.id, 'package_id': delivery.package_id,
            'snapshot_id': delivery.snapshot_id, 'status': delivery.status,
            'outcome': delivery.outcome, 'revision': delivery.revision,
            'checksum': delivery.checksum, 'attempts': delivery.attempts,
            'last_error': delivery.last_error, 'confirmed_at': delivery.confirmed_at,
        },
        'ack': delivery.ack,
        'sender': {'summary': summary, 'groups': groups},
        'receiver': None,
        'differences': [],
        'in_sync': False,
        'detail': '',
    }
    try:
        account = _receiver_get(
            f'/api/accounts/{quote(str(delivery.metric_code))}'
            f'?date_from={quote(str(delivery.date_from))}&date_to={quote(str(delivery.date_to))}'
        )
    except DeliveryError as exc:
        body['detail'] = str(exc)
        return body
    if not account:
        body['detail'] = '接收端没有该指标日期窗的入账记录'
        return body
    body['receiver'] = account

    differences = []
    for field in ('numerator', 'denominator', 'value', 'rows'):
        if not _same(summary.get(field), account.get(field)):
            differences.append({'field': field, 'sender': summary.get(field), 'receiver': account.get(field)})
    if account.get('package_id') != delivery.package_id:
        differences.append({'field': 'package_id', 'sender': delivery.package_id, 'receiver': account.get('package_id')})
    if account.get('revision') != delivery.revision:
        differences.append({'field': 'revision', 'sender': delivery.revision, 'receiver': account.get('revision')})
    if account.get('computed_checksum') != delivery.checksum:
        differences.append({'field': 'checksum', 'sender': delivery.checksum, 'receiver': account.get('computed_checksum')})
    sender_groups = {g.get('class_code'): g for g in groups}
    receiver_groups = {g.get('class_code'): g for g in account.get('groups') or []}
    if set(sender_groups) != set(receiver_groups):
        differences.append({'field': 'groups.class_codes', 'sender': sorted(sender_groups), 'receiver': sorted(receiver_groups)})
    else:
        for code in sorted(sender_groups):
            for field in ('numerator', 'denominator', 'value'):
                if not _same(sender_groups[code].get(field), receiver_groups[code].get(field)):
                    differences.append({'field': f'groups[{code}].{field}',
                                        'sender': sender_groups[code].get(field),
                                        'receiver': receiver_groups[code].get(field)})
    body['differences'] = differences
    body['in_sync'] = not differences
    if not differences:
        body['detail'] = '接收端入账内容与报送包一致'
    elif account.get('revision') is not None and account.get('revision') > delivery.revision:
        body['detail'] = f'接收端已有更新的 v{account["revision"]}，本包是历史版本'
    else:
        body['detail'] = '两端数据存在差异，请核对后补发或人工处理'
    return body
