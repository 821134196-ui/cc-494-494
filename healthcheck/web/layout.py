"""HTML 与共享布局工具（医疗白 / 石墨灰 / 克制青色）。"""
from __future__ import annotations

import html
from urllib.parse import urlencode

from .. import config

ROLE_CN = {"patient": "患者", "doctor": "医生", "family": "家属", "staff": "体检中心"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def fa(icon: str) -> str:
    """极简内联图标（无图标字体依赖），用 Unicode 几何符号。"""
    icons = {
        "clock": "◷", "calendar": "▦", "shield": "⛨", "doc": "❏", "note": "✎",
        "warn": "!", "info": "i", "check": "✓", "cross": "✕", "mail": "✉",
        "users": "❘❘", "refresh": "↻", "download": "↓", "link": "⚓",
    }
    return icons.get(icon, "•")


def qs(**kw) -> str:
    return urlencode({k: v for k, v in kw.items() if v is not None})


def with_flash(base: str, kind: str = "ok", msg: str = "", flash: str = "") -> str:
    """在可能已含查询串的地址后正确追加 flash 参数。"""
    text = flash or msg
    extra = qs(flash_kind=kind, flash=text)
    return f"{base}{'&' if '?' in base else '?'}{extra}"


def page(*, title: str, body: str, me=None, active: str = "", flash=None,
         extra_head: str = "") -> str:
    nav_items = _nav_for(me)
    nav = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for key, label, href in nav_items)
    ident = ""
    if me is not None:
        badge = ROLE_CN.get(me["role"], me["role"])
        ident = (
            f'<span class="identity">'
            f'<span class="role-badge">{esc(badge)}</span>'
            f'<span>{esc(me["name"])} · {esc(me["code"])}</span>'
            f'<a class="btn ghost sm" href="/login">切换身份</a></span>')
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{esc(msg)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)} · 报告解读平台</title>
<link rel="stylesheet" href="/static/app.css">
{extra_head}
</head>
<body>
<div class="demo-strip">{esc(config.DEMO_DISCLAIMER)}</div>
<header class="topbar">
  <div class="inner">
    <a class="brand" href="/"><span class="dot"></span>体检报告解读
      <small>演示环境</small></a>
    <span class="spacer"></span>
    {ident}
  </div>
  <nav class="nav">{nav}</nav>
</header>
<main class="wrap">
{flash_html}
{body}
<p class="footer-note">本页面所有人员、报告与数值均为虚构演示数据 · 报告仅对本人及明确授权者开放</p>
</main>
</body>
</html>"""


def _nav_for(me):
    if me is None:
        return []
    role = me["role"]
    if role == "patient":
        return [
            ("home", "我的预约", "/home"),
            ("calendar", "预约解读", "/calendar"),
            ("reports", "我的报告", "/reports"),
            ("interpretations", "解读记录", "/interpretations"),
            ("consents", "授权管理", "/consents"),
            ("logs", "访问日志", "/logs"),
            ("mail", "模拟邮箱", "/mail"),
        ]
    if role == "doctor":
        return [
            ("doctor", "号源与预约", "/doctor"),
            ("interpretations", "解读工作台", "/doctor/interpretations"),
            ("mail", "模拟邮箱", "/mail"),
        ]
    if role == "family":
        return [
            ("family", "被授权报告", "/family"),
            ("consents", "授权相关", "/family?tab=disputes"),
            ("mail", "模拟邮箱", "/mail"),
        ]
    if role == "staff":
        return [
            ("staff", "报告与更正", "/staff"),
            ("disputes", "授权争议", "/staff/disputes"),
        ]
    return []


def _identity_options(me) -> str:
    # 由调用上下文渲染更复杂；这里仅静态引用 /switch 页说明
    return f'<option value="{me["id"]}">{esc(me["name"])}（{esc(me["code"])}）</option>'


def status_badge(status: str) -> str:
    table = {
        "booked": ("ok", "已预约"),
        "reschedule_pending": ("warn", "待改约"),
        "completed": ("gray", "已完成"),
        "canceled": ("gray", "已取消"),
        "active": ("ok", "有效"),
        "revoked": ("gray", "已撤销"),
        "expired": ("gray", "已到期"),
        "disputed": ("warn", "争议冻结"),
        "draft": ("warn", "待签字"),
        "signed": ("ok", "已签字"),
        "voided": ("gray", "已作废"),
        "open": ("teal", "可约"),
        "canceled_slot": ("warn", "已停诊"),
        "blocked": ("gray", "暂停"),
    }
    cls, text = table.get(status, ("gray", status))
    return f'<span class="badge {cls}">{esc(text)}</span>'


def flash_qs(kind: str, msg: str) -> str:
    return "?" + urlencode({"flash_kind": kind, "flash": msg})
