"""Standalone report receiver (standard library only).

Run:
    RECEIVER_PORT=18211 RECEIVER_TOKEN=... python3 receiver/server.py

Endpoints (all JSON):
    GET  /health
    POST /api/receipts          receive one signed snapshot package
    GET  /api/receipts          recent receipt ledger
    GET  /api/receipts/<id>     one receipt incl. original payload and checks
    GET  /api/accounts          current accounted packages (latest revision)
    GET  /api/accounts/<code>?date_from=&date_to=
    GET  /                      small HTML view
"""
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import verify  # noqa: E402

MAX_BODY = 8 * 1024 * 1024


def authorized(header):
    token = os.getenv('RECEIVER_TOKEN')
    if not token:
        return True
    if not header:
        return False
    provided = header.split(' ', 1)[-1].strip()
    return hmac.compare_digest(provided, token)


class Handler(BaseHTTPRequestHandler):
    server_version = 'camp-metric-receiver/1'

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s\n' % (self.address_string(), fmt % args))

    def _send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return None, (400, {'status': 'invalid', 'outcome': 'bad_length', 'accounted': False,
                                'accounting': '未入账', 'detail': 'Content-Length不合法'})
        if length <= 0:
            return None, (400, {'status': 'invalid', 'outcome': 'empty_body', 'accounted': False,
                                'accounting': '未入账', 'detail': '请求体为空'})
        if length > MAX_BODY:
            return None, (413, {'status': 'invalid', 'outcome': 'too_large', 'accounted': False,
                                'accounting': '未入账', 'detail': '请求体超过8MB'})
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8')), None
        except (ValueError, UnicodeDecodeError):
            return None, (400, {'status': 'invalid', 'outcome': 'malformed_json', 'accounted': False,
                                'accounting': '未入账', 'detail': '请求体不是合法JSON'})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        if path == '/health':
            return self._send(200, {'status': 'ok', **store.stats()})
        if path == '/':
            return self._html()
        if not authorized(self.headers.get('Authorization')):
            return self._send(401, {'detail': '接收端凭据无效'})
        if path == '/api/receipts':
            return self._send(200, store.list_receipts())
        if path.startswith('/api/receipts/'):
            receipt = store.get_receipt(path.split('/', 3)[3])
            return self._send(200, receipt) if receipt else self._send(404, {'detail': '回执不存在'})
        if path == '/api/accounts':
            return self._send(200, store.list_accounts())
        if path.startswith('/api/accounts/'):
            code = path.split('/', 3)[3]
            query = parse_qs(parsed.query)
            date_from = (query.get('date_from') or [''])[0]
            date_to = (query.get('date_to') or [''])[0]
            account = store.account_for(code, date_from, date_to)
            return self._send(200, account) if account else self._send(404, {'detail': '未找到该指标日期窗的入账记录'})
        return self._send(404, {'detail': '路径不存在'})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        if path != '/api/receipts':
            return self._send(404, {'detail': '路径不存在'})
        if not authorized(self.headers.get('Authorization')):
            return self._send(401, {'detail': '接收端凭据无效', 'accounted': False, 'accounting': '未入账'})
        package, problem = self._json_body()
        if problem:
            return self._send(*problem)
        idempotency_key = (self.headers.get('Idempotency-Key') or '').strip()
        if idempotency_key and package.get('package_id') and idempotency_key != package.get('package_id'):
            return self._send(400, {'status': 'invalid', 'outcome': 'idempotency_key_mismatch',
                                    'accounted': False, 'accounting': '未入账',
                                    'detail': 'Idempotency-Key与package_id不一致'})
        try:
            status, body = store.ingest(package)
        except Exception as exc:  # never acknowledge an unpersisted receipt
            return self._send(500, {'status': 'error', 'outcome': 'not_persisted', 'accounted': False,
                                    'accounting': '未入账', 'detail': '入账事务失败: ' + str(exc)})
        return self._send(status, body)

    def _html(self):
        rows = ''.join(
            '<tr><td>{}</td><td>{}</td><td>v{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                _e(r['metric_code']), _e(r['date_from']), r['revision'], _e(r['date_to']),
                _e(r['numerator']), _e(r['denominator']),
            )
            for r in store.list_accounts()
        ) or '<tr><td colspan="6">尚无入账记录</td></tr>'
        receipts = ''.join(
            '<tr><td>{}</td><td>{}</td><td>v{}</td><td>{}</td><td>{}</td></tr>'.format(
                _e(r['package_id'][:12]), _e(r['snapshot_id']), r['revision'],
                _e(r['status']), _e(r['detail']),
            )
            for r in store.list_receipts(30)
        ) or '<tr><td colspan="5">尚无回执</td></tr>'
        html = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>启航 · 报送接收端</title>
<style>body{{font:14px sans-serif;margin:30px;max-width:1000px}}table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}code{{background:#f3f3f3;padding:2px 4px}}</style>
<h1>启航 · 报送接收端</h1>
<p>只读视图。接收端独立重算班级明细，校验checksum、总计和内容完整性后才入账；重复回执不重复入账。</p>
<h2>当前入账（每指标+日期窗最新版本）</h2>
<table><tr><th>指标</th><th>起</th><th>版本</th><th>止</th><th>分子</th><th>分母</th></tr>{rows}</table>
<h2>最近回执</h2>
<table><tr><th>包</th><th>快照</th><th>版本</th><th>状态</th><th>说明</th></tr>{receipts}</table>'''
        raw = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _e(value):
    return str(value if value is not None else '—').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def main():
    host = os.getenv('RECEIVER_HOST', '127.0.0.1')
    port = int(os.getenv('RECEIVER_PORT', '18211'))
    store.connect().close()
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({'receiver': f'http://{host}:{port}', 'db': store.db_path()}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
