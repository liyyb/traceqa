import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

export default function Settings({ onCatalogueChanged }: { onCatalogueChanged: () => void }) {
  const [status, setStatus] = useState<any | null>(null);
  const [checks, setChecks] = useState<any[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [backups, setBackups] = useState<any[]>([]);
  const [interrupted, setInterrupted] = useState<any[]>([]);
  const [dir, setDir] = useState("");

  const load = useCallback(async () => {
    const [s, b, i] = await Promise.all([
      api("/settings/status"),
      api("/settings/backups").catch(() => ({ backups: [] })),
      api("/settings/interrupted").catch(() => ({ runs: [] })),
    ]);
    setStatus(s); setDir(s.data_dir || "");
    setBackups(b.backups || []); setInterrupted(i.runs || []);
  }, []);
  useEffect(() => { load(); }, [load]);

  const selfcheck = async () => {
    try { setChecks((await api("/settings/selfcheck", { method: "POST" })).checks); }
    catch (e: any) { setMsg(`自检失败：${e.message}`); }
  };

  const act = async (path: string, body?: any, label?: string) => {
    setMsg("处理中…");
    try {
      const r = await api(path, body
        ? { method: "POST", body: JSON.stringify(body) } : { method: "POST" });
      setMsg(`${label || "操作"}完成：${r.note || r.backup_dir ||
        (r.retried ? `重试 ${r.retried.length} 项` : "") ||
        (r.rebuilt ? `重建 ${r.rebuilt.length} 个修订` : "") || ""}`);
      await load();
      if (r.rebuilt?.length) onCatalogueChanged();
    } catch (e: any) { setMsg(`${label || "操作"}失败：${e.message}`); }
  };

  return (
    <div className="settings-body">
      {status && (
        <div className="list-card">
          <div>数据目录：{status.data_dir}</div>
          <div>磁盘剩余：{status.disk_free_gb} GB · 数据库：{status.db_size_mb} MB</div>
          <div>解析器版本：{status.parser_version} · PDF 后端：{status.pdf_backend}</div>
          <div>模型后端：{status.model_backend} · 端口：{status.port ?? 8765}</div>
          <div>中断任务：{status.interrupted_runs} · 索引版本待重建修订：
            {status.incompatible_revisions}</div>
        </div>
      )}

      <h2 className="mt16" style={{ fontSize: 18 }}>数据目录与模型</h2>
      <div className="list-card">
        <label className="flex">资料目录
          <input style={{ minWidth: 360 }} value={dir}
                 onChange={(e) => setDir(e.target.value)} />
        </label>
        <div className="muted small mt8">
          首次启动检查目录可写、模型摘要、数据库版本和端口；更换目录后重启服务生效，
          原目录数据不会自动迁移。
        </div>
        <div className="flex mt8">
          <button className="secondary"
                  onClick={() => act("/settings/data-dir", { path: dir }, "保存目录")}>
            保存并重启后生效
          </button>
          <button className="ghost" onClick={selfcheck}>离线自检</button>
        </div>
      </div>
      {checks === null && <span className="muted">点「离线自检」查看检查结果。</span>}
      {checks && (
        <div className="list-card">
          {checks.map((c, i) => (
            <div key={i}>
              <span className={`tag ${c.ok ? "ok" : "warn"}`}>{c.ok ? "通过" : "异常"}</span>
              {" "}{c.name}{c.detail && <span className="muted"> · {c.detail}</span>}
            </div>
          ))}
        </div>
      )}

      <h2 className="mt16" style={{ fontSize: 18 }}>异常恢复</h2>
      <div className="list-card">
        <div className="flex">
          <button className="secondary" onClick={selfcheck}>重新自检</button>
          <button className="secondary"
                  onClick={() => act("/settings/retry-interrupted", {}, "重试中断任务")}>
            重试全部中断任务
          </button>
          <button className="secondary"
                  onClick={() => act("/settings/rebuild-index", {}, "重建索引")}>
            从原件重建索引
          </button>
        </div>
        <div className="muted small mt8">
          重试沿用输入快照并保留上次错误记录；重建索引会为原件发布新修订，
          历史引用仍指向当时的修订。
        </div>
        {interrupted.map((r) => (
          <div key={r.id} className="flex mt8">
            <span className="tag warn">已中断</span>
            <span>{r.question}</span><span className="muted">{r.created_at}</span>
            <button className="ghost"
                    onClick={() => act("/settings/retry-interrupted",
                      { run_id: r.id }, "重试任务")}>重试</button>
          </div>
        ))}
      </div>

      <h2 className="mt16" style={{ fontSize: 18 }}>备份与恢复</h2>
      <div className="list-card">
        <div className="flex">
          <button onClick={() => act("/settings/backup", undefined, "创建备份")}>
            创建一致性备份
          </button>
          <span className="muted small">
            备份含数据库快照、原件、定位修订、结构树与正式记录。
          </span>
        </div>
        <div className="mt8">
          {backups.length === 0 && <div className="muted">尚无备份。</div>}
          {backups.map((b) => (
            <div key={b.dir} className="flex mt8">
              <span className="tag">{b.name}</span>
              <span className="muted">{b.created_at || "—"} · {b.size_mb} MB</span>
              <button className="ghost" onClick={() => {
                if (!window.confirm(
                  `从 ${b.name} 恢复？当前数据会先自动留一份可回滚备份。`)) return;
                act("/settings/restore", { backup_dir: b.dir }, "恢复备份");
              }}>恢复到此版本</button>
            </div>
          ))}
        </div>
        <div className="muted small mt8">
          恢复前校验备份数据库完整性；升级或恢复后请随机打开一条 Word 和一条 PDF 引用核对。
        </div>
      </div>

      {msg && <div className="note-block mt16">{msg}</div>}
      <div className="muted mt16">
        服务仅监听本机回环地址，校验本机访问令牌与请求来源；日志默认只记录阶段耗时、
        任务标识和错误码。
      </div>
    </div>
  );
}
