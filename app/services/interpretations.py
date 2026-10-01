"""解读记录（会诊记录）：医生备注 + 结论 + 电子签字。

确定处理（会诊记录未签字）：
- 新建即 draft；signed_at 为 NULL 表示未签字。
- 未签字记录明确标注「未签字 · 不具备正式效力」，不能据此把预约置为完成，
  页面与接口都不得把它当正式结论。
- 已签字记录需要修改时，改为 amended 并清空签字（留痕），须重新签字才再次生效。
- 签字留痕：signature = sha256(医生|患者|报告版本|备注|结论|时间)。
"""
from __future__ import annotations

import hashlib
import json

from .. import audit
from ..db import Database, now
from ..errors import BusinessRule, NotFound, PermissionDenied, ValidationError
from . import access_control


def _signature(doctor_id: int, patient_id: int, report_id: int,
               notes: str, conclusion: str, signed_at: str) -> str:
    raw = "|".join(map(str, [doctor_id, patient_id, report_id,
                             notes, conclusion, signed_at]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def get_or_create_draft(db: Database, *, appointment_id: int, doctor_id: int) -> dict:
    appt = _appt(db, appointment_id)
    if appt["doctor_id"] != doctor_id:
        raise PermissionDenied("只有该预约的医生可以撰写解读记录")
    row = db.query_one("SELECT * FROM interpretations WHERE appointment_id=?",
                       (appointment_id,))
    if row:
        return _hydrate(db, row)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO interpretations(appointment_id,patient_id,doctor_id,report_id,"
            "notes,conclusion,created_at,signed_at,status) VALUES(?,?,?,?,?,?,?,NULL,'draft')",
            (appt["id"], appt["patient_id"], doctor_id, appt["report_id"],
             "", "", now()))
        new_id = cur.lastrowid
    return _hydrate(db, db.query_one("SELECT * FROM interpretations WHERE id=?", (new_id,)))


def save_notes(db: Database, *, appointment_id: int, doctor_id: int,
               notes: str, conclusion: str) -> dict:
    appt = _appt(db, appointment_id)
    if appt["doctor_id"] != doctor_id:
        raise PermissionDenied("只有该预约的医生可以修改解读记录")
    row = db.query_one("SELECT * FROM interpretations WHERE appointment_id=?",
                       (appointment_id,))
    if row is None:
        with db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO interpretations(appointment_id,patient_id,doctor_id,report_id,"
                "notes,conclusion,created_at,signed_at,status) VALUES(?,?,?,?,?,?,?,NULL,'draft')",
                (appt["id"], appt["patient_id"], doctor_id, appt["report_id"],
                 notes, conclusion, now()))
            row_id = cur.lastrowid
        audit.log(db, actor_id=doctor_id, actor_role="doctor",
                  action="interpretation_draft", target_type="interpretation",
                  target_id=row_id)
        return _hydrate(db, db.query_one("SELECT * FROM interpretations WHERE id=?", (row_id,)))

    if row["status"] == "signed":
        # 已签字内容被改动 -> 转为 amended 并清空签字，必须重新签字；
        # 同时把预约从 completed 退回 booked（正式结论已失效，待重签后再完成）
        db.execute(
            "UPDATE interpretations SET notes=?, conclusion=?, status='amended', "
            "signed_at=NULL, signer_id=NULL, signature=NULL WHERE id=?",
            (notes, conclusion, row["id"]))
        db.execute(
            "UPDATE appointments SET status='booked' WHERE id=? AND status='completed'",
            (appointment_id,))
        audit.log(db, actor_id=doctor_id, actor_role="doctor",
                  action="interpretation_amend", target_type="interpretation",
                  target_id=row["id"], detail="已签字记录修改，原签字失效待重签")
    else:
        # draft / amended 下编辑：仍为未签字草稿
        db.execute(
            "UPDATE interpretations SET notes=?, conclusion=?, status='draft' WHERE id=?",
            (notes, conclusion, row["id"]))
    return _hydrate(db, db.query_one("SELECT * FROM interpretations WHERE id=?", (row["id"],)))


def sign(db: Database, *, appointment_id: int, doctor_id: int) -> dict:
    appt = _appt(db, appointment_id)
    if appt["doctor_id"] != doctor_id:
        raise PermissionDenied("只有接诊医生可以签字")
    row = db.query_one("SELECT * FROM interpretations WHERE appointment_id=?",
                       (appointment_id,))
    if not row:
        raise ValidationError("请先填写备注或结论后再签字")
    if not (row["notes"].strip() or row["conclusion"].strip()):
        raise ValidationError("备注或结论至少填写一项后才能签字")
    ts = now()
    sig = _signature(doctor_id, appt["patient_id"], appt["report_id"],
                     row["notes"], row["conclusion"], ts)
    db.execute(
        "UPDATE interpretations SET signed_at=?, signer_id=?, signature=?, status='signed' WHERE id=?",
        (ts, doctor_id, sig, row["id"]))
    # 签字后预约方可完成
    db.execute("UPDATE appointments SET status='completed' WHERE id=? AND status='booked'",
               (appointment_id,))
    audit.log(db, actor_id=doctor_id, actor_role="doctor",
              action="sign", target_type="interpretation", target_id=row["id"],
              detail=f"sig={sig}")
    return _hydrate(db, db.query_one("SELECT * FROM interpretations WHERE id=?", (row["id"],)))


def get_for_reader(db: Database, *, appointment_id: int, actor_id: int) -> dict:
    """患者本人/被授权医生/授权家属可读；记录与指标、趋势并置由 Web 层组装。"""
    appt = _appt(db, appointment_id)
    decision = access_control.check_report_access(
        db, actor_id=actor_id, patient_id=appt["patient_id"])
    if not decision["allowed"]:
        audit.log(db, actor_id=actor_id, actor_role=decision["actor_role"],
                  action="interpretation_denied", target_type="interpretation",
                  detail=decision["reason"], allowed=False)
        raise PermissionDenied(decision["reason"])
    row = db.query_one("SELECT * FROM interpretations WHERE appointment_id=?",
                       (appointment_id,))
    result = _hydrate(db, row) if row else None
    audit.log(db, actor_id=actor_id, actor_role=decision["actor_role"],
              action="view_interpretation", target_type="appointment",
              target_id=appointment_id)
    return {"appointment": dict(appt), "interpretation": result}


def list_for_patient(db: Database, patient_id: int) -> list[dict]:
    rows = db.query(
        "SELECT i.*, a.slot_id, ds.start_at, du.name AS doctor_name, r.version AS report_version, "
        "r.report_no FROM interpretations i "
        "JOIN appointments a ON a.id=i.appointment_id "
        "JOIN users du ON du.id=i.doctor_id JOIN slots ds ON ds.id=a.slot_id "
        "JOIN reports r ON r.id=i.report_id "
        "WHERE i.patient_id=? ORDER BY i.id DESC", (patient_id,))
    return [_hydrate(db, r) for r in rows]


# ---------------------------------------------------------------- 内部

def _appt(db: Database, appointment_id: int):
    r = db.query_one("SELECT * FROM appointments WHERE id=?", (appointment_id,))
    if not r:
        raise NotFound("预约不存在")
    return r


def _hydrate(db: Database, row) -> dict:
    if row is None:
        return None
    d = dict(row)
    d["is_signed"] = d["signed_at"] is not None
    d["effective_label"] = "已签字 · 正式记录" if d["is_signed"] else "未签字 · 不具备正式效力"
    # notes 存为 {metric_key: 备注} 的 JSON；兼容旧的纯文本（放 _freeform）
    try:
        parsed = json.loads(d.get("notes") or "{}")
        if isinstance(parsed, dict):
            d["notes_by_metric"] = parsed
            d["notes_freeform"] = ""
        else:
            d["notes_by_metric"] = {}
            d["notes_freeform"] = d.get("notes") or ""
    except (json.JSONDecodeError, TypeError):
        d["notes_by_metric"] = {}
        d["notes_freeform"] = d.get("notes") or ""
    return d
