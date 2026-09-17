#!/usr/bin/env python3
"""cloud_agent — submit a shell-style job to the user's x86_64 "Claude Agent HTTP API" host and
wait for it.  Used when the Mac cannot verify a linux/amd64 image locally (Rosetta OOM).

  python3 tools/cloud_agent.py health
  python3 tools/cloud_agent.py submit --prompt-file P.txt [--workspace W] [--model deepseek] [--timeout-ms N]
  python3 tools/cloud_agent.py wait <task-id> [--out result.json]
  python3 tools/cloud_agent.py verify-image <task_id> [--tasks DIR] [--out result.json]
      builds tasks/<task_id>/environment/Dockerfile for linux/amd64 on the host, runs the smoke
      command from task-overrides.json (or the language default) and prints the agent's report.

Credentials: CLOUD_AGENT_URL / CLOUD_AGENT_TOKEN from the environment or swe-task-pipeline/.env.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from swepipe.build import smoke_command  # noqa: E402

ROOT = HERE.parent
TERMINAL = ("succeeded", "failed", "timeout", "canceled")


def env() -> tuple[str, str]:
    from swepipe.config import Config
    cfg = Config.load()
    cfg.require("CLOUD_AGENT_URL", "CLOUD_AGENT_TOKEN")
    return cfg.get("CLOUD_AGENT_URL").rstrip("/"), cfg.get("CLOUD_AGENT_TOKEN")


def call(path: str, payload=None, timeout: int = 60) -> dict:
    url, tok = env()
    req = urllib.request.Request(url + path, data=json.dumps(payload, ensure_ascii=False).encode() if payload else None,
                                 method="POST" if payload else "GET",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def submit(prompt: str, workspace: str | None, model: str, timeout_ms: int) -> dict:
    body = {"prompt": prompt, "model": model, "timeout_ms": timeout_ms}
    if workspace:
        body["workspace"] = workspace
    return call("/tasks", body)


def wait(task_id: str, out: Path | None = None) -> dict:
    while True:
        d = call(f"/tasks/{task_id}?wait=590", timeout=620)
        print(time.strftime("%H:%M:%S"), task_id, d.get("status"), flush=True)
        if d.get("status") in TERMINAL:
            if out:
                out.write_text(json.dumps(d, ensure_ascii=False, indent=1))
            return d


def verify_prompt(task_id: str, dockerfile: str, smoke: str) -> str:
    return f"""你在一台 x86_64 Debian 主机上，已安装 docker 和 buildx。任务：验证下面这个 Dockerfile 能否在 linux/amd64 上构建成功并通过冒烟测试。不要修改 Dockerfile 内容，不要 push 任何镜像或代码，不要做其它事情。

步骤（按顺序执行，每一步都把命令的真实输出保留下来）：
1. 在当前工作目录创建子目录 env，把下面 ```dockerfile 代码块的内容一字不差地写入 env/Dockerfile。
2. 先执行：timeout 1200 docker pull public.ecr.aws/x8v8d7g8/mars-base:latest 2>&1 | tail -n 2（这台主机上 BuildKit 解析基础镜像元数据会 DNS 超时，但 docker pull 可以；镜像已在本地时 build 才能通过 FROM）。
   然后执行：docker build --platform linux/amd64 --progress=plain -t swe-task-pipeline/{task_id}:amd64 env > build.log 2>&1; echo BUILD_EXIT=$?
   这一步可能需要几十分钟，请耐心等待完成，不要中途放弃或改用别的方式。
3. 如果 BUILD_EXIT 不是 0：执行 tail -n 80 build.log 并把输出原样贴出来，然后结束。
4. 如果 BUILD_EXIT 是 0：
   a. docker image inspect swe-task-pipeline/{task_id}:amd64 --format '{{{{.Size}}}}'
   b. docker run --rm --network none swe-task-pipeline/{task_id}:amd64 bash -c {json.dumps("set -o pipefail; " + smoke, ensure_ascii=False)}; echo SMOKE_EXIT=$?
   c. docker run --rm --network none swe-task-pipeline/{task_id}:amd64 bash -c 'git status --porcelain | head -n 5; git log --oneline -1'
   d. docker rmi -f swe-task-pipeline/{task_id}:amd64
5. 最后用下面固定格式汇报，字段值必须来自真实命令输出，不要猜测：
BUILD_EXIT=<数字>
IMAGE_SIZE_BYTES=<数字或 N/A>
SMOKE_EXIT=<数字或 N/A>
SMOKE_OUTPUT:
<4b 的输出>
GIT_CHECK:
<4c 的输出>
BUILD_LOG_TAIL:
<tail -n 40 build.log 的输出>

```dockerfile
{dockerfile}```
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    s = sub.add_parser("submit")
    s.add_argument("--prompt-file", required=True)
    s.add_argument("--workspace")
    s.add_argument("--model", default="deepseek")
    s.add_argument("--timeout-ms", type=int, default=5400000)
    w = sub.add_parser("wait")
    w.add_argument("task_id")
    w.add_argument("--out")
    v = sub.add_parser("verify-image")
    v.add_argument("task_id")
    v.add_argument("--tasks", default=str(ROOT / "tasks"))
    v.add_argument("--overrides", default=str(ROOT / "task-overrides.json"))
    v.add_argument("--model", default="deepseek")
    v.add_argument("--out")
    args = ap.parse_args(argv)
    if args.cmd == "health":
        url, _ = env()
        with urllib.request.urlopen(url + "/healthz", timeout=30) as r:
            print(r.read().decode())
        return 0
    if args.cmd == "submit":
        d = submit(Path(args.prompt_file).read_text(encoding="utf-8"), args.workspace, args.model, args.timeout_ms)
        print(json.dumps({k: d.get(k) for k in ("id", "status", "workspace", "timeout_ms")}, ensure_ascii=False))
        return 0
    if args.cmd == "wait":
        d = wait(args.task_id, Path(args.out) if args.out else None)
        print(d.get("result") or d.get("error") or "")
        return 0 if d.get("status") == "succeeded" else 1
    task_dir = Path(args.tasks) / args.task_id
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text(encoding="utf-8")
    smoke = smoke_command(task_dir, Path(args.overrides))
    d = submit(verify_prompt(args.task_id, dockerfile, smoke), f"swepipe-verify-{args.task_id}"[:64], args.model, 5400000)
    print("submitted", d.get("id"), d.get("status"))
    d = wait(d["id"], Path(args.out) if args.out else None)
    print(d.get("result") or d.get("error") or "")
    return 0 if d.get("status") == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
