"""双格式版本回归脚本（手册 §6）：对运行中的服务执行固定夹具用例。

用法：先启动服务（python backend/run_server.py），再运行
      python evaluation/regression.py [--base http://127.0.0.1:8765]
输出 PASS/FAIL 明细与定位命中率。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def req(self, method: str, path: str, body=None, raw_body=None, timeout=300):
        url = f"{self.base}/api{path}"
        data = None
        headers = {"X-TraceQA-Token": self.token}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if raw_body is not None:
            data = raw_body
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}

    def upload(self, path: Path):
        boundary = uuid.uuid4().hex
        fname = path.name.encode("utf-8")
        content = path.read_bytes()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="'
        ).encode("utf-8") + fname + (
            f'"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = f"{self.base}/api/imports"
        r = urllib.request.Request(
            url, data=body, method="POST",
            headers={"X-TraceQA-Token": self.token,
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def wait_import(self, import_id: str, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, body = self.req("GET", f"/imports/{import_id}")
            if body.get("status") == "done":
                return body
            time.sleep(1)
        raise TimeoutError("导入超时")

    def download_record(self, record_id: str) -> bytes:
        request = urllib.request.Request(
            f"{self.base}/api/records/{record_id}/download",
            headers={"X-TraceQA-Token": self.token})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()

    def run_question(self, question: str, revision_ids: list[str],
                     conditions=None, timeout=300):
        req_id = f"reg-{uuid.uuid4().hex[:10]}"
        st, body = self.req("POST", "/runs", {
            "request_id": req_id, "question": question,
            "revision_ids": revision_ids, "conditions": conditions or {}})
        assert st == 202, f"提交失败：{body}"
        return self.wait_run(body["run_id"], timeout)

    def wait_run(self, run_id: str, timeout=300, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st, snap = self.req("GET", f"/runs/{run_id}")
            if snap.get("status") in ("completed", "failed", "cancelled"):
                return snap
            time.sleep(poll_interval)
        raise TimeoutError("核查超时")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="覆盖会话文件中的服务地址")
    ap.add_argument("--data", type=Path, default=HERE.parent / "data",
                    help="服务启动时使用的数据目录")
    args = ap.parse_args()

    session = args.data / "session.json"
    if not session.exists():
        print(f"未找到 {session}：请先启动对应数据目录的服务")
        sys.exit(2)
    session_data = json.loads(session.read_text(encoding="utf-8"))
    c = Client(args.base or f"http://127.0.0.1:{session_data['port']}", session_data["token"])

    exp = json.loads((FIX / "expectations.json").read_text(encoding="utf-8"))
    files = exp["files"]

    print("== 导入夹具 ==")
    rev_of: dict[str, str] = {}
    for key, fname in files.items():
        p = FIX / fname
        if not p.exists():
            print(f"FAIL 缺少夹具文件 {fname}（先运行 make_fixtures.py）")
            sys.exit(2)
        up = c.upload(p)
        body = c.wait_import(up["import_id"])
        f0 = body["files"][0]
        if f0["status"] != "done":
            print(f"FAIL 导入 {fname}：{f0.get('error')}")
            sys.exit(1)
        rev_of[key] = f0["revision_id"]
        print(f"  ok {key} -> {f0['revision_id']} ({fname})")

    results = []

    def check(case_id: str, ok: bool, detail: str):
        results.append((case_id, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {case_id}：{detail}")

    print("\n== 用例 ==")
    runs = {}
    for case in exp["cases"]:
        scope = [rev_of[k] for k in case["scope"]]
        snap = c.run_question(case["question"], scope)
        runs[case["id"]] = snap
        claims = snap.get("claims", [])
        if "expect" in case:
            e = case["expect"]
            cl = next((x for x in claims if x["topic"] == e["topic"]), None)
            if cl is None:
                check(case["id"], False, f"未产生主题 {e['topic']} 的子问题")
                continue
            ok = cl["status"] == e["status"]
            detail = f"状态={cl['status']}（期望 {e['status']}）"
            if ok and e.get("amount"):
                amounts = [ev["quote"] for ev in cl["evidences"]
                           if ev["relation"] == "support"]
                ok = any(e["amount"] in (q or "") for q in amounts) or \
                     (e["amount"] in (cl.get("conclusion_text") or ""))
                detail += f"；金额 {e['amount']} {'命中' if ok else '未命中'}"
            if ok and e.get("amounts"):
                txt = json.dumps(cl, ensure_ascii=False)
                ok = all(a in txt for a in e["amounts"])
                detail += f"；冲突金额并列 {'是' if ok else '否'}"
            if ok and e.get("gap_attachment"):
                ok = e["gap_attachment"] in json.dumps(cl.get("gap") or {},
                                                       ensure_ascii=False)
                detail += f"；缺口附件 {'指明' if ok else '未指明'}"
            check(case["id"], ok, detail)
        elif "expect_multi" in case:
            all_ok = True
            details = []
            for e in case["expect_multi"]:
                cl = next((x for x in claims if x["topic"] == e["topic"]), None)
                ok = cl is not None and cl["status"] == e["status"]
                if ok and e.get("followup_contains"):
                    fu = (cl.get("followup") or {}).get("prompt", "")
                    ok = e["followup_contains"].replace(" ", "") in fu.replace(" ", "")
                all_ok &= ok
                details.append(f"{e['topic']}={cl['status'] if cl else '缺失'}")
            check(case["id"], all_ok, "；".join(details))

    print("\n== 交接与补件 ==")
    conflict = runs["conflict-A-C"]
    claim_id = conflict["claims"][0]["id"]
    note = {"claim_id": claim_id, "body": "财务电话答复按 800 元执行",
            "source": "电话沟通", "signature": "测试经办人"}
    status, _ = c.req("PUT", f"/claims/{claim_id}/note-draft", note)
    assert status == 200
    status, first = c.req("POST", "/records", {
        "run_id": conflict["run_id"], "request_id": uuid.uuid4().hex})
    assert status == 201, first
    first_bytes = c.download_record(first["id"])
    correction = {
        "base_rev_no": "R1", "reason": "重新核实电话答复",
        "note_updates": [{**note, "body": "需部门负责人审批"}],
        "request_id": uuid.uuid4().hex,
    }
    path = f"/records/{first['id']}/corrections"
    status, second = c.req("POST", path, correction)
    assert status == 201, second
    retry_status, retry = c.req("POST", path, correction)
    stale_status, _ = c.req("POST", path, {**correction, "request_id": uuid.uuid4().hex})
    check("record-correction",
          second["rev_no"] == "R2" and second["parent_record_id"] == first["id"]
          and second["snapshot"]["claims"][0]["status"] == "conflict"
          and first_bytes == c.download_record(first["id"])
          and "需部门负责人审批".encode("utf-8") in c.download_record(second["id"])
          and retry_status == 200 and retry["id"] == second["id"] and stale_status == 409,
          "保留旧件与冲突状态，重试返回同版，过期更正返回 409")

    unreadable = c.run_question("普通员工在上海住宿，每晚能报多少？早餐能单报吗？",
                                [rev_of[k] for k in ("D", "D_attach_scan", "A")])
    statuses = {claim["topic"]: claim["status"] for claim in unreadable["claims"]}
    check("scan-with-older-amount", statuses == {
        "住宿": "coverage_gap", "早餐": "no_evidence"},
        "旧金额不能填补住宿附件缺口，早餐保持独立")

    gap_run = runs["coverage-gap-D"]
    status, link = c.req("POST", "/supplements", {
        "original_revision_id": rev_of["D"], "gap": gap_run["claims"][0]["gap"],
        "supplement_revision_id": rev_of["D_attach_clear"]})
    assert status in (200, 201), link
    _, affected = c.req("GET", f"/supplements/{link['id']}/runs")
    status, rerun = c.req("POST", f"/runs/{gap_run['run_id']}/reruns", {
        "supplement_revision_ids": [rev_of["D_attach_clear"]],
        "request_id": uuid.uuid4().hex})
    assert status == 202, rerun
    updated = c.wait_run(rerun["run_id"])
    compared, _ = c.req("GET", f"/runs/{updated['run_id']}/comparison")
    check("supplement-rerun",
          any(row["run_id"] == gap_run["run_id"] for row in affected["runs"])
          and updated["parent_run_id"] == gap_run["run_id"]
          and updated["claims"][0]["status"] == "answered" and compared == 200,
          "关联受影响历史，补件后新建核查并生成对照")

    print("\n== 复合问题与历史定位 ==")
    scope = [rev_of["A"], rev_of["B"]]
    comparison = ("6月30日上海普通员工的住宿上限？"
                  "2026年7月1日上海普通员工的住宿上限？")
    original = c.run_question(comparison, scope)
    followup = original["claims"][0]["followup"] or {}
    corrected = c.run_question(comparison, scope, {"travel_date:0": "2026-06-30"})
    amounts = [claim["conclusion_text"] or "" for claim in corrected["claims"]]
    check("scoped-date-followup",
          followup.get("missing") == "travel_year" and followup.get("month_day") == "06-30"
          and len(amounts) == 2 and " 500 元" in amounts[0] and " 800 元" in amounts[1],
          "补年份只修改第一问，第二问继续使用 7 月 1 日")

    status, repeat = c.req("POST", f"/runs/{corrected['run_id']}/reruns",
                           {"request_id": uuid.uuid4().hex})
    assert status == 202, repeat
    repeated = c.wait_run(repeat["run_id"])
    status, difference = c.req("GET", f"/runs/{repeated['run_id']}/comparison")
    items = difference.get("items", [])
    check("compare-repeated-topic", status == 200 and len(items) == 2
          and all(not item["changed"] for item in items)
          and " 500 元" in items[0]["old"]["conclusion_text"]
          and " 800 元" in items[1]["old"]["conclusion_text"],
          "同一主题的两个事项分别对照，保持各自原结论")

    active = c.run_question("2026年2月30日上海普通员工住宿限额？", scope)
    check("invalid-calendar-date", active["claims"][0]["status"] == "needs_condition",
          "遇到不存在的公历日期时要求补充")

    old_word = next(ev for ev in runs["date-boundary-500"]["claims"][0]["evidences"]
                    if ev["format"] == "docx")
    version = old_word["version_id"]
    revision = old_word["revision_id"]
    tree_path = f"/versions/{version}/tree"
    before_status, before = c.req("GET", f"{tree_path}?revision_id={revision}")
    status, rebuild = c.req("POST", "/settings/rebuild-index", {"version_id": version})
    assert status == 200 and rebuild["rebuilt"], rebuild
    latest_revision = rebuild["rebuilt"][0]["revision_id"]
    after_status, after = c.req("GET", f"{tree_path}?revision_id={revision}")
    newest_status, newest = c.req("GET", tree_path)
    check("word-historical-revision",
          before_status == after_status == newest_status == 200 and before == after
          and newest.get("revision_id") == latest_revision and latest_revision != revision,
          "重建后默认读取最新修订，旧引用保持原修订")

    print("\n== 汇总 ==")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    if passed < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
