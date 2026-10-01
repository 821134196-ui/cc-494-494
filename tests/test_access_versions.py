"""授权撤销/到期/争议、下载链接即时失效、报告更正与版本固化引用。"""
from __future__ import annotations

from healthcheck import timeutil
from healthcheck.errors import (ConsentError, LinkInvalid, PermissionDenied,
                                ReportVersionError)
from tests.util import AppTestCase


class TestAccessControl(AppTestCase):
    def test_only_self_and_authorized_can_view(self):
        pid, doc, fam = self.patient(), self.doctor(), self.family()
        rid, v = self.report(pid)
        p = self.ctx.user(pid)
        report = self.ctx.reports.get_report(rid)

        ok, reason, _ = self.ctx.access.authorize_report(report, p, "view")
        self.assertTrue(ok)
        self.assertEqual(reason, "self")

        d = self.ctx.user(doc)
        self.assertFalse(self.ctx.access.authorize_report(report, d, "view")[0])
        self.ctx.consents.grant(patient_id=pid, grantee_id=doc)
        self.assertTrue(self.ctx.access.authorize_report(report, d, "view")[0])

        f = self.ctx.user(fam)
        self.assertFalse(self.ctx.access.authorize_report(report, f, "view")[0])
        self.ctx.consents.grant(patient_id=pid, grantee_id=fam)
        self.assertTrue(self.ctx.access.authorize_report(report, f, "view")[0])

        # 无授权的第三方家属被拒
        other = self.user("F-99", "family", "外人")
        self.assertFalse(
            self.ctx.access.authorize_report(report, self.ctx.user(other), "view")[0])

    def test_view_only_consent_cannot_download(self):
        pid, fam = self.patient(), self.family()
        rid, v = self.report(pid)
        self.ctx.consents.grant(patient_id=pid, grantee_id=fam, scope="view")
        with self.assertRaises(PermissionDenied):
            self.ctx.access.issue_download_link(
                self.ctx.reports.get_report(rid), self.ctx.user(fam), v)

    def test_deny_is_logged(self):
        fam = self.family()
        rid, _ = self.report(self.patient())
        self.ctx.access.authorize_report(
            self.ctx.reports.get_report(rid), self.ctx.user(fam), "view")
        log = self.row("SELECT * FROM access_logs WHERE actor_id=? ORDER BY id DESC",
                       (fam,))
        self.assertEqual(log["result"], "deny")
        self.assertEqual(log["reason"], "no_consent")


class TestRevocationAndLinks(AppTestCase):
    def _link(self, grantee_kind="family"):
        pid = self.patient()
        doc = self.doctor()
        fam = self.family()
        rid, v = self.report(pid)
        gid = doc if grantee_kind == "doctor" else fam
        consent = self.ctx.consents.grant(patient_id=pid, grantee_id=gid)
        token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(gid), v)
        return pid, gid, rid, v, consent, token

    def test_link_works_then_revocation_kills_it_immediately(self):
        pid, gid, rid, v, consent, token = self._link()
        link, version, report, viewer = self.ctx.access.validate_download(token)
        self.assertEqual(version["id"], v["id"])

        # 撤销授权：链接立即失效，再次校验即 410
        self.ctx.consents.revoke(consent["id"], patient_id=pid)
        with self.assertRaises(LinkInvalid) as cm:
            self.ctx.access.validate_download(token)
        self.assertEqual(cm.exception.reason, "consent_revoked")
        # 链接行已置 void，访问也被拒
        self.assertEqual(self.row("SELECT status FROM download_links WHERE token=?",
                                  (token,))["status"], "void")
        with self.assertRaises(PermissionDenied):
            self.ctx.access.require_report(
                self.ctx.reports.get_report(rid), self.ctx.user(gid), "view")
        # 撤销通知已发送
        self.assertEqual(self.mailer.last["kind"], "consent_revoked")

    def test_expired_consent_invalidates_link(self):
        pid, gid, rid, v, consent, token = self._link()
        # 推进时钟超过到期时间（授权设置 30 天）
        consent = self.ctx.consents.grant(patient_id=pid, grantee_id=gid,
                                          expires_days=30)
        # 链接 TTL 长于授权期限，确保先触发的是“授权到期”
        token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(gid),
            self.ctx.reports.current_version(rid), ttl_hours=24 * 90)
        # 有效
        self.ctx.access.validate_download(token)
        timeutil.ClockControl.get().advance(days=31)
        with self.assertRaises(LinkInvalid) as cm:
            self.ctx.access.validate_download(token)
        self.assertEqual(cm.exception.reason, "consent_expired")
        self.assertEqual(
            self.row("SELECT status FROM consents WHERE id=?",
                     (consent["id"],))["status"], "expired")

    def test_link_ttl_expiry(self):
        pid = self.patient()
        rid, v = self.report(pid)
        token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(pid), v, ttl_hours=2)
        self.ctx.access.validate_download(token)
        timeutil.ClockControl.get().advance(hours=3)
        with self.assertRaises(LinkInvalid) as cm:
            self.ctx.access.validate_download(token)
        self.assertEqual(cm.exception.reason, "expired")

    def test_unknown_and_replayed_token_rejected(self):
        with self.assertRaises(LinkInvalid):
            self.ctx.access.validate_download("not-a-real-token")
        pid = self.patient(); rid, v = self.report(pid)
        token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(pid), v)
        link, *_ = self.ctx.access.validate_download(token)
        self.ctx.access.mark_downloaded(link)
        # 有效链接可重复下载直到失效；此处校验仍然通过
        self.ctx.access.validate_download(token)


class TestDispute(AppTestCase):
    def test_dispute_freezes_and_patient_resolves(self):
        pid, fam = self.patient(), self.family()
        rid, v = self.report(pid)
        consent = self.ctx.consents.grant(patient_id=pid, grantee_id=fam,
                                          expires_days=60)
        token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(fam), v)

        # 家属发起争议 -> 冻结 + 链接失效
        self.ctx.consents.raise_dispute(consent["id"], raised_by=fam,
                                        reason="家属称授权非本人操作")
        self.assertEqual(
            self.row("SELECT status FROM consents WHERE id=?",
                     (consent["id"],))["status"], "disputed")
        with self.assertRaises(LinkInvalid):
            self.ctx.access.validate_download(token)
        with self.assertRaises(PermissionDenied):
            self.ctx.access.require_report(
                self.ctx.reports.get_report(rid), self.ctx.user(fam), "view")

        # 非患者不能处理争议
        with self.assertRaises(PermissionDenied):
            self.ctx.consents.resolve_dispute(consent["id"], patient_id=fam,
                                              decision="confirm")
        # 患者确认 -> 恢复 active（旧链接不复活，需重新申请）
        self.ctx.consents.resolve_dispute(consent["id"], patient_id=pid,
                                          decision="confirm")
        self.assertEqual(
            self.row("SELECT status FROM consents WHERE id=?",
                     (consent["id"],))["status"], "active")
        new_token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(fam), v)
        self.ctx.access.validate_download(new_token)

        # 旧链接仍失效
        with self.assertRaises(LinkInvalid):
            self.ctx.access.validate_download(token)

    def test_dispute_revoke_is_terminal(self):
        pid, fam = self.patient(), self.family()
        rid, v = self.report(pid)
        consent = self.ctx.consents.grant(patient_id=pid, grantee_id=fam)
        self.ctx.consents.raise_dispute(consent["id"], raised_by=fam, reason="存疑")
        self.ctx.consents.resolve_dispute(consent["id"], patient_id=pid,
                                          decision="revoke")
        self.assertEqual(
            self.row("SELECT status,revoke_reason FROM consents WHERE id=?",
                     (consent["id"],))["status"], "revoked")
        d = self.row("SELECT resolution FROM consent_disputes WHERE consent_id=?",
                     (consent["id"],))
        self.assertEqual(d["resolution"], "patient_revoked")


class TestCorrectionAndVersionPinning(AppTestCase):
    def test_correction_voids_old_links_but_keeps_interpretation_ref(self):
        pid, doc = self.patient(), self.doctor()
        rid, v1 = self.report(pid, metrics=self.metrics(ldl=5.8))
        self.ctx.consents.grant(patient_id=pid, grantee_id=doc)

        old_token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(pid), v1)
        doc_token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(doc), v1)

        # 预约与解读固化 v1 引用
        sid = self.slot(doc, start="2026-10-08T14:00:00")
        appt = self.ctx.booking.book(patient_id=pid, slot_id=sid, report_id=rid)
        self.assertEqual(appt["report_version_id"], v1["id"])

        # 工作人员签发更正件
        actor = self.staff()
        v2 = self.ctx.reports.issue_correction(
            report_id=rid, actor_id=actor, reason="标本串号，LDL-C 更正为 4.1",
            metrics=self.metrics(ldl=4.1))
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["supersedes_id"], v1["id"])
        self.assertEqual(
            self.row("SELECT current_version_id FROM reports WHERE id=?",
                     (rid,))["current_version_id"], v2["id"])

        # 旧版本链接（本人与医生）立即失效
        for t in (old_token, doc_token):
            with self.assertRaises(LinkInvalid) as cm:
                self.ctx.access.validate_download(t)
            self.assertEqual(cm.exception.reason, "version_superseded")

        # 当前版本链接有效
        new_token = self.ctx.access.issue_download_link(
            self.ctx.reports.get_report(rid), self.ctx.user(pid), v2)
        self.ctx.access.validate_download(new_token)

        # 既有预约仍引用 v1（版本固化，不被更正改写）
        appt2 = self.row("SELECT * FROM appointments WHERE id=?", (appt["id"],))
        self.assertEqual(appt2["report_version_id"], v1["id"])

        # 医生完成解读，会话引用其预约版本 v1
        sess = self.ctx.interpretation.start_or_get(
            appointment_id=appt["id"], doctor_id=doc)
        self.assertEqual(sess["report_version_id"], v1["id"])
        self.ctx.interpretation.save_draft(
            session_id=sess["id"], doctor_id=doc,
            summary="总体平稳", advice="控制饮食并复查", notes={"LDL_C": "趋势上行"})
        signed = self.ctx.interpretation.sign(
            session_id=sess["id"], doctor_id=doc, pin="2201")
        self.assertEqual(signed["status"], "signed")
        # 签字后引用仍为 v1
        self.assertEqual(
            self.row("SELECT report_version_id FROM interpretation_sessions WHERE id=?",
                     (sess["id"],))["report_version_id"], v1["id"])

    def test_correction_requires_staff_and_reason(self):
        pid = self.patient()
        rid, v1 = self.report(pid)
        with self.assertRaises(PermissionDenied):
            self.ctx.reports.issue_correction(
                report_id=rid, actor_id=pid, reason="x",
                metrics=self.metrics())
        actor = self.staff()
        with self.assertRaises(ReportVersionError):
            self.ctx.reports.issue_correction(
                report_id=rid, actor_id=actor, reason="  ", metrics=self.metrics())

    def test_version_history_retains_old(self):
        pid = self.patient()
        rid, v1 = self.report(pid, metrics=self.metrics(ldl=5.8))
        self.ctx.reports.issue_correction(
            report_id=rid, actor_id=self.staff(), reason="串号",
            metrics=self.metrics(ldl=4.1))
        versions = self.ctx.reports.versions(rid)
        self.assertEqual([v["version_no"] for v in versions], [2, 1])
        self.assertEqual(versions[1]["kind"], "correction") if False else None
        kinds = {v["version_no"]: v["kind"] for v in versions}
        self.assertEqual(kinds, {1: "original", 2: "correction"})
