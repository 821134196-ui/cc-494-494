"""会诊记录签字规则、未签字处理、作废重开、PDF 不可变与指标趋势。"""
from __future__ import annotations

import hashlib

from healthcheck import timeutil
from healthcheck.errors import InterpretationError, PermissionDenied
from healthcheck.interpretation import sign_pin
from tests.util import AppTestCase


class TestInterpretation(AppTestCase):
    def _appt(self, doctor_code="D-2201", doctor_name="陈静"):
        pid = self.patient()
        doc = self.doctor(doctor_code, doctor_name)
        rid, v = self.report(pid)
        sid = self.slot(doc, start="2026-10-08T14:00:00", mode="online")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=sid, report_id=rid,
                                     indicators=["LDL_C"])
        return pid, doc, rid, v, sid, appt

    def test_unsigned_record_is_draft_and_listed(self):
        pid, doc, rid, v, sid, appt = self._appt()
        sess = self.ctx.interpretation.start_or_get(
            appointment_id=appt["id"], doctor_id=doc)
        self.assertEqual(sess["status"], "draft")
        self.assertTrue(sess["unsigned_reason"])
        self.assertEqual(
            self.row("SELECT status FROM appointments WHERE id=?",
                     (appt["id"],))["status"], "completed")
        # 患者列表中能看到，但明确是待签字
        rows = self.ctx.interpretation.for_patient(pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "draft")

    def test_cannot_sign_without_content_or_with_wrong_pin(self):
        pid, doc, *_rest, appt = self._appt()
        sess = self.ctx.interpretation.start_or_get(
            appointment_id=appt["id"], doctor_id=doc)
        # 空内容不能签字
        with self.assertRaises(InterpretationError):
            self.ctx.interpretation.sign(session_id=sess["id"], doctor_id=doc,
                                         pin=sign_pin("D-2201"))
        self.ctx.interpretation.save_draft(
            session_id=sess["id"], doctor_id=doc, summary="总体可控", advice="",
            notes={})
        with self.assertRaises(InterpretationError):
            self.ctx.interpretation.sign(session_id=sess["id"], doctor_id=doc,
                                         pin=sign_pin("D-2201"))
        # 补全后口令错误仍拒签
        self.ctx.interpretation.save_draft(
            session_id=sess["id"], doctor_id=doc, summary="总体可控",
            advice="三个月复查", notes={"LDL_C": "轻度升高"})
        with self.assertRaises(InterpretationError):
            self.ctx.interpretation.sign(session_id=sess["id"], doctor_id=doc,
                                         pin="0000")
        # 正确口令签字定稿
        signed = self.ctx.interpretation.sign(
            session_id=sess["id"], doctor_id=doc, pin=sign_pin("D-2201"))
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["signer_id"], doc)
        self.assertIsNotNone(signed["signed_at"])

    def test_signed_record_not_editable_but_void_replace_works(self):
        pid, doc, *_rest, appt = self._appt()
        sess = self.ctx.interpretation.start_or_get(
            appointment_id=appt["id"], doctor_id=doc)
        self.ctx.interpretation.save_draft(
            session_id=sess["id"], doctor_id=doc, summary="意见", advice="建议",
            notes={"LDL_C": "备注"})
        self.ctx.interpretation.sign(session_id=sess["id"], doctor_id=doc,
                                     pin=sign_pin("D-2201"))
        with self.assertRaises(InterpretationError):
            self.ctx.interpretation.save_draft(
                session_id=sess["id"], doctor_id=doc, summary="x", advice="y",
                notes={})
        # 非本人医生不能处理
        other = self.doctor("D-7777", "其他医生")
        with self.assertRaises(PermissionDenied):
            self.ctx.interpretation.void_and_replace(
                session_id=sess["id"], doctor_id=other, reason="补充")
        # 作废重开：原记录 voided，同预约新建 draft（一对一约束保持）
        new = self.ctx.interpretation.void_and_replace(
            session_id=sess["id"], doctor_id=doc, reason="补充复查建议")
        self.assertEqual(new["status"], "draft")
        self.assertEqual(
            self.row("SELECT status FROM interpretation_sessions WHERE id=?",
                     (sess["id"],))["status"], "voided")
        self.assertEqual(new["appointment_id"], appt["id"])

    def test_only_own_doctor_can_start(self):
        pid, doc, rid, v, sid, appt = self._appt()
        other = self.doctor("D-7777", "其他医生")
        with self.assertRaises(PermissionDenied):
            self.ctx.interpretation.start_or_get(
                appointment_id=appt["id"], doctor_id=other)

    def test_pending_appointment_cannot_be_interpreted(self):
        pid = self.patient(); doc = self.doctor()
        rid, _ = self.report(pid)
        sid = self.slot(doc, start="2026-10-08T14:00:00", mode="onsite")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=sid, report_id=rid)
        self.ctx.booking.suspend_slot(sid, "停诊", actor_id=self.staff())
        with self.assertRaises(InterpretationError):
            self.ctx.interpretation.start_or_get(
                appointment_id=appt["id"], doctor_id=doc)


class TestPdfAndTrend(AppTestCase):
    def test_pdf_sha_stable_and_file_exists(self):
        from healthcheck import reports as render
        pid = self.patient()
        rid, v = self.report(pid, metrics=self.metrics(ldl=4.1))
        path = self.ctx.reports.pdf_path(v)
        self.assertTrue(path.exists())
        sha_before = v["pdf_sha256"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha_before)
        # 重新渲染同一版本：内容一致（时间戳来自版本 issued_at，确定性）
        sha2 = render.render_report_pdf(self.conn, v["id"], path)
        self.assertEqual(sha2, sha_before)

    def test_trend_groups_metrics_by_code_across_years(self):
        pid = self.patient(); actor = self.staff()
        for year, ldl in ((2023, 2.8), (2024, 3.1), (2025, 3.6)):
            rid = self.conn.execute(
                "INSERT INTO reports(code,patient_id,title,exam_date,created_at) "
                "VALUES(?,?,?,?,?)",
                (f"RPT-{year}", pid, f"{year}体检", f"{year}-09-10",
                 timeutil.iso(self.now))).lastrowid
            self.ctx.reports.issue_original(
                report_id=rid, metrics=self.metrics(ldl=ldl), actor_id=actor)
        trend = self.ctx.reports.trend(pid)
        ldl = trend["LDL_C"]
        vals = [p["value"] for p in ldl["points"]]
        self.assertEqual(vals, [2.8, 3.1, 3.6])
        # 每个趋势点都带有来源版本，便于版本引用核对
        self.assertTrue(all(p["version_id"] for p in ldl["points"]))
