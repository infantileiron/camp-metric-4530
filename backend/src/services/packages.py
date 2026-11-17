"""Signed snapshot reporting package.

Pure standard library so the payload format can be shared and audited without
importing the application models.  The checksum is calculated over the payload
with the ``checksum`` member removed, using canonical JSON.
"""
import hashlib
import json
import uuid

PACKAGE_SCHEMA = 'camp-metric-package-v1'
METRIC_FIELDS = (
    'id', 'code', 'version', 'name', 'numerator', 'denominator',
    'numerator_mode', 'denominator_mode',
)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def compute_checksum(package):
    content = {key: value for key, value in package.items() if key != 'checksum'}
    return hashlib.sha256(canonical_json(content).encode('utf-8')).hexdigest()


def build_package(snapshot, revision, approved_at, approved_by):
    """Build the immutable report package from a successful signed snapshot."""
    frozen = snapshot.result['snapshot']
    result = snapshot.result
    metric = {field: result['metric'].get(field) for field in METRIC_FIELDS}
    facts = [dict(row) for row in frozen['facts']]
    package = {
        'schema': PACKAGE_SCHEMA,
        'package_id': uuid.uuid4().hex,
        'snapshot_id': snapshot.id,
        'metric': metric,
        'date_from': snapshot.date_from,
        'date_to': snapshot.date_to,
        'revision': revision,
        'approved_at': approved_at,
        'approved_by': approved_by,
        'summary': {
            'value': result.get('value'),
            'numerator': result.get('numerator'),
            'denominator': result.get('denominator'),
            'rows': result.get('rows'),
            'numerator_rows': result.get('numerator_rows'),
            'denominator_rows': result.get('denominator_rows'),
            'facts_checksum': result.get('facts_checksum'),
            'snapshot_checksum': result.get('checksum'),
            'undefined_reason': result.get('undefined_reason', ''),
        },
        'groups': [dict(group) for group in result.get('groups') or []],
        'facts': facts,
        'method': result.get('method', ''),
        'analysis_version': result.get('version', ''),
    }
    package['checksum'] = compute_checksum(package)
    return package
