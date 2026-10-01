"""号源并发竞争、改期释放/锁定、停诊待改约、取消规则。"""
from __future__ import annotations

import sqlite3
import threading
import time

from healthcheck import config, db, timeutil
from healthcheck.appctx import AppContext
from healthcheck.errors import (AppointmentNotFound, SlotClosed,
                                SlotLockedByOther, SlotNotAvailable)
from tests.util import AppTestCase


class TestSlotContention(AppTestCase):
    def _contenders(self, n, report_prefix="RPT-C"):
        """为 n 位患者各建一份本人报告（可约同一号源）。"""
        actor = self.staff()
        ids = []
        for i in range(n):
            p = self.user(f"P-C{i}", "patient", f"竞争者{i}")
            rid = self.conn.execute(
                "INSERT INTO reports(code,patient_id,title,exam_date,created_at) "
                "VALUES(?,?,?,?,?)",
                (f"{report_prefix}{i}", p, "年度体检", "2026-09-10",
                 timeutil.iso(self.now))).lastrowid
            self.ctx.reports.issue_original(
                report_id=rid, metrics=self.metrics(), actor_id=actor)
            ids.append((p, rid))
        return ids

    def test_exactly_one_winner_for_single_capacity(self):
        """10 人抢同一号源：恰好 1 个成功，booked_count==1，无超卖。"""
        doc = self.doctor()
        sid = self.slot(doc, capacity=1)
        contenders = self._contenders(10)

        results, winners = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def attempt(i, p, rid):
            conn = db.connect(config.DB_PATH)
            ctx = AppContext(conn)
            barrier.wait()
            try:
                appt = ctx.booking.book(patient_id=p, slot_id=sid, report_id=rid)
                with lock:
                    winners.append(appt["code"])
                    results.append("ok")
            except SlotLockedByOther:
                with lock:
                    results.append("locked")
            except Exception as e:  # noqa
                with lock:
                    results.append(f"other:{type(e).__name__}")
            finally:
                conn.close()

        ts = [threading.Thread(target=attempt, args=(i, p, rid))
              for i, (p, rid) in enumerate(contenders)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        self.assertEqual(len(winners), 1, f"应有且仅有一个赢家，实际 {len(winners)}")
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("locked"), 9, results)
        slot = self.row("SELECT * FROM slots WHERE id=?", (sid,))
        self.assertEqual(slot["booked_count"], 1)
        n_appt = self.row(
            "SELECT COUNT(*) c FROM appointments WHERE slot_id=? AND status='booked'",
            (sid,))["c"]
        self.assertEqual(n_appt, 1)

    def test_two_capacity_admits_two(self):
        doc = self.doctor()
        sid = self.slot(doc, capacity=2)
        contenders = self._contenders(4, "RPT-D")

        outcomes = []
        barrier = threading.Barrier(4)
        lock = threading.Lock()

        def attempt(p, rid):
            conn = db.connect(config.DB_PATH)
            ctx = AppContext(conn)
            barrier.wait()
            try:
                ctx.booking.book(patient_id=p, slot_id=sid, report_id=rid)
                with lock:
                    outcomes.append("ok")
            except SlotLockedByOther:
                with lock:
                    outcomes.append("locked")
            finally:
                conn.close()

        ts = [threading.Thread(target=attempt, args=(p, rid))
              for p, rid in contenders]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(outcomes.count("ok"), 2)
        self.assertEqual(outcomes.count("locked"), 2)
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (sid,))["c"], 2)

    def test_no_booking_on_canceled_or_full_or_past(self):
        pid = self.patient()
        doc = self.doctor()
        rid, _ = self.report(pid)
        s_full = self.slot(doc, start="2026-10-09T10:00:00", capacity=1)
        self.ctx.booking.book(patient_id=pid, slot_id=s_full, report_id=rid)
        with self.assertRaises(SlotLockedByOther):
            self.ctx.booking.book(patient_id=pid, slot_id=s_full, report_id=rid)

        s_stop = self.slot(doc, start="2026-10-10T10:00:00", status="canceled")
        with self.assertRaises(SlotNotAvailable):
            self.ctx.booking.book(patient_id=pid, slot_id=s_stop, report_id=rid)


class TestReschedule(AppTestCase):
    def _setup(self):
        pid = self.patient()
        doc = self.doctor()
        rid, _ = self.report(pid)
        old = self.slot(doc, start="2026-10-08T14:00:00", mode="online")
        new = self.slot(doc, start="2026-10-12T10:00:00", mode="onsite")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=old, report_id=rid,
                                     indicators=["LDL_C"])
        return pid, doc, rid, old, new, appt

    def test_reschedule_releases_old_and_locks_new(self):
        pid, doc, rid, old, new, appt = self._setup()
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (old,))["c"], 1)
        updated = self.ctx.booking.reschedule(appt["code"], new)
        self.assertEqual(updated["slot_id"], new)
        self.assertEqual(updated["status"], "booked")
        self.assertEqual(updated["mode"], "onsite")
        self.assertEqual(updated["reschedule_count"], 1)
        # 原号源已释放，新号源已锁定
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (old,))["c"], 0)
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (new,))["c"], 1)
        types = [r["type"] for r in self.rows(
            "SELECT type FROM appointment_events WHERE appointment_id=? ORDER BY id",
            (appt["id"],))]
        self.assertIn("reschedule_release", types)
        self.assertIn("reschedule_lock", types)
        # 邮件
        self.assertIsNotNone(self.mailer.last)
        self.assertEqual(self.mailer.last["kind"], "reschedule")

    def test_reschedule_rolls_back_when_new_slot_full(self):
        pid, doc, rid, old, new, appt = self._setup()
        # 占满新号源
        other_p = self.user("P-99", "patient", "另一人")
        rid2 = self.conn.execute(
            "INSERT INTO reports(code,patient_id,title,exam_date,created_at) VALUES(?,?,?,?,?)",
            ("RPT-X", other_p, "体检", "2026-09-01",
             timeutil.iso(self.now))).lastrowid
        self.ctx.reports.issue_original(report_id=rid2, metrics=self.metrics(),
                                        actor_id=self.staff())
        self.ctx.booking.book(patient_id=other_p, slot_id=new, report_id=rid2)

        with self.assertRaises(SlotLockedByOther):
            self.ctx.booking.reschedule(appt["code"], new)
        # 事务回滚：原号源仍被本人占用，没有被错误释放
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (old,))["c"], 1)
        self.assertEqual(self.row("SELECT slot_id,status FROM appointments WHERE id=?",
                                  (appt["id"],))["slot_id"], old)

    def test_reschedule_competing_winner_keeps_slot(self):
        """两人同时改到同一号源：一人成功；失败者的原号源不得被释放。"""
        doc = self.doctor()
        target = self.slot(doc, start="2026-10-20T10:00:00", capacity=1)
        setups = []
        for i in range(2):
            p = self.user(f"P-R{i}", "patient", f"改约者{i}")
            rid = self.conn.execute(
                "INSERT INTO reports(code,patient_id,title,exam_date,created_at) "
                "VALUES(?,?,?,?,?)",
                (f"RPT-R{i}", p, "体检", "2026-09-01",
                 timeutil.iso(self.now))).lastrowid
            self.ctx.reports.issue_original(report_id=rid, metrics=self.metrics(),
                                            actor_id=self.staff())
            old = self.slot(doc, start=f"2026-10-0{8+i}T09:00:00", mode="online")
            a = self.ctx.booking.book(patient_id=p, slot_id=old, report_id=rid)
            setups.append((p, old, a))

        outcomes = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def move(old, code):
            conn = db.connect(config.DB_PATH)
            ctx = AppContext(conn)
            barrier.wait()
            try:
                ctx.booking.reschedule(code, target)
                with lock:
                    outcomes.append("ok")
            except SlotLockedByOther:
                with lock:
                    outcomes.append("locked")
            conn.close()

        ts = [threading.Thread(target=move, args=(old, a["code"]))
              for _, old, a in setups]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(sorted(outcomes), ["locked", "ok"])
        # 失败者原号源仍占 1
        loser_old = next(old for _, old, a in setups
                         if a["code"] != self.row(
                             "SELECT code FROM appointments WHERE slot_id=?",
                             (target,))["code"])
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (loser_old,))["c"], 1)
        self.assertEqual(self.row("SELECT booked_count c FROM slots WHERE id=?",
                                  (target,))["c"], 1)

    def test_cannot_reschedule_within_2_hours(self):
        pid, doc, rid, old, new, appt = self._setup()
        # 把原时段挪到当前时间 +1 小时
        self.conn.execute("UPDATE slots SET start_at=?,end_at=? WHERE id=?",
                          ("2026-10-01T10:00:00", "2026-10-01T10:30:00", old))
        with self.assertRaises(SlotClosed):
            self.ctx.booking.reschedule(appt["code"], new)


class TestSuspension(AppTestCase):
    def test_suspension_moves_appointments_to_pending(self):
        pid = self.patient()
        doc = self.doctor()
        rid, _ = self.report(pid)
        sid = self.slot(doc, start="2026-10-08T14:00:00", mode="onsite")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=sid, report_id=rid)

        result = self.ctx.booking.suspend_slot(sid, "临时会诊停诊",
                                               actor_id=self.staff())
        self.assertEqual(result["affected"], [appt["code"]])
        slot = self.row("SELECT * FROM slots WHERE id=?", (sid,))
        self.assertEqual(slot["status"], "canceled")
        self.assertEqual(slot["booked_count"], 0)
        a = self.row("SELECT * FROM appointments WHERE id=?", (appt["id"],))
        self.assertEqual(a["status"], "reschedule_pending")
        self.assertIsNone(a["slot_id"])
        self.assertIn("停诊", a["pending_reason"])
        # 待改约队列可查到，并能改到新号源
        q = self.ctx.booking.pending_queue(pid)
        self.assertEqual(len(q), 1)
        new = self.slot(doc, start="2026-10-15T09:00:00", mode="online")
        fixed = self.ctx.booking.reschedule(appt["code"], new)
        self.assertEqual(fixed["status"], "booked")
        self.assertEqual(fixed["slot_id"], new)
        self.assertEqual(self.row("SELECT COUNT(*) c FROM appointments "
                                 "WHERE status='reschedule_pending'")["c"], 0)

    def test_completed_appointment_unaffected_by_suspension(self):
        pid = self.patient()
        doc = self.doctor()
        rid, v = self.report(pid)
        sid = self.slot(doc, start="2026-10-08T10:00:00", mode="onsite")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=sid, report_id=rid)
        self.ctx.interpretation.start_or_get(appointment_id=appt["id"], doctor_id=doc)
        self.ctx.booking.suspend_slot(sid, "停诊", actor_id=self.staff())
        self.assertEqual(self.row("SELECT status FROM appointments WHERE id=?",
                                  (appt["id"],))["status"], "completed")
