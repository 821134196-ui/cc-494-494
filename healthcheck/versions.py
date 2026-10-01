"""报告版本服务：签发原件与更正件。

确定规则（报告更正）：
- 更正产生新的 report_versions 行（version_no 递增、kind=correction、
  supersedes 指向前版本），并渲染不可变 PDF、更新 reports.current_version_id；
- 旧版本仅作留档：不再对外提供下载，其既有下载链接立即作废
  （validate_download 返回 410 version_superseded）；
- 既往预约与解读记录通过 report_version_id 固化引用其当时版本，
  报告更正不改变、不抹除这些引用——解读始终对得上当时所看的版本；
- 患者在“版本历史”中可查看留档旧版本（明确标注“已被替代”），仅本人可见。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, reports, timeutil
from .access import AccessService
from .errors import PermissionDenied, ReportVersionError

CORRECTION = "correction"
ORIGINAL = "original"


class ReportService:
    def __init__(self, conn: sqlite3.Connection, access: AccessService, mailer=None):
        self.conn = conn
        self.access = access
        self.mailer = mailer

    def get_report(self, report_id: int):
        return self.conn.execute("SELECT * FROM reports WHERE id=?",
                                 (report_id,)).fetchone()

    def current_version(self, report_id: int):
        return self.conn.execute(
            """SELECT v.* FROM report_versions v JOIN reports r ON r.current_version_id=v.id
               WHERE r.id=?""", (report_id,)).fetchone()

    def versions(self, report_id: int):
        return self.conn.execute(
            "SELECT * FROM report_versions WHERE report_id=? ORDER BY version_no DESC",
            (report_id,)).fetchall()

    def metrics(self, version_id: int):
        return self.conn.execute(
            "SELECT * FROM metrics WHERE report_version_id=? ORDER BY sort_no,id",
            (version_id,)).fetchall()

    def patient_reports(self, patient_id: int):
        return self.conn.execute(
            """SELECT r.*, v.version_no, v.kind, v.issued_at
               FROM reports r JOIN report_versions v ON v.id=r.current_version_id
               WHERE r.patient_id=? ORDER BY r.exam_date DESC""",
            (patient_id,)).fetchall()

    def issue_correction(self, *, report_id: int, actor_id: int,
                         reason: str, metrics: list[dict]) -> sqlite3.Row:
        """体检中心工作人员签发更正件。metrics 为完整指标集合。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            actor = conn.execute("SELECT * FROM users WHERE id=?",
                                 (actor_id,)).fetchone()
            if actor is None or actor["role"] != "staff":
                raise PermissionDenied("仅体检中心工作人员可签发更正报告")
            report = conn.execute("SELECT * FROM reports WHERE id=?",
                                  (report_id,)).fetchone()
            if report is None:
                raise ReportVersionError("报告不存在")
            if not reason.strip():
                raise ReportVersionError("更正必须填写原因")
            if not metrics:
                raise ReportVersionError("更正件必须包含指标数据")

            old = conn.execute("SELECT * FROM report_versions WHERE id=?",
                               (report["current_version_id"],)).fetchone()
            new_no = old["version_no"] + 1
            nowiso = timeutil.iso(timeutil.now())
            vid = conn.execute(
                """INSERT INTO report_versions(report_id,version_no,kind,
                       correction_reason,issued_at,issued_by,supersedes_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (report_id, new_no, CORRECTION, reason, nowiso, actor_id,
                 old["id"])).lastrowid
            ReportService.add_metrics(conn, vid, metrics)

            fname = f"{report['code']}-v{new_no}.pdf"
            sha = reports.render_report_pdf(conn, vid, config.PDF_DIR / fname)
            conn.execute("UPDATE report_versions SET pdf_path=?,pdf_sha256=? WHERE id=?",
                         (fname, sha, vid))
            conn.execute("UPDATE reports SET current_version_id=? WHERE id=?",
                         (vid, report_id))
            # 旧版本链接立即失效（含本人旧链接；本人可重新申请当前版本）
            self.access.void_links_for_version(old["id"], "version_superseded")
            self.access.log(actor_id, "report.correct", "allow",
                            actor_role="staff", resource_type="report",
                            resource_id=report_id,
                            detail=f"v{old['version_no']}->v{new_no}")
            version = conn.execute("SELECT * FROM report_versions WHERE id=?",
                                   (vid,)).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if self.mailer:
            p = conn.execute("SELECT * FROM users WHERE id=?",
                             (report["patient_id"],)).fetchone()
            self.mailer.send_report_corrected(p, report["title"], new_no, reason)
        return version

    def issue_original(self, *, report_id: int, metrics: list[dict],
                       actor_id: int) -> sqlite3.Row:
        """首次签发原件（演示数据用；正常流程报告由体检中心录入）。"""
        conn = self.conn
        report = conn.execute("SELECT * FROM reports WHERE id=?",
                              (report_id,)).fetchone()
        if report is None:
            raise ReportVersionError("报告不存在")
        if report["current_version_id"] is not None:
            raise ReportVersionError("该报告已有版本，不能再签发原件")
        from . import db as dbmod
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(conn):
            vid = conn.execute(
                """INSERT INTO report_versions(report_id,version_no,kind,issued_at,
                       issued_by,supersedes_id)
                   VALUES(?,1,'original',?, ?,NULL)""",
                (report_id, nowiso, actor_id)).lastrowid
            ReportService.add_metrics(conn, vid, metrics)
            r = conn.execute("SELECT * FROM reports WHERE id=?",
                             (report_id,)).fetchone()
            fname = f"{r['code']}-v1.pdf"
            sha = reports.render_report_pdf(conn, vid, config.PDF_DIR / fname)
            conn.execute("UPDATE report_versions SET pdf_path=?,pdf_sha256=? WHERE id=?",
                         (fname, sha, vid))
            conn.execute("UPDATE reports SET current_version_id=? WHERE id=?",
                         (vid, report_id))
        return conn.execute("SELECT * FROM report_versions WHERE id=?",
                            (vid,)).fetchone()

    @staticmethod
    def add_metrics(conn, version_id: int, metrics: list[dict]):
        for i, m in enumerate(metrics):
            conn.execute(
                """INSERT INTO metrics(report_version_id,code,name,value,value_text,
                       unit,ref_low,ref_high,ref_text,flag,sort_no)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, m["code"], m["name"], m.get("value"),
                 m.get("value_text"), m.get("unit"), m.get("ref_low"),
                 m.get("ref_high"), m.get("ref_text"),
                 m.get("flag", "T" if "value_text" in m else "N"), i))

    # ---------- 跨年度趋势（各报告当前版本的同一指标） ----------
    def trend(self, patient_id: int):
        """返回 {metric_code: {name, unit, ref_text, points:[(exam_date, value, flag, version_id, report_id)]}}。"""
        rows = self.conn.execute(
            """SELECT m.code, m.name, m.unit, m.value, m.value_text, m.flag,
                      m.ref_low, m.ref_high, m.ref_text,
                      r.exam_date, r.id AS report_id, v.id AS version_id
               FROM reports r
               JOIN report_versions v ON v.id=r.current_version_id
               JOIN metrics m ON m.report_version_id=v.id
               WHERE r.patient_id=?
               ORDER BY r.exam_date, m.sort_no""", (patient_id,)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = out.setdefault(r["code"], {
                "code": r["code"], "name": r["name"], "unit": r["unit"],
                "ref_text": r["ref_text"],
                "ref_low": r["ref_low"], "ref_high": r["ref_high"],
                "points": []})
            d["points"].append({
                "exam_date": r["exam_date"],
                "value": r["value"], "value_text": r["value_text"],
                "flag": r["flag"], "version_id": r["version_id"],
                "report_id": r["report_id"]})
        return out

    def pdf_path(self, version: sqlite3.Row) -> Path:
        return config.PDF_DIR / version["pdf_path"]
