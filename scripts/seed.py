"""演示数据种子（全部为虚构数据，不含任何真实病历）。

用法：
    python3 scripts/seed.py            # 重建数据库与 PDF 到 data/
    HC_DATA_DIR=/tmp/x python3 scripts/seed.py

包含：
- 2 名虚构患者、2 名医生、3 名家属、1 名体检中心工作人员；
- 患者 2023–2026 年度体检报告（指标可看趋势）；
- 2026 报告“更正样例”：v1 原件（LDL-C 串号错误）与 v2 更正件，
  两份 PDF 都在 data/pdfs，旧链接随更正如期失效；
- 号源日历（线上/现场）、已预约、医生停诊→待改约、
  已签字与未签字解读记录；
- 授权：有效（带到期）、争议冻结、已撤销、已过期；
- 下载链接：有效、撤销作废、旧版本作废；
- 模拟邮箱通知。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from healthcheck import config, db, reports as report_render, timeutil
from healthcheck.appctx import AppContext
from healthcheck.mailer import MockMailer


def m(code, name, value, unit, lo, hi, flag=None, value_text=None, ref_text=None):
    if flag is None:
        if value_text is not None:
            flag = "T"
        elif value < lo:
            flag = "L"
        elif value > hi:
            flag = "H"
        else:
            flag = "N"
    return dict(code=code, name=name, value=value, value_text=value_text,
                unit=unit, ref_low=lo, ref_high=hi, ref_text=ref_text, flag=flag)


# 多年度指标（虚构）。2026 给出“录入错误值”，更正时改回正确值。
def yearly_metrics(year: int, ldl_2026_wrong: bool = False):
    table = {
        2023: dict(SBP=118, LDL=2.8, GLU=5.2, BMI=22.4, HDL=1.52, TG=1.1, AST=22),
        2024: dict(SBP=122, LDL=3.1, GLU=5.4, BMI=23.1, HDL=1.45, TG=1.3, AST=24),
        2025: dict(SBP=125, LDL=3.6, GLU=5.5, BMI=23.8, HDL=1.38, TG=1.6, AST=25),
        2026: dict(SBP=128, LDL=5.8 if ldl_2026_wrong else 4.1,
                   GLU=5.6, BMI=24.2, HDL=1.31, TG=1.9, AST=26),
    }[year]
    return [
        m("SBP", "收缩压", table["SBP"], "mmHg", 90, 120),
        m("DBP", "舒张压", 76 + (year - 2023), "mmHg", 60, 80),
        m("LDL_C", "低密度脂蛋白胆固醇", table["LDL"], "mmol/L", 2.1, 3.37),
        m("HDL_C", "高密度脂蛋白胆固醇", table["HDL"], "mmol/L", 1.03, 1.55),
        m("TG", "甘油三酯", table["TG"], "mmol/L", 0.45, 1.7),
        m("GLU_F", "空腹血糖", table["GLU"], "mmol/L", 3.9, 6.1),
        m("BMI", "体重指数", table["BMI"], "kg/m²", 18.5, 23.9),
        m("AST", "谷草转氨酶", table["AST"], "U/L", 15, 40),
        m("URINE", "尿常规", None, None, None, None,
          value_text="阴性", ref_text="阴性"),
    ]


ZHOU_METRICS = [
    m("SBP", "收缩压", 138, "mmHg", 90, 120),
    m("DBP", "舒张压", 88, "mmHg", 60, 80),
    m("LDL_C", "低密度脂蛋白胆固醇", 3.9, "mmol/L", 2.1, 3.37),
    m("GLU_F", "空腹血糖", 6.6, "mmol/L", 3.9, 6.1),
    m("BMI", "体重指数", 26.8, "kg/m²", 18.5, 23.9),
    m("URINE", "尿常规", None, None, None, None,
      value_text="葡萄糖 ±", ref_text="阴性", flag="T"),
]


def wipe():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    for p in (config.PDF_DIR).glob("*.pdf"):
        p.unlink()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(config.DB_PATH) + suffix)
        if f.exists():
            f.unlink()


def add_user(conn, **kw):
    cols = ("code", "role", "name", "phone", "email", "title", "department", "bio")
    vals = {c: kw.get(c) for c in cols}
    cur = conn.execute(
        """INSERT INTO users(code,role,name,phone,email,title,department,bio,is_demo,created_at)
           VALUES(:code,:role,:name,:phone,:email,:title,:department,:bio,1,:created_at)""",
        {**vals, "created_at": timeutil.iso(timeutil.now())})
    return cur.lastrowid


def add_report(conn, code, patient_id, title, exam_date):
    return conn.execute(
        "INSERT INTO reports(code,patient_id,title,exam_date,created_at) VALUES(?,?,?,?,?)",
        (code, patient_id, title, exam_date, timeutil.iso(timeutil.now()))).lastrowid


def main():
    wipe()
    conn = db.connect(config.DB_PATH)
    db.init_db(conn)
    mailer = MockMailer(lambda: conn)
    ctx = AppContext(conn, mailer)

    # ---------- 用户 ----------
    lin = add_user(conn, code="P-1001", role="patient", name="林晓",
                   phone="138-0000-1001", email="linxiao@demo.mail")
    zhou = add_user(conn, code="P-1002", role="patient", name="周明",
                    phone="138-0000-1002", email="zhouming@demo.mail")
    chen = add_user(conn, code="D-2201", role="doctor", name="陈静",
                    title="主任医师", department="心血管内科",
                    email="chenjing@demo.mail",
                    bio="擅长血脂异常、高血压的长期管理与报告解读")
    wang = add_user(conn, code="D-2202", role="doctor", name="王磊",
                    title="副主任医师", department="内分泌科",
                    email="wanglei@demo.mail",
                    bio="擅长糖尿病前期与代谢综合征的生活方式干预")
    fang = add_user(conn, code="F-3101", role="family", name="林芳",
                    phone="138-0000-3101", email="linfang@demo.mail")
    qiang = add_user(conn, code="F-3102", role="family", name="林强",
                     phone="138-0000-3102", email="linqiang@demo.mail")
    hua = add_user(conn, code="F-3103", role="family", name="李华",
                   phone="138-0000-3103", email="lihua@demo.mail")
    ting = add_user(conn, code="F-3201", role="family", name="周婷",
                    phone="138-0000-3201", email="zhouting@demo.mail")
    staff = add_user(conn, code="S-9001", role="staff", name="体检中心质控台",
                     email="qc@demo.mail")

    # ---------- 家属关系 ----------
    for fid, rel in [(fang, "女儿"), (qiang, "儿子"), (hua, "表弟")]:
        conn.execute(
            "INSERT INTO family_links(patient_id,family_id,relation,created_at) VALUES(?,?,?,?)",
            (lin, fid, rel, timeutil.iso(timeutil.now())))
    conn.execute(
        "INSERT INTO family_links(patient_id,family_id,relation,created_at) VALUES(?,?,?,?)",
        (zhou, ting, "配偶", timeutil.iso(timeutil.now())))

    # ---------- 报告：林晓 2023–2026 ----------
    lin_report_ids = {}
    for year in (2023, 2024, 2025, 2026):
        rid = add_report(conn, f"RPT-LX-{year}", lin,
                         f"{year} 年度健康体检报告", f"{year}-09-{(12 + year - 2023):02d}")
        lin_report_ids[year] = rid

    # 2026 先签发“错误原件”（LDL-C 串号为 5.8），其余年度正常
    for year in (2023, 2024, 2025):
        ctx.reports.issue_original(
            report_id=lin_report_ids[year], metrics=yearly_metrics(year),
            actor_id=staff)
    v_2026 = ctx.reports.issue_original(
        report_id=lin_report_ids[2026],
        metrics=yearly_metrics(2026, ldl_2026_wrong=True), actor_id=staff)

    # 周明 2026 报告
    zhou_rid = add_report(conn, "RPT-ZM-2026", zhou,
                          "2026 年度健康体检报告", "2026-09-18")
    ctx.reports.issue_original(report_id=zhou_rid, metrics=ZHOU_METRICS,
                               actor_id=staff)

    # ---------- 授权 ----------
    # 陈静医生：解读用授权（长期有效，可撤销）
    c_doctor = ctx.consents.grant(
        patient_id=lin, grantee_id=chen, scope="view,download",
        note="年度报告解读")
    # 女儿林芳：带到期时间
    c_fang = ctx.consents.grant(
        patient_id=lin, grantee_id=fang, scope="view,download",
        expires_at="2026-12-31T23:59:59", note="便于家属陪同复诊")
    # 儿子林强：授权后发生争议，当前冻结
    c_qiang = ctx.consents.grant(
        patient_id=lin, grantee_id=qiang, scope="view,download",
        expires_days=90, note="异地家属临时查看")
    ctx.consents.raise_dispute(
        c_qiang["id"], raised_by=qiang,
        reason="父亲称未在手机上确认过该授权，要求先暂停访问，核实后再恢复")
    # 表弟李华：曾授权，已撤销（链接随之失效）
    c_hua = ctx.consents.grant(
        patient_id=lin, grantee_id=hua, scope="view,download", note="代为打印一次")
    # 王磊医生：周明的解读授权（有效）
    ctx.consents.grant(patient_id=zhou, grantee_id=wang, scope="view,download")
    # 周婷：周明配偶授权，已过期（历史记录，直接插入）
    conn.execute(
        """INSERT INTO consents(code,patient_id,grantee_id,grantee_kind,scope,status,
               granted_at,expires_at,note)
           VALUES(?,?,?,?,?, 'expired', ?,?,?)""",
        ("AUTH-EXPIRED-ZT", zhou, ting, "family", "view,download",
         "2026-03-31T09:00:00", "2026-09-30T23:59:59", "半年度家属查看，已到期"))

    # ---------- 下载链接（更正前先为 v1 旧版本建一个本人链接） ----------
    lin_u = conn.execute("SELECT * FROM users WHERE id=?", (lin,)).fetchone()
    rep2026 = conn.execute("SELECT * FROM reports WHERE id=?",
                           (lin_report_ids[2026],)).fetchone()
    old_self_link = ctx.access.issue_download_link(rep2026, lin_u, v_2026)
    # 李华的链接（撤销后失效）
    hua_u = conn.execute("SELECT * FROM users WHERE id=?", (hua,)).fetchone()
    hua_link = ctx.access.issue_download_link(rep2026, hua_u, v_2026)

    # ---------- 更正 2026 报告（样例核心） ----------
    corrected = ctx.reports.issue_correction(
        report_id=lin_report_ids[2026], actor_id=staff,
        reason="实验室标本编号串号：LDL-C 误录为 5.8 mmol/L，"
               "经复核原始图谱与留样，正确结果为 4.1 mmol/L，其余指标无误。",
        metrics=yearly_metrics(2026, ldl_2026_wrong=False))

    # 撤销李华授权（其链接立即失效）
    ctx.consents.revoke(c_hua["id"], patient_id=lin,
                        reason="仅委托打印一次，打印完成后撤销")

    # 更正后的有效链接：本人、女儿、陈静医生
    cur2026 = conn.execute("SELECT * FROM report_versions WHERE id=?",
                           (corrected["id"],)).fetchone()
    link_self = ctx.access.issue_download_link(rep2026, lin_u, cur2026)
    fang_u = conn.execute("SELECT * FROM users WHERE id=?", (fang,)).fetchone()
    link_fang = ctx.access.issue_download_link(rep2026, fang_u, cur2026)
    chen_u = conn.execute("SELECT * FROM users WHERE id=?", (chen,)).fetchone()
    link_doctor = ctx.access.issue_download_link(rep2026, chen_u, cur2026)

    # 周婷过期授权清扫（演示惰性/主动到期）
    ctx.consents.sweep_expired()

    # ---------- 号源日历 ----------
    def slot(doctor, day, hhmm, mode, capacity=1, status="open", booked=0, note=None):
        start = f"2026-{day}T{hhmm}:00"
        h, mi = map(int, hhmm.split(":"))
        end_h, end_m = (h, mi + 30) if mi < 30 else (h + 1, (mi + 30) % 60)
        end = f"2026-{day}T{end_h:02d}:{end_m:02d}:00"
        return conn.execute(
            """INSERT INTO slots(doctor_id,start_at,end_at,mode,capacity,booked_count,
                   status,note) VALUES(?,?,?,?,?,?,?,?)""",
            (doctor, start, end, mode, capacity, booked, status, note)).lastrowid

    # 陈静：线上 + 现场，未来两周
    s_chen_online = slot(chen, "10-08", "14:00", "online")
    slot(chen, "10-08", "14:30", "online")
    slot(chen, "10-10", "09:30", "onsite")
    slot(chen, "10-12", "10:00", "online")
    slot(chen, "10-15", "15:30", "onsite")
    slot(chen, "10-17", "11:00", "online", status="blocked", note="科室会议占用")
    # 王磊
    slot(wang, "10-05", "10:00", "onsite")
    slot(wang, "10-09", "16:00", "online")
    slot(wang, "10-13", "09:00", "onsite")
    slot(wang, "10-16", "14:00", "online")

    # 历史号源（供已完成解读使用）
    s_past_chen = conn.execute(
        """INSERT INTO slots(doctor_id,start_at,end_at,mode,capacity,booked_count,status)
           VALUES(?,?,?,?,?,1,'open')""",
        (chen, "2025-10-12T10:00:00", "2025-10-12T10:30:00", "onsite", 1)).lastrowid
    s_past_wang = conn.execute(
        """INSERT INTO slots(doctor_id,start_at,end_at,mode,capacity,booked_count,status)
           VALUES(?,?,?,?,?,1,'open')""",
        (wang, "2026-09-28T10:00:00", "2026-09-28T10:30:00", "onsite", 1)).lastrowid

    # ---------- 预约 ----------
    # 林晓：即将到来的线上解读（预先勾选指标）
    appt_up = ctx.booking.book(
        patient_id=lin, slot_id=s_chen_online,
        report_id=lin_report_ids[2026],
        indicators=["LDL_C", "SBP", "TG"])

    # 林晓：2025 年度解读已完成，会诊记录已签字（趋势页并置备注）
    nowiso = timeutil.iso(timeutil.at("2025-10-12T10:35:00"))
    a_old = conn.execute(
        """INSERT INTO appointments(code,patient_id,doctor_id,slot_id,report_id,
               report_version_id,mode,indicators,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'completed',?,?)""",
        ("APT-2025-LX", lin, chen, s_past_chen, lin_report_ids[2025],
         conn.execute("SELECT id FROM report_versions WHERE report_id=?",
                      (lin_report_ids[2025],)).fetchone()["id"],
         "onsite", json.dumps(["LDL_C", "SBP"], ensure_ascii=False),
         nowiso, nowiso)).lastrowid
    sess_old = conn.execute(
        """INSERT INTO interpretation_sessions(code,appointment_id,patient_id,doctor_id,
               report_version_id,summary,advice,status,created_at,signed_at,signer_id)
           VALUES(?,?,?,?,?,?,?, 'signed', ?,?,?)""",
        ("ITP-2025-LX", a_old, lin, chen,
         conn.execute("SELECT current_version_id FROM reports WHERE id=?",
                      (lin_report_ids[2025],)).fetchone()["current_version_id"],
         "血压与 LDL-C 连续三年轻度上升，目前处于生活方式干预窗口，暂不需要药物治疗。",
         "1) 每周 150 分钟中等强度有氧运动；2) 减少反式脂肪与精糖摄入；"
         "3) 三个月后复查血脂四项；4) 家庭监测晨起血压并记录。",
         nowiso, nowiso, chen)).lastrowid
    for code_, content in [
        ("LDL_C", "2023 年 2.8、2025 年 3.6，年均约 +0.4，趋势需要重视，但仍属一级预防范围。"),
        ("SBP", "118 → 125 mmHg，暂未达高血压诊断标准，建议家庭监测排除白大衣效应。"),
        ("TG", "1.6 mmol/L 接近上限，优先控制含糖饮料与饮酒。"),
    ]:
        conn.execute(
            "INSERT INTO interpretation_notes(session_id,metric_code,content) VALUES(?,?,?)",
            (sess_old, code_, content))

    # 周明：2026-09-28 已完成解读，但记录未签字（待上级复核）
    a_zhou = conn.execute(
        """INSERT INTO appointments(code,patient_id,doctor_id,slot_id,report_id,
               report_version_id,mode,indicators,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'completed',?,?)""",
        ("APT-ZM-0928", zhou, wang, s_past_wang, zhou_rid,
         conn.execute("SELECT current_version_id FROM reports WHERE id=?",
                      (zhou_rid,)).fetchone()["current_version_id"],
         "onsite", json.dumps(["GLU_F", "BMI", "SBP"], ensure_ascii=False),
         timeutil.iso(timeutil.at("2026-09-28T10:30:00")),
         timeutil.iso(timeutil.at("2026-09-28T10:30:00")))).lastrowid
    conn.execute(
        """INSERT INTO interpretation_sessions(code,appointment_id,patient_id,doctor_id,
               report_version_id,summary,advice,status,created_at,unsigned_reason)
           VALUES(?,?,?,?,?,?,?, 'draft', ?,?)""",
        ("ITP-ZM-DRAFT", a_zhou, zhou, wang,
         conn.execute("SELECT current_version_id FROM reports WHERE id=?",
                      (zhou_rid,)).fetchone()["current_version_id"],
         "空腹血糖 6.6 mmol/L 达到糖尿病前期范围，合并超重与血压偏高，建议完善糖化血红蛋白。",
         "",  # 建议尚未补全，因此不能签字
         timeutil.iso(timeutil.at("2026-09-28T10:40:00")),
         "会诊记录待上级医师复核，建议项补全后签字"))

    # ---------- 医生停诊 → 待改约 ----------
    s_suspend = slot(wang, "10-05", "10:30", "onsite")
    a_susp = ctx.booking.book(
        patient_id=zhou, slot_id=s_suspend, report_id=zhou_rid,
        indicators=["GLU_F", "SBP"])
    ctx.booking.suspend_slot(
        s_suspend, reason="医师临时参加院内外会诊，上午门诊停诊", actor_id=staff)

    # 几条访问日志：无授权家属被拒
    ctx.access.log(hua, "report.view", "deny", actor_role="family",
                   resource_type="report", resource_id=lin_report_ids[2026],
                   reason="no_consent", detail="种子：撤销后尝试访问")
    ctx.access.log(ting, "report.view", "deny", actor_role="family",
                   resource_type="report", resource_id=zhou_rid,
                   reason="consent_expired", detail="种子：授权到期")

    n_pdf = len(list(config.PDF_DIR.glob("*.pdf")))
    print("种子完成：")
    print(f"  数据库     {config.DB_PATH}")
    print(f"  PDF 文件   {n_pdf} 个（含 RPT-LX-2026-v1 原件 与 v2 更正件）")
    print(f"  患者=林晓(P-1001) 周明(P-1002)；医生=陈静(D-2201) 王磊(D-2202)")
    print(f"  停诊待改约预约：{a_susp['code']}")
    print(f"  有效下载链接(本人)：/d/{link_self}")
    print("  全部人员与数据均为虚构演示数据。")


if __name__ == "__main__":
    main()
