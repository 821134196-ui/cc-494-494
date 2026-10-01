"""演示种子数据（全部虚构人员与合成数值，不含任何真实病历）。

用法：
    python -m app.seed            # 已初始化则跳过
    python -m app.seed --reset    # 删除数据库与 PDF 后重新生成
    python -m app.main            # 启动服务（首次运行自动种子）

包含：3 个年度的指标趋势、V1→V2 更正报告样例、已签字与未签字解读记录、
有效/到期/撤销/争议四种授权样例、停诊导致的待改约预约与模拟邮件。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

from . import config
from .db import Database, now
from .pdfgen import render_report_pdf
from .services import authorizations as auth_svc
from .services import reports as report_svc
from .services import scheduling as sched
from .services import interpretations as interp_svc


# ------------------------------------------------------------ 合成指标模板

def _m(key, label, value, unit, lo=None, hi=None):
    return {"metric_key": key, "label": label, "value": value, "unit": unit,
            "ref_low": lo, "ref_high": hi}


PANEL_2024 = [
    _m("glc_fpg", "空腹血糖", 5.4, "mmol/L", 3.9, 6.1),
    _m("hba1c", "糖化血红蛋白", 5.3, "%", 4.0, 6.0),
    _m("sbp", "收缩压", 122, "mmHg", 90, 139),
    _m("dbp", "舒张压", 78, "mmHg", 60, 89),
    _m("tc", "总胆固醇", 4.8, "mmol/L", None, 5.2),
    _m("tg", "甘油三酯", 1.3, "mmol/L", None, 1.7),
    _m("ldl", "低密度脂蛋白", 2.9, "mmol/L", None, 3.4),
    _m("hdl", "高密度脂蛋白", 1.35, "mmol/L", 1.0, None),
    _m("alt", "谷丙转氨酶", 22, "U/L", 7, 40),
    _m("cr", "肌酐", 76, "μmol/L", 57, 97),
]

PANEL_2025 = [
    _m("glc_fpg", "空腹血糖", 5.8, "mmol/L", 3.9, 6.1),
    _m("hba1c", "糖化血红蛋白", 5.6, "%", 4.0, 6.0),
    _m("sbp", "收缩压", 128, "mmHg", 90, 139),
    _m("dbp", "舒张压", 82, "mmHg", 60, 89),
    _m("tc", "总胆固醇", 5.0, "mmol/L", None, 5.2),
    _m("tg", "甘油三酯", 1.5, "mmol/L", None, 1.7),
    _m("ldl", "低密度脂蛋白", 3.2, "mmol/L", None, 3.4),
    _m("hdl", "高密度脂蛋白", 1.28, "mmol/L", 1.0, None),
    _m("alt", "谷丙转氨酶", 26, "U/L", 7, 40),
    _m("cr", "肌酐", 79, "μmol/L", 57, 97),
]

# 2026 初版：空腹血糖被实验室误录为 7.2（更正件将其改回 6.1）
PANEL_2026_V1 = [
    _m("glc_fpg", "空腹血糖", 7.2, "mmol/L", 3.9, 6.1),   # ← 录入错误
    _m("hba1c", "糖化血红蛋白", 5.7, "%", 4.0, 6.0),
    _m("sbp", "收缩压", 131, "mmHg", 90, 139),
    _m("dbp", "舒张压", 85, "mmHg", 60, 89),
    _m("tc", "总胆固醇", 5.4, "mmol/L", None, 5.2),       # 偏高
    _m("tg", "甘油三酯", 1.9, "mmol/L", None, 1.7),       # 偏高
    _m("ldl", "低密度脂蛋白", 3.6, "mmol/L", None, 3.4),  # 偏高
    _m("hdl", "高密度脂蛋白", 1.21, "mmol/L", 1.0, None),
    _m("alt", "谷丙转氨酶", 24, "U/L", 7, 40),
    _m("cr", "肌酐", 81, "μmol/L", 57, 97),
]

PANEL_2026_V2 = [dict(m, value=6.1) if m["metric_key"] == "glc_fpg" else m
                 for m in PANEL_2026_V1]

CORRECTION_NOTE = ("实验室信息系统录入错误：空腹血糖（glc_fpg）由 7.2 mmol/L "
                   "更正为 6.1 mmol/L，原始标本复核结果为 6.1；糖化血红蛋白及其余"
                   "指标不变。更正后空腹血糖处于参考范围上限附近，仍建议结合趋势随访。")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_pdf(report_no: str, version: int, title: str, examined_at: str,
              issued_at: str, patient_name: str, doctor_name: str | None,
              correction_note: str | None, metrics: list[dict]) -> tuple[str, str]:
    """生成 PDF 并返回 (相对路径, sha256)。"""
    os.makedirs(config.PDF_DIR, exist_ok=True)
    fname = f"{report_no}_v{version}.pdf"
    path = os.path.join(config.PDF_DIR, fname)
    tagged = []
    for m in metrics:
        mm = dict(m)
        mm["status"] = report_svc.metric_status(mm)
        tagged.append(mm)
    render_report_pdf(
        path, report_no=report_no, version=version, title=title,
        examined_at=examined_at, issued_at=issued_at,
        patient_name=patient_name, doctor_name=doctor_name,
        correction_note=correction_note, metrics=tagged,
        checksum_short="")  # 出具后回填名校验
    checksum = _sha256(path)
    # 用真实 checksum 短码重绘页脚（保证文件与其校验和同时自洽）
    render_report_pdf(
        path, report_no=report_no, version=version, title=title,
        examined_at=examined_at, issued_at=issued_at,
        patient_name=patient_name, doctor_name=doctor_name,
        correction_note=correction_note, metrics=tagged,
        checksum_short=checksum[:10])
    return os.path.join("pdfs", fname), checksum


# ---------------------------------------------------------------- 种子主流程

def seed(db: Database) -> None:
    if db.query_one("SELECT COUNT(*) AS c FROM users")["c"] > 0:
        return

    # ---- 用户（虚构）----
    users = [
        ("patient", "李明", "13800000001", "liming@example.test"),
        ("patient", "王芳", "13800000002", "wangfang@example.test"),
        ("doctor", "陈静（全科·主任医师）", "010-88001", "chenjing@example.test"),
        ("doctor", "赵磊（内分泌·副主任医师）", "010-88002", "zhaolei@example.test"),
        ("family", "李建国（患者之父）", "13900000003", "lijianguo@example.test"),
        ("family", "周小雨（患者之女）", "13900000004", "zhouxiaoyu@example.test"),
        ("family", "王强（患者之兄）", "13900000005", "wangqiang@example.test"),
    ]
    ids = {}
    for i, (role, name, phone, email) in enumerate(users, start=1):
        db.execute(
            "INSERT INTO users(id,role,name,phone,email,created_at) VALUES(?,?,?,?,?,?)",
            (i, role, name, phone, email, now()))
        ids[name] = i
    LI, WANG = 1, 2
    CHEN, ZHAO = 3, 4
    LI_DAD, WANG_DAUGHTER, WANG_BROTHER = 5, 6, 7

    # ---- 报告与 PDF（含更正样例）----
    def issue(patient_id, report_no, title, examined_at, panel, patient_name,
              doctor_name, correction_note=None, supersedes_id=None):
        # 先建一条记录拿到版本/路径，再补文件与 checksum：直接复用服务并生成 PDF
        if supersedes_id is None:
            version = 1
        else:
            version = db.query_one("SELECT version FROM reports WHERE id=?",
                                   (supersedes_id,))["version"] + 1
        rel, checksum = _make_pdf(
            report_no, version, title, examined_at,
            issued_at=now(), patient_name=patient_name, doctor_name=doctor_name,
            correction_note=correction_note, metrics=panel)
        return report_svc.issue_report(
            db, patient_id=patient_id, report_no=report_no, title=title,
            examined_at=examined_at, metrics=panel, pdf_path=rel,
            checksum=checksum, correction_note=correction_note,
            supersedes_id=supersedes_id)

    issue(LI, "TJ2024-0918", "年度体检报告", "2024-09-18", PANEL_2024,
          "李明", None)
    issue(LI, "TJ2025-0920", "年度体检报告", "2025-09-20", PANEL_2025,
          "李明", None)
    r_v1 = issue(LI, "TJ2026-0001", "年度体检报告", "2026-09-24", PANEL_2026_V1,
                 "李明", "陈静（全科·主任医师）")
    r_v2 = issue(LI, "TJ2026-0001", "年度体检报告", "2026-09-24", PANEL_2026_V2,
                 "李明", "陈静（全科·主任医师）",
                 correction_note=CORRECTION_NOTE, supersedes_id=r_v1["id"])

    wang_panel = [
        _m("glc_fpg", "空腹血糖", 5.1, "mmol/L", 3.9, 6.1),
        _m("hba1c", "糖化血红蛋白", 5.2, "%", 4.0, 6.0),
        _m("sbp", "收缩压", 118, "mmHg", 90, 139),
        _m("dbp", "舒张压", 76, "mmHg", 60, 89),
        _m("tg", "甘油三酯", 1.4, "mmol/L", None, 1.7),
        _m("alt", "谷丙转氨酶", 19, "U/L", 7, 40),
    ]
    issue(WANG, "TJ2026-0002", "年度体检报告", "2026-09-25", wang_panel,
          "王芳", "赵磊（内分泌·副主任医师）")

    # ---- 排班（今天起 14 个自然日）----
    today = datetime.now().date()
    d_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")   # 含一个过去号源
    d_to = (today + timedelta(days=14)).strftime("%Y-%m-%d")
    sched.generate_slots(db, doctor_id=CHEN, date_from=d_from, date_to=d_to,
                         capacity=1)
    sched.generate_slots(db, doctor_id=ZHAO, date_from=d_from, date_to=d_to,
                         capacity=1)

    # ---- 既往已完成 + 已签字解读记录（2025 报告）----
    past_start = "2026-09-21T10:00"
    db.execute(
        "INSERT INTO slots(doctor_id,channel,start_at,end_at,capacity,booked,status)"
        " VALUES(?,'online',?,'2026-09-21T10:30',1,1,'closed')",
        (ZHAO, past_start))
    past_slot = db.query_one("SELECT id FROM slots WHERE start_at=?", (past_start,))
    r2025 = db.query_one(
        "SELECT id FROM reports WHERE report_no='TJ2025-0920' AND version=1")
    db.execute(
        "INSERT INTO appointments(patient_id,doctor_id,slot_id,report_id,channel,"
        "topics,status,created_at) VALUES(?,?,?,?,'online',?, 'completed',?)",
        (LI, ZHAO, past_slot["id"], r2025["id"],
         '["glc_fpg","tg"]', now()))
    past_appt = db.query_one(
        "SELECT id FROM appointments WHERE slot_id=?", (past_slot["id"],))
    ts = now()
    import json as _json
    notes_2025 = _json.dumps({
        "glc_fpg": "5.8 较前次 5.4 轻度上行，仍在参考范围内；建议控制精米白面、餐后步行。",
        "tg": "1.5 接近上限，建议减少夜宵与饮酒，三个月后复查血脂。",
    }, ensure_ascii=False)
    sig = interp_svc._signature(ZHAO, LI, r2025["id"], notes_2025,
                                "糖代谢与血脂呈轻度上行趋势，目前无需用药；生活方式干预后复查。", ts)
    db.execute(
        "INSERT INTO interpretations(appointment_id,patient_id,doctor_id,report_id,"
        "notes,conclusion,created_at,signed_at,signer_id,signature,status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (past_appt["id"], LI, ZHAO, r2025["id"], notes_2025,
         "糖代谢与血脂呈轻度上行趋势，目前无需用药；生活方式干预后复查。",
         now(), ts, ZHAO, sig, "signed"))

    # ---- 未来预约 A：引用了已被更正的 V1（演示版本引用固定 + 更正提示）----
    future_slots = db.query(
        "SELECT * FROM slots WHERE doctor_id=? AND status='open' AND start_at>=? "
        "ORDER BY start_at", (CHEN, today.strftime("%Y-%m-%dT%H:%M")))
    online_slots = [s for s in future_slots if s["channel"] == "online"]
    onsite_slots = [s for s in future_slots if s["channel"] == "onsite"]
    appt_a = sched.book(db, patient_id=LI, slot_id=online_slots[3]["id"],
                        report_id=r_v1["id"], topics=["glc_fpg", "hba1c"],
                        created_by=LI)
    # 医生已起拟但尚未签字的备注（演示“未签字”状态）
    draft_notes = {
        "glc_fpg": "（草拟）注意：本单引用的是更正前 V1，空腹血糖 7.2 系录入错误，"
                   "解读时须以 V2 更正件 6.1 为准。",
        "hba1c": "糖化血红蛋白 5.7%，处于参考范围内，可结合血糖趋势综合判断。",
    }
    interp_svc.save_notes(
        db, appointment_id=appt_a["id"], doctor_id=CHEN,
        notes=json.dumps(draft_notes, ensure_ascii=False),
        conclusion="")

    # ---- 未来预约 B：引用 V2，随后因医生停诊转入待改约队列 ----
    # 从实际生成的号源中选一个“未来工作日”的现场号（避免落在周末不排班）。
    target = db.query_one(
        "SELECT * FROM slots WHERE doctor_id=? AND channel='onsite' AND status='open' "
        "AND date(start_at) > date(?) ORDER BY start_at LIMIT 1",
        (CHEN, today.strftime("%Y-%m-%d")))
    if target:
        sched.book(db, patient_id=LI, slot_id=target["id"],
                   report_id=r_v2["id"], topics=["tc", "tg", "ldl"], created_by=LI)
        suspend_date = target["start_at"][:10]
        sched.suspend_doctor_slots(
            db, doctor_id=CHEN, start_date=suspend_date, end_date=suspend_date,
            reason="医生参加突发医疗保障任务，出诊暂停一天", actor_id=CHEN)

    # ---- 授权样例 ----
    # 李明：医生陈静长期授权；父亲 30 天授权 + 一个有效下载链接
    auth_svc.grant(db, patient_id=LI, grantee_id=CHEN)
    dad = auth_svc.grant(db, patient_id=LI, grantee_id=LI_DAD, expires_days=30)
    auth_svc.create_link(db, patient_id=LI, grantee_id=LI_DAD,
                         report_id=r_v2["id"], expires_hours=72)

    # 王芳：女儿 7 天授权（有效）；哥哥家属授权处于争议冻结；
    #       曾授权赵磊医生后撤销（其下载链接同时失效，演示“旧链接立即失效”）
    auth_svc.grant(db, patient_id=WANG, grantee_id=WANG_DAUGHTER, expires_days=7)
    wang_r = db.query_one(
        "SELECT id FROM reports WHERE report_no='TJ2026-0002' AND version=1")
    disputed = auth_svc.grant(db, patient_id=WANG, grantee_id=WANG_BROTHER,
                              expires_days=90)
    auth_svc.raise_dispute(db, patient_id=WANG, authorization_id=disputed["id"],
                           actor_id=WANG_BROTHER,
                           reason="兄妹对母亲体检信息的知情范围存在分歧，申请本人裁决")
    revoked = auth_svc.grant(db, patient_id=WANG, grantee_id=ZHAO)
    auth_svc.create_link(db, patient_id=WANG, grantee_id=ZHAO,
                         report_id=wang_r["id"], expires_hours=72)
    auth_svc.revoke(db, patient_id=WANG, authorization_id=revoked["id"])


def main() -> None:
    reset = "--reset" in sys.argv
    if reset and os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    if reset and os.path.isdir(config.PDF_DIR):
        for f in os.listdir(config.PDF_DIR):
            os.remove(os.path.join(config.PDF_DIR, f))
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.PDF_DIR, exist_ok=True)
    db = Database(config.DB_PATH)
    seed(db)
    print(f"数据库: {config.DB_PATH}")
    print(f"PDF 目录: {config.PDF_DIR}")
    print("演示账号: 患者 李明(1) / 王芳(2)，医生 陈静(3) / 赵磊(4)，"
          "家属 李建国(5) / 周小雨(6) / 王强(7)")
    db.close()


if __name__ == "__main__":
    main()
