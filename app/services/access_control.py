"""访问判定（失败关闭 fail-closed）。

可见范围仅限：患者本人、被授权医生、明确授权的家属。
授权状态 active 且（未到期）时放行；revoked / expired / disputed 一律拒绝。
"""
from __future__ import annotations

from ..db import Database, now


def _as_utc_z(dt_str: str) -> str:
    return dt_str


def is_effective(auth) -> tuple[bool, str]:
    """授权当前是否有效。返回 (有效, 原因)。"""
    if auth is None:
        return False, "不存在授权记录"
    status = auth["status"]
    if status == "revoked":
        return False, "授权已撤销"
    if status == "expired":
        return False, "授权已到期"
    if status == "disputed":
        return False, "家属授权存在争议，已暂停访问（待本人裁决）"
    expires = auth["expires_at"]
    if expires and expires <= now():
        return False, "授权已到期"
    return True, "ok"


def actor(db: Database, actor_id: int):
    return db.query_one("SELECT * FROM users WHERE id=?", (actor_id,))


def check_report_access(db: Database, *, actor_id: int, patient_id: int) -> dict:
    """返回 {allowed, reason, basis, actor_role, auth_id}。"""
    user = actor(db, actor_id)
    if user is None:
        return {"allowed": False, "reason": "用户不存在", "basis": None,
                "actor_role": None, "auth_id": None}
    role = user["role"]
    if role == "patient" and actor_id == patient_id:
        return {"allowed": True, "reason": "本人", "basis": "self",
                "actor_role": "patient", "auth_id": None}
    if role in ("doctor", "family"):
        a = db.query_one(
            "SELECT * FROM authorizations WHERE patient_id=? AND grantee_id=?",
            (patient_id, actor_id))
        ok, reason = is_effective(a)
        if ok:
            return {"allowed": True, "reason": "有效授权",
                    "basis": f"authorization:{a['id']}", "actor_role": role,
                    "auth_id": a["id"]}
        return {"allowed": False, "reason": reason, "basis": None,
                "actor_role": role, "auth_id": a["id"] if a else None}
    # 其他任何角色默认拒绝
    return {"allowed": False, "reason": "不在可访问人员范围内", "basis": None,
            "actor_role": role, "auth_id": None}
