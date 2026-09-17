# 交付包（zip）模式：把台账一行组织成《SWE-like Repo 交付包规范》的包

`DELIVERY=zip` 时（`pipeline.toml` 的 `[delivery] mode`），验证通过的任务不再发布成 GitHub 仓库，而是打成
`<题目名称>.zip` 上传到台账该行的「交付包（zip）」附件列；这一列非空即视为完成。命令是 `deliver`。

流水线只做数据组织：表里怎么写就怎么装，不做规范里的内容校验（rubric 条数、patch 基准、收录规则）。
唯一保留的门禁是我们自己的：Dockerfile 钉版本 lint，以及每个架构构建 + 冒烟通过、镜像不超过 `max_image_gb`。

## 包结构

```
<题目名称>/                      目录名 = 题目名称；路径非法字符 / \ : * ? " < > | 换成全角
├── task.toml                   规范第 2 节的 16 个键，值逐字来自台账
├── instruction.md              需求 Prompt（原文）
├── environment/Dockerfile      tasks/<task_id>/environment/ 的副本（生成器产出，build 验证过）
├── tests/nl_rubric.yaml        「Verify Rubric」列原文
├── solution/                   空目录（本批允许留空）
└── evidence/
    ├── model.patch             「.patch文件」附件（多个附件时第一个叫 model.patch，其余保留原名）
    ├── trajectory.<ext>        「轨迹文件链接」下载；扩展名照链接（md = Trae IDE，jsonl = TraeX，json = miniswe）
    └── screenshots/<原文件名>  「证明图片链接」下载
```

## task.toml 键 ↔ 台账列

| task.toml 键 | 台账列 | 说明 |
|---|---|---|
| `title` | 题目名称 | 原文（目录名做了字符替换，title 不替换） |
| `submitter` | 提交人 | |
| `submit_date` | 提交日期 | YYYY-MM-DD |
| `language` | 主要语言 | |
| `task_type` | 任务类型 | |
| `repo_url` | Repo URL | |
| `base_commit` | Commit/版本 | 取前 40 位 SHA（台账有时带 "(v1.2.3)" 注释，去掉） |
| `realism_and_difficulty` | 真实性与难度说明 | 多行用 `"""` |
| `modules` | 可能涉及模块 | |
| `trae_session_id` | Trae Session ID | |
| `effective_turns` | 有效轮数 | 能解析成整数就写整数，否则原样写字符串（空为 `""`） |
| `harness` | Harness | |
| `seed_model` | Seed 模型/版本 | |
| `requirement_met` | 是否完成需求 | |
| `run_result` | 产物结果 | |
| `notes` | 备注 | |

## 运行

```bash
python3 tools/swepipe.py status            # done = 交付包已上传的行
python3 tools/swepipe.py pull              # zip 模式下 task_id 自动生成（<仓库名>-<6 位 hash>），不需要补英文元数据
python3 tools/swepipe.py gen
python3 tools/swepipe.py build
python3 tools/swepipe.py deliver --dry-run # 看哪些行会打包
python3 tools/swepipe.py deliver --no-upload   # 只在 work/deliver/ 下生成 zip，自己检查
python3 tools/swepipe.py deliver           # 打包 + 上传附件；镜像过大的行只写备注标记
```

产物与状态：`<work>/deliver/<题目名称>/`（展开的包）、`<work>/deliver/<题目名称>.zip`、`<work>/downloads/<record_id>/`
（附件与链接的下载缓存）、`<work>/deliver-state.json`。

## 对接别的数据源

zip 模式比 repo 模式多要两样东西（见 `docs/source-adapters.md`）：附件描述（`TaskRecord.attachments["patch_file"]`
是一组适配器自定义的描述，`fetch_file` 负责取回；默认支持 `{"path": ...}` 和 `{"url": ...}`）以及 `attach_output(key, zip)`
把 zip 挂回该行。飞书适配器用 `file_token` 下载、`drive upload_all`（≤ 20 MB）上传。
