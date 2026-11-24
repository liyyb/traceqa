import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { api, requestIdFor, clearRequestId } from "../api";
import type { WorkbenchRequest } from "../App";
import {
  DocumentView, DocVersion, Coverage, MetadataNote, GapInfo,
  SupplementView, AffectedRun,
} from "../types";

type MaterialInput = { file: File; folder: string };
type ImportFile = { id: string; file_name: string; folder_path: string; status: string;
  error: string | null; revision_id: string | null };
type ImportStatus = { status: "running" | "done"; files: ImportFile[] };
type BatchProgress = { total: number; ignored: number; files: ImportFile[]; busy: boolean };
type FolderNode = { name: string; path: string; folders: Map<string, FolderNode>;
  docs: DocumentView[]; count: number };

function selectedFiles(files: FileList | File[]): MaterialInput[] {
  return Array.from(files, (file) => ({
    file, folder: file.webkitRelativePath.split("/").slice(0, -1).join("/"),
  }));
}

async function readEntry(entry: FileSystemEntry, folder: string): Promise<MaterialInput[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) =>
      (entry as FileSystemFileEntry).file(resolve, reject));
    return [{ file, folder }];
  }
  if (!entry.isDirectory) return [];
  const path = folder ? folder + "/" + entry.name : entry.name;
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const files: MaterialInput[] = [];
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
      reader.readEntries(resolve, reject));
    if (!batch.length) return files;
    for (const child of batch) files.push(...await readEntry(child, path));
  }
}

interface ReadyRevision {
  revision_id: string;
  version_id: string;
  file_name: string;
  folder: string;
}

export default function Materials({ gapRequest, onChanged, onRun }: {
  gapRequest: number;
  onChanged: () => void;
  onRun: (request: WorkbenchRequest) => void;
}) {
  const [docs, setDocs] = useState<DocumentView[]>([]);
  const [importState, setImportState] = useState<BatchProgress | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [gap, setGap] = useState<(GapInfo & Record<string, any>) | null>(null);
  const [supplements, setSupplements] = useState<SupplementView[]>([]);
  const [loadError, setLoadError] = useState("");
  const [importError, setImportError] = useState("");
  const [loading, setLoading] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement | null>(null);
  const attachFolderPicker = useCallback((input: HTMLInputElement | null) => {
    folderRef.current = input;
    input?.setAttribute("webkitdirectory", "");
  }, []);
  const importing = useRef(false);
  const dragDepth = useRef(0);
  const [busy, setBusy] = useState(false);
  const supplementFileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [d, s] = await Promise.all([api("/documents"), api("/supplements")]);
      setDocs(d.documents);
      setSupplements(s.supplements || []);
    } catch {
      setLoadError("无法加载材料库，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("tq_gap");
      if (raw) setGap(JSON.parse(raw));
    } catch { /* ignore */ }
  }, [gapRequest]);

  const readyRevisions: ReadyRevision[] = docs.flatMap((d) =>
    d.versions.filter((v) => v.revision_id && v.revision_status === "ready")
      .map((v) => ({ revision_id: v.revision_id!, version_id: v.version_id,
                     file_name: v.file_name, folder: d.folder_path })));

  const importMaterials = async (getEntries: () => MaterialInput[] | Promise<MaterialInput[]>,
                                 supplement = false) => {
    if (importing.current) return;
    importing.current = true;
    setBusy(true);
    setImportState(null);
    setImportError("");
    const finished: ImportFile[] = [];
    let total = 0;
    let ignored = 0;
    let submitted = false;
    try {
      const entries = await getEntries();
      const supported = entries.filter(({ file }) => /\.(docx|pdf)$/i.test(file.name)
        && !file.name.startsWith("~$"));
      ignored = entries.length - supported.length;
      total = supported.length;
      if (!total) {
        setImportError("未找到可导入的 Word（.docx）或 PDF 文件。");
        return;
      }
      setImportState({ total, ignored, files: [], busy: true });
      for (let i = 0; i < supported.length; i += 160) {
        const batch = supported.slice(i, i + 160);
        const form = new FormData();
        for (const { file } of batch) form.append("files", file, file.name);
        if (batch.some(({ folder }) => folder)) {
          form.append("folder_paths", JSON.stringify(batch.map(({ folder }) => folder)));
        }
        const { import_id } = await api<{import_id: string}>("/imports", { method: "POST", body: form });
        submitted = true;
        let failures = 0;
        for (;;) {
          try {
            const status = await api<ImportStatus>("/imports/" + import_id);
            failures = 0;
            setImportState({ total, ignored, files: [...finished, ...status.files], busy: true });
            if (status.status === "done") {
              finished.push(...status.files);
              break;
            }
          } catch (err) {
            if (++failures === 3) throw err;
          }
          await new Promise((resolve) => setTimeout(resolve, 650));
        }
      }
      if (supplement && finished[0]?.revision_id) {
        setPendingSupplementRev(finished[0].revision_id);
        setSupplementFileName(finished[0].file_name);
        setConfirmed(false);
      }
    } catch {
      setImportError(submitted
        ? "导入进度连接中断。可重新导入，已完成的文件会自动复用。"
        : "未能读取或上传文件，请检查访问权限后重试。");
    } finally {
      if (submitted) {
        await load();
        onChanged();
      }
      setImportState((prev) => prev && { total, ignored, files: prev.files, busy: false });
      importing.current = false;
      setBusy(false);
    }
  };

  const dropMaterials = (transfer: DataTransfer) => {
    const files = selectedFiles(transfer.files);
    const items = Array.from(transfer.items).filter((item) => item.kind === "file");
    const entries = items.map((item) => item.webkitGetAsEntry?.() ?? item.getAsFile());
    void importMaterials(async () => {
      if (!entries.length || entries.some((entry) => entry === null)) return files;
      const results: MaterialInput[] = [];
      for (const entry of entries) {
        if (entry instanceof File) results.push({ file: entry, folder: "" });
        else if (entry) results.push(...await readEntry(entry, ""));
      }
      return results;
    });
  };

  const [pendingSupplementRev, setPendingSupplementRev] = useState("");
  const [supplementFileName, setSupplementFileName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [linkResult, setLinkResult] = useState<any | null>(null);
  const [linkErr, setLinkErr] = useState<string | null>(null);

  const linkSupplement = async () => {
    if (!pendingSupplementRev) { setLinkErr("请先选择或导入补件"); return; }
    if (!gap?.original_revision_id) {
      setLinkErr("无法确定原修订：请从核查结果的「补传清晰附件」进入"); return;
    }
    if (!confirmed) { setLinkErr("请先确认补件对应的附件"); return; }
    setLinkErr(null);
    try {
      const res = await api("/supplements", {
        method: "POST",
        body: JSON.stringify({
          original_revision_id: gap.original_revision_id,
          gap: { location: gap.location, clause: gap.clause,
                 attachment: gap.attachment },
          supplement_revision_id: pendingSupplementRev,
        }),
      });
      setLinkResult(res);
      await load();
    } catch (e: any) { setLinkErr(e.message); }
  };

  const closeGap = () => {
    sessionStorage.removeItem("tq_gap");
    setGap(null);
    setLinkResult(null);
    setPendingSupplementRev("");
    setSupplementFileName("");
    setConfirmed(false);
  };

  return (
    <div className="materials-body">
      {loading && <p className="muted" role="status">正在读取材料库…</p>}
      {loadError && <p className="error-box" role="alert">{loadError} <button onClick={() => void load()}>重试</button></p>}
      {importError && <p className="error-box" role="alert">{importError}</p>}

      {gap && (
        <div className="list-card supplement-card" role="dialog"
             aria-label="补传清晰附件">
          <h2 style={{ fontSize: 18 }}>补传清晰附件</h2>
          <table className="compare-table">
            <tbody>
              <tr><th>原文件</th><td>{gap.original_file_name || "—"}</td></tr>
              <tr><th>原附件名</th><td>{gap.attachment || "—"}</td></tr>
              <tr><th>缺失位置</th><td>{gap.location || "—"}</td></tr>
              <tr><th>已读条款</th><td>{gap.clause || "—"}</td></tr>
            </tbody>
          </table>
          <div className="mt8 flex">
            <label>选择已导入的补件修订
              <select value={pendingSupplementRev}
                      onChange={(e) => {
                        setPendingSupplementRev(e.target.value);
                        const r = readyRevisions.find(
                          (x) => x.revision_id === e.target.value);
                        setSupplementFileName(r?.file_name || "");
                      }}>
                <option value="">— 请选择 —</option>
                {readyRevisions.map((r) => (
                  <option key={r.revision_id} value={r.revision_id}>
                    {r.folder ? r.folder + " / " : ""}{r.file_name}
                  </option>
                ))}
              </select>
            </label>
            <button className="secondary"
                    disabled={busy} onClick={() => supplementFileRef.current?.click()}>
              上传新的清晰补件
            </button>
            <input ref={supplementFileRef} type="file" hidden
                   accept=".docx,.pdf"
                   onChange={(e) => {
                     const files = selectedFiles(e.currentTarget.files || []);
                     e.currentTarget.value = "";
                     if (files.length) void importMaterials(() => files, true);
                   }} />
          </div>
          {supplementFileName && (
            <div className="muted mt8">补件预览：{supplementFileName}</div>
          )}
          <label className="flex mt8">
            <input type="checkbox" checked={confirmed}
                   onChange={(e) => setConfirmed(e.target.checked)} />
            我已检查补件预览，确认它对应上表所列的附件（同名文件仅作候选，不自动建立替代关系）
          </label>
          {linkErr && <div className="error-box">{linkErr}</div>}
          {linkResult && (
            <div className="note-block">
              关联成功。补件可用证据 {linkResult.supplement_evidence_count} 条；
              仍未覆盖位置：{linkResult.supplement_coverage?.skipped?.length
                ? linkResult.supplement_coverage.skipped.map(
                    (s: any) => `第 ${s.pages.join("–")} 页（${s.reason}）`).join("、")
                : "无"}。原核查仍可打开。
            </div>
          )}
          <div className="flex mt8">
            <button onClick={linkSupplement}>确认关联</button>
            <button className="ghost" onClick={closeGap}>关闭（保留原核查）</button>
          </div>
        </div>
      )}

      <div
        className={dragOver ? "material-import is-over" : "material-import"}
        onDragEnter={(event) => {
          if (!event.dataTransfer.types.includes("Files")) return;
          event.preventDefault();
          if (++dragDepth.current === 1) setDragOver(true);
        }}
        onDragOver={(event) => {
          if (!event.dataTransfer.types.includes("Files")) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? "none" : "copy";
        }}
        onDragLeave={() => { if (dragDepth.current && --dragDepth.current === 0) setDragOver(false); }}
        onDrop={(event) => {
          event.preventDefault();
          dragDepth.current = 0;
          setDragOver(false);
          if (!busy) dropMaterials(event.dataTransfer);
        }}
      >
        <div className="material-import-copy">
          <h3>导入材料</h3>
          <p>拖入 Word、PDF 或整个文件夹，子文件夹会一起保留。</p>
        </div>
        <div className="material-import-actions">
          <button disabled={busy} onClick={() => folderRef.current?.click()}>选择文件夹</button>
          <button disabled={busy} className="secondary" onClick={() => fileRef.current?.click()}>选择文件</button>
        </div>
        <input ref={attachFolderPicker} type="file" multiple hidden data-testid="material-folder-input"
               onChange={(e) => {
                 const files = selectedFiles(e.currentTarget.files || []);
                 e.currentTarget.value = "";
                 if (files.length) void importMaterials(() => files);
               }} />
        <input ref={fileRef} type="file" multiple hidden accept=".docx,.pdf"
               onChange={(e) => {
                 const files = selectedFiles(e.currentTarget.files || []);
                 e.currentTarget.value = "";
                 if (files.length) void importMaterials(() => files);
               }} />
      </div>

      {busy && !importState && <p className="material-status" role="status">正在读取所选材料…</p>}
      {importState && <ImportSummary progress={importState} />}

      {(docs.length > 0 || (!loading && !loadError)) && <MaterialLibrary docs={docs} onChanged={load} />}
      {supplements.length > 0 && (
        <details className="material-supplements">
          <summary>补件关联 <span>{supplements.length} 项</span></summary>
          {supplements.map((s) => <SupplementCard key={s.id} sup={s} onRun={onRun} />)}
        </details>
      )}
    </div>
  );
}

function ImportSummary({ progress }: { progress: BatchProgress }) {
  const done = progress.files.filter((file) => file.status === "done").length;
  const failed = progress.files.filter((file) => file.status === "failed").length;
  return (
    <div className="material-progress">
      <div className="material-progress-head">
        <b>{progress.busy ? "正在导入" : done + failed === progress.total ? "导入完成" : "导入中断"}</b>
        <span role="status">
          {progress.busy && `${done + failed}/${progress.total} · `}
          {done} 份可用{failed > 0 && ` · ${failed} 份失败`}{progress.ignored > 0 && ` · 跳过 ${progress.ignored} 个其他文件`}
        </span>
      </div>
      {progress.busy && <progress aria-label="导入材料进度" max={progress.total} value={done + failed} />}
      {progress.files.length > 0 && <details open={failed > 0 ? true : undefined}>
        <summary>查看导入明细</summary>
        <ul className="material-import-files">
          {progress.files.map((file) => (
            <li key={file.id}>
              <span title={[file.folder_path, file.file_name].filter(Boolean).join("/")}>
                {[file.folder_path, file.file_name].filter(Boolean).join(" / ")}
              </span>
              <span className={file.status === "failed" ? "material-attention" : "muted"}>
                {file.status === "done" ? "可用" : file.status === "failed" ? file.error || "导入失败"
                  : file.status === "parsing" ? "解析中" : file.status === "storing" ? "保存中" : "排队"}
              </span>
            </li>
          ))}
        </ul>
      </details>}
    </div>
  );
}

const names = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

function buildFolders(docs: DocumentView[]): FolderNode {
  const root: FolderNode = { name: "", path: "", folders: new Map(), docs: [], count: 0 };
  for (const doc of docs) {
    let node = root;
    node.count++;
    for (const segment of (doc.folder_path || "").split("/").filter(Boolean)) {
      let child = node.folders.get(segment);
      if (!child) {
        const path = node.path ? node.path + "/" + segment : segment;
        child = { name: segment, path, folders: new Map(), docs: [], count: 0 };
        node.folders.set(segment, child);
      }
      node = child;
      node.count++;
    }
    node.docs.push(doc);
  }
  return root;
}

function MaterialLibrary({ docs, onChanged }: { docs: DocumentView[]; onChanged: () => void }) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState(() => new Map<string, boolean>());
  const key = search.trim().toLocaleLowerCase();
  const filtered = useMemo(() => key ? docs.filter((doc) =>
    [doc.folder_path, doc.title, ...doc.versions.map((version) => version.file_name)]
      .some((part) => part?.toLocaleLowerCase().includes(key))) : docs, [docs, key]);
  const tree = useMemo(() => buildFolders(filtered), [filtered]);
  const saveExpanded = (path: string, open: boolean) => {
    setExpanded((prev) => new Map(prev).set(path, open));
  };
  const disclosure = { expanded, saveExpanded, searchOpen: !!key };
  return (
    <section className="material-library" aria-label="制度原件">
      <div className="material-library-head">
        <h3>制度原件 <span>{key ? `${filtered.length} / ${docs.length}` : docs.length} 份</span></h3>
        {docs.length > 0 && <input aria-label="搜索材料" type="search" value={search}
          placeholder="搜索文件名或文件夹" onChange={(event) => setSearch(event.target.value)} />}
      </div>
      {tree.count === 0 ? (
        <p className="material-empty">{key ? "找不到匹配的文件或文件夹。" : "导入文件夹或文件后，材料会显示在这里。"}</p>
      ) : (
        <ul className="material-tree" key={key ? "search:" + key : "library"}>
          {[...tree.folders.values()].sort((a, b) => names.compare(a.name, b.name)).map((folder) =>
            <FolderRow key={"folder:" + folder.path} folder={folder} {...disclosure} depth={0} onChanged={onChanged} />)}
          {tree.docs.length > 0 && <FolderRow key="loose-files"
            folder={{ name: "单独导入的文件", path: "", docs: tree.docs, count: tree.docs.length, folders: new Map() }}
            {...disclosure} depth={0} onChanged={onChanged} />}
        </ul>
      )}
    </section>
  );
}

function FolderRow({ folder, searchOpen, expanded, saveExpanded, depth, onChanged }: {
  folder: FolderNode; searchOpen: boolean; expanded: Map<string, boolean>;
  saveExpanded: (path: string, open: boolean) => void; depth: number; onChanged: () => void;
}) {
  const [searchExpanded, setSearchExpanded] = useState(true);
  const open = searchOpen ? searchExpanded : expanded.get(folder.path) === true;
  const id = useId();
  return (
    <li className="material-folder">
      <button type="button" className="material-row material-folder-row" aria-expanded={open}
              aria-controls={id} onClick={() => searchOpen ? setSearchExpanded(!open) : saveExpanded(folder.path, !open)}>
        <span className="material-chevron" aria-hidden="true" />
        <svg className="material-folder-icon" width="20" height="20" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" />
        </svg>
        <span className="material-row-name" title={folder.path || undefined}>{folder.name}</span>
        <span className="material-row-meta">{folder.count} 份</span>
      </button>
      <ul id={id} className={`material-branch${depth < 6 ? " indented" : ""}`} hidden={!open}>
        {open && <>
          {[...folder.folders.values()].sort((a, b) => names.compare(a.name, b.name)).map((child) =>
            <FolderRow key={"folder:" + child.path} folder={child} depth={depth + 1}
              searchOpen={searchOpen} expanded={expanded} saveExpanded={saveExpanded} onChanged={onChanged} />)}
          {[...folder.docs].sort((a, b) => names.compare(a.title, b.title)).map((doc) =>
            <DocCard key={doc.id} doc={doc} onChanged={onChanged} />)}
        </>}
      </ul>
    </li>
  );
}

function SupplementCard({ sup, onRun }: {
  sup: SupplementView;
  onRun: (request: WorkbenchRequest) => void;
}) {
  const [runs, setRuns] = useState<AffectedRun[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [rerunFor, setRerunFor] = useState<AffectedRun | null>(null);
  const [rerunScope, setRerunScope] = useState<any | null>(null);

  const loadRuns = async () => {
    try {
      const r = await api(`/supplements/${sup.id}/runs`);
      setRuns(r.runs || []);
    } catch (e: any) { setErr(e.message); }
  };

  const prepareRerun = async (run: AffectedRun) => {
    try {
      const snap = await api(`/runs/${run.run_id}`);
      setRerunScope(snap);
      setRerunFor(run);
    } catch (e: any) { setErr(e.message); }
  };

  const doRerun = async () => {
    if (!rerunFor) return;
    try {
      const run = await api(`/runs/${rerunFor.run_id}/reruns`, {
        method: "POST",
        body: JSON.stringify({
          request_id: requestIdFor("rerun", true),
          supplement_revision_ids: [sup.supplement_revision_id],
        }),
      });
      clearRequestId("rerun");
      setRerunFor(null);
      onRun({ runId: run.run_id });
    } catch (e: any) {
      if (e.body?.missing) setErr(`原范围中的必需原件缺失：${e.body.missing.join("、")}`);
      else setErr(e.message);
    }
  };

  return (
    <div className="list-card">
      <div className="row1">
        <b>{sup.original_file}</b> ← 补件 <b>{sup.supplement_file}</b>
        <span className="tag ok">{sup.status}</span>
        <span className="meta">{sup.created_at}</span>
      </div>
      <div className="meta">缺口：{sup.gap?.attachment} · {sup.gap?.location} ·
        受影响核查 {sup.affected_total} 次</div>
      <div className="flex mt8">
        <button className="ghost" onClick={loadRuns}>查看受影响记录</button>
      </div>
      {err && <div className="error-box">{err}</div>}
      {runs && (
        <div className="mt8">
          {runs.length === 0 && <div className="muted">没有关联的历史核查。</div>}
          {runs.map((r) => (
            <div key={r.run_id} className="list-card">
              <div className="row1"><b>{r.question}</b>
                <span className="meta">{r.created_at}</span></div>
              <div className="flex mt8">
                {r.tags.map((t) => <span key={t} className="tag warn">{t}</span>)}
                <button className="secondary" onClick={() => prepareRerun(r)}>
                  一键重查
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {rerunFor && rerunScope && (
        <div className="preview-overlay" role="dialog" aria-label="确认重查范围">
          <div className="preview-sheet" style={{ maxWidth: 640 }}>
            <div className="preview-head"><h2>确认重查范围</h2></div>
            <div style={{ padding: 16 }}>
              <p>问题：{rerunFor.question}</p>
              <p className="muted">沿用原条件与全部未变文件的旧修订，仅加入选定补件；
                缺必需原件时会返回缺失清单，不会悄悄换成最新版。</p>
              <b>原范围（{rerunScope.scope_detail?.length || 0} 份）</b>
              <ul>{rerunScope.scope_detail?.map((s: any) =>
                <li key={s.revision_id}>{s.file_name}</li>)}</ul>
              <b className="ok-text">新增</b>
              <ul><li>{sup.supplement_file}（补件）</li></ul>
              <div className="flex mt8">
                <button onClick={doRerun}>确认并全部重新核验</button>
                <button className="ghost" onClick={() => setRerunFor(null)}>取消</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DocCard({ doc, onChanged }: { doc: DocumentView; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const latest = [...doc.versions].reverse()
    .find((v) => v.is_latest) || doc.versions[doc.versions.length - 1];
  const cov = latest?.coverage;
  const fields = doc.document_fields || {};
  const notes = doc.metadata_notes || [];

  const noText = (field: string) => {
    if (fields[field]) return `${fields[field].value}（原件载明）`;
    const n = notes.find((x: MetadataNote) => x.field === field);
    if (n) return `原件未载明；经办人补录：${n.value}（来源：${n.source}）`;
    if (field === "制度编号" && cov?.cover_status === "unreadable")
      return "封面无法读取，编号待核";
    return "原件未载明";
  };

  return (
    <li className="material-document">
      <button type="button" className="material-row material-file-row" aria-expanded={open}
              aria-controls={id} onClick={() => setOpen(!open)}>
        <span className="material-chevron" aria-hidden="true" />
        <span className="material-file-type">{latest?.format?.toUpperCase() || "文件"}</span>
        <span className="material-row-name" title={latest?.file_name || doc.title}>{latest?.file_name || doc.title}</span>
        {cov?.skipped?.length ? <span className="material-attention">有缺页</span> :
          latest?.revision_status !== "ready" && <span className="material-attention">待检查</span>}
        {doc.versions.length > 1 && <span className="material-row-meta">{doc.versions.length} 个版本</span>}
      </button>
      <div className="material-file-detail" id={id} hidden={!open}>
        {open && <>
          <div className="meta">制度编号：{noText("制度编号")} · 发布日期：{noText("发布日期")} · 生效日期：{noText("生效日期")}</div>
          {cov && <CoverageView cov={cov} />}
          {doc.versions.map((v) => (
            <VersionRow key={v.version_id} v={v} notes={notes} onChanged={onChanged} />
          ))}
        </>}
      </div>
    </li>
  );
}

function CoverageView({ cov }: { cov: Coverage }) {
  return (
    <div className="muted small">
      覆盖：{cov.evidence_count ?? 0} 条可用证据
      {cov.total_pages ? `；共 ${cov.total_pages} 页，覆盖 ${cov.covered_pages} 页` : ""}
      {cov.skipped?.map((s, i) => (
        <div key={i}>跳过 第 {s.pages.join("、")} 页：{s.reason}</div>
      ))}
      {cov.unsupported?.map((u, i) => <div key={i}>{u}</div>)}
    </div>
  );
}

function VersionRow({ v, notes, onChanged }:
  { v: DocVersion; notes: MetadataNote[]; onChanged: () => void }) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [field, setField] = useState("制度编号");
  const [value, setValue] = useState("");
  const [source, setSource] = useState("");
  const [signature, setSignature] = useState("");

  const saveNote = async () => {
    try {
      await api(`/versions/${v.version_id}/metadata-notes`, {
        method: "POST",
        body: JSON.stringify({ field, value, source, signature }),
      });
      setNoteOpen(false);
      setValue(""); setSource("");
      onChanged();
    } catch (e: any) { alert(e.message); }
  };

  return (
    <div className="material-version">
      <div className="flex">
        <b>{v.file_name}</b>
        {v.is_latest && <span className="tag ok">最新导入</span>}
        <span className="tag">{v.sha256_short}</span>
        <span className="meta">导入于 {v.imported_at}</span>
        <a href={`/api/versions/${v.version_id}/original?token=${sessionStorage.getItem("tq_token")}`}
           target="_blank" rel="noreferrer">打开原件</a>
        <button className="ghost" onClick={() => setNoteOpen(!noteOpen)}>补录元数据</button>
      </div>
      {notes.filter((n) => n.field).map((n, i) => (
        <div className="muted small" key={i}>
          经办人补录 {n.field}：{n.value}（来源：{n.source}
          {n.signature ? `，署名：${n.signature}` : ""}）
        </div>
      ))}
      {noteOpen && (
        <div className="note-form">
          <div className="row">
            <label>字段
              <select value={field} onChange={(e) => setField(e.target.value)}>
                <option>制度编号</option><option>发布日期</option><option>生效日期</option>
              </select>
            </label>
            <label>补录值 <input value={value} onChange={(e) => setValue(e.target.value)} /></label>
            <label>来源（必填）
              <input value={source} onChange={(e) => setSource(e.target.value)}
                     placeholder="如：部门收文登记簿" />
            </label>
            <label>署名 <input value={signature} onChange={(e) => setSignature(e.target.value)} /></label>
          </div>
          <button onClick={saveNote} disabled={!value.trim() || !source.trim()}>保存补录</button>
          <div className="muted small">
            原件提取值与补录分行保存；补录日期不自动成为条款生效或替代旧规的依据。
          </div>
        </div>
      )}
    </div>
  );
}
