# 环境配方套路

甲方的可运行性标准：**断网、没有 Docker、以 root 运行的容器里，仓库自己的测试要跑通**。上游 CI 通常不是这个条件，所以配方的本质是把 CI 环境搬进镜像，并把搬不进来的测试按名跳过、写明原因。以下按问题类型列出已验证的套路，具体配方在 `task-overrides.json` 里按 task_id 搜。

## 1. 基础镜像事实（mars-base，2025-12 构建，7 GB）

- Python 3.12 默认，`python3.11` 在 `/usr/bin`；pip 25；uv 0.9.18（太旧，配方自装 `uv==0.12.9`）；Poetry 1.8.2（读不了 lock 2.1）。
- Go 1.25.5，`GOTOOLCHAIN=auto`：`go.mod` 要求更新版本时构建期自动下载到 GOMODCACHE，离线复用。需要固定版本时 `ENV GOTOOLCHAIN=go1.26.8`。
- Node 24、yarn 1.22、**`NODE_ENV=production`**：`yarn install` / `npm install` 会跳过 devDependencies，前端构建要 `yarn install --frozen-lockfile --production=false` 或 `npm install --include=dev`。
- git 2.39.5（没有 `index.skipHash`、不支持 SHA-256 仓库；需要时从 kernel.org 源码包编译 2.47）。
- 有 tzdata、scp/ssh、setuptools 80.9；没有 ping、msgfmt、rsync、libsecret。
- `/etc/profile` 会重置 PATH：冒烟用 `bash -c`，不要登录 shell。

## 2. 需要外部服务：装进镜像，入口脚本启动

| 服务 | 做法（Debian 12 包，版本在 mars-base 里 `apt-cache policy` 查） |
|---|---|
| PostgreSQL | `postgresql-15`；安装段里 `pg_ctlcluster 15 main start` 后建角色/库再 stop；冒烟前 start。测试若通过 `docker compose up` 起库，写一个 `/usr/local/bin/docker` 垫片让 fixture 原样可用 |
| Redis | `redis-server=5:7.0.15-…`；冒烟 `redis-server --daemonize yes` |
| Solr / 大二进制 | 从 archive.apache.org 下载指定版本并钉 sha256，`ENTRYPOINT` 脚本一起启动 |
| 需要域名 | 入口脚本把域名写进 `/etc/hosts`（mock 过的 webhook 测试离线可跑） |
| GUI（Xvfb） | `xvfb` + Mesa 库，入口 `Xvfb :99 &`，`ENV DISPLAY=:99.0`；ALSA 用 null device |
| MPI | `libopenmpi-dev openmpi-bin` + `mpi4py` 钉版本；root 需要 `OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1` |

服务启动写成 `/usr/local/bin/<repo>-services` 或 ENTRYPOINT，冒烟命令引用它；镜像里不能有 Docker，用到 testcontainers / dktest 的测试按名跳过或排除整个包。

## 3. 离线：谁会失败

- DNS 解析公网域名的测试（`net.LookupHost`、`socket.getaddrinfo`、连 `ya.ru`、`example.com`）→ `-skip` / `--deselect`，注释写明。
- 需要外网 API 的测试（GitHub API、Helm 仓库、Terraform registry、npm）→ 排除包或按名跳过；`-short` 常能去掉一大批。
- 需要非回环网卡的测试（ICE 候选收集、集群 advertise、`net.InterfaceAddrs`）→ `smoke_network = "internal"`。
- 需要 `CAP_NET_ADMIN` 的（`SO_RCVBUFFORCE`）→ 跳过。
- 预下载数据：`scipy.datasets.ascent()`、DuckDB 扩展（`INSTALL tpch`）、Go 工具链、Node 版本——都在构建期 `RUN` 里拉好。

## 4. root 与 CI 用户的差异

root 无视文件权限位，所以这些测试在容器里会"意外通过"或失败：

- `chmod 0000/0500` 后断言 `PermissionError` 的测试 → 跳过并注明"root bypasses mode bits"。
- 写 `/nonexistent`、`/root` 权限测试、cgroup 内存统计、`/etc/mime.types` 与 shared-mime-info 差异 → 能修环境就修（装 `shared-mime-info`），否则跳过。
- 有些程序**拒绝 root**（gitea、celery 的部分测试）：`useradd -m -u 1000 ci`，把 GOMODCACHE / GOCACHE 移到 `/opt` 并 chown，冒烟 `su -p ci -c '…'`。

## 5. 测试范围与预算（3600 s）

- 全量 > 1 h 的仓库：`native` 只跑与题目相关的子包，`emulated` 只编译。
- 内存：Docker VM 8 GB 左右，`-race` 与大并发容易 OOM（exit 137）→ `-p 1 -parallel 4`、去掉原生的 `-race`、pytest 按目录分两次跑（celery：`t/unit --ignore=t/unit/worker` 再 `t/unit/worker`）。
- Go 多模块 / go.work：根目录 `go test ./...` 什么都不测，要逐模块 `cd` 或 `GOWORK=off go mod download all` 后按模块跑；大套件可以构建期预编译测试二进制（etcd）。
- 带 `-tags` 的仓库照 CI 写（`fts5`、`bls12381,secp256k1eth`、`skip_*`）。
- CI 环境变量照搬：`CI=true`、`GITHUB_ACTION=1`、`BUILDKITE=true`、`TZ=America/Los_Angeles`、`TIMESCALE_FACTOR=10`——很多测试用它们放宽时限或跳过慢用例。

## 6. 模拟架构（amd64 on Apple Silicon）的抖动

- 计时敏感测试（1 s deadline、gossip 收敛、io timeout）在 Rosetta 下随机失败 → 模拟架构缩小范围，或 VM 空闲时 `--jobs 1` 重跑。
- blst 等汇编库 SIGILL → `ENV CGO_CFLAGS="-O2 -D__BLST_PORTABLE__"`。
- Go 编译器偶发 SIGSEGV → 单纯重试。
- 构建进行时不要起诊断容器：一个 4 GB 的诊断容器会把并发的模拟架构编译 OOM 掉。

## 7. 规范骨架带来的副作用

- `git remote remove origin` 是规范要求，但检查 `origin` 的测试（工作区守卫、`RemoteURLs`）会失败 → `install_block` 里 `RUN git remote add origin <上游 URL>` 并注释说明（只登记，不拉取）。
- 其它分支和 tag 被删掉：依赖 `git describe` / tag 列表的测试会变 → 跳过或注明。
- `core.hooksPath=/dev/null`：测试自己装 hook 的要注意。
- 检出必须干净：构建期生成的文件（`go generate`、前端 `dist`、`make generate-go`）要么进 `.gitignore` 范围，要么 `rm -rf`，否则 `git status --porcelain` 断言失败。

## 8. Python 特有

- 有 C 扩展的包在 arm64 上没 wheel 时（PyQt6 6.7.1 之后、pyside6）：钉有 wheel 的版本或从依赖里去掉并注明。
- `pip install --no-build-isolation --no-deps -e .` 给自带 PEP 517 后端的仓库（yarl）。
- 老仓库 + 3.12 不兼容（urllib3 1.25 的 vendored six、`collections.Mapping`）→ `python3.11 -m venv /opt/venv`，或把单个坏依赖升到最近兼容版并注明。
- pytest 插件、测试专用 extras（`[tests]`、`--extra dev --extra web`）要一起钉进去，否则 `no tests collected` / 缺 marker。
- 不要在镜像里设会被测试比较的配置环境变量（如 `SQLMESH__*`）。

## 9. 交付前自查清单

- [ ] Dockerfile 每个第三方依赖都有精确版本（`tools/pin_lint.py`）。
- [ ] 跳过 / 排除的每个测试在 Dockerfile 注释里有名字和原因。
- [ ] 两个架构 `build-results.json` 都 `ok`，镜像 ≤ 12 GB。
- [ ] 冒烟命令返回真实退出码，日志末尾能看到 `passed` / `ok` 计数。
- [ ] `_note` 写了上游 CI 命令与本配方的差异。
