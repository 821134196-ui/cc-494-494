"""测试公共夹具：临时数据目录 + 最小数据集。"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Database  # noqa: E402
from app.pdfgen import render_report_pdf  # noqa: E402
from app.services import reports as report_svc  # noqa: E402
from app.services import scheduling as sched  # noqa: E402


def make_db(tmp: str | None = None) -> Database:
    tmp = tmp or tempfile.mkdtemp(prefix="hc-test-")
    return Database(os.path.join(tmp, "test.db"))


def seed_minimal(db: Database) -> dict:
    """两个患者、两个医生、一个家属，一份含更正件的报告 + 历史报告 + 号源。"""
    def add(role, name, email):
        cur = db.execute(
            "INSERT INTO users(role,name,phone,email,created_at) VALUES(?,?,?,?,?)",
            (role, name, None, email, "2026-09-01T00:00:00Z"))
        return cur.lastrowid

    p1 = add("patient", "患者甲", "p1@test")
    p2 = add("patient", "患者乙", "p2@test")
    doc1 = add("doctor", "医生甲", "d1@test")
    doc2 = add("doctor", "医生乙", "d2@test")
    fam1 = add("family", "家属甲", "f1@test")

    self_data_dir = tempfile.mkdtemp(prefix="hc-data-")
    pdf_dir = os.path.join(self_data_dir, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)

    def issue(patient, no, glc_value, *, correction=None, supersedes=None,
              examined="2026-09-24"):
        metrics = [
            {"metric_key": "glc_fpg", "label": "空腹血糖", "value": glc_value,
             "unit": "mmol/L", "ref_low": 3.9, "ref_high": 6.1},
            {"metric_key": "tc", "label": "总胆固醇", "value": 5.4,
             "unit": "mmol/L", "ref_low": None, "ref_high": 5.2},
        ]
        ver = 1
        if supersedes is not None:
            ver = db.query_one("SELECT version FROM reports WHERE id=?",
                               (supersedes,))["version"] + 1
        fname = f"{no}_v{ver}.pdf".replace("/", "_")
        abspath = os.path.join(pdf_dir, fname)
        tagged = [dict(m, status=report_svc.metric_status(m)) for m in metrics]
        render_report_pdf(
            abspath, report_no=no, version=ver, title="年度体检报告",
            examined_at=examined, issued_at="2026-09-25T00:00:00Z",
            patient_name="患者甲", doctor_name=None,
            correction_note=correction, metrics=tagged, checksum_short="test")
        checksum = hashlib.sha256(open(abspath, "rb").read()).hexdigest()
        r = report_svc.issue_report(
            db, patient_id=patient, report_no=no, title="年度体检报告",
            examined_at=examined, metrics=metrics,
            pdf_path=os.path.join("pdfs", fname), checksum=checksum,
            correction_note=correction, supersedes_id=supersedes)
        r["_abs_pdf"] = abspath
        return r

    v1 = issue(p1, "R-001", 7.2)
    v2 = issue(p1, "R-001", 6.1,
               correction="空腹血糖录入错误，7.2 更正为 6.1", supersedes=v1["id"])
    issue(p1, "R-OLD", 5.6, examined="2025-09-20")
    sched.generate_slots(db, doctor_id=doc1, date_from="2026-10-05",
                         date_to="2026-10-09", capacity=1)
    return {"p1": p1, "p2": p2, "doc1": doc1, "doc2": doc2, "fam1": fam1,
            "v1": v1, "v2": v2, "pdfdir": pdf_dir, "data_dir": self_data_dir}
