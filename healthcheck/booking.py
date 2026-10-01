"""号源日历与预约服务。

并发正确性：
- 所有写操作在单条 BEGIN IMMEDIATE 事务内完成；SQLite 的写锁将
  并发提交串行化。抢号使用“条件更新”：
      UPDATE slots SET booked_count=booked_count+1
      WHERE id=? AND status='open' AND booked_count<capacity
  只有影响行数为 1 的事务成功，其余立即得到 SlotLockedByOther(409)，
  因此同一时段恰好一人锁定，无超卖、无“读后写”竞态。
- 改期在同一事务内“释放原号源 + 锁定新号源”，要么都完成要么都回滚。
- 医生停诊：号源置 canceled，受影响预约原子地释放号源并转为
  reschedule_pending（待改约队列），逐一发模拟邮件。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from . import config, timeutil
from .errors import (AppointmentNotFound, PermissionDenied, SlotClosed,
                     SlotLockedByOther, SlotNotAvailable)
from .mailer import MockMailer

MODE_CN = {"online": "线上", "onsite": "现场"}


class BookingService:
    def __init__(self, conn: sqlite3.Connection, mailer: MockMailer | None = None):
        self.conn = conn
        self.mailer = mailer

    # ---------- 日历 ----------
    def calendar(self, doctor_id: int, date_from=None, days: int = 14):
        d0 = date_from or timeutil.today()
        rows = self.conn.execute(
            """SELECT s.*, u.name AS doctor_name
               FROM slots s JOIN users u ON u.id=s.doctor_id
               WHERE s.doctor_id=? AND date(s.start_at)>=date(?)
                 AND date(s.start_at)<=date(?, '+' || ? || ' days')
               ORDER BY s.start_at, s.mode""",
            (doctor_id, d0.isoformat(), d0.isoformat(), days)).fetchall()
        return rows

    def open_slots(self, doctor_id: int | None = None, days: int = 14):
        d0 = timeutil.today()
        sql = (
            "SELECT s.*, u.name AS doctor_name, u.title AS doctor_title "
            "FROM slots s JOIN users u ON u.id=s.doctor_id "
            "WHERE s.status='open' AND s.booked_count<s.capacity "
            "AND datetime(s.start_at)>datetime(?) "
            "AND date(s.start_at)<=date(?, '+' || ? || ' days') ")
        args: list = [timeutil.iso(timeutil.now()), d0.isoformat(), days]
        if doctor_id:
            sql += "AND s.doctor_id=? "
            args.append(doctor_id)
        sql += "ORDER BY s.start_at, u.name"
        return self.conn.execute(sql, args).fetchall()

    def get_slot(self, slot_id: int):
        return self.conn.execute(
            "SELECT s.*, u.name AS doctor_name FROM slots s JOIN users u ON u.id=s.doctor_id "
            "WHERE s.id=?", (slot_id,)).fetchone()

    # ---------- 预约 ----------
    def _check_slot_bookable(self, slot: sqlite3.Row):
        if slot["status"] != "open":
            raise SlotNotAvailable("该时段已停诊或暂停开放")
        if slot["booked_count"] >= slot["capacity"]:
            raise SlotLockedByOther()
        if timeutil.at(slot["start_at"]) <= timeutil.now():
            raise SlotClosed("该时段已开始，无法预约")

    def book(self, *, patient_id: int, slot_id: int, report_id: int,
             indicators: list[str] | None = None, actor_id: int | None = None,
             _conn_held: sqlite3.Connection | None = None) -> sqlite3.Row:
        """锁定号源并创建预约。返回预约 Row。"""
        own = _conn_held is None
        conn = _conn_held or self.conn
        indicators = indicators or []
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                (slot_id,)).fetchone()
            if slot is None:
                raise SlotNotAvailable("时段不存在")
            self._check_slot_bookable(slot)

            report = conn.execute(
                "SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                raise SlotNotAvailable("报告不存在")
            if report["patient_id"] != patient_id:
                raise PermissionDenied("只能为本人报告预约解读")
            version_id = report["current_version_id"]

            # 条件更新：并发抢号的唯一裁决点
            cur = conn.execute(
                "UPDATE slots SET booked_count=booked_count+1 "
                "WHERE id=? AND status='open' AND booked_count<capacity "
                "AND datetime(start_at)>datetime(?)",
                (slot_id, timeutil.iso(timeutil.now())))
            if cur.rowcount != 1:
                raise SlotLockedByOther()

            code = f"APT{timeutil.now().strftime('%m%d')}-{timeutil.new_token(3).upper()}"
            nowiso = timeutil.iso(timeutil.now())
            appt_id = conn.execute(
                """INSERT INTO appointments(code,patient_id,doctor_id,slot_id,
                       report_id,report_version_id,mode,indicators,status,
                       created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'booked',?,?)""",
                (code, patient_id, slot["doctor_id"], slot_id, report_id,
                 version_id, slot["mode"], json.dumps(indicators, ensure_ascii=False),
                 nowiso, nowiso)).lastrowid
            conn.execute(
                """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                   VALUES(?,?,?,?,?,?)""",
                (appt_id, nowiso, actor_id or patient_id, "booked", slot_id,
                 f"锁定 {MODE_CN[slot['mode']]} 时段"))
            appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                                (appt_id,)).fetchone()
            if own:
                conn.execute("COMMIT")
        except Exception:
            if own:
                conn.execute("ROLLBACK")
            raise

        if own and self.mailer:
            self._confirm_mail(appt)
        return appt

    def _confirm_mail(self, appt):
        patient = self.conn.execute("SELECT * FROM users WHERE id=?",
                                    (appt["patient_id"],)).fetchone()
        slot = self.get_slot(appt["slot_id"])
        self.mailer.send_booking_confirm(
            patient, appt["code"], slot["doctor_name"],
            timeutil.fmt_dt(timeutil.at(slot["start_at"]))
            + f"（{MODE_CN[appt['mode']]}）",
            MODE_CN[appt["mode"]])

    # ---------- 改期：释放原号 + 锁定新号（同一事务） ----------
    def reschedule(self, appt_code: str, new_slot_id: int, *,
                   actor_id: int | None = None) -> sqlite3.Row:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            appt = conn.execute("SELECT * FROM appointments WHERE code=?",
                                (appt_code,)).fetchone()
            if appt is None:
                raise AppointmentNotFound()
            if appt["status"] not in ("booked", "reschedule_pending"):
                raise SlotNotAvailable(
                    {"completed": "已完成的预约不能改期",
                     "canceled": "已取消的预约不能改期"}.get(appt["status"], "当前状态不能改期"))

            old_slot_id = appt["slot_id"]
            old_slot = None
            if old_slot_id:
                old_slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                        (old_slot_id,)).fetchone()
                if appt["status"] == "booked" and timeutil.at(old_slot["start_at"]) \
                        <= timeutil.now() + timedelta(hours=config.SLOT_CANCEL_HOURS):
                    raise SlotClosed(
                        f"距开始不足 {config.SLOT_CANCEL_HOURS} 小时，请联系体检中心改期")

            new_slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                    (new_slot_id,)).fetchone()
            if new_slot is None:
                raise SlotNotAvailable("新时段不存在")
            if new_slot["doctor_id"] != appt["doctor_id"]:
                raise SlotNotAvailable("改期须选择同一位医生；如需更换医生请重新预约")
            self._check_slot_bookable(new_slot)
            if old_slot_id == new_slot_id:
                raise SlotNotAvailable("新时段与原时段相同")

            nowiso = timeutil.iso(timeutil.now())

            # 1) 锁定新号（先锁新，避免“释放后抢不回”造成丢预约）
            cur = conn.execute(
                "UPDATE slots SET booked_count=booked_count+1 "
                "WHERE id=? AND status='open' AND booked_count<capacity "
                "AND datetime(start_at)>datetime(?)",
                (new_slot_id, timeutil.iso(timeutil.now())))
            if cur.rowcount != 1:
                raise SlotLockedByOther()

            # 2) 释放原号
            if old_slot_id:
                conn.execute(
                    "UPDATE slots SET booked_count=MAX(0,booked_count-1) WHERE id=?",
                    (old_slot_id,))
                conn.execute(
                    """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                       VALUES(?,?,?,?,?,?)""",
                    (appt["id"], nowiso, actor_id or appt["patient_id"],
                     "reschedule_release", old_slot_id, "改期释放原号源"))

            # 3) 更新预约
            conn.execute(
                """UPDATE appointments SET slot_id=?,mode=?,status='booked',
                       updated_at=?,reschedule_count=reschedule_count+1,
                       pending_reason=NULL WHERE id=?""",
                (new_slot_id, new_slot["mode"], nowiso, appt["id"]))
            conn.execute(
                """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                   VALUES(?,?,?,?,?,?)""",
                (appt["id"], nowiso, actor_id or appt["patient_id"],
                 "reschedule_lock", new_slot_id,
                 f"改期锁定 {MODE_CN[new_slot['mode']]} 新号源"))
            new_appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                                    (appt["id"],)).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if self.mailer:
            patient = self.conn.execute("SELECT * FROM users WHERE id=?",
                                        (new_appt["patient_id"],)).fetchone()
            doc = self.conn.execute("SELECT name FROM users WHERE id=?",
                                    (new_appt["doctor_id"],)).fetchone()["name"]
            old_when = (timeutil.fmt_dt(timeutil.at(old_slot["start_at"]))
                        + f"（{MODE_CN[old_slot['mode']]}）") if old_slot else "待改约"
            new_when = (timeutil.fmt_dt(timeutil.at(new_slot["start_at"]))
                        + f"（{MODE_CN[new_slot['mode']]}）")
            self.mailer.send_reschedule(patient, new_appt["code"], doc, old_when, new_when)
        return new_appt

    # ---------- 取消 ----------
    def cancel(self, appt_code: str, *, actor_id: int | None = None,
               reason: str = "患者取消") -> sqlite3.Row:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            appt = conn.execute("SELECT * FROM appointments WHERE code=?",
                                (appt_code,)).fetchone()
            if appt is None:
                raise AppointmentNotFound()
            if appt["status"] != "booked":
                raise SlotNotAvailable("仅已预约状态可取消")
            slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                (appt["slot_id"],)).fetchone()
            if timeutil.at(slot["start_at"]) <= timeutil.now() \
                    + timedelta(hours=config.SLOT_CANCEL_HOURS):
                raise SlotClosed(f"距开始不足 {config.SLOT_CANCEL_HOURS} 小时，无法自助取消")
            nowiso = timeutil.iso(timeutil.now())
            conn.execute("UPDATE slots SET booked_count=MAX(0,booked_count-1) WHERE id=?",
                         (appt["slot_id"],))
            conn.execute(
                "UPDATE appointments SET status='canceled',updated_at=? WHERE id=?",
                (nowiso, appt["id"]))
            conn.execute(
                """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                   VALUES(?,?,?,?,?,?)""",
                (appt["id"], nowiso, actor_id or appt["patient_id"], "canceled",
                 appt["slot_id"], reason))
            out = conn.execute("SELECT * FROM appointments WHERE id=?",
                               (appt["id"],)).fetchone()
            conn.execute("COMMIT")
            return out
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---------- 医生停诊：批量转待改约 ----------
    def suspend_slot(self, slot_id: int, reason: str, *,
                     actor_id: int | None = None) -> dict:
        """号源停诊。确定处理：
        - 该号源置 canceled 并释放全部已占名额（booked_count=0）；
        - 所有 booked 预约释放其对该号源的占用、slot_id 置空、
          状态转 reschedule_pending，记 pending_reason，入待改约队列；
        - 已完成/已取消预约不受影响；逐人发停诊邮件。
        返回受影响预约列表。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        affected = []
        try:
            slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                (slot_id,)).fetchone()
            if slot is None:
                raise SlotNotAvailable("时段不存在")
            if slot["status"] == "canceled":
                raise SlotNotAvailable("该时段已停诊")
            nowiso = timeutil.iso(timeutil.now())

            appts = conn.execute(
                "SELECT * FROM appointments WHERE slot_id=? AND status='booked'",
                (slot_id,)).fetchall()
            conn.execute(
                "UPDATE slots SET status='canceled',booked_count=0,note=? WHERE id=?",
                (f"停诊：{reason}", slot_id))
            for a in appts:
                conn.execute(
                    """UPDATE appointments SET status='reschedule_pending',slot_id=NULL,
                           updated_at=?,pending_reason=? WHERE id=?""",
                    (nowiso, f"医生停诊：{reason}", a["id"]))
                conn.execute(
                    """INSERT INTO appointment_events(appointment_id,ts,actor_id,type,slot_id,detail)
                       VALUES(?,?,?,?,?,?)""",
                    (a["id"], nowiso, actor_id, "doctor_suspended", slot_id, reason))
                affected.append(a["code"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if self.mailer and affected:
            s = self.get_slot(slot_id)
            for code in affected:
                a = self.conn.execute("SELECT * FROM appointments WHERE code=?",
                                      (code,)).fetchone()
                patient = self.conn.execute("SELECT * FROM users WHERE id=?",
                                            (a["patient_id"],)).fetchone()
                self.mailer.send_doctor_suspended(
                    patient, code, s["doctor_name"],
                    timeutil.fmt_dt(timeutil.at(s["start_at"]))
                    + f"（{MODE_CN[s['mode']]}）", reason)
        return {"slot_id": slot_id, "affected": affected}

    def pending_queue(self, patient_id: int):
        return self.conn.execute(
            """SELECT a.*, u.name AS doctor_name,
                      os.start_at AS old_start, os.mode AS old_mode
               FROM appointments a
               JOIN users u ON u.id=a.doctor_id
               LEFT JOIN appointment_events e
                 ON e.appointment_id=a.id AND e.type='doctor_suspended'
                    AND e.id=(SELECT MAX(id) FROM appointment_events
                              WHERE appointment_id=a.id AND type='doctor_suspended')
               LEFT JOIN slots os ON os.id=e.slot_id
               WHERE a.status='reschedule_pending' AND a.patient_id=?
               ORDER BY a.updated_at DESC""", (patient_id,)).fetchall()

    def my_appointments(self, patient_id: int):
        return self.conn.execute(
            """SELECT a.*, u.name AS doctor_name, u.title AS doctor_title,
                      s.start_at, s.end_at, s.mode AS slot_mode, s.status AS slot_status
               FROM appointments a
               JOIN users u ON u.id=a.doctor_id
               LEFT JOIN slots s ON s.id=a.slot_id
               WHERE a.patient_id=?
               ORDER BY CASE a.status WHEN 'reschedule_pending' THEN 0 ELSE 1 END,
                        COALESCE(s.start_at,'') DESC""",
            (patient_id,)).fetchall()

    def doctor_appointments(self, doctor_id: int):
        return self.conn.execute(
            """SELECT a.*, p.name AS patient_name, s.start_at, s.end_at, s.mode AS slot_mode,
                      s.status AS slot_status
               FROM appointments a
               JOIN users p ON p.id=a.patient_id
               LEFT JOIN slots s ON s.id=a.slot_id
               WHERE a.doctor_id=? AND a.status IN ('booked','reschedule_pending','completed')
               ORDER BY COALESCE(s.start_at,'') DESC""",
            (doctor_id,)).fetchall()
