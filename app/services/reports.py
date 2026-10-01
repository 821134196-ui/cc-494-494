"""报告版本与更正服务。

版本规则（确定处理）：
- 同一 report_no 的版本号严格递增；更正件 supersedes 指向被更正版本，
  旧版本保留且不可变（PDF + checksum 固化），预约/解读记录引用的具体版本
  不会因出具更正件而漂移。
- 更正必须填写更正原因；更正件出具后，旧版本状态为 superseded（通过
  supersedes 链表达），所有访问旧版本下载链接仍取到当时的旧 PDF。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .. import audit
from ..db import Database, now
from ..errors import BusinessRule, NotFound, PermissionDenied, ValidationError
from . import access_control


# ---------------------------------------------------------------- 取号/版本

def get_report(db: Database, report_id: int) -> dict:
    r = db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
    if not r:
        raise NotFound("报告不存在")
    return dict(r)


def get_by_version(db: Database, report_no: str, version: int) -> dict:
    r = db.query_one("SELECT * FROM reports WHERE report_no=? AND version=?",
                     (report_no, version))
    if not r:
        raise NotFound("该版本不存在")
    return dict(r)


def versions(db: Database, patient_id: int) -> list[dict]:
    """患者所有报告的全部版本（新在前）。"""
    rows = db.query(
        "SELECT r.*, "
        "(SELECT version FROM reports c WHERE c.supersedes=r.id) AS corrected_by_version, "
        "(SELECT COUNT(*) FROM metrics m WHERE m.report_id=r.id) AS metric_count "
        "FROM reports r WHERE r.patient_id=? ORDER BY r.report_no, r.version DESC",
        (patient_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["has_newer"] = r["supersedes"] is not None or r["corrected_by_version"] is not None
        out.append(d)
    return out


def latest_version(db: Database, report_no: str) -> dict:
    r = db.query_one(
        "SELECT * FROM reports WHERE report_no=? ORDER BY version DESC LIMIT 1",
        (report_no,))
    if not r:
        raise NotFound("报告不存在")
    return dict(r)


def list_patients(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT id,name FROM users WHERE role='patient' ORDER BY id")]


# ---------------------------------------------------------------- 指标 / 趋势

def metrics_of(db: Database, report_id: int) -> list[dict]:
    rows = db.query(
        "SELECT * FROM metrics WHERE report_id=? ORDER BY id", (report_id,))
    return [dict(r) for r in rows]


def metric_status(m: dict) -> str:
    """依据本版本固化的参考范围判定：low/high/normal。"""
    if m["ref_low"] is not None and m["value"] < m["ref_low"]:
        return "low"
    if m["ref_high"] is not None and m["value"] > m["ref_high"]:
        return "high"
    return "normal"


def trend(db: Database, patient_id: int) -> list[dict]:
    """按 metric_key 聚合该患者历次体检数值（跨报告版本取每次体检的最新值，
    同一 examined_at 只取版本号最大者），解读页据此画趋势。"""
    rows = db.query(
        """
        SELECT m.metric_key, m.label, m.unit, m.ref_low, m.ref_high,
               m.value, m.examined_at, r.version, r.report_no,
               m.report_id
        FROM metrics m
        JOIN reports r ON r.id = m.report_id
        WHERE m.patient_id=?
          AND r.id = (
              SELECT r2.id FROM reports r2
              JOIN metrics m2 ON m2.report_id=r2.id
              WHERE r2.patient_id=? AND m2.metric_key=m.metric_key
                AND r2.examined_at = r.examined_at
              ORDER BY r2.version DESC LIMIT 1)
        ORDER BY m.metric_key, m.examined_at
        """, (patient_id, patient_id))
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        key = r["metric_key"]
        if key not in grouped:
            grouped[key] = {
                "metric_key": key, "label": r["label"], "unit": r["unit"],
                "ref_low": r["ref_low"], "ref_high": r["ref_high"],
                "points": [],
            }
            order.append(key)
        grouped[key]["points"].append({
            "examined_at": r["examined_at"], "value": r["value"],
            "version": r["version"], "report_no": r["report_no"],
            "report_id": r["report_id"],
        })
    return [grouped[k] for k in order]


# ---------------------------------------------------------------- 出具/更正

def issue_report(db: Database, *, patient_id: int, report_no: str, title: str,
                 examined_at: str, metrics: list[dict], pdf_path: str,
                 checksum: str, correction_note: str | None = None,
                 supersedes_id: int | None = None) -> dict:
    """出具新版本。首次出具 version=1；更正时传 supersedes_id，版本号自动 +1。

    更正报告样例：空腹血糖实验室录入错误由 7.2 更正为 6.1。
    """
    with db.tx() as conn:
        if supersedes_id is not None:
            old = conn.execute("SELECT * FROM reports WHERE id=?",
                               (supersedes_id,)).fetchone()
            if not old:
                raise NotFound("被更正的原报告不存在")
            if old["patient_id"] != patient_id:
                raise ValidationError("不能跨患者更正")
            if not correction_note:
                raise ValidationError("更正报告必须填写更正原因")
            if old["report_no"] != report_no:
                raise ValidationError("更正件必须沿用同一报告编号")
            new_version = old["version"] + 1
            is_corr = 1
        else:
            dup = conn.execute(
                "SELECT 1 FROM reports WHERE report_no=? AND version=1",
                (report_no,)).fetchone()
            if dup:
                raise BusinessRule("报告编号已存在；更正请走更正流程", "duplicate_report")
            new_version = 1
            is_corr = 0
        cur = conn.execute(
            "INSERT INTO reports(patient_id,version,report_no,title,examined_at,issued_at,"
            "supersedes,correction_note,is_correction,pdf_path,checksum) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (patient_id, new_version, report_no, title, examined_at, now(),
             supersedes_id, correction_note, is_corr, pdf_path, checksum))
        report_id = cur.lastrowid
        for m in metrics:
            conn.execute(
                "INSERT INTO metrics(report_id,patient_id,metric_key,label,value,unit,"
                "ref_low,ref_high,examined_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (report_id, patient_id, m["metric_key"], m["label"],
                 float(m["value"]), m["unit"],
                 m.get("ref_low"), m.get("ref_high"), examined_at))
    audit.log(db, actor_id=None, actor_role="system",
              action="issue_correction" if is_corr else "issue_report",
              target_type="report", target_id=report_id,
              detail=f"{report_no} v{new_version}" + (f" supersedes={supersedes_id}" if supersedes_id else ""))
    return get_report(db, report_id)


# ---------------------------------------------------------------- 受控访问

def view_report(db: Database, *, actor_id: int, report_id: int) -> dict:
    """带鉴权的报告读取：本人 / 被授权医生 / 明确授权家属，其余拒绝并记日志。"""
    report = get_report(db, report_id)
    decision = access_control.check_report_access(db, actor_id=actor_id,
                                                  patient_id=report["patient_id"])
    if not decision["allowed"]:
        audit.log(db, actor_id=actor_id, actor_role=decision["actor_role"],
                  action="view_report_denied", target_type="report",
                  target_id=report_id, detail=decision["reason"], allowed=False)
        raise PermissionDenied(decision["reason"])
    report["metrics"] = metrics_of(db, report_id)
    for m in report["metrics"]:
        m["status"] = metric_status(m)
    report["trend"] = trend(db, report["patient_id"])
    report["versions"] = [
        v for v in versions(db, report["patient_id"])
        if v["report_no"] == report["report_no"]]
    audit.log(db, actor_id=actor_id, actor_role=decision["actor_role"],
              action="view_report", target_type="report", target_id=report_id,
              detail=f"as={decision['basis']}")
    return report
