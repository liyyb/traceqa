import { useCallback, useEffect, useState } from "react";
import { api, requestIdFor, clearRequestId, tokenParam } from "../api";
import ComparisonModal from "../components/ComparisonModal";
import type { WorkbenchRequest } from "../App";
import { STAGE_LABEL, RunStatus } from "../types";

export default function History({ refresh, onRun }: {
  refresh: number;
  onRun: (request: WorkbenchRequest) => void;
}) {
  const [runs, setRuns] = useState<any[]>([]);
  const [records, setRecords] = useState<any[]>([]);
  const [q, setQ] = useState("");
  const [comparison, setComparison] = useState<any | null>(null);
  const [correction, setCorrection] = useState<any | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);

  const load = useCallback(async (signal: AbortSignal) => {
    const [h, r] = await Promise.all([
      api(`/history${q ? `?q=${encodeURIComponent(q)}` : ""}`, { signal }),
      api(`/records${q ? `?q=${encodeURIComponent(q)}` : ""}`, { signal }),
    ]);
    if (signal.aborted) return;
    setRuns(h.runs);
    setRecords(r.records);
  }, [q]);
  useEffect(() => {
    const controller = new AbortController();
    setLoadError("");
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        await load(controller.signal);
      } catch {
        if (!controller.signal.aborted) setLoadError("无法加载核查记录，请重试。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, q ? 180 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load, q, refresh, retry]);

  const rerun = async (runId: string, useCurrent: boolean) => {
    // 沿用当时版本：以原范围重查；使用当前版本：到工作台显式选新版
    if (useCurrent) {
      const previous = runs.find((item) => item.run_id === runId);
      if (previous) onRun({ question: previous.question });
      return;
    }
    try {
      const run = await api(`/runs/${runId}/reruns`, {
        method: "POST",
        body: JSON.stringify({
          request_id: requestIdFor("rerun", true),
          supplement_revision_ids: [],
        }),
      });
      clearRequestId("rerun");
      onRun({ runId: run.run_id });
    } catch (e: any) {
      if (e.body?.missing) {
        alert(`原范围中的必需原件缺失：${e.body.missing.join("、")}`);
      } else alert(e.message);
    }
  };

  // 同一编号的最新版本（用于「本机已有 R2」提示与跳转）
  const latestOf = (r: any) => records
    .filter((x) => x.serial_no === r.serial_no)
    .sort((a, b) => (a.rev_no < b.rev_no ? 1 : -1))[0];

  return (
    <div className="history-body">
      <div className="flex">
        <input type="search" aria-label="搜索核查记录" placeholder="按文件名或问题搜索" value={q}
               onChange={(e) => setQ(e.target.value)} />
      </div>
      {loading && <p className="muted" role="status">正在读取核查记录…</p>}
      {loadError && <p className="error-box" role="alert">{loadError} <button onClick={() => setRetry(retry + 1)}>重试</button></p>}

      <h2 className="mt16" style={{ fontSize: 18 }}>核查记录</h2>
      {!loading && !loadError && runs.length === 0 && <p className="muted">{q ? "没有匹配的核查记录。" : "完成一次核查后，记录将在这里显示。"}</p>}
      {runs.map((r) => (
        <div className="list-card" key={r.run_id}>
          <div className="row1">
            <b>{r.question}</b>
            <span className={`tag ${r.status === "completed" ? "ok" : "warn"}`}>
              {STAGE_LABEL[r.status as RunStatus] || r.status}
            </span>
            {r.parent_run_id && <span className="tag">材料重查</span>}
          </div>
          <div className="meta">
            {r.created_at} · 范围：{r.files.join("、")}
          </div>
          <div className="flex mt8">
            <button className="ghost" onClick={() => rerun(r.run_id, false)}>
              重新执行（沿用当时版本）
            </button>
            <button className="ghost" onClick={() => rerun(r.run_id, true)}>
              使用当前版本
            </button>
            {r.parent_run_id && (
              <button className="secondary" onClick={async () => {
                setComparison(await api(`/runs/${r.run_id}/comparison`));
              }}>查看对照</button>
            )}
          </div>
        </div>
      ))}

      <h2 className="mt16" style={{ fontSize: 18 }}>正式记录</h2>
      {records.length === 0 && <div className="muted">尚无正式记录。</div>}
      {records.map((r) => {
        const latest = latestOf(r);
        const isLatest = latest?.id === r.id;
        return (
          <div className="list-card" key={r.id}>
            <div className="row1">
              <b>{r.serial_no} / {r.rev_no}</b>
              <span className="meta">{r.created_at}</span>
              {r.correction_reason && <span className="tag warn">说明更正</span>}
              {!isLatest && <span className="tag warn">本机已有 {latest.rev_no}</span>}
            </div>
            <div className="meta">{r.question}</div>
            {r.correction_reason && <div className="meta">更正原因：{r.correction_reason}</div>}
            <div className="flex mt8">
              <a href={`/api/records/${r.id}/download?token=${tokenParam()}`}>下载本版</a>
              {!isLatest && (
                <button className="ghost" onClick={async () => {
                  setCorrection({ record: await api(`/records/${latest.id}`),
                                 reason: "", updates: {} });
                }}>查看最新更正</button>
              )}
              <button className="ghost" onClick={async () => {
                const full = await api(`/records/${r.id}`);
                setCorrection({ record: full, reason: "", updates: {} });
              }}>创建更正版</button>
            </div>
          </div>
        );
      })}

      {comparison && <ComparisonModal data={comparison} onClose={() => setComparison(null)} />}
      {correction && (
        <CorrectionModal state={correction}
                         onClose={() => setCorrection(null)}
                         onDone={() => { setCorrection(null); setRetry((value) => value + 1); }} />
      )}
    </div>
  );
}

function CorrectionModal({ state, onClose, onDone }:
  { state: any; onClose: () => void; onDone: () => void }) {
  const rec = state.record;
  const snap = rec.snapshot;
  const [reason, setReason] = useState("");
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const withNotes = snap.claims.filter((c: any) => c.notes?.length);

  const submit = async () => {
    setErr(null);
    // 字段名与后端一致；来源/沟通时间/署名沿用原说明，只有正文改动才提交
    const note_updates = withNotes
      .map((c: any) => ({
        claim_id: c.id,
        body: updates[c.id] ?? c.notes[0].body,
        source: c.notes[0].source,
        contact_time: c.notes[0].contact_time,
        signature: c.notes[0].signature,
      }))
      .filter((u: any, i: number) => u.body.trim() !== withNotes[i].notes[0].body.trim());
    if (note_updates.length === 0) { setErr("请至少修改一条人工说明"); return; }
    setBusy(true);
    try {
      await api(`/records/${rec.id}/corrections`, {
        method: "POST",
        body: JSON.stringify({
          base_rev_no: rec.rev_no,
          reason, note_updates,
          request_id: requestIdFor("corr", true),
        }),
      });
      clearRequestId("corr");
      onDone();
    } catch (e: any) {
      if (e.status === 409 && e.body?.current_rev) {
        setErr(`版本已更新：本机已有 ${e.body.current_rev}，请基于最新版本重试。编辑内容已保留。`);
      } else setErr(e.message);
    } finally { setBusy(false); }
  };

  return (
    <div className="preview-overlay" role="dialog" aria-label="创建更正版">
      <div className="preview-sheet" style={{ maxWidth: 720 }}>
        <div className="preview-head">
          <h2>创建更正版 · {rec.serial_no}（基于 {rec.rev_no}）</h2>
          <button className="ghost" onClick={onClose}>撤销（Esc）</button>
        </div>
        <div style={{ padding: 16 }}>
          <div className="muted">
            版本链：{rec.chain.map((c: any) => c.rev_no).join(" → ")} ·
            仅更正人工说明时复用冻结的核查结果，不触发模型调用。
          </div>
          {withNotes.length === 0 && (
            <div className="error-box">该记录没有人工说明，无需创建说明更正版。</div>
          )}
          {withNotes.map((c: any) => (
            <div key={c.id} className="note-block mt8">
              <b>{c.topic}</b>
              <div className="muted small">原说明：{c.notes[0].body}</div>
              <div className="muted small">来源：{c.notes[0].source}
                {c.notes[0].contact_time ? ` · 沟通时间：${c.notes[0].contact_time}` : ""}
                {c.notes[0].signature ? ` · 记录人：${c.notes[0].signature}` : ""}</div>
              <textarea rows={2} className="mt8" style={{ width: "100%" }}
                        placeholder="修改后说明"
                        value={updates[c.id] ?? c.notes[0].body}
                        onChange={(e) => setUpdates({ ...updates,
                          [c.id]: e.target.value })} />
            </div>
          ))}
          <label className="mt8">更正原因（必填）
            <textarea rows={2} style={{ width: "100%" }} value={reason}
                      onChange={(e) => setReason(e.target.value)} />
          </label>
          {err && <div className="error-box">{err}</div>}
          <div className="flex mt8">
            <button onClick={submit} disabled={!reason.trim() || busy || !withNotes.length}>
              提交更正
            </button>
            <button className="ghost" onClick={onClose}>撤销（保留已编辑内容）</button>
          </div>
        </div>
      </div>
    </div>
  );
}
