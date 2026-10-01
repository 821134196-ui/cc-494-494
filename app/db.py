"""SQLite 数据访问层：建表、连接、事务辅助。

所有写操作都在 `with db:` 隐式事务中执行；号源竞争类操作使用
BEGIN IMMEDIATE 立即获取写锁，配合 slots 行上的条件 UPDATE 实现原子抢占。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 用户（演示数据，全部为虚构人员，不含任何真实病历）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    role        TEXT NOT NULL CHECK (role IN ('patient','doctor','family')),
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    created_at  TEXT NOT NULL
);

-- 报告：同一物理报告可有多个版本（version 递增），latest 指向当前版本
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES users(id),
    version         INTEGER NOT NULL,
    report_no       TEXT NOT NULL,
    title           TEXT NOT NULL,
    examined_at     TEXT NOT NULL,          -- 体检日期
    issued_at       TEXT NOT NULL,          -- 该版本出具时间
    supersedes      INTEGER,                -- 更正时指向旧版本 reports.id
    correction_note TEXT,                   -- 更正原因（本版为更正件时必填）
    is_correction   INTEGER NOT NULL DEFAULT 0,
    pdf_path        TEXT,                   -- 该版本对应 PDF（本地目录）
    checksum        TEXT,                   -- PDF 校验和，版本引用据此固定
    UNIQUE(report_no, version)
);

-- 指标：值随报告版本固定；解读页趋势按 metric_key 跨版本/历次体检聚合
CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY,
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    patient_id   INTEGER NOT NULL REFERENCES users(id),
    metric_key   TEXT NOT NULL,             -- 稳定标识，如 glc_fpg / sbp
    label        TEXT NOT NULL,             -- 空腹血糖
    value        REAL NOT NULL,
    unit         TEXT NOT NULL,
    ref_low      REAL,
    ref_high     REAL,
    examined_at  TEXT NOT NULL,             -- 冗余：便于趋势查询
    UNIQUE(report_id, metric_key)
);

-- 医生排班号源：一行 = 一个可预约号
CREATE TABLE IF NOT EXISTS slots (
    id           INTEGER PRIMARY KEY,
    doctor_id    INTEGER NOT NULL REFERENCES users(id),
    channel      TEXT NOT NULL CHECK (channel IN ('online','onsite')),
    start_at     TEXT NOT NULL,             -- ISO 本地时间
    end_at       TEXT NOT NULL,
    capacity     INTEGER NOT NULL DEFAULT 1,
    booked       INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','suspended','closed')),
    CHECK (booked <= capacity)
);
CREATE INDEX IF NOT EXISTS idx_slots_doctor_time ON slots(doctor_id, start_at);

-- 预约
CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES users(id),
    doctor_id     INTEGER NOT NULL REFERENCES users(id),
    slot_id       INTEGER NOT NULL REFERENCES slots(id),
    report_id     INTEGER NOT NULL REFERENCES reports(id), -- 引用具体版本
    channel       TEXT NOT NULL CHECK (channel IN ('online','onsite')),
    mode          TEXT NOT NULL DEFAULT 'interpretation',
    topics        TEXT NOT NULL DEFAULT '[]',              -- 预先勾选的指标 JSON
    status        TEXT NOT NULL DEFAULT 'booked'
                  CHECK (status IN ('booked','reschedule_requested','rescheduled',
                                    'cancelled','completed','pending_reschedule')),
    created_at    TEXT NOT NULL,
    rescheduled_from INTEGER REFERENCES appointments(id),  -- 改期后新单指向旧单
    created_by    INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_appt_slot ON appointments(slot_id);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, status);

-- 授权：本人 / 医生 / 明确授权的家属
CREATE TABLE IF NOT EXISTS authorizations (
    id           INTEGER PRIMARY KEY,
    patient_id   INTEGER NOT NULL REFERENCES users(id),
    grantee_id   INTEGER NOT NULL REFERENCES users(id),    -- 被授权人
    grantee_role TEXT NOT NULL CHECK (grantee_role IN ('doctor','family')),
    scope        TEXT NOT NULL DEFAULT 'report:read',      -- 范围
    granted_at   TEXT NOT NULL,
    expires_at   TEXT,                                     -- 可空=长期；可设到期
    revoked_at   TEXT,                                     -- 随时撤销
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','revoked','expired','disputed')),
                 -- disputed：家属授权争议期间冻结，失败关闭，须本人裁决
    UNIQUE(patient_id, grantee_id)
);

-- 下载链接：授权撤销后立即失效；到期失效；一次性可配
CREATE TABLE IF NOT EXISTS download_links (
    id           INTEGER PRIMARY KEY,
    token        TEXT NOT NULL UNIQUE,
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    version      INTEGER NOT NULL,                        -- 链接固定到版本
    authorized_id INTEGER REFERENCES authorizations(id),  -- 来源授权（撤销时级联失效）
    patient_id   INTEGER NOT NULL REFERENCES users(id),
    grantee_id   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_count   INTEGER NOT NULL DEFAULT 0,
    max_uses     INTEGER NOT NULL DEFAULT 5,
    status       TEXT NOT NULL DEFAULT 'valid'
                 CHECK (status IN ('valid','revoked','expired','exhausted'))
);

-- 解读记录（会诊记录）：医生备注与结论，须签字方生效
CREATE TABLE IF NOT EXISTS interpretations (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    patient_id     INTEGER NOT NULL REFERENCES users(id),
    doctor_id      INTEGER NOT NULL REFERENCES users(id),
    report_id      INTEGER NOT NULL REFERENCES reports(id),
    notes          TEXT NOT NULL DEFAULT '',              -- 医生备注（按指标）
    conclusion     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    signed_at      TEXT,                                  -- NULL=未签字
    signer_id      INTEGER REFERENCES users(id),
    signature      TEXT,                                  -- 签字留痕（姓名+时间戳哈希）
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','signed','amended'))
);

-- 访问日志：谁在何时以何身份访问了哪份报告/链接
CREATE TABLE IF NOT EXISTS access_logs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor_id    INTEGER REFERENCES users(id),
    actor_role  TEXT,
    action      TEXT NOT NULL,                            -- view/download/revoke...
    target_type TEXT NOT NULL,                            -- report/link/authorization...
    target_id   INTEGER,
    detail      TEXT,
    allowed     INTEGER NOT NULL
);

-- 邮件（模拟适配器落库，不真正发送）
CREATE TABLE IF NOT EXISTS mail_outbox (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    to_addr    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    related_id INTEGER,
    sent       INTEGER NOT NULL DEFAULT 1
);
"""


def now() -> str:
    """统一时间戳：UTC、ISO、精确到秒（字典序即可比较先后）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """线程安全的 SQLite 封装（check_same_thread=False + 连接锁）。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)  # 显式事务，关闭隐式管理
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        if path == ":memory:":
            self._conn.execute("PRAGMA journal_mode=MEMORY")
        self.init()

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self, immediate: bool = False):
        """序列化写事务。immediate=True 用于抢占号源等竞争场景。"""
        with self._lock:
            if immediate:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        # autocommit 模式下单语句立即落盘；若处于显式 tx() 中则参与该事务。
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
