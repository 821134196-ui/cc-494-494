"""路径与会话配置（可被环境变量覆盖）。"""
from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("HC_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("HC_DB_PATH", os.path.join(DATA_DIR, "healthcheck.db"))
PDF_DIR = os.environ.get("HC_PDF_DIR", os.path.join(DATA_DIR, "pdfs"))

HOST = os.environ.get("HC_HOST", "127.0.0.1")
PORT = int(os.environ.get("HC_PORT", "8000"))

# 会话 cookie
SESSION_COOKIE = "hc_session"
SECRET = os.environ.get("HC_SECRET", "demo-secret-not-for-production")
