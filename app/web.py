"""HTTP 应用：标准库 http.server，线程化；会话用 HMAC 签名 cookie。

路由分三类角色：患者（预约/授权/报告/记录）、医生（工作台/停诊/解读签字）、
家属（共享给我的报告）。所有报告读取与下载都走 access_control，失败关闭。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .db import now
from .errors import AppError, PermissionDenied
from .mailer import MailAdapter
from .services import authorizations as auth_svc
from .services import interpretations as interp_svc
from .services import reports as report_svc
from .services import scheduling as sched
from . import templates as T

WEEK_CN = "周一 周二 周三 周四 周五 周六 周日".split()
ROLE_CN = {"patient": "受检人", "doctor": "医生", "family": "家属"}
NAV_BY_ROLE = {"patient": T.NAV_PATIENT, "doctor": T.NAV_DOCTOR,
               "family": T.NAV_FAMILY}


# ---------------------------------------------------------------- 会话

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(uid: int) -> str:
    payload = _b64e(json.dumps({"uid": uid}, separators=(",", ":")).encode())
    sig = hmac.new(config.SECRET.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def parse_session(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expect = hmac.new(config.SECRET.encode(), payload.encode(),
                      hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        return json.loads(_b64d(payload))["uid"]
    except Exception:
        return None


# ---------------------------------------------------------------- 格式化

def fmt_dt(s: str | None) -> str:
    if not s:
        return "—"
    try:
        d = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        return f"{d.month}月{d.day}日 {WEEK_CN[d.weekday()]} {d:%H:%M}"
    except ValueError:
        return s.replace("T", " ")[:16]


def fmt_date(s: str | None) -> str:
    if not s:
        return "—"
    wd = WEEK_CN[datetime.strptime(s[:10], "%Y-%m-%d").weekday()]
    return f"{s[5:7]}月{s[8:10]}日 {wd}"


def fmt_ts(s: str | None) -> str:
    if not s:
        return "长期有效" if s is None else "—"
    if s is None:
        return "—"
    return s.replace("T", " ").replace("Z", " UTC")


def channel_label(c: str) -> str:
    return "线上视频" if c == "online" else "现场面诊"


def abn_flag(status: str) -> str:
    if status == "high":
        return '<span class="tag red">↑ 偏高</span>'
    if status == "low":
        return '<span class="tag red">↓ 偏低</span>'
    return '<span class="tag green">正常</span>'


def ref_text(m) -> str:
    lo, hi, unit = m["ref_low"], m["ref_high"], m["unit"]
    if lo is not None and hi is not None:
        return f"{lo:g} ~ {hi:g} {unit}"
    if hi is not None:
        return f"≤ {hi:g} {unit}"
    if lo is not None:
        return f"≥ {lo:g} {unit}"
    return "—"


# ---------------------------------------------------------------- 应用

class App:
    def __init__(self, db):
        self.db = db
        self.mail = MailAdapter(db)


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "HealthCheckDemo/1.0"
    user = None
    _body = None

    def log_message(self, fmt, *args):
        pass

    # -- 基础工具 --
    def _cookie_uid(self):
        ck = SimpleCookie()
        ck.load(self.headers.get("Cookie", ""))
        morsel = ck.get(config.SESSION_COOKIE)
        return parse_session(morsel.value if morsel else None)

    def _send(self, body: bytes, status=200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, flash=None):
        if flash:
            kind, msg = flash
            sep = "&" if "?" in path else "?"
            path += sep + "flash=" + urllib.parse.quote(f"{kind}|{msg}")
        self._send(b"", 303, extra_headers=[("Location", path)])

    def _render(self, *, title, active, body, flash=None):
        nav_items = NAV_BY_ROLE.get(self.user["role"], T.NAV_PATIENT)
        return T.page(title=title, nav=T.nav_html(nav_items, active),
                      who=self.user["name"], role=ROLE_CN[self.user["role"]],
                      body=body, flash=flash)

    def _read_form(self) -> dict[str, str]:
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(self._body, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _form_list(self, key: str) -> list[str]:
        if self._body is None:
            self._read_form()
        return urllib.parse.parse_qs(self._body).get(key, [])

    def _qs(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

    @staticmethod
    def _flash(qs):
        f = qs.get("flash", [""])[0]
        if not f:
            return None
        kind, _, msg = f.partition("|")
        return (kind if kind in ("ok", "err") else "ok"), msg

    # -- 入口 --
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str):
        try:
            uid = self._cookie_uid()
            path = self.path.split("?", 1)[0]
            qs = self._qs()

            if path == "/login":
                if method == "GET":
                    return self._send(self._login_page())
                form = self._read_form()
                token = make_session(int(form["user_id"]))
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 f"{config.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/logout" and method == "POST":
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", f"{config.SESSION_COOKIE}=; Path=/; Max-Age=0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if uid is None:
                return self._redirect("/login")
            self.user = self.app.db.query_one("SELECT * FROM users WHERE id=?", (uid,))
            if self.user is None:
                return self._redirect("/login")

            flash = self._flash(qs)
            self._route(method, path, qs, flash)
        except AppError as e:
            self._error_page(e)
        except BrokenPipeError:
            pass
        except Exception:  # 兜底：任何未预期错误返回 500，不拖垮服务线程
            import traceback
            traceback.print_exc()
            try:
                self._send("<h2>500</h2><p>服务器内部错误，请查看控制台日志。</p>".encode("utf-8"), 500)
            except Exception:
                pass

    def _error_page(self, e: AppError):
        body = (f"<div class='card'><h2>请求未完成</h2>"
                f"<div class='banner danger'>{T.esc(e.message)}</div>"
                f"<p><a href='javascript:history.back()'>返回上一页</a> · "
                f"<a href='/'>回到首页</a></p></div>")
        active_nav = NAV_BY_ROLE.get(self.user["role"], {}) if self.user else {}
        self._send(T.page(title="提示", nav=T.nav_html(active_nav, ""),
                          who=(self.user["name"] if self.user else "未登录"),
                          role=(ROLE_CN.get(self.user["role"], "") if self.user else ""),
                          body=body), e.http_status)

    # -- 路由分发 --
    def _route(self, method, path, qs, flash):
        role = self.user["role"]
        uid = self.user["id"]

        # 角色通用
        if path == "/mail" and method == "GET":
            return self._mail_page(flash)
        if path == "/logs" and method == "GET":
            return self._logs_page()
        if path == "/records/appt" and method == "GET":
            return self._interpretation_page(int(qs.get("id", ["0"])[0]), flash)
        if path == "/reports" and method == "GET":
            return self._report_view(int(qs.get("id", ["0"])[0]), flash)
        if path == "/pdf" and method == "GET":
            return self._serve_report_pdf(int(qs.get("id", ["0"])[0]),
                                          qs.get("inline", ["0"])[0] == "1")
        if path.startswith("/d/") and method == "GET":
            return self._serve_token_download(path.rsplit("/", 1)[-1])

        if role == "patient":
            return self._patient_routes(method, path, qs, flash)
        if role == "doctor":
            return self._doctor_routes(method, path, qs, flash)
        if role == "family":
            return self._family_routes(method, path, qs, flash)
        self._not_found()

    # ================================================================ 登录

    def _login_page(self) -> bytes:
        groups = {"patient": [], "doctor": [], "family": []}
        for r in self.app.db.query("SELECT * FROM users ORDER BY role, id"):
            groups[r["role"]].append(r)
        names = {"patient": "受检人（本人）", "doctor": "解读医生", "family": "被授权家属"}
        cards = []
        for role in ("patient", "doctor", "family"):
            buttons = "".join(
                f'<button class="btn ghost" style="width:100%;justify-content:flex-start;'
                f'margin:4px 0;text-align:left" name="user_id" value="{u["id"]}">'
                f'{T.esc(u["name"])} <span class="muted small">（{T.esc(u["email"])}）</span>'
                f'</button>' for u in groups[role])
            cards.append(f"<div class='card'><h2>{names[role]}</h2>{buttons}</div>")
        body = f"""
<div class="hero">
  <div class="kicker">演示环境 · 身份切换</div>
  <div class="big">体检报告解读预约系统</div>
  <p class="sub">选择一个演示身份进入。全部人员与病历数据均为虚构合成；
  报告仅本人、被授权医生与明确授权家属可见。</p>
</div>
<div class="grid">{''.join(cards)}</div>"""
        return T.page(title="登录", nav="", who="未登录", role="", body=body)

    # ================================================================ 患者

    def _patient_routes(self, method, path, qs, flash):
        uid = self.user["id"]
        if path == "/" and method == "GET":
            return self._overview(flash)
        if path == "/calendar" and method == "GET":
            return self._calendar_page(qs, flash)
        if path == "/book" and method == "GET":
            return self._book_form(int(qs.get("slot", ["0"])[0]), flash)
        if path == "/book" and method == "POST":
            form = self._read_form()
            appt = sched.book(
                self.app.db, patient_id=uid, slot_id=int(form["slot_id"]),
                report_id=int(form["report_id"]),
                topics=self._form_list("topics"), created_by=uid)
            return self._redirect("/appointments",
                                  ("ok", f"预约成功：{fmt_dt(appt['start_at'])}，号源已锁定"))
        if path == "/reschedule" and method == "POST":
            form = self._read_form()
            new = sched.reschedule(
                self.app.db, appointment_id=int(form["appt_id"]),
                new_slot_id=int(form["slot_id"]), actor_id=uid)
            return self._redirect("/appointments",
                                  ("ok", f"改期成功：原号源已释放，已锁定 {fmt_dt(new['start_at'])}"))
        if path == "/cancel" and method == "POST":
            form = self._read_form()
            sched.cancel(self.app.db, appointment_id=int(form["appt_id"]), actor_id=uid)
            return self._redirect("/appointments", ("ok", "预约已取消，号源已释放"))
        if path == "/appointments" and method == "GET":
            return self._appointments_page(flash)
        if path == "/records" and method == "GET":
            return self._records_list(flash)
        if path == "/authorizations" and method == "GET":
            return self._auth_page(flash)
        if path == "/authorizations/grant" and method == "POST":
            form = self._read_form()
            days = form.get("expires_days", "").strip()
            auth_svc.grant(self.app.db, patient_id=uid,
                           grantee_id=int(form["grantee_id"]),
                           expires_days=int(days) if days else None)
            return self._redirect("/authorizations", ("ok", "授权已授予"))
        if path == "/authorizations/revoke" and method == "POST":
            form = self._read_form()
            auth_svc.revoke(self.app.db, patient_id=uid,
                            authorization_id=int(form["id"]))
            return self._redirect("/authorizations",
                                  ("ok", "授权已撤销，相关下载链接立即失效"))
        if path == "/authorizations/resolve" and method == "POST":
            form = self._read_form()
            auth_svc.resolve_dispute(self.app.db, patient_id=uid,
                                     authorization_id=int(form["id"]),
                                     decision=form["decision"])
            label = "已恢复授权" if form["decision"] == "restore" else "已正式撤销授权"
            return self._redirect("/authorizations", ("ok", label))
        if path == "/links" and method == "POST":
            form = self._read_form()
            link = auth_svc.create_link(
                self.app.db, patient_id=uid, grantee_id=int(form["grantee_id"]),
                report_id=int(form["report_id"]))
            return self._redirect("/authorizations",
                                  ("ok", f"下载链接已生成（72 小时有效，固定引用 V{link['version']}）"))
        self._not_found()

    # -- 概览（移动端首屏：预约时间 + 授权状态）--
    def _overview(self, flash):
        uid = self.user["id"]
        today = datetime.now().strftime("%Y-%m-%dT%H:%M")
        upcoming = self.app.db.query(
            "SELECT a.*, du.name doctor_name, ds.start_at, ds.channel "
            "FROM appointments a JOIN users du ON du.id=a.doctor_id "
            "JOIN slots ds ON ds.id=a.slot_id "
            "WHERE a.patient_id=? AND a.status='booked' AND ds.start_at>=? "
            "ORDER BY ds.start_at LIMIT 1", (uid, today))
        queue = sched.reschedule_queue(self.app.db, patient_id=uid)
        grants = auth_svc.list_grants(self.app.db, uid)
        active_count = sum(1 for g in grants if g["effective"])

        if upcoming:
            a = upcoming[0]
            appt_card = f"""
<div class="hero">
  <div class="kicker">我的最近预约 {T.tag(a['status'])}</div>
  <div class="big">{fmt_dt(a['start_at'])}</div>
  <div class="kv">
    <dt>解读医生</dt><dd>{T.esc(a['doctor_name'])}</dd>
    <dt>方式</dt><dd>{channel_label(a['channel'])}</dd>
    <dt>报告</dt><dd><a href="/reports?id={a['report_id']}">查看本单引用的报告版本</a></dd>
  </div>
  <div class="pillrow">
    <a class="btn" href="/calendar?reschedule={a['id']}">申请改期</a>
    <a class="btn ghost" href="/records/appt?id={a['id']}">解读记录</a>
  </div>
</div>"""
        else:
            appt_card = """
<div class="hero">
  <div class="kicker">我的预约</div>
  <div class="big">近期暂无已确认预约</div>
  <div class="pillrow"><a class="btn" href="/calendar">去号源日历预约</a></div>
</div>"""

        qbanner = ""
        if queue:
            items = "".join(
                f'<li>原预约 <b>{fmt_dt(q["start_at"])}</b>（{T.esc(q["doctor_name"])}）'
                f'因医生停诊无法进行 —— <a href="/calendar?reschedule={q["id"]}"><b>立即改约</b></a></li>'
                for q in queue)
            qbanner = (f'<div class="banner danger"><b>待改约（{len(queue)}）</b>'
                       f'<ul style="margin:6px 0 0 18px">{items}</ul></div>')

        grant_lines = "".join(
            f'<li>{T.esc(g["grantee_name"])} · '
            f'{"医生" if g["grantee_role"] == "doctor" else "家属"} '
            f'{T.tag("valid" if g["effective"] else g["status"])} '
            f'<span class="muted small">{fmt_ts(g["expires_at"])}</span></li>'
            for g in grants) or "<li class='muted'>暂无授权</li>"
        grant_card = f"""
<div class="card">
  <h2>授权状态</h2>
  <p class="sub">{active_count} 项有效授权；可随时撤销，撤销后其下载链接立即失效。</p>
  <ul style="margin:0 0 10px 18px">{grant_lines}</ul>
  <a class="btn mini ghost" href="/authorizations">管理授权与下载链接</a>
</div>"""

        rep_rows = ""
        for v in report_svc.versions(self.app.db, uid):
            corr = '<span class="tag teal">更正件</span>' if v["is_correction"] else ""
            rep_rows += (f'<tr><td>{v["report_no"]}</td><td>V{v["version"]} {corr}</td>'
                         f'<td>{T.esc(v["title"])}</td><td>{fmt_date(v["examined_at"])}</td>'
                         f'<td>{fmt_ts(v["issued_at"])}</td>'
                         f'<td><a href="/reports?id={v["id"]}">查看</a></td></tr>')
        reports_card = f"""
<div class="card">
  <h2>我的报告与版本</h2>
  <p class="sub">预约与解读记录固定引用其当时的报告版本；更正件出具后旧版本仍保留可查。</p>
  <table><thead><tr><th>报告编号</th><th>版本</th><th>名称</th>
  <th>体检日期</th><th>出具时间</th><th></th></tr></thead>
  <tbody>{rep_rows}</tbody></table>
  <p class="small muted" style="margin-top:10px">
  <a href="/logs">查看访问日志</a>（谁在何时以何身份访问了报告）</p>
</div>"""

        return self._send(self._render(
            title="概览", active="/",
            body=appt_card + qbanner + grant_card + reports_card, flash=flash))

    # -- 号源日历 --
    def _calendar_page(self, qs, flash):
        doctor_id = qs.get("doctor", [None])[0]
        doctor_id = int(doctor_id) if doctor_id else None
        if self.user["role"] == "doctor":
            doctor_id = self.user["id"]
        date_from = qs.get("from", [datetime.now().strftime("%Y-%m-%d")])[0]
        d0 = datetime.strptime(date_from, "%Y-%m-%d")
        date_to = (d0 + timedelta(days=13)).strftime("%Y-%m-%d")
        reschedule_id = qs.get("reschedule", [None])[0]

        slots = sched.calendar(self.app.db, date_from=date_from, date_to=date_to,
                               doctor_id=doctor_id)
        docs = sched.doctors(self.app.db)
        doc_opts = '<option value="">全部医生</option>' + "".join(
            f'<option value="{d["id"]}" {"selected" if d["id"] == doctor_id else ""}>'
            f'{T.esc(d["name"])}</option>' for d in docs)

        rbanner = ""
        if reschedule_id:
            appt = sched.get_appointment(self.app.db, int(reschedule_id))
            rbanner = (f'<div class="banner info">改约模式：原预约 '
                       f'<b>{fmt_dt(appt["start_at"])}</b>（{T.esc(appt["doctor_name"])}）'
                       f'的号源将在确认时释放并锁定新号源。 '
                       f'<a href="/appointments">退出改约</a></div>')

        days: dict[str, list] = {}
        for s in slots:
            days.setdefault(s["start_at"][:10], []).append(s)
        day_html = []
        for day in sorted(days):
            chips = []
            for s in days[day]:
                remaining = s["capacity"] - s["booked"]
                info = (f'<div><div class="t">{s["start_at"][11:16]}</div>'
                        f'<div class="small muted">{T.esc(s["doctor_name"])} · '
                        f'{channel_label(s["channel"])}</div></div>')
                if s["status"] == "suspended":
                    chips.append(f'<div class="chip suspended">{info}{T.tag("suspended")}</div>')
                elif remaining <= 0:
                    chips.append(f'<div class="chip full">{info}'
                                f'<span class="tag gray">已约满</span></div>')
                else:
                    if reschedule_id:
                        action = (f'<form method="post" action="/reschedule" class="inline">'
                                  f'<input type="hidden" name="appt_id" value="{reschedule_id}">'
                                  f'<input type="hidden" name="slot_id" value="{s["id"]}">'
                                  f'<button class="btn mini">选这个时段</button></form>')
                    else:
                        action = f'<a class="btn mini" href="/book?slot={s["id"]}">预约</a>'
                    chips.append(
                        f'<div class="chip">{info}'
                        f'<div class="small muted" style="margin-top:2px">余 {remaining}</div>'
                        f'{action}</div>')
            day_html.append(f'<div class="cal-day"><h4>{fmt_date(day)}</h4>'
                            f'<div class="slot-chips">{"".join(chips)}</div></div>')
        cal = "".join(day_html) or '<p class="muted">该区间暂无可约号源。</p>'
        prevw = (d0 - timedelta(days=14)).strftime("%Y-%m-%d")
        nextw = (d0 + timedelta(days=14)).strftime("%Y-%m-%d")
        doctor_filter = ""
        if self.user["role"] == "patient":
            doctor_filter = f'<div><label class="fld"><span>医生</span><select name="doctor">{doc_opts}</select></label></div>'
        hidden_rs = f'<input type="hidden" name="reschedule" value="{reschedule_id}">' if reschedule_id else ""
        body = f"""
{rbanner}
<div class="card">
  <h2>号源日历</h2>
  <p class="sub">线上视频或现场面诊时段任选；停诊时段不可约。多人抢同一时段时先确认者得，
  其余得到明确“已约满”提示，不会超卖。</p>
  <form method="get" action="/calendar" class="row">
    {doctor_filter}
    <div><label class="fld"><span>起始日期</span><input type="date" name="from" value="{date_from}"></label></div>
    {hidden_rs}
    <div style="flex:0"><button class="btn">查看</button></div>
    <div style="flex:0"><a class="btn ghost" href="/calendar?from={prevw}">‹ 前两周</a></div>
    <div style="flex:0"><a class="btn ghost" href="/calendar?from={nextw}">后两周 ›</a></div>
  </form>
</div>
<div class="card">{cal}</div>"""
        return self._send(self._render(title="号源日历", active="/calendar",
                                       body=body, flash=flash))

    # -- 预约表单 --
    def _book_form(self, slot_id, flash):
        slot = sched.get_slot(self.app.db, slot_id)
        doctor_name = self.app.db.query_one(
            "SELECT name FROM users WHERE id=?", (slot["doctor_id"],))["name"]
        versions = report_svc.versions(self.app.db, self.user["id"])
        latest = {}
        for v in versions:
            latest.setdefault(v["report_no"], v)
        opts, panels = [], []
        for i, (no, v) in enumerate(latest.items()):
            metrics = report_svc.metrics_of(self.app.db, v["id"])
            checks = "".join(
                f'<label><input type="checkbox" name="topics" value="{T.esc(m["metric_key"])}" '
                f'{"checked" if report_svc.metric_status(m) != "normal" else ""}> '
                f'{T.esc(m["label"])}</label>' for m in metrics)
            corr = "（更正件）" if v["is_correction"] else ""
            opts.append(f'<option value="{v["id"]}">{no} V{v["version"]}{corr} · {T.esc(v["title"])}</option>')
            panels.append(f'<div class="topic-panel" data-panel="{v["id"]}">{checks}</div>')
        body = f"""
<div class="card">
  <h2>预约报告解读</h2>
  <div class="kv">
    <dt>时段</dt><dd>{fmt_dt(slot['start_at'])}</dd>
    <dt>医生</dt><dd>{T.esc(doctor_name)}</dd>
    <dt>方式</dt><dd>{channel_label(slot['channel'])}</dd>
  </div>
  <form method="post" action="/book">
    <input type="hidden" name="slot_id" value="{slot_id}">
    <label class="fld"><span>选择要解读的报告（预约将固定引用此版本）</span>
      <select name="report_id" id="rep">{"".join(opts)}</select></label>
    <div class="fld"><span style="display:block;margin:6px 0;font-weight:600">
      预先勾选想讨论的指标（异常项默认勾选）</span>
      <div id="topics" class="checklist">{"".join(panels)}</div></div>
    <div class="pillrow"><button class="btn">确认预约</button>
      <a class="btn ghost" href="/calendar">返回</a></div>
  </form>
</div>
<script>
const panels=[...document.querySelectorAll('.topic-panel')];
const sel=document.getElementById('rep');
function refresh(){{
  panels.forEach(p=>p.style.display=(p.dataset.panel===sel.value?'block':'none'));
  // 提交时只有可见面板的勾选生效
}}
panels.forEach(p=>{{
  p.style.display=(p.dataset.panel===sel.value?'block':'none');
  p.querySelectorAll('input').forEach(c=>c.name=p.dataset.panel===sel.value?'topics':'off_'+c.value);
}});
sel.addEventListener('change',()=>{{
  const id=sel.value;
  panels.forEach(p=>p.querySelectorAll('input').forEach(c=>{{
    c.name=(p.dataset.panel===id)?'topics':'off_'+c.value;
  }}));
  refresh();
}});
</script>"""
        return self._send(self._render(title="预约", active="/calendar",
                                       body=body, flash=flash))

    # -- 我的预约 --
    def _appointments_page(self, flash):
        uid = self.user["id"]
        rows = self.app.db.query(
            "SELECT a.*, du.name doctor_name, ds.start_at, r.report_no, "
            "r.version report_version, ds.status slot_status "
            "FROM appointments a JOIN users du ON du.id=a.doctor_id "
            "JOIN slots ds ON ds.id=a.slot_id JOIN reports r ON r.id=a.report_id "
            "WHERE a.patient_id=? ORDER BY a.created_at DESC", (uid,))
        trs = []
        for a in rows:
            topics = json.loads(a["topics"] or "[]")
            actions = ""
            if a["status"] in ("booked", "reschedule_requested", "pending_reschedule"):
                actions = (f'<a class="btn mini ghost" href="/calendar?reschedule={a["id"]}">改期</a> '
                           f'<form method="post" action="/cancel" class="inline">'
                           f'<input type="hidden" name="appt_id" value="{a["id"]}">'
                           f'<button class="btn mini danger">取消</button></form>')
            corr = ('<span class="tag amber">该版本已被更正</span>'
                    if self._is_superseded(a["report_id"]) else "")
            trs.append(f"""<tr>
<td>{fmt_dt(a['start_at'])}<br><span class="small muted">{channel_label(a['channel'])}</span></td>
<td>{T.esc(a['doctor_name'])}</td>
<td>{T.esc(a['report_no'])} V{a['report_version']} {corr}
  <div class="small muted">{T.esc("、".join(topics)) or "—"}</div></td>
<td>{T.tag(a['status'])}</td>
<td>{actions} <a href="/records/appt?id={a['id']}">解读记录</a></td></tr>""")
        tbody = "".join(trs) or '<tr><td colspan="5" class="muted">暂无预约</td></tr>'
        body = f"""
<div class="card"><h2>我的预约</h2>
<p class="sub">改期在同一数据库事务中释放原号源并锁定新号源；医生停诊的预约自动进入待改约队列。</p>
<table><thead><tr><th>时段/方式</th><th>医生</th><th>报告版本与讨论指标</th>
<th>状态</th><th>操作</th></tr></thead><tbody>{tbody}</tbody></table></div>"""
        return self._send(self._render(title="我的预约", active="/appointments",
                                       body=body, flash=flash))

    def _is_superseded(self, report_id: int) -> bool:
        return self.app.db.query_one(
            "SELECT 1 FROM reports WHERE supersedes=?", (report_id,)) is not None

    # -- 解读记录列表 --
    def _records_list(self, flash):
        uid = self.user["id"]
        recs = interp_svc.list_for_patient(self.app.db, uid)
        rows = "".join(
            f'<tr><td>{fmt_date(r["created_at"][:10])}</td>'
            f'<td>{T.esc(r["doctor_name"])}</td>'
            f'<td>{T.esc(r["report_no"])} V{r["report_version"]}</td>'
            f'<td>{T.tag(r["status"])}</td>'
            f'<td><a href="/records/appt?id={r["appointment_id"]}">查看</a></td></tr>'
            for r in recs) or '<tr><td colspan="5" class="muted">暂无解读记录</td></tr>'
        upcoming = "".join(
            f'<li>{fmt_dt(u["start_at"])} · {T.esc(u["doctor_name"])} · '
            f'{T.esc(u["report_title"])} V{u["report_version"]} —— '
            f'<a href="/records/appt?id={u["id"]}">进入</a></li>'
            for u in sched.list_for_patient(self.app.db, uid))
        extra = f'<ul>{upcoming}</ul>' if upcoming else ""
        body = f"""
<div class="card"><h2>解读记录</h2>
<p class="sub">医生备注与指标趋势并置；未签字的记录明确标注“不具备正式效力”。</p>
{extra}
<table><thead><tr><th>日期</th><th>医生</th><th>报告版本</th><th>签字状态</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>"""
        return self._send(self._render(title="解读记录", active="/records",
                                       body=body, flash=flash))

    # -- 解读页（趋势 + 医生备注并置）--
    def _interpretation_page(self, appt_id, flash):
        bundle = interp_svc.get_for_reader(
            self.app.db, appointment_id=appt_id, actor_id=self.user["id"])
        appt = sched.get_appointment(self.app.db, appt_id)
        interp = bundle["interpretation"]
        report = report_svc.view_report(
            self.app.db, actor_id=self.user["id"], report_id=appt["report_id"])
        is_doctor = (self.user["role"] == "doctor" and self.user["id"] == appt["doctor_id"])
        doctor_name = self.app.db.query_one(
            "SELECT name FROM users WHERE id=?", (appt["doctor_id"],))["name"]

        corr_banner = ""
        if self._is_superseded(report["id"]):
            newer = self.app.db.query_one(
                "SELECT * FROM reports WHERE supersedes=? ORDER BY version DESC LIMIT 1",
                (report["id"],))
            corr_banner = f"""<div class="banner warn">
本单引用的是 <b>V{report['version']}</b>，该版本已被更正件
<a href="/reports?id={newer['id']}"><b>V{newer['version']}</b></a> 取代。
解读与结论以更正件为准；本单仍保留对原版本的固定引用。<br>
<span class="small">更正原因：{T.esc(newer['correction_note'])}</span></div>"""

        topic_keys = set(json.loads(appt["topics"] or "[]"))
        metrics_now = {m["metric_key"]: m for m in report["metrics"]}
        trend_map = {t["metric_key"]: t for t in report["trend"]}
        keys = list(metrics_now.keys())
        for k in topic_keys:
            if k not in metrics_now and k in trend_map and k not in keys:
                keys.append(k)
        notes_map = (interp or {}).get("notes_by_metric", {})

        metric_rows = []
        for key in keys:
            m = metrics_now.get(key)
            tr = trend_map.get(key)
            if m:
                if m["status"] == "high":
                    val = f'<span class="val-bad">{m["value"]:g} {T.esc(m["unit"])} ↑ 偏高</span>'
                elif m["status"] == "low":
                    val = f'<span class="val-bad">{m["value"]:g} {T.esc(m["unit"])} ↓ 偏低</span>'
                else:
                    val = f'{m["value"]:g} {T.esc(m["unit"])}'
                head = (f'<div class="metric-head">{T.esc(m["label"])}</div>'
                        f'<div class="metric-val">本次 {val} '
                        f'<span class="muted small">参考 {T.esc(ref_text(m))}</span></div>')
            else:
                head = (f'<div class="metric-head">{T.esc(tr["label"])}</div>'
                        f'<div class="metric-val muted small">本次报告未包含该指标</div>')
            svg = T.sparkline(tr["points"], tr["ref_low"], tr["ref_high"]) if tr else ""
            unsigned = bool(interp and not interp["is_signed"])
            note = notes_map.get(key, "")
            note_inner = T.esc(note) if note else '<span class="muted">医生暂未备注</span>'
            metric_rows.append(
                f'<div class="metric-row"><div>{head}'
                f'<div style="margin-top:6px">{svg}</div></div>'
                f'<div><div class="small muted" style="margin-bottom:4px">医生备注</div>'
                f'<div class="note-box {"unsigned" if unsigned else ""}">{note_inner}</div></div></div>')

        if interp and interp["is_signed"]:
            signer = self.app.db.query_one("SELECT name FROM users WHERE id=?",
                                           (interp["signer_id"],))["name"]
            sig_banner = f"""<div class="sig signed">✓ {T.esc(signer)} 已于
{fmt_ts(interp['signed_at'])} 电子签字 · 留痕 <code>{T.esc(interp['signature'])}</code>
· 正式记录</div>"""
        elif interp:
            sig_banner = ('<div class="sig unsigned">⚠ 会诊记录尚未签字 · '
                          '不具备正式效力，不能作为正式结论；须医生签字后生效。</div>')
        else:
            sig_banner = ('<div class="sig unsigned">医生尚未撰写解读记录。</div>')

        conclusion = ""
        if interp and interp.get("conclusion"):
            conclusion = (f'<h3>总体结论</h3><div class="note-box">'
                          f'{T.esc(interp["conclusion"])}</div>')
        editor = self._doctor_editor(appt, interp, keys, metrics_now, trend_map) if is_doctor else ""

        body = f"""
{corr_banner}
<div class="card">
  <h2>解读记录 · {T.esc(report['report_no'])} V{report['version']}</h2>
  <p class="sub">{T.esc(doctor_name)} · {fmt_dt(appt['start_at'])} · {channel_label(appt['channel'])}
   · <a href="/reports?id={report['id']}">查看报告 PDF 与全部版本</a></p>
  {sig_banner}
  <h3 style="margin-top:18px">指标趋势 与 医生备注</h3>
  <div class="trend-note">{"".join(metric_rows)}</div>
  {conclusion}{editor}
</div>"""
        return self._send(self._render(title="解读记录", active="/records",
                                       body=body, flash=flash))

    # -- 报告查看（鉴权）--
    def _report_view(self, report_id, flash):
        r = report_svc.view_report(self.app.db, actor_id=self.user["id"],
                                   report_id=report_id)
        corr_banner = ""
        if r["is_correction"]:
            old = self.app.db.query_one("SELECT * FROM reports WHERE id=?",
                                        (r["supersedes"],))
            corr_banner = f"""<div class="banner info">本件为<b>更正报告 V{r['version']}</b>，
取代 <a href="/reports?id={old['id']}">V{old['version']}</a>。
更正原因：{T.esc(r['correction_note'])}</div>"""
        elif self._is_superseded(r["id"]):
            newer = self.app.db.query_one(
                "SELECT * FROM reports WHERE supersedes=? ORDER BY version DESC LIMIT 1",
                (r["id"],))
            corr_banner = f"""<div class="banner warn">您正在查看的 V{r['version']}
已被 <a href="/reports?id={newer['id']}">更正件 V{newer['version']}</a> 取代；
旧版本按规定保留可查、内容不可变。</div>"""

        mrows = []
        for m in r["metrics"]:
            val = f'<span class="val-bad">{m["value"]:g}</span>' if m["status"] != "normal" else f'{m["value"]:g}'
            mrows.append(
                f"<tr><td>{T.esc(m['label'])}</td><td>{val}</td>"
                f"<td>{T.esc(ref_text(m))}</td><td>{abn_flag(m['status'])}</td></tr>")

        ver_rows = []
        for v in r["versions"]:
            label = "当前版本" if v["id"] == r["id"] else f'<a href="/reports?id={v["id"]}">查看</a>'
            tag = "（更正件）" if v["is_correction"] else ""
            ver_rows.append(
                f'<tr><td>V{v["version"]}{tag}</td><td>{fmt_ts(v["issued_at"])}</td>'
                f'<td><code>{T.esc((v["checksum"] or "")[:12])}</code></td><td>{label}</td></tr>')
        body = f"""
{corr_banner}
<div class="grid-3">
<div class="card">
  <h2>{T.esc(r['title'])} · {T.esc(r['report_no'])} V{r['version']}</h2>
  <p class="sub">体检日期 {fmt_date(r['examined_at'])} · 出具 {fmt_ts(r['issued_at'])}
  · PDF 校验和 <code>{T.esc((r['checksum'] or '')[:16])}</code></p>
  <table><thead><tr><th>指标</th><th>结果</th><th>参考范围</th><th>提示</th></tr></thead>
  <tbody>{"".join(mrows)}</tbody></table>
  <div class="pillrow" style="margin-top:14px">
    <a class="btn" href="/pdf?id={r['id']}&inline=1" target="_blank">查看 PDF</a>
    <a class="btn ghost" href="/pdf?id={r['id']}">下载 PDF（受控）</a>
  </div>
</div>
<div class="card">
  <h2>版本记录</h2>
  <p class="sub">每个版本有独立 PDF 与校验和，内容不可变。</p>
  <table><thead><tr><th>版本</th><th>出具</th><th>校验</th><th></th></tr></thead>
  <tbody>{"".join(ver_rows)}</tbody></table>
</div>
</div>"""
        active = "/records" if self.user["role"] != "patient" else "/appointments"
        return self._send(self._render(title="报告", active=active, body=body, flash=flash))

    # -- 授权管理 --
    def _auth_page(self, flash):
        uid = self.user["id"]
        grants = auth_svc.list_grants(self.app.db, uid)
        others = self.app.db.query(
            "SELECT * FROM users WHERE id!=? AND role IN ('doctor','family') ORDER BY role,id",
            (uid,))
        opts = "".join(
            f'<option value="{u["id"]}">{T.esc(u["name"])}'
            f'（{"医生" if u["role"] == "doctor" else "家属"}）</option>' for u in others)
        my_versions = report_svc.versions(self.app.db, uid)
        vopts = "".join(
            f'<option value="{v["id"]}">{v["report_no"]} V{v["version"]}</option>'
            for v in my_versions)
        cards = []
        for g in grants:
            link_lines = []
            for l in g["links"]:
                if l["status"] == "valid":
                    tail = f'<a href="/d/{T.esc(l["token"])}">下载链接</a>'
                else:
                    why = {"revoked": "授权已撤销", "expired": "已过期",
                           "exhausted": "次数用完"}[l["status"]]
                    tail = f'<span class="val-bad">链接已失效（{why}）</span>'
                link_lines.append(
                    f'<div class="small" style="margin:4px 0">V{l["version"]} · {tail} '
                    f'<span class="muted">{fmt_ts(l["expires_at"])} 前有效 · '
                    f'已用 {l["used_count"]}/{l["max_uses"]}</span></div>')
            links = "".join(link_lines) or '<div class="small muted">暂无下载链接</div>'

            dispute = ""
            if g["status"] == "disputed":
                dispute = f"""<div class="banner warn" style="margin:8px 0">
家属授权争议冻结中，暂停该家属的全部访问；只有本人可以裁决：
<form method="post" action="/authorizations/resolve" class="inline" style="margin-top:6px">
<input type="hidden" name="id" value="{g['id']}">
<button name="decision" value="restore" class="btn mini">恢复授权</button>
<button name="decision" value="revoke" class="btn mini danger">撤销授权</button></form></div>"""

            btns = []
            if g["status"] in ("active", "disputed"):
                btns.append(
                    f'<form method="post" action="/authorizations/revoke" class="inline">'
                    f'<input type="hidden" name="id" value="{g["id"]}">'
                    f'<button class="btn mini danger">撤销授权</button></form>')
            if g["effective"]:
                btns.append(
                    f'<form method="post" action="/links" class="inline" '
                    f'style="display:inline-flex;gap:6px;align-items:center">'
                    f'<input type="hidden" name="grantee_id" value="{g["grantee_id"]}">'
                    f'<select name="report_id" style="width:auto">{vopts}</select>'
                    f'<button class="btn mini ghost">生成下载链接</button></form>')
            cards.append(f"""<div class="card">
<h3>{T.esc(g['grantee_name'])} · {"医生" if g['grantee_role'] == 'doctor' else "家属"}
{T.tag("valid" if g["effective"] else g["status"])}</h3>
<p class="small muted">授予于 {fmt_ts(g['granted_at'])} · 到期 {fmt_ts(g['expires_at'])}
· 状态说明：{T.esc(g['effective_reason'])}</p>
{links}{dispute}
<div class="pillrow">{"".join(btns)}</div></div>""")
        body = f"""
<div class="card">
<h2>授权管理</h2>
<p class="sub">仅可向医生或家属授权；可设到期时间并随时撤销。撤销后其名下全部有效下载链接立即失效。</p>
<form method="post" action="/authorizations/grant" class="row">
<div><label class="fld"><span>被授权人</span><select name="grantee_id">{opts}</select></label></div>
<div><label class="fld"><span>有效期（天，留空为长期）</span>
<input type="number" name="expires_days" min="1" placeholder="如 30"></label></div>
<div style="flex:0"><button class="btn">授予</button></div></form>
</div>
{''.join(cards)}"""
        return self._send(self._render(title="授权管理", active="/authorizations",
                                       body=body, flash=flash))

    # ================================================================ 医生

    def _doctor_routes(self, method, path, qs, flash):
        uid = self.user["id"]
        if path == "/doctor" and method == "GET":
            return self._doctor_workbench(flash)
        if path == "/doctor/suspend" and method == "POST":
            form = self._read_form()
            res = sched.suspend_doctor_slots(
                self.app.db, doctor_id=uid, start_date=form["start_date"],
                end_date=form["end_date"], reason=form.get("reason", ""), actor_id=uid)
            return self._redirect("/doctor", ("ok",
                f"已停诊 {res['suspended_slots']} 个号源，"
                f"{len(res['affected_appointments'])} 个预约转入待改约并已邮件通知"))
        if path == "/doctor/appt" and method == "GET":
            return self._interpretation_page(int(qs.get("id", ["0"])[0]), flash)
        if path == "/doctor/appt/notes" and method == "POST":
            form = self._read_form()
            appt_id = int(form["appt_id"])
            notes = {k[5:]: v.strip() for k, v in form.items()
                     if k.startswith("note_") and v.strip()}
            interp_svc.save_notes(
                self.app.db, appointment_id=appt_id, doctor_id=uid,
                notes=json.dumps(notes, ensure_ascii=False),
                conclusion=form.get("conclusion", ""))
            return self._redirect(f"/doctor/appt?id={appt_id}",
                                  ("ok", "草稿已保存（尚未签字）"))
        if path == "/doctor/appt/sign" and method == "POST":
            form = self._read_form()
            appt_id = int(form["appt_id"])
            interp_svc.sign(self.app.db, appointment_id=appt_id, doctor_id=uid)
            return self._redirect(f"/doctor/appt?id={appt_id}",
                                  ("ok", "已电子签字，记录成为正式结论"))
        if path == "/calendar" and method == "GET":
            return self._calendar_page(qs, flash)
        self._not_found()

    def _doctor_workbench(self, flash):
        uid = self.user["id"]
        rows = self.app.db.query(
            "SELECT a.id appt_id,a.status,ds.start_at,ds.channel,"
            "u.name patient_name,r.report_no,r.version report_version,r.id report_id "
            "FROM appointments a JOIN slots ds ON ds.id=a.slot_id "
            "JOIN users u ON u.id=a.patient_id JOIN reports r ON r.id=a.report_id "
            "WHERE a.doctor_id=? ORDER BY ds.start_at DESC LIMIT 30", (uid,))
        trs = []
        for a in rows:
            i = self.app.db.query_one(
                "SELECT status FROM interpretations WHERE appointment_id=?",
                (a["appt_id"],))
            sig = T.tag(i["status"]) if i else '<span class="tag gray">未撰写</span>'
            sup = '<span class="tag amber">有更正件</span>' if self._is_superseded(a["report_id"]) else ""
            trs.append(f"""<tr><td>{fmt_dt(a['start_at'])}<br>
<span class="small muted">{channel_label(a['channel'])}</span></td>
<td>{T.esc(a['patient_name'])}</td>
<td>{T.esc(a['report_no'])} V{a['report_version']} {sup}</td>
<td>{T.tag(a['status'])}</td><td>{sig}</td>
<td><a class="btn mini" href="/doctor/appt?id={a['appt_id']}">处理</a></td></tr>""")
        tbody = "".join(trs) or '<tr><td colspan="6" class="muted">暂无预约</td></tr>'
        today = datetime.now().strftime("%Y-%m-%d")
        far = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
        body = f"""
<div class="card"><h2>临时停诊</h2>
<p class="sub">设定停诊区间后，受影响预约自动转为「待改约」、释放号源，并经模拟邮件通知患者。</p>
<form method="post" action="/doctor/suspend" class="row">
<div><label class="fld"><span>停诊开始</span><input type="date" name="start_date" value="{today}"></label></div>
<div><label class="fld"><span>停诊结束</span><input type="date" name="end_date" value="{far}"></label></div>
<div style="flex:2"><label class="fld"><span>原因</span>
<input type="text" name="reason" placeholder="如：参加突发医疗保障任务"></label></div>
<div style="flex:0"><button class="btn danger">临时停诊</button></div></form>
</div>
<div class="card"><h2>预约与解读状态</h2>
<table><thead><tr><th>时段</th><th>患者</th><th>报告版本</th><th>预约</th><th>记录</th><th></th></tr></thead>
<tbody>{tbody}</tbody></table></div>"""
        return self._send(self._render(title="医生工作台", active="/doctor",
                                       body=body, flash=flash))

    def _doctor_editor(self, appt, interp, keys, metrics_now, trend_map):
        labels = {k: (metrics_now.get(k) or trend_map.get(k) or {}).get("label", k)
                  for k in keys}
        notes = (interp or {}).get("notes_by_metric", {})
        fields = "".join(
            f'<label class="fld"><span>{T.esc(labels[k])}</span>'
            f'<textarea name="note_{k}">{T.esc(notes.get(k, ""))}</textarea></label>'
            for k in keys)
        conclusion = (interp or {}).get("conclusion", "") if interp else ""
        signed = bool(interp and interp["is_signed"])
        return f"""
<h3 style="margin-top:22px">医生撰写区</h3>
<p class="sub">先存草稿；签字后成为正式记录。已签字记录若再修改，将自动转为“修改待重签”。</p>
<form method="post" action="/doctor/appt/notes">
<input type="hidden" name="appt_id" value="{appt['id']}">{fields}
<label class="fld"><span>总体结论</span><textarea name="conclusion">{T.esc(conclusion)}</textarea></label>
<button class="btn ghost">保存草稿</button></form>
<form method="post" action="/doctor/appt/sign" style="margin-top:10px">
<input type="hidden" name="appt_id" value="{appt['id']}">
<button class="btn" {"disabled" if signed else ""}>{'已签字 ✓' if signed else '核对无误，电子签字'}</button>
</form>"""

    # ================================================================ 家属

    def _family_routes(self, method, path, qs, flash):
        if path == "/shared" and method == "GET":
            return self._shared_page(flash)
        self._not_found()

    def _shared_page(self, flash):
        uid = self.user["id"]
        from .services.access_control import is_effective
        blocks = []
        for a in self.app.db.query(
                "SELECT * FROM authorizations WHERE grantee_id=? ORDER BY id", (uid,)):
            patient = self.app.db.query_one("SELECT * FROM users WHERE id=?",
                                            (a["patient_id"],))
            eff, why = is_effective(a)
            if eff:
                vrows = "".join(
                    f'<li>{v["report_no"]} V{v["version"]} · '
                    f'<a href="/reports?id={v["id"]}">在线查看</a> · '
                    f'<a href="/pdf?id={v["id"]}">下载 PDF</a></li>'
                    for v in self.app.db.query(
                        "SELECT * FROM reports WHERE patient_id=? ORDER BY report_no,version DESC",
                        (a["patient_id"],)))
                content = f'<ul style="margin:6px 0 0 18px">{vrows}</ul>'
            else:
                content = (f'<div class="banner danger">当前不可访问：{T.esc(why)}。'
                           f'名下下载链接同步失效。</div>')
            link_lines = []
            for l in self.app.db.query(
                    "SELECT * FROM download_links WHERE grantee_id=? AND authorized_id=? "
                    "ORDER BY id DESC", (uid, a["id"])):
                if l["status"] == "valid":
                    tail = f'<a href="/d/{T.esc(l["token"])}">下载链接</a>'
                else:
                    tail = '<span class="val-bad">链接已失效</span>'
                link_lines.append(
                    f'<div class="small" style="margin:4px 0">V{l["version"]} · {tail} '
                    f'<span class="muted">{fmt_ts(l["expires_at"])} 前有效</span></div>')
            blocks.append(f"""<div class="card">
<h2>{T.esc(patient['name'])} 的报告 {T.tag("valid" if eff else a["status"])}</h2>
<p class="sub">授权范围：{T.esc(a['scope'])} · 到期 {fmt_ts(a['expires_at'])}</p>
{content}{"".join(link_lines)}</div>""")
        intro = ('<div class="card"><h2>共享给我的报告</h2>'
                 '<p class="sub">仅展示本人作为被授权家属、且授权当前有效的报告。</p></div>')
        body = intro + ("".join(blocks) or '<div class="card muted">暂无家属授权。</div>')
        return self._send(self._render(title="共享给我", active="/shared",
                                       body=body, flash=flash))

    # ================================================================ 文件/其他

    def _safe_pdf_path(self, rel: str) -> str:
        base = os.path.realpath(config.PDF_DIR)
        full = os.path.realpath(os.path.join(config.DATA_DIR, rel))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            raise PermissionDenied("文件不在允许的目录内")
        return full

    def _serve_report_pdf(self, report_id: int, inline: bool):
        r = report_svc.view_report(self.app.db, actor_id=self.user["id"],
                                   report_id=report_id)
        full = self._safe_pdf_path(r["pdf_path"])
        data = open(full, "rb").read()
        fname = f"{r['report_no']}_v{r['version']}.pdf"
        self._send(data, 200, "application/pdf",
                   [("Content-Disposition",
                     f'{"inline" if inline else "attachment"}; '
                     f"filename*=UTF-8''{urllib.parse.quote(fname)}")])

    def _serve_token_download(self, token: str):
        res = auth_svc.resolve_link(self.app.db, token=token, actor_id=self.user["id"])
        # 只有链接的被授权家属/医生或本人可用此链接
        if self.user["id"] not in (res["report"]["patient_id"],
                                   self.app.db.query_one(
                                       "SELECT grantee_id FROM download_links WHERE token=?",
                                       (token,))["grantee_id"]):
            raise PermissionDenied("该下载链接不属于当前身份")
        full = self._safe_pdf_path(res["path"])
        data = open(full, "rb").read()
        r = res["report"]
        fname = f"{r['report_no']}_v{r['version']}.pdf"
        self._send(data, 200, "application/pdf",
                   [("Content-Disposition",
                     f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}")])

    def _mail_page(self, flash):
        msgs = self.app.mail.list_for(self.user["email"], limit=30)
        items = "".join(
            f'<div class="card"><h3>{T.esc(m["subject"])}</h3>'
            f'<p class="small muted">{fmt_ts(m["ts"])} → {T.esc(m["to_addr"])}</p>'
            f'<div style="white-space:pre-wrap">{T.esc(m["body"])}</div></div>'
            for m in msgs) or '<div class="card muted">模拟邮箱为空。</div>'
        return self._send(self._render(title="模拟邮件", active="/mail",
                                       body=items, flash=flash))

    def _logs_page(self):
        rows = self.app.db.query(
            "SELECT l.*, u.name actor_name FROM access_logs l "
            "LEFT JOIN users u ON u.id=l.actor_id ORDER BY l.id DESC LIMIT 100")
        trs = "".join(
            f'<tr style="{"" if r["allowed"] else "background:var(--red-bg)"}"><td>{r["id"]}</td>'
            f'<td>{fmt_ts(r["ts"])}</td><td>{T.esc(r["actor_name"] or "—")}</td>'
            f'<td>{T.esc(r["action"])}</td><td>{T.esc(r["target_type"])}#{r["target_id"]}</td>'
            f'<td>{"✓" if r["allowed"] else "✗ 拒绝"}</td>'
            f'<td class="small">{T.esc(r["detail"])}</td></tr>' for r in rows)
        body = f"""<div class="card"><h2>访问日志（最近 100 条）</h2>
<table><thead><tr><th>#</th><th>时间</th><th>访问者</th><th>动作</th>
<th>对象</th><th>结果</th><th>详情</th></tr></thead><tbody>{trs}</tbody></table></div>"""
        return self._send(self._render(title="访问日志", active="/logs", body=body))

    def _not_found(self):
        self._send(self._render(title="404", active="",
                                body="<div class='card'>页面不存在。</div>"), 404)


# ---------------------------------------------------------------- 启动

def build_server(db, host=config.HOST, port=config.PORT):
    class BoundHandler(Handler):
        pass

    BoundHandler.app = App(db)
    return ThreadingHTTPServer((host, port), BoundHandler)
