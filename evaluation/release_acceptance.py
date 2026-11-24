"""Generate and score a reproducible 30-document, 500-page synthetic test library."""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from time import perf_counter

import pymupdf
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "parsers")]
from traceqa_server import db
from traceqa_server.config import Config
from traceqa_server.imports_svc import ImportService
from traceqa_server.runs_svc import RunService

CITIES = ("上海", "北京", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安", "重庆")


def make_pdf(path: Path, pages: int, answer_page: int, text: str):
    with pymupdf.open() as pdf:
        for page_number in range(1, pages + 1):
            page = pdf.new_page()
            page.insert_text((56, 65), f"制度测试资料 · 第 {page_number} 页",
                             fontname="china-s", fontsize=15)
            if page_number == answer_page:
                page.insert_text((56, 111), text, fontname="china-s", fontsize=12)
            else:
                page.insert_text((56, 111), "本页归档说明：单据按事项整理，票据副本交档案人员。",
                                 fontname="china-s", fontsize=12)
        pdf.save(path)


def build_corpus(root: Path):
    documents = []
    cases = []
    for n, city in enumerate(CITIES, 1):
        family = f"F{n:02}"
        base, other, gap = (f"{family}-{suffix}" for suffix in ("A", "B", "C"))
        subject = "普通员工" if n % 2 else "中层干部"
        topic = ("住宿", "餐补", "交通补贴")[(n - 1) % 3]
        denom = "每人每晚" if topic == "住宿" else "每人每天"
        who = f"{city}{subject}"
        amount, newer = 100 + n * 17, 400 + n * 13
        main_clause = f"{who}{topic}{denom}不超过{amount}元，自2026年1月1日起执行。"
        second_clause = f"{who}{topic}{denom}不超过{newer}元，自2026年7月1日起执行。"
        gap_clause = f"{who}{topic}标准详见附件{n}《标准核准表》。"
        alternate_page = 2 + n
        gap_page = 23 - n
        word = Document()
        word.add_heading(f"{family}费用管理制度", 0)
        word.add_heading("核准条款", 1)
        word.add_paragraph(main_clause)
        word.save(root / f"{base}.docx")
        make_pdf(root / f"{other}.pdf", 25, alternate_page, second_clause)
        make_pdf(root / f"{gap}.pdf", 24, gap_page, gap_clause)
        documents.extend((f"{base}.docx", f"{other}.pdf", f"{gap}.pdf"))
        first = {"file": f"{base}.docx", "text": main_clause}
        second = {"file": f"{other}.pdf", "text": second_clause, "page": alternate_page}
        missing = {"file": f"{gap}.pdf", "text": gap_clause, "page": gap_page}
        for variant, ending in enumerate(("标准是多少？", "限额如何核算？")):
            stem = f"{who}的{topic}{ending}"
            rows = [
                ("answered", f"2026年6月{10 + variant}日{stem}", [base, other], [first], amount),
                ("conflict", f"2026年8月{10 + variant}日{stem}", [base, other], [first, second], None),
                ("needs_condition", stem, [base, other], [first, second], None),
                ("no_evidence", f"{who}早餐{'能报销' if variant else '有没有单列规定'}？",
                 [base, other], [], None),
                ("coverage_gap", stem, [base, gap], [missing], None),
            ]
            for expected, question, scope, evidence, value in rows:
                cases.append({"id": f"{family}-{expected}-{variant}", "family": family,
                              "question": question, "scope": scope, "expected": expected,
                              "evidence": evidence, "amount": value})
    wider_scope_case = {**cases[0], "id": "cross-department-gaps"}
    wider_scope_case["scope"] = [Path(name).stem for name in documents
                                 if Path(name).stem != "F01-C"]
    cases.append(wider_scope_case)
    return documents, cases


def matches(mark, file, quote, page):
    def normalized(text):
        return "".join(text.split()).replace("．", ".")
    return (file == mark["file"] and normalized(mark["text"]) in normalized(quote or "")
            and (mark.get("page") is None or mark["page"] == page))


def locator_matches(mark, locator):
    if "page" not in mark:
        return (locator.get("kind") == "docx" and locator.get("block_path") == [2]
                and locator.get("text_start") == 0 and locator.get("text_end", 0) > 0)
    return (locator.get("kind") == "pdf" and locator.get("page_index") == mark["page"] - 1
            and any(box["x0"] <= 0.14 <= box["x1"] and box["y0"] <= 0.125 <= box["y1"]
                    for box in locator.get("boxes", [])))


def rate(n, d):
    return {"numerator": n, "denominator": d, "rate": round(n / d, 4) if d else None}


def evaluate(root: Path, documents: list[str], cases: list[dict]):
    cfg = Config(data_dir=root / "library", ollama_model="", embedding_model="")
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    imports = ImportService(cfg)
    service = RunService(cfg, cfg.db_path)
    counts = Counter()
    outputs = []
    with closing(db.connect(cfg.db_path)) as conn:
        revisions = {}
        for name in documents:
            content = (root / name).read_bytes()
            batch = imports.create_import(conn, [(name, content)])
            item = imports.get_import(conn, batch)["files"][0]
            imports.process_file(conn, item["id"], name, content)
            finished = imports.get_import(conn, batch)["files"][0]
            if finished["status"] != "done":
                raise RuntimeError(f"{name}: {finished['error']}")
            revisions[Path(name).stem] = finished["revision_id"]
        files = {revision: name for name in documents
                 for revision in [revisions[Path(name).stem]]}
        for case in cases:
            scope = [revisions[k] for k in case["scope"]]
            _, submitted = service.submit(conn, case["id"], case["question"], scope, {})
            started = perf_counter()
            service.execute(submitted["run_id"])
            elapsed = round((perf_counter() - started) * 1000, 2)
            snapshot = service.snapshot(conn, submitted["run_id"])
            claims = snapshot["claims"]
            actual = claims[0] if claims else {"status": snapshot["status"], "evidences": []}
            expected = case["expected"]
            observed = actual["status"]
            counts["correct"] += observed == expected
            counts[f"status:{expected}"] += 1
            counts["missed_refusal"] += (expected != "answered" and observed == "answered")
            counts["false_refusal"] += (expected == "answered" and observed != "answered")
            counts["conflict_hit"] += (expected == observed == "conflict")
            counts["conflict_false"] += (observed == "conflict" and expected != "conflict")
            ranked, _ = service._retrieve(conn, case["question"], scope)
            all_hits = []
            for mark in case["evidence"]:
                rank = next((i for i, row in enumerate(ranked, 1)
                             if matches(mark, files[row["revision_id"]], row["text_orig"], row["page_no"])), None)
                all_hits.append(rank)
                counts["annotation"] += 1
                counts["top5_hit"] += rank is not None and rank <= 5
            if all_hits:
                counts["with_evidence"] += 1
                counts["all_top5"] += all(rank is not None and rank <= 5 for rank in all_hits)
            cited = []
            if expected in {"answered", "conflict", "coverage_gap"}:
                for mark in case["evidence"]:
                    fmt = Path(mark["file"]).suffix
                    counts[f"location:{fmt}"] += 1
                    hit = any(matches(mark, ev["file_name"], ev["quote"], ev["page_no"])
                              and locator_matches(mark, ev["locator"])
                              for ev in actual["evidences"])
                    counts[f"located:{fmt}"] += hit
                    cited.append(hit)
            if observed == "answered":
                counts["answered"] += 1
                counts["supported"] += (expected == "answered" and all(cited)
                                         and f" {case['amount']} 元" in (actual["conclusion_text"] or ""))
            outputs.append({**case, "observed": observed, "claim": actual,
                            "top_ranks": all_hits, "latency_ms": elapsed})
    return counts, outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    os.environ.setdefault("TRACEQA_PDF_BACKEND", "fitz")
    with tempfile.TemporaryDirectory(prefix="traceqa-release-") as temp:
        corpus = Path(temp)
        documents, cases = build_corpus(corpus)
        counts, outputs = evaluate(corpus, documents, cases)

    times = sorted(item["latency_ms"] for item in outputs)
    report = {
        "kind": "synthetic-reproducible", "platform": platform.platform(),
        "processor": platform.processor(), "documents": documents, "page_equivalent": 500,
        "labels": {key.removeprefix("status:"): n for key, n in counts.items()
                   if key.startswith("status:")},
        "metrics": {
            "status_accuracy": rate(counts["correct"], len(cases)),
            "recall_at_5": rate(counts["top5_hit"], counts["annotation"]),
            "all_required_at_5": rate(counts["all_top5"], counts["with_evidence"]),
            "supported_answers_proxy": rate(counts["supported"], counts["answered"]),
            "false_refusal": rate(counts["false_refusal"], counts["status:answered"]),
            "missed_refusal": rate(counts["missed_refusal"], len(cases) - counts["status:answered"]),
            "conflict_recall": rate(counts["conflict_hit"], counts["status:conflict"]),
            "conflict_false_alarm": rate(counts["conflict_false"], len(cases) - counts["status:conflict"]),
            "word_locator": rate(counts["located:.docx"], counts["location:.docx"]),
            "pdf_locator": rate(counts["located:.pdf"], counts["location:.pdf"]),
        },
        "rule_only_worker_ms": {"median": statistics.median(times),
                               "p95": times[(len(times) * 95 + 99) // 100 - 1]},
        "cases": outputs,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
