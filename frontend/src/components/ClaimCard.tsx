import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { CLAIM_LABEL, ClaimView, EvidenceView, GapInfo, humanLocator } from "../types";

export default function ClaimCard(props: {
  claim: ClaimView;
  runId: string;
  citeNumbers: Record<string, number>;
  onOpenEvidence: (ev: EvidenceView, anchor: string,
                   list: EvidenceView[], index: number) => void;
  onFollowup: (claim: ClaimView, value: string) => void;
  onMaterials: () => void;
}) {
  const { claim, citeNumbers } = props;
  const cite = (ev: EvidenceView): number => citeNumbers[ev.id] || 0;
  const isConflict = claim.status === "conflict";
  const groups = groupBy(claim.evidences, (e) => e.group_no);
  const list = dedupeById(claim.evidences);

  const open = (ev: EvidenceView, anchor: string) => {
    const idx = list.findIndex((x) => x.id === ev.id);
    props.onOpenEvidence(ev, anchor, list, Math.max(0, idx));
  };

  const gotoSupplement = () => {
    // 缺口旁「补传清晰附件」：表单预填原文件、原附件名和缺失位置（§2.3）
    const gap: GapInfo = claim.gap || {
      location: "", clause: "", attachment: "",
    };
    const file = gap.file_name ||
      (claim.evidences[0]?.file_name || "");
    sessionStorage.setItem("tq_gap", JSON.stringify({
      ...gap,
      original_revision_id: gap.revision_id ||
        claim.evidences[0]?.revision_id || "",
      original_file_name: file,
      claim_id: claim.id,
      run_id: props.runId,
    }));
    props.onMaterials();
  };

  return (
    <div className={`claim-card ${isConflict ? "conflict" : ""}`}
         id={`claim-${claim.id}`} tabIndex={-1}>
      <h2>
        {claim.question_text || claim.topic}
        <span className={`badge ${isConflict || claim.status === "coverage_gap" ? "warn" : ""}`}>
          {CLAIM_LABEL[claim.status]}
        </span>
      </h2>

      {/* 结论置顶成「答案块」：与依据、条件、脚注拉开层级，第一眼先看到结论 */}
      {claim.conclusion_text && (
        <div className={`answer-block ${isConflict ? "conflict" : ""}`}>
          <div className="answer-label">核查结论</div>
          <p className="answer-line">{highlightAmount(claim.conclusion_text)}
            {list.map((ev) => (
              <button key={ev.id} className="cite" id={`inline-${claim.id}-${ev.id}`}
                      aria-label={`查看依据 ${cite(ev)}，${ev.file_name}，${humanLocator(ev.locator, ev.heading_path)}`}
                      onClick={(event) => open(ev, event.currentTarget.id)}>[{cite(ev)}]</button>
            ))}
          </p>
          {claim.status === "answered" && claim.conditions && (
            <div className="conditions">
              适用条件：{Object.values(claim.conditions).join(" · ")}
            </div>
          )}
        </div>
      )}

      {/* 依据：冲突时两组并列，同权重排版（手册 §2.4 冲突样例） */}
      {groups.map((g, gi) => (
        <div key={gi}>
          {isConflict && groups.length > 1 && (
            <div className="muted mt8">依据 {gi + 1}</div>
          )}
          {g.map((ev) => (
            <div key={ev.id + gi} className={`quote-line ${isConflict ? "conflict-side" : ""}`}>
              {ev.quote || ev.text_orig}
              <button className="cite" id={`quote-${claim.id}-${gi}-${ev.id}`}
                      aria-label={`查看依据 ${cite(ev)}，${ev.file_name}，${humanLocator(ev.locator, ev.heading_path)}`}
                      onClick={(event) => open(ev, event.currentTarget.id)}>
                [{cite(ev)}]
              </button>
            </div>
          ))}
        </div>
      ))}

      {/* 脚注行：与句末编号同一入口（手册 §3 事项内脚注） */}
      {list.length > 0 && (
        <div className="footnotes">
          {list.map((ev) => (
            <div className="fn" key={ev.id} id={`fn-${claim.id}-${ev.id}`}>
              <span className="no">[{cite(ev)}]</span>
              <button className="loc-link" id={`link-${claim.id}-${ev.id}`}
                      aria-label={`查看依据 ${cite(ev)}，${ev.file_name}，${humanLocator(ev.locator, ev.heading_path)}`}
                      onClick={(event) => open(ev, event.currentTarget.id)}>
                {ev.file_name} · {humanLocator(ev.locator, ev.heading_path)}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 追问（需补条件） */}
      {claim.status === "needs_condition" && claim.followup && (
        <FollowupForm prompt={claim.followup.prompt}
                      missing={claim.followup.missing === "travel_year" && !claim.followup.month_day
                        ? "travel_date" : claim.followup.missing}
                      monthDay={claim.followup.month_day}
                      onSubmit={(v) => props.onFollowup(claim, v)} />
      )}

      {/* 材料覆盖不足 */}
      {claim.status === "coverage_gap" && claim.gap && (
        <div className="note-block">
          缺失位置：{claim.gap.location}<br />
          已读条款：{claim.gap.clause}<br />
          缺失附件：{claim.gap.attachment}
          <div className="mt8">
            <button className="secondary" onClick={gotoSupplement}>
              补传清晰附件
            </button>
            <span className="muted"> 预填原文件与缺失位置，关闭补件窗口不影响本次核查</span>
          </div>
        </div>
      )}

      {/* 人工说明 */}
      <NoteSection claim={claim} />
    </div>
  );
}

function FollowupForm(props: { prompt: string; missing: string; monthDay?: string;
                               onSubmit: (v: string) => void }) {
  const [val, setVal] = useState("");
  const hasDateInput = props.missing === "travel_year" || props.missing === "travel_date";
  // 已知月日时统一展示「月日 + 年份输入」，不再让同类追问出现两种控件。
  const md = props.monthDay?.split("-").map(Number);
  const mdLabel = md && md.length === 2 ? `${md[0]}月${md[1]}日 ·` : "";
  return (
    <div className="note-form">
      <div className="error-box">{props.prompt}</div>
      {hasDateInput && <div className="row">
        {props.missing === "travel_year" ? (
          <>
            {mdLabel && <span className="muted">{mdLabel}</span>}
            <input type="number" aria-label="出差年份" placeholder="例如 2026" value={val}
                   onChange={(e) => setVal(e.target.value)} min={1990} max={2100} />
          </>
        ) : (
          <input type="date" aria-label="出差日期" value={val} onChange={(e) => setVal(e.target.value)} />
        )}
        <button onClick={() => props.onSubmit(val)}
                disabled={!val || (props.missing === "travel_year" && !/^\d{4}$/.test(val))}>
          补充并重新核查
        </button>
      </div>}
    </div>
  );
}

function NoteSection({ claim }: { claim: ClaimView }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [source, setSource] = useState("电话沟通");
  const [contactTime, setContactTime] = useState("");
  const [signature, setSignature] = useState("");
  const [saved, setSaved] = useState<any[]>(claim.notes || []);
  const [err, setErr] = useState<string | null>(null);
  const [draftVer, setDraftVer] = useState(claim.notes?.[0]?.draft_version ?? 0);

  useEffect(() => {
    setSaved(claim.notes || []);
    setDraftVer(claim.notes?.[0]?.draft_version ?? 0);
  }, [claim.notes]);

  const save = async () => {
    setErr(null);
    try {
      const res = await api(`/claims/${claim.id}/note-draft`, {
        method: "PUT",
        body: JSON.stringify({
          body, source,
          contact_time: contactTime || null,
          signature: signature || null,
          expected_draft_version: draftVer,
        }),
      });
      setDraftVer(res.draft_version);
      setSaved([{ body, source, contact_time: contactTime, signature,
                  recorded_at: res.recorded_at, draft_version: res.draft_version }]);
      setBody("");
      setOpen(false);
    } catch (e: any) {
      if (e.status === 409) {
        setErr("另一窗口已保存该说明，本地输入已保留；请刷新后重试。");
      } else setErr(e.message);
    }
  };

  return (
    <div className="mt16">
      {saved.map((n, i) => (
        <div className="note-block" key={i}>
          <b>人工说明（经办人记录，非制度条款）</b><br />
          {n.body}
          <div className="src">
            来源：{n.source}
            {n.contact_time ? `；沟通时间：${n.contact_time}` : ""}
            {n.signature ? `；记录人：${n.signature}` : ""}
            ；记录时间：{n.recorded_at}
          </div>
        </div>
      ))}
      {!open ? (
        <button className="secondary mt8" onClick={() => setOpen(true)}>
          添加人工说明
        </button>
      ) : (
        <div className="note-form">
          <div className="row">
            <label>信息来源（必填）
              <select value={source} onChange={(e) => setSource(e.target.value)}>
                <option>电话沟通</option>
                <option>当面沟通</option>
                <option>邮件答复</option>
                <option>其他</option>
              </select>
            </label>
            <label>沟通时间
              <input type="date" value={contactTime}
                     onChange={(e) => setContactTime(e.target.value)} />
            </label>
            <label>经办人署名
              <input value={signature} onChange={(e) => setSignature(e.target.value)}
                     placeholder="自报身份" />
            </label>
          </div>
          <textarea rows={3} placeholder="说明正文（必填）" value={body}
                    onChange={(e) => setBody(e.target.value)} />
          <div className="muted small">保存前预览：读者将看到“经办人记录：{body || "（正文）"}”，
            并列来源、记录人和时间。</div>
          {err && <div className="error-box">{err}</div>}
          <div className="row">
            <button onClick={save} disabled={!body.trim() || !source}>保存说明</button>
            <button className="ghost" onClick={() => {
              if (body && !window.confirm("有未保存内容：保留草稿并关闭？")) return;
              setOpen(false);
            }}>收起</button>
          </div>
        </div>
      )}
    </div>
  );
}

// 结论里的金额用强样式标出（如“标准为 800 元”），让答案一眼可见。
// 只匹配“数字 + 可选空格 + 元”的片段，避免误伤日期、编号。
const AMOUNT_RE = /\d[\d,]*(?:\.\d+)?\s*元(?:[\/／][^\s，。；）)]+)?/g;
function highlightAmount(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(AMOUNT_RE)) {
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    out.push(<strong className="amount" key={`${i}-${m[0]}`}>{m[0]}</strong>);
    last = i + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function groupBy<T>(arr: T[], key: (t: T) => number): T[][] {
  const m = new Map<number, T[]>();
  for (const a of arr) {
    const k = key(a);
    if (!m.has(k)) m.set(k, []);
    m.get(k)!.push(a);
  }
  return [...m.keys()].sort((a, b) => a - b).map((k) => m.get(k)!);
}

function dedupeById(arr: EvidenceView[]): EvidenceView[] {
  const seen = new Set<string>();
  return arr.filter((e) => (seen.has(e.id) ? false : (seen.add(e.id), true)));
}
