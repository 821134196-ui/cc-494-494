"""访问审计日志。"""
from __future__ import annotations

from .db import Database, now


def log(db: Database, *, actor_id: int | None, actor_role: str | None,
        action: str, target_type: str, target_id: int | None = None,
        detail: str | None = None, allowed: bool = True) -> None:
    db.execute(
        "INSERT INTO access_logs(ts,actor_id,actor_role,action,target_type,target_id,detail,allowed)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now(), actor_id, actor_role, action, target_type, target_id, detail, 1 if allowed else 0),
    )
