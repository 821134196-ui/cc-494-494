"""模拟邮件适配器。

不连接真实 SMTP：所有外发邮件写入 SQLite email_outbox，
并在页面“模拟邮箱”中可查收。提供与真实适配器一致的 send 接口，
便于替换；同时支持一次性“外发钩子”用于测试断言。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from . import timeutil


class MockMailer:
    def __init__(self, get_conn: Callable[[], sqlite3.Connection]):
        self._get_conn = get_conn
        self.last: dict | None = None  # 最近一封（便捷断言）

    def send(self, to_addr: str, subject: str, body: str, kind: str = "generic",
             to_user_id: int | None = None, related_id: str | None = None) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO email_outbox(ts,to_addr,to_user_id,subject,body,kind,related_id)
               VALUES(?,?,?,?,?,?,?)""",
            (timeutil.iso(timeutil.now()), to_addr, to_user_id, subject, body,
             kind, related_id),
        )
        mail = {
            "id": cur.lastrowid, "to": to_addr, "subject": subject,
            "body": body, "kind": kind, "related_id": related_id,
        }
        self.last = mail
        return cur.lastrowid

    # ---- 业务模板 ----
    def send_booking_confirm(self, patient, appt_code, doctor_name, when, mode_cn):
        return self.send(
            patient["email"],
            "【体检解读】预约成功确认",
            f"{patient['name']} 您好：\n\n"
            f"您已成功预约 {doctor_name} 医师的体检报告解读（{mode_cn}）。\n"
            f"时间：{when}\n预约号：{appt_code}\n\n"
            f"如需改期，请至少提前 2 小时操作，原号源将立即释放。\n"
            f"（演示系统邮件，无需回复）",
            kind="booking_confirm", to_user_id=patient["id"], related_id=appt_code)

    def send_reschedule(self, patient, appt_code, doctor_name, old_when, new_when):
        return self.send(
            patient["email"],
            "【体检解读】改期成功",
            f"{patient['name']} 您好：\n\n"
            f"您的预约 {appt_code} 已由 {old_when} 改期至 {new_when}（{doctor_name} 医师）。\n"
            f"原号源已释放，新号源已锁定。\n（演示系统邮件）",
            kind="reschedule", to_user_id=patient["id"], related_id=appt_code)

    def send_doctor_suspended(self, patient, appt_code, doctor_name, slot_when, reason):
        return self.send(
            patient["email"],
            "【体检解读】医生停诊通知 · 请改约",
            f"{patient['name']} 您好：\n\n"
            f"{doctor_name} 医师于 {slot_when} 的号源临时停诊（原因：{reason}）。\n"
            f"您的预约 {appt_code} 已转为“待改约”，原号源已释放，"
            f"请在预约页选择新时段；改约不额外收费。\n（演示系统邮件）",
            kind="doctor_suspended", to_user_id=patient["id"], related_id=appt_code)

    def send_consent_granted(self, grantee, patient_name, scope_cn, expires_text):
        tail = f"有效期至 {expires_text}" if expires_text else "本次有效，可随时被撤销"
        return self.send(
            grantee["email"],
            "【体检解读】您获得一份报告访问授权",
            f"{grantee['name']} 您好：\n\n"
            f"{patient_name} 已授权您访问其体检报告（权限：{scope_cn}）。{tail}。\n"
            f"授权可被患者随时撤销，撤销后相关下载链接立即失效。\n（演示系统邮件）",
            kind="consent_granted", to_user_id=grantee["id"])

    def send_consent_revoked(self, grantee, patient_name):
        return self.send(
            grantee["email"],
            "【体检解读】访问授权已撤销",
            f"{grantee['name']} 您好：\n\n"
            f"{patient_name} 已撤销对您的报告访问授权，您的访问与下载链接即刻失效。\n"
            f"（演示系统邮件）",
            kind="consent_revoked", to_user_id=grantee["id"])

    def send_report_corrected(self, patient, report_title, new_version_no, reason):
        return self.send(
            patient["email"],
            f"【体检解读】报告已更正：{report_title}（v{new_version_no}）",
            f"{patient['name']} 您好：\n\n"
            f"您的《{report_title}》已发布更正版本 v{new_version_no}。\n"
            f"更正原因：{reason}\n旧版本仅作留档，已停止对外提供下载；"
            f"既往解读记录仍引用其当时版本，内容不变。\n（演示系统邮件）",
            kind="report_corrected", to_user_id=patient["id"])

    def inbox(self, user_id: int) -> list[sqlite3.Row]:
        conn = self._get_conn()
        return conn.execute(
            "SELECT * FROM email_outbox WHERE to_user_id=? ORDER BY id DESC",
            (user_id,)).fetchall()

    def mark_read(self, mail_id: int, user_id: int):
        conn = self._get_conn()
        conn.execute(
            "UPDATE email_outbox SET read_at=COALESCE(read_at,?) WHERE id=? AND to_user_id=?",
            (timeutil.iso(timeutil.now()), mail_id, user_id))
