"""页面渲染：纯 Python 拼 HTML（零模板引擎依赖），内联 CSS/SVG。

设计令牌（全系统统一）：
  --white  #ffffff 医疗白；--ink 石墨灰 #2b2f33；--teal 克制青 #0e8a8c；
  异常用克制暗红 #b3261e（同时给出 ↑/↓ 与数值，不仅靠颜色）。
移动端：单列、首屏卡片固定为「预约时间 + 授权状态」。
"""
from __future__ import annotations

import html
import json
from datetime import datetime

# ---------------------------------------------------------------- 配色/样式

CSS = """
:root{
  --white:#ffffff; --bg:#f5f7f8; --ink:#2b2f33; --ink-2:#4a5056;
  --muted:#7c858c; --line:#e2e6e9; --soft:#f0f3f4;
  --teal:#0e8a8c; --teal-d:#0a6e70; --teal-bg:#eef6f6; --teal-line:#bfe0e0;
  --red:#b3261e; --red-bg:#fbeceb; --amber-bg:#fdf3e3; --amber:#8a5a12;
  --green:#2e6b4f; --green-bg:#ecf4ef;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;}
a{color:var(--teal-d);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px 64px}
header.top{background:var(--white);border-bottom:1px solid var(--line);
  position:sticky;top:0;z-index:10}
.topbar{max-width:1080px;margin:0 auto;display:flex;align-items:center;
  gap:16px;padding:10px 16px;flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;color:var(--ink);white-space:nowrap}
.brand .dot{color:var(--teal)}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{padding:5px 10px;border-radius:6px;color:var(--ink-2);font-size:13px}
nav a:hover{background:var(--soft);text-decoration:none}
nav a.on{background:var(--teal-bg);color:var(--teal-d);font-weight:600}
.spacer{flex:1}
.who{font-size:12px;color:var(--muted);white-space:nowrap}
.btn{display:inline-block;border:1px solid var(--teal);background:var(--teal);
  color:#fff;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer;
  text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--teal-d);text-decoration:none}
.btn.ghost{background:#fff;color:var(--teal-d)}
.btn.ghost:hover{background:var(--teal-bg)}
.btn.danger{background:#fff;color:var(--red);border-color:var(--red)}
.btn.danger:hover{background:var(--red-bg)}
.btn.mini{padding:3px 9px;font-size:12px}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.card{background:var(--white);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin:16px 0;}
.card h2{margin:0 0 4px;font-size:16px}
.card h3{margin:18px 0 8px;font-size:14px;color:var(--ink)}
.sub{color:var(--muted);font-size:12.5px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid-3{display:grid;grid-template-columns:2fr 1fr;gap:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;background:var(--soft)}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;
  line-height:1.7;border:1px solid transparent;white-space:nowrap}
.tag.teal{background:var(--teal-bg);color:var(--teal-d);border-color:var(--teal-line)}
.tag.red{background:var(--red-bg);color:var(--red);border-color:#f0c7c4}
.tag.gray{background:var(--soft);color:var(--ink-2);border-color:var(--line)}
.tag.green{background:var(--green-bg);color:var(--green);border-color:#cfe3d6}
.tag.amber{background:var(--amber-bg);color:var(--amber);border-color:#eed9ad}
.val-bad{color:var(--red);font-weight:700}
.muted{color:var(--muted)}
.small{font-size:12px}
form.inline{display:inline}
label.fld{display:block;margin:10px 0;font-size:13px;color:var(--ink-2)}
label.fld span{display:block;margin-bottom:3px;font-weight:600;color:var(--ink)}
input[type=text],input[type=date],input[type=number],select,textarea{
  width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:6px;
  font:inherit;background:#fff;color:var(--ink)}
textarea{min-height:80px;resize:vertical}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row > div{flex:1;min-width:140px}
.flash{border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px}
.flash.ok{background:var(--green-bg);color:var(--green);border:1px solid #cfe3d6}
.flash.err{background:var(--red-bg);color:var(--red);border:1px solid #f0c7c4}
.banner{border-radius:8px;padding:11px 14px;margin:12px 0;font-size:13px;
  border:1px solid}
.banner.warn{background:var(--amber-bg);border-color:#eed9ad;color:var(--amber)}
.banner.info{background:var(--teal-bg);border-color:var(--teal-line);color:var(--teal-d)}
.banner.danger{background:var(--red-bg);border-color:#f0c7c4;color:var(--red)}
.hero{background:linear-gradient(135deg,#ffffff 0%,#f2f9f9 100%);
  border:1px solid var(--teal-line);border-radius:12px;padding:18px 20px;margin:16px 0}
.hero .kicker{color:var(--teal-d);font-size:12px;font-weight:700;letter-spacing:.05em}
.hero .big{font-size:20px;font-weight:700;margin:2px 0}
.pillrow{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.cal-day{margin:16px 0}
.cal-day h4{margin:0 0 6px;font-size:13.5px;color:var(--ink);
  display:flex;align-items:center;gap:8px}
.slot-chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border:1px solid var(--line);border-radius:8px;padding:7px 10px;
  background:#fff;min-width:210px;display:flex;align-items:center;gap:10px}
.chip .t{font-weight:600;font-size:13px}
.chip.suspended{background:var(--soft);color:var(--muted)}
.chip.full{background:var(--soft)}
.kv{display:grid;grid-template-columns:88px 1fr;gap:3px 12px;font-size:13px;
  margin:8px 0}
.kv dt{color:var(--muted)}
.kv dd{margin:0}
.trend-note{display:grid;grid-template-columns:1fr;gap:0}
.metric-row{display:grid;grid-template-columns:210px 1fr;gap:0;
  border-top:1px solid var(--line);padding:14px 0}
.metric-row:first-child{border-top:none}
.metric-head{font-weight:600}
.metric-val{font-size:13px;margin-top:3px}
.note-box{background:var(--soft);border-left:3px solid var(--teal);
  border-radius:0 8px 8px 0;padding:10px 12px;font-size:13px}
.note-box.unsigned{border-left-color:var(--amber)}
.sig{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:8px;
  margin-top:14px;font-size:13.5px}
.sig.signed{background:var(--green-bg);color:var(--green)}
.sig.unsigned{background:var(--amber-bg);color:var(--amber)}
.sigline{border-bottom:1.5px solid var(--ink-2);display:inline-block;min-width:120px;
  text-align:center;margin:0 6px}
.pdfframe{width:100%;height:640px;border:1px solid var(--line);border-radius:8px;
  background:#fff}
code{background:var(--soft);padding:1px 5px;border-radius:4px;font-size:12px}
.checklist label{display:inline-flex;gap:6px;align-items:center;margin:2px 14px 2px 0;
  font-size:12.5px;font-weight:400}
footer.foot{max-width:1080px;margin:24px auto 0;padding:14px 16px;color:var(--muted);
  font-size:12px}
@media (max-width:760px){
  .grid,.grid-3{grid-template-columns:1fr}
  .metric-row{grid-template-columns:1fr;gap:10px}
  .wrap{padding:0 12px 48px}
  nav{order:3;width:100%}
  .who{margin-left:auto}
}
"""

STATUS_LABELS = {
    "booked": ("已预约", "teal"),
    "reschedule_requested": ("改期申请中", "amber"),
    "pending_reschedule": ("待改约", "red"),
    "rescheduled": ("已改期", "gray"),
    "cancelled": ("已取消", "gray"),
    "completed": ("已完成", "green"),
    "draft": ("未签字", "amber"),
    "signed": ("已签字", "green"),
    "amended": ("修改待重签", "amber"),
    "active": ("有效", "green"),
    "revoked": ("已撤销", "red"),
    "expired": ("已到期", "gray"),
    "disputed": ("争议冻结", "red"),
    "valid": ("有效", "green"),
    "exhausted": ("次数用完", "gray"),
    "online": ("线上", "teal"),
    "onsite": ("现场", "gray"),
    "open": ("可约", "green"),
    "suspended": ("停诊", "red"),
    "closed": ("已结束", "gray"),
}


def tag(status: str) -> str:
    label, cls = STATUS_LABELS.get(status, (status, "gray"))
    return f'<span class="tag {cls}">{esc(label)}</span>'


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# ---------------------------------------------------------------- SVG 趋势图

def sparkline(points: list[dict], ref_low, ref_high, width=240, height=58) -> str:
    """指标趋势迷你图：参考范围带（浅青）+ 折线（石墨灰）+ 异常点（暗红）。

    异常点同时在右侧数值表/标记中给出 ↑↓ 与数值，不依赖颜色 alone。
    """
    vals = [p["value"] for p in points]
    if not vals:
        return ""
    pad_l, pad_r, pad_t, pad_b = 8, 34, 8, 14
    lo = min(vals + ([ref_low] if ref_low is not None else []))
    hi = max(vals + ([ref_high] if ref_high is not None else []))
    span = (hi - lo) or (abs(hi) or 1)
    lo -= span * 0.18
    hi += span * 0.18
    span = hi - lo

    def X(i):
        if len(points) == 1:
            return pad_l + (width - pad_l - pad_r) / 2
        return pad_l + i * (width - pad_l - pad_r) / (len(points) - 1)

    def Y(v):
        return pad_t + (1 - (v - lo) / span) * (height - pad_t - pad_b)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'role="img" aria-label="趋势图" style="max-width:{width}px">']
    # 参考范围带
    if ref_low is not None and ref_high is not None:
        y1, y2 = Y(ref_high), Y(ref_low)
        parts.append(f'<rect x="{pad_l}" y="{y1:.1f}" width="{width - pad_l - pad_r}" '
                     f'height="{(y2 - y1):.1f}" fill="#eef6f6"/>')
        parts.append(f'<line x1="{pad_l}" y1="{y1:.1f}" x2="{width - pad_r}" y2="{y1:.1f}" '
                     f'stroke="#bfe0e0" stroke-width="1" stroke-dasharray="3 3"/>')
        parts.append(f'<line x1="{pad_l}" y1="{y2:.1f}" x2="{width - pad_r}" y2="{y2:.1f}" '
                     f'stroke="#bfe0e0" stroke-width="1" stroke-dasharray="3 3"/>')
    coords = [f"{X(i):.1f},{Y(p['value']):.1f}" for i, p in enumerate(points)]
    parts.append(f'<polyline fill="none" stroke="#2b2f33" stroke-width="1.8" '
                 f'points="{" ".join(coords)}" stroke-linejoin="round"/>')
    for i, p in enumerate(points):
        abnormal = ((ref_low is not None and p["value"] < ref_low) or
                    (ref_high is not None and p["value"] > ref_high))
        cx, cy = X(i), Y(p["value"])
        fill = "#b3261e" if abnormal else "#2b2f33"
        r = 3.4 if abnormal else 2.4
        tip = (f"{p['examined_at']}：{p['value']:g}（V{p['version']}）" +
               (" 异常" if abnormal else ""))
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}" '
            f'stroke="#fff" stroke-width="1"><title>{esc(tip)}</title></circle>')
        if i == len(points) - 1:
            parts.append(f'<text x="{cx + 5:.1f}" y="{cy + 3.5:.1f}" font-size="10.5" '
                         f'fill="{fill}" font-weight="700">{p["value"]:g}</text>')
        parts.append(f'<text x="{X(i):.1f}" y="{height - 3:.1f}" font-size="8.5" '
                     f'fill="#7c858c" text-anchor="middle">{p["examined_at"][2:7]}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- 页面骨架

def page(*, title: str, nav: str, who: str, role: str, body: str,
         flash: str | None = None) -> bytes:
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{esc(msg)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 体检报告解读预约</title>
<style>{CSS}</style>
</head><body>
<header class="top"><div class="topbar">
  <a class="brand" href="/">康和体检<span class="dot">·</span>解读预约</a>
  <nav>{nav}</nav>
  <span class="spacer"></span>
  <span class="who">{esc(who)} · {esc(role)}</span>
  <form class="inline" method="post" action="/logout"><button class="btn mini ghost">切换身份</button></form>
</div></header>
<main class="wrap">{flash_html}{body}</main>
<footer class="foot">演示系统：全部受检人与数据均为虚构合成，不含真实病历；
邮件由模拟适配器处理（仅落库，不真实发送）；PDF 保存在本地 data/pdfs 目录。</footer>
</body></html>""".encode("utf-8")


NAV_PATIENT = {
    "/": "概览", "/calendar": "号源日历", "/appointments": "我的预约",
    "/records": "解读记录", "/authorizations": "授权管理", "/mail": "模拟邮件",
}
NAV_DOCTOR = {
    "/doctor": "我的工作台", "/calendar": "号源日历",
    "/records": "解读记录", "/mail": "模拟邮件",
}
NAV_FAMILY = {
    "/shared": "共享给我", "/mail": "模拟邮件",
}


def nav_html(items: dict[str, str], active: str) -> str:
    out = []
    for path, label in items.items():
        cls = ' class="on"' if path == active else ""
        out.append(f'<a href="{path}"{cls}>{label}</a>')
    return "".join(out)
