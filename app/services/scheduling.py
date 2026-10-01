"""号源与预约服务：抢号、改期（释放原号源+锁定新号源）、医生停诊转待改约。

并发正确性：所有涉及名额变动的操作使用单条条件 UPDATE，在 BEGIN IMMEDIATE
写事务内完成。SQLite 的写锁 + WHERE booked < capacity 保证 N 个并发请求中
至多 capacity 个成功，其余收到 Conflict（多人抢同一时段的确定结果：一个成功，
其余明确失败，不会超卖）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import audit
from ..db import Database, now
from ..errors import BusinessRule, Conflict, NotFound, PermissionDenied, ValidationError

SLOT_STEP_MIN = 30


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- 号源日历

def calendar(db: Database, *, date_from: str, date_to: str,
             doctor_id: int | None = None) -> list[dict]:
    """返回日期区间内号源（含剩余名额、状态），按时间升序。"""
    sql = ("SELECT s.*, u.name AS doctor_name FROM slots s JOIN users u ON u.id=s.doctor_id "
           "WHERE s.start_at >= ? AND s.start_at < ? ")
    params: list = [date_from + "T00:00", date_to + "T23:59"]
    if doctor_id:
        sql += "AND s.doctor_id=? "
        params.append(doctor_id)
    sql += "ORDER BY s.start_at, s.id"
    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        d = dict(r)
        d["remaining"] = d["capacity"] - d["booked"]
        out.append(d)
    return out


def doctors(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT id,name FROM users WHERE role='doctor' ORDER BY id")]


def get_slot(db: Database, slot_id: int):
    r = db.query_one("SELECT * FROM slots WHERE id=?", (slot_id,))
    if not r:
        raise NotFound("号源不存在")
    return r


def _slot_lockable(conn, slot_id: int) -> bool:
        """条件抢占：仅在开放且仍有名额时 +1。返回是否抢到。"""
        cur = conn.execute(
            "UPDATE slots SET booked=booked+1 "
            "WHERE id=? AND status='open' AND booked < capacity",
            (slot_id,))
        return cur.rowcount == 1


def _release_slot(conn, slot_id: int) -> None:
    cur = conn.execute(
        "UPDATE slots SET booked=MAX(booked-1,0) WHERE id=?", (slot_id,))
    if cur.rowcount == 0:
        raise NotFound("号源不存在")


# ---------------------------------------------------------------- 预约

def book(db: Database, *, patient_id: int, slot_id: int, report_id: int,
         topics: list[str] | None = None, created_by: int | None = None) -> dict:
    """预约解读；预先勾选想讨论的指标 topics（metric_key 列表）。"""
    topics = topics or []
    with db.tx(immediate=True) as conn:
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise NotFound("号源不存在")
        report = conn.execute(
            "SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            raise NotFound("报告不存在")
        if report["patient_id"] != patient_id:
            raise ValidationError("只能为本人的报告预约")
        if not _slot_lockable(conn, slot_id):
            # 确定结果：并发抢号的落败者得到明确 409，而非静默或超卖
            status = conn.execute("SELECT status FROM slots WHERE id=?", (slot_id,)).fetchone()["status"]
            msg = "医生已停诊，该时段不可预约，请选择其他时段" if status == "suspended" else "该时段已被约满，请选择其他时段"
            raise Conflict(msg, "slot_taken")
        cur = conn.execute(
            "INSERT INTO appointments(patient_id,doctor_id,slot_id,report_id,channel,"
            "topics,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (patient_id, slot["doctor_id"], slot_id, report_id, slot["channel"],
             json.dumps(topics, ensure_ascii=False), "booked", now(), created_by))
        appt_id = cur.lastrowid
    audit.log(db, actor_id=created_by or patient_id, actor_role="patient",
                action="book", target_type="appointment", target_id=appt_id,
                detail=f"slot={slot_id},report={report_id}")
    return get_appointment(db, appt_id)


def reschedule(db: Database, *, appointment_id: int, new_slot_id: int,
               actor_id: int) -> dict:
    """改期：在同一写事务内释放原号源并锁定新号源。

    - 新号源抢不到 -> 整体回滚，原预约不受影响（不会两头落空）。
    - 成功 -> 旧单 rescheduled，新单 booked 且 rescheduled_from 指向旧单。
    """
    with db.tx(immediate=True) as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise NotFound("预约不存在")
        if actor_id != appt["patient_id"]:
            raise PermissionDenied()
        if appt["status"] not in ("booked", "reschedule_requested", "pending_reschedule"):
            raise BusinessRule(f"当前状态（{appt['status']}）不可改期")
        if new_slot_id == appt["slot_id"]:
            raise ValidationError("新时段与原时段相同")
        new_slot = conn.execute("SELECT * FROM slots WHERE id=?",
                                (new_slot_id,)).fetchone()
        if not new_slot:
            raise NotFound("新号源不存在")

        # 1) 先锁新号源（失败则整个事务回滚，原号源仍被占用）
        if not _slot_lockable(conn, new_slot_id):
            status = conn.execute("SELECT status FROM slots WHERE id=?",
                                  (new_slot_id,)).fetchone()["status"]
            msg = "目标时段医生已停诊" if status == "suspended" else "目标时段已被约满"
            raise Conflict(msg, "slot_taken")
        # 2) 释放原号源
        _release_slot(conn, appt["slot_id"])
        # 3) 旧单标记 rescheduled
        conn.execute("UPDATE appointments SET status='rescheduled' WHERE id=?",
                     (appointment_id,))
        # 4) 新建新单，保留讨论指标与报告版本引用
        cur = conn.execute(
            "INSERT INTO appointments(patient_id,doctor_id,slot_id,report_id,channel,"
            "topics,status,created_at,rescheduled_from,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (appt["patient_id"], new_slot["doctor_id"], new_slot_id,
             appt["report_id"], new_slot["channel"], appt["topics"], "booked",
             now(), appointment_id, actor_id))
        new_id = cur.lastrowid
    audit.log(db, actor_id=actor_id, actor_role="patient",
                action="reschedule", target_type="appointment", target_id=new_id,
                detail=f"from={appointment_id},slot={new_slot_id}")
    return get_appointment(db, new_id)


def cancel(db: Database, *, appointment_id: int, actor_id: int) -> None:
    with db.tx(immediate=True) as conn:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise NotFound("预约不存在")
        if actor_id != appt["patient_id"]:
            raise PermissionDenied()
        if appt["status"] not in ("booked", "reschedule_requested", "pending_reschedule"):
            raise BusinessRule("该预约状态不可取消")
        _release_slot(conn, appt["slot_id"])
        conn.execute("UPDATE appointments SET status='cancelled' WHERE id=?",
                     (appointment_id,))
    audit.log(db, actor_id=actor_id, actor_role="patient",
                action="cancel", target_type="appointment", target_id=appointment_id)


# ---------------------------------------------------------------- 医生停诊

def suspend_doctor_slots(db: Database, *, doctor_id: int, start_date: str,
                         end_date: str, reason: str = "", actor_id: int | None = None
                         ) -> dict:
    """医生临时停诊：区间内号源置 suspended，受影响的已预约单转 pending_reschedule
    （待改约队列），释放占用名额，并经模拟邮件通知患者。返回影响统计。"""
    from ..mailer import MailAdapter
    mail = MailAdapter(db)
    start = start_date + "T00:00"
    end = end_date + "T23:59"
    affected: list[int] = []
    pending_mail: list[dict] = []
    with db.tx(immediate=True) as conn:
        slots = conn.execute(
            "SELECT * FROM slots WHERE doctor_id=? AND start_at BETWEEN ? AND ? "
            "AND status='open' ORDER BY start_at",
            (doctor_id, start, end)).fetchall()
        slot_ids = [s["id"] for s in slots]
        if slot_ids:
            conn.execute(
                f"UPDATE slots SET status='suspended' WHERE id IN "
                f"({','.join('?' * len(slot_ids))})", slot_ids)
            appts = conn.execute(
                f"SELECT a.*, u.email AS patient_email, u.name AS patient_name, "
                f"ds.start_at AS slot_start FROM appointments a "
                f"JOIN users u ON u.id=a.patient_id "
                f"JOIN slots ds ON ds.id=a.slot_id "
                f"WHERE a.slot_id IN ({','.join('?' * len(slot_ids))}) "
                f"AND a.status IN ('booked','reschedule_requested')",
                slot_ids).fetchall()
            for a in appts:
                conn.execute(
                    "UPDATE appointments SET status='pending_reschedule' WHERE id=?",
                    (a["id"],))
                affected.append(a["id"])
                pending_mail.append(dict(a))
            # 释放被转单占用的名额
            if appts:
                conn.execute(
                    f"UPDATE slots SET booked=("
                    f"SELECT COUNT(*) FROM appointments a WHERE a.slot_id=slots.id "
                    f"AND a.status IN ('booked','reschedule_requested')) "
                    f"WHERE id IN ({','.join('?' * len(slot_ids))})", slot_ids)
    # 邮件在事务提交后发送（模拟适配器），避免嵌套事务
    for a in pending_mail:
        mail.send(
            a["patient_email"],
            "【体检解读】医生停诊，请您改约",
            f"{a['patient_name']}您好：您预约于 {a['slot_start']} 的报告解读因医生临时停诊"
            f"无法进行（原因：{reason or '医生临时停诊'}）。您的预约已转入待改约队列，"
            f"原号源已释放，请重新选择时段。",
            related_id=a["id"])
    audit.log(db, actor_id=actor_id or doctor_id, actor_role="doctor",
                action="suspend", target_type="doctor", target_id=doctor_id,
                detail=f"{start_date}~{end_date}, affected={len(affected)}: {affected}")
    return {"suspended_slots": len(slot_ids), "affected_appointments": affected}


def reopen_slot(db: Database, *, slot_id: int, actor_id: int | None = None) -> None:
    """停诊恢复（如医生提前复诊）。待改约预约不会自动回填，需患者确认改约。"""
    with db.tx() as conn:
        cur = conn.execute("UPDATE slots SET status='open' WHERE id=? AND status='suspended'",
                           (slot_id,))
        if cur.rowcount == 0:
            raise NotFound("该号源不存在或未处于停诊状态")
    audit.log(db, actor_id=actor_id or 0, actor_role="doctor",
                action="reopen_slot", target_type="slot", target_id=slot_id)


# ---------------------------------------------------------------- 查询 / 队列

def get_appointment(db: Database, appt_id: int) -> dict:
    r = db.query_one(
        "SELECT a.*, du.name AS doctor_name, ds.start_at, ds.end_at, ds.channel AS slot_channel, "
        "r.title AS report_title, r.version AS report_version, r.report_no "
        "FROM appointments a "
        "JOIN users du ON du.id=a.doctor_id "
        "JOIN slots ds ON ds.id=a.slot_id "
        "JOIN reports r ON r.id=a.report_id "
        "WHERE a.id=?", (appt_id,))
    if not r:
        raise NotFound("预约不存在")
    d = dict(r)
    d["topics_list"] = json.loads(d["topics"] or "[]")
    return d


def reschedule_queue(db: Database, patient_id: int | None = None) -> list[dict]:
    """待改约队列：医生停诊导致的待改约 + 主动申请改期中的预约。"""
    sql = ("SELECT a.*, du.name AS doctor_name, ds.start_at, r.title AS report_title, "
           "r.report_no FROM appointments a "
           "JOIN users du ON du.id=a.doctor_id JOIN slots ds ON ds.id=a.slot_id "
           "JOIN reports r ON r.id=a.report_id "
           "WHERE a.status='pending_reschedule' ")
    params: list = []
    if patient_id is not None:
        sql += "AND a.patient_id=? "
        params.append(patient_id)
    sql += "ORDER BY ds.start_at"
    return [dict(r) for r in db.query(sql, tuple(params))]


def list_for_patient(db: Database, patient_id: int) -> list[dict]:
    rows = db.query(
        "SELECT a.*, du.name AS doctor_name, ds.start_at, ds.end_at, "
        "r.title AS report_title, r.version AS report_version "
        "FROM appointments a JOIN users du ON du.id=a.doctor_id "
        "JOIN slots ds ON ds.id=a.slot_id JOIN reports r ON r.id=a.report_id "
        "WHERE a.patient_id=? AND a.status IN ('booked','reschedule_requested','pending_reschedule') "
        "ORDER BY ds.start_at", (patient_id,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 排班生成

def generate_slots(db: Database, *, doctor_id: int, date_from: str, date_to: str,
                   channels=("online", "onsite"), hour_from: int = 9, hour_to: int = 17,
                   capacity: int = 1) -> int:
    """演示用：在日期区间内按 30 分钟粒度生成开放号源（跳过午休）。"""
    d0 = datetime.strptime(date_from, "%Y-%m-%d")
    d1 = datetime.strptime(date_to, "%Y-%m-%d")
    count = 0
    with db.tx() as conn:
        day = d0
        while day <= d1:
            if day.weekday() >= 5:  # 周末不出诊
                day += timedelta(days=1)
                continue
            for channel in channels:
                for h in range(hour_from, hour_to):
                    for m in (0, 30):
                        if h == 12:  # 午休
                            continue
                        start = day.replace(hour=h, minute=m)
                        end = start + timedelta(minutes=SLOT_STEP_MIN)
                        exists = conn.execute(
                            "SELECT 1 FROM slots WHERE doctor_id=? AND channel=? AND start_at=?",
                            (doctor_id, channel, _iso(start))).fetchone()
                        if exists:
                            continue
                        conn.execute(
                            "INSERT INTO slots(doctor_id,channel,start_at,end_at,capacity,booked,status)"
                            " VALUES(?,?,?,?,?,0,'open')",
                            (doctor_id, channel, _iso(start), _iso(end), capacity))
                        count += 1
            day += timedelta(days=1)
    return count
