"""家属端：仅能看到患者明确授权范围内的报告与解读。"""
from __future__ import annotations

from .. import timeutil
from .layout import esc, fa, page, status_badge


def home(req):
    me, ctx, conn = req.me, req.ctx, req.conn
    # 该家属持有的全部有效/历史授权
    all_consents = conn.execute(
        """SELECT c.*, p.name AS patient_name, fl.relation
           FROM consents c JOIN users p ON p.id=c.patient_id
           LEFT JOIN family_links fl ON fl.patient_id=c.patient_id AND fl.family_id=c.grantee_id
           WHERE c.grantee_id=me_param""".replace("me_param", "?"),
        (me["id"],)).fetchall()

    active = [c for c in all_consents if c["status"] == "active"]
    report_cards = []
    for c in active:
        reports = ctx.reports.patient_reports(c["patient_id"])
        for r in reports:
            report_cards.append((c, r))

    # 该家属在授权范围内可见的解读记录（非作废）
    sessions = []
    for c in active:
        rows = conn.execute(
            """SELECT s.*, r.title AS report_title
               FROM interpretation_sessions s
               JOIN appointments a ON a.id=s.appointment_id
               JOIN reports r ON r.id=a.report_id
               WHERE s.patient_id=? AND s.is_current=1 AND s.status!='voided'
               ORDER BY s.created_at DESC""", (c["patient_id"],)).fetchall()
        for s in rows:
            sessions.append((c, s))

    items = []
    for c, r in report_cards:
        scope_cn = "可查看与下载" if "download" in (c["scope"] or "") else "仅可查看"
        items.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(r['title'])}</b>
            <span class="badge teal">{esc(c['patient_name'])}（{esc(c['relation'] or '家属')}）</span>
            {('<span class="badge warn">含更正件</span>' ) if r['kind']=='correction' else ''}
            <br><span class="small muted">{esc(r['exam_date'])} · v{r['version_no']} · {scope_cn}</span></div>
          <div class="btnrow" style="margin:0">
            <a class="btn ghost sm" href="/report?id={r['id']}">查看报告</a></div></div>""")

    history = []
    for c in all_consents:
        expiry = ('到期 ' + c["expires_at"][:16].replace("T", " ")) if c["expires_at"] else "当次有效"
        history.append(f"""
        <div class="list-item">
          <div class="row spread"><b>{esc(c['patient_name'])}（{esc(c['relation'] or '家属')}）</b>
            {status_badge(c['status'])}</div>
          <div class="small muted">{('查看与下载' if 'download' in (c['scope'] or '') else '仅查看')} · {esc(expiry)}</div>
        </div>""")

    disputes = [c for c in all_consents if c["status"] == "disputed"]
    dispute_banner = ""
    if disputes:
        dispute_banner = """
        <div class="alert warn"><span class="ic">!</span><div>
        <b>有授权处于争议冻结中</b>。需由患者本人确认恢复或撤销；在此之前您无法访问报告，
        已有下载链接均已失效。若您认为授权并非患者本人意愿，可再次说明情况，等待患者处理。</div></div>"""

    body = f"""
    {dispute_banner}
    <section class="card">
      <h2>{fa('shield')} 被授权报告</h2>
      <p class="muted small">报告访问权完全来自患者的明确授权。授权撤销、到期或报告更正后，
        您手里的旧下载链接立即失效。</p>
      {''.join(items) or '<p class="muted small">当前没有可查看的报告（无有效授权）。</p>'}
    </section>
    <section class="card">
      <h2>解读记录</h2>
      {''.join(_session_lines(sessions)) or '<p class="muted small">暂无解读记录。</p>'}
    </section>
    <section class="card">
      <h2>我的授权状态</h2>
      {''.join(history) or '<p class="muted small">暂无授权记录。</p>'}
    </section>"""
    return page(title="被授权报告", body=body, me=me, active="family",
                flash=req.flash())


def _session_lines(sessions):
    out = []
    for c, s in sessions:
        out.append(f"""
        <div class="list-item row spread">
          <div><b>{esc(s['report_title'])}</b>
            <span class="badge teal">{esc(c['patient_name'])}（{esc(c['relation'] or '家属')}）</span>
            {status_badge(s['status'])}<br>
            <span class="small muted">记录 {esc(s['code'])} · {s['created_at'][:10]}
              {'· 待签字，不作正式结论' if s['status']=='draft' else ''}</span></div>
          <a class="btn ghost sm" href="/interpretation?id={s['id']}">查看解读</a></div>""")
    return out
