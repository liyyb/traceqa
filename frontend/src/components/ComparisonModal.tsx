import { useEffect, useState } from "react";
import { CLAIM_LABEL, type ClaimStatus } from "../types";

export default function ComparisonModal({ data, onClose }: { data: any; onClose: () => void }) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const items = (data.items || []).filter((it: any) =>
    showUnchanged || it.changed || it.basis_changed ||
    it.evidence_diff?.added || it.evidence_diff?.removed);
  return (
    <div className="preview-overlay" role="dialog" aria-label="重查对照">
      <div className="preview-sheet">
        <div className="preview-head">
          <h2>重查对照</h2>
          <div className="flex">
            <button className="ghost" onClick={() => setShowUnchanged(!showUnchanged)}>
              {showUnchanged ? "折叠未变项" : "展开未变项"}
            </button>
            <button className="ghost" onClick={onClose}>关闭（Esc）</button>
          </div>
        </div>
        <div style={{ padding: 16 }}>
          {!data.conditions_same && (
            <div className="error-box">
              条件发生变化：旧 {JSON.stringify(data.parent_conditions)}；
              新 {JSON.stringify(data.child_conditions)}。
              防止读者把结果变化全部归因于补件。
            </div>
          )}
          {data.model_diff && (
            <div className="muted">
              注意：规则/模型版本存在差异（{data.model_version.rules_old} →
              {data.model_version.rules_new}）。
            </div>
          )}
          {data.rerun_meta?.added_revisions?.length > 0 && (
            <div className="muted">范围变化：新增 {data.rerun_meta.added_revisions.length} 份补件修订。</div>
          )}
          <table className="compare-table">
            <thead><tr><th>对照栏</th><th>原核查</th><th>本次</th><th>依据变化</th></tr></thead>
            <tbody>
              {items.map((it: any, i: number) => (
                <tr key={i}>
                  <td>{it.question_text || it.topic}{it.basis_changed && <span className="tag warn">依据已变</span>}</td>
                  <td>{it.old?.label || <>
                    <span className={`tag ${it.old?.status === "answered" ? "ok" : "warn"}`}>
                      {CLAIM_LABEL[it.old?.status as ClaimStatus] || it.old?.status}
                    </span><br />{it.old?.conclusion_text}
                  </>}</td>
                  <td>{it.new?.label || <>
                    <span className={`tag ${it.new?.status === "answered" ? "ok" : "warn"}`}>
                      {CLAIM_LABEL[it.new?.status as ClaimStatus] || it.new?.status}
                    </span><br />{it.new?.conclusion_text}
                  </>}</td>
                  <td>{it.evidence_diff
                    ? <>沿用 {it.evidence_diff.kept} · 增加 {it.evidence_diff.added} ·
                       移除 {it.evidence_diff.removed}</> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted">旧人工说明留在原记录；需要沿用时由经办人在新结果中确认并重新署记。</div>
        </div>
      </div>
    </div>
  );
}
