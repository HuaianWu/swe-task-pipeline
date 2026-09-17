# 流水线架构：命令、数据流、状态文件

## 分层

```
tools/swepipe/
├── config.py     配置合并（CLI > env > .env > pipeline.toml > 默认）
├── model.py      TaskRecord：与数据源无关的一行任务；状态判定（candidate / skipped / done）
├── sources/      数据源适配器：feishu（生产）、jsonfile（测试与最小集成）；接 DB/API 见 source-adapters.md
├── select.py     选行与去重（纯函数）
├── ledger.py     TaskRecord ↔ 生成器需要的 ledger.xlsx / records.json
├── generate.py   调用生成器 tools/xlsx2task.py
├── build.py      docker build + 冒烟 + 镜像大小，N 并发
├── package.py    zip 模式：组织交付包并挂回台账（deliver）
└── publish.py    repo 模式：建 GitHub 仓库、推送、写回 URL（publish / push）
```

生成器 `tools/xlsx2task.py` 是独立可用的：输入 xlsx，输出任务目录；`swepipe` 只是给它喂数据并串起前后步骤。

## 命令一览

每条命令都是幂等的，可反复执行。

| 命令 | 输入 | 输出 | 不做什么 |
|---|---|---|---|
| `status` | 数据源全表 | 计数：records / done / candidates / skipped_by_marker | 不写 |
| `pull [--all-local]` | 数据源全表 + `tasks/` 已有目录 | `<work>/ledger.xlsx`、`records.json`、`overrides.stub.json`、`pull-summary.json` | 不写数据源 |
| `gen [--task-ids a,b] [--force] [--preflight]` | `ledger.xlsx` + `task-overrides.json` | `tasks/<task_id>/{task.toml,instruction.md,environment/Dockerfile}`、`<work>/gen-report.json` | 全批任一行不合格就**一个文件都不写**（用 `--task-ids` 分批） |
| `build [--task-ids] [--jobs N] [--timeout S] [--rebuild] [--keep-images]` | `tasks/<id>/environment` | 每架构一个镜像（通过即删）、`<work>/build-results.json`、`<work>/logs/<id>-<arch>.log` | 不上传；已 ok 的 (任务, 架构) 跳过 |
| `deliver [--dry-run] [--no-upload] [--task-ids] [--redeliver] [--allow-untyped] [--allow-invalid]` | `records.json` + `build-results.json` + 数据源实时状态 | `<work>/deliver/<题目名称>.zip`、附件挂到该行、`deliver-state.json` | 不碰未验证行；不校验表格内容语义 |
| `publish [--dry-run]` | 同上（repo 模式） | GitHub 仓库、URL 写回、`publish-state.json` | 不碰未验证行 |
| `push [--task-ids] [--no-verify] [--dry-run]` | `tasks/<id>` | 已存在仓库的更新提交 | 不写数据源 |
| `lint` | `tasks/*/environment/Dockerfile` | 违规清单 | |
| `config` | | 生效配置（密钥打码） | |

全局选项放在命令前：`--root DIR --source feishu|json --delivery zip|repo --work DIR --tasks DIR --overrides FILE --owner NAME --platforms a,b`。

## 一行任务的生命周期

1. **pull**：`select.py` 按顺序判定每行——非候选忽略；备注以跳过标记开头忽略；标题（去空白、忽略大小写）匹配本地任务目录 → `existing`（复用目录，只构建上传）；标题在别的行已有产物 → 重复忽略；本批内重复 → 忽略；否则 `new`。zip 模式下 `task_id = <仓库名>-<source_id 前 6 位>` 自动生成；repo 模式需要人在 overrides 里补英文 `task_id / display_title / display_description`（`overrides.stub.json` 列出缺的行）。
2. **gen**：生成器浅克隆仓库到 `REPOS_DIR`，校验 base commit 在默认分支上，检测构建方式（pyproject / setup.py / requirements / uv.lock / poetry / go.mod / Cargo / package.json），解析钉死依赖，按 `templates/` 渲染三件套。overrides 里的 `install_block` 整段替换依赖安装段。生成后立即 `pin_lint`。
3. **build**：先按「Dockerfile 字节 + 各平台冒烟计划 + 冒烟网络」给每个任务算指纹，指纹相同的任务只构建一个代表，其余直接复用结果（`build-results.json` 里带 `reused_from` 和 `fingerprint`）；以前跑过的同指纹结果也会被复用，`--rebuild` 时每组重建一次。然后对每个 (任务, 平台) 依次 `docker build`（网络类错误自动重试 4 次）、容器内 `du` 量大小、在 `--network none`（或 `--internal` 网络）容器里用 `bash -c` 跑冒烟命令（3600 s 预算）、通过即删镜像。冒烟计划在任务**开始**时从 overrides 读取：原生平台默认跑完整测试，模拟平台默认轻量冒烟，`smoke` 覆盖项优先。失败分类：`environment`（连不上服务、缺模块、DNS、找不到 docker）、`tests`（用例本身红）、`timeout`。
4. **deliver**：对 `build-results.json` 里每个平台都 ok 且大小达标的行，实时读取该行（防止台账已改），下载 patch / 轨迹 / 截图，规范化 rubric 与产物结果，写 16 键 `task.toml`，打成 `<题目名称>.zip`，跑 `package_check`，上传并挂到附件列，记入 `deliver-state.json`。
5. **verify_attachments**：独立复核，从台账重新下载附件跑体检。

## 状态文件（`<work>/`，不入库）

| 文件 | 写入者 | 内容 |
|---|---|---|
| `records.json` | pull | 本次选中的行：`ledger_row, key(record_id), seq, title, source_id, task_id, existing, local_only, language…` |
| `ledger.xlsx` | pull | 生成器输入 |
| `overrides.stub.json` | pull | repo 模式下缺英文元数据的行 |
| `select-options.json` | dump_select_options | 表的单选项，供 package_check |
| `gen-report.json` | gen | 每行的生成结果 / 阻断原因 |
| `build-results.json` | build | `{task_id: {platform: {status, reason, size_gb, seconds, log, smoke, fingerprint, reused_from?}}}` |
| `logs/<task>-<arch>.log` | build | 完整构建 + 冒烟日志 |
| `deliver/<题目名称>.zip`、`downloads/<key>/` | deliver | 交付包与附件缓存 |
| `deliver-state.json` / `publish-state.json` | deliver / publish | 已交付行、附件名或仓库 URL、时间 |

`source_id` 是行的稳定标识（由仓库 + base commit + 标题派生，`bz` 前缀的 32 位 hash），`task-overrides.json` 用它做键，所以台账重排、改序号都不影响配方匹配。

## Dockerfile 骨架

`templates/Dockerfile.head.tmpl` 按规范逐字生成头部：`FROM mars-base@sha256:…`（钉 digest），然后是规范里的"时间旅行"段：克隆仓库、`git checkout -B <默认分支> <base>`、`git remote remove origin`、删掉其它分支和不在 base 祖先链上的 tag、`git gc`、`core.hooksPath=/dev/null`，让容器里的仓库看起来就像 base commit 当天的样子。之后是语言安装段（`install.python.tmpl` / `install.go.tmpl`，或 overrides 的 `install_block`），末尾 `Dockerfile.tail.tmpl` 附加 `test -z "$(git status --porcelain)"` 断言检出干净。虚拟环境放 `/opt/venv`，不放在 `/app`。

注意：`git remote remove origin` 是规范要求，但会让检查仓库远端的测试失败（见 environment-recipes.md 的处理方式）。
