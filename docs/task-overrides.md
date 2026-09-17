# task-overrides.json：每个任务的人工配方

生成器能自动处理"标准"仓库（有锁文件或 pyproject 的 Python 包、普通 Go module）。其余仓库——需要系统包、外部服务、特殊构建步骤、要缩小测试范围、要跳过离线不可运行的用例——都靠这个文件。**它是这个项目积累下来的核心知识**：三百多个任务的配方，绝大多数新任务都能在里面找到同一个仓库或同类仓库的先例。

## 键的含义

文件是一个 JSON 对象，键是台账行的 `source_id`（`pull` 写进 `records.json`；`bz` 开头的 32 位 hash，由仓库 + base commit + 标题派生，台账重排不受影响）。每个值：

| 键 | 用途 |
|---|---|
| `task_id` | 该行对应的任务目录名（zip 模式自动生成；`add_override.py` 会填）。`build` 用它匹配 `smoke`，所以**必须有** |
| `source_title` | 行标题，仅作重排保护与人读 |
| `_note` | 给人看的说明：上游 CI 怎么跑、为什么这样改、跳过了什么。不进 Dockerfile，但请写——下一个人靠它判断配方能否复用 |
| `install_block` | **整段替换** Dockerfile 的依赖安装段（模板第 3 段之后、CMD 之前）。写完整的 `RUN` / `ENV` / `COPY` 行；Go 仓库必须自己包含 `RUN go mod download`。所有第三方依赖钉死版本，跳过的测试及原因写成注释 |
| `smoke` | 冒烟命令，字符串（两个架构相同）或 `{"native": "...", "emulated": "..."}`（按架构区分）。用 `bash -c` 在 `--network none` 容器里执行，工作目录 `/app`，超时 3600 s |
| `smoke_network` | `"internal"`：冒烟容器接入 `docker network create --internal` 的网络（有网卡、无外网路由，仍是离线）。给需要非回环网卡的测试用（WebRTC/ICE、集群 advertise、绑定本机 IPv4 的 cron） |
| `language` | 覆盖台账的语言（如 `JavaScript/TypeScript` 需要指定成 `typescript`） |
| `category` | 覆盖任务类型 |
| `display_title` / `display_description` / `instruction_en` | repo 模式的英文元数据；zip 模式不需要 |

## 冒烟命令的标准写法

原生架构跑真实测试，日志写到文件，只回显失败行和结尾，返回真实退出码：

```bash
{ go test -count=1 -timeout 30m ./... -skip '^(TestNeedsDNS|TestNeedsDocker)$'; } > /tmp/gotest.log 2>&1; rc=$?; grep -E '^(FAIL|--- FAIL|panic)' /tmp/gotest.log | head -n 30; tail -n 8 /tmp/gotest.log; exit $rc
```

```bash
{ python -m pytest -q -p no:cacheprovider tests --deselect tests/test_net.py::test_dns; } > /tmp/pytest.log 2>&1; rc=$?; grep -E '^(FAILED|ERROR)' /tmp/pytest.log | head -n 30; tail -n 15 /tmp/pytest.log; exit $rc
```

- 命令里有 `&&` 串联时**必须用花括号包起来**，否则重定向只作用于最后一个命令，前面失败的输出会淹没日志。
- `cmd | tail` 这种管道的退出码是 tail 的；要真实退出码就用上面 `rc=$?` 的写法。
- 不要用 `bash -lc`：mars-base 的 `/etc/profile` 会重置 PATH，Dockerfile `ENV PATH` 加进去的 venv、`/root/go/bin` 都会消失。
- 大仓库（全量 > 1 h）：`native` 只跑与题目相关的子包，`emulated` 只编译（`go build ./... && go vet ./...`）。
- 需要服务的仓库在冒烟命令开头启动服务（或 ENTRYPOINT 脚本），例如 `redis-server --daemonize yes && …`、`pg_ctlcluster 15 main start && …`。

## 工作流

1. `pull` 之后看 `records.json` 里的新任务；对每个仓库先在本文件里搜同名仓库的先例（`grep -n '"task_id": "<repo>-' task-overrides.json`），同一仓库不同 commit 的配方通常直接复制。
2. 新仓库：读上游 CI（`.github/workflows`、`Makefile`、`tox.ini`、`noxfile.py`），列出系统包、服务、环境变量、测试命令；判断哪些测试离线 / 无 Docker / root 下跑不了（见 environment-recipes.md）。
3. 把配方写成按 task_id 键的 JSON（样例 `examples/override-spec.json`），合并进来：

   ```bash
   .venv/bin/python tools/add_override.py spec.json --work feishu-sync/work-zip
   ```

4. 重新生成并构建这些任务（`gen` 拒绝覆盖已有目录，先删）：

   ```bash
   rm -rf tasks/<task_id> && .venv/bin/python tools/swepipe.py gen --task-ids <task_id>
   .venv/bin/python tools/swepipe.py build --task-ids <task_id> --jobs 1
   ```

5. `build` 在任务**开始时**读取冒烟计划：改了 `smoke` 只影响还没开始的任务，正在跑的要等它结束后 `--rebuild`。

## apt 版本怎么查

Debian 12 的精确版本在 mars-base 里解析（两个架构的 `+b1` 后缀实测相同）：

```bash
docker run --rm --platform linux/arm64 public.ecr.aws/x8v8d7g8/mars-base@sha256:91db850db926024eed328c4bf519d54986bc10aad75302cbb074f8e9d79b4c46 \
  bash -c 'apt-get update -qq; apt-cache policy redis-server libpq-dev | grep -E "^[a-z]|Candidate"'
```

## Python 依赖怎么钉

- 有 `uv.lock` / `poetry.lock` / `requirements*.txt` 带精确版本：生成器直接用（uv 0.9 太旧，脚本自装 uv 0.12.9；poetry 1.8 读不了 lock 2.1 的用 uv 解析）。
- 没有锁文件：`uv pip compile --exclude-newer=<base commit 日期> pyproject.toml --extra dev ...`，把结果写进 `install_block`。`--exclude-newer` 只对 pypi.org 有效（镜像缺上传时间元数据）；镜像不通时设 `UV_INDEX_URL`，但带 `[tool.uv] exclude-newer` 的仓库必须走 pypi.org。
- 编译输出里缩进的 `# via` 行要过滤掉（按 strip 后的行判断）。
- `setuptools>=81` 删掉了 `pkg_resources`，老仓库（apscheduler 3.x、scrapyd、archivy）钉 `setuptools==80.9.0`。
- 钉之前的 wheel 在 3.12 上没有时用 `python3.11 -m venv /opt/venv`（mars-base 自带 3.11）。
