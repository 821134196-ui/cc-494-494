"""解读记录服务。

确定规则（会诊记录未签字）：
- 解读记录初始为 draft（未签字），必须由进行解读的医生本人“电子签字”后
  才成为 signed（定稿）；未签字记录明确标注“待签字”，不得作为正式结论，
  页面与 PDF 均不展示为定稿，患者侧显示待签字原因；
- 医生可补签：draft -> signed；已签字记录不可再编辑（如需修改须作废重开
  voided + 新建）；签字必须校验签名口令（演示固定为医生工号后 4 位）；
- 记录固化 report_version_id（解读当时所依据的版本）；报告事后更正
  不改变该引用，并在记录上提示“依据历史版本 vN”；
- 指标级医生备注与指标趋势在解读页并置展示；
- 预约完成后才能建立解读记录，建立/签字均写审计。
"""
from __future__ import annotations

import sqlite3

from . import timeutil
from . import db as dbmod
from .errors import InterpretationError, PermissionDenied

# 演示用签名口令：医生 code 的数字后 4 位
def sign_pin(doctor_code: str) -> str:
    digits = "".join(ch for ch in doctor_code if ch.isdigit())
    return digits[-4:].zfill(4) if digits else "0000"


class InterpretationService:
    def __init__(self, conn, access=None, mailer=None):
        self.conn = conn
        self.access = access
        self.mailer = mailer

    def get_by_appointment(self, appointment_id: int):
        return self.conn.execute(
            "SELECT * FROM interpretation_sessions WHERE appointment_id=? "
            "AND is_current=1", (appointment_id,)).fetchone()

    def notes(self, session_id: int):
        return self.conn.execute(
            "SELECT metric_code, content FROM interpretation_notes WHERE session_id=? "
            "ORDER BY id", (session_id,)).fetchall()

    def notes_map(self, session_id: int) -> dict[str, str]:
        return {r["metric_code"]: r["content"] for r in self.notes(session_id)}

    def start_or_get(self, *, appointment_id: int, doctor_id: int):
        """医生开始解读：预约须属于该医生且已到/已完成；返回会话。"""
        appt = self.conn.execute("SELECT * FROM appointments WHERE id=?",
                                 (appointment_id,)).fetchone()
        if appt is None:
            raise InterpretationError("预约不存在")
        if appt["doctor_id"] != doctor_id:
            raise PermissionDenied("只能解读自己的预约")
        if appt["status"] == "canceled":
            raise InterpretationError("已取消的预约不能建立解读记录")
        if appt["status"] == "reschedule_pending":
            raise InterpretationError("该预约处于待改约，尚不能解读")

        sess = self.get_by_appointment(appointment_id)
        if sess is not None:
            if sess["doctor_id"] != doctor_id:
                raise PermissionDenied("该解读记录属于其他医生")
            return sess

        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(self.conn):
            sid = self.conn.execute(
                """INSERT INTO interpretation_sessions(code,appointment_id,patient_id,
                       doctor_id,report_version_id,status,created_at,unsigned_reason)
                   VALUES(?,?,?,?,?, 'draft', ?,?)""",
                (f"ITP-{timeutil.new_token(5)}", appointment_id, appt["patient_id"],
                 doctor_id, appt["report_version_id"], nowiso,
                 "会诊记录待医生签字确认")).lastrowid
            # 到达解读环节即把预约置为 completed
            if appt["status"] != "completed":
                self.conn.execute(
                    "UPDATE appointments SET status='completed',updated_at=? WHERE id=?",
                    (nowiso, appointment_id))
                self.conn.execute(
                    """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                       VALUES(?,?,?,?,?,?)""",
                    (appointment_id, nowiso, doctor_id, "completed",
                     appt["slot_id"], "完成解读并建立记录（待签字）"))
        return self.conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                                 (sid,)).fetchone()

    def save_draft(self, *, session_id: int, doctor_id: int,
                   summary: str, advice: str,
                   notes: dict[str, str], unsigned_reason: str | None = None):
        sess = self._require_editable(session_id, doctor_id)
        with dbmod.tx(self.conn):
            self.conn.execute(
                "UPDATE interpretation_sessions SET summary=?,advice=?,"
                "unsigned_reason=? WHERE id=?",
                (summary, advice,
                 unsigned_reason or "会诊记录待医生签字确认", session_id))
            self._replace_notes(session_id, notes)
        return self.conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                                 (session_id,)).fetchone()

    def sign(self, *, session_id: int, doctor_id: int, pin: str):
        """医生本人签字定稿。口令错误、内容为空均拒绝（确定处理）。"""
        sess = self._require_editable(session_id, doctor_id)
        doc = self.conn.execute("SELECT * FROM users WHERE id=?",
                                (doctor_id,)).fetchone()
        if pin != sign_pin(doc["code"]):
            raise InterpretationError("签名口令不正确，未完成签字")
        if not (sess["summary"] or "").strip():
            raise InterpretationError("会诊记录缺少总体意见，不能签字")
        if not (sess["advice"] or "").strip():
            raise InterpretationError("会诊记录缺少建议，不能签字")
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(self.conn):
            self.conn.execute(
                "UPDATE interpretation_sessions SET status='signed',signed_at=?,"
                "signer_id=?,unsigned_reason=NULL WHERE id=?",
                (nowiso, doctor_id, session_id))
            if self.access:
                self.access.log(doctor_id, "interpretation.sign", "allow",
                                actor_role="doctor", resource_type="interpretation",
                                resource_id=session_id, reason="signed")
        return self.conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                                 (session_id,)).fetchone()

    def void_and_replace(self, *, session_id: int, doctor_id: int, reason: str):
        """已签字记录如需修改：作废并基于同一预约新建草稿。"""
        sess = self.conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                                 (session_id,)).fetchone()
        if sess is None:
            raise InterpretationError("解读记录不存在")
        if sess["doctor_id"] != doctor_id:
            raise PermissionDenied("只能处理自己的解读记录")
        if sess["status"] != "signed":
            raise InterpretationError("仅已签字记录需要作废重开")
        if not reason.strip():
            raise InterpretationError("请填写作废原因")
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(self.conn):
            # 旧记录作废并退出“当前记录”，为同预约新建草稿腾出唯一槽位
            self.conn.execute(
                "UPDATE interpretation_sessions SET is_current=0 WHERE id=?",
                (session_id,))
            self.conn.execute(
                "UPDATE interpretation_sessions SET status='voided',unsigned_reason=?, "
                "is_current=0 WHERE id=?", (f"已作废：{reason}", session_id))
            new = self.start_or_get(appointment_id=sess["appointment_id"],
                                    doctor_id=doctor_id)
        return new

    def _require_editable(self, session_id, doctor_id):
        sess = self.conn.execute("SELECT * FROM interpretation_sessions WHERE id=?",
                                 (session_id,)).fetchone()
        if sess is None:
            raise InterpretationError("解读记录不存在")
        if sess["doctor_id"] != doctor_id:
            raise PermissionDenied("只能编辑自己的解读记录")
        if sess["status"] == "signed":
            raise InterpretationError("已签字记录不可修改；如需更正请作废后重开")
        if sess["status"] == "voided":
            raise InterpretationError("该记录已作废")
        return sess

    def _replace_notes(self, session_id, notes: dict[str, str]):
        self.conn.execute("DELETE FROM interpretation_notes WHERE session_id=?",
                          (session_id,))
        for code, content in notes.items():
            if content and content.strip():
                self.conn.execute(
                    """INSERT OR REPLACE INTO interpretation_notes
                           (session_id,metric_code,content) VALUES(?,?,?)""",
                    (session_id, code, content.strip()))

    # ---------- 列表 ----------
    def for_patient(self, patient_id: int):
        return self.conn.execute(
            """SELECT s.*, u.name AS doctor_name, u.title AS doctor_title,
                      a.code AS appt_code, r.title AS report_title
               FROM interpretation_sessions s
               JOIN users u ON u.id=s.doctor_id
               JOIN appointments a ON a.id=s.appointment_id
               JOIN reports r ON r.id=a.report_id
               WHERE s.patient_id=? AND s.status!='voided'
               ORDER BY s.created_at DESC""", (patient_id,)).fetchall()

    def for_doctor(self, doctor_id: int):
        return self.conn.execute(
            """SELECT s.*, p.name AS patient_name, a.code AS appt_code
               FROM interpretation_sessions s
               JOIN users p ON p.id=s.patient_id
               JOIN appointments a ON a.id=s.appointment_id
               WHERE s.doctor_id=? AND s.status!='voided'
               ORDER BY CASE s.status WHEN 'draft' THEN 0 ELSE 1 END, s.created_at DESC""",
            (doctor_id,)).fetchall()
