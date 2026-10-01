"""报告 PDF 渲染：把某个 report_version 渲染成本地 PDF。

- 异常指标行显示数值与参考范围，并加克制的青色/琥珀徽记；
- 更正件在首页显著标注“更正报告”、更正原因与替代版本；
- PDF 文件内容对版本不可变（同一版本重新渲染结果一致）。
"""
from __future__ import annotations

import hashlib
import sqlite3

from . import config, timeutil
from .pdfkit import PDFDoc

FLAG_CN = {"N": "正常", "H": "↑ 偏高", "L": "↓ 偏低", "T": "—"}


def _metric_value(m: sqlite3.Row) -> str:
    if m["flag"] == "T":
        return m["value_text"] or "—"
    v = m["value"]
    return ("%g" % v) if v is not None else "—"


def _metric_ref(m: sqlite3.Row) -> str:
    if m["ref_text"]:
        return m["ref_text"]
    lo, hi = m["ref_low"], m["ref_high"]
    if lo is not None and hi is not None:
        return f"{lo:g} – {hi:g}"
    if lo is not None:
        return f"≥ {lo:g}"
    if hi is not None:
        return f"≤ {hi:g}"
    return "—"


def _metric_unit(m: sqlite3.Row) -> str:
    return m["unit"] or ""


def render_report_pdf(conn: sqlite3.Connection, version_id: int,
                      out_path) -> str:
    v = conn.execute(
        """SELECT v.*, r.title, r.code AS report_code, r.exam_date,
                  u.name AS patient_name, u.code AS patient_code
           FROM report_versions v
           JOIN reports r ON r.id=v.report_id
           JOIN users u ON u.id=r.patient_id
           WHERE v.id=?""", (version_id,)).fetchone()
    metrics = conn.execute(
        "SELECT * FROM metrics WHERE report_version_id=? ORDER BY sort_no,id",
        (version_id,)).fetchall()
    issuer = conn.execute(
        "SELECT name FROM users WHERE id=?", (v["issued_by"],)).fetchone()

    doc = PDFDoc(v["title"])
    label = f"{v['report_code']} · 第 {v['version_no']} 版"
    if v["kind"] == "correction":
        label += "（更正件）"

    doc.heading(v["title"])
    if v["kind"] == "correction":
        doc.chip("更正报告 CORRECTED", "#ffffff", config.COLORS["teal"])
        prev = conn.execute(
            "SELECT version_no FROM report_versions WHERE id=?",
            (v["supersedes_id"],)).fetchone()
        prev_no = prev["version_no"] if prev else v["version_no"] - 1
        doc.paragraph(
            f"本件为更正版本 v{v['version_no']}，替代 v{prev_no}。"
            f"更正原因：{v['correction_reason'] or '（由体检中心更正）'}。"
            f"原版本仅作留档，不再对外提供下载。",
            color=config.COLORS["teal_ink"])
        doc.hr()
    else:
        doc.chip("正式报告 ORIGINAL", config.COLORS["teal_ink"], config.COLORS["teal_soft"])

    doc.paragraph(
        f"报告编号：{v['report_code']}　　版本：v{v['version_no']}\n"
        f"受检人：{v['patient_name']}（演示身份 {v['patient_code']}）\n"
        f"体检日期：{v['exam_date']}　　签发：{issuer['name'] if issuer else '体检中心'}\n"
        f"签发时间：{timeutil.fmt_dt(timeutil.at(v['issued_at']))}"
    )
    doc.gap(4)
    doc.heading("检验指标", size=13)

    rows, fills, abn = [], [], []
    for m in metrics:
        val = _metric_value(m)
        if m["unit"]:
            val = f"{val} {m['unit']}"
        rows.append([m["name"], val, _metric_ref(m), FLAG_CN[m["flag"]]])
        if m["flag"] in ("H", "L"):
            fills.append("#f6efe3")
            abn.append(m)
        else:
            fills.append(None)
    doc.table(
        ["指标", "本次结果", "参考范围", "判断"],
        rows, col_w=[170, 120, 120, 83], row_colors=fills)
    doc.gap(2)
    doc.paragraph(
        "注：参考范围由体检实验室提供；结果判断仅对本份样本负责。"
        "带底色项目表示超出参考范围，建议结合医生解读。",
        size=8.6, color=config.COLORS["ink_2"])

    if abn:
        doc.gap(4)
        doc.heading("异常指标摘要", size=12)
        for m in abn:
            doc.paragraph(
                f"· {m['name']}：{_metric_value(m)} {_metric_unit(m)}"
                f"（参考范围 {_metric_ref(m)} {_metric_unit(m)}），{FLAG_CN[m['flag']]}。",
                size=9.6)

    doc.gap(6)
    doc.hr()
    doc.paragraph(config.DEMO_DISCLAIMER, size=8.6, color=config.COLORS["ink_2"])

    path = doc.save(out_path, label)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return sha
