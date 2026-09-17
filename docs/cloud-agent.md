# 可选：用 x86 云主机补验 amd64

Apple Silicon 上 amd64 走 Rosetta 模拟，巨型前端 monorepo 或超大 Go 编译会 OOM。`tools/cloud_agent.py` 把这类任务交给一台 x86_64 主机上的"agent HTTP API"执行，本机只负责提交任务和读报告。

## 主机侧要求

- x86_64 Linux，Docker + buildx，能拉 `public.ecr.aws/x8v8d7g8/mars-base`。
- 一个接受以下接口的 agent 服务（本项目用的是一个跑在主机上的编码 agent，模型固定，因此提示词写成逐步指令 + 固定报告格式）：

| 接口 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 |
| `POST /tasks` `{prompt, workspace?, model, timeout_ms}` | 提交任务，返回 `{id, status, …}` |
| `GET /tasks/{id}?wait=590` | 长轮询，`status ∈ queued/running/succeeded/failed/timeout/canceled`，完成时带 `result` |

鉴权：`Authorization: Bearer <CLOUD_AGENT_TOKEN>`。单任务上限 90 分钟；更长的构建要把 `docker build` 放进主机上守护的后台容器再轮询。

## 配置

`.env`：

```
CLOUD_AGENT_URL=https://agent.example.com
CLOUD_AGENT_TOKEN=...
```

## 用法

```bash
.venv/bin/python tools/cloud_agent.py health
.venv/bin/python tools/cloud_agent.py verify-image <task_id> --tasks tasks --out result.json
```

`verify-image` 把 `tasks/<task_id>/environment/Dockerfile` 与该任务的冒烟命令（overrides 或语言默认）打包成提示词：主机先 `docker pull` 基础镜像（某些主机上 BuildKit 的 FROM 元数据解析会 DNS 超时，预拉后 build 才能过），再 `docker build --platform linux/amd64`，通过后 `--network none` 跑冒烟、检查检出干净、删镜像，最后按固定格式汇报 `BUILD_EXIT / IMAGE_SIZE_BYTES / SMOKE_EXIT / SMOKE_OUTPUT / GIT_CHECK / BUILD_LOG_TAIL`。

通过后把结果手工写进 `<work>/build-results.json` 对应任务的 `linux/amd64` 项（`status: "ok"`, `size_gb`），再 `deliver`。

`submit --prompt-file` / `wait <id>` 是通用接口，可用于任何脚本化的远程操作。
