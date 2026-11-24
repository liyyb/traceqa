import ComparisonModal from "../components/ComparisonModal";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api, requestIdFor, clearRequestId, tokenParam } from "../api";
import {
  RunSnapshot, RunStatus, STAGE_LABEL, EvidenceView, ClaimView,
  ScopeCatalogItem, ScopeUpdate,
} from "../types";
import ClaimCard from "../components/ClaimCard";
import type { WorkbenchRequest } from "../App";
const ReaderPane = lazy(() => import("../components/ReaderPane"));

interface ScopeInfo {
  revisions: string[];
  detail: { revision_id: string; available: boolean;
            file_name: string | null; title: string | null; folder_path?: string }[];
  catalog: ScopeCatalogItem[];
  updates: ScopeUpdate[];
  run_id?: string;
  created_at?: string;
}

type Phase = "ask" | "submitting" | "running" | "done";
const ACTIVE_KEY = "tq_active_run";
const QUESTION_KEY = "tq_question";
const DEFAULT_EXAMPLE = "2026年7月10日普通员工去上海住宿，每晚能报多少？早餐能单报吗？";

export default function Workbench({ catalogue, request, onResult, onMaterials }: {
  catalogue: number;
  request?: WorkbenchRequest;
  onResult: () => void;
  onMaterials: (gap?: boolean) => void;
}) {
  const [phase, setPhase] = useState<Phase>("ask");
  const [question, setQuestion] = useState(
    () => sessionStorage.getItem(QUESTION_KEY) || "");
  const [scope, setScope] = useState<ScopeInfo | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [scopeOpen, setScopeOpen] = useState(false);
  const [example, setExample] = useState<{ scope: string; question: string | null }>();
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [stageText, setStageText] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [readerEv, setReaderEv] = useState<EvidenceView | null>(null);
  const [readerList, setReaderList] = useState<EvidenceView[]>([]);
  const [readerIndex, setReaderIndex] = useState(0);
  const [readerAnchor, setReaderAnchor] = useState<string | null>(null);
  const readerOffset = useRef<number | null>(null);
  const [readerTopic, setReaderTopic] = useState("");
  const [narrow, setNarrow] = useState(false);
  const runIdRef = useRef<string | null>(null);
  const lastSeqRef = useRef(0);
  const eventsRef = useRef<EventSource | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();
  const scopeInitialized = useRef(false);

  // 断点：工作区可用宽度低于 1000 CSS px → 原文占满（手册 §3）
  useEffect(() => {
    const check = () => setNarrow(window.innerWidth < 1000);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  useEffect(() => { sessionStorage.setItem(QUESTION_KEY, question); }, [question]);

  const loadScope = useCallback(async () => {
    try {
      const s = await api<ScopeInfo>("/scope/last");
      const catalog = s.catalog || [];
      setScope({ ...s, catalog, updates: s.updates || [],
                 detail: s.detail || [] });
      const firstSelection = !scopeInitialized.current && catalog.length > 0;
      if (firstSelection) scopeInitialized.current = true;
      setSelected((prev) => {
        if (!firstSelection) {
          const available = new Set(catalog.map((item) => item.revision_id));
          return prev.filter((id) => available.has(id));
        }
        if (s.revisions?.length) {
          const available = new Set((s.detail || [])
            .filter((d) => d.available).map((d) => d.revision_id));
          return s.revisions.filter((r) => available.has(r));
        }
        // 第一次使用：引导选择，默认全部最新可用修订
        return catalog.filter((c) => c.is_latest).map((c) => c.revision_id);
      });
    } catch { /* 首次使用 */ }
  }, []);

  useEffect(() => { loadScope(); }, [loadScope, catalogue]);

  const exampleScope = JSON.stringify([...selected].sort());
  useEffect(() => {
    if (phase !== "ask" || exampleScope === "[]") return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void api<{ question: string | null }>("/scope/example", {
        method: "POST", body: `{"revision_ids":${exampleScope}}`, signal: controller.signal,
      }).then(({ question }) => {
        if (!controller.signal.aborted) setExample({ scope: exampleScope, question });
      }).catch(() => {});
    }, 150);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [exampleScope, phase]);

  // ---------------- SSE 订阅（断线重连：提交最近 seq） ----------------
  const subscribe = useCallback((runId: string) => {
    eventsRef.current?.close();
    clearTimeout(reconnectRef.current);
    const es = new EventSource(
      `/api/runs/${runId}/events?last_seq=${lastSeqRef.current}&token=${tokenParam()}`);
    eventsRef.current = es;
    es.addEventListener("stage", (e: MessageEvent) => {
      const ev = JSON.parse(e.data);
      lastSeqRef.current = ev.seq;
      const label = STAGE_LABEL[ev.stage as RunStatus] || ev.stage;
      setStageText(ev.total != null ? `${label} ${ev.done}/${ev.total}` : label);
    });
    es.addEventListener("done", async (e: MessageEvent) => {
      es.close();
      const d = JSON.parse(e.data);
      const snapshot = await api<RunSnapshot>(`/runs/${runId}`).catch(() => null);
      if (eventsRef.current !== es) return;
      if (!snapshot) {
        reconnectRef.current = setTimeout(() => subscribe(runId), 800);
        return;
      }
      setSnapshot(snapshot);
      localStorage.removeItem(ACTIVE_KEY);
      if (d.status === "completed") {
        setPhase("done");
        onResult();
        await loadScope();  // 范围在提交时保存，完成后刷新新版提示
      } else {
        setError(`核查${STAGE_LABEL[d.status as RunStatus] || d.status}`);
        setPhase("ask");
      }
    });
    es.onerror = async () => {
      es.close();
      // 断线恢复：终态一律从快照恢复（手册 §4.1）
      const snap = await api<RunSnapshot>(`/runs/${runId}`).catch(() => null);
      if (eventsRef.current !== es) return;
      if (snap) {
        setSnapshot(snap);
        if (snap.status === "completed") {
          localStorage.removeItem(ACTIVE_KEY);
          setPhase("done"); onResult(); return;
        }
        if (["failed", "cancelled", "interrupted"].includes(snap.status)) {
          localStorage.removeItem(ACTIVE_KEY);
          setError(`核查${STAGE_LABEL[snap.status]}`);
          setPhase("ask");
          return;
        }
      }
      reconnectRef.current = setTimeout(() => subscribe(runId), 800);
    };
  }, [loadScope, onResult]);

  useEffect(() => () => {
    clearTimeout(reconnectRef.current);
    eventsRef.current?.close();
    eventsRef.current = null;
  }, []);

  // 刷新后恢复进行中的任务（手册 §2「刷新后从任务记录恢复」）
  useEffect(() => {
    if (request && "question" in request) {
      clearTimeout(reconnectRef.current);
      eventsRef.current?.close();
      eventsRef.current = null;
      setReaderEv(null);
      setQuestion(request.question);
      setError(null);
      setPhase("ask");
      return;
    }
    const saved = request && "runId" in request ? request.runId : localStorage.getItem(ACTIVE_KEY);
    if (!saved) return;
    let alive = true;
    (async () => {
      try {
        const snap = await api<RunSnapshot>(`/runs/${saved}`);
        if (!alive) return;
        setReaderEv(null);
        setQuestion((q) => request ? snap.question : q || snap.question);
        setSnapshot(snap);
        if (snap.status === "completed") {
          localStorage.removeItem(ACTIVE_KEY);
          setPhase("done");
          onResult();
        } else if (["failed", "cancelled", "interrupted"].includes(snap.status)) {
          localStorage.removeItem(ACTIVE_KEY);
          setError(`核查${STAGE_LABEL[snap.status]}，可重新提交`);
          setPhase("ask");
        } else {
          runIdRef.current = saved;
          localStorage.setItem(ACTIVE_KEY, saved);
          lastSeqRef.current = snap.seq;
          setStageText(snap.stage_label || STAGE_LABEL[snap.status]);
          setPhase("running");
          subscribe(saved);
        }
      } catch {
        localStorage.removeItem(ACTIVE_KEY);
      }
    })();
    return () => { alive = false; };
  }, [request, subscribe, onResult]);

  // ---------------- 提交 ----------------
  const submit = async (overrideConditions?: Record<string, string>, reuseQuestion?: string,
                        scope = selected) => {
    const q = (reuseQuestion ?? question).trim();
    if (!q || scope.length === 0) return;
    setPhase("submitting");
    setError(null);
    const reqId = requestIdFor("run");
    try {
      const res = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "X-TraceQA-Token": sessionStorage.getItem("tq_token") || "" },
        body: JSON.stringify({
          request_id: reqId, question: q,
          revision_ids: scope,
          conditions: overrideConditions || {},
        }),
      });
      const body = await res.json();
      if (!res.ok) {
        if (res.status < 500) clearRequestId("run");
        setError(body.error || "提交失败");
        setPhase("ask");
        return;
      }
      clearRequestId("run");
      runIdRef.current = body.run_id;
      localStorage.setItem(ACTIVE_KEY, body.run_id);
      lastSeqRef.current = 0;
      setStageText(STAGE_LABEL.queued);
      setPhase("running");
      subscribe(body.run_id);
    } catch (e: any) {
      setError(e.message || "网络错误");
      setPhase("ask");
    }
  };

  const cancel = async () => {
    if (!runIdRef.current) return;
    setCancelling(true);
    try {
      await api(`/runs/${runIdRef.current}/cancel`, { method: "POST" });
    } finally {
      setTimeout(() => setCancelling(false), 1500);
    }
  };

  // 追问：补一个条件后沿用已选版本重新核查（手册 §2 核对或追问）
  const followup = (claim: ClaimView, value: string) => {
    if (!snapshot || !claim.followup) return;
    const suppliedDate = claim.followup.missing === "travel_year" && claim.followup.month_day
      ? `${value}-${claim.followup.month_day}`
      : value;
    const conds = { ...snapshot.conditions, [`travel_date:${claim.seq_no}`]: suppliedDate };
    setQuestion(snapshot.question);
    submit(conds, snapshot.question, snapshot.scope);
  };

  const citeNumbers: Record<string, number> = {};
  if (snapshot) {
    for (const c of snapshot.claims) {
      for (const ev of c.evidences) {
        if (!(ev.id in citeNumbers)) citeNumbers[ev.id] = Object.keys(citeNumbers).length + 1;
      }
    }
  }

  const restoreAnchor = (anchor: string | null) => {
    requestAnimationFrame(() => {
      const el = anchor ? document.getElementById(anchor) : null;
      if (el) {
        if (readerOffset.current !== null) {
          window.scrollBy(0, el.getBoundingClientRect().top - readerOffset.current);
        }
        (el as HTMLElement).focus({ preventScroll: true });
      }
    });
  };

  const closeReader = (fromPop = false) => {
    if (!fromPop && window.history.state?.tqReader) {
      window.history.back();   // popstate 会再次调用 closeReader(true)
      return;
    }
    const anchor = readerAnchor;
    setReaderEv(null);
    restoreAnchor(anchor);
  };

  useEffect(() => {
    const onPop = () => { if (readerEv) closeReader(true); };
    const onKey = (event: KeyboardEvent) => {
      if (readerEv && event.key === "Escape" && !event.defaultPrevented) closeReader();
    };
    window.addEventListener("popstate", onPop);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("keydown", onKey);
    };
  });

  const openEvidence = (ev: EvidenceView, anchorId: string,
                        list: EvidenceView[], index: number) => {
    const claim = snapshot?.claims.find((c) => c.evidences.some((e) => e.id === ev.id));
    if (!readerEv) readerOffset.current = document.getElementById(anchorId)?.getBoundingClientRect().top ?? null;
    setReaderEv(ev);
    setReaderList(list);
    setReaderIndex(index);
    setReaderAnchor(anchorId);
    setReaderTopic(claim?.topic || ev.doc_title);
    if (!readerEv && narrow) window.history.pushState({ tqReader: true }, "");
    if (!narrow) requestAnimationFrame(() => document.getElementById(anchorId)?.scrollIntoView({ block: "nearest" }));
  };

  const navigateReader = (i: number) => {
    if (!readerList.length) return;
    const next = (i + readerList.length) % readerList.length;
    setReaderIndex(next);
    setReaderEv(readerList[next]);
  };

  const toggleSelected = (rid: string) => {
    setSelected((prev) => prev.includes(rid) ? prev.filter((x) => x !== rid)
      : [...prev, rid]);
  };

  const adoptNew = (up: ScopeUpdate) => {
    setSelected((prev) => {
      const base = prev.includes(up.revision_id)
        ? prev.map((x) => x === up.revision_id ? up.newer_revision_id : x)
        : [...prev, up.newer_revision_id];
      return [...new Set(base)];
    });
  };

  const nameOf = (rid: string) => {
    const item = scope?.catalog.find((c) => c.revision_id === rid) ||
                 scope?.detail.find((d) => d.revision_id === rid);
    return item?.file_name ? [item.folder_path, item.file_name].filter(Boolean).join(" / ") : rid;
  };

  const scopeNames = selected.map(nameOf);
  const unavailable = (scope?.detail || []).filter(
    (d) => selected.includes(d.revision_id) && !d.available);
  const grouped = (scope?.catalog || []).reduce<Record<string, ScopeCatalogItem[]>>(
    (acc, item) => {
      (acc[item.document_id] ||= []).push(item);
      return acc;
    }, {});

  const scopeBar = (
    <div className="scope-bar">
      <div className="flex">
        <span>{selected.length ? `已选择 ${selected.length} 份材料` : "请选择核查材料"}</span>
        {scopeNames.length > 0 && <ul className="scope-files">
          {scopeNames.slice(0, 3).map((name, index) => <li key={selected[index]}>{name}</li>)}
        </ul>}
        <button className="ghost" aria-expanded={scopeOpen} onClick={() => setScopeOpen(!scopeOpen)}>
          {scopeOpen ? "收起范围" : "调整范围"}
        </button>
      </div>
      {(scope?.updates?.length || 0) > 0 && (
        <div className="newver mt8">
          {scope!.updates.length} 份有新版（默认沿用当前版本，不会静默更新）
        </div>
      )}
      {unavailable.length > 0 && (
        <div className="error-box">
          有 {unavailable.length} 项材料已移除或无法读取，提交前请取消勾选或在材料页处理。
        </div>
      )}
      {scopeOpen && (
        <div className="scope-panel mt8">
          {Object.entries(grouped).map(([docId, items]) => (
            <div key={docId} className="scope-group">
              <b>{[items[0].folder_path, items[0].title].filter(Boolean).join(" / ")}</b>
              {items.map((item) => (
                <label key={item.revision_id} className="scope-item">
                  <input type="checkbox" checked={selected.includes(item.revision_id)}
                         onChange={() => toggleSelected(item.revision_id)} />
                  <span>{item.file_name}</span>
                  <span className="tag">{item.format.toUpperCase()}</span>
                  {item.is_latest && <span className="tag ok">当前版本</span>}
                  {!item.is_latest && <span className="tag warn">旧版本</span>}
                  <span className="muted">{item.imported_at}</span>
                </label>
              ))}
            </div>
          ))}
          {(scope?.updates || []).length > 0 && (
            <div className="scope-group">
              <b>发现新修订</b>
              {scope!.updates.map((up, i) => (
                <div className="scope-item" key={i}>
                  <span>{up.file_name} → <b>{up.newer_file_name}</b></span>
                  <span className="muted">{up.coverage_change}</span>
                  <button className="secondary" onClick={() => adoptNew(up)}>
                    采用新版
                  </button>
                  <button className="ghost" onClick={() => {
                    setSelected((prev) => prev.includes(up.newer_revision_id)
                      ? prev.map((x) => x === up.newer_revision_id
                        ? up.revision_id : x) : prev);
                  }}>沿用当前版本</button>
                </div>
              ))}
              <div className="muted">提交前始终可见实际版本清单：{scopeNames.join("、")}</div>
            </div>
          )}
          <button className="ghost mt8" onClick={() => onMaterials()}>
            前往材料页导入
          </button>
        </div>
      )}
    </div>
  );

  // ---------------- 画面 ----------------
  if (phase === "running" || phase === "submitting") {
    return (
      <div className="ask-wrap run-pending">
        {scopeBar}
        <div className="result-head"><h1>{question}</h1></div>
        <div className="progress-line" role="status" aria-live="polite">
          <span className="stage">{phase === "submitting" ? "正在提交" : stageText}</span>
          <button className="secondary" onClick={cancel} disabled={cancelling}>
            {cancelling ? "正在取消…" : "取消核查"}
          </button>
        </div>
        {error && <div className="error-box">{error}</div>}
      </div>
    );
  }

  if (phase === "done" && snapshot) {
    const split = readerEv && !narrow;
    const reader = readerEv && (
      narrow ? (
        <div className="reader-full" role="region" aria-label="原文阅读器">
          <div className="reader-top">
            <button className="ghost" autoFocus onClick={() => closeReader()}>
              ← 返回 · {readerTopic}
            </button>
          </div>
          <ReaderPane evidence={readerEv} full evidenceList={readerList}
                      index={readerIndex} onNavigate={navigateReader} />
        </div>
      ) : (
        <div className="right">
          <ReaderPane evidence={readerEv} evidenceList={readerList}
                      index={readerIndex} onNavigate={navigateReader}
                      onClose={() => closeReader()} />
        </div>
      )
    );

    return (
      <div className={split ? "reader-layout" : undefined}>
        <div className={split ? "left" : "result-wrap"} hidden={!!readerEv && narrow}>
          <ResultsBody key={snapshot.run_id} snapshot={snapshot} citeNumbers={citeNumbers}
                       onOpenEvidence={openEvidence} onFollowup={followup}
                       onMaterials={() => onMaterials(true)} onRecord={onResult}
                       onRerun={() => { setReaderEv(null); setPhase("ask"); }} />
        </div>
        {reader && (
          <Suspense fallback={<p className="muted" role="status">正在打开原文…</p>}>
            {reader}
          </Suspense>
        )}
      </div>
    );
  }

  // ---------------- 提问画面 ----------------
  const firstUse = !scope?.catalog?.length;
  const suggested = example?.scope === exampleScope ? example.question : null;
  const placeholder = firstUse ? `例如：${DEFAULT_EXAMPLE}`
    : !selected.length ? "选择核查材料后，可在这里看到基于原文的提问示例。"
    : suggested ? `例如：${suggested}` : "输入一个与所选材料相关的问题";
  return (
    <div className="ask-wrap composer-layout">
      <div className="composer-main">
      <label className="composer-label" htmlFor="trace-question">核查问题</label>
      <textarea
        id="trace-question"
        className="ask-box"
        placeholder={placeholder}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submit();
        }}
      />
      <div className="ask-actions">
        <button onClick={() => submit()}
                disabled={!question.trim() || selected.length === 0}>
          开始核查
        </button>
        <kbd>Ctrl + Enter</kbd>
      </div>
      {error && <div className="error-box">{error}</div>}
      </div>
      <aside className="composer-scope" aria-labelledby="scope-title">
        <h2 id="scope-title">材料范围</h2>
        {firstUse ? <p>添加 Word 或 PDF 后即可开始核查。</p> : scopeBar}
        <button type="button" className="scope-add" onClick={() => onMaterials()}>导入材料</button>
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------- 结果主体

function ResultsBody(props: {
  snapshot: RunSnapshot;
  citeNumbers: Record<string, number>;
  onOpenEvidence: (ev: EvidenceView, anchor: string,
                   list: EvidenceView[], index: number) => void;
  onFollowup: (claim: ClaimView, value: string) => void;
  onMaterials: () => void;
  onRecord: () => void;
  onRerun: () => void;
}) {
  const { snapshot } = props;
  const [preview, setPreview] = useState<any | null>(null);
  const [issued, setIssued] = useState<any | null>(null);
  const [comparison, setComparison] = useState<any | null>(null);

  const doPreview = async () => {
    try { setPreview(await api(`/runs/${snapshot.run_id}/export`)); }
    catch (e: any) { alert(e.message); }
  };
  const doIssue = async () => {
    try {
      const rec = await api("/records", {
        method: "POST",
        body: JSON.stringify({
          run_id: snapshot.run_id,
          request_id: requestIdFor("record", true),
        }),
      });
      clearRequestId("record");
      setIssued(rec);
      props.onRecord();
    } catch (e: any) { alert(e.message); }
  };

  return (
    <>
      <div className="result-head">
        <div className="muted">核查范围：{
          snapshot.scope_detail.map((s) => s.file_name).join("、")}</div>
        <h2>{snapshot.question}</h2>
        <div className="flex">
          <button className="secondary" onClick={doPreview}>预览核查记录</button>
          <button onClick={doIssue}>生成正式记录</button>
          <button className="ghost" onClick={props.onRerun}>发起新核查</button>
          {snapshot.parent_run_id && (
            <button className="ghost" onClick={async () => {
              try { setComparison(await api(`/runs/${snapshot.run_id}/comparison`)); }
              catch (e: any) { alert(e.message); }
            }}>查看重查对照</button>
          )}
        </div>
        {issued && (
          <div className="note-block">
            已生成正式记录 <b>{issued.serial_no} / {issued.rev_no}</b>
            <a href={`/api/records/${issued.id}/download?token=${tokenParam()}`}> 下载</a>
            <span className="muted">（重复下载同一编号返回同一内容）</span>
          </div>
        )}
        {preview && <PreviewModal preview={preview} onClose={() => setPreview(null)} />}
        {comparison && <ComparisonModal data={comparison}
                                        onClose={() => setComparison(null)} />}
      </div>
      {snapshot.claims.map((c) => (
        <ClaimCard key={c.id} claim={c} runId={snapshot.run_id}
                   citeNumbers={props.citeNumbers}
                   onOpenEvidence={props.onOpenEvidence}
                   onMaterials={props.onMaterials}
                   onFollowup={props.onFollowup} />
      ))}
    </>
  );
}

function PreviewModal({ preview, onClose }: { preview: any; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const s = preview.summary || {};
  return (
    <div className="preview-overlay" role="dialog" aria-label="核查记录预览">
      <div className="preview-sheet">
        <div className="preview-head">
          <h2>核查记录预览（草稿状态 · 按实际导出顺序排版）</h2>
          <div className="flex">
            {s.confirmed > 0 && <a className="tag ok" href={`#${s.confirmed_ids?.[0]}`}>确定结论 {s.confirmed}</a>}
            {s.pending > 0 && <span className="tag warn">待确认项 {s.pending}</span>}
            {s.gaps > 0 && <span className="tag warn">材料缺口 {s.gaps}</span>}
            <button className="ghost" onClick={onClose}>关闭（Esc）</button>
          </div>
        </div>
        <iframe title="核查记录草稿" className="preview-frame"
                srcDoc={preview.html || "<p>草稿生成失败</p>"} />
      </div>
    </div>
  );
}
