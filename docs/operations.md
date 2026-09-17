# 操作手册：一批新行从出现到交付

## 0. 约定

- 一张表一套目录：`--work feishu-sync/work-<表>` 与 `--tasks tasks-<表>`；`task-overrides.json` 共享。
- 命令统一用项目 venv：`P=.venv/bin/python`。
- 下面把 `FEISHU_TABLE_ID=… $P tools/swepipe.py --work … --tasks …` 简写成 `swepipe`。

## 1. 发现新行

```bash
swepipe status                # candidates > 0 说明有待处理行
swepipe pull                  # 写 records.json；输出里 new / existing / dup 一目了然
```

`existing` 表示标题与本地任务目录相同（例如被人从另一张表迁移过来），不需要重新生成，只要 build-results 里有记录就能直接 deliver。

## 2. 写配方

对 `records.json` 里每个新 task_id：

1. 在 `task-overrides.json` 搜同一仓库的先例，能复制就复制（换 task_id）。
2. 新仓库：读上游 CI，按 [environment-recipes.md](environment-recipes.md) 写 `install_block` / `smoke`，用 `tools/add_override.py` 合并。
3. 低风险（纯 Python 包、普通 Go module）可以不写配方，让生成器默认处理，失败再补。

## 3. 生成与构建

```bash
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple swepipe gen          # 全批；任一行阻断则一个都不写
swepipe gen --task-ids a,b                                                  # 分批绕开阻断行
grep -c '==' tasks-<表>/*/environment/Dockerfile | sort -t: -k2 -n | tail   # 粗看钉版本数量是否合理
swepipe build --jobs 2 > feishu-sync/work-<表>/build1.log 2>&1 &            # 后台跑
echo $! > feishu-sync/work-<表>/build1.pid
```

分批建议：先跑低风险的一批，再跑需要服务 / 大套件的一批；重跑抖动任务用 `--jobs 1`。

**等待构建结束的正确姿势**：用 PID，不要用 `pgrep -f "<命令文本>"`——等待脚本自己的命令行包含同样的文本，会匹配到自己永远不退出（这个坑曾浪费 11 小时）。

```bash
while kill -0 $(cat feishu-sync/work-<表>/build1.pid) 2>/dev/null; do sleep 60; done
```

看结果：

```bash
$P - <<'EOF2'
import json; r=json.load(open('feishu-sync/work-<表>/build-results.json'))
for t,v in r.items():
    bad={p:(x['status'],x.get('reason')) for p,x in v.items() if x['status']!='ok'}
    if bad: print(t,bad)
EOF2
```

失败日志在 `<work>/logs/<task>-<arch>.log`；失败镜像保留，可 `docker run -it --network none <prefix>/<task>:<arch> bash` 进去复现。修配方后：

```bash
rm -rf tasks-<表>/<task_id> && swepipe gen --task-ids <task_id> && swepipe build --task-ids <task_id> --jobs 1
```

## 4. 交付与复核

```bash
swepipe deliver --dry-run                    # 列出将打包的行、被扣住的行及原因
swepipe deliver                              # 打包 + 上传；每轮构建结束后都可以增量跑
$P tools/verify_attachments.py --work feishu-sync/work-<表> <task_id>,...   # 从台账重新下载复核
```

`deliver` 扣住的行及处理：

| 原因 | 含义 | 处理 |
|---|---|---|
| `rubric_untyped` | Verify Rubric 条目没标 f2p/p2p | 退回提交人补；不要猜 |
| `result_unstructured` | 产物结果只有总结、没有逐条 通过/未通过 | 退回提交人按 rubric 逐条补 |
| 标题含 `/`、`:` 等路径非法字符 | zip 顶层目录名会被替换成全角，与题目名称不一致，入库校验会拒 | 退回提交人改标题 |
| 缺轨迹 / patch 附件 | 包不完整 | 退回提交人补附件 |
| base commit 与需求不符（如指向多年前的祖先提交） | 环境能建但题目跑不通 | 退回提交人改 Commit/版本 |
| 镜像 > 12 GB | 备注前加"镜像过大，暂不上传。" | 想办法瘦身（去掉缓存、拆 extras）再 `--rebuild` |
| 某架构失败 | 留在 build-results 里 | 修配方重建 |

## 5. 返修已交付的行

台账内容改了（rubric 改写、标题改名、patch 替换、日期修正）需要重新打包：

```bash
swepipe deliver --redeliver --task-ids <task_id>      # 按 deliver-state.json 重新打包并替换附件
```

只改配方（Dockerfile）时同样先 `gen --force` / 重建，再 `--redeliver`。repo 模式对应 `push --task-ids`。

## 6. 收尾

- 汇总：交付数、复核数、扣住的行与原因，通知提交人处理。
- `docker system df` 看磁盘；通过的镜像已自动删除，失败的用 `docker rmi` 清。
- 把新学到的配方要点写进 `_note`，方便下一个人。

## 7. 时间与日期

`task.toml` 的 `submit_date` 必须与表里「提交日期」显示一致（北京时间）；适配器用 `Asia/Shanghai` 转换毫秒时间戳。跨时区部署时不要改这个。
