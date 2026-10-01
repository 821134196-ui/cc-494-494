"""授权授予/到期/撤销、下载链接立即失效、家属授权争议。"""
import unittest

from app.db import Database, now
from app.errors import BusinessRule, PermissionDenied, ValidationError
from app.services import authorizations as auth_svc
from app.services import reports as report_svc
from app.services import scheduling as sched
from tests.conftest_common import seed_minimal


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)

    def test_grant_and_effective_access(self):
        auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                       grantee_id=self.ctx["doc2"])
        r = report_svc.view_report(self.db, actor_id=self.ctx["doc2"],
                                   report_id=self.ctx["v2"]["id"])
        self.assertEqual(r["version"], 2)

    def test_patient_cannot_grant_to_patient_or_self(self):
        with self.assertRaises(ValidationError):
            auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                           grantee_id=self.ctx["p2"])
        with self.assertRaises(ValidationError):
            auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                           grantee_id=self.ctx["p1"])

    def test_expired_authorization_denied(self):
        a = auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                           grantee_id=self.ctx["doc2"], expires_days=1)
        self.db.execute("UPDATE authorizations SET expires_at=? WHERE id=?",
                        ("2000-01-01T00:00:00Z", a["id"]))
        auth_svc.sweep_expired(self.db)
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["doc2"],
                                   report_id=self.ctx["v2"]["id"])

    def test_no_authorization_denied_fail_closed(self):
        # 未授权医生 / 其他患者 / 无记录：一律拒绝
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["doc2"],
                                   report_id=self.ctx["v2"]["id"])
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["p2"],
                                   report_id=self.ctx["v2"]["id"])


class RevokeLinkTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        self.auth = auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                                   grantee_id=self.ctx["fam1"], expires_days=30)
        self.link = auth_svc.create_link(
            self.db, patient_id=self.ctx["p1"], grantee_id=self.ctx["fam1"],
            report_id=self.ctx["v1"]["id"])

    def test_link_works_before_revoke(self):
        res = auth_svc.resolve_link(self.db, token=self.link["token"],
                                    actor_id=self.ctx["fam1"])
        self.assertEqual(res["report"]["id"], self.ctx["v1"]["id"])
        self.assertEqual(res["report"]["version"], 1)

    def test_revoke_invalidates_link_immediately(self):
        # 旧链接在撤销前可正常下载
        auth_svc.resolve_link(self.db, token=self.link["token"],
                              actor_id=self.ctx["fam1"])
        auth_svc.revoke(self.db, patient_id=self.ctx["p1"],
                        authorization_id=self.auth["id"])
        # 撤销后旧链接立即失效（不是等过期）
        with self.assertRaises(PermissionDenied):
            auth_svc.resolve_link(self.db, token=self.link["token"],
                                  actor_id=self.ctx["fam1"])
        row = self.db.query_one("SELECT status FROM download_links WHERE token=?",
                                (self.link["token"],))
        self.assertEqual(row["status"], "revoked")
        # 撤销后也不能再看到报告
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["fam1"],
                                   report_id=self.ctx["v1"]["id"])

    def test_link_expiry_denied(self):
        self.db.execute("UPDATE download_links SET expires_at=? WHERE token=?",
                        ("2000-01-01T00:00:00Z", self.link["token"]))
        with self.assertRaises(PermissionDenied):
            auth_svc.resolve_link(self.db, token=self.link["token"],
                                  actor_id=self.ctx["fam1"])

    def test_link_pins_version_after_correction(self):
        """链接固定到 V1；V2 更正件出具后，旧链接取到的仍是 V1 内容与校验和。"""
        res = auth_svc.resolve_link(self.db, token=self.link["token"],
                                    actor_id=self.ctx["fam1"])
        self.assertEqual(res["report"]["id"], self.ctx["v1"]["id"])
        self.assertEqual(res["checksum"], self.ctx["v1"]["checksum"])
        self.assertNotEqual(res["checksum"], self.ctx["v2"]["checksum"])

    def test_cannot_create_link_without_effective_grant(self):
        auth_svc.revoke(self.db, patient_id=self.ctx["p1"],
                        authorization_id=self.auth["id"])
        with self.assertRaises(BusinessRule):
            auth_svc.create_link(self.db, patient_id=self.ctx["p1"],
                                 grantee_id=self.ctx["fam1"],
                                 report_id=self.ctx["v2"]["id"])


class DisputeTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)
        self.auth = auth_svc.grant(self.db, patient_id=self.ctx["p1"],
                                   grantee_id=self.ctx["fam1"])
        self.link = auth_svc.create_link(
            self.db, patient_id=self.ctx["p1"], grantee_id=self.ctx["fam1"],
            report_id=self.ctx["v2"]["id"])

    def test_dispute_freezes_access_and_links(self):
        auth_svc.raise_dispute(self.db, patient_id=self.ctx["p1"],
                               authorization_id=self.auth["id"],
                               actor_id=self.ctx["fam1"], reason="知情范围分歧")
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["fam1"],
                                   report_id=self.ctx["v2"]["id"])
        with self.assertRaises(PermissionDenied):
            auth_svc.resolve_link(self.db, token=self.link["token"],
                                  actor_id=self.ctx["fam1"])

    def test_only_patient_can_resolve_dispute(self):
        auth_svc.raise_dispute(self.db, patient_id=self.ctx["p1"],
                               authorization_id=self.auth["id"],
                               actor_id=self.ctx["fam1"], reason="x")
        # 家属不能自行恢复
        with self.assertRaises(PermissionDenied):
            auth_svc.resolve_dispute(self.db, patient_id=self.ctx["p2"],
                                     authorization_id=self.auth["id"],
                                     decision="restore")
        # 本人裁决恢复
        auth_svc.resolve_dispute(self.db, patient_id=self.ctx["p1"],
                                 authorization_id=self.auth["id"],
                                 decision="restore")
        r = report_svc.view_report(self.db, actor_id=self.ctx["fam1"],
                                   report_id=self.ctx["v2"]["id"])
        self.assertEqual(r["version"], 2)
        # 本人裁决撤销
        self.link = auth_svc.create_link(
            self.db, patient_id=self.ctx["p1"], grantee_id=self.ctx["fam1"],
            report_id=self.ctx["v2"]["id"])
        auth_svc.raise_dispute(self.db, patient_id=self.ctx["p1"],
                               authorization_id=self.auth["id"],
                               actor_id=self.ctx["p1"], reason="x")
        auth_svc.resolve_dispute(self.db, patient_id=self.ctx["p1"],
                                 authorization_id=self.auth["id"],
                                 decision="revoke")
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["fam1"],
                                   report_id=self.ctx["v2"]["id"])


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.ctx = seed_minimal(self.db)

    def test_denied_access_is_logged(self):
        with self.assertRaises(PermissionDenied):
            report_svc.view_report(self.db, actor_id=self.ctx["p2"],
                                   report_id=self.ctx["v1"]["id"])
        row = self.db.query_one(
            "SELECT * FROM access_logs WHERE allowed=0 AND action='view_report_denied'")
        self.assertIsNotNone(row)
        self.assertEqual(row["actor_id"], self.ctx["p2"])


if __name__ == "__main__":
    unittest.main()
