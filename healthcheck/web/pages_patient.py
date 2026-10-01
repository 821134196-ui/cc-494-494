"""患者端（同时被家属/医生复用报告与解读只读视图）。"""
from __future__ import annotations

import json
import urllib.parse
from datetime import timedelta

from .. import config, timeutil
from ..charts import trend_svg
from ..errors import LinkInvalid
from .layout import esc, fa, page, qs, status_badge, with_flash

MODE_CN = {"online": "线上视频", "onsite": "现场"}


# ---------------- 登录 ----------------
def login(req, err=""):
    groups = [
        ("patient", "患者（本人）"),
        ("doctor", "解读医生"),
        ("family", "被授权家属"),
        ("staff", "体检中心"),
    ]
    cards = []
    for role, label in groups:
        users = req.conn.execute(
            "SELECT * FROM users WHERE role=? ORDER BY code", (role,)).fetchall()
        items = []
        for u in users:
            sub = u["title"] or u["department"] or u["code"]
            items.append(
                f'<button type="submit" name="uid" value="{u["id"]}" class="loginas">'
                f'<span><b>{esc(u["name"])}</b>'
                f'<span class="muted small"> {esc(sub)}</span></span>'
                f'<span class="r">{esc(u["code"])} →</span></button>')
        cards.append(
            f'<h3 class="small muted">{label}</h3><form class="login-grid">'
            + "".join(items) + "</form>")
    body = f"""
    <div class="card login-card">
      <h2><span class="dot" style="color:var(--teal)">●</span> 体检报告解读平台</h2>
      <p class="muted small">选择一个演示身份进入。真实环境中此处为本人实名登录，
      家属与医生仅在获得明确授权后可见报告。</p>
      {f'<div class="flash err">{esc(err)}</div>' if err else ''}
      {''.join(cards)}
    </div>
    <style>button.loginas{{all:unset;cursor:pointer;display:flex;justify-content:space-between;
      align-items:center;gap:10px;padding:11px 14px;border:1px solid var(--line);
      border-radius:10px;background:#fff;width:100%;box-sizing:border-box;}}
      button.loginas:hover{{border-color:var(--teal);background:var(--teal-soft);}}</style>
    """
    return page(title="选择身份", body=body, me=None)


# ---------------- 首页：预约时间 + 授权状态优先 ----------------
def home(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    appts = ctx.booking.my_appointments(me["id"])
    pending = [a for a in appts if a["status"] == "reschedule_pending"]
    upcoming = [a for a in appts if a["status"] == "booked" and a["start_at"]
                and timeutil.at(a["start_at"]) >= timeutil.now()]
    upcoming.sort(key=lambda a: a["start_at"])
    nxt = upcoming[0] if upcoming else None

    consents = ctx.consents.list_for_patient(me["id"])
    n_active = sum(1 for c in consents if c["status"] == "active")
    n_dispute = sum(1 for c in consents if c["status"] == "disputed")
    sessions = ctx.interpretation.for_patient(me["id"])
    n_unsigned = sum(1 for s in sessions if s["status"] == "draft")

    parts = []
    # 1) 首屏：下一次预约时间
    if nxt:
        slot = conn.execute("SELECT * FROM slots WHERE id=?",
                            (nxt["slot_id"],)).fetchone()
        dt = timeutil.at(nxt["start_at"])
        inds = _indicator_names(conn, nxt)
        parts.append(f"""
        <section class="hero">
          <div class="muted small">您的下一次报告解读</div>
          <div class="when"><span class="clock">{fa('clock')}</span>
            {dt.strftime('%m月%d日 %H:%M')}
            <span class="badge teal">{MODE_CN[nxt['mode']]}</span></div>
          <div class="sub">{esc(nxt['doctor_name'])} · {esc(nxt['doctor_title'] or '')}
            · 预约号 {esc(nxt['code'])}</div>
          <div class="authline">
            <a class="btn ghost sm" href="/calendar?reschedule={nxt['code']}">改期</a>
            <form class="inline-form" method="post" action="/cancel">
              <input type="hidden" name="appt_code" value="{esc(nxt['code'])}">
              <button class="btn danger sm">取消预约</button></form>
            <span class="muted small">预先讨论：{esc('、'.join(inds)) or '未勾选'}</span>
          </div>
        </section>""")
    else:
        parts.append("""
        <section class="hero">
          <div class="muted small">您的下一次报告解读</div>
          <div class="when" style="font-size:1.1rem">暂无已预约时段</div>
          <div class="sub">选择线上或现场号源，预约医生解读年度体检报告。</div>
          <div class="authline"><a class="btn" href="/calendar">预约解读</a></div>
        </section>""")

    # 2) 待改约（医生停诊）
    for a in pending:
        parts.append(f"""
        <div class="alert warn"><span class="ic">{fa('warn')}</span>
          <div><b>预约 {esc(a['code'])} 需改约</b><br>
          {esc(a['pending_reason'] or '原号源已不可用')}，原号源已释放。
          请尽快选择同一位医生的新时段（不改约不产生费用）。
          <div class="btnrow"><a class="btn sm" href="/calendar?reschedule={a['code']}">
            立即选择新时段</a></div></div></div>""")

    # 3) 授权状态
    authline = (
        f'<span class="badge ok">{fa("shield")} 有效授权 {n_active}</span>'
        + (f'<span class="badge warn">{fa("warn")} 争议待处理 {n_dispute}</span>'
           if n_dispute else ''))
    parts.append(f"""
    <section class="card">
      <h2>{fa('shield')} 授权状态
        <a class="btn ghost sm" style="margin-left:auto" href="/consents">管理授权</a></h2>
      <div class="row">{authline}</div>
      <p class="muted small" style="margin-bottom:0">报告仅对您本人、被授权医生和明确授权的家属开放。
        授权可设到期时间并随时撤销，撤销后对方下载链接立即失效。</p>
    </section>""")

    # 4) 未签字解读提醒
    if n_unsigned:
        parts.append(f"""
        <div class="alert info"><span class="ic">{fa('info')}</span>
          <div>有 <b>{n_unsigned}</b> 份会诊记录尚待医生签字，签字前不作为正式结论。
          <a href="/interpretations">查看解读记录</a></div></div>""")

    # 5) 我的预约概览
    rows = "".join(_appt_line(conn, a) for a in appts[:6]) or \
        '<p class="muted small">暂无预约记录。</p>'
    parts.append(f"""
    <section class="card">
      <h2>{fa('calendar')} 我的预约</h2>{rows}
      <div class="btnrow"><a class="btn ghost sm" href="/calendar">预约新解读</a></div>
    </section>""")
    return page(title="我的预约", body="".join(parts), me=me, active="home",
                flash=req.flash())


def _indicator_names(conn, appt):
    try:
        codes = json.loads(appt["indicators"] or "[]")
    except Exception:
        codes = []
    if not codes:
        return []
    qmarks = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT DISTINCT name FROM metrics WHERE code IN ({qmarks})", codes).fetchall()
    names = [r["name"] for r in rows]
    return names or codes


def _appt_line(conn, a):
    if a["status"] == "reschedule_pending":
        when = "待选择新时段"
    elif a["start_at"]:
        when = timeutil.at(a["start_at"]).strftime("%Y-%m-%d %H:%M")
    else:
        when = "—"
    return f"""
    <div class="list-item"><div class="row spread">
      <div><b>{esc(a['doctor_name'])}</b>
        <span class="badge {'teal' if a['mode']=='online' else 'gray'}">{MODE_CN[a['mode']]}</span>
        {status_badge(a['status'])}
      </div>
      <span class="small muted">{esc(when)}</span></div>
      <div class="small muted">预约号 {esc(a['code'])}
        {'· ' + esc(a['pending_reason']) if a['pending_reason'] else ''}</div>
    </div>"""


# ---------------- 号源日历 + 预约/改约 ----------------
def calendar(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    doctors = conn.execute(
        "SELECT * FROM users WHERE role='doctor' ORDER BY id").fetchall()
    doctor_id = int(req.q.get("doctor", doctors[0]["id"]))
    reschedule_code = req.q.get("reschedule")
    reports = ctx.reports.patient_reports(me["id"])

    reschedule_appt = None
    if reschedule_code:
        reschedule_appt = conn.execute(
            "SELECT * FROM appointments WHERE code=? AND patient_id=?",
            (reschedule_code, me["id"])).fetchone()
        if reschedule_appt:
            doctor_id = reschedule_appt["doctor_id"]

    days = 21
    slots = ctx.booking.calendar(doctor_id, days=days)
    # 按日期分组
    by_day: dict[str, list] = {}
    for s in slots:
        if timeutil.at(s["start_at"]) <= timeutil.now():
            continue
        by_day.setdefault(s["start_at"][:10], []).append(s)

    doctor_opts = "".join(
        f'<option value="{d["id"]}" {"selected" if d["id"]==doctor_id else ""}>'
        f'{esc(d["name"])}（{esc(d["title"])}）</option>' for d in doctors)

    banner = ""
    if reschedule_appt:
        banner = f"""
        <div class="alert info"><span class="ic">{fa('refresh')}</span>
        <div>正在为预约 <b>{esc(reschedule_appt['code'])}</b> 改期。
        请选择 <b>同一位医生</b> 的新时段；提交后原号源立即释放、新号源立即锁定。</div></div>"""

    report_opts = "".join(
        f'<option value="{r["id"]}">{esc(r["title"])}'
        f'{"（更正件 v%d）" % r["version_no"] if r["kind"]=="correction" else ""}</option>'
        for r in reports)

    day_html = []
    weekday = "一二三四五六日"
    for d in sorted(by_day):
        dt = timeutil.at(d + "T00:00:00")
        head = f'{int(d[5:7])}月{int(d[8:10])}日 周{weekday[dt.weekday()]}'
        lines = []
        for s in by_day[d]:
            t = timeutil.at(s["start_at"]).strftime("%H:%M")
            full = s["booked_count"] >= s["capacity"]
            stopped = s["status"] == "canceled"
            blocked = s["status"] == "blocked"
            right = ""
            if stopped:
                right = ('<span class="stopped small">医生停诊</span>')
            elif blocked:
                right = (f'<span class="muted small">{esc(s["note"] or "暂停开放")}</span>')
            elif full:
                right = '<span class="full small">已约满</span>'
            elif reschedule_appt:
                if s["doctor_id"] != reschedule_appt["doctor_id"]:
                    right = '<span class="muted small">非同一医生</span>'
                else:
                    right = (f'<form class="inline-form" method="post" action="/reschedule">'
                             f'<input type="hidden" name="appt_code" value="{esc(reschedule_appt["code"])}">'
                             f'<input type="hidden" name="new_slot_id" value="{s["id"]}">'
                             f'<button class="btn sm">改到此时段</button></form>')
            else:
                right = (f'<button class="btn sm" onclick="document.getElementById('
                         f"'bk-{s['id']}').scrollIntoView({{behavior:'smooth'}})\">"
                         f'选择</button>')
            lines.append(f"""
            <div class="slotline">
              <div><span class="t">{t}</span>
                <span class="badge {'teal' if s['mode']=='online' else 'gray'}">
                  {MODE_CN[s['mode']]}</span>
                <span class="meta">余 {s['capacity']-s['booked_count']} 个号</span></div>
              {right}</div>""")
        day_html.append(f'<div class="cal-day"><div class="d">{head}</div>{"".join(lines)}</div>')

    # 预约表单（指标勾选来自最新报告）
    latest_report = reports[0] if reports else None
    check_html = ""
    if latest_report:
        metrics = ctx.reports.metrics(latest_report["current_version_id"])
        check_html = "".join(
            f'<label><input type="checkbox" name="indicators" value="{esc(m["code"])}" '
            f'{"checked" if m["flag"] in ("H","L") else ""}>'
            f'{esc(m["name"])}{("（"+("↑" if m["flag"]=="H" else "↓")+"）") if m["flag"] in ("H","L") else ""}</label>'
            for m in metrics)

    form = ""
    if not reschedule_appt and latest_report:
        form = f"""
        <section class="card" id="booking-form">
          <h2>{fa('doc')} 确认预约并勾选想讨论的指标</h2>
          <form method="post" action="/book">
            <label class="fld"><span>选择医生 / 时段（在上方日历点“选择”，或直接指定）</span>
              <select name="slot_id" required>
                {''.join(f'<option value="{s["id"]}">{esc(_slot_label(s))}</option>'
                         for sl in by_day.values() for s in sl
                         if s['status']=='open' and s['booked_count']<s['capacity'])}
              </select></label>
            <input type="hidden" name="report_id" value="{latest_report['id']}">
            <label class="fld"><span>预先勾选想讨论的指标（异常项已默认勾选）</span>
              <div class="checkgrid">{check_html}</div></label>
            <button class="btn">确认预约</button>
            <span class="muted small" style="margin-left:8px">开始前 2 小时内不可自助改期/取消</span>
          </form>
        </section>"""
    elif not reschedule_appt:
        form = '<div class="card"><p class="muted">暂无可解读的报告。</p></div>'

    body = f"""
    {banner}
    <section class="card">
      <h2>{fa('calendar')} 号源日历
        <form method="get" style="margin-left:auto"><select name="doctor"
          onchange="this.form.submit()" style="width:auto">{doctor_opts}</select>
          {f'<input type="hidden" name="reschedule" value="{esc(reschedule_code)}">' if reschedule_code else ''}
        </form></h2>
      <div style="max-height:none">{''.join(day_html) or
        '<p class="muted small">该医生近期暂无可约号源。</p>'}</div>
    </section>
    {form}"""
    return page(title="预约解读", body=body, me=me, active="calendar",
                flash=req.flash())


def _slot_label(s):
    return (f"{s['doctor_name']} · {s['start_at'][5:16].replace('T',' ')} "
            f"{MODE_CN[s['mode']]}")


def book(req):
    me, ctx = req.me, req.ctx
    slot_id = int(req.form["slot_id"])
    report_id = int(req.form["report_id"])
    indicators = req.getlist("indicators")
    appt = ctx.booking.book(patient_id=me["id"], slot_id=slot_id,
                            report_id=report_id, indicators=indicators)
    return ("redirect", with_flash("/home", "ok",
            flash=f"预约成功，预约号 {appt['code']}，确认邮件已发送"))


def reschedule(req):
    me, ctx = req.me, req.ctx
    code = req.form["appt_code"]
    owned = ctx.conn.execute(
        "SELECT id FROM appointments WHERE code=? AND patient_id=?",
        (code, me["id"])).fetchone()
    if not owned:
        return ("error", 404, "预约不存在", me)
    appt = ctx.booking.reschedule(code, int(req.form["new_slot_id"]))
    return ("redirect", with_flash("/home", "ok",
            flash=f"改期成功：原号源已释放，新号源已锁定（{appt['code']}）"))


def cancel(req):
    me, ctx = req.me, req.ctx
    code = req.form["appt_code"]
    owned = ctx.conn.execute(
        "SELECT id FROM appointments WHERE code=? AND patient_id=?",
        (code, me["id"])).fetchone()
    if not owned:
        return ("error", 404, "预约不存在", me)
    ctx.booking.cancel(code)
    return ("redirect", with_flash("/home", "ok", flash="预约已取消，号源已释放"))


# ---------------- 报告列表 / 详情（趋势 + 备注并置） ----------------
def reports(req):
    me, ctx = req.me, req.ctx
    rows = ctx.reports.patient_reports(me["id"])
    items = []
    for r in rows:
        cor = ('<span class="badge warn">含更正件 v%d</span>' % r["version_no"]
               if r["kind"] == "correction" else '<span class="badge teal">正式报告</span>')
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(r['title'])}</b> {cor}<br>
          <span class="small muted">体检日期 {esc(r['exam_date'])} · 当前版本 v{r['version_no']}</span></div>
          <a class="btn ghost sm" href="/report?id={r['id']}">查看</a></div>""")
    body = f"""
    <section class="card"><h2>{fa('doc')} 我的体检报告</h2>
      {''.join(items) or '<p class="muted small">暂无报告。</p>'}</section>"""
    return page(title="我的报告", body=body, me=me, active="reports",
                flash=req.flash())


def report_detail(req):
    """本人/医生/家属共用：先鉴权。"""
    me, ctx, conn = req.me, req.ctx, req.conn
    rid = int(req.q["id"])
    report = ctx.reports.get_report(rid)
    if report is None:
        return ("error", 404, "报告不存在", me)
    consent = ctx.access.require_report(report, me, "view", ip=req.ip)

    versions = ctx.reports.versions(rid)
    sel_no = int(req.q.get("version", versions[0]["version_no"]))
    version = next((v for v in versions if v["version_no"] == sel_no), versions[0])
    is_current = version["id"] == report["current_version_id"]
    metrics = ctx.reports.metrics(version["id"])

    # 版本切换
    vtabs = []
    for v in versions:
        cur = v["id"] == version["id"]
        label = f"v{v['version_no']}" + ("（更正件）" if v["kind"] == "correction" else "（原件）")
        if v["id"] != report["current_version_id"]:
            label += " · 已留档"
        vtabs.append(
            f'<a class="btn {"ghost" if not cur else ""} sm" '
            f'style="pointer-events:{"none" if cur else "auto"}" '
            f'href="/report?id={rid}&version={v["version_no"]}">{label}</a>')

    # 指标表（数值 + 参考范围）
    flag_cn = {"N": "正常", "H": "↑ 偏高", "L": "↓ 偏低", "T": "—"}
    trs = []
    for m in metrics:
        if m["flag"] == "T":
            val = esc(m["value_text"] or "—"); unit = ""
        else:
            val = "%g" % m["value"] if m["value"] is not None else "—"
            unit = esc(m["unit"] or "")
        ref = esc(m["ref_text"] or _ref_range(m))
        abn = "abn" if m["flag"] in ("H", "L") else ""
        trs.append(f"""
        <tr class="{abn}">
          <td>{esc(m['name'])}</td>
          <td class="val">{val} <span class="muted small">{unit}</span></td>
          <td class="ref">{ref} <span class="muted small">{unit}</span></td>
          <td class="flag-{m['flag']}">{flag_cn[m['flag']]}</td></tr>""")

    cor_banner = ""
    if version["kind"] == "correction":
        cor_banner = f"""
        <div class="alert info"><span class="ic">{fa('info')}</span><div>
        <b>这是更正报告 v{version['version_no']}</b><br>
        更正原因：{esc(version['correction_reason'])}<br>
        旧版本仅作留档、不再提供下载；既往解读仍引用其当时版本。</div></div>"""
    elif not is_current:
        cor_banner = """
        <div class="alert warn"><span class="ic">!</span><div>
        <b>这是已被替代的留档版本</b>，仅供本人追溯核对，不提供下载。
        最新版本含更正内容。</div></div>"""

    # 下载：本人/授权医生家属可申请当前版本链接
    dl = ""
    can_dl = is_current and (
        me["id"] == report["patient_id"]
        or (consent and "download" in (consent["scope"] or "")))
    if can_dl:
        dl = f"""
        <form method="post" action="/report/download-link">
          <input type="hidden" name="report_id" value="{rid}">
          <input type="hidden" name="version_id" value="{version['id']}">
          <button class="btn">{fa('download')} 生成下载链接（PDF）</button>
          <span class="muted small">链接绑定您的身份；授权撤销或报告更正后立即失效。</span>
        </form>"""

    # 本人额外看到跨年度趋势 + 医生备注并置
    trend_html = ""
    if me["id"] == report["patient_id"]:
        trend = ctx.reports.trend(me["id"])
        notes = _latest_notes(conn, me["id"])
        blocks = []
        ordered = [m for m in metrics if m["flag"] != "T"]
        for m in ordered:
            t = trend.get(m["code"])
            svg = trend_svg(t["points"], ref_low=t["ref_low"],
                            ref_high=t["ref_high"], unit=t["unit"] or "") if t else ""
            note = notes.get(m["code"])
            note_html = (f'<div class="doctor-note">{esc(note)}</div>' if note else
                         '<div class="doctor-note empty">该指标暂无医生备注，'
                         '可在预约时勾选讨论</div>')
            blocks.append(f"""
            <div class="metric-block">
              <div class="mb-head"><span class="name">{esc(m['name'])}</span>
                <span class="muted small">单位 {esc(m['unit'] or '—')} ·
                  参考 {esc(_ref_range(m))}</span></div>
              <div class="mb-body"><div>{svg}</div><div>{note_html}</div></div>
            </div>""")
        trend_html = f"""
        <h3 style="margin-top:18px">{fa('note')} 指标趋势与医生备注</h3>
        <p class="muted small">左：历年趋势（色带为参考范围，琥珀点为超出范围）；
          右：医生在解读时给出的备注。</p>
        {''.join(blocks)}"""

    body = f"""
    <section class="card">
      <div class="row spread">
        <h2 style="margin:0">{esc(report['title'])}</h2>
        <span class="badge {'teal' if is_current else 'gray'}">
          {'当前版本' if is_current else '历史留档版本'}</span>
      </div>
      <p class="muted small">报告编号 {esc(report['code'])} · 体检日期 {esc(report['exam_date'])}
        · 版本 {' '.join(vtabs)}</p>
      {cor_banner}
      <table class="metrics">
        <thead><tr><th>指标</th><th>本次结果</th><th>参考范围</th><th>判断</th></tr></thead>
        <tbody>{''.join(trs)}</tbody></table>
      <div class="btnrow">{dl}
        <a class="btn ghost" href="/reports">返回报告列表</a></div>
    </section>
    {f'<section class="card">{trend_html}</section>' if trend_html else ''}"""
    return page(title=report["title"], body=body, me=me, active="reports",
                flash=req.flash())


def _ref_range(m):
    lo, hi = m["ref_low"], m["ref_high"]
    if lo is not None and hi is not None:
        return f"{lo:g} – {hi:g}"
    if lo is not None:
        return f"≥ {lo:g}"
    if hi is not None:
        return f"≤ {hi:g}"
    return m["ref_text"] or "—"


def _latest_notes(conn, patient_id):
    """每个指标取最近一次非作废解读中的医生备注（已签字优先）。"""
    rows = conn.execute(
        """SELECT n.metric_code, n.content,
                  CASE s.status WHEN 'signed' THEN 1 ELSE 0 END AS signed_rank,
                  s.created_at
           FROM interpretation_notes n
           JOIN interpretation_sessions s ON s.id=n.session_id
           WHERE s.patient_id=? AND s.is_current=1 AND s.status!='voided'
           ORDER BY signed_rank DESC, s.created_at DESC""",
        (patient_id,)).fetchall()
    out = {}
    for r in rows:
        if r["metric_code"] not in out:
            out[r["metric_code"]] = r["content"]
    return out


def request_download(req):
    me, ctx = req.me, req.ctx
    rid = int(req.form["report_id"])
    vid = int(req.form.get("version_id") or 0)
    report = ctx.reports.get_report(rid)
    version = (ctx.conn.execute("SELECT * FROM report_versions WHERE id=?", (vid,)).fetchone()
               if vid else ctx.reports.current_version(rid))
    token = ctx.access.issue_download_link(report, me, version, ip=req.ip)
    # 直接跳转到令牌地址（校验通过即返回 PDF；失效会得到 410）
    return ("redirect", "/d/" + token)


def download(req, token):
    ctx = req.me and req.ctx
    try:
        link, version, report, viewer = req.ctx.access.validate_download(
            token, ip=req.ip)
    except LinkInvalid as e:
        return ("error", 410, e.message, req.me)
    path = req.ctx.reports.pdf_path(version)
    if not path.exists():
        return ("error", 404, "PDF 文件缺失（演示本地目录）", req.me)
    req.ctx.access.mark_downloaded(link, ip=req.ip)
    fname = urllib.parse.quote(path.name)
    return ("pdf", path.read_bytes(), fname, req.q.get("dl") == "1")


# ---------------- 解读记录 ----------------
def interpretations(req):
    me, ctx = req.me, req.ctx
    rows = ctx.interpretation.for_patient(me["id"])
    items = []
    for s in rows:
        ver = ctx.conn.execute("SELECT * FROM report_versions WHERE id=?",
                               (s["report_version_id"],)).fetchone()
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(s['report_title'])}</b>
            {status_badge(s['status'])}
            <span class="badge gray">依据 v{ver['version_no'] if ver else '?'}</span><br>
            <span class="small muted">{esc(s['doctor_name'])}
              {esc(s['doctor_title'] or '')} · 预约 {esc(s['appt_code'])} ·
              {s['created_at'][:10]}</span></div>
          <a class="btn ghost sm" href="/interpretation?id={s['id']}">查看</a></div>""")
    body = f"""
    <section class="card"><h2>{fa('note')} 解读记录（会诊记录）</h2>
    <p class="muted small">会诊记录须经医生电子签字后方为定稿；未签字记录不作为正式结论。
      记录固定引用解读当时的报告版本，事后报告更正不改变该引用。</p>
    {''.join(items) or '<p class="muted small">暂无解读记录。</p>'}</section>"""
    return page(title="解读记录", body=body, me=me, active="interpretations",
                flash=req.flash())


def interpretation_detail(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    sid = int(req.q["id"])
    s = conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                     (sid,)).fetchone()
    if s is None:
        return ("error", 404, "解读记录不存在", me)
    # 本人、或该患者授权的医生/家属可见
    if me["id"] != s["patient_id"]:
        report = ctx.reports.get_report(
            conn.execute("SELECT report_id FROM appointments WHERE id=?",
                         (s["appointment_id"],)).fetchone()["report_id"])
        ctx.access.require_report(report, me, "view", ip=req.ip)
    ver = conn.execute("SELECT * FROM report_versions WHERE id=?",
                       (s["report_version_id"],)).fetchone()
    report = conn.execute("SELECT * FROM reports r WHERE r.id=?",
                          (conn.execute("SELECT report_id FROM appointments WHERE id=?",
                                        (s["appointment_id"],)).fetchone()["report_id"],)
                          ).fetchone()
    is_historic = ver["id"] != report["current_version_id"]

    notes = ctx.interpretation.notes_map(sid)
    trend = ctx.reports.trend(s["patient_id"])
    # 仅展示该版本包含的指标
    metrics = ctx.reports.metrics(ver["id"])
    blocks = []
    for m in metrics:
        if m["flag"] == "T":
            continue
        t = trend.get(m["code"])
        svg = trend_svg(t["points"], ref_low=t["ref_low"], ref_high=t["ref_high"],
                        unit=t["unit"] or "") if t else ""
        note = notes.get(m["code"])
        note_html = (f'<div class="doctor-note">{esc(note)}</div>' if note else
                     '<div class="doctor-note empty">本次未就该指标写备注</div>')
        blocks.append(f"""
        <div class="metric-block">
          <div class="mb-head"><span class="name">{esc(m['name'])}</span>
            <span class="muted small">本次 %g {esc(m['unit'] or '')} · 参考 {esc(_ref_range(m))}</span></div>
          <div class="mb-body"><div>{svg}</div><div>{note_html}</div></div>
        </div>""" % (m["value"],))

    unsigned = ""
    if s["status"] == "draft":
        unsigned = f"""
        <div class="alert warn"><span class="ic">{fa('warn')}</span><div>
        <b>这份会诊记录尚未签字（待签字）</b><br>
        原因：{esc(s['unsigned_reason'] or '待医生签字确认')}。
        在医生补签前，以下内容不作为正式医疗结论。</div></div>"""
    historic = ""
    if is_historic:
        cur = ctx.reports.current_version(report["id"])
        historic = f"""
        <div class="alert info"><span class="ic">{fa('info')}</span><div>
        本记录解读时依据的是 <b>v{ver['version_no']}</b>；该报告此后已发布
        <b>v{cur['version_no']} 更正件</b>。按规定保留当时引用，解读内容不随之改写。</div></div>"""

    body = f"""
    <section class="card">
      <div class="row spread"><h2 style="margin:0">{esc(report['title'])} · 医生解读</h2>
        {status_badge(s['status'])}</div>
      <p class="muted small">医生：
        {esc(conn.execute('SELECT name FROM users WHERE id=?',(s['doctor_id'],)).fetchone()['name'])}
        · 记录 {esc(s['code'])} · 依据版本 v{ver['version_no']}
        {'（更正件）' if ver['kind']=='correction' else '（原件）'}</p>
      {unsigned}{historic}
      <h3>总体意见</h3>
      <div>{esc(s['summary'] or '（待医生填写）')}</div>
      <h3>建议</h3>
      <div>{esc(s['advice'] or '（待医生补全后签字）')}</div>
      {f'<p class="small muted">签字时间 {esc(s["signed_at"])} · 签字医师 {esc(conn.execute("SELECT name FROM users WHERE id=?",(s["signer_id"],)).fetchone()["name"] if s["signer_id"] else "")}</p>'
        if s['status']=='signed' else ''}
    </section>
    <section class="card"><h2>{fa('note')} 指标趋势与医生备注（并置）</h2>
      {''.join(blocks)}</section>
    <div class="btnrow"><a class="btn ghost" href="javascript:history.back()">返回</a></div>"""
    return page(title="解读详情", body=body, me=me, active="interpretations")


# ---------------- 授权管理 ----------------
def consents(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    rows = ctx.consents.list_for_patient(me["id"])
    doctors = conn.execute("SELECT * FROM users WHERE role='doctor'").fetchall()
    family = conn.execute(
        """SELECT u.*, fl.relation FROM users u JOIN family_links fl
           ON fl.family_id=u.id WHERE fl.patient_id=? ORDER BY u.id""",
        (me["id"],)).fetchall()

    cards = []
    for c in rows:
        who = f'{c["grantee_name"]}（{c["relation"] or ("医生" if c["grantee_role"]=="doctor" else "家属")}）'
        expiry = ('到期 ' + c["expires_at"][:16].replace("T", " ")) if c["expires_at"] else "当次有效"
        scope = "查看与下载" if "download" in (c["scope"] or "") else "仅查看"
        actions = ""
        if c["status"] in ("active", "disputed"):
            actions += f"""
            <form class="inline-form" method="post" action="/consents/revoke">
              <input type="hidden" name="consent_id" value="{c['id']}">
              <button class="btn danger sm">撤销授权</button></form>"""
        dispute_html = ""
        if c["open_dispute"]:
            d = c["open_dispute"]
            dispute_html = f"""
            <div class="alert warn" style="margin:8px 0 0">
              <span class="ic">{fa('warn')}</span><div>
              <b>授权争议（{esc(d['created_at'][:10])}）</b><br>{esc(d['reason'])}
              <div class="btnrow">
                <form method="post" action="/consents/resolve">
                  <input type="hidden" name="consent_id" value="{c['id']}">
                  <input type="hidden" name="decision" value="confirm">
                  <button class="btn sm">确认授权（恢复）</button></form>
                <form method="post" action="/consents/resolve">
                  <input type="hidden" name="consent_id" value="{c['id']}">
                  <input type="hidden" name="decision" value="revoke">
                  <button class="btn danger sm">否认并撤销</button></form>
              </div></div></div>"""
        cards.append(f"""
        <div class="list-item">
          <div class="row spread"><div><b>{esc(who)}</b> {status_badge(c['status'])}</div>
            <span class="small muted">{scope} · {esc(expiry)}</span></div>
          {f'<p class="small muted" style="margin:6px 0 0">备注：{esc(c["note"])}</p>' if c['note'] else ''}
          {dispute_html}
          <div class="btnrow">{actions}</div>
        </div>""")

    grantee_opts = "".join(
        [f'<optgroup label="医生">' +
         "".join(f'<option value="doctor:{d["id"]}">{esc(d["name"])}（{esc(d["title"])}）</option>'
                 for d in doctors) + "</optgroup>"]
        + ([f'<optgroup label="家属">' +
            "".join(f'<option value="family:{f["id"]}">{esc(f["name"])}（{esc(f["relation"])}）</option>'
                    for f in family) + "</optgroup>"] if family else []))

    body = f"""
    <section class="card">
      <h2>{fa('shield')} 授权管理</h2>
      <p class="muted small">仅可向解读医生或已登记家属授权；可设到期时间，到期自动失效；
        可随时撤销，撤销后对方既有下载链接立即失效。家属若对授权有异议可发起争议，
        争议期间授权冻结，由您终局确认或撤销。</p>
      {''.join(cards) or '<p class="muted small">暂无授权记录。</p>'}
    </section>
    <section class="card">
      <h2>新增授权</h2>
      <form method="post" action="/consents/grant">
        <label class="fld"><span>授权对象</span>
          <select name="grantee" required>{grantee_opts}</select></label>
        <div class="row">
          <label class="fld" style="flex:1;min-width:140px"><span>权限范围</span>
            <select name="scope"><option value="view,download">查看与下载</option>
            <option value="view">仅查看</option></select></label>
          <label class="fld" style="flex:1;min-width:140px"><span>到期时间（可选）</span>
            <input type="datetime-local" name="expires_at"></label>
        </div>
        <label class="fld"><span>或有效天数（填写后优先，最长 {config.AUTH_MAX_DAYS} 天）</span>
          <input type="text" name="expires_days" placeholder="例如 30，留空表示当次有效"></label>
        <label class="fld"><span>备注</span><input type="text" name="note" placeholder="如：陪同复诊"></label>
        <button class="btn">授予访问权</button>
      </form>
    </section>"""
    return page(title="授权管理", body=body, me=me, active="consents",
                flash=req.flash())


def consent_grant(req):
    me, ctx = req.me, req.ctx
    kind, gid = req.form["grantee"].split(":")
    kwargs = dict(scope=req.form.get("scope", "view,download"),
                  note=req.form.get("note", ""))
    days = req.form.get("expires_days", "").strip()
    expires_at = req.form.get("expires_at", "").strip()
    if days:
        kwargs["expires_days"] = int(days)
    elif expires_at:
        kwargs["expires_at"] = expires_at.replace("T", " ") + ":00" if len(expires_at) == 16 else expires_at
    ctx.consents.grant(patient_id=me["id"], grantee_id=int(gid), **kwargs)
    return ("redirect", with_flash("/consents", "ok", flash="授权已生效，对方已收到模拟邮件通知"))


def consent_revoke(req):
    me, ctx = req.me, req.ctx
    ctx.consents.revoke(int(req.form["consent_id"]), patient_id=me["id"])
    return ("redirect", with_flash("/consents", "ok",
            flash="授权已撤销，对方的访问与下载链接立即失效"))


def consent_dispute(req):
    """患者本人也可对自己授予的授权发起争议（如怀疑账号异常）。"""
    me, ctx = req.me, req.ctx
    cid = int(req.form["consent_id"])
    reason = req.form.get("reason", "患者对该授权存疑，要求暂停访问")
    ctx.consents.raise_dispute(cid, raised_by=me["id"], reason=reason)
    back = req.q.get("back") or ("/consents" if me["role"] == "patient" else "/family")
    return ("redirect", with_flash(back, "ok", flash="已发起争议，授权在您确认前保持冻结"))


def consent_resolve(req):
    me, ctx = req.me, req.ctx
    ctx.consents.resolve_dispute(int(req.form["consent_id"]), patient_id=me["id"],
                                 decision=req.form["decision"])
    return ("redirect", with_flash("/consents", "ok",
            flash="争议已处理" if req.form['decision']=='confirm' else "已否认并撤销授权"))


# ---------------- 模拟邮箱 ----------------
def mailbox(req):
    me, ctx = req.me, req.ctx
    mails = ctx.mailer.inbox(me["id"])
    items = []
    for m in mails:
        unread = '<span class="badge teal">未读</span>' if not m["read_at"] else ""
        items.append(f"""
        <div class="mail">
          <div class="mh"><b>{esc(m['subject'])}</b>
            <span>{m['ts'][5:16].replace('T',' ')} {unread}</span></div>
          <div class="mb">{esc(m['body'])}</div>
        </div>""")
    body = f"""
    <section class="card"><h2>{fa('mail')} 模拟邮箱</h2>
      <p class="muted small">系统不发送真实邮件，所有通知由模拟适配器写入此邮箱。</p>
      {''.join(items) or '<p class="muted small">暂无邮件。</p>'}</section>"""
    return page(title="模拟邮箱", body=body, me=me, active="mail",
                flash=req.flash())


def mail_read(req):
    req.ctx.mailer.mark_read(int(req.form["id"]), req.me["id"])
    return ("redirect", "/mail")


def logs(req):
    """本人相关的访问审计：谁在何时查看/下载了我的报告，或被拒绝。"""
    me, conn = req.me, req.conn
    rows = conn.execute(
        """SELECT l.*, u.name AS actor_name, u.role AS actor_role_name
           FROM access_logs l LEFT JOIN users u ON u.id=l.actor_id
           WHERE l.resource_type='report' AND l.resource_id IN
                 (SELECT CAST(id AS TEXT) FROM reports WHERE patient_id=?)
           ORDER BY l.id DESC LIMIT(60)""", (me["id"],)).fetchall()
    reason_cn = {
        "self": "本人", "ok": "授权访问", "no_consent": "无授权被拒",
        "consent_revoked": "授权已撤销", "consent_expired": "授权已到期",
        "consent_disputed": "争议冻结中", "scope_no_download": "无下载权限",
        "version_superseded": "旧版本链接失效", "expired": "链接过期",
        "report.correct": "体检中心更正",
    }
    role_cn = {"doctor": "医生", "family": "家属", "staff": "体检中心",
               "patient": "本人"}
    items = []
    for r in rows:
        res = ('<span class="badge ok">允许</span>' if r["result"] == "allow"
               else '<span class="badge warn">拒绝</span>')
        action = {"report.view": "查看报告", "report.download": "请求下载",
                  "report.view.deny": "查看报告", "report.download.deny": "请求下载",
                  "download.ok": "下载 PDF", "download.deny": "下载被拒",
                  "download.link_issue": "签发下载链接",
                  "report.correct": "签发更正报告"}.get(r["action"], r["action"])
        role_label = role_cn.get(r["actor_role_name"] or r["actor_role"], "")
        who = (r["actor_name"] or "系统") + (f"（{role_label}）" if role_label else "")
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(action)}</b> {res}<br>
          <span class="small muted">{esc(who)} · 资源 {esc(r['resource_type'])}#{esc(r['resource_id'])} ·
            {esc(reason_cn.get(r['reason'], r['reason'] or ''))}</span></div>
          <span class="small muted">{r['ts'][5:16].replace('T',' ')}</span></div>""")
    body = f"""
    <section class="card"><h2>{fa('shield')} 访问日志</h2>
      <p class="muted small">系统记录与您报告相关的每一次查看、下载与拒绝，便于追溯。</p>
      {''.join(items) or '<p class="muted small">暂无访问记录。</p>'}</section>"""
    return page(title="访问日志", body=body, me=me, active="consents")
