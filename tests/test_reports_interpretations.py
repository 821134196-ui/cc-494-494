"""报告版本/更正引用、趋势、解读记录签字规则。"""
import json
import unittest

from app.db import Database
from app.errors import BusinessRule, PermissionDenied, ValidationError
from app.services import interpretations as interp
from app.services import reports as report_svc
from app.services import scheduling as sched
from tests.conftest_common import seed_minimal


class ReportVersionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        slots = self.db.query(
            "SELECT id FROM slots WHERE status='open' ORDER BY start_at LIMIT 1")
        self.slot_id = slots[0]["id"]

    def test_correction_creates_version_chain(self):
        v1, v2 = self.ctx["v1"], self.ctx["v2"]
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], v1["id"])
        self.assertTrue(v2["is_correction"])
        self.assertIsNotNone(v2["correction_note"])
        # 两版本 PDF 校验和不同且均固定
        self.assertNotEqual(v1["checksum"], v2["checksum"])
        latest = report_svc.latest_version(self.db, "R-001")
        self.assertEqual(latest["id"], v2["id"])

    def test_correction_requires_reason(self):
        with self.assertRaises(ValidationError):
            report_svc.issue_report(
                self.db, patient_id=self.ctx["p1"], report_no="R-X",
                title="t", examined_at="2026-09-24", metrics=[],
                pdf_path="x", checksum="x",
                correction_note=None, supersedes_id=self.ctx["v1"]["id"])

    def test_appointment_pins_version_across_correction(self):
        """预约引用 V1；更正件 V2 出具后，预约仍指向 V1（版本不漂移）。"""
        appt = sched.book(self.db, patient_id=self.ctx["p1"],
                          slot_id=self.slot_id, report_id=self.ctx["v1"]["id"],
                          topics=["glc_fpg"])
        self.assertEqual(appt["report_version"], 1)
        again = sched.get_appointment(self.db, appt["id"])
        self.assertEqual(again["report_id"], self.ctx["v1"]["id"])
        # V1 仍可访问、内容不变（血糖仍为 7.2 的初版）
        old = report_svc.get_report(self.db, self.ctx["v1"]["id"])
        old_metric = self.db.query_one(
            "SELECT value FROM metrics WHERE report_id=? AND metric_key='glc_fpg'",
            (self.ctx["v1"]["id"],))
        self.assertAlmostEqual(old_metric["value"], 7.2)
        self.assertEqual(old["checksum"], self.ctx["v1"]["checksum"])

    def test_trend_uses_latest_version_per_exam_date(self):
        trend = {t["metric_key"]: t for t in
                 report_svc.trend(self.db, self.ctx["p1"])}
        glc = trend["glc_fpg"]
        # 2026-09-24 有 V1/V2 两个版本，趋势只取 V2 的 6.1，不重复计点
        pts_2026 = [p for p in glc["points"] if p["examined_at"] == "2026-09-24"]
        self.assertEqual(len(pts_2026), 1)
        self.assertAlmostEqual(pts_2026[0]["value"], 6.1)
        self.assertEqual(pts_2026[0]["version"], 2)
        exam_dates = [p["examined_at"] for p in glc["points"]]
        self.assertEqual(exam_dates, sorted(exam_dates))


class InterpretationSignTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        self.slot_id = self.db.query_one(
            "SELECT id FROM slots ORDER BY start_at LIMIT 1")["id"]
        self.appt = sched.book(self.db, patient_id=self.ctx["p1"],
                               slot_id=self.slot_id,
                               report_id=self.ctx["v2"]["id"])

    def test_unsigned_record_is_not_effective(self):
        interp.save_notes(self.db, appointment_id=self.appt["id"],
                          doctor_id=self.ctx["doc1"],
                          notes=json.dumps({"glc_fpg": "观察"}, ensure_ascii=False),
                          conclusion="")
        rec = interp.get_for_reader(
            self.db, appointment_id=self.appt["id"], actor_id=self.ctx["p1"])
        self.assertIsNotNone(rec["interpretation"])
        self.assertFalse(rec["interpretation"]["is_signed"])
        self.assertIn("未签字", rec["interpretation"]["effective_label"])
        # 未签字不把预约置为 completed
        self.assertEqual(
            self.db.query_one("SELECT status FROM appointments WHERE id=?",
                              (self.appt["id"],))["status"], "booked")

    def test_sign_completes_appointment(self):
        interp.save_notes(self.db, appointment_id=self.appt["id"],
                          doctor_id=self.ctx["doc1"],
                          notes=json.dumps({"glc_fpg": "控制可"}, ensure_ascii=False),
                          conclusion="继续随访")
        signed = interp.sign(self.db, appointment_id=self.appt["id"],
                             doctor_id=self.ctx["doc1"])
        self.assertTrue(signed["is_signed"])
        self.assertEqual(
            self.db.query_one("SELECT status FROM appointments WHERE id=?",
                              (self.appt["id"],))["status"], "completed")
        self.assertTrue(len(signed["signature"]) >= 12)

    def test_sign_empty_record_rejected(self):
        with self.assertRaises(ValidationError):
            interp.sign(self.db, appointment_id=self.appt["id"],
                        doctor_id=self.ctx["doc1"])

    def test_amend_after_sign_clears_signature(self):
        interp.save_notes(self.db, appointment_id=self.appt["id"],
                          doctor_id=self.ctx["doc1"], notes="{}", conclusion="结论")
        interp.sign(self.db, appointment_id=self.appt["id"],
                    doctor_id=self.ctx["doc1"])
        updated = interp.save_notes(
            self.db, appointment_id=self.appt["id"], doctor_id=self.ctx["doc1"],
            notes=json.dumps({"tc": "修改"}, ensure_ascii=False), conclusion="新结论")
        self.assertEqual(updated["status"], "amended")
        self.assertFalse(updated["is_signed"])
        # 预约退回 booked（正式结论失效），重签后再次完成
        self.assertEqual(
            self.db.query_one("SELECT status FROM appointments WHERE id=?",
                              (self.appt["id"],))["status"], "booked")
        interp.sign(self.db, appointment_id=self.appt["id"],
                    doctor_id=self.ctx["doc1"])
        self.assertEqual(
            self.db.query_one("SELECT status FROM appointments WHERE id=?",
                              (self.appt["id"],))["status"], "completed")

    def test_other_doctor_cannot_write(self):
        with self.assertRaises(PermissionDenied):
            interp.save_notes(self.db, appointment_id=self.appt["id"],
                              doctor_id=self.ctx["doc2"], notes="{}", conclusion="x")

    def test_unauthorized_family_cannot_read(self):
        with self.assertRaises(PermissionDenied):
            interp.get_for_reader(self.db, appointment_id=self.appt["id"],
                                  actor_id=self.ctx["fam1"])


if __name__ == "__main__":
    unittest.main()
