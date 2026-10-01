"""测试夹具：每个用例使用独立临时数据目录与数据库。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from healthcheck import config, db, timeutil
from healthcheck.appctx import AppContext
from healthcheck.mailer import MockMailer


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._monkeypatch = [
            (config, "DATA_DIR", self.dir),
            (config, "DB_PATH", self.dir / "healthcheck.db"),
            (config, "PDF_DIR", self.dir / "pdfs"),
        ]
        for mod, attr, val in self._monkeypatch:
            setattr(mod, attr, val)
        # pdfkit 的 FONT 路径基于 config.ROOT，不受影响
        config.PDF_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(config.DB_PATH)
        db.init_db(self.conn)
        self.mailer = MockMailer(lambda: self.conn)
        self.ctx = AppContext(self.conn, self.mailer)
        # 重置时钟
        timeutil.ClockControl.get().set(timeutil.at(config.DEMO_NOW_ISO))
        self.now = timeutil.now()
        self._ids: dict[tuple, int] = {}

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    # ---------- 数据构造助手 ----------
    def user(self, code, role, name=None, email=None):
        existing = self.conn.execute(
            "SELECT id FROM users WHERE code=?", (code,)).fetchone()
        if existing:
            return existing["id"]
        cur = self.conn.execute(
            """INSERT INTO users(code,role,name,email,is_demo,created_at)
               VALUES(?,?,?,?,1,?)""",
            (code, role, name or code, email or f"{code.lower()}@demo.mail",
             timeutil.iso(self.now)))
        return cur.lastrowid

    def patient(self, code="P-01", name="林晓"):
        return self.user(code, "patient", name)

    def doctor(self, code="D-2201", name="陈静"):
        return self.user(code, "doctor", name)

    def family(self, code="F-31", name="林芳"):
        return self.user(code, "family", name)

    def staff(self, code="S-90", name="质控台"):
        return self.user(code, "staff", name)

    def link_family(self, pid, fid, relation="女儿"):
        self.conn.execute(
            "INSERT INTO family_links(patient_id,family_id,relation,created_at) VALUES(?,?,?,?)",
            (pid, fid, relation, timeutil.iso(self.now)))

    def slot(self, doctor_id, start="2026-10-08T14:00:00", mode="online",
             capacity=1, status="open"):
        cur = self.conn.execute(
            """INSERT INTO slots(doctor_id,start_at,end_at,mode,capacity,
                   booked_count,status) VALUES(?,?,?,?,?,0,?)""",
            (doctor_id, start, start.replace("14:00", "14:30")
             if "14:00" in start else start, mode, capacity, status))
        return cur.lastrowid

    def metric(self, code, name, value, unit, lo, hi, flag=None, value_text=None):
        flag = flag or ("T" if value_text is not None else
                        ("L" if value < lo else "H" if value > hi else "N"))
        return dict(code=code, name=name, value=value, value_text=value_text,
                    unit=unit, ref_low=lo, ref_high=hi, ref_text=None, flag=flag)

    def metrics(self, ldl=3.2, sbp=122):
        return [
            self.metric("SBP", "收缩压", sbp, "mmHg", 90, 120),
            self.metric("LDL_C", "低密度脂蛋白胆固醇", ldl, "mmol/L", 2.1, 3.37),
            self.metric("GLU_F", "空腹血糖", 5.4, "mmol/L", 3.9, 6.1),
        ]

    def report(self, pid, code="RPT-T1", title="年度体检报告",
               exam="2026-09-10", metrics=None, actor=None):
        rid = self.conn.execute(
            "INSERT INTO reports(code,patient_id,title,exam_date,created_at) VALUES(?,?,?,?,?)",
            (code, pid, title, exam, timeutil.iso(self.now))).lastrowid
        v = self.ctx.reports.issue_original(
            report_id=rid, metrics=metrics or self.metrics(),
            actor_id=actor or self.staff())
        return rid, v

    def row(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def rows(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()
