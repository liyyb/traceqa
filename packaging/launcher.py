"""TraceQA 启动器（手册 §2 首次启动、§7 桌面入口）。

职责：单实例、健康检查、查看空间/模型包/本地服务状态、打开工作台、
退出时回收工作进程。用法：
    python launcher.py [--no-open] [--status] [--data DIR] [--port 8765]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
sys.path.insert(0, str(BACKEND))

from traceqa_server.config import (  # noqa: E402
    Config, configured_data_dir, FROZEN, PARSER_VERSION,
)


def session_path() -> Path:
    return configured_data_dir() / "session.json"


def read_session() -> dict:
    p = session_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def status_report(port: int) -> int:
    """首次启动前检查：目录可写、空间、模型摘要、数据库版本和端口。"""
    import shutil
    data = configured_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    total, used, free = shutil.disk_usage(data)
    writable = os.access(data, os.W_OK)
    session = read_session()
    print(f"资料目录      : {data}（可写：{writable}）")
    print(f"磁盘可用      : {free / 2 ** 30:.1f} GB")
    print(f"解析器版本    : {PARSER_VERSION}")
    print(f"生成/规则后端 : {Config().model_backend}")
    print(f"端口          : {port}（{'已监听' if is_running(port) else '未启动'}）")
    print(f"数据库文件    : {data / 'traceqa.db'}"
          f"（{'存在' if (data / 'traceqa.db').exists() else '尚未创建'}）")
    print(f"模型包目录    : {data / 'models'}")
    if session:
        print(f"本机会话端口  : {session.get('port', '—')}")
        token = session.get("token")
        if token:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/bootstrap",
                    headers={"X-TraceQA-Token": token})
                with urllib.request.urlopen(req, timeout=2) as r:
                    b = json.loads(r.read().decode("utf-8"))
                print(f"PDF 后端      : {b.get('pdf_backend')}")
                print(f"已导入制度    : {b.get('doc_count')} 份")
            except Exception as e:
                print(f"服务状态读取失败：{e}")
    else:
        print("本机访问地址  : 尚未生成（首次启动后写入 session.json）")
    return 0 if writable else 1


def main():
    if FROZEN and sys.argv[1:2] == ["--serve"]:
        from run_server import main as serve
        del sys.argv[1]
        serve()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reindex-semantic", action="store_true",
                    help="生成或补齐 Ollama 语义索引")
    ap.add_argument("--data", default=None)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TRACEQA_PORT", "8765")))
    args = ap.parse_args()

    if args.data:
        p = Path(args.data).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        os.environ["TRACEQA_DATA"] = str(p)
    PORT = args.port

    if args.status:
        sys.exit(status_report(PORT))
    if args.reindex_semantic:
        from contextlib import closing
        from traceqa_server import db, semantic
        cfg = Config()
        cfg.ensure_dirs()
        db.init_db(cfg.db_path)
        try:
            with closing(db.connect(cfg.db_path)) as connection:
                print(f"新增语义索引 {semantic.build_index(connection, cfg)} 条")
        except semantic.UNAVAILABLE as exc:
            sys.exit(f"无法构建语义索引。{exc}")
        return

    proc = None
    if not is_running(PORT):
        env = dict(os.environ)
        env["TRACEQA_AUTO_OPEN"] = "0"
        env["TRACEQA_PORT"] = str(PORT)
        if args.data:
            env["TRACEQA_DATA"] = str(Path(args.data).resolve())
        server_arg = "--serve" if FROZEN else str(BACKEND / "run_server.py")
        proc = subprocess.Popen(
            [sys.executable, server_arg, "--port", str(PORT)], env=env)
        # 等待健康检查通过（首次准备与日常启动分别计时）
        t0 = time.time()
        for _ in range(120):
            if is_running(PORT):
                break
            if proc.poll() is not None:
                print("服务启动失败，请查看后端日志。")
                sys.exit(1)
            time.sleep(0.5)
        else:
            proc.terminate()
            proc.wait(timeout=10)
            print("服务启动超时。")
            sys.exit(1)
        print(f"本地服务就绪（{time.time() - t0:.1f}s）")

    session = read_session()
    url = session.get("url")
    if not url or session.get("port") != PORT:
        sys.exit("当前端口与资料目录的会话不匹配，请换一个端口或关闭其他 TraceQA 实例。")
    if proc is None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/bootstrap",
            headers={"X-TraceQA-Token": session["token"]})
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except OSError:
            sys.exit("运行中的服务使用其他资料目录，请关闭该服务或换一个端口。")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    print(f"工作台已启动（本机端口 {PORT}）")
    print("按 Ctrl+C 退出并回收工作进程。")
    try:
        if proc is not None:
            return proc.wait()
    except KeyboardInterrupt:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("已退出。")


if __name__ == "__main__":
    sys.exit(main())
