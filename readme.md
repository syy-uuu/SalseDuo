# SalesDuo

这个仓库是一个根据工作经验、利用开源 AdventureWorks 数据模拟真实业务场景，而做的"结构化+非结构化混合检索、动态多跳 Agent"的工程参考实现/练习项目。

**一句话总结项目的特点**："LangGraph 路由 + Genie + Vector Search 二选一/循环"的组合实现。

**从官方的 app-templates 视角来看**：SalesDuo ≈ [`agent-langgraph`](https://github.com/databricks/app-templates/tree/main/agent-langgraph) 模板的骨架（MLflow ResponsesAgent + LangGraph + Databricks Apps 部署）+ [`streamlit-chatbot-app`](https://github.com/databricks/app-templates/tree/main/streamlit-chatbot-app) 的前端调用模式，但把模板里"注释掉、留给用户自己填"的 Genie/Vector Search/UC Function 工具集成部分真实做出来了，并且用手写的多节点路由图（而非模板默认的单节点 ReAct 循环）来实现"结构化↔非结构化"之间的动态多跳。`app-templates` 里最接近"结构化+非结构化融合"的是 Showcase 里的 `rag-chat`（纯 RAG，pgvector+Lakebase，不含 Genie/结构化查询）和 `inventory-intelligence`/`agentic-support-console`（Genie 分析面板+Lakebase CRUD，但不是 LangGraph 多跳 Agent 架构）——SalesDuo 这种组合在官方模板里没有直接对应物。

特别地，作为开发的 engineer，在本次练习中，注重**白盒化的实现**，以便于对 agent 框架的理解和增加掌控力，从而知道哪些地方出了什么样的问题，积累经验，方便后续工作中的 agent 性能扩展和提升。

---

## 架构

```
                     ┌───────────────────────────────────────────┐
                     │  MLflow ResponsesAgent (predict / predict_stream)
                     │  ← 对外唯一契约，Databricks Apps 聊天框只认这个接口
                     └──────────────────┬──────────────────────────┘
                                        │
                          LangGraph StateGraph（router 节点 + 循环边）
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
         router 节点               structured_agent          unstructured_agent
   （每一步判断：继续查            （调用 Genie Space：           （调用 Vector Search：
    结构化 / 继续查非结构化         AdventureWorksLT 表 +          documents_generated/
    / 结束）                       UC Function 业务规则计算）      下的文档 chunk 检索）
              │                         │                         │
              └──── 循环回 router，直到判定信息齐全（或触发 loop 上限）──┘
                                        │
                                    finalize 节点
                              （综合所有中间结果，生成最终回答）
```

- `router` 每一步都用 Pydantic 强制结构化输出判断 `next_step`（`structured` / `unstructured` / `finalize`），不解析自由文本，避免路由结果解析出错导致状态机行为不可预测；`loop_count` 超过 `MAX_ROUTER_LOOPS` 会强制走 `finalize`，兜底防止死循环。
- `structured_agent` 和 `unstructured_agent` 是两个**完全独立、互不知情**的节点——"混合查询"这个能力是 LangGraph 的图结构给出来的，不是 Genie 或 Vector Search 任何一侧产品自带的。

### 关于"tool"的一个实现细节

结构化 agent 是包办给 Genie Space 的，这个毫无疑问；关键在于它是怎么"会算业务规则"的。做法是提前建好两个 UC SQL Function（`calculate_credit_terms`、`check_large_transaction_compliance`），当作 Genie 的"工具"用。但这里没有走 Genie Space 正式的"挂载函数为工具"功能——尝试过，`update_space` API 只要 payload 里带 `instructions.sql_functions` 字段就会整体报错（UI 能存，API 写不进去，是这个 workspace 上确认过的平台限制，不是代码 bug）。最终做法是把函数的全限定名连同返回 STRUCT 的精确字段名，直接写进 Genie 的自由文本 Instructions，Genie 生成 SQL 时按这段说明正确调用。

**这个技巧解决的是结构化数据侧的业务规则计算问题，跟非结构化文档检索无关**——Genie 在这个项目里从头到尾没有接触过 `documents_generated/` 下的文档内容。非结构化检索完全是 `unstructured_agent` 节点单独调用 Vector Search 完成的：本次实验非结构化文本量很少（两份 docx），业务逻辑也能简单提炼成 2 个函数，所以选择自建轻量 Vector Search 检索，而不是依赖 Genie 原生的文档检索能力，顺便也和"结构化+非结构化都交给 LangGraph 编排"这条路径做个对照——但这个对照原本计划做成一次正式的 A/B 对比测试，实际没有完成，代码逻辑已经实现，缺的是系统性的对比结果（见下方"开发过程回顾"）。

在整体编排上，`router` 用强制结构化输出判断 `next_step`，再由 `build_graph.py` 的条件边分派到 `structured_agent`（调 Genie）或 `unstructured_agent`（调 Vector Search），循环直到信息齐全。

---

## 仓库结构导览

```
SalesDuo/
├── src/                   【运行时】mlflow log_model 的部署边界，只放会被打进 Serving 容器的代码
│   ├── config.py          全项目唯一的环境变量读取入口
│   ├── db_client.py       Databricks SDK 认证唯一入口（get_workspace_client()）
│   ├── agent.py           MLflow ResponsesAgent 包装：predict / predict_stream
│   ├── graph/              LangGraph 编排：router / structured_agent / unstructured_agent / finalize
│   └── clients/            外部服务客户端：LLM（ChatDatabricks）/ Genie / Vector Search
│
├── ops/                    【非运行时】一次性建仓/部署脚本，人工触发，不参与 serving
│   ├── structured/          UC SQL Function 建仓 + Genie Space 数据源/Instructions 配置
│   ├── rag/                 文档解析切块 → Delta 表 → Vector Search endpoint/index
│   └── deploy_model.py     把 agent 打包成 MLflow Model，注册 UC，部署 Model Serving Endpoint
│
├── app/                    Databricks Apps 前端：Streamlit 聊天框，调用已部署的 Serving Endpoint
├── prompts/                各节点用到的 system prompt（.prompt 文件 + 统一 loader）
├── documents_generated/    非结构化数据源（两份合成的政策文档，详见下方"数据来源与免责声明"）
├── chat.py                 本地交互式 CLI，手动调试完整 LangGraph 图，不打包进部署产物
├── tests/                   单测 + 集成测试 + eval/（LLM-as-judge 评测集与结果）
└── docs/                    开发过程文档：复盘笔记、代码审查记录、真实环境验证记录、目录结构详解
```

完整的目录职责划分，以及"改什么需求该动哪个文件"速查表，见 [`docs/REPOSITORY_STRUCTURE.md`](docs/REPOSITORY_STRUCTURE.md)。

---

## 技术栈

- **编排**：[LangGraph](https://github.com/langchain-ai/langgraph)（`StateGraph` + 条件边循环）
- **Agent 契约**：MLflow `ResponsesAgent`（`predict`/`predict_stream`，兼容 OpenAI Responses API 形状）
- **结构化查询**：Databricks Genie Space（NL2SQL）+ Unity Catalog SQL Function（业务规则计算）
- **非结构化检索**：Databricks Vector Search（Delta Sync Index）+ 自建 top-k retriever
- **LLM**：`ChatDatabricks`（`databricks-langchain`），走 Databricks Foundation Model API
- **可观测性**：`mlflow.langchain.autolog()` + 自定义白盒 `trace`（透传在 `custom_outputs.trace` 里）
- **部署**：Databricks Model Serving Endpoint（承载 agent）+ Databricks Apps（Streamlit 前端），App 资源走 Asset Bundle（`databricks bundle deploy`）管理
- **认证**：Azure 原生认证（`az login` + Azure 资源坐标解析 workspace host），不使用裸 PAT
- **测试/评测**：pytest（离线单测 + 需真实凭据的集成测试）、LLM-as-judge 自动评测（`tests/eval/`）
- **运行环境**：Python 3.11

---

## 数据来源与免责声明

- **结构化数据**：Unity Catalog 里的 `AdventureWorksLT`，是 Microsoft 官方发布的公开示例数据库（销售/客户/产品等虚构业务数据），不对应任何真实公司或真实客户信息。
- **非结构化数据**（`documents_generated/` 下两份 `.docx`）：由 AI 生成的**虚构**公司信用政策/合规文档，用于模拟"公司内部政策文档"这一类非结构化数据源。文中提到的公司名、政策编号（如 `AW-FIN-POL-003`）、具体条款数值均为练习目的编造，不对应任何真实企业的真实政策，请勿作为真实业务规则参考。

---

## 如何复刻项目

1. **workspace 等级至少 Premium**（不能是免费/14 天 trial）——Model Serving 在 trial workspace 上不可用，这是本项目实测踩过的坑（trial 期间部署直接卡死，升级 Premium 后才能继续）。需要开通 Unity Catalog、Genie、Vector Search、Model Serving、Databricks Apps 这几个产品面（不同区域/合同可能不是默认全开）。需要有 Foundation Model API 可用的 LLM 和 embedding 端点（`.env.example` 里默认值是 `databricks-meta-llama-3-3-70b-instruct` 和 `databricks-gte-large-en`，换 workspace 要确认这两个端点名在你那边确实存在/可用）。

2. **认证层面**（当前是 Azure 原生认证，不是通用前提）：本机需要 Azure CLI 已装好并 `az login` 过，且这个 Azure AD 身份要对目标 workspace 有权限——代码不接受裸 PAT/token，靠 `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME` 三个 Azure 资源坐标解析出 host（[src/config.py](src/config.py)）。这意味着 workspace 必须部署在 Azure 上（AWS/GCP 上的 Databricks workspace 这套认证方式不适用，得换回 PAT 或改认证代码）。

3. **已有数据前提**（这是最容易被忽略的一条）：`UC_CATALOG=adventureworks_dataagent` 这个目录本身不是这个仓库建的，是复用一个已经存在、已经导入好 AdventureWorksLT 表的目录（`sales`/`person`/`production`/`humanresources` 等 schema）。仓库只建 `salesduo_agent_tools` 这个自己的 schema 放 UC Function/Delta 表，不会帮你把 AdventureWorksLT 样例数据集导进去——复刻前得自己先有这批结构化数据，或者改动 `ops/structured/setup_genie.py` 里挂哪些表。`documents_generated/` 下两份 docx 政策文档是仓库自带的样例非结构化数据，这个不用额外准备。

4. **权限前提**：在 `UC_CATALOG` 下建 schema/Function/Volume 的权限，建 SQL Warehouse、Genie Space、Vector Search endpoint、Model Serving Endpoint、Databricks App 的权限——基本上要求这个 Azure AD 身份在目标 workspace 里是 admin 或有对应资源的 CREATE 权限。

5. **本地开发环境**：Python 3.11 + venv/uv，装 `requirements.txt`（含 `databricks-connect`、`azure-core`、`azure-storage-file-datalake`），装好 Databricks CLI 用于 `databricks bundle deploy`。

前提都满足后，建仓/部署按依赖顺序执行（前一步的输出是后一步需要的环境变量）：

```bash
python -m ops.structured.setup_uc_functions   # 建 UC SQL Function
python -m ops.structured.setup_genie          # 配置 Genie Space（挂表 + 写 Instructions）
python -m ops.rag.setup_vs_endpoint           # 建 Vector Search endpoint
python -m ops.rag.ingest_docs                 # 文档切块 → Delta 表 → 建 index
python -m ops.deploy_model                    # 打包 agent，部署 Model Serving Endpoint
databricks bundle deploy && databricks bundle run salesduo_agent   # 部署并启动 Databricks App
```

---

## 已知问题

线上部署态（Databricks App → Model Serving Endpoint）下，Genie 查询结构化表会报 `PERMISSION_DENIED`；本地直接跑（`chat.py`，用个人 Azure 凭据）和 Genie Space 自己的原生 UI 则完全正常。

- 已排查并确认**无效**的方向：给 App 的 service principal 做 schema 级 `SELECT`/`USE SCHEMA` 授权、给 Genie Space 做 ACL 授权。（`workload_size` 调大这个改动本身是必要的，修的是另一个并发导致的 OOM 问题，跟这个权限问题无关。）
- Serving Endpoint 内部调用 Genie 时用的身份是**不可枚举的**——`service_principals.list()`、Serving Endpoint 自己的 `get_permissions()`、Genie Space 的 ACL，都找不到对应的可授权主体。理论上的修复路径（把 Genie Space 的执行模式改成"Run as owner"）在这个 workspace 的 Genie Space UI 里也不存在这个设置。
- **现状**：纯政策文档问答（只走 `unstructured_agent`）在部署态完整可用；涉及结构化数据/业务规则计算的问题，部署态会在达到 `MAX_ROUTER_LOOPS` 后强制 `finalize`，回答"信息可能不完整"。本地 `chat.py` 和 Genie 原生 UI 是目前验证 agent 完整逻辑（含结构化半边）的有效方式。
- 复刻本仓库如果目标是"部署态下跑通完整的结构化+非结构化多跳"，这个问题大概率会在你的 workspace 上重现（除非权限模型不同），建议提前预期。真正的修复方向是给 Genie 调用配一个显式的、走 client-credential 认证的专用 service principal（绕开 `resources=[...]` 这种隐式自动授权），但这需要创建新 SP 这类敏感 IAM 操作，这次练习里选择了如实记录、搁置，而不是继续排查。

---

## 评测结果

**1. 离线单测**：`tests/` 下纯逻辑测试（docx 切块、router loop 上限兜底、多跳检索 query 构造、App 错误处理不污染历史）全部离线可跑，不需要真实 Databricks 凭据：

```bash
pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py \
       tests/test_unstructured_agent_query.py tests/test_app_error_handling.py -v
```

**2. LLM-as-judge 端到端评测**（`tests/eval/`，10 道题，覆盖 `structured_only` / `unstructured_only` / `multi_hop` 三类，`multi_hop` 是本项目要验证的核心难点）：

| 跑批 | CORRECT | PARTIALLY_CORRECT | INCORRECT | ERROR |
|---|---|---|---|---|
| 第一次跑批 | 6/10 | 1 | 1 | 2 |
| 第二次跑批 | 8/10 | 0 | 2 | 0 |

第二次跑批修复了第一次暴露的具体问题（Genie 生成 SQL 的三类语法错误、router 结构化输出偶发格式错误），把 `ERROR` 从 2 降到 0。完整原始结果（含每题的模型输出、白盒 trace）见 [`tests/eval/results/`](tests/eval/results/)。

**3. 多轮记忆验证**（3 次真实调用，覆盖图内部调用和 `ResponsesAgent` 协议层两条独立代码路径）：`genie_conversation_id` 跨轮复用、代词消歧（"他们的信用额度"→正确关联到上一轮客户）3/3 次成立；1/3 次触发已知的 LLM 结构化输出偶发格式错误（router 降级为 `finalize`，靠历史数字推测而非重新计算，结果凑巧正确但方法论不可靠）。详见 [`tests/eval/results/multi_turn_memory_verification_20260728.md`](tests/eval/results/multi_turn_memory_verification_20260728.md)。

**如实说明局限**：10 题的评测集样本量小，两次跑批之间同一道题的判定都出现过波动，不足以得出"整体正确率是多少"这种统计意义上的结论，只能定性地说"核心多跳链路能跑通，且已知问题都有明确记录"。`top_k=8` 等参数是针对这两份具体文档调出的经验值，不是网格搜索出的最优解。原计划再做一次"Genie 独立处理非结构化 vs. LangGraph 编排拆分"的 A/B 对比测试，代码逻辑都已实现，但最终没有完成，这也是本项目遗留的一个明确缺口。完整已知局限清单见 [`docs/DEVELOPMENT_JOURNAL.md`](docs/DEVELOPMENT_JOURNAL.md) Part 4。

---

## 开发过程回顾

这个项目按一份自定的 7 步计划推进（认证 → 结构化数据侧 UC Function/Genie → 非结构化数据侧文档解析/Vector Search → LangGraph 编排 → 包装成 MLflow ResponsesAgent → 本地验证 → 部署），过程中踩的坑比预想的多，完整的排查案例集（12 个案例）保留在 [`docs/DEVELOPMENT_JOURNAL.md`](docs/DEVELOPMENT_JOURNAL.md) 里，这里挑几个有代表性的说：

- **"语句执行成功"不等于"资源真的建成了"**：两个 UC SQL Function 因为 `RETURN` 子句的多列 `SELECT` 没包一层 `STRUCT()`，静默失败了好几轮——`CREATE OR REPLACE FUNCTION` 语句本身报的是成功，直到直接查 `information_schema.routines` 才发现函数根本不存在。教训是校验建仓结果不能只看语句返回状态。
- **平台限制要靠实测确认，不能靠猜**：Genie Space 挂载 UC Function 为"工具"的正式 API 字段在这个 workspace 下写不进去，一开始怀疑是权限或 payload 格式问题，来回试了好几种写法才确认是平台限制，最终绕过方案是把全限定函数名直接写进 Instructions 自由文本（见"架构"一节）。
- **换一次认证方式，牵连出一个依赖不兼容**：项目中途从 PAT 认证切换到 Azure 原生认证，连带发现 Vector Search 客户端包 `databricks-ai-search` 只支持静态 token、不支持 Azure CLI 的动态刷新 token，只能整体换成 `databricks-sdk` 原生的 API。
- **本地能跑通不代表部署态能跑通**：部署到 Model Serving 之后接连暴露了 mlflow tracking URI 默认指向本地 SQLite、`code_paths` 缺失导致 import 失败、Azure 存储 SDK 依赖链缺失、`workload_size` 太小导致多进程 OOM 这几个问题——都是真实调用触发的，不是靠读文档能提前想到的。
- **留下一个没有继续深挖的问题**（见"已知问题"）：Genie 在 Serving Endpoint 身份下的权限报错，排查到"这个身份本身在 workspace 的 IAM 体系里不可枚举"这一步就停了下来，继续修需要创建专用 service principal（敏感 IAM 操作）。这次选择如实记录、不强行给出一个"已解决"的结论。
- **计划做但没完成的部分**：结构化+非结构化的组合本可以有两种实现路径——"完全交给 Genie 原生能力做"和"当前这种 LangGraph 显式编排拆分"——原本想拿这两种路径做一次正式的 A/B 对比（效果、延迟、可观测性），代码逻辑已经实现，但最终没有跑完这组对比测试，如实标注为遗留项，而不是假装做过。

比起"功能全部做完"，这次练习更看重的是每一步遇到问题时，能不能靠白盒 trace 和实测（而不是猜测或单纯看文档）把根因定位到位——这也是为什么整个架构坚持不用 no-code 的 Agent Bricks，坚持在 `AgentState.trace` 里累积每个节点的真实输入输出。

---

## License

MIT License，见 [`LICENSE`](LICENSE)。
