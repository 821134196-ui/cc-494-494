"""体检中心端：报告版本管理（签发更正件）与家属授权争议总览。"""
from __future__ import annotations

from .. import timeutil
from .layout import esc, fa, page, qs, status_badge, with_flash


def home(req):
    conn = req.conn
    reports = conn.execute(
        """SELECT r.*, p.name AS patient_name, v.version_no, v.kind
           FROM reports r
           JOIN users p ON p.id=r.patient_id
           JOIN report_versions v ON v.id=r.current_version_id
           ORDER BY r.exam_date DESC""").fetchall()
    items = []
    for r in reports:
        cor = ('<span class="badge warn">已更正 v%d</span>' % r["version_no"]
               if r["kind"] == "correction" else '<span class="badge teal">原件 v1</span>')
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(r['code'])}</b> {cor}<br>
          <span class="small muted">{esc(r['patient_name'])} · {esc(r['title'])} ·
            体检 {esc(r['exam_date'])}</span></div>
          <a class="btn ghost sm" href="/staff/report?id={r['id']}">版本管理 / 更正</a></div>""")
    n_disp = conn.execute(
        "SELECT COUNT(*) c FROM consent_disputes WHERE status='open'").fetchone()["c"]
    body = f"""
    <section class="card">
      <h2>{fa('doc')} 报告与版本
        <a class="btn ghost sm" style="margin-left:auto" href="/staff/disputes">
          授权争议 {n_disp}</a></h2>
      <p class="muted small">更正报告会生成不可变新版本、渲染新 PDF，旧版本仅留档且下载链接立即失效；
        既往预约与会诊记录继续引用其当时版本。工作人员账号不能浏览个人报告内容，仅做版本管理。</p>
      {''.join(items)}</section>"""
    return page(title="报告管理", body=body, me=req.me, active="staff",
                flash=req.flash())


def report_admin(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    rid = int(req.q["id"])
    report = ctx.reports.get_report(rid)
    if report is None:
        return ("error", 404, "报告不存在", me)
    patient = conn.execute("SELECT * FROM users WHERE id=?",
                           (report["patient_id"],)).fetchone()
    versions = ctx.reports.versions(rid)
    current = ctx.reports.current_version(rid)
    metrics = ctx.reports.metrics(current["id"])

    vrows = []
    for v in versions:
        vrows.append(f"""
        <div class="list-item row spread">
          <div><b>v{v['version_no']}</b>
            {status_badge('signed') if v['kind']=='original' else status_badge('draft')}
            <span class="badge {'teal' if v['id']==report['current_version_id'] else 'gray'}">
              {'当前版本' if v['id']==report['current_version_id'] else '已留档'}</span>
            <br><span class="small muted">{v['issued_at'][:16].replace('T',' ')} ·
              PDF {esc(v['pdf_path'])} · sha256 {esc((v['pdf_sha256'] or '')[:12])}…</span>
            {f'<br><span class="small" style="color:var(--warn)">更正原因：{esc(v["correction_reason"])}</span>'
              if v['correction_reason'] else ''}
          </div></div>""")

    # 更正表单：预填当前指标，可修改数值
    rows = []
    for m in metrics:
        val = m["value_text"] if m["flag"] == "T" else (
            "%g" % m["value"] if m["value"] is not None else "")
        rows.append(f"""
        <tr>
          <td class="small">{esc(m['name'])}<br><span class="muted">{esc(m['code'])}</span></td>
          <td><input type="hidden" name="code" value="{esc(m['code'])}">
              <input type="hidden" name="name" value="{esc(m['name'])}">
              <input type="hidden" name="unit" value="{esc(m['unit'] or '')}">
              <input type="hidden" name="ref_low" value="{'' if m['ref_low'] is None else m['ref_low']}">
              <input type="hidden" name="ref_high" value="{'' if m['ref_high'] is None else m['ref_high']}">
              <input type="hidden" name="value_text" value="{esc(m['value_text'] or '')}">
              <input type="text" name="value" value="{esc(val)}" style="width:90px"></td>
        </tr>""")

    body = f"""
    <section class="card">
      <h2>{esc(report['code'])} · 版本历史</h2>
      <p class="muted small">受检人 {esc(patient['name'])}（{esc(patient['code'])}）·
        {esc(report['title'])} · 体检 {esc(report['exam_date'])}</p>
      {''.join(vrows)}
    </section>
    <section class="card">
      <h2>签发更正报告</h2>
      <p class="muted small">用于标本串号、录入错误等更正。提交后生成 v{current['version_no']+1}，
        患者会收到模拟邮件，旧版本全部下载链接立即失效。</p>
      <form method="post" action="/staff/correct">
        <input type="hidden" name="report_id" value="{rid}">
        <label class="fld"><span>更正原因（将印在更正件首页）</span>
          <textarea name="reason" rows="2" required placeholder="如：标本编号串号，LDL-C 复核后更正为…"></textarea></label>
        <table class="metrics"><thead><tr><th style="width:60%">指标</th><th>更正后结果</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table>
        <div class="btnrow"><button class="btn">生成更正版本与 PDF</button>
          <a class="btn ghost" href="/staff">返回</a></div>
      </form>
    </section>"""
    return page(title="报告版本管理", body=body, me=me, active="staff")


def correct(req):
    me, ctx = req.me, req.ctx
    rid = int(req.form["report_id"])
    reason = req.form.get("reason", "")
    codes = req.form_multi.get("code", [])
    names = req.form_multi.get("name", [])
    units = req.form_multi.get("unit", [])
    los = req.form_multi.get("ref_low", [])
    his = req.form_multi.get("ref_high", [])
    texts = req.form_multi.get("value_text", [])
    vals = req.form_multi.get("value", [])

    metrics = []
    for i, code in enumerate(codes):
        text = texts[i].strip()
        lo = float(los[i]) if los[i].strip() else None
        hi = float(his[i]) if his[i].strip() else None
        if text:
            metrics.append(dict(code=code, name=names[i], value=None,
                                value_text=text, unit=units[i] or None,
                                ref_low=lo, ref_high=hi, ref_text=None, flag="T"))
        else:
            v = float(vals[i])
            flag = "N"
            if lo is not None and v < lo:
                flag = "L"
            elif hi is not None and v > hi:
                flag = "H"
            metrics.append(dict(code=code, name=names[i], value=v, value_text=None,
                                unit=units[i] or None, ref_low=lo, ref_high=hi,
                                ref_text=None, flag=flag))
    v = ctx.reports.issue_correction(report_id=rid, actor_id=me["id"],
                                    reason=reason, metrics=metrics)
    return ("redirect", with_flash(f"/staff/report?id={rid}", "ok",
            f"已签发更正件 v{v['version_no']}，旧链接已全部失效"))


def disputes(req):
    me, ctx = req.me, req.ctx
    open_d = ctx.consents.list_disputes_open()
    items = []
    for d in open_d:
        items.append(f"""
        <div class="list-item">
          <div class="row spread"><b>{esc(d['patient_name'])} → {esc(d['grantee_name'])}</b>
            <span class="small muted">{d['created_at'][:16].replace('T',' ')}</span></div>
          <p class="small" style="margin:6px 0">争议由 {esc(d['raiser_name'])} 发起：{esc(d['reason'])}</p>
          <p class="muted small">该争议只能由患者本人在“授权管理”中终局处理（确认恢复或撤销）；
            工作人员无权代替患者决定。</p>
        </div>""")
    resolved = req.conn.execute(
        """SELECT d.*, p.name AS pn, g.name AS gn FROM consent_disputes d
           JOIN users p ON p.id=(SELECT patient_id FROM consents WHERE id=d.consent_id)
           JOIN users g ON g.id=(SELECT grantee_id FROM consents WHERE id=d.consent_id)
           WHERE d.status='resolved' ORDER BY d.resolved_at DESC LIMIT 10""").fetchall()
    ritems = "".join(
        f'<div class="list-item small"><b>{esc(r["pn"])} → {esc(r["gn"])}</b> '
        f'{status_badge("active" if r["resolution"]=="patient_confirmed" else "revoked")} '
        f'<span class="muted">{ "患者确认恢复" if r["resolution"]=="patient_confirmed" else "患者撤销授权" } · '
        f'{esc(r["resolved_at"][:10])}</span></div>' for r in resolved)
    body = f"""
    <section class="card"><h2>{fa('shield')} 家属授权争议</h2>
      {''.join(items) or '<p class="muted small">当前没有待处理争议。</p>'}</section>
    <section class="card"><h2>近期处理结果</h2>{ritems or '<p class="muted small">暂无。</p>'}</section>"""
    return page(title="授权争议", body=body, me=me, active="disputes")
