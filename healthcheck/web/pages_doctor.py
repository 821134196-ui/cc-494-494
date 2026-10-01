"""医生端：号源管理、临时停诊、解读工作台与签字。"""
from __future__ import annotations

import json

from .. import timeutil
from ..errors import InterpretationError
from .layout import esc, fa, page, with_flash, status_badge
from . import pages_patient as PP

MODE_CN = PP.MODE_CN


def _back(appt_id, kind="ok", msg=""):
    """构造带回跳 + flash 的医生解读编辑地址（正确处理已有查询串）。"""
    return with_flash(f"/doctor/interpret?appt={appt_id}", kind, msg)


def board(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    slots = conn.execute(
        """SELECT * FROM slots WHERE doctor_id=?
           ORDER BY CASE WHEN datetime(start_at)<=datetime(?) THEN 1 ELSE 0 END,
                    start_at""",
        (me["id"], timeutil.iso(timeutil.now()))).fetchall()
    appts = ctx.booking.doctor_appointments(me["id"])

    slot_rows = []
    for s in slots:
        dt = timeutil.at(s["start_at"])
        past = dt <= timeutil.now()
        st = {"open": "可约/已排", "canceled": "已停诊", "blocked": "暂停"}[s["status"]]
        btn = ""
        if s["status"] == "open" and not past:
            btn = f"""
            <form class="inline-form" method="post" action="/doctor/suspend"
              onsubmit="return confirm('确认临时停诊？受影响预约将转为待改约并释放号源。')">
              <input type="hidden" name="slot_id" value="{s['id']}">
              <input type="hidden" name="reason" value="医生临时停诊">
              <button class="btn danger sm">临时停诊</button></form>"""
        slot_rows.append(f"""
        <div class="slotline">
          <div><span class="t">{dt.strftime('%m-%d %H:%M')}</span>
            <span class="badge {'teal' if s['mode']=='online' else 'gray'}">{MODE_CN[s['mode']]}</span>
            <span class="meta">已约 {s['booked_count']}/{s['capacity']} · {st}
              {('· ' + esc(s['note'])) if s['note'] else ''}</span></div>
          {btn}</div>""")

    appt_rows = []
    for a in appts:
        when = timeutil.at(a["start_at"]).strftime("%m-%d %H:%M") if a["start_at"] else "待改约"
        interpret_btn = ""
        if a["status"] in ("booked",):
            interpret_btn = (f'<a class="btn sm" href="/doctor/interpret?appt={a["id"]}">'
                             f'开始/查看解读</a>')
        elif a["status"] == "completed":
            interpret_btn = (f'<a class="btn ghost sm" href="/doctor/interpret?appt={a["id"]}">'
                             f'解读记录</a>')
        appt_rows.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(a['patient_name'])}</b> {status_badge(a['status'])}
            <span class="badge {'teal' if a['mode']=='online' else 'gray'}">{MODE_CN[a['mode']]}</span><br>
            <span class="small muted">{when} · {esc(a['code'])} ·
              讨论 {esc('、'.join(json.loads(a['indicators'] or '[]')))}</span></div>
          {interpret_btn}</div>""")

    body = f"""
    <section class="card">
      <h2>{fa('users')} 我的号源</h2>
      <p class="muted small">临时停诊后，该时段所有预约将<b>原子地释放号源并转为“待改约”</b>，
        系统逐人发送改约通知；已完成解读不受影响。</p>
      {''.join(slot_rows) or '<p class="muted small">暂无号源。</p>'}
    </section>
    <section class="card">
      <h2>{fa('calendar')} 预约列表</h2>
      {''.join(appt_rows) or '<p class="muted small">暂无预约。</p>'}
    </section>"""
    return page(title="医生工作台", body=body, me=me, active="doctor",
                flash=req.flash())


def suspend(req):
    me, ctx = req.me, req.ctx
    slot_id = int(req.form["slot_id"])
    slot = ctx.booking.get_slot(slot_id)
    if slot is None or slot["doctor_id"] != me["id"]:
        return ("error", 403, "只能停诊自己的号源", me)
    result = ctx.booking.suspend_slot(slot_id, req.form.get("reason", "医生临时停诊"),
                                      actor_id=me["id"])
    return ("redirect", with_flash("/doctor", "ok",
            f"已停诊，{len(result['affected'])} 个预约转入待改约队列"))


def list_sessions(req):
    me, ctx = req.me, req.ctx
    rows = ctx.interpretation.for_doctor(me["id"])
    items = []
    for s in rows:
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(s['patient_name'])}</b> {status_badge(s['status'])}
            <span class="badge gray">预约 {esc(s['appt_code'])}</span><br>
            <span class="small muted">{s['created_at'][:16].replace('T',' ')}</span></div>
          <a class="btn {'ghost' if s['status']=='signed' else ''} sm"
            href="/doctor/interpret?appt={s['appointment_id']}">
            {'查看' if s['status']=='signed' else '继续处理'}</a></div>""")
    body = f"""
    <section class="card"><h2>{fa('note')} 解读工作台</h2>
    <p class="muted small">会诊记录须填写总体意见与建议，并输入签名口令完成电子签字；
      未签字记录对患者显示为“待签字”，不作为正式结论。</p>
    {''.join(items) or '<p class="muted small">暂无解读记录。</p>'}</section>"""
    return page(title="解读工作台", body=body, me=me, active="interpretations",
                flash=req.flash())


def edit_session(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    appt_id = int(req.q["appt"])
    appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                        (appt_id,)).fetchone()
    if appt is None or appt["doctor_id"] != me["id"]:
        return ("error", 403, "只能处理自己的预约", me)
    if appt["status"] == "reschedule_pending":
        return ("error", 409, "该预约处于待改约，暂不能解读", me)
    sess = ctx.interpretation.start_or_get(appointment_id=appt_id, doctor_id=me["id"])

    report = conn.execute("SELECT * FROM reports WHERE id=?",
                          (appt["report_id"],)).fetchone()
    ver = conn.execute("SELECT * FROM report_versions WHERE id=?",
                       (sess["report_version_id"],)).fetchone()
    metrics = ctx.reports.metrics(ver["id"])
    selected = set(json.loads(appt["indicators"] or "[]"))
    notes = ctx.interpretation.notes_map(sess["id"])

    locked = sess["status"] == "signed"
    readonly_notice = ""
    if locked:
        readonly_notice = """
        <div class="alert info"><span class="ic">i</span><div>
        记录已签字定稿，内容只读。如需修改，请在底部作废后基于同一预约重开，
        原记录保留留痕。</div></div>"""

    note_fields = []
    for m in metrics:
        if m["flag"] == "T":
            continue
        want = m["code"] in selected
        ref = PP._ref_range(m)
        val = "—" if m["value"] is None else "%g" % m["value"]
        attrs = "disabled" if locked else ""
        note_fields.append(f"""
        <div class="metric-block">
          <div class="mb-head"><span class="name">{esc(m['name'])}
            {'<span class="badge warn">患者要求讨论</span>' if want else ''}</span>
            <span class="muted small">{val} {esc(m['unit'] or '')} · 参考 {esc(ref)}
              {'· <span class="flag-%s">%s</span>' % (m['flag'], {'H':'偏高','L':'偏低','N':'正常'}[m['flag']])}</span></div>
          <div class="mb-body" style="grid-template-columns:1fr">
            <textarea name="note__{esc(m['code'])}" rows="2"
              placeholder="就该指标写下备注（将与趋势图并置展示给患者）" {attrs}>{esc(notes.get(m['code'],''))}</textarea>
          </div></div>""")

    from ..interpretation import sign_pin
    pin = sign_pin(me["code"])
    body = f"""
    <section class="card">
      <div class="row spread"><h2 style="margin:0">
        {esc(report['title'])} · 解读记录</h2>{status_badge(sess['status'])}</div>
      <p class="muted small">患者：
        {esc(conn.execute('SELECT name FROM users WHERE id=?',(appt['patient_id'],)).fetchone()['name'])}
        · 预约 {esc(appt['code'])} · 依据版本 v{ver['version_no']}
        {'（更正件）' if ver['kind']=='correction' else '（原件）'}
        {' · 该版本已被后续更正替代' if ver['id']!=report['current_version_id'] else ''}</p>
      {readonly_notice}
      <form method="post" action="/doctor/interpret/save">
        <input type="hidden" name="session_id" value="{sess['id']}">
        <label class="fld"><span>总体意见</span>
          <textarea name="summary" rows="3" {"disabled" if locked else ""}>{esc(sess['summary'] or '')}</textarea></label>
        <label class="fld"><span>建议（生活方式 / 复查 / 复诊）</span>
          <textarea name="advice" rows="3" {"disabled" if locked else ""}>{esc(sess['advice'] or '')}</textarea></label>
        <label class="fld"><span>未签字原因（如待上级复核）</span>
          <input type="text" name="unsigned_reason"
            value="{esc(sess['unsigned_reason'] or '会诊记录待医生签字确认')}"
            {"disabled" if locked else ""}></label>
        <h3>指标备注</h3>
        {''.join(note_fields)}
        {'' if locked else '<button class="btn">保存草稿</button>'}
      </form>
    </section>
    {'' if locked else f'''
    <section class="card">
      <h2>{fa('check')} 电子签字定稿</h2>
      <p class="muted small">演示签名口令为您的工号后 4 位（当前账号提示：{esc(me['code'])} → {pin}）。
        总体意见与建议均填写后才能签字。</p>
      <form method="post" action="/doctor/interpret/sign" class="row">
        <input type="hidden" name="session_id" value="{sess['id']}">
        <label class="fld" style="margin:0;min-width:180px"><span>签名口令</span>
          <input type="password" name="pin" inputmode="numeric" placeholder="4 位数字" required></label>
        <button class="btn" style="margin-top:14px">确认签字</button>
      </form>
    </section>'''}
    {f'''
    <section class="card">
      <h2>作废并重开</h2>
      <form method="post" action="/doctor/interpret/void" class="row">
        <input type="hidden" name="session_id" value="{sess['id']}">
        <label class="fld" style="margin:0;flex:1;min-width:200px"><span>作废原因</span>
          <input type="text" name="reason" required placeholder="如：补充复查建议后重新出具"></label>
        <button class="btn danger" style="margin-top:14px">作废并重开</button>
      </form></section>''' if locked else ''}
    <div class="btnrow"><a class="btn ghost" href="/doctor/interpretations">返回工作台</a></div>"""
    return page(title="解读编辑", body=body, me=me, active="interpretations",
                flash=req.flash())


def _save_common(req):
    me, ctx = req.me, req.ctx
    sid = int(req.form["session_id"])
    notes = {}
    for k, v in req.form.items():
        if k.startswith("note__") and v.strip():
            notes[k[len("note__"):]] = v
    return sid, notes


def save_session(req):
    me, ctx = req.me, req.ctx
    sid, notes = _save_common(req)
    ctx.interpretation.save_draft(
        session_id=sid, doctor_id=me["id"], summary=req.form.get("summary", ""),
        advice=req.form.get("advice", ""), notes=notes,
        unsigned_reason=req.form.get("unsigned_reason"))
    appt = ctx.conn.execute(
        "SELECT appointment_id FROM interpretation_sessions WHERE id=?",
        (sid,)).fetchone()
    return ("redirect", _back(appt["appointment_id"], "ok", "草稿已保存，尚待签字"))


def sign_session(req):
    me, ctx = req.me, req.ctx
    sid = int(req.form["session_id"])
    try:
        ctx.interpretation.sign(session_id=sid, doctor_id=me["id"],
                                pin=req.form.get("pin", ""))
    except InterpretationError as e:
        appt = ctx.conn.execute(
            "SELECT appointment_id FROM interpretation_sessions WHERE id=?",
            (sid,)).fetchone()
        return ("redirect", _back(appt["appointment_id"], "err", e.message))
    appt = ctx.conn.execute(
        "SELECT appointment_id FROM interpretation_sessions WHERE id=?",
        (sid,)).fetchone()
    return ("redirect", _back(appt["appointment_id"], "ok",
                              "已签字定稿，患者可查看正式结论"))


def void_session(req):
    me, ctx = req.me, req.ctx
    sid = int(req.form["session_id"])
    new = ctx.interpretation.void_and_replace(
        session_id=sid, doctor_id=me["id"], reason=req.form.get("reason", ""))
    return ("redirect", _back(new["appointment_id"], "ok",
                              "原记录已作废留痕，已为同一预约新建草稿"))
