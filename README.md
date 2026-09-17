# swe-task-pipeline：把任务台账变成可复现的 SWE-like 任务环境

把台账（飞书多维表格，或任何能导出 JSON 的系统）里"初检通过"的一行任务——题目、上游仓库、base commit、需求原文、rubric、patch、轨迹——变成一个**离线可运行、依赖全部钉死、双架构验证过**的任务环境，然后按交付模式产出：

- `DELIVERY=zip`：按《SWE-like Repo 交付包规范》组织成 `<题目名称>.zip`，上传到该行的「交付包（zip）」附件列（当前生产模式）。
- `DELIVERY=repo`：发布成一个 GitHub 仓库，把 URL 写回台账。

```
台账一行 ──pull──▶ ledger.xlsx + records.json ──gen──▶ tasks/<task_id>/
                                                        ├── task.toml               任务元数据
                                                        ├── instruction.md          需求原文（逐字）
                                                        └── environment/Dockerfile  可复现环境（依赖钉死）
        ──build──▶ 每个架构 docker build + 离线真实测试 ──deliver──▶ <题目名称>.zip ──▶ 挂到台账「交付包（zip）」列
                                                       └─publish──▶ github.com/<owner>/<task_id> ──▶ URL 写回台账
```

台账是唯一的状态：附件 / URL 已填 = 完成；每一步都可以重复执行，只处理还没完成的行。

## 文档导航

| 想知道 | 看 |
|---|---|
| 5 分钟跑起来 | 本文「快速开始」 |
| 每个命令做什么、状态文件在哪 | [docs/architecture.md](docs/architecture.md) |
| 交付包里有什么、task.toml 字段怎么来的 | [docs/delivery-package.md](docs/delivery-package.md) |
| 给某个仓库写环境配方（`task-overrides.json` 的每个键） | [docs/task-overrides.md](docs/task-overrides.md) |
| 常见仓库类型的配方套路（服务内置、root 差异、模拟架构抖动……） | [docs/environment-recipes.md](docs/environment-recipes.md) |
| 日常批处理的操作手册（新行、返修、复核、哪些行要退回提交人） | [docs/operations.md](docs/operations.md) |
| 报错怎么办 | [docs/troubleshooting.md](docs/troubleshooting.md) |
| 接数据库 / API 代替飞书 | [docs/source-adapters.md](docs/source-adapters.md) |
| 用一台 x86 云主机补验 amd64 | [docs/cloud-agent.md](docs/cloud-agent.md) |

## 仓库结构

| 路径 | 作用 |
|---|---|
| `tools/swepipe.py` | 流水线命令入口；实现在 `tools/swepipe/`（config / sources / select / ledger / generate / build / package / publish） |
| `tools/xlsx2task.py` | 生成器：克隆仓库、检测构建方式、解析并钉死依赖、渲染 task.toml + instruction.md + Dockerfile |
| `tools/pin_lint.py` | 依赖钉版本规则检查器；gen / build / deliver / publish 都以它为门禁 |
| `tools/package_check.py` | 交付包入库体检的离线复刻（结构、Dockerfile、task.toml 16 键、rubric、run_result） |
| `tools/rubric_yaml.py` | 把「Verify Rubric」单元格的各种写法规范成 `tests/nl_rubric.yaml`，并规范化「产物结果」 |
| `tools/add_override.py` | 按 task_id 把配方合并进 `task-overrides.json`（自动换算成 source_id 键） |
| `tools/verify_attachments.py` | 交付后复核：从台账重新下载附件，解压，跑 package_check |
| `tools/dump_select_options.py` | 从飞书表结构导出单选项列表，供 package_check 校验 |
| `tools/cloud_agent.py` | 可选：把 amd64 验证交给一台 x86 云主机上的 agent |
| `tools/tests/` | 离线自检（不需要飞书、Docker、GitHub） |
| `task-overrides.json` | **知识库**：三百多个已交付任务的环境配方（安装段、冒烟命令、跳过原因），按台账行的 source_id 键 |
| `templates/` | Dockerfile 头尾（规范骨架）与语言安装段模板 |
| `docs/` | 文档 |
| `examples/` | JSON 数据源样例、配方样例 |
| `pipeline.toml` / `.env` | 本机配置（不入库；示例见 `pipeline.toml.example`、`.env.example`） |
| `feishu-sync/work*/`、`tasks*/` | 运行时产物（不入库）：状态文件、下载、生成的任务目录 |

## 快速开始

依赖：Python ≥ 3.10、Docker（含 buildx，能跑 `--platform linux/amd64` 与 `linux/arm64`）、git、`gh` CLI（仅 repo 模式）。生成器首次运行会自动下载一份 uv 到 `feishu-sync/_repos/.tools`。

```bash
git clone <this repo> && cd swe-task-pipeline
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp pipeline.toml.example pipeline.toml     # 填表 ID、交付模式、工作目录、平台
cp .env.example .env && chmod 600 .env     # 填 FEISHU_APP_ID / FEISHU_APP_SECRET（及可选的 GITHUB_TOKEN、CLOUD_AGENT_*）
```

飞书侧需要一个自建应用，开通多维表格读写和云文档上传权限，并把应用添加为该多维表格的协作者（可编辑）。

```bash
P=.venv/bin/python
$P tools/swepipe.py config                 # 看生效配置（密钥打码）
$P tools/swepipe.py status                 # 台账计数：总数 / 已完成 / 待处理
$P tools/swepipe.py pull                   # 选行、去重，写 work/ledger.xlsx + records.json
$P tools/dump_select_options.py            # 一张表做一次：导出单选项给 package_check
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple $P tools/swepipe.py gen   # 生成 tasks/<task_id>/（克隆仓库，几分钟）
$P tools/swepipe.py build --jobs 2         # 双架构构建 + 离线真实测试；结果在 work/build-results.json
$P tools/swepipe.py deliver --dry-run      # 看会打包上传什么
$P tools/swepipe.py deliver                # 打包 <题目名称>.zip 并挂到台账
$P tools/verify_attachments.py --all       # 从台账重新下载附件复核
```

多张表并行处理时，每张表用自己的工作目录和任务目录，配方文件共享：

```bash
FEISHU_TABLE_ID=<table id> $P tools/swepipe.py --work feishu-sync/work-b --tasks tasks-b <cmd>
```

## 核心规则（甲方验收标准的落地）

1. **只处理** `初检结果 == 初检通过` 且交付列为空的行；标题与本地已有 `tasks/*/task.toml` 相同的行复用目录；标题在别处已交付或本批重复的行忽略（表内去重由人工处理）。
2. **依赖必须钉死**：Dockerfile 里 pip / apt / `go get` 的每个依赖都要精确版本（`pkg==x.y.z`、`pkg=debver`、`mod@vX`）；仓库自己的 `-e .`、`-r` 文件、锁文件安装、`go mod download` 豁免。没有锁文件的 Python 仓库用 `uv pip compile --exclude-newer=<base commit 日期>` 解析。基础镜像钉 digest。`pin_lint.py` 强制执行。
3. **可运行性**：原生架构在 `--network none` 的容器里跑仓库**自己的真实测试**（Python `pytest`，Go `go test ./...`），3600 s 预算；模拟架构跑轻量冒烟（编译 / 收集）。需要外部服务的仓库把服务装进镜像并在入口脚本里启动；依赖 Docker 守护进程、DNS、外网、root 权限差异的测试**按名跳过并在 Dockerfile 注释里写明原因**。
4. **相同环境只验证一次**：Dockerfile 与冒烟计划完全相同的任务（同一仓库、同一 commit、同一配方的多行）只构建、测试一次，其余行记录同一结果并标 `reused_from`；`--rebuild` 时每组也只重建一次。
5. **只上传验证过的**：每个配置的架构都 `ok` 且镜像 `du` 大小 ≤ `max_image_gb`（默认 12 GB）才上传；过大只在备注前加标记；任一架构失败留到下次重试。
6. **rubric 类型不猜**：Verify Rubric 条目缺 f2p/p2p 的行（`rubric_untyped`）、「产物结果」没有逐条记录的行（`result_unstructured`）不打包，退回提交人补。
7. **instruction.md 保持中文原文**，逐字来自「需求 Prompt（原文）」列。

## 配置

优先级：命令行 > 环境变量 > `.env` > `pipeline.toml` > 默认值。全部键见 `tools/swepipe/config.py` 顶部。非密钥配置（表 ID、交付模式、工作目录、平台、并发）放 `pipeline.toml`，密钥只放 `.env`。

- 换表：`[feishu] table_id` 或 `FEISHU_TABLE_ID`；换 base：`base_token` / `FEISHU_BASE_TOKEN`。每张表用独立的 `[paths] work` 与 `tasks`。
- 换交付模式：`[delivery] mode = "zip" | "repo"`。
- repo 模式换账号：`.env` 里设 `GITHUB_TOKEN`（需要 `repo` 权限）与 `GITHUB_OWNER`；不设则用本机 `gh auth login` 的账号。
- 只能构建一种架构的机器：`PLATFORMS=linux/amd64`。
- 本机代理不稳时：`[build] direct = true`，构建的 RUN 步骤绕开 Docker Desktop 注入的代理。
- `FULL_TESTS=0` 临时退回全部轻量冒烟（只用于排查，交付前必须恢复）。

## 运行环境与资源

脚本没有平台假设。目前在 Apple Silicon Mac 上运行：arm64 原生，amd64 走 Rosetta 模拟（慢 2–3 倍）。Docker VM 内存 8 GB 左右时：`build --jobs 2` 是上限，大型 Go / Python 套件用 `--jobs 1`；构建进行时不要再起诊断容器（会把并发的模拟架构编译 OOM 掉，exit 137）。通过的镜像会自动删除，失败的保留供排查；`docker builder prune` 自动运行，保留 40 GB 缓存。

## 自检

```bash
.venv/bin/python tools/tests/test_swepipe.py       # 选行、去重、ledger、JSON 数据源、打包
.venv/bin/python tools/tests/test_rubric_yaml.py   # rubric 解析
```

## 许可

MIT，见 [LICENSE](LICENSE)。`task-overrides.json` 里引用的上游仓库各自遵循其许可证。
