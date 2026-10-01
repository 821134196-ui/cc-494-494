"""号源竞争、改期原子性、医生停诊队列。"""
import os
import tempfile
import threading
import unittest

from app.db import Database
from app.errors import Conflict
from app.services import scheduling as sched
from tests.conftest_common import seed_minimal


class SlotRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-race-")
        self.path = os.path.join(self.tmp, "race.db")
        self.db = Database(self.path)
        self.ctx = seed_minimal(self.db)
        slot = self.db.query_one(
            "SELECT id FROM slots WHERE status='open' ORDER BY start_at LIMIT 1")
        self.slot_id = slot["id"]

    def tearDown(self):
        self.db.close()

    def test_only_one_winner_when_many_grab_same_slot(self):
        """10 个连接并发抢同一容量 1 号源：恰好 1 个成功，9 个明确失败，不超卖。"""
        n = 10
        results = []          # (ok, error_code)
        barrier = threading.Barrier(n)

        def attempt(i):
            db = Database(self.path)
            try:
                barrier.wait()
                sched.book(db, patient_id=self.ctx["p1"], slot_id=self.slot_id,
                           report_id=self.ctx["v2"]["id"], created_by=self.ctx["p1"])
                results.append((True, None))
            except Conflict as e:
                results.append((False, e.code))
            except Exception as e:  # 任何其他异常都算测试失败线索
                results.append((False, f"unexpected:{type(e).__name__}:{e}"))
            finally:
                db.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        winners = [r for r in results if r[0]]
        losers = [r for r in results if not r[0]]
        self.assertEqual(len(results), n)
        self.assertEqual(len(winners), 1, f"应有且仅有一个赢家，实际 {len(winners)}: {results}")
        self.assertEqual(len(losers), n - 1)
        self.assertTrue(all(code == "slot_taken" for _, code in losers),
                        f"落败原因必须全部是 slot_taken: {losers}")
        row = self.db.query_one("SELECT booked, capacity FROM slots WHERE id=?",
                                (self.slot_id,))
        self.assertEqual(row["booked"], 1)
        self.assertLessEqual(row["booked"], row["capacity"])

    def test_second_booking_returns_conflict_after_fill(self):
        sched.book(self.db, patient_id=self.ctx["p1"], slot_id=self.slot_id,
                   report_id=self.ctx["v2"]["id"])
        with self.assertRaises(Conflict):
            sched.book(self.db, patient_id=self.ctx["p1"], slot_id=self.slot_id,
                       report_id=self.ctx["v2"]["id"])


class RescheduleTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        slots = self.db.query(
            "SELECT id FROM slots WHERE status='open' ORDER BY start_at LIMIT 2")
        self.slot_a, self.slot_b = slots[0]["id"], slots[1]["id"]

    def test_reschedule_releases_old_and_locks_new(self):
        appt = sched.book(self.db, patient_id=self.ctx["p1"], slot_id=self.slot_a,
                          report_id=self.ctx["v1"]["id"], topics=["glc_fpg"])
        self.assertEqual(self._booked(self.slot_a), 1)
        self.assertEqual(self._booked(self.slot_b), 0)

        new = sched.reschedule(self.db, appointment_id=appt["id"],
                               new_slot_id=self.slot_b, actor_id=self.ctx["p1"])
        # 原号源释放、新号源锁定
        self.assertEqual(self._booked(self.slot_a), 0)
        self.assertEqual(self._booked(self.slot_b), 1)
        # 旧单 rescheduled、新单 booked 且有血缘指针，讨论指标与版本引用保留
        old = self.db.query_one("SELECT * FROM appointments WHERE id=?", (appt["id"],))
        self.assertEqual(old["status"], "rescheduled")
        self.assertEqual(new["status"], "booked")
        self.assertEqual(new["rescheduled_from"], appt["id"])
        self.assertEqual(new["report_id"], self.ctx["v1"]["id"])
        self.assertIn("glc_fpg", new["topics_list"])

    def test_failed_reschedule_keeps_original_intact(self):
        appt = sched.book(self.db, patient_id=self.ctx["p1"], slot_id=self.slot_a,
                          report_id=self.ctx["v2"]["id"])
        # 把新号源约满（用一个容量已耗尽的手段：直接置 booked=capacity）
        self.db.execute("UPDATE slots SET booked=capacity WHERE id=?",
                        (self.slot_b,))
        with self.assertRaises(Conflict):
            sched.reschedule(self.db, appointment_id=appt["id"],
                             new_slot_id=self.slot_b, actor_id=self.ctx["p1"])
        # 原预约与原号源都不受影响（事务回滚）
        self.assertEqual(self._booked(self.slot_a), 1)
        still = self.db.query_one("SELECT status FROM appointments WHERE id=?",
                                  (appt["id"],))
        self.assertEqual(still["status"], "booked")

    def test_cannot_reschedule_completed(self):
        from app.errors import BusinessRule
        appt = sched.book(self.db, patient_id=self.ctx["p1"], slot_id=self.slot_a,
                          report_id=self.ctx["v2"]["id"])
        self.db.execute("UPDATE appointments SET status='completed' WHERE id=?",
                        (appt["id"],))
        with self.assertRaises(BusinessRule):
            sched.reschedule(self.db, appointment_id=appt["id"],
                             new_slot_id=self.slot_b, actor_id=self.ctx["p1"])

    def _booked(self, slot_id):
        return self.db.query_one("SELECT booked FROM slots WHERE id=?",
                                 (slot_id,))["booked"]


class SuspendTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)

    def test_suspension_moves_appointments_to_pending_queue(self):
        target = self.db.query_one(
            "SELECT * FROM slots WHERE status='open' ORDER BY start_at LIMIT 1")
        appt = sched.book(self.db, patient_id=self.ctx["p1"],
                          slot_id=target["id"], report_id=self.ctx["v2"]["id"])
        day = target["start_at"][:10]
        res = sched.suspend_doctor_slots(
            self.db, doctor_id=self.ctx["doc1"], start_date=day, end_date=day,
            reason="突发医疗保障")
        self.assertIn(appt["id"], res["affected_appointments"])
        queue = sched.reschedule_queue(self.db, patient_id=self.ctx["p1"])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["id"], appt["id"])
        # 号源停诊且名额释放
        slot = self.db.query_one("SELECT * FROM slots WHERE id=?", (target["id"],))
        self.assertEqual(slot["status"], "suspended")
        self.assertEqual(slot["booked"], 0)
        # 停诊号源不可再约
        with self.assertRaises(Conflict):
            sched.book(self.db, patient_id=self.ctx["p1"],
                       slot_id=target["id"], report_id=self.ctx["v2"]["id"])
        # 模拟邮件已落库
        mail = self.db.query_one("SELECT COUNT(*) c FROM mail_outbox")
        self.assertGreaterEqual(mail["c"], 1)

    def test_pending_can_reschedule_to_open_slot(self):
        target = self.db.query_one(
            "SELECT * FROM slots WHERE status='open' ORDER BY start_at LIMIT 1")
        appt = sched.book(self.db, patient_id=self.ctx["p1"],
                          slot_id=target["id"], report_id=self.ctx["v2"]["id"])
        day = target["start_at"][:10]
        sched.suspend_doctor_slots(self.db, doctor_id=self.ctx["doc1"],
                                   start_date=day, end_date=day)
        other = self.db.query_one(
            "SELECT * FROM slots WHERE status='open' ORDER BY start_at LIMIT 1")
        new = sched.reschedule(self.db, appointment_id=appt["id"],
                               new_slot_id=other["id"], actor_id=self.ctx["p1"])
        self.assertEqual(new["status"], "booked")
        self.assertEqual(sched.reschedule_queue(self.db, patient_id=self.ctx["p1"]), [])


if __name__ == "__main__":
    unittest.main()
