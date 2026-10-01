"""单命令启动：python -m app.main

首次运行自动初始化 SQLite 与演示数据（含更正报告样例 PDF）。
环境变量：HC_HOST / HC_PORT / HC_DATA_DIR / HC_DB_PATH / HC_PDF_DIR。
"""
from __future__ import annotations

import os
import sys

from . import config
from .db import Database
from .seed import seed
from .web import build_server


def main() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.PDF_DIR, exist_ok=True)
    db = Database(config.DB_PATH)
    seed(db)  # 已有数据则跳过；重置请用 python -m app.seed --reset

    httpd = build_server(db, config.HOST, config.PORT)
    url = f"http://{config.HOST}:{config.PORT}/"
    print("=" * 64)
    print(" 体检报告解读预约系统（演示）已启动")
    print(f"  地址：{url}")
    print(f"  数据库：{config.DB_PATH}")
    print(f"  PDF 目录：{config.PDF_DIR}")
    print(" 演示身份：患者 李明/王芳 · 医生 陈静/赵磊 · 家属 李建国/周小雨/王强")
    print(" 按 Ctrl+C 停止")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        db.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
