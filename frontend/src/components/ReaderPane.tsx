import { useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
// @ts-ignore - worker URL import
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { api, tokenParam } from "../api";
import { EvidenceView, Locator, humanLocator } from "../types";

pdfjsLib.GlobalWorkerOptions.workerSrc = PdfWorker;
const pdfAssets = import.meta.env.DEV ? "/node_modules/pdfjs-dist/" : "/assets/pdfjs/";

export default function ReaderPane(props: {
  evidence: EvidenceView;
  full?: boolean;
  onClose?: () => void;
  evidenceList?: EvidenceView[];
  index?: number;
  onNavigate?: (i: number) => void;
}) {
  const { evidence } = props;
  const list = props.evidenceList || [evidence];
  const idx = props.index ?? 0;
  return (
    <div>
      <div className="reader-top">
        <span className="file">{evidence.file_name}</span>
        <span className="muted">{humanLocator(evidence.locator, evidence.heading_path)}</span>
        {list.length > 1 && (
          <span className="reader-nav">
            <button className="ghost" aria-label="上一个依据"
                    onClick={() => props.onNavigate?.((idx - 1 + list.length) % list.length)}>‹</button>
            依据 {idx + 1}/{list.length}
            <button className="ghost" aria-label="下一个依据"
                    onClick={() => props.onNavigate?.((idx + 1) % list.length)}>›</button>
          </span>
        )}
        <span style={{ marginLeft: "auto" }} />
        <a className="secondary" style={{ fontSize: 14 }}
           href={`/api/versions/${evidence.version_id}/original?token=${tokenParam()}`}
           target="_blank" rel="noreferrer">打开原件</a>
        {props.onClose && <button className="ghost" onClick={props.onClose}>关闭</button>}
      </div>
      {evidence.format === "pdf" ? (
        <PdfViewer versionId={evidence.version_id}
                   revisionId={evidence.revision_id}
                   locator={evidence.locator} />
      ) : (
        <DocxViewer key={evidence.revision_id || evidence.version_id}
                    versionId={evidence.version_id} revisionId={evidence.revision_id}
                    locator={evidence.locator} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- PDF

/** 未旋转归一化坐标 → 显示坐标（含页面旋转变换，手册 §5.1）。 */
function transformBox(box: { x0: number; y0: number; x1: number; y1: number },
                      rotation: number) {
  const pts = [
    [box.x0, box.y0], [box.x1, box.y0], [box.x1, box.y1], [box.x0, box.y1],
  ].map(([x, y]) => {
    switch (((rotation % 360) + 360) % 360) {
      case 90: return [1 - y, x];
      case 180: return [1 - x, 1 - y];
      case 270: return [y, 1 - x];
      default: return [x, y];
    }
  });
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  return {
    x: Math.min(...xs), y: Math.min(...ys),
    w: Math.max(...xs) - Math.min(...xs),
    h: Math.max(...ys) - Math.min(...ys),
  };
}

interface PageGeom {
  page_index: number;
  rotation: number;
  printed_page: string | null;
}

function PdfViewer({ versionId, revisionId, locator }:
  { versionId: string; revisionId?: string; locator: Locator }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const lastWidthRef = useRef(0);
  const renderingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const targetPage = (locator.page_index ?? 0) + 1;
  const [pageNum, setPageNum] = useState(targetPage);
  const [numPages, setNumPages] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [renderToken, setRenderToken] = useState(0);
  const [geoms, setGeoms] = useState<PageGeom[]>([]);
  const [documentSource, setDocumentSource] = useState<{
    versionId: string;
    pdf: pdfjsLib.PDFDocumentProxy;
  } | null>(null);

  useEffect(() => { setPageNum(targetPage); }, [targetPage, versionId]);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    setNumPages(0);
    const task = pdfjsLib.getDocument({
      url: `/api/versions/${versionId}/original?token=${tokenParam()}`,
      cMapUrl: `${pdfAssets}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${pdfAssets}standard_fonts/`,
    });
    task.promise.then((pdf) => {
      if (!cancelled) {
        setDocumentSource({ versionId, pdf });
        setNumPages(pdf.numPages);
      }
    }).catch((e) => {
      if (!cancelled) setError(`PDF 加载失败：${e?.message || e}`);
    });
    return () => {
      cancelled = true;
      void task.destroy().catch(() => {});
    };
  }, [versionId]);

  // 页面几何：印刷页码与旋转角来自落库的修订，而非当前解析器
  useEffect(() => {
    let alive = true;
    const q = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
    api(`/versions/${versionId}/page-geom${q}`)
      .then((r: any) => { if (alive) setGeoms(r.pages || []); })
      .catch(() => { if (alive) setGeoms([]); });
    return () => { alive = false; };
  }, [versionId, revisionId]);

  // 缩放或容器宽度变化后重算高亮框（§5.1：窗口缩放后框仍罩住同一原句）
  useEffect(() => {
    const onResize = () => {
      if (renderingRef.current) return;
      const w = wrapRef.current?.clientWidth || 0;
      if (Math.abs(w - lastWidthRef.current) > 8) {
        lastWidthRef.current = w;
        setRenderToken((t) => t + 1);
      }
    };
    window.addEventListener("resize", onResize);
    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(onResize) : null;
    if (ro && wrapRef.current) ro.observe(wrapRef.current);
    return () => {
      window.removeEventListener("resize", onResize);
      ro?.disconnect();
    };
  }, []);

  useEffect(() => {
    if (documentSource?.versionId !== versionId) return;
    const doc = documentSource.pdf;
    let cancelled = false;
    let render: ReturnType<pdfjsLib.PDFPageProxy["render"]> | undefined;
    renderingRef.current = true;

    (async () => {
      try {
        const host = wrapRef.current;
        if (!host) return;
        host.innerHTML = "";
        lastWidthRef.current = host.clientWidth || 0;
        const containerW = (host.clientWidth || 800) - 32;

        // 只渲染可见页和邻页（手册 §6）；命中的高亮只落在目标页
        const wanted = [pageNum - 1, pageNum, pageNum + 1]
          .filter((n) => n >= 1 && n <= doc.numPages);
        for (const no of wanted) {
          if (cancelled) return;
          const page = await doc.getPage(no);
          if (cancelled) return;
          const base = page.getViewport({ scale: 1 });
          const scale = Math.min(1.8, Math.max(0.5,
            containerW / base.width)) * zoom;
          const viewport = page.getViewport({ scale });
          const div = document.createElement("div");
          div.className = "pdf-page-wrap";
          div.dataset.page = String(no);
          div.style.width = `${viewport.width}px`;
          div.style.height = `${viewport.height}px`;
          const canvas = document.createElement("canvas");
          canvas.width = Math.floor(viewport.width);
          canvas.height = Math.floor(viewport.height);
          div.appendChild(canvas);
          host.appendChild(div);
          render = page.render({ canvasContext: canvas.getContext("2d")!, viewport });
          await render.promise;
          if (cancelled) return;

          if (no === targetPage && locator.boxes?.length) {
            const geom = geoms.find((g) => g.page_index === no - 1);
            const rotation = geom?.rotation ?? page.rotate;
            const overlay = document.createElement("div");
            overlay.className = "pdf-overlay";
            overlay.style.width = `${viewport.width}px`;
            overlay.style.height = `${viewport.height}px`;
            for (const b of locator.boxes) {
              const t = transformBox(b, rotation);
              const el = document.createElement("div");
              el.className = "box";
              el.style.left = `${t.x * viewport.width}px`;
              el.style.top = `${t.y * viewport.height}px`;
              el.style.width = `${Math.max(t.w * viewport.width, 6)}px`;
              el.style.height = `${Math.max(t.h * viewport.height, 6)}px`;
              overlay.appendChild(el);
            }
            div.appendChild(overlay);
          }
        }
        const target = host.querySelector(`[data-page='${pageNum}']`);
        target?.scrollIntoView({ block: "nearest" });
      } catch (e: any) {
        if (!cancelled) setError(`PDF 加载失败：${e?.message || e}`);
      } finally {
        if (!cancelled) renderingRef.current = false;
      }
    })();

    return () => {
      cancelled = true;
      renderingRef.current = false;
      render?.cancel();
    };
  }, [documentSource, versionId, targetPage, locator, pageNum, zoom, renderToken, geoms]);

  const printed = geoms.find((g) => g.page_index === pageNum - 1)?.printed_page
    || (pageNum === targetPage ? locator.printed_page : null);

  if (error) return <div className="error-box">{error}</div>;
  return (
    <div>
      <div className="reader-toolbar">
        <span>物理第 {pageNum}／{numPages || "…"} 页</span>
        {printed && <span className="muted">印刷页码 {printed}</span>}
        <button className="ghost" disabled={pageNum <= 1}
                onClick={() => setPageNum((p) => Math.max(1, p - 1))}>上一页</button>
        <button className="ghost" disabled={!!numPages && pageNum >= numPages}
                onClick={() => setPageNum((p) => p + 1)}>下一页</button>
        <span className="muted">缩放</span>
        {[0.75, 1, 1.25, 1.5].map((z) => (
          <button key={z} className={zoom === z ? "" : "ghost"}
                  onClick={() => setZoom(z)}>{Math.round(z * 100)}%</button>
        ))}
      </div>
      <div ref={wrapRef} style={{ padding: 12 }} />
    </div>
  );
}

// ---------------------------------------------------------------- DOCX

interface TreeBlock {
  type: "p" | "tbl";
  path: number[];
  text?: string;
  segs?: { t: string; start: number; bold: boolean }[];
  heading_level?: number;
  style?: string;
  rows?: any[][];
}

/** Unicode 码点偏移 → UTF-16 偏移（手册 §5.1：含生僻字时不会错位）。 */
function cpToUtf16(s: string, cp: number): number {
  let i = 0, n = 0;
  while (i < s.length && n < cp) {
    const code = s.codePointAt(i)!;
    i += code > 0xffff ? 2 : 1;
    n += 1;
  }
  return i;
}

function DocxViewer({ versionId, revisionId, locator }:
  { versionId: string; revisionId?: string; locator: Locator }) {
  const [tree, setTree] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
    api(`/versions/${versionId}/tree${query}`)
      .then((value) => { if (active) setTree(value); })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [versionId, revisionId]);

  useEffect(() => {
    if (!tree || !rootRef.current) return;
    const el = rootRef.current.querySelector("[data-hit='1']");
    if (el) el.scrollIntoView({ block: "center" });
  }, [tree, locator]);

  if (error) return <div className="error-box">{error}</div>;
  if (!tree) return <div className="muted" style={{ padding: 16 }}>正在加载结构树…</div>;

  const blockPath = locator.block_path || [];
  const isCell = locator.table_row != null;
  const paraMatches = (path: number[]) => !isCell && pathEq(path, blockPath);

  const renderPara = (p: any, key: string) => {
    const hit = paraMatches(p.path);
    let content: React.ReactNode = p.text;
    if (hit && locator.text_start != null && locator.text_end != null) {
      const s = cpToUtf16(p.text, locator.text_start);
      const e = cpToUtf16(p.text, locator.text_end);
      content = (
        <>
          {p.text.slice(0, s)}
          <mark className="tq-hl">{p.text.slice(s, e)}</mark>
          {p.text.slice(e)}
        </>
      );
    }
    const cls = p.heading_level ? `p h${Math.min(p.heading_level, 3)}` : "p";
    return <div key={key} className={cls} data-hit={hit ? "1" : undefined}>{content}</div>;
  };

  return (
    <div className="docx-tree" ref={rootRef}>
      {tree.tree.blocks.map((blk: TreeBlock, i: number) => {
        if (blk.type === "p") return renderPara(blk, `b${i}`);
        return (
          <table key={`b${i}`}>
            <tbody>
              {blk.rows!.map((row: any[], ri: number) => (
                <tr key={ri}>
                  {row.map((cell: any) => {
                    if (cell.covered_by != null) return null;
                    const cellHit = isCell &&
                      pathEq(blk.path, blockPath) &&
                      cell.row === locator.table_row &&
                      cell.col === locator.table_col;
                    let text: React.ReactNode = cell.text;
                    if (cellHit && locator.text_start != null &&
                        locator.text_end != null) {
                      const s = cpToUtf16(cell.text, locator.text_start);
                      const e = cpToUtf16(cell.text, locator.text_end);
                      text = <>{cell.text.slice(0, s)}
                        <mark className="tq-hl">{cell.text.slice(s, e)}</mark>
                        {cell.text.slice(e)}</>;
                    }
                    return (
                      <td key={`${cell.row}-${cell.col}`}
                          className={cellHit ? "hl-cell" : ""}
                          data-hit={cellHit ? "1" : undefined}
                          colSpan={cell.col_span || 1} rowSpan={cell.row_span || 1}>
                        {text}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        );
      })}
      <div className="muted mt16">
        标题路径：{(locator as any).heading_path?.join(" › ") || "见上方结构树"} ·
        命中处上下保留相邻段落或完整表头
      </div>
    </div>
  );
}

function pathEq(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}
