"""应用上下文：每个请求/任务一个连接，装配各领域服务与模拟邮件。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config, db
from .access import AccessService
from .booking import BookingService
from .consents import ConsentService
from .interpretation import InterpretationService
from .mailer import MockMailer
from .versions import ReportService


class AppContext:
    def __init__(self, conn: sqlite3.Connection, mailer: MockMailer | None = None):
        self.conn = conn
        self.mailer = mailer or MockMailer(lambda: conn)
        self.access = AccessService(conn)
        self.booking = BookingService(conn, self.mailer)
        self.consents = ConsentService(conn, self.access, self.mailer)
        self.reports = ReportService(conn, self.access, self.mailer)
        self.interpretation = InterpretationService(conn, self.access, self.mailer)

    def user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE id=?",
                                 (user_id,)).fetchone()

    def by_code(self, code: str):
        return self.conn.execute("SELECT * FROM users WHERE code=?",
                                 (code,)).fetchone()

    def sweep_expired(self) -> int:
        return self.consents.sweep_expired()


def fresh_context(path=None, mailer: MockMailer | None = None) -> AppContext:
    conn = db.connect(path)
    return AppContext(conn, mailer)


@contextmanager
def app_context(path=None, shared_mailer: MockMailer | None = None):
    ctx = fresh_context(path, shared_mailer)
    try:
        yield ctx
    finally:
        ctx.conn.close()
