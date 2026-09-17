# 数据源适配器（接数据库 / API 时看这篇）

流水线只认一种数据结构：`swepipe.model.TaskRecord`（一行任务）。谁提供这些行、写回结果到哪里，由**数据源适配器**决定。现在有两个：

| 名称 | 类 | 用途 |
|---|---|---|
| `feishu` | `swepipe/sources/feishu.py` | 飞书多维表格（当前生产用） |
| `json` | `swepipe/sources/jsonfile.py` | 本地 JSON 文件；既是测试用，也是"别的系统先导出、跑完再导回"的最小集成方式 |

用 `--source json` / `SOURCE=json` 或 `pipeline.toml` 的 `[source] type` 切换。

## TaskRecord 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `key` | 是 | 行在数据源里的唯一 id（飞书 record_id、数据库主键、API id）。写回时用它定位 |
| `title` | 是 | 题目名称。去重依据：去掉空白、忽略大小写后比较 |
| `repo_url` | 是 | `https://github.com/<owner>/<repo>`（可带 .git） |
| `base_sha` | 是 | 40 位完整 commit |
| `language` | 是 | Python / Go / Rust / TypeScript / JavaScript / Java；`JavaScript/TypeScript` 这类含 `/` 的要在 overrides 里指定 |
| `task_type` | 是 | 功能新增 / 功能增强 / Bug 修复 / 重构/性能，或 feature_request / enhancement / bugfix |
| `prompt` | 是 | 需求原文，原样成为 `instruction.md` |
| `rubric` | 否 | Verify Rubric（缺失只会 warning） |
| `review` | 是 | 等于 `初检通过` 才会被处理 |
| `output_url` | 写回 | 流水线产物：GitHub 仓库 URL。非空即"已完成" |
| `remark` | 写回 | 初检备注。流水线会在最前面加一行 `镜像过大，暂不上传。` 表示跳过 |
| `seq` | 否 | 人看的行号，只用于日志 |
| 其余 | 否 | seed_type、submitter、submitted_at、difficulty、modules、result、result_extra、solution_commit_url、patch_file、solution_sha、done：原样带进 ledger，流水线不用 |

状态判定全部在 `model.py`：`is_candidate = review == 初检通过 且 output_url 为空`，`is_skipped = remark 以跳过标记开头`。

## 适配器接口

```python
from swepipe.sources import LedgerSource
from swepipe.model import TaskRecord

class MyDbSource(LedgerSource):
    name = "mydb"                                   # --source mydb

    def __init__(self, config):                     # config.get("MYDB_DSN") 等读取配置
        super().__init__(config)

    def fetch(self) -> list[TaskRecord]: ...        # 全表；选行和去重由流水线做
    def get(self, key) -> TaskRecord | None: ...    # 单行最新状态；不存在返回 None
    def set_output_url(self, key, url): ...         # 写回仓库 URL
    def prepend_remark(self, key, text): ...        # 在备注最前面加一行跳过标记
```

在 `swepipe/sources/__init__.py` 的 `get_source` 里注册类名即可。`fetch` 返回的顺序就是处理顺序（飞书按序号升序）。

## 飞书表格的列名要求（`feishu` 数据源）

列名映射在 `swepipe/sources/feishu.py` 的 `FIELDS`（改名时在 `ALIASES` 加别名即可，如 `序号` → `序号编码`）。适配器启动时会读取表结构，缺少以下任一列会直接拒绝运行，而不是把所有行都当成新任务：

| 列 | 用途 |
|---|---|
| `题目名称`、`Repo URL`、`Commit/版本` | 生成任务的最小输入 |
| `初检结果` | 只处理 `初检通过` |
| `SWE-like Image Repo URL` | 空 = 待处理；`publish` 把仓库地址写回这里，**换新表时必须先建好这一列（文本类型）** |
| `初检备注` | 镜像过大时在最前面写入跳过标记 |

其他列（提交人、Type、真实性与难度说明、`.patch文件` 等）缺失只会让对应字段为空；表里多出的列一律忽略。切换表格只改 `FEISHU_TABLE_ID`（`.env` 或 `pipeline.toml` 的 `[feishu] table_id`），新表和旧表之间**不会**自动按题目去重，需要时先用 `pull` 的报告核对。

## JSON 文件格式（`json` 数据源）

```json
{"records": [
  {"key": "42", "seq": "42", "title": "restic find 按文件大小过滤",
   "repo_url": "https://github.com/restic/restic", "base_sha": "ba802d42…(40 位)",
   "language": "Go", "task_type": "功能新增", "prompt": "……", "rubric": "……",
   "review": "初检通过", "remark": "", "output_url": ""}
]}
```

跑完 `publish` 后，同一个文件里对应行的 `output_url` 或 `remark` 会被更新。做 API 集成时最省事的方式：API 侧导出成这个文件 → 运行流水线 → 把 `output_url` / `remark` 读回去。

## 为什么中间还有一个 ledger.xlsx

生成器 `tools/xlsx2task.py` 早于飞书接入，输入是 xlsx。`swepipe/ledger.py` 负责把 TaskRecord 写成它认识的列（列名映射在 `model.LEDGER_COLUMN`）。以后要去掉 xlsx，只需改 `ledger.py` 和 `xlsx2task.read_ledger`。
