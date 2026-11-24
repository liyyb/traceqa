"""原件存储：按完整哈希寻址，先写临时件、校验后改名发布（手册 §4.2）。"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .config import Config


def word_tree_path(root: Path, version_id: str, revision_id: str, parser_version: str) -> Path:
    """Old revisions reference the version-level tree used by the original reader."""
    legacy = parser_version.split("+", 1)[0] in {"traceqa-parser-0.1.0", "traceqa-parser-0.2.0"}
    return root / "trees" / f"{version_id if legacy else revision_id}.json"


class OriginalStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _final_path(self, sha256: str) -> Path:
        return self.cfg.originals_dir / sha256[:2] / f"{sha256}.bin"

    def path_for(self, sha256: str) -> Path:
        return self._final_path(sha256)

    def exists(self, sha256: str) -> bool:
        return self._final_path(sha256).exists()

    def stage(self, data: bytes) -> tuple[str, Path]:
        """写入同盘临时目录并计算哈希，返回 (sha256, tmp_path)。"""
        self.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(data).hexdigest()
        tmp = self.cfg.tmp_dir / f"{sha}.part"
        with open(tmp, "wb") as f:
            f.write(data)
        # 回读校验
        h2 = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if h2 != sha:
            tmp.unlink(missing_ok=True)
            raise IOError("临时件校验失败")
        return sha, tmp

    def publish(self, sha256: str, tmp_path: Path) -> Path:
        """原子发布：改名到正式位置。发布前中断不留半件。"""
        final = self._final_path(sha256)
        if final.exists():
            tmp_path.unlink(missing_ok=True)
            return final
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tmp_path), str(final))
        return final

    def cleanup_orphan_tmps(self):
        """只清理由本次任务产生且无任何引用的临时件。"""
        if not self.cfg.tmp_dir.exists():
            return
        for p in self.cfg.tmp_dir.glob("*.part"):
            try:
                p.unlink()
            except OSError:
                pass
