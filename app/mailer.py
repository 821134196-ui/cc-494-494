"""模拟邮件适配器：不真正发送，落库 mail_outbox，供演示与测试断言。"""
from __future__ import annotations

from .db import Database, now


class MailAdapter:
    def __init__(self, db: Database):
        self.db = db

    def send(self, to_addr: str, subject: str, body: str, related_id: int | None = None) -> int:
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO mail_outbox(ts,to_addr,subject,body,related_id,sent) "
                "VALUES(?,?,?,?,?,1)",
                (now(), to_addr, subject, body, related_id),
            )
            return cur.lastrowid

    def list_for(self, to_addr: str, limit: int = 20) -> list:
        return self.db.query(
            "SELECT * FROM mail_outbox WHERE to_addr=? ORDER BY id DESC LIMIT ?",
            (to_addr, limit),
        )

    def all_outbox(self, limit: int = 50) -> list:
        return self.db.query("SELECT * FROM mail_outbox ORDER BY id DESC LIMIT ?", (limit,))
