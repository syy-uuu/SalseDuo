# 2026-07-27 重构验证记录

> 本文档汇总两批验证工作，都跟同一次改动相关——**把认证方式从 PAT（`DATABRICKS_HOST`/
> `DATABRICKS_TOKEN`）切换成 Azure 原生认证（`az login` + Azure 资源坐标）**，以及同一次
> 提交里顺带做的目录重构（`src/setup/`→`ops/`、`src/tools/`→`src/clients/` 等，完整对照表
> 见 `docs/REPOSITORY_STRUCTURE.md`）：
>
> - **Part A**：这次改动提交（commit `9092eb3`，提交信息 "refactoring but not review"）
>   当时做的验证——只到 import 级别 + 离线 pytest，**没有连过真实 Databricks**。
> - **Part B**：本次会话（`az login` 完成之后）补做的验证——**真实连接 Databricks**，按
>   CLAUDE.md 原本的 Step 1-7 顺序过了一遍，每步都在这台机器上真实执行过，可以照着"执行
>   方式"栏原样重跑。
>
> 读这份文档时留意：Part A 验证的是"代码改完了、能不能正常导入、纯逻辑测试过不过"；
> Part B 验证的是"这些代码打到真实 workspace 上到底通不通"——两者互补，不能互相替代。
> Part B 过程中还发现并修了一个 Part A 没覆盖到的真实 bug（`databricks-ai-search` 包跟
> Azure CLI 认证不兼容），这是本文档的重点之一，在 Part B / Step 3 里详细展开。

---

## Part A：重构提交（9092eb3）时做的静态验证

### A.1 全模块 import 级验证

**验证内容**：目录重构（文件搬家）+ 认证方式改代码（`config.py`/`db_client.py` 加 Azure
字段）之后，所有运行时和建仓模块的 import 路径有没有改漏、改错。

**目的**：这次重构涉及大量 `from src.xxx import yyy` / `from src.setup.xxx import yyy`
路径改写（比如 `src.tools.genie_client` → `src.clients.genie_client`），纯靠肉眼核对容易
漏改一处；import 级验证是最低成本、能立刻发现"路径写错"这类问题的手段，不需要连接
Databricks。

**代码文件**：以下 20 个模块，逐个验证：
```
src.config
src.db_client
src.agent
src.graph.state
src.graph.build_graph
src.graph.router
src.graph.structured_agent
src.graph.unstructured_agent
src.graph.finalize
src.clients.llm
src.clients.genie_client
src.clients.retriever
ops.sql_utils
ops.verify_connection
ops.deploy_model
ops.rag.chunk_docs
ops.rag.ingest_docs
ops.rag.setup_vs_endpoint
ops.structured.setup_uc_functions
ops.structured.setup_genie
```

**执行方式**：
```bash
source .venv/bin/activate
python3 -c "
import importlib
mods = [
    'src.config', 'src.db_client', 'src.agent',
    'src.graph.state', 'src.graph.build_graph', 'src.graph.router',
    'src.graph.structured_agent', 'src.graph.unstructured_agent', 'src.graph.finalize',
    'src.clients.llm', 'src.clients.genie_client', 'src.clients.retriever',
    'ops.sql_utils', 'ops.verify_connection', 'ops.deploy_model',
    'ops.rag.chunk_docs', 'ops.rag.ingest_docs', 'ops.rag.setup_vs_endpoint',
    'ops.structured.setup_uc_functions', 'ops.structured.setup_genie',
]
for m in mods:
    importlib.import_module(m)
    print('OK', m)
"
```

**结果**：20 个模块全部 `OK`，没有 `ImportError`/`ModuleNotFoundError`。

**采取的行动**：第一次跑之前，`ops/rag/ingest_docs.py` 里 `_PROJECT_ROOT` 的路径深度算错
了一次——文件从 `src/setup/ingest_docs.py`（相对项目根 2 层子目录）搬到
`ops/rag/ingest_docs.py`（同样是 2 层子目录：`ops`、`rag`），深度其实没变，应该还是
`Path(__file__).resolve().parent.parent.parent`（3 个 `.parent`），但当时手滑改成了
`.parent.parent`（2 个），import 检查本身不会测出这个问题（`_PROJECT_ROOT` 算错只有真正
调用 `upload_raw_docs()` 用到它时才会报错，import 阶段不执行函数体）——是另外读代码复查
时发现改错了，手动改回 3 层。这说明 import 级验证只能保证"能导入"，保证不了"路径算得对"，
这也是为什么 Part B 里 `ops/rag/ingest_docs.py` 还要单独用真实调用去验证一次。

**最终结果**：✅ 通过（20/20 import 成功）。

**耗时**：几秒钟，纯本地 Python 解释器启动+导入，不涉及任何网络调用。

---

### A.2 离线 pytest

**验证内容**：两个不需要真实 Databricks 连接的测试文件，改完目录结构之后还能不能正常跑。

**目的**：
- `tests/test_chunk_docs.py` 验证的是 `ops/rag/chunk_docs.py`（原 `src/setup/chunk_docs.py`）
  的切块逻辑本身没有被搬家动作影响。
- `tests/test_router_loop_limit.py` 验证的是 `src/graph/router.py` 里
  `MAX_ROUTER_LOOPS` 循环上限这个安全阀——这条逻辑不依赖 `src.clients.llm` 真的连上
  LLM（`loop_count` 超限时在调用 LLM 之前就直接短路返回），所以可以离线跑，用来确认
  这次改动（`from src.graph.llm import get_llm` → `from src.clients.llm import get_llm`）
  没有把这条短路逻辑改坏。

**代码文件**：
- `tests/test_chunk_docs.py`（验证 `ops/rag/chunk_docs.py`）
- `tests/test_router_loop_limit.py`（验证 `src/graph/router.py`）

**执行方式**：
```bash
source .venv/bin/activate
python -m pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py -v
```

**结果**：
```
tests/test_chunk_docs.py::test_chunk_all_produces_chunks_from_both_documents PASSED
tests/test_chunk_docs.py::test_credit_tier_matrix_rows_are_chunked_individually PASSED
tests/test_chunk_docs.py::test_settlement_method_rows_are_chunked_individually PASSED
tests/test_chunk_docs.py::test_every_chunk_has_required_fields PASSED
tests/test_router_loop_limit.py::test_router_forces_finalize_when_loop_limit_reached PASSED
tests/test_router_loop_limit.py::test_router_forces_finalize_when_loop_limit_exceeded PASSED
6 passed, 1 warning in 1.7s
```

**采取的行动**：无需调整，一次通过。

**最终结果**：✅ 通过（6/6）。

**耗时**：约 1.7 秒。

**一个花絮（不算正式验证步骤，但跟这次重构直接相关，值得记一笔）**：在做完上面两步之后、
正式开始 Part B 之前，曾经不小心跑过一次 `pytest tests/`（全量，不带过滤），这条命令
额外拉起了 `tests/test_integration_cases.py`——这个文件在 `.env` 配置了真实凭据时**不会
跳过，会真的连 Databricks**，当时因为 Vector Search index 已被手动删除，触发了
`NotFound: ... salesduo_docs_index does not exist` 报错。这不是重构引入的 bug（导入、
路径都是对的，报错性质是"连上了、查了正确的资源名、发现资源不存在"，不是
`ModuleNotFoundError`），但确实是一次不小心的真实网络调用，事后用 `TaskStop` 中止了。
教训：离线验证阶段要显式点名要跑的测试文件，不要用不带过滤的 `pytest tests/`。

---

## Part B：本次会话的真实连接验证（`az login` 之后）

以下每一步都真实连接了 Databricks workspace，按 CLAUDE.md 原本的 Step 1-7 顺序执行
（CLAUDE.md 本体已删除，历史内容见 `docs/DEVELOPMENT_JOURNAL.md` Part 5）。

### Step 1：Azure CLI 认证 + 连通性

**验证内容**：新的认证方式（`az login` 会话 + `AZURE_SUBSCRIPTION_ID`/
`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME` 三个环境变量）能不能真的连上
workspace，列出 `UC_CATALOG` 下的 schema。

**目的**：这是整个认证切换改动里最基础的一环——如果连这一步都通不过，后面所有步骤都无从
谈起。对应改动点：`src/config.py` 新增的三个 `azure_*` 字段、`src/db_client.py` 里
`get_workspace_client()` 的认证优先级逻辑。

**代码文件**：
- `src/db_client.py`（`get_workspace_client()`）
- `src/config.py`（`azure_subscription_id`/`azure_resource_group_name`/
  `azure_databricks_workspace_name` 三个字段）
- `ops/verify_connection.py`（实际跑的脚本）

**执行方式**：
```bash
# 前提：本机已执行过 az login，且 .env 里填好了
# AZURE_SUBSCRIPTION_ID / RESOURCE_GROUP_NAME / DATABRICKS_WORKSPACE_NAME
source .venv/bin/activate
python -m ops.verify_connection
```

**结果**：
```
目标 catalog: adventureworks_dataagent
共找到 7 个 schema:
  - humanresources
  - information_schema
  - person
  - production
  - purchasing
  - sales
  - salesduo_agent_tools
```

**采取的行动**：无需调整，一次通过。

**最终结果**：✅ 通过。

**耗时**：几秒钟（一次 SDK 调用）。

---

### Step 2：UC Function + Genie Space 配置验证（只读）

这一步**没有重跑 `ops/structured/setup_uc_functions.py` 或 `ops/structured/setup_genie.py`
这两个建仓脚本**，而是手写了几段只读检查代码在 bash 里直接跑。原因：这两个建仓脚本都是
**有副作用的写操作**——`setup_genie.py` 会整体覆盖 Genie Space 的 `instructions`/
`tables` 配置（`serialized_space` 是整体替换语义，见该文件模块顶部注释），
`setup_uc_functions.py` 会 `CREATE OR REPLACE FUNCTION`。Step 2 的验证目标只是"新认证
方式下，之前建好的资源还能不能正常访问/调用"，不是"重新建一遍"，所以用只读查询更安全、
更能精确对应"验证新认证方式"这个目的，不会因为顺手把已经调好的 Genie instructions 覆盖掉
而引入不必要的风险。

#### 2a. UC Function 是否存在

**验证内容**：`information_schema.routines` 里还能不能查到两个 UC SQL Function。

**目的**：对应 CLAUDE.md 原 Step 2 的硬性验收标准之一——"查
`information_schema.routines` 确认函数记录真实存在"，这次是用新认证方式重新做一遍这个
检查。

**代码文件**：无对应脚本文件，是针对 `ops/structured/sql/calculate_credit_terms.sql` +
`ops/structured/sql/check_large_transaction_compliance.sql` 这两个函数的直接 SQL 查询。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
sql = """
SELECT routine_name, data_type
FROM adventureworks_dataagent.information_schema.routines
WHERE routine_schema = 'salesduo_agent_tools'
ORDER BY routine_name
"""
resp = client.statement_execution.execute_statement(
    warehouse_id=settings.sql_warehouse_id, statement=sql, wait_timeout="30s"
)
for row in resp.result.data_array:
    print(row)
EOF
```

**结果**：
```
['calculate_credit_terms', 'STRUCT']
['check_large_transaction_compliance', 'STRUCT']
```

**采取的行动**：无。

**最终结果**：✅ 通过。

#### 2b. UC Function 实际调用

**验证内容**：不只看函数"存在"，还要真的调用一次，确认返回值结构正确（对应 CLAUDE.md
Step 2 的另一条硬性验收标准："CREATE 语句返回成功不代表函数可用，必须实际调用一次
验证"）。

**目的**：同 2a，防止"函数建了但调不通"（历史上真的发生过，见
`docs/DEVELOPMENT_JOURNAL.md` 案例 2）。

**代码文件**：`ops/structured/sql/calculate_credit_terms.sql`。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
sql = """
SELECT adventureworks_dataagent.salesduo_agent_tools.calculate_credit_terms(
    3.0, 500000.0, false, 100000.0, 60
) AS result
"""
resp = client.statement_execution.execute_statement(
    warehouse_id=settings.sql_warehouse_id, statement=sql, wait_timeout="30s"
)
print(resp.result.data_array[0])
EOF
```

**结果**：
```
['{"tier":"Tier 3 Standard Account","requires_net90_escalation":"false","exceeds_credit_limit":"false","advance_payment_max_pct":"0.1","max_credit_limit_usd":"250000.0","overage_pct":"0.0","advance_payment_min_pct":"0.0","required_approval":"NONE","max_credit_term_days":"45"}']
```

**采取的行动**：无。

**最终结果**：✅ 通过，返回了结构正确的 STRUCT。

#### 2c. Genie Space 配置检查

**验证内容**：Genie Space 挂载的表数量、`text_instructions` 内容是否还完整（挂载表应该
是 20 张：19 张 `sales` schema 表 + `person.person`；instructions 文本应该包含两个 UC
Function 的全限定名）。

**目的**：对应 `ops/structured/setup_genie.py` 写入的配置——验证这份配置在新认证方式下
读出来还是完整的，没有被之前的操作意外破坏。

**代码文件**：`ops/structured/setup_genie.py`（验证它写入的配置内容还在）。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
import json
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
space = client.genie.get_space(settings.genie_space_id, include_serialized_space=True)
parsed = json.loads(space.serialized_space)
tables = parsed.get("data_sources", {}).get("tables", [])
print(f"挂载表数量: {len(tables)}")
instr = parsed.get("instructions", {}).get("text_instructions", [])
print(f"text_instructions 条数: {len(instr)}")
if instr:
    # 注意：content 是按段落拆开的字符串列表，要 join 起来再判断，不能只看 content[0]
    full_text = "".join(instr[0].get("content", []))
    print(f"完整 instructions 长度: {len(full_text)} 字符")
    print("是否包含 calculate_credit_terms 全限定名:", "calculate_credit_terms" in full_text)
EOF
```

**结果**：
```
挂载表数量: 20
text_instructions 条数: 1
完整 instructions 长度: 约 1500+ 字符（含表关联路径提示、两个函数全限定名+返回字段、时间函数用法提示）
是否包含 calculate_credit_terms 全限定名: True
```

**采取的行动**：**第一次跑这个检查时得出了错误结论**。第一版脚本写的是
`content = instr[0].get("content", [""])[0]`，只取了 `content` 列表的第一个元素
（`content` 字段的真实结构是"按段落拆开的字符串列表"，不是一整段字符串），算出的
"instructions 长度: 54 字符"，且判断 `"calculate_credit_terms" in content` 是
`False`——一度以为 Genie 的 instructions 配置丢失了。把完整的 `content` 列表 `print`
出来后发现列表里其实有 27 个元素，包含完整的表关联路径提示、两个函数的全限定名和返回
字段说明、时间函数用法提示——**这是我自己验证脚本的 bug（少 join 了一步），不是 Genie
配置真的有问题**。改成上面 `"".join(instr[0].get("content", []))` 之后拿到了正确结果。
这个教训被保留在这份文档里，是想提醒你：`serialized_space` 里 `text_instructions[].
content` 字段是一个字符串列表，读取时要 join 完整再做长度/包含判断，不要只看第一个
元素。

**最终结果**：✅ 通过（配置完整，第一次误报已自证是脚本 bug）。

**Step 2 耗时**：几秒钟（几次 SQL Warehouse 查询 + 一次 Genie API 调用），不含误报排查
的思考时间。

---

### Step 3：Vector Search 重建 + 验证（本次改动量最大的一步）

**背景**：会话过程中用户提到"24小时烧钱的 vector search 我就把它删了，要重新建"——也就是
说这一步要验证的 Vector Search endpoint/index 在开始验证前就已经不存在了，是预期状态，
不是 bug。

这一步实际做了两件事：**(1) 发现并修复了一个认证切换带来的真实兼容性问题；(2) 在修复
之后，把 endpoint 和 index 真的重建了一遍。** 下面分小节展开，`3.1`-`3.3` 是问题发现和
修复，`3.4`-`3.7` 是重建过程。

#### 3.1 复现问题：`databricks-ai-search` 包认证失败

**验证内容**：用重构前一直在用的 `databricks.ai_search.client.VectorSearchClient` 检查
endpoint/index 是否存在。

**目的**：这是 Vector Search 相关代码原本的实现方式（`src/clients/retriever.py`、
`ops/rag/setup_vs_endpoint.py`、`ops/rag/ingest_docs.py` 当时都在用这个包），先按老办法
跑一遍，确认新认证方式下这条路径通不通。

**代码文件**：改动前的 `src/clients/retriever.py`（`_get_index()` 函数）。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from databricks.ai_search.client import VectorSearchClient
from src.config import settings

vsc = VectorSearchClient(disable_notice=True)
endpoints = vsc.list_endpoints().get("endpoints", [])
EOF
```

**结果**：报错，完整堆栈里的关键信息：
```
WARNING mlflow.utils.databricks_utils: Failed to create databricks SDK workspace client,
error: ValueError('default auth: cannot configure default credentials, ...').
Falling back to legacy authentication.
...
mlflow.exceptions.MlflowException: Reading Databricks credential configuration failed
with MLflow tracking URI 'None'. ...
```

**根因排查**：`VectorSearchClient.__init__()` 内部不是走我们自己封装的
`src/db_client.py::get_workspace_client()`，而是走
`mlflow.utils.databricks_utils.get_databricks_host_creds()`——这个函数会新建一个**不带
任何参数**的 `WorkspaceClient()`。我们在 `db_client.py` 里手动拼的
`azure_workspace_resource_id` 参数只存在于我们自己封装的函数调用里，这个裸
`WorkspaceClient()` 完全不知道要用 Azure 认证，因为读的是环境变量而我们没有设置
databricks-sdk 官方认的那个环境变量名。

**采取的行动（修复 1/2）**：databricks-sdk 官方认的环境变量是 `DATABRICKS_AZURE_
RESOURCE_ID`（任何裸 `WorkspaceClient()` 都会自动读它）。在 `src/config.py` 的
`Settings` 类里加了一个 `__post_init__`，读到 `AZURE_SUBSCRIPTION_ID`/
`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME` 三个变量后，自动拼成资源 ID 字符串
写回 `os.environ["DATABRICKS_AZURE_RESOURCE_ID"]`，这样不管是我们自己的封装还是任何第三方
库创建的裸 `WorkspaceClient()`，只要在 `import src.config` 之后创建，都能读到。

**对应代码**（`src/config.py`）：
```python
def __post_init__(self) -> None:
    if (
        not os.environ.get("DATABRICKS_AZURE_RESOURCE_ID")
        and self.azure_subscription_id
        and self.azure_resource_group_name
        and self.azure_databricks_workspace_name
    ):
        os.environ["DATABRICKS_AZURE_RESOURCE_ID"] = (
            f"/subscriptions/{self.azure_subscription_id}"
            f"/resourceGroups/{self.azure_resource_group_name}"
            f"/providers/Microsoft.Databricks/workspaces/{self.azure_databricks_workspace_name}"
        )
```

#### 3.2 修复后重试，发现更深层的问题

**验证内容**：加了上面的环境变量注入之后，重跑同一段 `VectorSearchClient` 检查代码。

**结果**：警告消失了（说明裸 `WorkspaceClient()` 这次真的认证成功了），但换成了新的报错：
```
databricks.ai_search.exceptions.InvalidInputException:
Please specify either personal access token or service principal client ID and secret.
```

**根因排查**：读了 `databricks-ai-search` 包（版本 `>=0.1.0`）的 `VectorSearchClient.
validate()` 源码，发现它的校验逻辑是硬编码的：
```python
if not (
    self.personal_access_token
    or (self.service_principal_client_id and self.service_principal_client_secret)
):
    raise InvalidInputException(...)
```
**这个包本身只认两种认证方式：静态 PAT 字符串，或者 Service Principal 的 client_id+
secret。它拿到的 `token` 字段来自 `get_databricks_host_creds().token`——但 Azure CLI
认证走的是会自动刷新的临时令牌，不是一个固定字符串，`mlflow` 在纯 SDK 认证（非 legacy）
成功的情况下返回的 `MlflowHostCreds` 根本不带 `token` 字段。也就是说，不管环境变量怎么
配，这个包的这个版本从架构上就不支持 Azure CLI 这种动态令牌认证方式**——这不是配置
问题，是第三方库本身的限制。

**采取的行动（修复 2/2，决定迁移）**：`databricks-sdk` 自带
`client.vector_search_indexes` / `client.vector_search_endpoints`
（`databricks.sdk.service.vectorsearch` 模块），走跟其他所有代码完全一样的
`get_workspace_client()` 认证，不需要给 Vector Search 这一块单独处理认证。决定把三个
文件全部迁移到这套原生 API，弃用 `databricks-ai-search` 这个包。

#### 3.3 迁移到 `databricks-sdk` 原生 Vector Search API

**代码文件**（三个都改了）：
- `src/clients/retriever.py`
- `ops/rag/setup_vs_endpoint.py`
- `ops/rag/ingest_docs.py`
- `requirements-runtime.txt`（去掉 `databricks-ai-search>=0.1.0` 这一行依赖）

**具体改动**：

`src/clients/retriever.py` 的 `retrieve()`——查询从
`VectorSearchClient().get_index(...).similarity_search(...)` 改成
`client.vector_search_indexes.query_index(...)`：
```python
def retrieve(query: str, k: int = 5) -> list[RetrievedChunk]:
    settings.require("vector_search_index")
    client = get_workspace_client()
    response = client.vector_search_indexes.query_index(
        index_name=settings.vector_search_index,
        columns=_RESULT_COLUMNS,
        query_text=query,
        num_results=k,
    )
    data_array = (response.result.data_array if response.result else None) or []
    ...
```
返回值结构（`response.result.data_array`，`List[List[str]]`）跟原来
`similarity_search()` 返回的 `result["result"]["data_array"]` 形状一致，后续解析逻辑
（按列名 `zip` 成字典）基本没变。

`ops/rag/setup_vs_endpoint.py` 的 `ensure_endpoint_exists()`——从
`VectorSearchClient().create_endpoint(...)` 改成
`client.vector_search_endpoints.create_endpoint(...)`（`EndpointType.STANDARD` 换成
SDK 自己的枚举类型）。

`ops/rag/ingest_docs.py` 的 `create_delta_sync_index()`——从
`VectorSearchClient().create_delta_sync_index(...)` 改成
`client.vector_search_indexes.create_index(...)`，需要用
`DeltaSyncVectorIndexSpecRequest` + `EmbeddingSourceColumn` 这两个类型把参数包装一层
（原来的包是扁平参数，SDK 原生 API 是嵌套结构）：
```python
client.vector_search_indexes.create_index(
    name=settings.vector_search_index,
    endpoint_name=settings.vector_search_endpoint,
    primary_key="chunk_id",
    index_type=VectorIndexType.DELTA_SYNC,
    delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
        source_table=settings.delta_table_docs_chunks,
        pipeline_type=PipelineType.TRIGGERED,
        embedding_source_columns=[
            EmbeddingSourceColumn(
                name="content",
                embedding_model_endpoint_name=settings.embedding_model_endpoint,
            )
        ],
    ),
)
```

**验证**：改完之后先做了一次 import 级检查（同 Part A.1 的方式，只测这三个模块）：
```bash
python3 -c "
import importlib
for m in ['src.clients.retriever', 'ops.rag.setup_vs_endpoint', 'ops.rag.ingest_docs']:
    importlib.import_module(m); print('OK', m)
"
```
三个都 `OK`。这一步只证明了"改完的代码语法对、import 对"，真正证明"逻辑对"要看下面
3.4-3.7 的真实建仓结果。

**最终结果**：✅ 迁移完成，且后续 3.4-3.7 的真实建仓验证证明了迁移后的代码可用。

---

#### 3.4 创建 Vector Search Endpoint

**验证内容**：用迁移后的 `ops/rag/setup_vs_endpoint.py` 建 endpoint。

**目的**：验证 3.3 的迁移代码在"从零创建资源"这个场景下是对的（不只是查询场景）。

**代码文件**：`ops/rag/setup_vs_endpoint.py`。

**执行方式**（正常情况下用户应该直接跑这条）：
```bash
source .venv/bin/activate
python -m ops.rag.setup_vs_endpoint
```

**采取的行动（遇到一个 bug，改完重试一次）**：实际操作时为了能在 endpoint 变成 ONLINE
之前不用一直卡在前台等，写了一个临时脚本用 `wait_get_endpoint_vector_search_endpoint_
online(...)` 轮询状态，第一版传参写错了：
```python
# 第一版（错误）：
info = client.vector_search_endpoints.wait_get_endpoint_vector_search_endpoint_online(
    name=settings.vector_search_endpoint,  # ← 参数名错了
    timeout=datetime.timedelta(seconds=3600),
)
```
报错：`TypeError: ... got an unexpected keyword argument 'name'`。注意：**这个报错发生
在等待阶段，`create_endpoint(...)` 本身在这之前已经调用成功了**（endpoint 已经在后台
开始建了），所以不需要重新创建，只需要把等待脚本的参数名改成 SDK 真实签名要求的
`endpoint_name`，重新等一次：
```python
# 第二版（正确）：
info = client.vector_search_endpoints.wait_get_endpoint_vector_search_endpoint_online(
    endpoint_name=settings.vector_search_endpoint,
    timeout=datetime.timedelta(seconds=3600),
)
```
这类"等待/轮询"辅助逻辑，`ops/rag/setup_vs_endpoint.py` 本体代码里没有（本体只是
`fire-and-forget` 式的 `create_endpoint(...)`，不阻塞），是当时为了**在这次验证过程中
主动等到 ONLINE 再往下走**才临时加的，不是这个文件的固定实现的一部分——如果你直接跑
`python -m ops.rag.setup_vs_endpoint`，它会立刻返回，不会帮你等待 ONLINE，需要自己用
`client.vector_search_endpoints.get_endpoint(name=...).endpoint_status.state` 或上面
`wait_get_endpoint_vector_search_endpoint_online` 自行确认。

**结果**：
```
DONE endpoint_status=EndpointStatusState.ONLINE
```

**最终结果**：✅ 通过（endpoint `salesduo-vs-endpoint` 变成 `ONLINE`）。

**耗时**：从发起创建到 `ONLINE`，大约 1-2 分钟——**比历史记录里"第一次创建可能要十几到
几十分钟"快很多**（`docs/DEVELOPMENT_JOURNAL.md` 里记的 35-40 分钟是这个 workspace 里
第一次创建 Vector Search endpoint 的情况；这次是重建，workspace 底层可能已经有过
一次性的初始化，所以快了）。如果你重新跑这一步，预期耗时不确定，做好等到几十分钟的心理
准备，但也可能像这次一样很快。

---

#### 3.5 重建 Delta 表 + 创建 Index

**验证内容**：用迁移后的 `ops/rag/ingest_docs.py` 重新上传原始文档、刷新 Delta 表、
发起建 index 请求。

**目的**：验证 3.3 迁移里 `create_delta_sync_index()` 部分的代码对不对；同时这一步本身
也是"文档站点内容有没有丢"的验证——`ops/rag/ingest_docs.py::create_and_populate_delta_
table()` 会先 `DELETE FROM` 再重新 `INSERT`，等于把 18 条 chunk 重新写了一遍。

**代码文件**：
- `ops/rag/ingest_docs.py`（`ensure_volume_exists`、`upload_raw_docs`、
  `create_and_populate_delta_table`、`create_delta_sync_index`）
- `ops/rag/chunk_docs.py`（被 `ingest_docs.py` 调用，本身逻辑这次没改，Part A 已经
  离线验证过）

**执行方式**：
```bash
source .venv/bin/activate
python -m ops.rag.ingest_docs
```

**结果**：
```
UC Volume 已存在: adventureworks_dataagent.salesduo_agent_tools.salesduo_docs_volume
已上传原始文档: .../AW_Corporate_Credit_and_Payment_Terms_Policy.docx
已上传原始文档: .../AW_Large_Transaction_and_Special_Settlement_Compliance_Regulation-v2.docx
已写入 18 条 chunk 到 adventureworks_dataagent.salesduo_agent_tools.salesduo_docs_chunks
已创建 Vector Search index: adventureworks_dataagent.salesduo_agent_tools.salesduo_docs_index
文档解析与索引建仓完成。
```

**采取的行动**：无需调整，一次通过（3.4 的 bug 只出在我额外写的等待脚本里，不在这个
文件本体）。

**最终结果**：✅ 脚本本身跑完成功。**但这只代表"发起了建 index 的请求"，不代表 index
已经能查询**——Delta Sync Index 首次同步需要时间，紧接着 3.6 单独验证了这一点，不能只看
这一步"跑完不报错"就当作完成。

**耗时**：不到 1 分钟（上传两个小文件 + 18 行 INSERT + 一次 API 调用，都很快；真正慢的
是后台同步，见 3.6）。

---

#### 3.6 轮询 Index 真正 Ready

**验证内容**：3.5 只是"发起了建 index 的请求"，这一步持续查 index 状态，直到
`status.ready == True`。

**目的**：对应 CLAUDE.md 原 Step 3 的硬性验收标准——"索引建完并同步完成后...不能只看
索引的 `status.ready == True` 就当作这一步完成"反过来说也成立："不能只看 create_index
调用返回成功就当作完成"，必须真的查到 `ready == True`。

**代码文件**：无对应仓库脚本，临时代码，针对
`adventureworks_dataagent.salesduo_agent_tools.salesduo_docs_index` 这个 index。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
import time
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
deadline = time.time() + 3300  # 最多等 55 分钟
i = 0
while time.time() < deadline:
    idx = client.vector_search_indexes.get_index(index_name=settings.vector_search_index)
    status = idx.status
    print(f"[{i}] ready={status.ready} {status.message[:150] if status.message else ''}")
    if status.ready:
        print("INDEX_READY")
        break
    i += 1
    time.sleep(30)
else:
    print("TIMEOUT_NOT_READY")
EOF
```

**结果**：状态经历了明确的四个阶段（每 30 秒查一次）：
```
[0]-[11]  ready=False  Delta sync index creation is pending endpoint provisioning.
[12]      ready=False  Delta sync Index creation is pending. Check latest status: ...
[13]      ready=False  Index is currently pending setup of pipeline resources. ...
[14]-[18] ready=False  Index is currently is in the process of syncing initial data. ...
[19]      ready=True   Index creation succeeded. ...
INDEX_READY
```

**采取的行动**：第一次用更短的轮询窗口（20 次 × 15 秒 = 5 分钟）跑过一次，5 分钟内一直
停在"pending endpoint provisioning"，没等到 ready，于是换成上面这版更长的窗口（最多约
55 分钟，每 30 秒查一次）重新等，最终在约第 19 次（约 9.5-10 分钟）变成 `ready=True`。
这不是代码 bug，是正常的基础设施异步过程，加长等待时间即可，不需要改任何业务逻辑。

**最终结果**：✅ 通过，index 从发起创建到 `ready=True` 总共约 10-15 分钟（含两段轮询的
衔接时间）。

**耗时**：约 10-15 分钟，原因见上——Delta Sync Index 首次同步不是瞬时的，要经过
"等 endpoint 就绪 → 等 pipeline 资源 → 同步初始数据 → ready" 四个阶段。

---

#### 3.7 检索质量验证（真实业务问题）

**验证内容**：用 4 个跟两份政策文档相关的真实中文问题，调 `src/clients/retriever.py::
retrieve()`，检查 top-k 结果里排在前面的是不是真的相关。

**目的**：对应 CLAUDE.md 原 Step 3 硬性验收标准——"索引建完并同步完成后，必须用至少
3-5 个真实业务问题跑一遍 `similarity_search`，人工检查 top-k 结果里是否包含真正回答该
问题所需的段落"，不能只看 `status.ready == True` 就当完成。同时这也是对 3.3 里
`retrieve()` 迁移代码的最终验证——用真实检索请求，不只是"能返回结果"，而是"返回的结果
质量对不对"。

**代码文件**：`src/clients/retriever.py`（`retrieve()`）。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.clients.retriever import retrieve

questions = [
    "Tier 2 Preferred Account 客户的账期和信用额度上限是多少？",
    "超限15%需要谁审批？",
    "大额交易用公司支票结算允许吗？",
    "什么情况下需要外汇对冲条款？",
]

for q in questions:
    print("=" * 60)
    print("Q:", q)
    chunks = retrieve(q, k=8)
    for c in chunks[:3]:
        print(f"  score={c.score:.3f} [{c.section_title}] {c.content[:80]}")
EOF
```

**结果**（节选，完整输出见执行记录）：
- "Tier 2 账期额度" → top-3 全部命中 `2. Customer Tiering and Payment Terms Matrix`
  章节，score 0.55-0.59。
- "超限15%审批" → 第一名命中 `3. Exception Handling and Special Approval Workflow`，
  score 0.48。
- "公司支票结算" → 第一名命中 `2. Approved and Restricted Settlement Methods`，
  score 0.50。
- "外汇对冲条款" → 前几名分布在 `Exception Handling` 和 `Approved and Restricted
  Settlement Methods` 两个相关章节，score 0.45-0.47。

**采取的行动**：无需调整，4 个问题的 top-3 都命中了语义相关的章节，判定检索质量可用。

**最终结果**：✅ 通过。

**耗时**：几秒钟（4 次检索请求）。

**Step 3 总耗时**：约 20-25 分钟，绝大部分时间花在 3.6 的 index 同步等待上（10-15
分钟）和 3.4 的 endpoint 创建等待（1-2 分钟，这次运气好比较快），代码本身的执行时间
（3.1-3.3 的排查+迁移、3.5 的建表、3.7 的检索测试）加起来不到 5 分钟。

---

### Step 4-6：LangGraph 端到端编排验证

**验证内容**：三类端到端用例——只需结构化数据、只需非结构化数据、需要多跳（非结构化→
计算→结构化）——加上 loop 上限兜底用例，全部真实连接 Genie + Vector Search + LLM 跑一遍。

**目的**：Step 1-3 分别验证了各个外部资源"单独连得通"，这一步验证的是
`src/graph/build_graph.py` 组装出来的整张图——`router` 的条件边路由、
`structured_agent`/`unstructured_agent` 循环回 `router`、`finalize` 兜底——在真实调用
链路下，各节点之间传递状态（`AgentState`，尤其是 `genie_conversation_id`、
`credit_info`、`structured_result`）有没有问题。这一步同时也是对 Step 3 迁移的
`retriever.py` 和 Step 1-2 验证过的 `genie_client.py`/UC Function 的一次组合验证——
单独测都通过不代表组合起来也没问题。

**代码文件**：
- `tests/test_integration_cases.py`（测试文件本身）
- 间接覆盖：`src/graph/build_graph.py`、`src/graph/router.py`、
  `src/graph/structured_agent.py`、`src/graph/unstructured_agent.py`、
  `src/graph/finalize.py`、`src/clients/genie_client.py`、`src/clients/retriever.py`

**执行方式**：
```bash
source .venv/bin/activate
python -m pytest tests/test_integration_cases.py -v
```

**结果**：
```
tests/test_integration_cases.py::test_structured_only_question PASSED
tests/test_integration_cases.py::test_unstructured_only_question PASSED
tests/test_integration_cases.py::test_multi_hop_credit_then_structured_question PASSED
tests/test_integration_cases.py::test_loop_count_never_exceeds_configured_max PASSED
4 passed, 1 warning in 153.32s (0:02:33)
```

**采取的行动**：无需调整，一次通过（这里唯一需要说明的是：`test_integration_cases.py`
本身的 `_READY` 跳过判断条件，在 Part A 的重构提交里已经改成了检查
`azure_subscription_id`/`azure_resource_group_name`/`azure_databricks_workspace_name`
三个新字段，不再检查已经废弃的 `databricks_host`/`databricks_token`，这个改动 Part A
时已经做完，Step 4-6 只是第一次真正在这个改动生效的情况下跑通全部 4 个用例）。

**最终结果**：✅ 通过（4/4）。

**耗时**：153.32 秒（约 2 分 33 秒），主要是 Genie 多次问答轮询（每次 Genie 调用要等 SQL
执行完成）+ 多个 router/finalize 的 LLM 调用累加起来的时间，属于正常范围。

---

### Step 7：部署（模型注册 + Serving Endpoint + Databricks App）

#### 7a. 部署前先只读检查当前状态

**验证内容**：在动手部署之前，先查一下 Serving Endpoint 和 App 现在到底是什么状态——
不假设"之前部署过就还在正常跑"。

**目的**：避免对一个状态未知的资源盲目操作；同时这一步查出来的信息（Serving Endpoint
存在且 READY，但 App 是 STOPPED）直接决定了 7b/7c 具体要做什么操作。

**代码文件**：无对应脚本，读 `src/config.py` 的 `model_serving_endpoint_name`/
`databricks_app_name` 两个配置项对应的真实资源状态。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
names = [e.name for e in client.serving_endpoints.list()]
print("Serving Endpoint 存在:", settings.model_serving_endpoint_name in names)
if settings.model_serving_endpoint_name in names:
    ep = client.serving_endpoints.get(settings.model_serving_endpoint_name)
    print("state:", ep.state)

app = client.apps.get(settings.databricks_app_name)
print("app_status:", app.app_status)
print("compute_status:", app.compute_status)
print("url:", app.url)
EOF
```

**结果**：
```
Serving Endpoint 存在: True
state: ready=READY, config_update=NOT_UPDATING
app_status: state=UNAVAILABLE
compute_status: state=STOPPED
url: https://salesduo-agent-7405610143165482.2.azure.databricksapps.com
```

**采取的行动**：无（纯信息收集）。

**最终结果**：ℹ️ 信息性检查，不是"通过/不通过"，但结论是"Serving Endpoint 存在但代码是
重构前的旧版本，必须重新注册；App 存在但 compute 停着，需要重新启动"，直接指导了 7b/7c。

---

#### 7b. 注册模型 + 更新 Serving Endpoint

**验证内容**：把最新代码（含目录重构、Vector Search 迁移）重新打包注册成一个新的模型
版本，更新到已存在的 Serving Endpoint 上。

**目的**：Serving Endpoint 运行的是打包进去的那份代码快照，不会自动感知本地代码变了；
这次改动量很大（整个目录结构、`retriever.py` 实现方式都变了），必须重新部署，不能假设
线上跑的还是对的版本。同时这一步验证了 `ops/deploy_model.py` 里另一条相关改动——
`_REQUIREMENTS_FILE` 从指向根目录 `requirements.txt` 改成指向
`requirements-runtime.txt`（更小的依赖集，见 `docs/CODE_REVIEW_FINDINGS.md` 第 2 条）。

**代码文件**：`ops/deploy_model.py`（`log_and_register_model()`、
`deploy_serving_endpoint()`）。

**执行方式**：
```bash
source .venv/bin/activate
python -m ops.deploy_model
```

**结果**（节选）：
```
Registered model 'adventureworks_dataagent.salesduo_agent_tools.salesduo_agent' already exists. Creating a new version of this model...
Created version '5' of model 'adventureworks_dataagent.salesduo_agent_tools.salesduo_agent'.
已注册模型: adventureworks_dataagent.salesduo_agent_tools.salesduo_agent version 5
已更新 serving endpoint: salesduo-agent -> version 5
Step 7 模型注册与 Serving Endpoint 部署完成。
```

**采取的行动**：无需调整，一次通过。

**最终结果**：✅ 通过，随后又单独用 SDK 确认了一遍（不只信脚本自己打印的"完成"）：
```python
ep = client.serving_endpoints.get(settings.model_serving_endpoint_name)
# state: ready=READY, config_update=NOT_UPDATING
# served_entities[0]: entity_version=5, deployment=DEPLOYMENT_READY
```

**耗时**：约 11-12 分钟（`log_model` 打包上传 23 个 artifact 本身很快，几秒钟；主要耗时
在 `update_config_and_wait`——Serving Endpoint 滚动更新到新版本需要重新拉起容器，这段
时间不可压缩，是 Model Serving 平台本身的行为）。

---

#### 7c. 部署 Databricks App

**验证内容**：把 `app/` 目录的代码同步到 workspace 并启动 App 的 compute。

**目的**：对应 CLAUDE.md 原 Step 7 的硬性规则——"对于 apps 类型的资源，`databricks
bundle deploy` 只上传代码/注册资源定义，不会启动 App，必须额外执行 `databricks bundle
run <app_resource_key>` 才会真正启动"。这次要验证两件事：(1) `databricks.yml` 里的
资源定义在目录重构后还有效（`databricks.yml` 唯一相关的改动是把注释里的 `src/setup/`
改成了 `ops/`，不影响实际资源定义）；(2) **`databricks` CLI 本身能不能用新的 Azure
认证方式**——CLI 是独立的 Go 二进制进程，不会继承 Python 进程内部通过
`os.environ[...] = ...` 设置的环境变量，必须在调用 CLI 的这个 shell 里单独 `export`。

**代码文件**：`databricks.yml`（资源定义，`resources.apps.salesduo_agent`）、
`app/app.py`（实际被部署+启动的代码，这次认证切换本身没有改动这个文件——App 运行时用的
是 Databricks Apps 自带的 service principal 默认凭据链，跟本机 `az login`/Azure 资源坐标
完全无关，这是另一套独立的身份）。

**执行方式**：
```bash
cd /Users/elaine/repository/SalesDuo

# 关键：CLI 是独立进程，必须在这个 shell 里单独 export，
# 不能指望 Python 里 src/config.py 设置的环境变量会被 CLI 进程看到
export DATABRICKS_AZURE_RESOURCE_ID=$(python3 -c "
from src.config import settings
print(f'/subscriptions/{settings.azure_subscription_id}/resourceGroups/{settings.azure_resource_group_name}/providers/Microsoft.Databricks/workspaces/{settings.azure_databricks_workspace_name}')
")

databricks bundle deploy
databricks bundle run salesduo_agent
```

**结果**：
```
# bundle deploy:
Uploading bundle files to /Workspace/Users/.../.bundle/salesduo/dev/files...
Deploying resources...
Deployment complete!

# bundle run salesduo_agent:
✓ App is in UNAVAILABLE state
✓ App compute is in STOPPED state
✓ Starting the app salesduo-agent
✓ App is starting...  (重复约 18 次，说明是持续轮询)
✓ Pending deployment is completed!
✓ App is started!
✓ Starting app with command: streamlit run app.py
✓ App started successfully
You can access the app at https://salesduo-agent-7405610143165482.2.azure.databricksapps.com
```

**采取的行动**：无需调整，一次通过。在跑之前先用 `databricks bundle validate` 单独确认
过一次 CLI 认证配置本身是对的（`Validation OK!`），属于正式操作前的额外确认，不算"遇到
问题调整"。

**最终结果**：CLI 输出说"成功"，但按项目自己的验收标准（"不能只看 `bundle deploy`
命令本身返回成功"）**这一步单独不算最终验收**，真正的验收在 7d。

**耗时**：`bundle deploy` 几秒钟；`bundle run` 约 3-4 分钟（主要是等 compute 从
`STOPPED` 拉起 + 部署代码到 compute 上）。

---

#### 7d. 用 SDK 确认 App 真实状态（不信任 CLI 文字输出）

**验证内容**：CLI 打印"App started successfully"之后，单独用 SDK 查一次真实状态字段。

**目的**：对应 CLAUDE.md 原 Step 7 硬性规则——"验收标准是用
`client.apps.get(app_name).app_status.state == 'RUNNING'` 且
`compute_status.state == 'ACTIVE'`，不能只看 `bundle deploy`/`bundle run` 命令本身
返回成功"。

**代码文件**：无对应脚本，直接查 `databricks_app_name` 对应的真实资源。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
app = client.apps.get(settings.databricks_app_name)
print("app_status.state:", app.app_status.state)
print("compute_status.state:", app.compute_status.state)
print("url:", app.url)
EOF
```

**结果**：
```
app_status.state: ApplicationState.RUNNING
compute_status.state: ComputeState.ACTIVE
url: https://salesduo-agent-7405610143165482.2.azure.databricksapps.com
```

**采取的行动**：无。

**最终结果**：✅ 通过，两个状态字段都满足硬性验收标准。

**耗时**：几秒钟（一次 API 调用）。

---

#### 7e. 最终端到端实测（模拟聊天框真实调用）

**验证内容**：直接调已部署的 Serving Endpoint（不经过浏览器点 App，但走的是跟
`app/app.py::ask()` 完全一样的 REST 调用方式），发一个真实的多跳问题，检查完整链路
（含 Serving Endpoint 自己的运行时身份，不是本机 `az login` 身份）能不能跑通。

**目的**：这是最终、也是最贴近真实用户体验的一步——前面所有验证都是"本机身份能不能连上
各个资源"，但部署到 Serving Endpoint 之后，代码运行在 Databricks 托管的容器里，用的是
**Serving Endpoint 自己的身份**（通过 `ops/deploy_model.py` 里 `resources=[...]`
声明自动获得的授权），这个身份跟本机 `az login` 的身份是两回事，必须单独测。这一步
对应 `docs/DEVELOPMENT_JOURNAL.md` 案例 11 记录的一个已知未解决问题——专门用来确认
这个问题现在是否还存在。

**代码文件**：无新增代码，逻辑照抄 `app/app.py::ask()` 的调用方式。

**执行方式**：
```bash
source .venv/bin/activate
python3 - <<'EOF'
from src.db_client import get_workspace_client
from src.config import settings

client = get_workspace_client()
question = (
    "客户 Bike World 目前的年采购额和合作年限是多少？"
    "按公司信用政策，他们能申请到的最高信用额度和账期是多少？"
)
raw = client.api_client.do(
    "POST",
    f"/serving-endpoints/{settings.model_serving_endpoint_name}/invocations",
    body={"input": [{"role": "user", "content": question}]},
)

def extract_text(raw):
    for item in raw.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c.get("text", "")
    return None

print(extract_text(raw))
trace = raw.get("custom_outputs", {}).get("trace")
for t in trace or []:
    print(f"- {t.get('step')}: {t.get('reasoning') or t.get('output_summary', '')[:100]}")
    if t.get("error"):
        print("  ERROR:", t["error"])
EOF
```

**结果**：`unstructured_agent`（Vector Search 检索）这一跳完全正常，没有报错；但每次
`structured_agent`（Genie 查询）都失败：
```
PERMISSION_DENIED: An error occurred accessing the schema. Failed to fetch tables for
the agent. ... No access to 'adventureworks_dataagent.sales.customer'. ...
（sales/person схема下几乎所有表都报同样的权限错误）
```
`router` 按设计重试到 `MAX_ROUTER_LOOPS=5` 上限后，`finalize` 生成了"信息可能不完整"
的降级回答，整个请求**没有崩溃、没有报 500**，只是给不出完整答案。

**采取的行动**：**这是本次会话唯一一处触发"遇到问题只尝试一次"策略的地方**——按事先约定
的原则，这个问题不影响"App 部署"这条主线（App 已经成功部署、在跑，非结构化/政策类问题
完全可用），且是 `docs/DEVELOPMENT_JOURNAL.md` 案例 11 里记录的、认证切换之前就存在的
已知未解决问题（不是这次改动引入或应该修复的新 bug），所以没有继续排查 Databricks 那边
的 IAM/Unity Catalog 授权配置，原样记录下来。

**最终结果**：⚠️ **部分通过，有已知遗留问题**——App 部署本身✅完全成功
（`RUNNING`/`ACTIVE`，可访问），端到端链路里的**非结构化路径**✅可用，**结构化路径**
（Genie 查询底层表）❌受 Serving Endpoint 身份权限问题阻塞，是历史遗留问题，非本次引入。

**耗时**：几十秒（一次完整的 5 轮 router 循环 + 多次 LLM/Genie 调用）。

---

## 总表

| 步骤 | 验证内容 | 最终结果 | 耗时 |
|---|---|---|---|
| A.1 | 20 模块 import | ✅ 通过 | 几秒 |
| A.2 | 离线 pytest（6 用例） | ✅ 通过 | 1.7s |
| 1 | Azure CLI 认证 + 连通性 | ✅ 通过 | 几秒 |
| 2a/2b | UC Function 存在+可调用 | ✅ 通过 | 几秒 |
| 2c | Genie Space 配置完整性 | ✅ 通过（含一次自证的脚本误报） | 几秒 |
| 3.1-3.3 | 发现+修复 databricks-ai-search 认证不兼容 | ✅ 已迁移到 SDK 原生 API | — |
| 3.4 | 创建 Vector Search Endpoint | ✅ ONLINE | 1-2 分钟 |
| 3.5 | 重建 Delta 表 + 发起建 index | ✅ 通过 | <1 分钟 |
| 3.6 | 轮询 index ready | ✅ ready=True | 10-15 分钟 |
| 3.7 | 检索质量（4 个真实问题） | ✅ 通过 | 几秒 |
| 4-6 | 端到端编排（4 用例） | ✅ 通过 | 153s |
| 7a | 部署前状态检查 | ℹ️ 信息性 | 几秒 |
| 7b | 模型注册 v5 + Serving Endpoint 更新 | ✅ READY | 11-12 分钟 |
| 7c | `bundle deploy` + `bundle run` | ✅ CLI 报告成功 | ~4 分钟 |
| 7d | SDK 确认 App RUNNING/ACTIVE | ✅ 通过 | 几秒 |
| 7e | 最终端到端实测（多跳问题） | ⚠️ 部分通过，Genie 权限问题遗留 | 几十秒 |

**全流程从 Step 1 到 Step 7e 总耗时约 55-65 分钟**，其中约 30 分钟花在两处不可压缩的
基础设施等待上（3.6 index 同步约 10-15 分钟，7b Serving Endpoint 滚动更新约 11-12
分钟），代码本身的执行和验证时间累计不到 10 分钟。

---

## 补充：Step 7e 遗留问题已解决（同日）

### 排查修正

7e 记录时，曾用"直接调 `/invocations` 绕过 App，还是报同样的权限错误"作为证据，推断
"问题出在 Serving Endpoint 自己的运行时身份，不是 App 的 service principal"——**这个
推断是错的**。真正的原因：调 `/invocations` 时，无论调用方是谁（App，还是直接拿个人
身份发 REST 请求），Serving Endpoint 内部真正执行 Genie 查询时用的都是**同一个固定
身份**，而这个固定身份实际上就是 **App 自动生成的 service principal**——不是"我自己
调用时用我的身份""App 调用时用 App 的身份"这种按调用方切换的模型，所以"绕过 App 直接调"
并不能排除"是 App 的 service principal 权限不够"这个可能性，这一步的推理漏洞在于把
"发起 REST 请求的身份"和"endpoint 内部实际执行代码时的身份"当成了同一回事。

### 验证方式（用户完成）

1. 本机用个人 `az login` 身份跑 `chat.py`——结构化查询一直都能正常拿到数据（这条路径
   从 Step 1-6 起就没出过问题，不是这次要验证的对象，列出来只是作为对照组）。
2. 找到 App 的 service principal：`client.apps.get(app_name).service_principal_
   client_id`。
3. 用 `ops/grant_app_permissions.py` 给这个 service principal 授权（`USE CATALOG` +
   `sales`/`person` 的 `USE SCHEMA`+`SELECT` + `salesduo_agent_tools` 的
   `USE SCHEMA`+`EXECUTE`，后者是查完表之后又发现漏掉的——Genie 调用业务规则函数需要
   `EXECUTE`，不是 `SELECT`）。
4. 直接在部署好的 Databricks App 聊天框里实测（不是走 REST 脚本模拟），同一类问题
   （"你能拿到哪些数据"）确认能正常返回结构化数据摘要，权限问题消失。

**代码文件**：`ops/grant_app_permissions.py`（新增脚本，可重复执行，`GRANT` 语句本身
是幂等的）。

**执行方式**：
```bash
source .venv/bin/activate
python -m ops.grant_app_permissions
```

**最终结果**：✅ 已解决。`docs/DEVELOPMENT_JOURNAL.md` 案例 11 标题已改为"已解决"并加了
2026-07-27 补充说明，Part 4 已知局限第 1 条也已划掉更新。

**遗留的操作性提醒**：App 每次被删除重建都会拿到一个新的 service principal，这份授权
不是对"salesduo-agent 这个 App 名字"永久生效，而是对"当前这个 service principal 对象"
生效——重建 App 之后如果又复现权限错误，先确认是不是忘了对新的 service principal 重新
跑一次 `ops/grant_app_permissions.py`。
