// 与后端读模型对应的类型（手册 §4.1）

export type RunStatus =
  | "queued" | "retrieving" | "checking" | "committing"
  | "completed" | "failed" | "cancelled" | "interrupted";

export type ClaimStatus =
  | "answered" | "needs_condition" | "no_evidence" | "conflict" | "coverage_gap";

export const STAGE_LABEL: Record<RunStatus, string> = {
  queued: "已排队",
  retrieving: "正在检索",
  checking: "正在核对依据",
  committing: "正在提交结果",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断（可重试）",
};

export const CLAIM_LABEL: Record<ClaimStatus, string> = {
  answered: "有据可答",
  needs_condition: "需补条件",
  no_evidence: "未找到依据",
  conflict: "待确认冲突",
  coverage_gap: "材料覆盖不足",
};

export interface Locator {
  kind: "docx" | "pdf";
  part?: string;
  block_path?: number[];
  text_start?: number;
  text_end?: number;
  table_row?: number | null;
  table_col?: number | null;
  row_span?: number;
  col_span?: number;
  page_index?: number;
  boxes?: { x0: number; y0: number; x1: number; y1: number }[];
  printed_page?: string;
}

export interface EvidenceView {
  id: string;
  ce_id?: string;
  relation: "support" | "conflict" | "related";
  group_no: number;
  quote: string | null;
  locator: Locator;
  heading_path: string[];
  page_no: number | null;
  text_orig: string;
  version_id: string;
  revision_id?: string;
  file_name: string;
  doc_title: string;
  format: "docx" | "pdf";
}

export interface GapInfo {
  location: string;
  clause: string;
  attachment: string;
  revision_id?: string;
  file_name?: string;
}

export interface NoteView {
  id?: string;
  body: string;
  source: string;
  contact_time?: string | null;
  signature?: string | null;
  recorded_at: string;
  draft_version?: number;
}

export interface ClaimView {
  id: string;
  seq_no: number;
  topic: string;
  question_text: string;
  status: ClaimStatus;
  conclusion_text: string | null;
  conditions: Record<string, string>;
  followup: { missing: string; prompt: string; month_day?: string } | null;
  gap: GapInfo | null;
  evidences: EvidenceView[];
  notes: NoteView[];
}

export interface ScopeCatalogItem {
  folder_path?: string;
  revision_id: string;
  version_id: string;
  document_id: string;
  file_name: string;
  title: string;
  format: "docx" | "pdf";
  imported_at: string;
  is_latest: boolean;
  coverage: Coverage | null;
}

export interface ScopeUpdate {
  revision_id: string;
  file_name: string;
  newer_revision_id: string;
  newer_file_name: string;
  newer_imported_at: string;
  coverage_change: string;
}

export interface SupplementView {
  id: string;
  original_revision_id: string;
  supplement_revision_id: string;
  original_file: string;
  supplement_file: string;
  gap: GapInfo;
  status: string;
  created_at: string;
  affected_total: number;
}

export interface AffectedRun {
  run_id: string;
  question: string;
  created_at: string;
  status: string;
  tags: string[];
  cited: boolean;
}

export interface ScopeDetailItem {
  revision_id: string;
  version_id: string;
  file_name: string;
  title: string;
  format: "docx" | "pdf";
  coverage: Coverage | null;
  available?: boolean;
}

export interface Coverage {
  total_pages?: number | null;
  covered_pages?: number | null;
  skipped?: { pages: number[]; reason: string }[];
  unsupported?: string[];
  evidence_count?: number;
  cover_status?: "ok" | "absent" | "unreadable";
  document_fields?: Record<string, { value: string; source: string }>;
  is_new_version?: boolean;
}

export interface RunSnapshot {
  run_id: string;
  status: RunStatus;
  question: string;
  conditions: Record<string, string>;
  scope: string[];
  scope_detail: ScopeDetailItem[];
  model_version: string;
  rules_version: string;
  seq: number;
  error: string | null;
  parent_run_id: string | null;
  created_at: string;
  finished_at: string | null;
  claims: ClaimView[];
  stage_label: string;
}

export interface DocVersion {
  version_id: string;
  file_name: string;
  format: "docx" | "pdf";
  sha256_short: string;
  imported_at: string;
  revision_id: string | null;
  revision_status: string | null;
  is_latest?: boolean;
  coverage: Coverage | null;
}

export interface MetadataNote {
  field: string;
  value: string;
  source: string;
  signature?: string | null;
  created_at: string;
}

export interface DocumentView {
  id: string;
  title: string;
  folder_path: string;
  created_at: string;
  document_fields?: Record<string, { field: string; value: string; source: string; note?: string }>;
  metadata_notes?: MetadataNote[];
  versions: DocVersion[];
}

export function humanLocator(loc: Locator, headingPath: string[]): string {
  if (loc.kind === "pdf") {
    const page = (loc.page_index ?? 0) + 1;
    let t = `物理第 ${page} 页`;
    if (loc.printed_page) t += `（印刷页码 ${loc.printed_page}）`;
    if (headingPath.length) t += ` · ${headingPath.join(" › ")}`;
    return t;
  }
  const parts: string[] = [];
  if (headingPath.length) parts.push(headingPath.join(" › "));
  if (loc.table_row != null) {
    parts.push(`表格 行${(loc.table_row ?? 0) + 1}／列${(loc.table_col ?? 0) + 1}`);
  } else {
    parts.push("段落");
  }
  return parts.join(" · ");
}
