"""Create verifiable snapshots and apply a scheduled restore before serving."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from . import db
from .config import Config
from .store import word_tree_path

ASSET_DIRS = ("originals", "trees", "records")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create_backup(cfg: Config, prefix: str = "snapshot") -> Path:
    dest = cfg.data_dir / "backups" / db.new_id(prefix)
    dest.mkdir(parents=True)
    with closing(db.connect(cfg.db_path)) as conn:
        conn.execute("VACUUM INTO ?", (str(dest / "traceqa.db"),))
    for name in ASSET_DIRS:
        if (cfg.data_dir / name).is_dir():
            shutil.copytree(cfg.data_dir / name, dest / name)
    hashes = {p.relative_to(dest).as_posix(): _digest(p)
              for p in dest.rglob("*") if p.is_file()}
    (dest / "manifest.json").write_text(
        json.dumps({"created_at": db.now_iso(), "sha256": hashes}, ensure_ascii=False),
        encoding="utf-8")
    return dest


def validate(source: Path) -> None:
    database = source / "traceqa.db"
    if not database.is_file():
        raise FileNotFoundError("备份不存在或缺少数据库")
    manifest = source / "manifest.json"
    if manifest.exists():
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("sha256", {})
        for relative, expected in entries.items():
            target = (source / relative).resolve()
            if not target.is_relative_to(source.resolve()) or _digest(target) != expected:
                raise ValueError(f"备份文件校验失败：{relative}")
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性校验失败")
        for sha, version_id in conn.execute(
                "SELECT sha256, id FROM versions WHERE status='published'"):
            if _digest(source / "originals" / sha[:2] / f"{sha}.bin") != sha:
                raise ValueError(f"原件校验失败：{version_id}")
        for version_id, revision_id, parser in conn.execute(
                "SELECT v.id, r.id, r.parser_version FROM parse_revisions r "
                "JOIN versions v ON r.version_id=v.id WHERE v.format='docx' AND r.status='ready'"):
            if not word_tree_path(source, version_id, revision_id, parser).is_file():
                raise FileNotFoundError(f"缺少 Word 定位数据：{revision_id}")
        for old_path, sha in conn.execute("SELECT file_path, content_hash FROM records"):
            if _digest(source / "records" / Path(old_path).name) != sha:
                raise ValueError("正式记录校验失败")


def _validate_target(cfg: Config, source: Path) -> None:
    validate(source)
    for name in ASSET_DIRS:
        for entry in (source / name).rglob("*"):
            target = cfg.data_dir / entry.relative_to(source)
            if entry.is_file() and target.exists() and _digest(entry) != _digest(target):
                raise ValueError(f"恢复目标已有同名文件且内容不同，请恢复到独立资料目录：{target.name}")


def schedule_restore(cfg: Config, source: Path) -> None:
    source = source.resolve()
    _validate_target(cfg, source)
    pending = cfg.data_dir / "restore-pending.json"
    temp = cfg.tmp_dir / db.new_id("restore")
    temp.write_text(json.dumps({"source": str(source)}), encoding="utf-8")
    temp.replace(pending)


def apply_pending_restore(cfg: Config) -> Path | None:
    pending = cfg.data_dir / "restore-pending.json"
    if not pending.is_file():
        return None
    try:
        source = Path(json.loads(pending.read_text(encoding="utf-8"))["source"])
        _validate_target(cfg, source)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        # Validation precedes writes to the live store. Keep the request for diagnosis.
        pending.replace(cfg.data_dir / f"{db.new_id('restore-failed')}.json")
        print(f"已跳过失效的恢复请求，继续打开现有资料库：{exc}", flush=True)
        return None
    if cfg.db_path.exists():
        create_backup(cfg, prefix="before-restore")
    for name in ASSET_DIRS:
        if (source / name).is_dir():
            shutil.copytree(source / name, cfg.data_dir / name, dirs_exist_ok=True)
    source_db = source / "traceqa.db"
    with closing(sqlite3.connect(f"{source_db.resolve().as_uri()}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(cfg.db_path)) as target:
            src.backup(target)
    pending.unlink()
    return source
