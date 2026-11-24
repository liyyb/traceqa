import { useCallback, useEffect, useState } from "react";
import Workbench from "./pages/Workbench";
import Materials from "./pages/Materials";
import History from "./pages/History";
import Settings from "./pages/Settings";
import { api } from "./api";

const sections = [["workbench", "核查"], ["materials", "材料"], ["history", "记录"], ["settings", "设置"]] as const;
type Section = typeof sections[number][0];
export type WorkbenchRequest = { runId: string } | { question: string };

export default function App() {
  const [health, setHealth] = useState<"loading" | "ready" | "error">("loading");
  const [catalogue, setCatalogue] = useState(0);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [gapRequest, setGapRequest] = useState(0);
  const [workbenchRequest, setWorkbenchRequest] = useState<WorkbenchRequest>();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [materialsReady, setMaterialsReady] = useState(false);
  const [historyReady, setHistoryReady] = useState(false);
  const refreshCatalogue = useCallback(() => setCatalogue((value) => value + 1), []);
  const refreshHistory = useCallback(() => setHistoryRevision((value) => value + 1), []);

  function navigate(section: Section) {
    if (section === "settings") setSettingsOpen(true);
    if (section === "materials") setMaterialsReady(true);
    if (section === "history") setHistoryReady(true);
    if (window.location.hash !== `#${section}`) {
      window.history.replaceState(window.history.state, "", `#${section}`);
    }
    requestAnimationFrame(() => {
      const target = document.getElementById(section);
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ block: "start" });
    });
  }

  function openWorkbench(request: WorkbenchRequest) {
    setWorkbenchRequest(request);
    navigate("workbench");
  }

  useEffect(() => {
    api("/bootstrap").then(() => setHealth("ready")).catch(() => setHealth("error"));
    const section = window.location.hash.replace(/^#\/?/, "");
    if (section === "materials" || section === "history" || section === "settings") navigate(section);
  }, []);

  useEffect(() => {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        if (entry.target.id === "materials") setMaterialsReady(true);
        if (entry.target.id === "history") setHistoryReady(true);
        observer.unobserve(entry.target);
      }
    });
    for (const id of ["materials", "history"]) {
      const section = document.getElementById(id);
      if (section) observer.observe(section);
    }
    return () => observer.disconnect();
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="nav-inner">
          <a className="brand" href="#workbench" onClick={(event) => {
            event.preventDefault(); navigate("workbench");
          }}><span className="brand-mark" aria-hidden="true" />TraceQA</a>
          <nav aria-label="工作台目录">
            {sections.map(([id, label]) => (
              <a key={id} href={`#${id}`} onClick={(event) => {
                event.preventDefault(); navigate(id);
              }}>{label}</a>
            ))}
          </nav>
          <span className={`service-state ${health}`} role="status">
            {health === "ready" ? "本地运行" : health === "error" ? "服务未连接" : "正在连接"}
          </span>
        </div>
      </header>
      <main className="page-width">
        <section id="workbench" className="workspace-section" tabIndex={-1} aria-labelledby="workspace-title">
          <div className="workspace-intro">
            <h1 id="workspace-title">文档溯源<span>.</span></h1>
            <p>在选定材料中提问，沿引用核对原文。</p>
          </div>
          <Workbench catalogue={catalogue} request={workbenchRequest} onResult={refreshHistory}
                     onMaterials={(gap = false) => {
                       if (gap) setGapRequest((value) => value + 1);
                       navigate("materials");
                     }} />
        </section>
        <section id="materials" className="index-section" tabIndex={-1} aria-labelledby="materials-title">
          <header className="section-heading"><h2 id="materials-title">材料库</h2><p>导入和管理制度原件。</p></header>
          {materialsReady && <Materials gapRequest={gapRequest} onChanged={refreshCatalogue} onRun={openWorkbench} />}
        </section>
        <section id="history" className="index-section" tabIndex={-1} aria-labelledby="history-title">
          <header className="section-heading"><h2 id="history-title">核查记录</h2><p>回看、重查或下载正式记录。</p></header>
          {historyReady && <History refresh={historyRevision} onRun={openWorkbench} />}
        </section>
        <section id="settings" className="index-section settings-section" tabIndex={-1}>
          <details open={settingsOpen} onToggle={(event) => setSettingsOpen(event.currentTarget.open)}>
            <summary className="section-heading">设置<span>数据目录、自检与备份</span></summary>
            {settingsOpen && <Settings onCatalogueChanged={refreshCatalogue} />}
          </details>
        </section>
      </main>
    </div>
  );
}
