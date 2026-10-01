"""授权管理服务。

确定规则：
- 授权可设到期时间（绝对时间或天数），到期自动失效（惰性 + 定时清扫）；
- 患者可随时撤销；撤销即刻生效，相关下载链接立即作废并邮件通知；
- 家属授权争议：任一方可发起争议，争议期间授权冻结为 disputed、
  链接作废、访问被拒；争议只能由患者本人终局处理——
  “确认授权”恢复 active，或“撤销授权”终局 revoked；
- 争议处理结果写入 consent_disputes，全流程可审计。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import timeutil
from . import db as dbmod
from .access import AccessService
from .errors import ConsentError, PermissionDenied

SCOPE_VIEWDOWNLOAD = "view,download"
SCOPE_VIEW = "view"
SCOPE_CN = {SCOPE_VIEWDOWNLOAD: "查看与下载", SCOPE_VIEW: "仅查看"}


class ConsentService:
    def __init__(self, conn: sqlite3.Connection, access: AccessService, mailer=None):
        self.conn = conn
        self.access = access
        self.mailer = mailer

    # ---------- 查询 ----------
    def list_for_patient(self, patient_id: int):
        rows = self.conn.execute(
            """SELECT c.*, u.name AS grantee_name, u.role AS grantee_role,
                      u.email AS grantee_email, fl.relation
               FROM consents c
               JOIN users u ON u.id=c.grantee_id
               LEFT JOIN family_links fl
                 ON fl.patient_id=c.patient_id AND fl.family_id=c.grantee_id
               WHERE c.patient_id=?
               ORDER BY CASE c.status WHEN 'active' THEN 0 WHEN 'disputed' THEN 1
                      WHEN 'expired' THEN 2 ELSE 3 END, c.id DESC""",
            (patient_id,)).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            r["open_dispute"] = self.conn.execute(
                "SELECT * FROM consent_disputes WHERE consent_id=? AND status='open' "
                "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            out.append(r)
        return out

    def get_for_patient(self, consent_id: int, patient_id: int):
        row = self.conn.execute(
            "SELECT * FROM consents WHERE id=? AND patient_id=?",
            (consent_id, patient_id)).fetchone()
        if row is None:
            raise PermissionDenied("授权不存在或不属于本人")
        return row

    # ---------- 到期清扫 ----------
    def sweep_expired(self) -> int:
        """把已到期但仍 active 的授权置为 expired 并作废链接。返回处理数。"""
        with dbmod.tx(self.conn):
            rows = self.conn.execute(
                "SELECT id FROM consents WHERE status='active' AND expires_at IS NOT NULL "
                "AND datetime(expires_at)<=datetime(?)",
                (timeutil.iso(timeutil.now()),)).fetchall()
            for r in rows:
                self.conn.execute(
                    "UPDATE consents SET status='expired' WHERE id=?", (r["id"],))
                self.access.void_links_for_consent(r["id"], "consent_expired")
        return len(rows)

    # ---------- 授予 ----------
    def grant(self, *, patient_id: int, grantee_id: int,
              scope: str = SCOPE_VIEWDOWNLOAD, expires_days: int | None = None,
              expires_at: str | datetime | None = None, note: str = "",
              allow_reactivate: bool = True) -> sqlite3.Row:
        from . import config
        conn = self.conn
        patient = conn.execute("SELECT * FROM users WHERE id=?",
                               (patient_id,)).fetchone()
        grantee = conn.execute("SELECT * FROM users WHERE id=?",
                               (grantee_id,)).fetchone()
        if patient is None or grantee is None:
            raise ConsentError("用户不存在")
        if grantee["id"] == patient["id"]:
            raise ConsentError("无需对本人授权")
        kind = "doctor" if grantee["role"] == "doctor" else (
            "family" if grantee["role"] == "family" else None)
        if kind is None:
            raise ConsentError("只能向医生或家属授予访问权")

        # 争议未决时不能重新授权，必须先终局处理
        open_d = conn.execute(
            "SELECT id FROM consent_disputes WHERE consent_id IN "
            "(SELECT id FROM consents WHERE patient_id=? AND grantee_id=?) "
            "AND status='open'", (patient_id, grantee_id)).fetchone()
        if open_d:
            raise ConsentError("该授权存在未处理的争议，请先确认或撤销")

        exp = None
        if expires_at:
            exp = expires_at if isinstance(expires_at, datetime) else timeutil.at(expires_at)
        elif expires_days is not None:
            if expires_days <= 0:
                raise ConsentError("到期时间必须晚于当前")
            if expires_days > config.AUTH_MAX_DAYS:
                raise ConsentError(f"授权有效期最长 {config.AUTH_MAX_DAYS} 天")
            exp = timeutil.now() + timedelta(days=expires_days)
        if exp and exp <= timeutil.now():
            raise ConsentError("到期时间必须晚于当前")

        existing = conn.execute(
            "SELECT * FROM consents WHERE patient_id=? AND grantee_id=?",
            (patient_id, grantee_id)).fetchone()
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(conn):
            if existing is None:
                cid = conn.execute(
                    """INSERT INTO consents(code,patient_id,grantee_id,grantee_kind,scope,
                           status,granted_at,expires_at,note)
                       VALUES(?,?,?,?,?, 'active', ?,?,?)""",
                    (f"AUTH-{timeutil.new_token(6)}", patient_id, grantee_id, kind,
                     scope, nowiso, timeutil.iso(exp) if exp else None, note)).lastrowid
            else:
                if existing["status"] == "disputed":
                    raise ConsentError("争议中的授权不可直接恢复，请先处理争议")
                if not allow_reactivate and existing["status"] in ("revoked", "expired"):
                    raise ConsentError("已终止的授权不可恢复")
                cid = existing["id"]
                conn.execute(
                    """UPDATE consents SET scope=?,status='active',granted_at=?,expires_at=?,
                           revoked_at=NULL,revoke_reason=NULL,note=?,grantee_kind=? WHERE id=?""",
                    (scope, nowiso, timeutil.iso(exp) if exp else None, note, kind, cid))

            row = conn.execute("SELECT * FROM consents WHERE id=?", (cid,)).fetchone()
        if self.mailer:
            exp_text = timeutil.fmt_dt(exp) if exp else ""
            self.mailer.send_consent_granted(
                grantee, patient["name"], SCOPE_CN.get(scope, scope), exp_text)
        return row

    # ---------- 撤销（立即生效） ----------
    def revoke(self, consent_id: int, *, patient_id: int,
               reason: str = "患者主动撤销") -> sqlite3.Row:
        conn = self.conn
        row = self.get_for_patient(consent_id, patient_id)
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(conn):
            conn.execute(
                "UPDATE consents SET status='revoked',revoked_at=?,revoke_reason=? WHERE id=?",
                (nowiso, reason, consent_id))
            # 立即作废下载链接
            self.access.void_links_for_consent(consent_id, "consent_revoked")
            # 未决争议自动随撤销终局
            conn.execute(
                "UPDATE consent_disputes SET status='resolved',resolution='patient_revoked',"
                "resolved_at=? WHERE consent_id=? AND status='open'",
                (nowiso, consent_id))
        if self.mailer:
            g = conn.execute("SELECT * FROM users WHERE id=?",
                             (row["grantee_id"],)).fetchone()
            p = conn.execute("SELECT name FROM users WHERE id=?",
                             (patient_id,)).fetchone()
            self.mailer.send_consent_revoked(g, p["name"])
        return conn.execute("SELECT * FROM consents WHERE id=?",
                            (consent_id,)).fetchone()

    # ---------- 家属授权争议 ----------
    def raise_dispute(self, consent_id: int, *, raised_by: int, reason: str):
        conn = self.conn
        consent = conn.execute("SELECT * FROM consents WHERE id=?",
                               (consent_id,)).fetchone()
        if consent is None:
            raise ConsentError("授权不存在")
        raiser = conn.execute("SELECT * FROM users WHERE id=?",
                              (raised_by,)).fetchone()
        # 患者本人或该授权的被授权家属可发起争议
        allowed = (raised_by == consent["patient_id"]
                   or (raiser and raiser["role"] == "family"
                       and raised_by == consent["grantee_id"]))
        if not allowed:
            raise PermissionDenied("仅患者本人或该授权家属可发起争议")
        if not reason.strip():
            raise ConsentError("请填写争议理由")
        open_d = conn.execute(
            "SELECT id FROM consent_disputes WHERE consent_id=? AND status='open'",
            (consent_id,)).fetchone()
        if open_d:
            raise ConsentError("该授权已有进行中的争议")

        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(conn):
            did = conn.execute(
                """INSERT INTO consent_disputes(consent_id,raised_by,reason,status,created_at)
                   VALUES(?,?,?, 'open', ?)""",
                (consent_id, raised_by, reason, nowiso)).lastrowid
            # 争议期间冻结授权并立即作废链接（确定处理：先冻结，等待患者终局）
            conn.execute("UPDATE consents SET status='disputed' WHERE id=? "
                         "AND status='active'", (consent_id,))
            cur = conn.execute("SELECT status FROM consents WHERE id=?",
                               (consent_id,)).fetchone()
            if cur["status"] == "disputed":
                self.access.void_links_for_consent(consent_id, "consent_disputed")
        return conn.execute("SELECT * FROM consent_disputes WHERE id=?",
                            (did,)).fetchone()

    def resolve_dispute(self, consent_id: int, *, patient_id: int,
                        decision: str) -> sqlite3.Row:
        """患者终局处理：confirm=确认恢复；revoke=撤销授权。"""
        conn = self.conn
        consent = self.get_for_patient(consent_id, patient_id)
        d = conn.execute(
            "SELECT * FROM consent_disputes WHERE consent_id=? AND status='open' "
            "ORDER BY id DESC LIMIT 1", (consent_id,)).fetchone()
        if d is None:
            raise ConsentError("没有进行中的争议")
        if decision not in ("confirm", "revoke"):
            raise ConsentError("处理方式必须是确认或撤销")
        nowiso = timeutil.iso(timeutil.now())
        with dbmod.tx(conn):
            if decision == "confirm":
                # 确认：若未到期则恢复，否则终局为 expired
                expired = bool(consent["expires_at"]) and (
                    timeutil.at(consent["expires_at"]) <= timeutil.now())
                new_status = "expired" if expired else "active"
                conn.execute("UPDATE consents SET status=? WHERE id=?",
                             (new_status, consent_id))
                resolution = "patient_confirmed"
                if expired:
                    self.access.void_links_for_consent(consent_id, "consent_expired")
            else:
                conn.execute(
                    "UPDATE consents SET status='revoked',revoked_at=?,revoke_reason=? "
                    "WHERE id=?",
                    (nowiso, "争议处理：患者撤销", consent_id))
                self.access.void_links_for_consent(consent_id, "consent_revoked")
                resolution = "patient_revoked"
                g = conn.execute("SELECT * FROM users WHERE id=?",
                                 (consent["grantee_id"],)).fetchone()
                p = conn.execute("SELECT name FROM users WHERE id=?",
                                 (patient_id,)).fetchone()
                mail_g, mail_p = g, p
            conn.execute(
                "UPDATE consent_disputes SET status='resolved',resolution=?,resolved_at=? "
                "WHERE id=?", (resolution, nowiso, d["id"]))
        if decision == "revoke" and self.mailer:
            self.mailer.send_consent_revoked(mail_g, mail_p["name"])
        return conn.execute("SELECT * FROM consents WHERE id=?",
                            (consent_id,)).fetchone()

    def list_disputes_open(self):
        return self.conn.execute(
            """SELECT d.*, c.patient_id, c.grantee_id, c.scope,
                      p.name AS patient_name, g.name AS grantee_name,
                      u.name AS raiser_name
               FROM consent_disputes d
               JOIN consents c ON c.id=d.consent_id
               JOIN users p ON p.id=c.patient_id
               JOIN users g ON g.id=c.grantee_id
               JOIN users u ON u.id=d.raised_by
               WHERE d.status='open' ORDER BY d.id""").fetchall()
