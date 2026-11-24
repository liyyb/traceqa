# TraceQA

## 下载体验

从 [GitHub Releases](../../releases) 下载 `traceqa-windows.zip` 并完整解压。Windows 双击启动 TraceQA.cmd。体验材料另行下载。先导入同组材料，再提问并查看原文引用。

体验材料附件 `traceqa-demo-materials.zip` 提供模拟制度。上海案例可打开 PDF 与 Word 的原文引用核对冲突。


中文本地文档核查工作台：选择制度原件 → 提出实际问题 → 获得带适用条件和出处的核查记录 → 导出可离线复核的 HTML。

## 目录结构

```
traceqa/
├── backend/traceqa_server/   FastAPI 服务、SQLite 数据契约、检索/判断/记录/补件
├── parsers/traceqa_parsers/  DOCX（OOXML 结构树）与 PDF 解析器、定位契约
├── frontend/                 React + TypeScript + Vite，PDF.js 阅读器
├── evaluation/               夹具生成器、回归脚本、定位契约单元测试
├── packaging/launcher.py     桌面启动器（单实例、健康检查、退出回收）
├── requirements.txt          依赖声明
└── data/                     运行时数据（自动创建，不入库）
    ├── traceqa.db            SQLite（WAL）
    ├── session.json          本次启动的回环地址与访问令牌
    ├── originals/            按 SHA-256 寻址的不可变原件
    ├── trees/                DOCX 结构树
    ├── records/              已签发的正式 HTML 记录
    └── backups/              一致性备份
```

## 首次运行

Windows 用户可双击项目中的 `启动 TraceQA.cmd`。开发目录需要先准备 Python
依赖并构建前端。便携包解压后双击同名脚本即可启动，运行时依赖随包提供。
关闭服务窗口前可按 Ctrl+C 正常退出。便携版默认数据目录为
`%LOCALAPPDATA%\TraceQA`。

```bash
cd traceqa
python -m pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
```

生成验收夹具（虚构制度，9 份，含旋转页／扫描附件／无编号制度）：

```bash
python evaluation/make_fixtures.py
```

启动（任选其一）：

```bash
# 1) 启动器：自动拉起服务并打开浏览器
python packaging/launcher.py
#    首次启动前查看目录/空间/模型包/数据库/端口：python packaging/launcher.py --status

# 2) 直接启动服务（--open 打开浏览器）
python backend/run_server.py --open

# 3) 开发模式：后端 + Vite 前端（5173 端口，/api 代理到 8765）
python backend/run_server.py
cd frontend && npm run dev
```

服务仅监听 `127.0.0.1:8765`，浏览器地址由 `data/session.json` 给出，形如
`http://127.0.0.1:8765/?token=...`。所有接口需带该令牌（请求头
`X-TraceQA-Token` 或查询参数 `token`，后者仅供直链与 SSE 使用）。

环境变量：

| 变量 | 含义 |
|---|---|
| `TRACEQA_DATA` | 数据目录（也可在设置页选择，重启后生效） |
| `TRACEQA_PORT` | 服务端口，默认 8765 |
| `TRACEQA_PDF_BACKEND` | `auto`（默认）／`fitz`（强制 PyMuPDF）／`docling` |
| `DOCLING_ARTIFACTS_PATH` | Docling 模型缓存目录（默认 `data/models/docling`） |
| `TRACEQA_OLLAMA_MODEL` | 可选。本机 Ollama 已安装的模型名称，包含标签。留空时使用规则核查 |
| `TRACEQA_EMBEDDING_MODEL` | 可选。本机 Ollama 已安装的向量模型名称和标签 |
| `TRACEQA_EMBEDDING_DIGEST` | 与向量模型对应的完整 SHA-256 摘要，从本机 Ollama `/api/tags` 读取 |

## 解析后端

| 后端 | 依赖 | 行为 |
|---|---|---|
| Docling | `docling` + 模型包 | PDF 布局、表格结构、标题层级 |
| PyMuPDF（回退） | `PyMuPDF` | 文本块 + `find_tables()` 表格 → 同样的归一化证据框与表格上下文 |

两种后端写入**同一份定位契约**（`parsers/traceqa_parsers/locator.py`）：
Word 用 Unicode 码点区间，PDF 用未旋转裁切区域的归一化框。后端变更会写入
`parse_revisions.parser_version`，历史引用始终指向当时的修订。

## 端到端回归

```bash
# 服务已启动、夹具已生成
python evaluation/regression.py
# 定位契约单元测试（无需服务）
python -m unittest discover -s evaluation/tests -q
```

`regression.py` 从会话文件读取端口和令牌。测试会导入虚构制度并创建正式记录，
建议为测试单独启动服务，避免这些记录混入日常制度库。

```bash
# 在两个终端执行，两个命令使用同一个测试数据目录
python backend/run_server.py --data data/regression --port 8876
python evaluation/regression.py --data data/regression
```

回归覆盖日期分界、分别补充复合问题的条件、冲突并列、关键扫描附件缺口、补件重查、
Word 历史修订定位，以及正式记录更正和旧版下载。单元测试也覆盖解析失败后重复导入和
备份来源失效后继续启动。

## 30 份混杂制度体验库

`evaluation/make_testlib.py` 生成 15 份 PDF + 15 份 DOCX、约 500 页的虚构制度库，
原件在 `evaluation/testlib/`。它刻意包含日期边界、显式替代、真实冲突、扫描附件与
清晰补件、无编号封面、地区分类缺映射、跨页表格、旋转页和同名新版。

```bash
python evaluation/make_testlib.py
python evaluation/acceptance_test.py --base http://127.0.0.1:8765 --data data
python evaluation/perf_probe.py --base http://127.0.0.1:8765 --data data --n 30
```

验收结果见 `evaluation/TEST_REPORT.md`。当前 `data/` 已切换为该测试库，
原夹具数据备份在 `data_fixtures_20261122/`。

## 本地模型与性能测量

启动前在 PowerShell 设置 `$env:TRACEQA_OLLAMA_MODEL = '已安装的模型名:标签'`，
然后用同一个终端启动 TraceQA。服务会向回环地址 `127.0.0.1:11434` 发出结构化请求。
每次核查最多调用一次，用于推荐第一个依据不足事项的候选原文。模型返回片段编号，
系统直接读取资料库中的原文与位置，并以关联材料显示。金额、版本冲突和覆盖缺口
由规则核查。模型无法连接、超时或返回无效编号时，保留规则结果。
使用模型完成推荐的核查会记录模型名称。[Ollama Generate API](https://docs.ollama.com/api/generate)

配置向量模型和摘要后，执行 `python packaging/launcher.py --reindex-semantic`
或便携版的 `TraceQA.exe --reindex-semantic` 构建索引。导入新材料后可再次运行，
已有向量按模型摘要复用。检索同时保留原有关键词与词袋候选。
[Ollama Embed API](https://docs.ollama.com/api/embed)

用独立测试库做性能测量。先跑回归或手动完成一条样本核查，从历史详情或接口取运行 ID，
然后执行下列命令。脚本会新建 31 条测试运行，校对状态和结论是否与样本一致，
并输出首轮样本、后续 30 次的明细及各阶段中位数和 P95。

```powershell
python -X utf8 evaluation/benchmark.py --data data/regression --run-id <样本运行ID> --samples 30
```

端到端时间包含本机 HTTP 和轮询间隔，前端绘制耗时需单独在浏览器中测量。
首轮样本可能使用已热的进程或已加载的模型。冷启动应在独立重启后单独记录。

## 当前实现边界

- 结论生成使用确定性规则。检索默认融合关键词和哈希词袋。配置 Ollama 后可加用语义索引及关联原文推荐。
  已实现的独立校验：证据在范围内、原句一致、金额回读、单位/比较关系区分、
  地区分类依据缺失时不得给确定金额、按显式线索二次补取（`MAX_REFETCH`）。
- 首版 Word 输入为 `.docx`，扫描页列为覆盖缺口，未启用 OCR。
- 人工说明、补录字段、正式记录与更正版均冻结快照，旧版下载逐字节一致。
- 导出件含制度全称／编号／日期及含义／导入文件名／定位／原句、文内 `[n]`
  引用锚点、事项内脚注与打印重复页眉；补录与原件字段分行，不会被印成原件字段。

## 尚未交付（手册 §6、§8）

合成评测可用 `python evaluation/release_acceptance.py --report release/qa/quality.json`
复跑。该脚本生成 30 份、约 500 页、101 道带出处标注的测试题并保存实际输出和指标。
首轮基线为本机的 Windows 11、Ryzen 7 9800X3D、约 32 GB 内存及 Radeon RX 9070 XT。
真实制度盲测、实际 Ollama 模型的冷/热性能及质量验证、缩放和键盘
可访问性完整走查、干净 Windows 账户断网演练及人工复核属于正式发布验收。

## Windows 便携包构建

在装好项目依赖与 PyInstaller 的 Windows Python 环境中构建前端，再运行
`python packaging/build_windows.py --output release/portable --zip-only`。每次构建使用新的输出目录。
`--zip-only` 只保留 ZIP 和校验文件。省略此参数还会保留解压后的 `TraceQA` 目录。
打包过程的中间文件放在系统临时目录，构建结束后自动清理。
包内含运行环境版本清单、前端依赖锁定文件、Python 依赖附带的许可证和
`SHA256.json`。将包解压到可读目录后运行，默认用户数据单独存放。
Ollama 运行时与模型需按部署环境单独准备。


## 备份与恢复

设置页可创建一致性备份（`VACUUM INTO` + 原件 + 结构树 + 记录）并从中恢复。
恢复请求先校验数据库、原件及记录的完整性。服务下次启动时先创建当前目录的回滚
备份，再应用选定快照。迁移到新机器时可将备份放入目标资料目录的 `backups`，
在设置中选中并重启。也可以在不同资料目录中验证恢复后的原件和记录。
