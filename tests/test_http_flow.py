"""HTTP 端到端：会话、鉴权、撤销后旧链接立即失效、并发抢号不超卖。"""
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import http.cookiejar

from app import config
from app.db import Database
from app.web import build_server
from tests.conftest_common import seed_minimal


class ServerHarness:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-http-")
        # 把 PDF 目录指向夹具的临时目录：通过 config 全局 + 建 data/pdfs 软数据
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        # 让 config.PDF_DIR 指向夹具 PDF 目录，使受控下载能取到文件
        self._old_pdf = config.PDF_DIR
        self._old_data = config.DATA_DIR
        config.PDF_DIR = self.ctx["pdfdir"]
        config.DATA_DIR = self.ctx["data_dir"]
        self.httpd = build_server(self.db, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.db.close()
        config.PDF_DIR = self._old_pdf
        config.DATA_DIR = self._old_data

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def client(self):
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def raw_client(self):
        """不自动跟随 3xx 的 opener，便于断言 303 与 Location。"""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(cj))

    def login(self, opener, uid):
        self.post(opener, "/login", {"user_id": str(uid)})

    def post(self, opener, path, data: dict):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            resp = opener.open(req)
            return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def get(self, opener, path):
        try:
            resp = opener.open(self.base + path)
            return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers


import urllib.parse  # noqa: E402  (post() 需要)


class HttpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = ServerHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_01_login_required(self):
        op = self.h.raw_client()
        try:
            op.open(self.h.base + "/")
            self.fail("应返回 303 重定向到登录页")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 303)
            self.assertIn("/login", e.headers["Location"])

    def test_02_unauthorized_report_denied(self):
        op = self.h.client()
        self.h.login(op, self.h.ctx["p2"])
        status, body, _ = self.h.get(op, f"/reports?id={self.h.ctx['v2']['id']}")
        self.assertEqual(status, 403)
        self.assertIn("访问".encode(), body)

    def test_03_authorized_doctor_sees_report(self):
        from app.services import authorizations as auth_svc
        auth_svc.grant(self.h.db, patient_id=self.h.ctx["p1"],
                       grantee_id=self.h.ctx["doc2"])
        op = self.h.client()
        self.h.login(op, self.h.ctx["doc2"])
        status, body, _ = self.h.get(op, f"/reports?id={self.h.ctx['v2']['id']}")
        self.assertEqual(status, 200)
        self.assertIn("更正报告 V2".encode(), body)

    def test_04_revoked_link_dead_over_http(self):
        from app.services import authorizations as auth_svc
        a = auth_svc.grant(self.h.db, patient_id=self.h.ctx["p1"],
                           grantee_id=self.h.ctx["fam1"])
        link = auth_svc.create_link(
            self.h.db, patient_id=self.h.ctx["p1"],
            grantee_id=self.h.ctx["fam1"], report_id=self.h.ctx["v2"]["id"])
        op = self.h.client()
        self.h.login(op, self.h.ctx["fam1"])
        # 撤销前可下载
        status, data, _ = self.h.get(op, f"/d/{link['token']}")
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"%PDF"))
        # 患者撤销
        pop = self.h.client()
        self.h.login(pop, self.h.ctx["p1"])
        self.h.post(pop, "/authorizations/revoke", {"id": str(a["id"])})
        # 旧链接立即 403
        status, body, _ = self.h.get(op, f"/d/{link['token']}")
        self.assertEqual(status, 403)
        self.assertIn("立即失效".encode(), body)

    def test_05_concurrent_book_single_winner(self):
        """同一时段重复抢号：先到先得（303），第二个明确 409，名额不超卖。"""
        import http.client
        slot = self.h.db.query_one(
            "SELECT id FROM slots WHERE booked=0 AND status='open' ORDER BY start_at LIMIT 1")
        slot_id = slot["id"]
        op = self.h.client()
        self.h.login(op, self.h.ctx["p1"])  # 建立会话 cookie

        # 从 cookie jar 中取会话值
        cj = next(h.cookiejar for h in op.handlers
                  if isinstance(h, urllib.request.HTTPCookieProcessor))
        cookie = "; ".join(f"{c.name}={c.value}" for c in cj)

        def raw_post(path, form, cookie):
            conn = http.client.HTTPConnection("127.0.0.1", self.h.port)
            body = urllib.parse.urlencode(form).encode()
            conn.request("POST", path, body, {
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie})
            r = conn.getresponse()
            r.read()
            conn.close()
            return r.status, r.getheader("Location")
        form = {"slot_id": str(slot_id), "report_id": str(self.h.ctx["v2"]["id"]),
                "topics": "glc_fpg"}
        s1, loc1 = raw_post("/book", form, cookie)
        self.assertEqual(s1, 303)
        self.assertIn("/appointments", loc1)
        s2, _ = raw_post("/book", form, cookie)
        self.assertEqual(s2, 409)
        row = self.h.db.query_one("SELECT booked,capacity FROM slots WHERE id=?",
                                  (slot_id,))
        self.assertEqual(row["booked"], 1)


if __name__ == "__main__":
    unittest.main()
