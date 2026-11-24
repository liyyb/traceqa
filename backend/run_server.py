"""TraceQA 服务入口：生成令牌、启动 uvicorn（仅回环地址）。

用法：python run_server.py [--port 8765] [--data DIR] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "parsers"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRACEQA_PORT", "8765")))
    ap.add_argument("--data", default=None)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    if args.data:
        os.environ["TRACEQA_DATA"] = str(Path(args.data).resolve())
    os.environ["TRACEQA_PORT"] = str(args.port)
    from traceqa_server.config import Config
    cfg = Config()
    cfg.ensure_dirs()
    with (cfg.data_dir / "service.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        serve(cfg, args)


def serve(cfg, args):
    from traceqa_server.backup import apply_pending_restore
    restored = apply_pending_restore(cfg)
    if restored:
        print(f"已从备份恢复：{restored}", flush=True)
    os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(cfg.artifacts_dir / "docling"))

    from traceqa_server.config import new_token
    from traceqa_server.api import create_app
    token = new_token()
    app = create_app(cfg, token)

    session_file = cfg.data_dir / "session.json"
    session_file.write_text(json.dumps(
        {"token": token, "port": args.port,
         "url": f"http://127.0.0.1:{args.port}/?token={token}"},
        ensure_ascii=False), encoding="utf-8")

    if args.open or os.environ.get("TRACEQA_AUTO_OPEN") == "1":
        webbrowser.open(f"http://127.0.0.1:{args.port}/?token={token}")

    import uvicorn
    uvicorn.run(app, host=cfg.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
