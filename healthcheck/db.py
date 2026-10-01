"""SQLite 持久化层。

WAL 模式 + 立即事务（BEGIN IMMEDIATE）保证号源竞争下的原子性：
抢号以一条条件 UPDATE 完成，数据库写锁串行化并发，恰好一方成功。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 用户：患者 / 医生 / 家属 / 体检中心工作人员
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('patient','doctor','family','staff')),
  name TEXT NOT NULL,
  phone TEXT, email TEXT,
  title TEXT,            -- 医生职称
  department TEXT,       -- 医生科室
  bio TEXT,
  is_demo INTEGER NOT NULL DEFAULT 1,  -- 1=虚构演示身份
  created_at TEXT NOT NULL
);

-- 家属关系（登记，不代表有访问权；访问权由 consents 决定）
CREATE TABLE IF NOT EXISTS family_links(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES users(id),
  family_id INTEGER NOT NULL REFERENCES users(id),
  relation TEXT NOT NULL,           -- 之子/之女/配偶…
  created_at TEXT NOT NULL,
  UNIQUE(patient_id, family_id)
);

-- 体检报告（逻辑单据，跨版本稳定）
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  exam_date TEXT NOT NULL,          -- 体检日期
  current_version_id INTEGER,       -- 当前有效版本
  created_at TEXT NOT NULL
);

-- 报告版本：原件 / 更正件；版本引用不可变
CREATE TABLE IF NOT EXISTS report_versions(
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES reports(id),
  version_no INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('original','correction')),
  correction_reason TEXT,
  pdf_path TEXT,                    -- 本地目录中的相对文件名
  pdf_sha256 TEXT,
  issued_at TEXT NOT NULL,
  issued_by INTEGER REFERENCES users(id),
  supersedes_id INTEGER REFERENCES report_versions(id),
  UNIQUE(report_id, version_no)
);

-- 指标数值（隶属于具体版本；趋势取各报告当前版本）
CREATE TABLE IF NOT EXISTS metrics(
  id INTEGER PRIMARY KEY,
  report_version_id INTEGER NOT NULL REFERENCES report_versions(id),
  code TEXT NOT NULL,               -- LDL_C / SBP …
  name TEXT NOT NULL,
  value REAL, value_text TEXT,      -- 数值或文本型结果
  unit TEXT,
  ref_low REAL, ref_high REAL, ref_text TEXT,
  flag TEXT NOT NULL DEFAULT 'N' CHECK(flag IN ('N','H','L','T')), -- T=文本
  sort_no INTEGER NOT NULL DEFAULT 0
);

-- 号源日历
CREATE TABLE IF NOT EXISTS slots(
  id INTEGER PRIMARY KEY,
  doctor_id INTEGER NOT NULL REFERENCES users(id),
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('online','onsite')),
  capacity INTEGER NOT NULL DEFAULT 1,
  booked_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
     CHECK(status IN ('open','blocked','canceled')), -- blocked=维护不可约; canceled=医生停诊
  note TEXT,
  UNIQUE(doctor_id, start_at, mode)
);
CREATE INDEX IF NOT EXISTS idx_slots_cal ON slots(doctor_id, start_at);

-- 预约
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES users(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id),
  slot_id INTEGER REFERENCES slots(id),  -- 待改约时可空（原号源已释放）
  report_id INTEGER NOT NULL REFERENCES reports(id),
  report_version_id INTEGER NOT NULL REFERENCES report_versions(id), -- 预约时固化引用
  mode TEXT NOT NULL CHECK(mode IN ('online','onsite')),
  indicators TEXT NOT NULL DEFAULT '[]', -- 预先勾选的指标 code（JSON）
  status TEXT NOT NULL CHECK(status IN
     ('booked','reschedule_pending','completed','canceled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reschedule_count INTEGER NOT NULL DEFAULT 0,
  pending_reason TEXT                  -- 进入待改约的原因
);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_appt_doctor ON appointments(doctor_id, status);

-- 预约/号源事件流水（改期释放与锁定、停诊影响均可审计）
CREATE TABLE IF NOT EXISTS appointment_events(
  id INTEGER PRIMARY KEY,
  appointment_id INTEGER NOT NULL REFERENCES appointments(id),
  ts TEXT NOT NULL,
  actor_id INTEGER REFERENCES users(id),
  type TEXT NOT NULL,   -- booked/reschedule_release/reschedule_lock/canceled/doctor_suspended/completed
  slot_id INTEGER, detail TEXT
);

-- 授权：本人对医生 / 家属的访问许可
CREATE TABLE IF NOT EXISTS consents(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES users(id),
  grantee_id INTEGER NOT NULL REFERENCES users(id),
  grantee_kind TEXT NOT NULL CHECK(grantee_kind IN ('doctor','family')),
  scope TEXT NOT NULL DEFAULT 'view,download',
  status TEXT NOT NULL CHECK(status IN
     ('active','revoked','expired','disputed')),
  granted_at TEXT NOT NULL,
  expires_at TEXT,                    -- 可设到期；NULL=当次有效但可撤销
  revoked_at TEXT, revoke_reason TEXT,
  note TEXT,
  UNIQUE(patient_id, grantee_id)
);

-- 家属授权争议
CREATE TABLE IF NOT EXISTS consent_disputes(
  id INTEGER PRIMARY KEY,
  consent_id INTEGER NOT NULL REFERENCES consents(id),
  raised_by INTEGER NOT NULL REFERENCES users(id),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','resolved')),
  resolution TEXT,                    -- patient_confirmed / patient_revoked
  resolved_at TEXT,
  created_at TEXT NOT NULL
);

-- 下载链接（不透明令牌；授权撤销/到期/旧版本即时失效）
CREATE TABLE IF NOT EXISTS download_links(
  id INTEGER PRIMARY KEY,
  token TEXT UNIQUE NOT NULL,
  report_version_id INTEGER NOT NULL REFERENCES report_versions(id),
  consent_id INTEGER REFERENCES consents(id),  -- 本人下载时可空
  viewer_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'active'
     CHECK(status IN ('active','void')),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  voided_at TEXT,
  void_reason TEXT,
  download_count INTEGER NOT NULL DEFAULT 0
);

-- 解读记录（会诊记录须签字方为定稿）
-- 同一预约允许存在多条（作废留痕 + 新建），但任意时刻至多一条“当前记录”
CREATE TABLE IF NOT EXISTS interpretation_sessions(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  appointment_id INTEGER NOT NULL REFERENCES appointments(id),
  patient_id INTEGER NOT NULL REFERENCES users(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id),
  report_version_id INTEGER NOT NULL REFERENCES report_versions(id), -- 解读时引用版本固化
  summary TEXT,
  advice TEXT,
  status TEXT NOT NULL CHECK(status IN ('draft','signed','voided')),
  is_current INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  signed_at TEXT,
  signer_id INTEGER REFERENCES users(id),
  unsigned_reason TEXT               -- 未签字原因说明（如“会诊待上级医师复核”）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_itp_current ON interpretation_sessions(appointment_id)
  WHERE is_current=1;

-- 指标级医生备注（与趋势并置展示）
CREATE TABLE IF NOT EXISTS interpretation_notes(
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES interpretation_sessions(id),
  metric_code TEXT NOT NULL,
  content TEXT NOT NULL,
  UNIQUE(session_id, metric_code)
);

-- 访问日志（查看/下载/拒绝均可追溯）
CREATE TABLE IF NOT EXISTS access_logs(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_id INTEGER REFERENCES users(id),
  actor_role TEXT,
  action TEXT NOT NULL,          -- report.view / report.deny / download.ok / download.deny ...
  resource_type TEXT, resource_id TEXT,
  result TEXT NOT NULL CHECK(result IN ('allow','deny')),
  reason TEXT, ip TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON access_logs(ts);
CREATE INDEX IF NOT EXISTS idx_log_actor ON access_logs(actor_id);

-- 模拟邮件外发箱（MockMailer 写入）
CREATE TABLE IF NOT EXISTS email_outbox(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  to_addr TEXT NOT NULL,
  to_user_id INTEGER REFERENCES users(id),
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL,
  related_id TEXT,
  read_at TEXT
);
"""

_init_lock = threading.Lock()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection):
    with _init_lock:
        conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """写事务。

    最外层使用 BEGIN IMMEDIATE（立即取保留锁，冲突由 busy_timeout 串行化）；
    已在事务中时退化为 SAVEPOINT，支持服务间嵌套调用且语义为原子子事务。
    """
    if conn.in_transaction:
        sp = f"sp_{id(conn)}_{tx._depth[0]}"
        tx._depth[0] += 1
        conn.execute(f"SAVEPOINT {sp}")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            raise
        finally:
            tx._depth[0] -= 1
        return

    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


tx._depth = [0]
