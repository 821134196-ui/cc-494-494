"""时间与令牌工具。

演示环境以固定时钟运行（见 config.DEMO_NOW_ISO），保证并发测试与
演示流程结果确定。测试可通过 set_clock 推进时间，模拟授权到期等场景。
"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import config

_CST = timezone(timedelta(hours=8))  # 演示统一使用 UTC+8


def now() -> datetime:
    """当前时间。默认固定在 DEMO_NOW，可用 set_clock 推进。"""
    ctl = ClockControl.get()
    return ctl.value()


def at(iso: str) -> datetime:
    """解析 ISO 时间（无时区按 UTC+8 处理）。"""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return dt


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return dt.astimezone(_CST).isoformat(timespec="seconds")


def today():
    return now().astimezone(_CST).date()


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(_CST).strftime("%Y-%m-%d %H:%M")


def fmt_date(dt) -> str:
    return dt.strftime("%Y-%m-%d")


def new_token(nbytes: int = 24) -> str:
    """随机不透明令牌（下载链接、授权回执等）。"""
    return secrets.token_urlsafe(nbytes)


class ClockControl:
    """进程内可调时钟（仅演示/测试），跨线程共享同一时刻。"""

    def __init__(self):
        self._fixed = at(config.DEMO_NOW_ISO)
        self._lock = threading.Lock()

    @classmethod
    def get(cls) -> "ClockControl":
        global _CLOCK
        return _CLOCK

    def value(self) -> datetime:
        with self._lock:
            return self._fixed

    def set(self, dt: datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CST)
        with self._lock:
            self._fixed = dt

    def advance(self, **kw):
        with self._lock:
            self._fixed += timedelta(**kw)
            return self._fixed


_CLOCK = ClockControl()
