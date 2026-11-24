"""Measure an already imported, representative question using fresh runs."""
from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import uuid
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import urlopen

from regression import Client


def events(client: Client, run_id: str) -> list[dict]:
    query = urlencode({"token": client.token})
    url = f"{client.base}/api/runs/{run_id}/events?{query}"
    with urlopen(url, timeout=10) as response:
        lines = response.read().decode("utf-8").splitlines()
    return [json.loads(line.removeprefix("data: ").strip())
            for line in lines if line.startswith("data: ")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--run-id", required=True, help="已完成的样本核查 ID")
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.samples <= 300:
        parser.error("--samples must be between 1 and 300")
    session = json.loads((args.data / "session.json").read_text(encoding="utf-8"))
    client = Client(f"http://127.0.0.1:{session['port']}", session["token"])
    status, seed = client.req("GET", f"/runs/{args.run_id}")
    if status != 200 or seed.get("status") != "completed":
        parser.error("--run-id must identify a completed run in the selected library")

    expected = [(c["topic"], c["status"], c["conclusion_text"]) for c in seed["claims"]]
    samples = []
    for _ in range(args.samples + 1):
        started = perf_counter()
        status, submitted = client.req("POST", "/runs", {
            "request_id": f"bench-{uuid.uuid4().hex}", "question": seed["question"],
            "conditions": seed["conditions"], "revision_ids": seed["scope"],
        })
        if status != 202:
            raise RuntimeError(f"提交失败（HTTP {status}）")
        accepted = perf_counter()
        run_id = submitted["run_id"]
        snapshot = client.wait_run(run_id, poll_interval=0.05)
        elapsed = perf_counter() - started
        actual = [(c["topic"], c["status"], c["conclusion_text"]) for c in snapshot["claims"]]
        if snapshot["status"] != "completed" or actual != expected:
            raise RuntimeError(f"样本 {run_id} 的结论与基线不同，无法纳入延迟统计")
        metrics = {}
        for event in events(client, run_id):
            for key, value in (event.get("payload") or {}).items():
                if key.endswith("_ms"):
                    metrics[key] = value
        samples.append({"run_id": run_id, "model_version": snapshot["model_version"],
                        "accepted_ms": round((accepted - started) * 1000),
                        "end_to_end_ms": round(elapsed * 1000), **metrics})

    warm = samples[1:]
    summary = {}
    for key in ("accepted_ms", "retrieval_ms", "checking_ms", "commit_ms",
                "worker_ms", "end_to_end_ms"):
        values = sorted(row[key] for row in warm)
        summary[key] = {"median": statistics.median(values),
                        "p95": values[math.ceil(len(values) * 0.95) - 1]}
    print(json.dumps({
        "machine": platform.platform(), "processor": platform.processor(),
        "scope_count": len(seed["scope"]), "rule_version": snapshot["rules_version"],
        "warmup": samples[0], "warm_summary": summary, "warm_samples": warm,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
