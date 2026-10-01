"""HTTP 服务：标准库 http.server，会话用签名 Cookie。

- 单进程演示服务；每个请求独立连接（WAL 支持并发读写）；
- 静态文件 /static/*；PDF 下载走令牌校验；
- 路由匹配后调用 web.views 中的处理函数。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import config, db
from ..appctx import AppContext
from ..mailer import MockMailer
from . import views


def _session_key() -> bytes:
    path = config.DATA_DIR / ".sessionkey"
    if path.exists():
        return path.read_bytes()
    key = hashlib.sha256(b"healthcheck-demo" + str(path).encode()).digest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    return key


def make_token(uid: int) -> str:
    payload = base64.urlsafe_b64encode(str(uid).encode()).decode()
    sig = hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def read_token(token: str) -> int | None:
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expect = hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        return int(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "HealthCheckDemo/1.0"

    def log_message(self, fmt, *args):  # 安静日志
        if getattr(self.server, "quiet", True):
            return
        super().log_message(fmt, *args)

    # ---- 响应辅助 ----
    def _send(self, status, body: bytes, headers=None, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html_str: str, status=200, extra_headers=None):
        self._send(status, html_str.encode("utf-8"), extra_headers)

    def _redirect(self, location, cookie=None):
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(HTTPStatus.SEE_OTHER, b"", headers)

    # ---- 入口 ----
    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        form_multi = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        form = {k: v[0] for k, v in form_multi.items()}

        # 静态资源
        if method == "GET" and path.startswith("/static/"):
            return self._static(path)

        # 会话
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        me = None
        uid = None
        if "hc_session" in cookie:
            uid = read_token(cookie["hc_session"].value)

        # 全程使用同一连接（me Row 也绑定在该连接上），结束后统一关闭
        conn = self.server.connect()
        resp = None
        try:
            if uid:
                me = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            mailer = MockMailer(lambda: conn)
            ctx = AppContext(conn, mailer)
            # 每次请求清扫到期授权（同时惰性判定兜底）
            ctx.sweep_expired()
            req = views.Request(ctx, me, q, form, form_multi,
                                ip=self.client_address[0],
                                set_cookie=lambda u: f"hc_session={make_token(u)}; Path=/; HttpOnly")
            resp = views.dispatch(method, path, req)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self._html(views.error_page(me, 500, "服务器内部错误，请查看控制台"), 500)
            except Exception:
                pass
            return
        finally:
            conn.close()

        if resp is None:
            return self._html(views.not_found(me), 404)
        kind = resp[0]
        if kind == "html":
            self._html(resp[1], resp[2] if len(resp) > 2 else 200)
        elif kind == "redirect":
            cookie_header = None
            if len(resp) > 2 and resp[2]:
                cookie_header = f"hc_session={make_token(resp[2])}; Path=/; HttpOnly"
            self._redirect(resp[1], cookie_header)
        elif kind == "logout":
            self._redirect(resp[1],
                           "hc_session=; Path=/; HttpOnly; Max-Age=0")
        elif kind == "pdf":
            _, data, filename, download = resp
            disp = "attachment" if download else "inline"
            self._send(200, data, {
                "Content-Disposition": f'{disp}; filename*=UTF-8\'\'{filename}',
            }, content_type="application/pdf")
        elif kind == "error":
            _, status, msg, me_row = resp
            self._html(views.error_page(me_row, status, msg), status)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _static(self, path):
        rel = path[len("/static/"):]
        full = (config.STATIC_DIR / rel).resolve()
        if not str(full).startswith(str(config.STATIC_DIR.resolve())) or not full.is_file():
            return self._send(404, b"not found", content_type="text/plain")
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        self._send(200, full.read_bytes(),
                   {"Cache-Control": "public, max-age=60"}, content_type=ctype)


def build_server(host="127.0.0.1", port=8000, db_path=None, quiet=True):
    if db_path:
        config.DB_PATH = db_path
    httpd = ThreadingHTTPServer((host, port), Handler)
    # 每请求由 handler 新建并关闭连接（WAL 支持并发，写事务自动串行化）
    httpd.connect = lambda: db.connect(db_path)  # noqa
    httpd.quiet = quiet
    return httpd
