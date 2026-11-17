"""Independent verification and re-aggregation for received report packages.

The receiver never trusts the sender's totals.  It validates the package
checksum, rebuilds the class groups from the frozen fact rows with its own
whitelist expression evaluator, and only then compares the sender's summary.
"""
import ast
import hashlib
import json
import math

FIELDS = ('enrolled', 'attended', 'completed', 'refunds')
MODES = ('全部', '线上', '线下')
METRIC_FIELDS = (
    'code', 'version', 'name', 'numerator', 'denominator',
    'numerator_mode', 'denominator_mode',
)
OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
EPS = 1e-9


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def compute_checksum(package):
    content = {key: value for key, value in package.items() if key != 'checksum'}
    return hashlib.sha256(canonical_json(content).encode('utf-8')).hexdigest()


def facts_checksum(rows):
    # Must match the analysis worker's serialization exactly.
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def parse_expression(expression):
    if not isinstance(expression, str):
        raise ValueError('表达式必须是文本')
    if len(expression) > 200:
        raise ValueError('表达式最多200字符')
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError:
        raise ValueError('表达式语法不合法')
    if len(list(ast.walk(tree))) > 60:
        raise ValueError('表达式过于复杂')

    def check(node):
        if isinstance(node, ast.Expression):
            check(node.body)
        elif isinstance(node, ast.Name):
            if node.id not in FIELDS:
                raise ValueError('只允许字段 enrolled/attended/completed/refunds')
        elif isinstance(node, ast.Constant):
            if type(node.value) not in (int, float) or not math.isfinite(node.value) or abs(node.value) > 1000000:
                raise ValueError('只允许有限数值常量，绝对值不超过1000000')
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            check(node.left)
            check(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            check(node.operand)
        else:
            raise ValueError('仅允许字段、数值、括号、+ - * /；禁止函数、属性、索引或SQL')

    check(tree)
    return tree


def execute(tree, row):
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return row[node.id]
        if isinstance(node, ast.UnaryOp):
            return -walk(node.operand) if isinstance(node.op, ast.USub) else walk(node.operand)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ValueError('行级表达式除零')
            return OPERATORS[type(node.op)](left, right)
        raise ValueError('不支持的表达式')

    value = float(walk(tree))
    if not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError('表达式结果超出允许范围')
    return value


def ratio(numerator, denominator):
    if denominator == 0:
        return None
    value = numerator / denominator
    if not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError('指标比值超出允许范围')
    return value


def _close(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    try:
        return abs(float(left) - float(right)) <= EPS * max(1.0, abs(float(left)), abs(float(right)))
    except (TypeError, ValueError):
        return False


def _validate_fact(fact, index, date_from, date_to):
    if not isinstance(fact, dict):
        return f'第{index}行事实不是对象'
    for key in ('class_code', 'day', 'mode') + FIELDS:
        if key not in fact:
            return f'第{index}行事实缺少字段 {key}'
    if not isinstance(fact['class_code'], str) or not fact['class_code'].strip():
        return f'第{index}行班级代码不合法'
    if not isinstance(fact['day'], str) or len(fact['day']) != 10:
        return f'第{index}行日期不合法'
    if not (date_from <= fact['day'] <= date_to):
        return f'第{index}行日期 {fact["day"]} 超出报送日期窗'
    if fact['mode'] not in MODES[1:]:
        return f'第{index}行授课方式不合法'
    for key in FIELDS:
        value = fact[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return f'第{index}行 {key} 不是有限数值'
        if value < 0 or value > 1000000:
            return f'第{index}行 {key} 超出允许范围'
    return None


def validate_package(package):
    """Structural validation.  Returns (errors, context)."""
    errors = []
    if not isinstance(package, dict):
        return ['报送包必须是JSON对象'], None
    if package.get('schema') != 'camp-metric-package-v1':
        errors.append('报送包版本不受支持')
    package_id = package.get('package_id')
    if not isinstance(package_id, str) or len(package_id) != 32 or not all(c in '0123456789abcdef' for c in package_id):
        errors.append('package_id 不合法')
    if not isinstance(package.get('snapshot_id'), int) or package.get('snapshot_id', 0) <= 0:
        errors.append('snapshot_id 不合法')
    revision = package.get('revision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append('revision 必须是正整数')
    metric = package.get('metric')
    if not isinstance(metric, dict):
        errors.append('缺少 metric')
    else:
        for field in METRIC_FIELDS:
            if field not in metric:
                errors.append(f'metric 缺少字段 {field}')
        if isinstance(metric.get('code'), str) is False or not metric.get('code'):
            errors.append('metric.code 不合法')
        if metric.get('numerator_mode') not in MODES or metric.get('denominator_mode') not in MODES:
            errors.append('授课方式过滤不合法')
    date_from, date_to = package.get('date_from'), package.get('date_to')
    if not isinstance(date_from, str) or not isinstance(date_to, str) or len(date_from) != 10 or len(date_to) != 10:
        errors.append('日期窗不合法')
    elif date_from > date_to:
        errors.append('日期窗起点晚于终点')
    facts = package.get('facts')
    if not isinstance(facts, list) or not facts:
        errors.append('缺少冻结事实明细')
    elif isinstance(date_from, str) and isinstance(date_to, str):
        seen = set()
        for index, fact in enumerate(facts):
            problem = _validate_fact(fact, index, date_from, date_to)
            if problem:
                errors.append(problem)
                break
            key = (fact['class_code'], fact['day'])
            if key in seen:
                errors.append(f'班级 {key[0]} 日期 {key[1]} 重复')
                break
            seen.add(key)
    if not isinstance(package.get('summary'), dict):
        errors.append('缺少汇总')
    if not isinstance(package.get('groups'), list):
        errors.append('缺少班级明细')
    expected = compute_checksum(package)
    if expected != package.get('checksum'):
        errors.append('报送包checksum不一致，内容可能被改写')
    return errors, {'expected_checksum': expected}


def reaggregate(package):
    """Recompute summary and per-class groups from the frozen facts."""
    metric = package['metric']
    rows = package['facts']
    numerator_tree = parse_expression(metric['numerator'])
    denominator_tree = parse_expression(metric['denominator'])
    numerator_mode = metric['numerator_mode']
    denominator_mode = metric['denominator_mode']
    numerator_rows = [r for r in rows if numerator_mode == '全部' or r['mode'] == numerator_mode]
    denominator_rows = [r for r in rows if denominator_mode == '全部' or r['mode'] == denominator_mode]
    numerator = sum(execute(numerator_tree, r) for r in numerator_rows)
    denominator = sum(execute(denominator_tree, r) for r in denominator_rows)
    value = ratio(numerator, denominator)
    groups = []
    for code in sorted({r['class_code'] for r in rows}):
        n = sum(execute(numerator_tree, r) for r in numerator_rows if r['class_code'] == code)
        d = sum(execute(denominator_tree, r) for r in denominator_rows if r['class_code'] == code)
        groups.append({'class_code': code, 'numerator': n, 'denominator': d, 'value': ratio(n, d)})
    summary = {
        'value': value,
        'numerator': numerator,
        'denominator': denominator,
        'rows': len(rows),
        'numerator_rows': len(numerator_rows),
        'denominator_rows': len(denominator_rows),
        'facts_checksum': facts_checksum(rows),
        'undefined_reason': '外层聚合分母为零' if value is None else '',
    }
    return summary, groups


def verify(package):
    """Return dict with errors, sender-vs-receiver differences and recomputed data.

    ``ok`` is True only when the package is intact and every reported total,
    group and checksum matches the receiver's independent calculation.
    """
    errors, context = validate_package(package)
    if errors:
        return {'ok': False, 'errors': errors, 'differences': [], 'summary': None, 'groups': None}
    try:
        summary, groups = reaggregate(package)
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        return {'ok': False, 'errors': ['接收端重算失败: ' + str(exc)], 'differences': [], 'summary': None, 'groups': None}

    sender = package.get('summary') or {}
    differences = []
    for field in ('numerator', 'denominator', 'value', 'rows', 'numerator_rows', 'denominator_rows'):
        if not _close(sender.get(field), summary.get(field)):
            differences.append({'field': field, 'sender': sender.get(field), 'receiver': summary.get(field)})
    if sender.get('facts_checksum') != summary['facts_checksum']:
        differences.append({'field': 'facts_checksum', 'sender': sender.get('facts_checksum'), 'receiver': summary['facts_checksum']})

    sender_groups = package.get('groups') or []
    sender_by_code = {g.get('class_code'): g for g in sender_groups if isinstance(g, dict)}
    receiver_by_code = {g['class_code']: g for g in groups}
    if set(sender_by_code) != set(receiver_by_code):
        differences.append({'field': 'groups.class_codes', 'sender': sorted(sender_by_code), 'receiver': sorted(receiver_by_code)})
    else:
        for code in sorted(receiver_by_code):
            for field in ('numerator', 'denominator', 'value'):
                if not _close(sender_by_code[code].get(field), receiver_by_code[code].get(field)):
                    differences.append({'field': f'groups[{code}].{field}', 'sender': sender_by_code[code].get(field), 'receiver': receiver_by_code[code].get(field)})

    if differences:
        errors.append('汇总或班级明细与接收端重算结果不一致')
    return {
        'ok': not errors,
        'errors': errors,
        'differences': differences,
        'summary': summary,
        'groups': groups,
        'checksum': context['expected_checksum'],
    }
