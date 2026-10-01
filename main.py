"""单命令启动入口：初始化（如需）+ 种子 + HTTP 服务。

    python3 main.py            # 首次自动建库并植入演示数据，随后启动
    python3 main.py --reseed   # 重置演示数据后启动
    python3 main.py --seed-only
"""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from healthcheck import config, db
from healthcheck.web.server import build_server


def ensure_db(reseed: bool = False):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    need_seed = reseed or not config.DB_PATH.exists()
    if need_seed:
        runpy.run_path(str(Path(__file__).parent / "scripts" / "seed.py"),
                       run_name="__main__")
    else:
        conn = db.connect(config.DB_PATH)
        try:
            db.init_db(conn)  # 幂等
        finally:
            conn.close()
    return need_seed


def main(argv=None):
    ap = argparse.ArgumentParser(description="体检报告解读预约系统（演示）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reseed", action="store_true", help="重置演示数据")
    ap.add_argument("--seed-only", action="store_true", help="仅初始化数据后退出")
    args = ap.parse_args(argv)

    seeded = ensure_db(args.reseed)
    if args.seed_only:
        print("数据初始化完成。")
        return 0

    httpd = build_server(args.host, args.port)
    print("=" * 64)
    print("体检报告解读预约系统（演示）已启动")
    print(f"  地址：http://{args.host}:{args.port}/")
    print("  演示身份：患者 林晓(P-1001) / 周明(P-1002)；医生 陈静(D-2201) / 王磊(D-2202)")
    print("  数据与 PDF：", config.DATA_DIR)
    print("  Ctrl+C 停止")
    if not seeded:
        print("  （沿用已有数据库；如需重置演示数据：python3 main.py --reseed）")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
