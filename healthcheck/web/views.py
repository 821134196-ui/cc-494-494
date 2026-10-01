"""路由分发与请求对象。"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import DomainError
from . import pages_patient, pages_doctor, pages_family, pages_staff
from .layout import page, esc


@dataclass
class Request:
    ctx: object
    me: object
    q: dict
    form: dict
    form_multi: dict
    ip: str
    set_cookie: object
    url: str = "/"

    @property
    def conn(self):
        return self.ctx.conn

    def getlist(self, name):
        return self.form_multi.get(name, [])

    def flash(self):
        if self.q.get("flash"):
            return (self.q.get("flash_kind", "ok"), self.q["flash"])
        return None


def _require(req, *roles):
    if req.me is None:
        return ("redirect", "/login")
    if roles and req.me["role"] not in roles:
        return ("error", 403, "当前身份无权访问该页面", req.me)
    return None


def dispatch(method, path, req):
    req.url = path
    # 公共
    if path == "/" and method == "GET":
        return ("redirect", "/home" if req.me else "/login")
    if path == "/login" and method == "GET":
        return ("html", pages_patient.login(req))
    if path == "/login" and method == "POST":
        uid = int(req.form.get("uid", 0))
        user = req.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if user is None:
            return ("html", pages_patient.login(req, "请选择演示身份"), 400)
        return ("redirect", _landing(user), uid)
    if path == "/logout":
        # 清除会话 cookie 后回到登录页
        return ("logout", "/login")

    if req.me is None:
        return ("redirect", "/login")

    # 跨角色访问管理面：已登录但身份不符 -> 明确 403（而非 404 暴露存在性）
    if path.startswith("/doctor") and req.me["role"] != "doctor":
        return ("error", 403, "该区域仅限解读医生访问", req.me)
    if path.startswith("/staff") and req.me["role"] != "staff":
        return ("error", 403, "该区域仅限体检中心工作人员访问", req.me)

    # 下载令牌（任何已登录者点自己的链接；链接自身绑定身份）
    if method == "GET" and path.startswith("/d/"):
        return pages_patient.download(req, path[len("/d/"):])

    # 患者端
    if req.me["role"] == "patient":
        resp = _patient_routes(method, path, req)
        if resp is not None:
            return resp
    # 医生端
    if req.me["role"] == "doctor":
        resp = _doctor_routes(method, path, req)
        if resp is not None:
            return resp
    # 家属端
    if req.me["role"] == "family":
        resp = _family_routes(method, path, req)
        if resp is not None:
            return resp
    # 体检中心
    if req.me["role"] == "staff":
        resp = _staff_routes(method, path, req)
        if resp is not None:
            return resp

    return None


def _landing(user):
    return {"patient": "/home", "doctor": "/doctor",
            "family": "/family", "staff": "/staff"}[user["role"]]


def _patient_routes(method, path, req):
    r = _require(req, "patient")
    if r:
        return r
    P = pages_patient
    table = [
        ("GET", "/home", P.home),
        ("GET", "/calendar", P.calendar),
        ("POST", "/book", P.book),
        ("POST", "/reschedule", P.reschedule),
        ("POST", "/cancel", P.cancel),
        ("GET", "/reports", P.reports),
        ("GET", "/report", P.report_detail),
        ("GET", "/interpretations", P.interpretations),
        ("GET", "/interpretation", P.interpretation_detail),
        ("GET", "/consents", P.consents),
        ("POST", "/consents/grant", P.consent_grant),
        ("POST", "/consents/revoke", P.consent_revoke),
        ("POST", "/consents/dispute", P.consent_dispute),
        ("POST", "/consents/resolve", P.consent_resolve),
        ("POST", "/report/download-link", P.request_download),
        ("GET", "/mail", P.mailbox),
        ("POST", "/mail/read", P.mail_read),
        ("GET", "/logs", P.logs),
    ]
    for m, pat, fn in table:
        if method == m and path == pat:
            return _wrap(fn, req)
    return None


def _doctor_routes(method, path, req):
    r = _require(req, "doctor")
    if r:
        return r
    P = pages_doctor
    table = [
        ("GET", "/doctor", P.board),
        ("POST", "/doctor/suspend", P.suspend),
        ("GET", "/doctor/interpretations", P.list_sessions),
        ("GET", "/doctor/interpret", P.edit_session),
        ("POST", "/doctor/interpret/save", P.save_session),
        ("POST", "/doctor/interpret/sign", P.sign_session),
        ("POST", "/doctor/interpret/void", P.void_session),
        ("GET", "/report", pages_patient.report_detail),
        ("GET", "/d/", None),  # 下载在公共段处理
        ("GET", "/mail", pages_patient.mailbox),
        ("POST", "/mail/read", pages_patient.mail_read),
        ("GET", "/interpretation", pages_patient.interpretation_detail),
    ]
    for m, pat, fn in table:
        if method == m and path == pat:
            if fn is None:
                continue
            return _wrap(fn, req)
    if method == "GET" and path.startswith("/d/"):
        return pages_patient.download(req, path[len("/d/"):])
    return None


def _family_routes(method, path, req):
    r = _require(req, "family")
    if r:
        return r
    P = pages_family
    table = [
        ("GET", "/family", P.home),
        ("GET", "/report", pages_patient.report_detail),
        ("GET", "/interpretation", pages_patient.interpretation_detail),
        ("POST", "/report/download-link", pages_patient.request_download),
        ("POST", "/consents/dispute", pages_patient.consent_dispute),
        ("GET", "/mail", pages_patient.mailbox),
        ("POST", "/mail/read", pages_patient.mail_read),
    ]
    for m, pat, fn in table:
        if method == m and path == pat:
            return _wrap(fn, req)
    if method == "GET" and path.startswith("/d/"):
        return pages_patient.download(req, path[len("/d/"):])
    return None


def _staff_routes(method, path, req):
    r = _require(req, "staff")
    if r:
        return r
    P = pages_staff
    table = [
        ("GET", "/staff", P.home),
        ("GET", "/staff/report", P.report_admin),
        ("POST", "/staff/correct", P.correct),
        ("GET", "/staff/disputes", P.disputes),
        ("GET", "/mail", pages_patient.mailbox),
    ]
    for m, pat, fn in table:
        if method == m and path == pat:
            return _wrap(fn, req)
    return None


def _wrap(fn, req):
    try:
        resp = fn(req)
    except DomainError as e:
        return ("error", e.http_status, e.message, req.me)
    # 页面函数可直接返回 HTML 字符串，统一归一化为 ("html", ...)
    if isinstance(resp, str):
        return ("html", resp)
    return resp


def not_found(me):
    return page(title="未找到", body='<div class="card"><h2>页面不存在</h2>'
                '<p class="muted">请从导航进入功能页面。</p></div>', me=me)


def error_page(me, status, msg):
    icon = "🔒" if status == 403 else ("⛔" if status == 410 else "⚠")
    return page(title=f"{status}", active="", me=me, body=f"""
    <div class="card" style="max-width:560px;margin:24px auto;">
      <h2>{icon} {status} · {esc(msg)}</h2>
      <p class="muted">该操作有确定的处理规则。如对结果有疑问，请返回上一页重试或联系体检中心。</p>
      <div class="btnrow"><a class="btn ghost" href="javascript:history.back()">返回上一页</a>
      <a class="btn" href="/home">回到首页</a></div>
    </div>""")
