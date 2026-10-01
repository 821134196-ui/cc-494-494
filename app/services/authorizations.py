"""授权管理：授予（可设到期）、撤销（立即生效）、家属授权争议、下载链接。

确定处理：
- 撤销授权：状态置 revoked 且该授权名下所有 valid 下载链接立即 revoked 失效。
- 到期：惰性失效 + 定时扫描；过期授权与链接不可再用。
- 家属授权争议：被本人或家属提出后授权冻结为 disputed（失败关闭），
  只能由患者本人裁决（恢复 active 或撤销 revoked），家属无权自行恢复。
- 下载链接固定到具体报告版本；旧链接在撤销/过期后访问得到 410 并记日志。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .. import audit
from ..db import Database, now
from ..errors import BusinessRule, Conflict, NotFound, PermissionDenied, ValidationError
from . import access_control


def _future(days: int | None, hours: int | None = None) -> str | None:
    if days is None and hours is None:
        return None
    d = datetime.strptime(now(), "%Y-%m-%dT%H:%M:%SZ")
    if days is not None:
        d += timedelta(days=days)
    if hours is not None:
        d += timedelta(hours=hours)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 授予/列举/撤销

def grant(db: Database, *, patient_id: int, grantee_id: int,
          expires_days: int | None = None, scope: str = "report:read") -> dict:
    grantee = db.query_one("SELECT * FROM users WHERE id=?", (grantee_id,))
    if not grantee:
        raise NotFound("被授权用户不存在")
    if grantee["role"] not in ("doctor", "family"):
        raise ValidationError("只能向医生或家属授权")
    if grantee_id == patient_id:
        raise ValidationError("无需对本人授权")
    expires_at = _future(expires_days)
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT * FROM authorizations WHERE patient_id=? AND grantee_id=?",
            (patient_id, grantee_id)).fetchone()
        if existing:
            if existing["status"] in ("active", "disputed"):
                raise BusinessRule("授权已存在（争议冻结中）"
                                   if existing["status"] == "disputed"
                                   else "授权已存在，请勿重复授予")
            # revoked/expired -> 重新激活
            conn.execute(
                "UPDATE authorizations SET status='active', granted_at=?, expires_at=?, "
                "revoked_at=NULL, scope=? WHERE id=?",
                (now(), expires_at, scope, existing["id"]))
            auth_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO authorizations(patient_id,grantee_id,grantee_role,scope,"
                "granted_at,expires_at,revoked_at,status) VALUES(?,?,?,?,?,?,NULL,'active')",
                (patient_id, grantee_id, grantee["role"], scope, now(), expires_at))
            auth_id = cur.lastrowid
    audit.log(db, actor_id=patient_id, actor_role="patient",
              action="grant", target_type="authorization", target_id=auth_id,
              detail=f"grantee={grantee_id},expires={expires_at}")
    return _auth_dict(db, auth_id)


def list_grants(db: Database, patient_id: int) -> list[dict]:
    rows = db.query(
        "SELECT a.*, u.name AS grantee_name, u.email AS grantee_email "
        "FROM authorizations a JOIN users u ON u.id=a.grantee_id "
        "WHERE a.patient_id=? ORDER BY a.id", (patient_id,))
    out = []
    for r in rows:
        d = dict(r)
        ok, reason = access_control.is_effective(r)
        d["effective"] = 1 if ok else 0
        d["effective_reason"] = reason
        d["links"] = [dict(x) for x in db.query(
            "SELECT id,token,report_id,version,status,expires_at,used_count,max_uses "
            "FROM download_links WHERE authorized_id=? ORDER BY id DESC", (r["id"],))]
        out.append(d)
    return out


def _auth_dict(db: Database, auth_id: int) -> dict:
    r = db.query_one(
        "SELECT a.*, u.name AS grantee_name FROM authorizations a "
        "JOIN users u ON u.id=a.grantee_id WHERE a.id=?", (auth_id,))
    if not r:
        raise NotFound("授权不存在")
    return dict(r)


def revoke(db: Database, *, patient_id: int, authorization_id: int) -> None:
    """本人随时撤销：授权失效 + 其名下全部有效下载链接立即失效。"""
    with db.tx(immediate=True) as conn:
        a = conn.execute("SELECT * FROM authorizations WHERE id=?",
                         (authorization_id,)).fetchone()
        if not a:
            raise NotFound("授权不存在")
        if a["patient_id"] != patient_id:
            raise PermissionDenied("只有本人可以撤销授权")
        if a["status"] == "revoked":
            raise BusinessRule("授权此前已撤销")
        conn.execute(
            "UPDATE authorizations SET status='revoked', revoked_at=? WHERE id=?",
            (now(), authorization_id))
        conn.execute(
            "UPDATE download_links SET status='revoked' "
            "WHERE authorized_id=? AND status='valid'", (authorization_id,))
    audit.log(db, actor_id=patient_id, actor_role="patient",
              action="revoke", target_type="authorization", target_id=authorization_id,
              detail="授权及其下载链接立即失效")


def sweep_expired(db: Database) -> dict:
    """到期扫描：过期授权与链接置 expired（确定性兜底，正常访问也会惰性判定）。"""
    ts = now()
    with db.tx() as conn:
        c1 = conn.execute(
            "UPDATE authorizations SET status='expired' "
            "WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?",
            (ts,)).rowcount
        c2 = conn.execute(
            "UPDATE download_links SET status='expired' "
            "WHERE status='valid' AND expires_at<=?", (ts,)).rowcount
    return {"expired_authorizations": c1, "expired_links": c2}


# ---------------------------------------------------------------- 家属授权争议

def raise_dispute(db: Database, *, patient_id: int, authorization_id: int,
                  actor_id: int, reason: str) -> None:
    """家属授权争议：患者本人或被授权家属均可发起；发起即冻结，待本人裁决。"""
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM authorizations WHERE id=?",
                         (authorization_id,)).fetchone()
        if not a:
            raise NotFound("授权不存在")
        if a["grantee_role"] != "family":
            raise ValidationError("仅家属授权可发起争议")
        if actor_id != a["patient_id"] and actor_id != a["grantee_id"]:
            raise PermissionDenied("只有本人或该家属可以发起争议")
        conn.execute(
            "UPDATE authorizations SET status='disputed' WHERE id=? AND status IN ('active','disputed')",
            (authorization_id,))
        conn.execute(
            "UPDATE download_links SET status='revoked' "
            "WHERE authorized_id=? AND status='valid'", (authorization_id,))
    audit.log(db, actor_id=actor_id, actor_role="family",
              action="dispute", target_type="authorization",
              target_id=authorization_id, detail=reason[:200])


def resolve_dispute(db: Database, *, patient_id: int, authorization_id: int,
                    decision: str) -> None:
    """本人裁决：restore=恢复有效（重新开放，需重新生成链接）；revoke=正式撤销。
    这是争议的唯一出口，结果确定。"""
    if decision not in ("restore", "revoke"):
        raise ValidationError("裁决必须是 restore 或 revoke")
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM authorizations WHERE id=?",
                         (authorization_id,)).fetchone()
        if not a:
            raise NotFound("授权不存在")
        if a["patient_id"] != patient_id:
            raise PermissionDenied("只有本人可以裁决授权争议")
        if a["status"] != "disputed":
            raise BusinessRule("该授权不处于争议状态")
        if decision == "restore":
            conn.execute("UPDATE authorizations SET status='active' WHERE id=?",
                         (authorization_id,))
        else:
            conn.execute(
                "UPDATE authorizations SET status='revoked', revoked_at=? WHERE id=?",
                (now(), authorization_id))
    audit.log(db, actor_id=patient_id, actor_role="patient",
              action="resolve_dispute", target_type="authorization",
              target_id=authorization_id, detail=decision)


# ---------------------------------------------------------------- 下载链接

def create_link(db: Database, *, patient_id: int, grantee_id: int,
                report_id: int, expires_hours: int = 72) -> dict:
    """本人为被授权人生成固定到版本的下载链接。授权必须当前有效。"""
    report = db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
    if not report:
        raise NotFound("报告不存在")
    if report["patient_id"] != patient_id:
        raise PermissionDenied("只能分享本人报告")
    a = db.query_one(
        "SELECT * FROM authorizations WHERE patient_id=? AND grantee_id=?",
        (patient_id, grantee_id))
    ok, reason = access_control.is_effective(a)
    if not ok:
        raise BusinessRule(f"无法生成链接：{reason}")
    token = secrets.token_urlsafe(18)
    expires_at = _future(None, hours=expires_hours)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO download_links(token,report_id,version,authorized_id,patient_id,"
            "grantee_id,created_at,expires_at,used_count,max_uses,status) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,'valid')",
            (token, report_id, report["version"], a["id"], patient_id, grantee_id,
             now(), expires_at, 5))
        link_id = cur.lastrowid
    audit.log(db, actor_id=patient_id, actor_role="patient",
              action="create_link", target_type="link", target_id=link_id,
              detail=f"report={report_id} v{report['version']}, grantee={grantee_id}")
    return {"id": link_id, "token": token, "report_id": report_id,
            "version": report["version"], "expires_at": expires_at}


def resolve_link(db: Database, *, token: str, actor_id: int) -> dict:
    """凭链接下载。返回 {report, path, checksum}；失效一律拒绝（旧链接立即失效）。"""
    link = db.query_one("SELECT * FROM download_links WHERE token=?", (token,))
    if not link:
        audit.log(db, actor_id=actor_id, actor_role=None,
                  action="download_denied", target_type="link",
                  detail="token 不存在", allowed=False)
        raise NotFound("链接不存在")
    # 权威状态判定（不看缓存的 status 字段，重新核验授权与时间）
    if link["status"] == "revoked":
        audit.log(db, actor_id=actor_id, actor_role=None,
                  action="download_denied", target_type="link", target_id=link["id"],
                  detail="授权已撤销，链接立即失效", allowed=False)
        raise PermissionDenied("该链接因授权撤销已立即失效")
    auth = db.query_one("SELECT * FROM authorizations WHERE id=?",
                        (link["authorized_id"],))
    ok, reason = access_control.is_effective(auth)
    if not ok:
        # 惰性把链接置失效
        new_status = "revoked" if (auth and auth["status"] == "revoked") else "expired"
        db.execute("UPDATE download_links SET status=? WHERE id=? AND status='valid'",
                   (new_status, link["id"]))
        audit.log(db, actor_id=actor_id, actor_role=None,
                  action="download_denied", target_type="link", target_id=link["id"],
                  detail=reason, allowed=False)
        raise PermissionDenied(f"链接不可用：{reason}")
    if link["expires_at"] <= now():
        db.execute("UPDATE download_links SET status='expired' WHERE id=?", (link["id"],))
        audit.log(db, actor_id=actor_id, actor_role=None,
                  action="download_denied", target_type="link", target_id=link["id"],
                  detail="链接已过期", allowed=False)
        raise PermissionDenied("链接已过期")
    if link["used_count"] >= link["max_uses"]:
        db.execute("UPDATE download_links SET status='exhausted' WHERE id=?", (link["id"],))
        raise PermissionDenied("链接下载次数已用完")
    report = db.query_one("SELECT * FROM reports WHERE id=?", (link["report_id"],))
    if not report:
        raise NotFound("链接指向的报告版本不存在")
    with db.tx() as conn:
        conn.execute("UPDATE download_links SET used_count=used_count+1 WHERE id=?",
                     (link["id"],))
    audit.log(db, actor_id=actor_id, actor_role=auth["grantee_role"],
              action="download", target_type="link", target_id=link["id"],
              detail=f"report={report['id']} v{link['version']}")
    return {"report": dict(report), "path": report["pdf_path"],
            "checksum": report["checksum"], "link_id": link["id"]}
