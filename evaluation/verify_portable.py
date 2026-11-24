"""Verify a Windows ZIP in a temporary, independent user-data directory."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from regression import Client

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def server(exe: Path, data: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
               TRACEQA_AUTO_OPEN="0", TRACEQA_PDF_BACKEND="fitz",
               TRACEQA_OLLAMA_MODEL="", TRACEQA_EMBEDDING_MODEL="")
    log = data.parent / f"{data.name}-{port}.log"
    with log.open("w", encoding="utf-8") as output:
        started = time.perf_counter()
        proc = subprocess.Popen([exe, "--serve", "--port", str(port), "--data", data],
                                cwd=exe.parent, env=env, stdout=output,
                                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError(log.read_text(encoding="utf-8", errors="replace"))
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise TimeoutError("Portable service startup timeout")
            session = json.loads((data / "session.json").read_text(encoding="utf-8"))
            client = Client(base, session["token"])
            client.startup_ms = round((time.perf_counter() - started) * 1000)
            client.pid = proc.pid
            yield client
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)


def check_record(client, record_id, expected_bytes):
    assert client.download_record(record_id) == expected_bytes
    _, record = client.req("GET", f"/records/{record_id}")
    evidence = record["snapshot"]["claims"][0]["evidences"]
    assert {item["locator"]["kind"] for item in evidence} == {"docx", "pdf"}
    for item in evidence:
        version = item["version_id"]
        original = urllib.request.Request(
            f"{client.base}/api/versions/{version}/original",
            headers={"X-TraceQA-Token": client.token})
        fmt = item["locator"]["kind"]
        with urllib.request.urlopen(original, timeout=5) as response:
            assert response.read(4).startswith(b"%PDF" if fmt == "pdf" else b"PK")
        if fmt == "docx":
            revision = item["revision_id"]
            status, tree = client.req("GET", f"/versions/{version}/tree?revision_id={revision}")
            assert status == 200 and tree["tree"] and tree["revision_id"] == revision


def peak_working_set(pid: int) -> int:
    fields = ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
              "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
              "PagefileUsage", "PeakPagefileUsage")

    class ProcessMemory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (field, ctypes.c_size_t) for field in fields]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]
    handle = kernel.OpenProcess(0x0410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = ProcessMemory()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    finally:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="TraceQA portable ") as temp:
        root = Path(temp)
        with zipfile.ZipFile(args.archive) as zip_file:
            zip_file.extractall(root)
        bundle = root / "TraceQA"
        manifest = json.loads((bundle / "SHA256.json").read_text(encoding="utf-8"))
        for relative, expected in manifest.items():
            with (bundle / relative).open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, relative
        exe = bundle / "TraceQA.exe"
        source, moved = root / "原始资料", root / "恢复验证"
        subprocess.run([str(bundle / "启动 TraceQA.cmd"), "--status", "--data", str(source)],
                       check=True, cwd=bundle, timeout=15)
        with server(exe, source) as client:
            with urllib.request.urlopen(client.base, timeout=5) as response:
                assert b"/assets/" in response.read()
            worker = next((bundle / "_internal" / "frontend" / "dist" / "assets").glob("pdf.worker*.mjs"))
            with urllib.request.urlopen(f"{client.base}/assets/{worker.name}", timeout=5) as response:
                assert response.headers.get_content_type() in {"text/javascript", "application/javascript"}
            assets = bundle / "_internal" / "frontend" / "dist" / "assets" / "pdfjs"
            for folder in ("cmaps", "standard_fonts"):
                expected = ROOT / "frontend" / "node_modules" / "pdfjs-dist" / folder
                files = {path.name for path in expected.iterdir() if path.is_file()}
                assert files and files == {path.name for path in (assets / folder).iterdir()}
                sample = sorted(files)[0]
                with urllib.request.urlopen(
                        f"{client.base}/assets/pdfjs/{folder}/{sample}", timeout=5) as response:
                    assert response.read() == (expected / sample).read_bytes()
            subprocess.run([sys.executable, "-X", "utf8", ROOT / "evaluation" / "regression.py",
                            "--data", source], check=True, cwd=ROOT)
            _, records = client.req("GET", "/records")
            record_id = next(record["id"] for record in records["records"] if record["rev_no"] == "R1")
            frozen = client.download_record(record_id)
            status, response = client.req("POST", "/settings/backup")
            assert status == 200, response
            backup = response["backup_dir"]
            check_record(client, record_id, frozen)
            _, record = client.req("GET", f"/records/{record_id}")
            _, run = client.req("GET", f"/runs/{record['run_id']}")
            warm = subprocess.run(
                [sys.executable, "-X", "utf8", ROOT / "evaluation" / "benchmark.py",
                 "--data", source, "--run-id", run["run_id"], "--samples", "30"],
                check=True, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            warm_report = json.loads(warm.stdout)
        with server(exe, moved) as client:
            status, response = client.req("POST", "/settings/restore", {"backup_dir": backup})
            assert status == 200, response
            assert client.req("GET", f"/records/{record_id}")[0] == 404
        with server(exe, moved) as client:
            check_record(client, record_id, frozen)
            status, snaps = client.req("GET", "/settings/backups")
            assert status == 200
            rollback = next(x["dir"] for x in snaps["backups"] if x["name"].startswith("before-restore"))
            assert client.req("POST", "/settings/restore", {"backup_dir": rollback})[0] == 200
        with server(exe, moved) as client:
            assert client.req("GET", f"/records/{record_id}")[0] == 404
        cold = []
        for _ in range(5):
            with server(exe, source) as client:
                started = time.perf_counter()
                status, request = client.req("POST", "/runs", {
                    "request_id": f"cold-{client.pid}", "question": run["question"],
                    "revision_ids": run["scope"], "conditions": run["conditions"],
                })
                assert status == 202
                result = client.wait_run(request["run_id"], poll_interval=0.05)
                assert result["status"] == "completed"
                cold.append({"startup_ms": client.startup_ms,
                             "first_query_ms": round((time.perf_counter() - started) * 1000),
                             "service_peak_working_set_bytes": peak_working_set(client.pid)})
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.archive.open("rb") as stream:
                archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            args.report.write_text(json.dumps({
                "portable_smoke": "passed", "cold_restart_samples": cold,
                "warm_benchmark": warm_report, "archive_sha256": archive_hash,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS Windows ZIP, manifest, launcher, portable service, regression, relocated restore and rollback")


if __name__ == "__main__":
    main()
