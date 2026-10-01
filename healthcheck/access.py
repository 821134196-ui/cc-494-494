"""访问控制与审计：授权判定、家属争议、下载链接、访问日志。

确定规则：
- 报告仅对 本人 / 持有效授权的医生 / 持有效授权的家属开放；
- 授权可设到期时间；到期即拒绝（惰性置 expired）；可随时撤销；
- 家属争议期间授权冻结（disputed），须本人确认恢复或撤销；
- 下载链接为不透明令牌，绑定“人 + 版本 + 授权”，
  授权撤销/到期、链接过期、版本被更正替代时一律 410 并立即作废；
- 每次允许/拒绝都写 access_logs。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import config, timeutil
from .errors import LinkInvalid, PermissionDenied

VIEW = "view"
DOWNLOAD = "download"


class AccessService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 日志 ----------
    def log(self, actor_id, action, result, *, actor_role=None,
            resource_type=None, resource_id=None, reason=None, ip=None, detail=None):
        self.conn.execute(
            """INSERT INTO access_logs(ts,actor_id,actor_role,action,resource_type,
                   resource_id,result,reason,ip,detail)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (timeutil.iso(timeutil.now()), actor_id, actor_role, action,
             resource_type, str(resource_id) if resource_id is not None else None,
             result, reason, ip, detail))

    # ---------- 授权状态 ----------
    def _sweep_consent(self, row: sqlite3.Row) -> sqlite3.Row:
        """惰性到期：到了 expires_at 且仍 active，标记 expired。"""
        if (row and row["status"] == "active" and row["expires_at"]
                and timeutil.at(row["expires_at"]) <= timeutil.now()):
            self.conn.execute(
                "UPDATE consents SET status='expired' WHERE id=? AND status='active'",
                (row["id"],))
            return self.conn.execute("SELECT * FROM consents WHERE id=?",
                                     (row["id"],)).fetchone()
        return row

    def effective_consent(self, patient_id: int, viewer_id: int):
        row = self.conn.execute(
            "SELECT * FROM consents WHERE patient_id=? AND grantee_id=?",
            (patient_id, viewer_id)).fetchone()
        return self._sweep_consent(row) if row else None

    def consent_reason(self, consent) -> str:
        if consent is None:
            return "no_consent"
        return {
            "active": "ok", "revoked": "consent_revoked",
            "expired": "consent_expired", "disputed": "consent_disputed",
        }[consent["status"]]

    # ---------- 报告访问判定 ----------
    def authorize_report(self, report, viewer, need: str = VIEW,
                         *, ip=None, log: bool = True):
        """返回 (granted: bool, reason, consent)。report/viewer 为 Row。"""
        conn = self.conn
        rid, vid = report["id"], viewer["id"]
        role = viewer["role"]

        consent = None
        if vid == report["patient_id"]:
            granted, reason = True, "self"
        elif role in ("doctor", "family"):
            consent = self.effective_consent(report["patient_id"], vid)
            if consent is None:
                granted, reason = False, "no_consent"
            else:
                reason = self.consent_reason(consent)
                granted = reason == "ok"
                if granted and need == DOWNLOAD and "download" not in (consent["scope"] or ""):
                    granted, reason = False, "scope_no_download"
        elif role == "staff":
            # 体检中心工作人员可做签发/更正，但默认不能浏览个人报告内容
            granted, reason = False, "staff_no_view"
        else:
            granted, reason = False, "unknown_role"

        if log:
            self.log(vid,
                     f"report.{need}" if granted else f"report.{need}.deny",
                     "allow" if granted else "deny",
                     actor_role=role, resource_type="report", resource_id=rid,
                     reason=reason, ip=ip,
                     detail=f"patient={report['patient_id']}")
        if not granted:
            return False, reason, consent
        return True, reason, consent

    def require_report(self, report, viewer, need=VIEW, ip=None):
        ok, reason, consent = self.authorize_report(report, viewer, need, ip=ip)
        if not ok:
            raise PermissionDenied(self._deny_text(reason))
        return consent

    @staticmethod
    def _deny_text(reason: str) -> str:
        return {
            "no_consent": "您没有该报告的访问授权",
            "consent_revoked": "授权已被患者撤销",
            "consent_expired": "授权已到期",
            "consent_disputed": "授权存在争议，已暂时冻结，待患者确认",
            "scope_no_download": "当前授权不包含下载权限",
            "staff_no_view": "工作人员账号不可浏览个人报告",
            "unknown_role": "未知身份，拒绝访问",
        }.get(reason, "访问被拒绝")

    # ---------- 下载链接 ----------
    def issue_download_link(self, report, viewer, version, *, ip=None,
                            ttl_hours: int | None = None) -> str:
        """先鉴权（需要 download），通过后签发一次性可追踪链接。"""
        consent = self.require_report(report, viewer, DOWNLOAD, ip=ip)
        token = timeutil.new_token()
        ttl = ttl_hours if ttl_hours is not None else config.LINK_TTL_HOURS
        expires = timeutil.now() + __import__("datetime").timedelta(hours=ttl)
        cur = self.conn.execute(
            """INSERT INTO download_links(token,report_version_id,consent_id,viewer_id,
                  status,created_at,expires_at)
               VALUES(?,?,?,?,'active',?,?)""",
            (token, version["id"], consent["id"] if consent else None,
             viewer["id"], timeutil.iso(timeutil.now()), timeutil.iso(expires)))
        self.log(viewer["id"], "download.link_issue", "allow",
                 actor_role=viewer["role"], resource_type="report_version",
                 resource_id=version["id"], reason="issued", ip=ip,
                 detail=f"link={cur.lastrowid}")
        return token

    def validate_download(self, token: str, *, ip=None):
        """返回 (link, version, report, viewer)；任何失效情形都抛 LinkInvalid(410)。"""
        conn = self.conn
        link = conn.execute("SELECT * FROM download_links WHERE token=?",
                            (token,)).fetchone()
        viewer = conn.execute("SELECT * FROM users WHERE id=?",
                              (link["viewer_id"],)).fetchone() if link else None

        def deny(reason, text):
            if link:
                conn.execute(
                    "UPDATE download_links SET status='void',voided_at=?,void_reason=? "
                    "WHERE id=? AND status='active'",
                    (timeutil.iso(timeutil.now()), reason, link["id"]))
                self.log(link["viewer_id"], "download.deny", "deny",
                         actor_role=viewer["role"] if viewer else None,
                         resource_type="report_version",
                         resource_id=link["report_version_id"], reason=reason, ip=ip)
            raise LinkInvalid(text, reason)

        if link is None:
            raise LinkInvalid("链接不存在或已失效", "not_found")
        if link["status"] != "active":
            raise LinkInvalid("链接已作废", link["void_reason"] or "voided")
        if timeutil.at(link["expires_at"]) <= timeutil.now():
            deny("expired", "链接已过期，请重新申请")

        version = conn.execute("SELECT * FROM report_versions WHERE id=?",
                               (link["report_version_id"],)).fetchone()
        report = conn.execute("SELECT * FROM reports WHERE id=?",
                              (version["report_id"],)).fetchone()
        # 版本已被更正替代 -> 旧链接失效
        if report["current_version_id"] != version["id"]:
            deny("version_superseded", "报告已有更正版本，旧链接已失效")

        # 本人链接：本人身份恒有效；授权链接：复核授权仍然有效
        if link["consent_id"]:
            consent = conn.execute("SELECT * FROM consents WHERE id=?",
                                   (link["consent_id"],)).fetchone()
            consent = self._sweep_consent(consent)
            reason = self.consent_reason(consent)
            if reason != "ok":
                deny(reason, self._deny_text(reason))
        elif link["viewer_id"] != report["patient_id"]:
            deny("orphan_link", "链接与本人身份不符")

        return link, version, report, viewer

    def mark_downloaded(self, link, *, ip=None):
        self.conn.execute(
            "UPDATE download_links SET download_count=download_count+1 WHERE id=?",
            (link["id"],))
        self.log(link["viewer_id"], "download.ok", "allow",
                 resource_type="report_version",
                 resource_id=link["report_version_id"], reason="downloaded", ip=ip)

    def void_links_for_consent(self, consent_id: int, reason: str):
        """撤销/到期/争议冻结时，立即作废该授权下的所有有效链接。"""
        self.conn.execute(
            """UPDATE download_links SET status='void',voided_at=?,void_reason=?
               WHERE consent_id=? AND status='active'""",
            (timeutil.iso(timeutil.now()), reason, consent_id))

    def void_links_for_version(self, version_id: int, reason: str):
        """报告更正时，作废旧版本的全部下载链接。"""
        self.conn.execute(
            """UPDATE download_links SET status='void',voided_at=?,void_reason=?
               WHERE report_version_id=? AND status='active'""",
            (timeutil.iso(timeutil.now()), reason, version_id))
