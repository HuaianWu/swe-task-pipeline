# 排错

按阶段排列。日志位置：构建 / 冒烟在 `<work>/logs/<task>-<arch>.log`，生成在 `<work>/gen-report.json`。

## 配置与数据源

| 现象 | 原因 / 处理 |
|---|---|
| `missing configuration [...]` | `.env` / `pipeline.toml` 缺键；`swepipe config` 看生效值 |
| `feishu table ... lacks required column(s)` | 表缺必需列（题目名称、Repo URL、Commit/版本、初检结果、交付列）。新表先建列，或在 `sources/feishu.py` 的 `ALIASES` 加别名 |
| `feishu error 99991663/99991668` | 应用没有该多维表格权限：把应用加为协作者（可编辑），并开通 bitable 与 drive 上传权限 |
| `feishu error 403` 打开别人贴的链接 | 跨租户文档，API 拿不到，忽略 |
| `status` 的 candidates 比预期少 | 行的初检结果不是"初检通过"，或交付列已非空，或备注以跳过标记开头 |
| `pull` 报 `rows needing overrides` | repo 模式需要英文元数据；按 `overrides.stub.json` 补进 `task-overrides.json`（键用 source_id） |

## gen

| 现象 | 原因 / 处理 |
|---|---|
| 整批一个目录都没生成 | 全批任一行阻断即不写；看 `gen-report.json`，用 `--task-ids` 分批 |
| `unpinned pip dependency` | 裸包名进了 Dockerfile：在 `install_block` 里写死版本；脚本自动加的包在 `xlsx2task.PIP_PINS` / `APT_PINS` |
| `uv pip compile` 报 "no versions of X" | 镜像缺上传时间元数据，`--exclude-newer` 失效：对该行去掉 `UV_INDEX_URL` 走 pypi.org |
| pip `ResolutionImpossible` / 重复钉版本 | venv 缺 `packaging`，marker 求值退化成启发式；装上后重新 gen。`grep -c '==' Dockerfile` 与 `sort | uniq -d` 找重复 |
| 编译结果里混进 `# via` 行 | 处理 uv 输出时按 strip 后的行过滤注释 |
| `base commit not on default branch` | 台账的 commit 在分支 / fork 上，或根本是错的：退回提交人 |
| 目录已存在 | `gen` 不覆盖：`rm -rf tasks/<id>` 或 `--force` |

## build

| 现象 | 原因 / 处理 |
|---|---|
| `no generated task dir ...` 立刻退出 | records.json 里有行没有任务目录；先 gen 或 `--task-ids` 限定 |
| `gnutls_handshake() failed` / `proxy.golang.org ... EOF` | 本机代理丢并发 TLS 握手；build 自动重试 4 次，仍失败再跑一次（只重建未通过的），或 `[build] direct = true` |
| exit 137 / `signal: killed` | Docker VM OOM：降并发 `--jobs 1`，去掉 `-race`，pytest 分目录跑，等其它容器结束 |
| 冒烟 `environment`：`Connection refused` / `ModuleNotFoundError` / `docker: not found` / `Name or service not known` | 环境不完整：服务没装或没启动、测试 extras 没钉、testcontainers、DNS。按 environment-recipes.md 处理 |
| 冒烟 `tests` | 用例本身红：看是不是 root/离线/时区导致，是则跳过并注明；不是则可能是 base commit 本身就红（少数，注明） |
| 冒烟 `timeout` | 3600 s 不够：缩范围、`-short`、模拟架构只编译 |
| `no tests collected` / `not found in markers` | pytest 配置目录不对（monorepo 子目录）、缺插件、缺 `conftest` 依赖 |
| 模拟架构随机失败、原生稳定 | 计时敏感或汇编 SIGILL：见 recipes 第 6 节；VM 空闲时 `--jobs 1` 重跑 |
| 改了 `smoke` 但没生效 | build 在任务开始时读取计划；等结束后 `--rebuild` |
| 改了 `.env`/`pipeline.toml` 但 build 行为不变 | 已启动的进程不重读配置 |
| `test -z "$(git status --porcelain)"` 失败 | 构建期生成的文件弄脏了检出：`rm -rf` 或加到安装段末尾清理 |
| 找不到 venv 里的命令 | 用了登录 shell（`bash -lc`），mars-base 的 profile 重置 PATH |

## deliver / package_check

| 现象 | 原因 / 处理 |
|---|---|
| `rubric_untyped` / `result_unstructured` | 台账内容不合规，退回提交人（`--allow-untyped` 只用于自己检查） |
| `run_result 第 N 行格式无效 / 带句点 ID` | 产物结果规范化后仍不合规：单元格里有总结句或 `1.` 形式的 ID；`rubric_yaml.normalize_run_result` 处理不了的让提交人改 |
| `zip 里必须只有一个顶层目录` | 手工改包时混进了 `__MACOSX` 或多个目录 |
| task.toml 单选值不在选项里 | 表的单选项与默认列表不同：`tools/dump_select_options.py` 导出到 `<work>/select-options.json` |
| `row no longer in the source` | 台账里这行删了，跳过 |
| 上传 > 20 MB 失败 | 飞书 `upload_all` 上限；截图 / 轨迹过大时让提交人压缩 |
| submit_date 差一天 | 早于 UTC 08:00 提交的行按 UTC 转会前移一天；适配器已用 Asia/Shanghai，旧包需 `--redeliver` |

## 等待与监控脚本

- `pgrep -f "<命令文本>"` 会匹配等待脚本自己 → 用 PID（`kill -0`）或 `pgrep -f "swepipe[.]py --work …"`。
- zsh 里以 `=` 开头的词会被当成命令查找（`echo ====` 报错）。
- 判断"通过"要看真实返回码：`cmd | tail` 的 `$?` 是 tail 的。

## 磁盘

`docker system df`；`docker builder prune` 自动跑（保留 40 GB 缓存）；失败镜像手动 `docker rmi`；`feishu-sync/_repos` 是浅克隆缓存，可删。
