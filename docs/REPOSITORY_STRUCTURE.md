# 仓库结构（2026-07-27 重构后，当前实际结构）

> 本文件描述的目录结构已经落地——文件已按此结构搬动、import 路径已同步修正，`pytest
> tests/test_chunk_docs.py tests/test_router_loop_limit.py`（离线可跑的用例）全部通过，
> 所有运行时/建仓模块也逐个做过 `import` 级别的验证。**这次重构没有做任何需要真实连接
> Databricks 的验证**（不涉及行为变更，纯粹是文件搬家 + import 路径修正，详见本文件末尾
> "本次重构的验证范围"一节）。
>
> 结构取舍的详细论证过程（哪些问题成立、哪些不成立、为什么这样分）见对话记录，代码层面的
> 具体问题清单（这次重构*没有*顺带修的遗留问题）见 `CODE_REVIEW_FINDINGS.md`。

```
SalesDuo/
├── chat.py                         # 本地交互式 CLI，手动调试 agent，不打包进部署产物
├── requirements.txt                 # -r requirements-runtime.txt + ops/ 专用依赖（databricks-connect、
│                                     # azure-core/azure-storage-file-datalake、python-docx、pytest）
├── requirements-runtime.txt          # 只列 src/ 运行时代码实际 import 的包，deploy_model.py 用这份
├── databricks.yml                   # Asset Bundle 配置（app 资源定义）
├── .env / .env.example
├── .gitignore
│
├── docs/                            # 非自动加载类文档统一收纳
│   ├── CLAUDE_v1.md                 # 历史版本，供对比差异
│   ├── DEVELOPMENT_JOURNAL.md       # 开发复盘/踩坑记录（含 Part 5：从已删除的 CLAUDE.md 迁移的内容）
│   ├── CODE_REVIEW_FINDINGS.md      # 代码评审发现的问题清单（待修复/延后）
│   └── REPOSITORY_STRUCTURE.md      # 本文件
│
├── documents_generated/             # 只读原始文档（非结构化数据源，两份 docx）
│
├── src/                             # 【运行时】mlflow log_model 的 code_paths 边界，只放会被部署的代码
│   ├── config.py                    # 全项目唯一的环境变量读取入口
│   ├── db_client.py                 # Databricks SDK 认证唯一入口（get_workspace_client()）
│   ├── agent.py                     # MLflow ResponsesAgent 包装：predict / predict_stream
│   │
│   ├── graph/                       # LangGraph 编排：节点实现 + 编排骨架同层
│   │   ├── state.py                 # AgentState 定义（含累加式 trace 字段）
│   │   ├── build_graph.py           # 组装 StateGraph：router 循环边 + 四个节点
│   │   ├── router.py                # router 节点：强制结构化输出判断 next_step
│   │   ├── structured_agent.py      # 节点：调 Genie（结构化数据 + UC Function 业务规则）
│   │   ├── unstructured_agent.py    # 节点：调 Vector Search（文档片段检索）
│   │   └── finalize.py              # 节点：综合中间结果生成最终回答，唯一连到 END
│   │
│   └── clients/                     # 节点调用的外部服务底层客户端（基础设施层，不含业务逻辑）
│       ├── llm.py                   # 编排用 LLM 客户端（ChatDatabricks）
│       ├── genie_client.py          # Genie 会话调用（手动轮询，失败原因不裸抛）
│       └── retriever.py             # Vector Search top-k 检索轻量封装
│
├── ops/                              # 【非运行时】一次性建仓/部署脚本，人工/CI 触发，不参与 serving
│   ├── rag/                          # 非结构化数据 pipeline
│   │   ├── chunk_docs.py             # docx 解析 + 切块（纯本地逻辑，不连 Databricks）
│   │   ├── ingest_docs.py            # 上传原文 → 写 Delta 表 → 建 Vector Search 索引
│   │   └── setup_vs_endpoint.py      # Vector Search endpoint 建置（从 ingest_docs.py 拆出）
│   │
│   ├── structured/                    # 结构化数据 / Genie pipeline
│   │   ├── setup_uc_functions.py      # 建 UC SQL Function（信用条款计算、大额交易合规）
│   │   ├── sql/
│   │   │   ├── calculate_credit_terms.sql
│   │   │   └── check_large_transaction_compliance.sql
│   │   └── setup_genie.py             # 配置 Genie Space 数据源表 + Instructions 文本
│   │
│   ├── sql_utils.py                   # rag/ 和 structured/ 都会反向 import：提交 SQL 到
│   │                                   # Warehouse 并轮询结果，放在两者共同的父级
│   ├── verify_connection.py           # 跟哪条 pipeline 都无关，纯连接检查
│   └── deploy_model.py                # 部署整个 agent，跨两条 pipeline，log_model + 建 Serving Endpoint
│
├── app/                              # Databricks Apps 前端，独立轻量依赖，不共用主项目 requirements
│   ├── app.py                        # Streamlit 聊天框，调用已部署的 Serving Endpoint
│   ├── app.yaml
│   └── requirements.txt
│
└── tests/
    ├── test_chunk_docs.py            # 纯本地测试：docx 切块逻辑
    ├── test_router_loop_limit.py     # 验证 MAX_ROUTER_LOOPS 兜底（离线可跑）
    ├── test_integration_cases.py     # 三类端到端用例（需真实 workspace 凭据，否则跳过）
    └── eval/
        ├── eval_set.json             # 带 ground truth 的评测题集
        ├── run_eval.py               # 自动跑批 + LLM 裁判打分
        └── results/                  # 历次跑批结果（含完整 trace）
```

## 相对旧结构的改动一览

| 旧路径 | 新路径 | 为什么 |
|---|---|---|
| `src/tools/genie_client.py`、`src/tools/retriever.py` | `src/clients/` | 改名更准确：装的是"外部服务底层客户端"，跟 `db_client.py` 是同一类东西 |
| `src/graph/llm.py` | `src/clients/llm.py` | `get_llm()` 是纯基础设施，不是图业务逻辑，`router.py`/`finalize.py` 只是恰好要用它 |
| `src/setup/chunk_docs.py`、`src/setup/ingest_docs.py` | `ops/rag/` | 从"运行时代码目录"里彻底移出去，且按"RAG pipeline"聚在一起，一眼可见 |
| `src/setup/setup_uc_functions.py`、`src/setup/sql/`、`src/setup/setup_genie.py` | `ops/structured/` | 同上，按"结构化/Genie pipeline"聚在一起，跟 `ops/rag/` 对称 |
| `src/setup/ingest_docs.py` 里建 VS endpoint 的部分 | `ops/rag/setup_vs_endpoint.py`（新文件） | endpoint 是共享基础设施，index 才是绑定某个数据源的资产，拆开职责更清楚 |
| `src/setup/sql_utils.py`、`src/setup/verify_connection.py`、`src/setup/deploy_model.py` | `ops/` 顶层 | 跨两条 pipeline 共用，或者两条都不属于，留在父级 |
| 根目录 `CLAUDE_v1.md`、`DEVELOPMENT_JOURNAL.md`、`CODE_REVIEW_FINDINGS.md` | `docs/` | 统一收纳非自动加载类文档 |
| 根目录 `CLAUDE.md` | **已删除**（不是搬走） | 建仓任务已完成，v2 独有内容已并入 `docs/DEVELOPMENT_JOURNAL.md` Part 5 再删；`CLAUDE.md` 本身不能挪进 `docs/`——Claude Code 依赖它在项目根目录才会自动加载为项目指令 |
| （所有其他文件） | 位置不变 | `chat.py`、`app/`、`documents_generated/`、`tests/`、requirements 系列都保持/新增在根目录 |

关键约束仍然是同一条：**`src/` = 运行时边界，`deploy_model.py` 的 `code_paths` 只指向
`src/`**——这版结构下 `ops/` 和 `docs/` 都是 `src/` 的兄弟目录，天然被排除在部署产物之外，
不需要任何额外排除逻辑。

## 速查：某个需求该改哪个文件

| 需求 | 文件 |
|---|---|
| 加/改环境变量 | `src/config.py` + `.env.example` |
| 改路由判断逻辑 | `src/graph/router.py` |
| 改 Genie 调用方式（重试、conversation 复用） | `src/clients/genie_client.py`、`src/graph/structured_agent.py` |
| 改检索 top_k / embedding 语言处理 | `src/clients/retriever.py`、`src/graph/unstructured_agent.py` |
| 改业务规则计算逻辑 | `ops/structured/sql/*.sql` + 重跑 `python -m ops.structured.setup_uc_functions` |
| 改文档切块策略 | `ops/rag/chunk_docs.py`（改完重跑 `python -m ops.rag.ingest_docs` 重建索引） |
| 改部署环境变量/资源依赖 | `ops/deploy_model.py` |
| 改运行时依赖 | `requirements-runtime.txt`（改建仓/测试依赖改 `requirements.txt`） |
| 加新测试 case | `tests/test_integration_cases.py` 或 `tests/eval/eval_set.json` |

## 本次重构的验证范围

按用户要求,这次只做结构重构,不做需要真实连接 Databricks 的验证。实际做了/没做的核实工作:

- **做了**:全部 20 个运行时+建仓模块逐个 `importlib.import_module()` 验证 import 路径没有
  遗漏(`src.*` 3 个、`src.graph.*` 6 个、`src.clients.*` 3 个、`ops.*` 3 个、
  `ops.rag.*` 3 个、`ops.structured.*` 2 个);离线测试 `pytest tests/test_chunk_docs.py
  tests/test_router_loop_limit.py`(6 个用例)全部通过;`pytest tests/ --collect-only`
  确认 `test_integration_cases.py`、`tests/eval/run_eval.py` 能正常收集/import,不会在
  没有真实凭据时因为 import 错误而报错(它们本身设计为无凭据时跳过或需要手动触发)。
- **没做,需要以后手动验证**:任何需要真实调用 Genie/Vector Search/SQL Warehouse/Model
  Serving 的路径——也就是说 `ops/` 下所有脚本的 `main()` 从未被实际跑过一次,`_PROJECT_ROOT`
  这类路径计算是靠人工逐行核对目录层级得出的,不是靠脚本真的跑通反推验证的,建议下次有真实
  workspace 连接时,至少把 `ops/` 下每个脚本用 `python -m ops.xxx.yyy` 的方式跑一遍
  (尤其是 `deploy_model.py`,它计算项目根路径的方式改动最大——从 3 层 `.parent` 改成了
  2 层)。
- **`ops/rag/setup_vs_endpoint.py` + `ops/rag/ingest_docs.py`**:这是本次重构里唯一新增了
  一点代码逻辑(不是纯粹搬文件)的地方,原来的 `create_vector_search_index()` 被拆成
  `setup_vs_endpoint.py::ensure_endpoint_exists()` + `ingest_docs.py::
  create_delta_sync_index()` 两个函数,两边各自只保留了拆分前就存在的检查/创建逻辑,没有
  改动判断条件本身,但因为是新的文件边界,值得在重跑真实环境时重点看一眼(尤其是运行顺序:
  必须先跑 `setup_vs_endpoint.py` 再跑 `ingest_docs.py`,这是拆分后新引入的前置依赖)。
