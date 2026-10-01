"""路径、配色与全局配置。

配色规范（医疗白 / 石墨灰 / 克制的青色提示）：
- 背景/卡片：#ffffff（医疗白）、次级底 #f6f8f9
- 文字/描边：石墨灰 #2f3638、次级 #606a6e、描边 #dce2e4
- 强调/提示：克制的青色 #147d80（仅用于可操作元素与轻度提示底 #e6f2f2）
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HC_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "healthcheck.db"
PDF_DIR = DATA_DIR / "pdfs"
STATIC_DIR = ROOT / "static"

# 演示系统固定“当前时间”，保证演示与测试确定可复现。
# 可用环境变量覆盖；真实系统应使用 now()。
DEMO_NOW_ISO = os.environ.get("HC_DEMO_NOW", "2026-10-01T09:00:00")

# 号源开放/截止规则
SLOT_OPEN_DAYS = 30          # 可预约未来 30 天内号源
SLOT_CANCEL_HOURS = 2        # 开始前 2 小时内不允许自助改约/取消

# 授权与下载链接
AUTH_MAX_DAYS = 365          # 家属授权最长可设到期时间
LINK_TTL_HOURS = 48          # 下载链接有效期（演示值）

# 演示数据声明
DEMO_DISCLAIMER = (
    "本系统为功能演示环境，所有人员、报告与检验数值均为虚构数据，"
    "不包含任何真实病历，不得用于实际医疗决策。"
)

# 配色（供页面与 PDF 共用）
COLORS = {
    "white": "#ffffff",
    "bg": "#f6f8f9",
    "ink": "#2f3638",
    "ink_2": "#606a6e",
    "line": "#dce2e4",
    "teal": "#147d80",
    "teal_soft": "#e6f2f2",
    "teal_ink": "#0d5f62",
    "warn": "#9a5b00",       # 石墨灰体系内的克制警示（琥珀灰）
    "warn_soft": "#f6efe3",
}
