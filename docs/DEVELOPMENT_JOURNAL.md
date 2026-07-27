# SalesDuo 开发复盘 — Databricks Agent 技术栈学习笔记

> 写给谁：正在学习 Databricks agent 技术栈（Genie、Vector Search、LangGraph、MLflow
> ResponsesAgent、Model Serving、Databricks Apps）的你自己。
>
> **关于素材来源的诚实说明**：这个项目**不是 git 仓库**（`git log` 直接报
> `fatal: not a git repository`），所以本文档不是基于 commit 历史写的，而是基于：
> (a) 本地文件的最后修改时间（`stat` 拿到的 mtime，用来还原开发顺序）；
> (b) `tests/eval/results/` 下两次评测跑批留下的完整 trace + LLM 评分记录（这是本项目
> 唯一被结构化持久化下来的"trace"，MLflow 的 tracing 是发到 Databricks 服务端的，本地
> 没有留文件）；
> (c) 开发过程中在终端里直接跑诊断命令看到的真实报错/返回值（这些命令的完整原始输出
> 当时被记录在会话里，本文档引用时是逐字摘录，不是凭印象转述）。
> 凡是**没有留存原始记录、只能靠代码改动倒推**的地方，都会显式标注"以下是推断"，不会
> 装作是有完整证据的样子。
>
> **路径提醒**：本文档 Part 1-4 里引用的文件路径（`src/setup/`、`src/tools/` 等）是写作
> 当时的真实路径，反映的是重构前的目录结构，本文档不做追溯性修改。重构后的当前路径见
> `docs/REPOSITORY_STRUCTURE.md`。

---

## Part 1 — 项目文件地图

### 目录树 + 依赖关系

```
SalesDuo/
├── CLAUDE.md                          # 项目需求文档（本次复盘后会产出 v2）
├── .env / .env.example                # 唯一配置来源，所有脚本从这里读参数
├── documents_generated/               # 只读原始文档（2份 docx，信用/合规政策）
│
├── src/
│   ├── config.py                      # 【配置层】读 .env，全项目唯一的环境变量入口
│   ├── db_client.py                   # 【认证层】get_workspace_client()，全项目唯一的 SDK 认证入口
│   │
│   ├── setup/                         # 【建仓脚本】一次性/幂等的资源初始化，不在运行时被调用
│   │   ├── sql_utils.py               #   通用: 提交 SQL 到 warehouse 并等待结果
│   │   ├── sql/
│   │   │   ├── calculate_credit_terms.sql            #   UC SQL Function 定义（信用规则）
│   │   │   └── check_large_transaction_compliance.sql #  UC SQL Function 定义（合规规则）
│   │   ├── setup_uc_functions.py      #   建 schema + 执行上面两个 .sql 文件
│   │   ├── setup_genie.py             #   配置 Genie Space（表、instructions 文本）
│   │   ├── chunk_docs.py              #   纯本地逻辑: docx → chunk 列表（无需连 Databricks）
│   │   ├── ingest_docs.py             #   建 UC Volume/Delta 表/Vector Search index，调 chunk_docs
│   │   ├── deploy_model.py            #   注册 MLflow 模型 + 建/更新 Model Serving Endpoint
│   │   └── verify_connection.py       #   Step 1 连通性验证脚本
│   │
│   ├── tools/                         # 【工具封装层】graph 节点内部调用的外部服务客户端
│   │   ├── genie_client.py            #   ask_genie(): 封装 Genie 多轮对话调用
│   │   └── retriever.py               #   retrieve(): 封装 Vector Search 查询
│   │
│   ├── graph/                         # 【编排层】LangGraph StateGraph 定义
│   │   ├── state.py                   #   AgentState TypedDict（含白盒 trace 字段）
│   │   ├── llm.py                     #   get_llm(): 编排用的 ChatDatabricks 实例
│   │   ├── router.py                  #   router 节点：判断下一步 + 循环上限兜底
│   │   ├── structured_agent.py        #   structured_agent 节点：调 ask_genie()
│   │   ├── unstructured_agent.py      #   unstructured_agent 节点：调 retrieve()
│   │   ├── finalize.py                #   finalize 节点：综合信息生成最终回答
│   │   └── build_graph.py             #   把以上节点用条件边/循环边组装成图
│   │
│   └── agent.py                       # 【对外契约层】MLflow ResponsesAgent 包装，唯一部署入口
│
├── app/
│   ├── app.py                         # 【交付层】Streamlit 聊天框，调已部署的 Serving Endpoint
│   ├── app.yaml                       #   Databricks Apps 静态清单（启动命令+env）
│   └── requirements.txt               #   App 自己的轻量依赖（不含 mlflow/langgraph 等重依赖）
├── databricks.yml                     # Asset Bundle 配置，定义 App 资源
│
├── chat.py                             # 【本地调试工具】终端交互式聊天，直接调 build_graph()
├── tests/
│   ├── test_chunk_docs.py             #   纯离线单测
│   ├── test_router_loop_limit.py      #   纯离线单测（循环上限兜底）
│   ├── test_integration_cases.py      #   真实连 Databricks 的集成测试
│   └── eval/
│       ├── eval_set.json              #   10 道评测题 + ground truth
│       ├── run_eval.py                #   自动跑评测集 + LLM 裁判打分
│       └── results/*.json             #   每次跑批的完整结果（含白盒 trace）
└── requirements.txt                    # 项目总依赖
```

### 对照 CLAUDE.md 架构图，每个文件属于哪一层

CLAUDE.md 的架构图是四层：`MLflow ResponsesAgent` → `LangGraph StateGraph` → 四个节点
(`router`/`structured_agent`/`unstructured_agent`/`finalize`) → 各自调用的外部服务。

| 架构图里的层 | 对应文件 |
|---|---|
| 对外唯一契约 | `src/agent.py` |
| LangGraph 编排 | `src/graph/build_graph.py`, `src/graph/state.py` |
| router 节点 | `src/graph/router.py` |
| structured_agent 节点 | `src/graph/structured_agent.py` → 调 `src/tools/genie_client.py` → 调 Genie Space（间接调 `salesduo_agent_tools.calculate_credit_terms` / `check_large_transaction_compliance` 两个 UC Function） |
| unstructured_agent 节点 | `src/graph/unstructured_agent.py` → 调 `src/tools/retriever.py` → 调 Vector Search Index |
| finalize 节点 | `src/graph/finalize.py` |
| 建仓（不在架构图里，是"把架构图里的资源建出来"这一步） | `src/setup/*.py` 全部 |
| 部署（架构图外层，"怎么把整个图跑起来给别人用"） | `src/setup/deploy_model.py`、`databricks.yml`、`app/*` |

### 调用关系（谁调用谁）

```
用户请求
  └─> src/agent.py: SalesDuoResponsesAgent.predict()
        └─> src/graph/build_graph.py: build_graph().invoke()
              └─> LangGraph 内部按条件边调度:
                    router_node (src/graph/router.py)
                      └─> src/graph/llm.py: get_llm().with_structured_output(RouterDecision)
                    structured_agent_node (src/graph/structured_agent.py)
                      └─> src/tools/genie_client.py: ask_genie()
                            └─> src/db_client.py: get_workspace_client()
                            └─> Databricks Genie API（内部会调 UC Function）
                    unstructured_agent_node (src/graph/unstructured_agent.py)
                      └─> src/tools/retriever.py: retrieve()
                            └─> Databricks Vector Search API
                    finalize_node (src/graph/finalize.py)
                      └─> src/graph/llm.py: get_llm()
        全程用到的配置都来自 src/config.py（唯一 env 入口）
```

`src/setup/*.py` 里的脚本**不在这条调用链里**，它们是"建仓"脚本，只在你（开发者）手动运行
`python -m src.setup.xxx` 的时候才执行一次，跟运行时的用户请求无关。这是这个项目里最容易
搞混的一点：`setup_genie.py` 只是把 Genie Space **配置好**，真正**使用** Genie 的代码在
`genie_client.py` 里。

---

## Part 2 — 技术方法清单

按开发的实际先后顺序排列。

### 1. Databricks SDK 统一认证封装

**文件**：`src/db_client.py`

```python
@lru_cache(maxsize=1)
def get_workspace_client() -> WorkspaceClient:
    if settings.databricks_config_profile:
        return WorkspaceClient(profile=settings.databricks_config_profile)
    if settings.databricks_host and settings.databricks_token:
        return WorkspaceClient(host=settings.databricks_host, token=settings.databricks_token)
    return WorkspaceClient()
```

**为什么这么写**：`databricks-sdk` 的 `WorkspaceClient()` 支持好几种认证方式（PAT、profile、
Databricks 原生环境里的默认凭据链）。这里做了一个"优先级链"：本地开发有 `.env` 里的
host+token 就用那个；如果代码跑在 Databricks 环境里（比如 Model Serving 容器内），
`.env` 不存在，走最后那个空参数的 `WorkspaceClient()`，SDK 会自动识别当前所在的
Databricks 环境并拿到对应的凭据——这一行代码就是本项目"本地开发"和"线上部署"能共用同一份
代码的关键。`@lru_cache` 是因为整个项目里到处都要拿 client，缓存一份单例，不用每次都重新
认证一遍。

### 2. UC SQL Function 返回 STRUCT

**文件**：`src/setup/sql/calculate_credit_terms.sql`

```sql
CREATE OR REPLACE FUNCTION {catalog}.{schema}.calculate_credit_terms(
  relationship_years DOUBLE, ...
)
RETURNS STRUCT<tier: STRING, max_credit_limit_usd: DOUBLE, ...>
RETURN (
  WITH tier_calc AS (...), matrix AS (...), exceed_calc AS (...)
  SELECT STRUCT(tier, ..., required_approval)   -- 关键：用 STRUCT() 包起来
  FROM exceed_calc
);
```

**为什么这么写**：一个 SQL 标量函数的 `RETURN` 必须是**单一表达式**（对应 `RETURNS`
声明的类型），不能是一个多列 `SELECT`。业务规则需要同时返回 9 个字段（信用分级、额度上限、
审批要求……），如果直接 `SELECT col1, col2, ... FROM x`，会被 Spark SQL 判定成一个"标量子
查询返回了 9 列"而报错。解法是把这些列包进一个 `STRUCT(...)` 表达式，这样整个 SELECT 只
返回"一列"（这一列的类型是 STRUCT），才符合 `RETURN` 的要求。调用方拿到结果后可以用
`.字段名` 取值，比如 `calculate_credit_terms(...).tier`。

### 3. Genie Space 配置：读-改-写 serialized_space

**文件**：`src/setup/setup_genie.py`

```python
space = client.genie.get_space(settings.genie_space_id, include_serialized_space=True)
parsed = json.loads(space.serialized_space)
_merge_tables(parsed, table_fullnames)          # 往 parsed["data_sources"]["tables"] 里加表
_set_text_instructions(parsed, _build_instructions())  # 改 parsed["instructions"]["text_instructions"]
client.genie.update_space(space_id=..., serialized_space=json.dumps(parsed))
```

**为什么这么写**：Genie Space 的配置（挂了哪些表、Instructions 文本）在 API 层面是一整块
不透明的 JSON 字符串（`serialized_space`），没有公开的字段级 API（不能只改一个字段），只能
"整个读出来 → 在 Python dict 里改 → 整个写回去"。这个 JSON 的字段名（`data_sources.tables`、
`instructions.text_instructions`）不是查文档查到的，是通过"在 UI 里配置一次，再用
`get_space` 读出实际存的内容"反向确认的（详见 Part 3 案例 1）。

### 4. python-docx 按 XML 原始顺序解析段落和表格

**文件**：`src/setup/chunk_docs.py`

```python
def _iter_block_items(document):
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)
```

**为什么这么写**：`python-docx` 默认的 `document.paragraphs` 和 `document.tables` 是两个
**分开**的列表，会丢失"这段文字和这张表在原文里的先后顺序"这个信息（比如"第2节的表格"和
"第3节的表格"会被拍平成 `document.tables[0]`、`document.tables[1]`，看不出哪个属于哪一
节）。直接遍历 XML body 的子节点，按标签类型（`w:p`=段落，`w:tbl`=表格）动态构造对象，
才能保留真实的文档结构顺序，这样切块时才能正确地把每张表归到它所在的小节标题下面。

### 5. UC Volume 创建 + 原始文件上传

**文件**：`src/setup/ingest_docs.py`

```python
client.volumes.create(catalog_name=catalog, schema_name=schema, name=volume_name, volume_type=VolumeType.MANAGED)
client.files.upload(dest, f, overwrite=True)
```

**为什么用它**：Unity Catalog Volume 是 Databricks 里"存非结构化文件"的标准位置（对标
S3/ADLS 里的一个目录，但受 UC 权限管控）。这里只是把原始 docx 存一份档，真正用来做检索的
内容是下面第 6 步落到 Delta 表里的切块文本，不是这两个 docx 文件本身。

### 6. Vector Search Delta Sync Index

**文件**：`src/setup/ingest_docs.py` / `src/tools/retriever.py`

```python
vsc.create_delta_sync_index(
    endpoint_name=..., index_name=..., primary_key="chunk_id",
    source_table_name=settings.delta_table_docs_chunks,
    pipeline_type="TRIGGERED",
    embedding_source_column="content",
    embedding_model_endpoint_name=settings.embedding_model_endpoint,
)
...
index.similarity_search(columns=[...], query_text=query, num_results=k)
```

**为什么这么用**：Delta Sync Index 是 Vector Search 里"自动帮你做 embedding + 自动同步
Delta 表变化"的索引类型（相对的是"Direct Vector Access Index"，那种要自己算好向量再传进
去）。`pipeline_type="TRIGGERED"` 表示不是实时流式同步，而是手动调 `index.sync()` 才增量
更新一次——本项目的文档几乎不变，不需要实时同步，`TRIGGERED` 更省资源。查询时调
`similarity_search`，`query_text` 直接传原始文字，embedding 计算是 Vector Search 服务端
自动做的，不需要自己先调 embedding 模型。

### 7. LangGraph StateGraph：条件边实现循环路由

**文件**：`src/graph/build_graph.py`

```python
graph.set_entry_point("router")
graph.add_conditional_edges(
    "router", route_after_router,
    {"structured": "structured_agent", "unstructured": "unstructured_agent", "finalize": "finalize"},
)
graph.add_edge("structured_agent", "router")     # 关键：跑完再绕回 router，不是直接到下一步
graph.add_edge("unstructured_agent", "router")
graph.add_edge("finalize", END)
```

**为什么这么写**：这是把 CLAUDE.md 里"router + 循环边"这个设计落地的核心代码。
`add_conditional_edges` 的第二个参数 `route_after_router` 是一个普通 Python 函数，读
`state["next_step"]` 返回一个字符串，LangGraph 根据这个字符串去查第三个参数（字典）决定
下一个节点。**循环**靠的是 `structured_agent`/`unstructured_agent` 跑完之后都无条件地
`add_edge` 回 `router`，而不是直接连到 `finalize` 或下一个业务节点——router 每次都要重新
判断一遍"现在信息够不够"，这样同一个节点可以在一次请求里被访问任意多次（直到 `loop_count`
到上限或者 router 主动判断够了）。

### 8. LangGraph 状态累加：Annotated + operator.add

**文件**：`src/graph/state.py`

```python
class AgentState(TypedDict, total=False):
    ...
    trace: Annotated[list[TraceStep], operator.add]
```

**为什么这么写**：`AgentState` 大部分字段（比如 `structured_result`）是"覆盖式"的——每个
节点返回新值就直接替换旧值。但 `trace` 需要的是"追加式"——router 跑 5 次，我要看到 5 条
记录，不是只看到最后一次。给字段加 `Annotated[list[X], operator.add]` 类型标注后，
LangGraph 在合并某个节点的返回值到全局状态时，会对这个字段调用 `operator.add`（也就是
列表相加）而不是直接覆盖。所以每个节点只需要 `return {"trace": [这一步的一条新记录]}`，
不需要自己手动拼接历史记录，LangGraph 自动把新记录追加到已有列表后面。

### 9. Pydantic + with_structured_output 强制路由输出格式

**文件**：`src/graph/router.py`

```python
class RouterDecision(BaseModel):
    next_step: NextStep = Field(description="...")
    reason: str = Field(description="...")

llm = get_llm().with_structured_output(RouterDecision)
decision: RouterDecision = llm.invoke(messages)
```

**为什么这么用**：如果让 LLM 自由输出文字再用正则/关键词去解析"它想选哪个分支"，解析出错
的概率很高（模型可能说"我觉得应该查一下结构化数据"而不是精确说出 `structured` 这个词）。
`with_structured_output(RouterDecision)` 是 LangChain 的能力，会把 Pydantic model 转成
一个"工具定义"传给模型做强制 tool-calling，模型的输出直接被解析成 `RouterDecision` 实例，
`next_step` 字段的类型是 `Literal["structured","unstructured","finalize"]`，模型
**只能**从这三个值里选，没法输出别的东西——这就是 CLAUDE.md 要求的"router 输出必须走结构
化输出，不要解析自由文本"。

### 10. Genie conversation_id 跨节点透传实现多轮

**文件**：`src/graph/structured_agent.py` + `src/tools/genie_client.py`

```python
# structured_agent.py
answer = ask_genie(question, conversation_id=state.get("genie_conversation_id"))
update = {..., "genie_conversation_id": answer.conversation_id}

# genie_client.py
def ask_genie(question, conversation_id=None):
    if conversation_id:
        wait = client.genie.create_message(space_id=..., conversation_id=conversation_id, content=question)
    else:
        wait = client.genie.start_conversation(space_id=..., content=question)
```

**为什么这么写**：Genie 的多轮对话是靠 `conversation_id` 维护的服务端状态，第一次问问题
用 `start_conversation`（服务端分配一个新 `conversation_id`），后续同一个用户请求里如果
又要问 Genie（比如多跳场景里先查了一次，router 判断还要再查一次），必须用
`create_message` 并传入**同一个** `conversation_id`，否则 Genie 会把第二次问题当成一个
全新对话，之前问过的上下文全部丢失。这里的做法是：`ask_genie()` 返回值里带上这次用到的
`conversation_id`，`structured_agent_node` 把它写回 `AgentState`，下次这个节点再被
router 调度到时，从 state 里读出上次的 `conversation_id` 传进去——`AgentState` 本身就是
"跨节点、跨循环共享的记忆"，不需要额外的存储。

### 11. MLflow ResponsesAgent：predict 接 LangGraph invoke

**文件**：`src/agent.py`

```python
class SalesDuoResponsesAgent(ResponsesAgent):
    def __init__(self):
        self._graph = build_graph()

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        result = self._run_graph(request)          # 内部调 self._graph.invoke(initial_state)
        final_text = self._final_text(result)
        output_item = self.create_text_output_item(text=final_text, id=str(uuid.uuid4()))
        return ResponsesAgentResponse(output=[output_item], custom_outputs={"trace": result.get("trace", [])})
```

**为什么这么写**：`mlflow.pyfunc.ResponsesAgent` 是 Databricks/MLflow 定义的一套"部署给
聊天类应用用"的标准协议（对应 OpenAI 的 Responses API 格式），Databricks Apps、Model
Serving 都只认这个接口。这个类本质上是个"适配器"：外部传进来的是标准的
`ResponsesAgentRequest`（`.input` 是消息列表），内部转换成我们自己的 `AgentState` 格式去
调 `graph.invoke()`，跑完之后再把 LangGraph 的输出转换回标准的 `ResponsesAgentResponse`。
`predict_stream` 同理但返回的是一个生成器，因为图内部没法逐 token 流式产出，所以是"整个跑
完之后一次性包成一个 delta 事件 + 一个 done 事件"，伪装成流式接口，但不是逐 token 真流式。
`custom_outputs` 是协议里专门留给"额外调试信息"的字段，我们把白盒 trace 塞进去（详见 Part
3 案例 10）。

### 12. mlflow.pyfunc.log_model：models-from-code + code_paths + resources

**文件**：`src/setup/deploy_model.py`

```python
mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")
model_info = mlflow.pyfunc.log_model(
    name="agent",
    python_model=_AGENT_ENTRYPOINT,       # 指向 src/agent.py 这个文件路径
    pip_requirements=_REQUIREMENTS_FILE,
    code_paths=[str(_PROJECT_ROOT / "src")],   # 把整个 src/ 包一起打进模型
    resources=_resources(),               # 声明运行时依赖的 Genie/Vector Search/Warehouse/Function
    registered_model_name=registered_model_name,
)
```

**为什么这么用**：`python_model=<文件路径>` 是 mlflow 较新的"models from code"用法（相对
更老的"传一个 Python 对象序列化成 pickle"的方式），好处是不依赖 pickle 兼容性问题。
`code_paths` 解决"agent.py 里 `from src.xxx import yyy` 这种依赖别的本地文件的 import 在
部署环境里能不能找到"的问题（详见 Part 3 案例 7）。`resources=[...]` 是 Databricks 对
mlflow 的扩展：声明这个模型运行时会用到哪些 Databricks 资源（Genie Space、Vector Search
Index、SQL Warehouse、UC Function），部署成 Model Serving 后，Databricks 会自动帮这个
Serving Endpoint 的身份做好对应的鉴权，不需要在代码里手动传 token 给这些服务——虽然这个
机制目前对 Genie 底层数据访问这块还有个没解决的权限问题（详见 Part 3 案例 11）。

### 13. Model Serving Endpoint：ServedEntityInput + environment_vars

**文件**：`src/setup/deploy_model.py`

```python
served_entity = ServedEntityInput(
    entity_name=registered_model_name, entity_version=model_version,
    workload_size="Small", scale_to_zero_enabled=True,
    environment_vars=_serving_environment_vars(),   # 手动传所有非密钥配置
)
client.serving_endpoints.create_and_wait(name=endpoint_name, config=EndpointCoreConfigInput(...))
```

**为什么这么用**：`scale_to_zero_enabled=True` 让这个 endpoint 没有真实流量时不计费（对
比 Vector Search endpoint 是常驻服务，没有这个选项）。`environment_vars` 是因为被部署的
模型运行在一个隔离容器里，没有我们本地的 `.env` 文件，`src/config.py` 读到的环境变量必须
由部署方（也就是这段代码）显式传进去，服务端才知道该连哪个 Genie Space、哪个 Vector
Search Index。

### 14. Databricks Asset Bundle + Apps 两段式部署

**文件**：`databricks.yml` + 部署时用的 CLI 命令

```bash
databricks bundle validate     # 只读校验配置对不对
databricks bundle deploy       # 上传代码 + 创建/更新资源定义，但不会启动 App
databricks bundle run salesduo_agent   # 真正把代码部署到跑着的 compute 上并启动
```

**为什么是两步**：这是 Databricks Asset Bundle 对"Apps"和"Jobs"这类资源的通用模式——
`deploy` 只是把本地文件同步到 workspace 并注册/更新资源的**定义**（类似 `git push` 到一个
配置仓库），真正让它跑起来（分配 compute、装依赖、启动进程）需要额外一步 `bundle run`
（对 Job 来说这一步是"触发一次运行"，对 App 来说是"启动这个 App 的常驻进程"）。第一次不知道
这个区别时，`bundle deploy` 跑完看起来"部署完成"了，但 `client.apps.get()` 查出来
`compute_status.state` 是 `STOPPED`，页面打不开——这也是本项目真实踩过的一个坑（详见 Part
3 案例 12）。

### 15. LLM-as-judge 自动评测

**文件**：`tests/eval/run_eval.py`

```python
class Grade(BaseModel):
    verdict: Literal["CORRECT", "PARTIALLY_CORRECT", "INCORRECT"]
    reasoning: str

grade_result = get_llm().with_structured_output(Grade).invoke([...问题+标准答案+评分要点+实际回答...])
```

**为什么这么用**：10 道题的标准答案是自然语言描述（比如"最高信用额度 $250,000，账期 Net
45 天"），没法用简单的字符串相等去判断 agent 的回答对不对（agent 可能措辞不同但意思对）。
用同一个 LLM（跟路由用的是同一个 `get_llm()`）作为裁判，把"标准答案 + 评分要点 + agent
实际回答"一起喂给它，让它给出三档判断，这是目前业界做"生成式回答"自动评测最常见的方法，
比字符串匹配灵活，比人工全部过一遍快。

---

## Part 3 — 问题排查案例集

### 案例 1：Genie Space 的 UI"挂载函数"功能，实际上不需要找

**现象**：一开始以为需要在 Genie Space 的 Configure 界面里找到一个"挂载 UC Function 为
工具"的入口（类似"Instructions"里应该有个"SQL Functions"标签页），但翻遍了 Instructions
（只有一个 General Instructions 文本框）和 Examples（Example Query/Filter/Measure/
Field/Join，都是要手填 SQL 或语义层定义），都没找到能直接从 Unity Catalog 选一个函数的
入口。

**排查过程**：
1. 先尝试直接调用 `client.genie.update_space(..., serialized_space=...)` 写入猜测的字段
   名（`instructions.sql_functions`，用了从一份 GitHub 上的 Databricks 方案文档里看到的
   字段名），返回一个奇怪的报错：`Certified answer 'xxx' does not exist`。
2. 换了很多种参数格式重试（字符串数组、带 id 的对象数组、排序……），全部失败，且失败信息
   都指向"certified answer"这个概念，跟"函数"看起来不是一回事。
3. 用一个诊断脚本，先 `get_space(include_serialized_space=True)` 把当前配置读出来打印，
   发现顶层只有 `version` 和 `data_sources` 两个字段，根本没有 `instructions` 字段——说明
   一开始的猜测字段名从未生效过。
4. 请用户在 Genie UI 里手动填一段 General Instructions 文本并保存，然后重新
   `get_space` 读取，这次看到了真实的 JSON 结构：
   `instructions.text_instructions` 是一个列表，元素形如 `{"id": "<32位hex>", "content":
   [...]}`。
5. 直接把两个函数的**全限定名**（`catalog.schema.calculate_credit_terms`）写进这段文本
   里，Genie 自己生成 SQL 时就会用这个全限定名去调用——完全不需要那个"挂载为工具"的 UI
   功能。

**根因**：`instructions.sql_functions` 这个字段（无论是否找到正确的 JSON 结构）实际上是
Genie 的"认证答案（Certified Answer）"功能用的，跟"让 Genie 知道有这个函数可以调用"是两回
事。Genie 判断"能不能调用某个函数"，只需要这个函数的全限定名出现在它能看到的文本
（instructions）里，以及这个函数本身在 Unity Catalog 里是可执行的——不需要任何"注册"步骤。

**解决方案**：`src/setup/setup_genie.py` 里的 `_build_instructions()` 函数，把两个函数的
全限定名和**准确的返回字段名**都写进 instructions 文本：

```python
def _build_instructions() -> str:
    fn_schema = f"{settings.uc_catalog}.{settings.uc_function_schema}"
    return f"""...
- {fn_schema}.calculate_credit_terms(...)
  返回 STRUCT 字段：tier, advance_payment_min_pct, ..., required_approval
..."""
```

**验证方式**：直接问 Genie 一个需要调用该函数的问题，检查它返回的
`attachment.query.query`（实际生成执行的 SQL）里是否出现了正确的全限定函数调用——确认看
到了 `adventureworks_dataagent.salesduo_agent_tools.calculate_credit_terms(...)` 被正确
调用并返回了字段。

**对应文件**：`src/setup/setup_genie.py`

---

### 案例 2：UC Function 从 Step 2 起就从未真正创建成功过

**现象**：Genie 报 `UNRESOLVED_ROUTINE: Cannot resolve routine calculate_credit_terms`，
一开始以为是权限问题（Genie 的 search path 里没有这个 schema）。

**排查过程**：
1. 直接用自己的 token 在 SQL Warehouse 上跑
   `SELECT adventureworks_dataagent.salesduo_agent_tools.calculate_credit_terms(3.0,
   200000.0)`，结果**同样报 UNRESOLVED_ROUTINE**——说明这不是 Genie 特有的问题，而是这个
   函数压根不存在。
2. 查 `information_schema.routines` 表，`WHERE routine_schema = 'salesduo_agent_tools'`
   返回 0 行——确认函数从未真正被创建过。
3. 重新手动跑一遍 `CREATE OR REPLACE FUNCTION ...`，这次没有静默通过，而是直接报错：
   `INVALID_SUBQUERY_EXPRESSION.SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN:
   Scalar subquery must return only one column, but got 9`。

**根因**：函数体是 `RETURN (SELECT col1, col2, ..., col9 FROM x)`，一个标量 SQL 函数的
`RETURN` 只能是单一表达式，多列 `SELECT` 不满足这个要求。**更关键的是**：Databricks 的
`CREATE OR REPLACE FUNCTION` 语句在最早一次执行时，由于某种原因（可能是当时 SQL Warehouse
的状态/是否是第一次编译，具体机制没有进一步验证）没有报出这个类型错误，看起来像是"成功"
了，导致这个 bug 潜伏了很久，直到这次重新跑评测集时才通过 `information_schema` 直接查证
才发现函数根本不存在。**这一段"为什么最早一次没报错"目前没有留存当时的原始命令输出，
以下是推断**：很可能最早那次调用因为某个环境或参数差异走了不同的代码路径，或者当时确实
报错了但没有被仔细检查返回状态就继续了后续步骤。

**解决方案**：把 `RETURN` 体里的多列 `SELECT` 包一层 `STRUCT()`：

```diff
- SELECT
-   tier,
-   advance_payment_min_pct,
-   ...
-   required_approval
- FROM exceed_calc
+ SELECT STRUCT(
+   tier,
+   advance_payment_min_pct,
+   ...
+   required_approval
+ )
+ FROM exceed_calc
```

**验证方式**：改完后重新跑 `information_schema.routines` 查询，确认两行函数记录真实存在；
再直接 `SELECT` 调用函数验证返回值字段和计算结果都正确（比如
`relationship_years=3, annual_purchase_volume_usd=200000` 应该落在 Tier 3，
`requested_credit_amount_usd=800000` 超过 Tier 3 的 $250,000 上限，超限比例应为 220%，
审批要求应为 `VP_SALES_AND_CFO_SIGNOFF`——实测输出跟手算完全一致）。

**对应文件**：`src/setup/sql/calculate_credit_terms.sql`,
`src/setup/sql/check_large_transaction_compliance.sql`

---

### 案例 3：Genie 生成的 SQL 三种不同的错误写法（同一类问题反复出现）

**现象**：对同一家店（Brakes and Gears）问结构相似的多跳问题，在不同轮次里，Genie 生成的
SQL 分别在三个完全不同的地方出错。

**排查过程**：靠 Part 2 里加的白盒 `trace`，每次都能从
`trace[...]["sql_queries"]`（Genie 实际生成的 SQL 原文）和 `trace[...]["error"]`（Genie
返回的具体报错）里直接看到问题所在，不需要靠猜。

**根因（三个独立的例子，都有实际报错文本为证）**：

1. **跳过中间表直接错误 JOIN**：Genie 生成的 SQL 里，把
   `salesorderheader.customerid` 直接和 `store.businessentityid` 关联（`h.customerid =
   s.businessentityid`），跳过了中间的 `customer` 表。而正确关联路径应该是
   `store.businessentityid = customer.storeid`，`customer.customerid =
   salesorderheader.customerid`。这个错误 join 不会报 SQL 语法错误（两边都是合法的列），
   而是安静地查出 0 行/NULL，导致后续 `calculate_credit_terms` 用 NULL 的
   `annual_purchase_volume_usd` 算出了错误的"New Customer"分级（应为 Tier 3），进而把超限
   比例算成了 500%（正确答案是 20%）。

2. **`DATE_TRUNC` 和 `DATEDIFF` 的引号用法搞反**：生成的 SQL 里写了
   `DATE_TRUNC(YEAR, CURRENT_DATE)`（不带引号），报错
   `UNRESOLVED_COLUMN.WITHOUT_SUGGESTION: A column ... with name YEAR cannot be
   resolved`。实测确认 `DATE_TRUNC` 的时间单位需要**加引号**的字符串（`'YEAR'`），而
   `DATEDIFF` 反而需要**不加引号**的关键字（`DATEDIFF(YEAR, ...)`）——这是两个函数刚好
   相反的参数约定，Genie 从我们此前给它的"DATEDIFF 不要加引号"这条指引里过度泛化，错误地
   套用到了 `DATE_TRUNC` 上。

3. **把标量函数当表函数调用**：生成的 SQL 里用
   `FROM order_stats, LATERAL calculate_credit_terms(...) AS ct` 这种表函数调用语法去调
   一个标量函数，报错 `NOT_A_TABLE_FUNCTION: ... appears as a table function here, but
   the function was defined as a scalar function`。

**解决方案**：每次都是在 `setup_genie.py` 的 instructions 文本里加一条具体的、针对性的
纠正说明，而不是试图"通用地"防住所有可能的 SQL 写法错误：

```diff
+ 重要的表关联路径（不要跳过 customer 表直接把 store 和 salesorderheader 关联起来）：
+ store.businessentityid = customer.storeid
+ customer.customerid = salesorderheader.customerid
...
+ 写 SQL 时间函数注意，这两个函数的时间单位写法不一样，不要混用：
+ - DATEDIFF 的时间单位要用不加引号的关键字：DATEDIFF(YEAR, start, end)
+ - DATE_TRUNC 的时间单位要用加引号的字符串：DATE_TRUNC('YEAR', some_date)
...
+ 这是标量函数（SCALAR，不是表函数），只能出现在 SELECT 的列表达式里，不能写在
+ FROM/LATERAL 子句里当表来用，否则会报 NOT_A_TABLE_FUNCTION。
```

**验证方式**：每改一条指引，立刻用同一个真实问题重新问一遍，检查这次生成的 SQL 是否还犯
同一个错误。三个都验证过"改完这条不再犯这个特定错误"，但**没有**验证过"改完之后再也不会
犯任何新的 SQL 错误"——这一点在 Part 4 里作为已知局限列出。

**对应文件**：`src/setup/setup_genie.py`

---

### 案例 4：向量检索漏检——低信息量的元数据 chunk 把真正相关的段落挤下去

**现象**：评测集里"如果客户信用额度超限比例超过15%，需要谁签字批准？"这道题，agent 反复
检索了 5 次（耗尽了 `MAX_ROUTER_LOOPS`），每次给出的检索结果都不包含真正讲审批流程的那段
文字，最终答错（引用了文档头部的"Approved By: Chief Financial Officer & VP of Risk
Management"这行元数据，误当成了审批人）。

**排查过程**：
1. 从评测结果的 `trace` 里看 `unstructured_agent` 每一步的 `retrieved_chunks`，发现连续
   5 次返回的都是同样的 5 个 chunk，且全部标记为 `section_title: "Header"`（Policy
   ID、Effective Date、Approved By、Applicable To 这几行文档元数据）。
2. 手动调用 `retrieve(query, k=26)`（等于把全部 chunk 都取出来看排名），确认真正讲审批
   流程的那段（"3. Exception Handling and Special Approval Workflow"）虽然存在于索引里，
   但排在第 15 名，分数（0.462）比几条 Header 元数据（分数 0.48~0.50）都低。

**根因**：这几条 Header 元数据文本很短、内容通用（"Effective Date: July 1, 2026"这种），
在向量空间里似乎"什么问题都沾点边"，导致它们对几乎任何查询都能拿到一个中等偏高的相似度
分数，反而把真正需要"精确匹配"才能显著胜出的长段落挤到后面。这是短文本/通用文本在
embedding 检索里常见的一种噪音模式，跟具体用的哪个 embedding 模型关系不大。

**解决方案**：
1. 直接把这类文档头部元数据行从索引的 chunk 里剔除（`src/setup/chunk_docs.py`）：
   ```diff
     is_key_value_table = len(rows[0]) == 2
   + if is_key_value_table:
   +     # 元数据表，索引价值低、噪音大，直接跳过不索引
   +     continue
   ```
2. 剔除之后重新测，目标段落从第 15 名升到第 7 名——**仍然**没进 top-5，所以同时把检索的
   `top_k` 从 5 调到 8（`src/graph/unstructured_agent.py`）：
   ```diff
   - _TOP_K = 5
   + _TOP_K = 8
   ```

**验证方式**：重新跑同一个问题（`build_graph().invoke(...)`），确认最终回答里正确提到了
"VP of Sales"和"CFO"两个角色；再跑一遍完整评测集，这道题从 INCORRECT 变成 CORRECT。

**对应文件**：`src/setup/chunk_docs.py`, `src/graph/unstructured_agent.py`

---

### 案例 5：路由 LLM 在 reason 字段较长时可复现地生成格式错误的 JSON

**现象**：跑多跳评测题时，router 节点报错
`openai.BadRequestError: ... Model response did not respect the required format ...
Model Output: <function=RouterDecision>{"next_step": "finalize", "reason": "...)}`（注意
JSON 字符串末尾多了一个不该有的右括号 `)`）。

**排查过程**：重新跑同一个问题两次，两次都在同一个位置报同样的错——确认这不是偶发的随机
噪音，而是这个模型（`databricks-meta-llama-3-3-70b-instruct`）在 `reason` 字段内容较长/
较复杂时，被强制走 tool-calling 格式输出会不稳定地在结尾多吐一个字符。

**根因**：底层模型在强制结构化输出（tool-calling schema）下的格式遵循能力有限，跟输出内容
长度/复杂度相关，这是模型本身的生成质量问题，不是我们代码逻辑的 bug。

**解决方案**：给这一次 LLM 调用加重试 + 安全兜底，而不是试图"修好"模型的输出（修不了）：

```diff
+ _MAX_DECISION_ATTEMPTS = 3
  ...
+ for _ in range(_MAX_DECISION_ATTEMPTS):
+     try:
+         decision = llm.invoke(messages)
+         break
+     except Exception as exc:
+         last_error = exc
+ if decision is None:
+     return {..., "next_step": "finalize", "router_reason": f"路由模型连续{...}次输出格式错误，安全降级为 finalize"}
```

**验证方式**：重新跑同一个之前失败的问题，确认这次（可能是第 2、3 次重试）拿到了合法的
`RouterDecision`，流程正常往下走；即使全部重试都失败，也确认会走到"强制 finalize"分支而
不是让整个请求崩溃抛异常给用户。

**对应文件**：`src/graph/router.py`

---

### 案例 6：mlflow 把模型"注册"到了本地 SQLite，不是真的 Databricks workspace

**现象**：`deploy_model.py` 打印"已注册模型 ... version 1"，看起来成功了，但紧接着创建
Model Serving Endpoint 时报错：`ResourceDoesNotExist: Registered model ... does not
exist. It might have been deleted.`

**排查过程**：
1. 直接用 Databricks SDK 的 `client.registered_models.get(...)` 查这个模型名，也报"不
   存在"。
2. 用 `mlflow.MlflowClient().get_registered_model(...)`（先补上正确的 `databricks-uc`
   registry URI 认证）复现同样的"不存在"。
3. 检查项目目录，发现多了两个之前没有的文件/文件夹：`mlflow.db`（SQLite 数据库文件）和
   `mlruns/`——这两个是 mlflow 在**没有显式指定 tracking/registry URI** 时的本地默认存储
   位置。

**根因**：`deploy_model.py` 从来没有调用过 `mlflow.set_tracking_uri("databricks")` /
`mlflow.set_registry_uri("databricks-uc")`。在 Databricks Notebook/Job 里运行 mlflow
代码时，这两个 URI 通常会被环境自动配置好，但本项目是**从本地 IDE 直接跑这个脚本**，脱离
了那个自动配置的上下文，mlflow 于是乖乖地按自己的默认行为，把所有东西写到了本地文件里——
"注册成功"的提示是真的（相对那个假的本地 registry 而言），只是那个 registry 根本不是真正
的 Databricks workspace。

**解决方案**：

```diff
  def log_and_register_model() -> str:
+     mlflow.set_tracking_uri("databricks")
+     mlflow.set_registry_uri("databricks-uc")
      mlflow.set_experiment(settings.mlflow_experiment_path)
      ...
```
并清理掉误生成的 `mlflow.db`、`mlruns/`，加进 `.gitignore` 防止以后再被提交。

**验证方式**：重新跑脚本，这次的输出里能看到真实的 Databricks 实验 URL（形如
`https://adb-xxx.azuredatabricks.net/ml/experiments/...`），再用 mlflow client 直接查
`get_registered_model` 确认这次真的能查到。

**对应文件**：`src/setup/deploy_model.py`, `.gitignore`

---

### 案例 7：模型部署后 import 不到 `src` 包

**背景（这一条没有真实报错记录，是从代码设计上直接规避掉的，如实标注）**：在真正遇到这个
报错之前，就已经意识到 `mlflow.pyfunc.log_model(python_model="src/agent.py", ...)` 这种
"models from code"的用法，如果 `agent.py` 里有 `from src.config import settings` 这种
依赖同项目其他文件的 import，被部署到一个隔离的 Serving 容器里之后大概率会因为 import 不
到而失败——这是根据阅读 mlflow 源码（`mlflow/utils/model_utils.py` 里的
`_add_code_to_system_path` / `_validate_and_copy_code_paths`）主动推导出来的，**没有让
它先真的报错过再修**。

**排查过程（这次是"预防"，不是"排查"）**：读了 mlflow 源码确认两件事：
1. `code_paths=[X]` 会把 `X` 整个拷贝到模型 artifact 目录下的
   `code/<X的最后一级目录名>`。
2. 模型加载时，被加进 `sys.path` 的是 `code/` 这一层，**不是** `code/<X的目录名>` 那一层。

**根因（提前规避，不是事后修复）**：如果不传 `code_paths`，`src/agent.py` 被单独提取出来
跑的时候，`from src.config import settings` 会因为找不到 `src` 这个包而报
`ModuleNotFoundError`。

**解决方案**：

```python
_CODE_PATHS = [str(_PROJECT_ROOT / "src")]
...
mlflow.pyfunc.log_model(..., code_paths=_CODE_PATHS, ...)
```
因为 `_CODE_PATHS` 里放的是 `.../SalesDuo/src`（最后一级目录名正好是 `src`），拷贝后落在
`code/src/`，而 `code/` 被加进 `sys.path`，所以 `import src.config` 能在
`code/src/config.py` 找到——刚好对上。

**验证方式**：部署完成后，直接调用 Serving Endpoint 的一个简单问题（"Tier 3 客户的账期是
多少天？"），返回了正确答案而不是 import 错误——间接验证了 `src` 包在容器里能正常被
import。

**对应文件**：`src/setup/deploy_model.py`

---

### 案例 8：Azure 存储依赖链缺失

**现象**：`log_model` 在往 Unity Catalog 上传模型版本文件时报
`ModuleNotFoundError: No module named 'azure'`。装了 `azure-core` 之后重跑，又报
`ModuleNotFoundError: No module named 'azure.storage'`（更具体是
`azure.storage.filedatalake`）。

**排查过程**：直接看报错堆栈，定位到 mlflow 内部
`mlflow/utils/_unity_catalog_utils.py` 里 `get_artifact_repo_from_storage_info` 函数
根据 UC 返回的凭据类型（`azure_user_delegation_sas`）走到了
`AzureDataLakeArtifactRepository`，这个类内部会 `from azure.storage.filedatalake import
DataLakeServiceClient`——说明这个 workspace 的 Unity Catalog 底层存储是 Azure Data Lake
Storage，上传 UC 模型 artifact 这个操作本身需要这两个 Azure SDK 包，跟"部署出来的模型
运行时"需不需要没关系（运行时只是读数据，不用自己上传文件）。

**根因**：本地开发环境没预装这两个 Azure SDK 包（因为一开始的依赖清单是照 AWS/通用场景
准备的，没想到这个 workspace 是 Azure 后端）。

**解决方案**：`pip install azure-core azure-storage-file-datalake`，加进
`requirements.txt`。

**验证方式**：重新跑 `deploy_model.py`，这次能看到真实的 "Uploading artifacts: 100%"
进度条，并打印出 "Created version 'N'"。

**对应文件**：`requirements.txt`

---

### 案例 9：Serving Endpoint 需要显式传运行时环境变量

**现象**：Serving Endpoint 部署成功、状态是 `READY`，但真正调用时报错：
`缺少必需的环境变量/配置项: vector_search_index。请在 .env 中补充后重试。`

**排查过程**：这个报错信息本身就是我们自己代码（`src/config.py` 的
`settings.require(...)`）抛出来的，一眼就能看出原因：被部署的模型运行在一个全新的容器里，
没有本地这份 `.env` 文件。

**根因**：`ServedEntityInput` 没有配置 `environment_vars`，导致容器里
`os.environ` 里根本没有 `VECTOR_SEARCH_INDEX` 这些变量。

**解决方案**：

```python
def _serving_environment_vars() -> dict:
    return {
        "UC_CATALOG": settings.uc_catalog,
        ...
        "VECTOR_SEARCH_INDEX": settings.vector_search_index,
        ...
    }   # 注意：不传 DATABRICKS_HOST/TOKEN，认证走 resources 自动鉴权
```

**验证方式**：更新 endpoint 配置后重新调用，之前的报错消失，能正常走到业务逻辑。

**对应文件**：`src/setup/deploy_model.py`

---

### 案例 10：SDK 的 `serving_endpoints.query()` 解析不出自定义输出

**现象**：用 `databricks-sdk` 的 `client.serving_endpoints.query(...).as_dict()` 调用
部署好的 Endpoint，返回值只有 `{"served-model-name": "salesduo_agent-3"}`，看不到任何
实际回答内容。

**排查过程**：怀疑是 SDK 的类型化封装没有正确解析响应，改用最底层的 REST 调用
（`client.api_client.do("POST", f"/serving-endpoints/{name}/invocations", body=...)`）
直接打，这次拿到了完整的 `{"object": "response", "output": [{"type": "message",
"content": [{"type": "output_text", "text": "..."}]}]}`。

**根因**：`QueryEndpointResponse`（SDK 里 `query()` 方法的返回类型）是为**通用**
chat/completions/embeddings 类型的 serving endpoint 设计的，它的字段（`choices`、
`predictions`、`outputs`……）里没有一个能对上我们这个自定义 ResponsesAgent 返回的
`output` 字段结构，所以 `.as_dict()` 序列化的时候这部分内容直接被丢弃了。

**解决方案**：不管是诊断脚本还是 `app/app.py`，统一改成直接调原始 REST 接口，自己解析
`output` 字段：

```diff
- response = client.serving_endpoints.query(name=..., input=[...])
- return _extract_text(response.as_dict())
+ raw = client.api_client.do("POST", f"/serving-endpoints/{name}/invocations",
+                             body={"input": [...]})
+ return _extract_text(raw)
```

**验证方式**：改完之后同一个问题重新问一遍，能拿到正确的文本回答。

**对应文件**：`app/app.py`

---

### 案例 11（已解决，见 2026-07-27 补充）：Serving Endpoint 身份下 Genie 查表报权限错误

**现象**：本地用个人 token 跑 `build_graph().invoke(...)` 完全正常，但同一个问题通过已
部署的 Serving Endpoint 调用时，`structured_agent` 报错：

```
PERMISSION_DENIED: An error occurred accessing the schema. Failed to fetch tables for
the agent. Please resolve these errors to continue: No access to
'adventureworks_dataagent.sales.store'. To use this Genie agent, you must have SELECT
on each data asset, and at least USE CATALOG and USE SCHEMA on the containing catalog
and schema. ...（后面列了 Genie space 挂的全部 20 张表）
```

**排查过程**：
1. 给这次部署的 `agent.py` 加了 `custom_outputs={"trace": ...}`，让 Serving Endpoint
   返回的 JSON 里也能看到完整白盒 trace（不加这个，线上环境完全是黑盒，没法诊断）。
2. 从 trace 里确认：`router` 判断正确、`unstructured_agent` 正常、`structured_agent`
   每次都报上面这条一模一样的 `PERMISSION_DENIED`，一直到 `loop_count` 到上限。
3. 猜测是 Serving Endpoint 用的身份跟本地 token 不是同一个，`GRANT USE CATALOG/SCHEMA +
   SELECT ON SCHEMA sales/person TO `account users`` ——**没有解决**。
4. 查了 Genie Space 本身的 ACL（`client.permissions.get(request_object_type="genie",
   ...)`），只有我自己和 `admins` 组，于是又追加 `GRANT CAN_RUN` 给 `account users`
   ——**仍然没有解决**。
5. 检查 SQL Warehouse 权限，`users` 组已经有 `CAN_USE`，排除这个可能。
6. 查 `client.service_principals.list()`，返回空列表——没能找到 Serving Endpoint 实际
   使用的那个身份具体是谁。
7. 搜了 Databricks 官方文档相关内容，确认了"Model Serving 的系统身份需要单独被授予
   Genie 的 CAN RUN 权限，以及底层表的 UC 权限"这个大方向是对的，但没能定位到具体应该
   授权给哪个可枚举、可授权的对象。

**根因**：**目前未查明**。已确认不是"忘记 GRANT"这么简单（两类权限都试过了），大概率是
Databricks Model Serving 针对 Genie 这类资源的"自动鉴权"机制里，用了一个通过现有 API
（`permissions.get`/`service_principals.list`）找不到的内部身份，或者这个身份的授权
需要走另一个目前没试过的入口（比如 workspace 管理后台的图形界面、或者需要
`on_behalf_of_user=True` 这种更复杂的 OAuth 透传配置）。

**当前影响**：部署上线的 Databricks App，纯政策类问题（不需要查具体客户数据）能正常
回答；涉及查询 AdventureWorksLT 具体数据的问题会在 `loop_count` 耗尽后返回"信息可能不
完整"的降级回答。

**对应文件**：这个问题本身没有对应的代码修改（还没修好），相关的诊断代码在
`src/agent.py`（trace 通过 `custom_outputs` 暴露）里。

**2026-07-27 补充（问题已解决）**：当时排查方向卡在"给 `account users` 组授权"和
"找 Serving Endpoint 自己的系统身份"这两条路上，两条都走不通。真正的答案是**第三条路**：
**Databricks App 部署后会自动生成一个独立的 service principal
（`client.apps.get(app_name).service_principal_client_id`），这个 App 自己的
service principal 才是实际执行 Genie 查询时用到的身份**，不是 Serving Endpoint 自己
另外有一个身份，也不是 `account users` 这种账号级别的组。用户自己查到了这条线索，验证
方式是：先用本机个人身份跑 `chat.py` 确认结构化查询本身没问题（一直都能跑通，不是这次
新发现），再直接在部署好的 App 聊天框里测，复现权限报错——授权给这个 App service
principal 之后，同一个问题在 App 里就能正常拿到结构化数据了。

修复脚本：`ops/grant_app_permissions.py`（新增），授予 App 的 service principal：
`USE CATALOG` on `adventureworks_dataagent`、`USE SCHEMA` + `SELECT` on
`sales`/`person` 两个 schema、`USE SCHEMA` + `EXECUTE` on `salesduo_agent_tools`
schema（后者容易漏掉——Genie 生成的 SQL 会调用两个业务规则函数，函数需要的是 `EXECUTE`
权限，不是 `SELECT`，两种权限分开管，缺一个都会在不同阶段报错）。完整过程见
`docs/VERIFICATION_2026-07-27.md` 补充章节。

一个需要注意的推论：**每次 App 被删除重建，会拿到一个新的 service principal**，这份授权
需要跟着重新跑一次，不是一次性永久生效的——如果以后重建过 App 之后又复现这个权限错误，
先检查是不是忘了对新的 service principal 重新跑 `ops/grant_app_permissions.py`。

---

### 案例 12：`databricks bundle deploy` 不会自动启动 App

**现象**：`databricks bundle deploy` 命令成功返回 "Deployment complete!"，但
`databricks bundle summary` 显示 App 的 URL 是 "(not deployed)"；用 SDK 查
`client.apps.get("salesduo-agent")`，`compute_status.state` 是 `STOPPED`。

**排查过程**：查了 `databricks bundle run --help`，发现这个命令的说明是"Run the job,
pipeline or app identified by KEY"——意识到 Asset Bundle 对 apps/jobs 这类资源是
"deploy 定义 + run 启动"两段式的，`bundle deploy` 只负责前半段。

**根因**：对 Asset Bundle 的 `apps` 资源类型的部署流程理解不完整，以为 `bundle deploy`
就是终点。

**解决方案**：额外跑一次

```bash
databricks bundle run salesduo_agent
```

**验证方式**：跑完之后终端直接打印出 "App started successfully" 和真实可访问的 URL，
再用 `client.apps.get()` 确认 `app_status.state == RUNNING` 且
`compute_status.state == ACTIVE`。

**对应文件**：无代码改动，是部署操作流程本身。

---

## Part 4 — 现在还存在的已知局限

以下是这次开发中**有意识**跳过、简化、或者没有严格验证的地方，如实列出：

1. ~~**Serving Endpoint 下 Genie 权限问题未解决**（详见 Part 3 案例 11）。这是目前最大的
   功能缺口：线上环境实际上只有"非结构化"这一半是完全好用的。~~
   **2026-07-27 已解决**，见 Part 3 案例 11 的补充说明——根因是 App 自己的 service
   principal 没被授予底层表 SELECT / 业务规则函数 EXECUTE 权限，不是 Serving Endpoint
   另有一个查不到的系统身份。修复脚本：`ops/grant_app_permissions.py`。

2. **Genie 的 NL2SQL 生成本质上是非确定性的**。案例 3 里修的三个具体 SQL 错误，都是"这
   次具体遇到了、具体修了"，不是穷举了 Genie 可能犯的所有错误类型。换一个问题的措辞、
   换一次模型调用，理论上还可能生成新的、目前没见过的错误写法。这不是一个可以彻底"修完"
   的 bug 列表，是这个架构方式（让 LLM 自己写 SQL）天然带有的不确定性。

3. **`top_k=8`、chunk 过滤规则是针对这两份具体文档、具体几道测试题调出来的经验值**，
   不是系统性搜过一组参数网格（比如 top_k 分别试 5/8/10/15，看哪个整体效果最好）后选出
   来的最优值。换一批文档/问题，这个值不一定还是最优的。

4. **评测集只有 10 道题**，覆盖面很小，两次跑批之间同一道题的判定结果都出现过波动（比如
   `multi_hop_2`、`multi_hop_6` 在两次跑批里分数不一样），这本身就说明 10 题的样本量不
   足以得出稳定的"这个系统整体正确率是多少"的结论，只能定性地说"核心链路能跑通，还有已知
   问题"。

5. **`MAX_ROUTER_LOOPS=5`、`EMBEDDING_MODEL_ENDPOINT=databricks-gte-large-en`、
   `LLM_SERVING_ENDPOINT=databricks-meta-llama-3-3-70b-instruct` 这些都是 CLAUDE.md
   v1 允许"先用行业常见默认值"选的**，没有针对这个具体场景做过 A/B 测试或者调参。

6. **LLM 裁判打分本身也可能不稳定**（案例 5 提到的"模型强制结构化输出时格式偶发出错"这个
   问题，评分用的也是同一个 `get_llm()`，理论上评分本身也可能受到同样的模型质量波动
   影响，只是评测脚本目前没有对评分结果做重试）。

7. **没有做任何内容安全/PII 脱敏/prompt injection 防护**——这是 CLAUDE.md v1 里明确说
   "现在不做，以后需要再加"的范围，不是这次疏漏，但延续到这次复盘里如实说明现状。

8. **`chat.py` 维护多轮对话历史（`messages` 列表）没有做长度/token 限制**，理论上如果
   一次会话问很多轮，`messages` 会无限增长，直到某次调用因为 prompt 太长而失败——这个
   场景没有被测试到。

9. **`app.py` 除了 Databricks 自带的 SSO 登录之外，没有额外的输入校验**（比如没有限制
   单次输入长度、没有做速率限制）。

10. **本项目全程没有初始化 git**，所以严格意义上"开发时间线"是靠文件 mtime 和会话记录
    倒推出来的，不是 commit 历史——如果以后要接手这个项目做进一步开发，建议先补一个初始
    commit，后续变更走正常的 git 工作流，不要再依赖 mtime 排查问题。

---

## Part 5 — 从 CLAUDE.md 迁移的补充记录

`CLAUDE.md`（原建仓指示文档 v2）在 2026-07-27 被删除——它的任务（指导本项目从零建仓）已经
完成，`CLAUDE_v1.md` 作为历史版本保留供对比，但 v1/v2 内容并不相同，v2 独有的两条内容当时
没有被本文档收录，删除前迁移过来，避免丢失：

### 已知风险点核对表（原 CLAUDE.md 第 7 节，逐条标注本文档对应出处）

1. UC Function 作为 agent 工具执行需要 serverless generic compute（不是 SQL Warehouse），
   未开启会报权限错误——**本项目实际用的是 SQL Function，执行走 SQL Warehouse，没有触发
   这条风险**；如果以后改成 Python UC Function 实现规则计算，需要单独确认这项是否开启。
2. Genie 多轮对话必须复用 `conversation_id`——见 [Part 2 技术方法 10](#10-genie-conversation_id-跨节点透传实现多轮)。
3. Router 存在判断错误导致无限循环的风险，`MAX_ROUTER_LOOPS` 必须落地并测试触发路径——
   由 `tests/test_router_loop_limit.py` 覆盖（离线可跑，构造 `loop_count` 已达上限的 state，
   验证 router 强制走 `finalize` 且不报错）。
4. Vector Search 的 Delta Sync Index 依赖源 Delta 表，不能直接对原始 docx 建索引——见
   [Part 2 技术方法 6](#6-vector-search-delta-sync-index)。
5. Genie Space 的 `serialized_space` 配置不透明，没有字段级 API 文档——见
   [Part 3 案例 1](#案例-1genie-space-的-ui挂载函数功能实际上不需要找)。
6. UC SQL Function 的 `CREATE OR REPLACE FUNCTION` 不校验 `RETURN` 类型是否匹配
   `RETURNS` 声明——见 [Part 3 案例 2](#案例-2uc-function-从-step-2-起就从未真正创建成功过)。
7. Genie 的 NL2SQL 生成本质非确定性——见 [Part 3 案例 3](#案例-3genie-生成的-sql-三种不同的错误写法同一类问题反复出现)
   和 [Part 4](#part-4--现在还存在的已知局限) 第 2 条。
8. **本地跑 `databricks` CLI 命令（`bundle validate`/`bundle deploy`）时，CLI 不会读取
   项目的 `.env` 文件**——需要在当前 shell 里单独 `export DATABRICKS_HOST`/
   `DATABRICKS_TOKEN`，或者配置 `~/.databrickscfg` profile。（这一条此前没有被任何具体
   案例记录下来，是这次迁移唯一补充的"新"内容——踩坑发生在本地跑 `databricks bundle`
   相关命令时，当时判断问题明显、修复只是一行 `export`，没有单独写成案例。）
9. mlflow 从本地环境注册模型默认写到本地 SQLite，不是真正的 Databricks workspace——见
   [Part 3 案例 6](#案例-6mlflow-把模型注册到了本地-sqlite不是真的-databricks-workspace)。
10. Model Serving Endpoint 运行时调用 Genie 查询底层 UC 表可能遇到权限错误，即使本地个人
    token 完全正常——见 [Part 3 案例 11（未解决）](#案例-11未解决serving-endpoint-身份下-genie-查表报权限错误)。
