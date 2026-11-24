"""TraceQA 本地服务配置。单机、单用户，仅监听回环地址。"""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

PARSER_VERSION = "traceqa-parser-0.4.0"
INDEX_VERSION = "traceqa-index-0.1.0"
RULES_VERSION = "traceqa-rules-0.5.0"
MODEL_VERSION = "rules-only-0.1.0"

# 设置页选择的资料目录持久化到项目根的 .traceqa_data_dir，
# 重启生效；避免把用户数据写进会随程序升级被覆盖的位置。
FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = (Path(sys._MEIPASS) if FROZEN
                else Path(__file__).resolve().parent.parent.parent)
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else PROJECT_ROOT
DEFAULT_DATA_DIR = (Path(os.environ.get("LOCALAPPDATA", INSTALL_DIR)) / "TraceQA"
                    if FROZEN else PROJECT_ROOT / "data")
DATA_DIR_FILE = (DEFAULT_DATA_DIR / ".traceqa_data_dir" if FROZEN
                 else PROJECT_ROOT.parent / ".traceqa_data_dir")


def configured_data_dir() -> Path:
    if os.environ.get("TRACEQA_DATA"):
        return Path(os.environ["TRACEQA_DATA"])
    try:
        if DATA_DIR_FILE.exists():
            p = Path(DATA_DIR_FILE.read_text(encoding="utf-8").strip())
            if p.is_absolute():
                return p
    except OSError:
        pass
    return DEFAULT_DATA_DIR


@dataclass
class Config:
    data_dir: Path = field(default_factory=configured_data_dir)
    host: str = "127.0.0.1"
    port: int = int(os.environ.get("TRACEQA_PORT", "8765"))
    ollama_model: str = field(default_factory=lambda:
                              os.environ.get("TRACEQA_OLLAMA_MODEL", "").strip())
    embedding_model: str = field(default_factory=lambda:
                                 os.environ.get("TRACEQA_EMBEDDING_MODEL", "").strip())
    embedding_digest: str = field(default_factory=lambda:
                                  os.environ.get("TRACEQA_EMBEDDING_DIGEST", "").strip())

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)

    @property
    def model_backend(self) -> str:
        if self.ollama_model:
            return f"规则核查 + Ollama 原文推荐（{self.ollama_model}）"
        return MODEL_VERSION

    @property
    def db_path(self) -> Path:
        return self.data_dir / "traceqa.db"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "records"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self):
        for d in (self.data_dir, self.originals_dir, self.tmp_dir,
                  self.exports_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)


def new_token() -> str:
    return secrets.token_urlsafe(24)
