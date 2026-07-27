# SALESDUO — AdventureWorks 结构化+非结构化数据 Agent

> **v2 说明**：本文件是 v1（见 `CLAUDE_v1.md`）基于一次完整实现+部署经验的修订版。
> 目标、架构、环境变量规范等未被本次经验证伪的部分保持不变；实际踩过的坑被转化成了本版
> 里的硬性验收标准（而不是事后补丁），具体改了哪里、为什么改，见文末"版本变更说明"一节，
> 更详细的排查过程见 `DEVELOPMENT_JOURNAL.md`。

## 0. 一句话目标

基于 Databricks 生态,构建一个能同时查询 **结构化数据**(Unity Catalog 里的 AdventureWorksLT 表)和 **非结构化数据**(`documents_generated/` 下的公司文档)的对话式 Agent,最终以一个简单的聊天框 UI(Databricks Apps)交付给用户。查询模式不是简单的并列查询汇总,而是**动态多跳**:例如先查非结构化文档得到用户 credit 信息 → 结合业务规则计算 → 再决定要不要查结构化数据 → 结果可能还需要回头再查一次非结构化数据。跳数和顺序**不固定**,由模型在运行时动态决定。

---

## 1. 现状与前提

- Unity Catalog 里已有目录 `adventureworks_dataagent`,包含 schema:`humanresources`、`information_schema`、`person`、`production`、`purchasing`、`sales`。**默认复用这个已有目录**,不要新建 catalog,除非发现表结构不满足需求。
- 仓库根目录下只有 `documents_generated/` 文件夹,里面是两份 `.docx` 公司文件(非结构化数据源,大概率是信用/业务规则类文档)。这两份文档需要被解析、切块、建索引。
- 开发全程在本地 IDE(Claude Code)中进行,通过 Databricks SDK / Databricks CLI / Databricks Connect 连接 workspace,不依赖手工在 Web UI 里点选。**唯一的例外**：Genie Space 本身的创建、以及第一次配置 Instructions/Examples 必须先在 UI 里手动做一次（详见第 7 节风险点 1），这是当前 Databricks API 的限制，不是可以规避的实现选择。
- **只允许在 `SALESDUO/` 当前项目目录内创建/修改文件**。不要修改、访问、或探测这个目录之外的任何本地路径。对 Databricks workspace 侧的资源(catalog/schema/index/endpoint/app)有创建权限,但命名必须清晰打上项目前缀(见第 6 节),不要污染其他项目的资源命名空间。
- 本项目**不强制要求 git 初始化**，但如果要长期维护，建议尽早补一个 git 仓库——v1 开发全程没有 git，导致复盘时只能靠文件 mtime 和会话记录倒推时间线，不如正常 commit 历史可靠。

---

## 2. 架构总览

```
                     ┌─────────────────────────────────────────┐
                     │  MLflow ResponsesAgent (predict/predict_stream)
                     │  ← 对外唯一契约，Databricks Apps 聊天框只认这个接口
                     └──────────────────┬────────────────────────┘
                                        │
                          LangGraph StateGraph (supervisor-with-cycles)
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
        router 节点                structured_agent           unstructured_agent
   (每步判断：继续查结构化      (调用 Genie Agent：           (调用 Vector Search /
    /继续查非结构化/结束)        AdventureWorksLT 表 +          Knowledge Assistant：
                                UC Function 业务规则计算)       docx 文档 chunk 检索)
              │                         │                         │
              └──── 循环回 router，直到 router 判定信息齐全 ──────┘
                                        │
                                    finalize 节点
                              (综合所有中间结果，生成最终回答)
```

**关键设计原则(来自前序讨论,必须遵守)：**

1. **不使用 Agent Bricks: Multi-Agent Supervisor(no-code)**——因为该产品的路由是一次性分类,不支持"执行完一个子 agent 后根据结果动态决定下一步、可能循环"这种场景。本项目必须走**代码优先(code-first)**路径。
2. **不要设计成"dispatcher 节点 + summarizer 节点"两段式固定结构**——分任务和总结/再分任务是同一个决策动作的反复发生,必须用**同一个 router 节点 + 条件边(conditional edge)+ 循环边**来实现,而不是两个角色互相调用。LangGraph 里的具体做法：`structured_agent`/`unstructured_agent` 跑完都无条件 `add_edge` 回 `router`（不是直接连到下一个业务节点或 `finalize`），`router` 用 `add_conditional_edges` 依据 `state["next_step"]` 决定去哪；`finalize` 是唯一连到 `END` 的节点。
3. Genie 是**有状态的多轮对话**(靠 `conversation_id` 维持上下文)。如果流程里同一个用户请求触发了对 Genie 的第二次调用,必须复用同一个 `conversation_id`,不要每次开新会话。
4. 必须设置 **`loop_count` 上限**(路由循环次数上限),防止 router 判断错误导致的无限循环。超过上限时强制走 `finalize`,并在最终回答中注明信息可能不完整。
5. **【v2 新增】`AgentState` 必须包含一个累加式的白盒追踪字段（如 `trace: Annotated[list[dict], operator.add]`）**，每个节点执行完追加一条记录（router 的判断理由、Genie 实际生成执行的 SQL、Vector Search 检索到的 chunk id/分数、每步的失败原因），不是覆盖式的"只看最后一次结果"。这不是可选的调试糖——v1 开发中期发现，没有这个字段时，多跳场景里第二次调用同一节点会覆盖掉第一次的中间结果，出问题时完全没法诊断是哪一跳出的错。部署上线之后（Model Serving/Databricks App），本地就看不到 `graph.invoke()` 的返回值了，必须通过 `ResponsesAgentResponse(custom_outputs={"trace": ...})` 把这个字段带出来，否则线上问题会变成纯黑盒排查。

---

## 3. 环境变量(唯一配置来源)

**所有可变参数一律走环境变量,代码中不允许出现任何硬编码的 catalog 名、schema 名、endpoint 名、workspace URL、token 等。** 在项目根目录维护一个 `.env.example`(不含真实值,仅做字段说明),真实值放本地 `.env`(需加入 `.gitignore`)。

v1 定义的这份变量清单在实际开发中被完整验证有效，没有发现遗漏或多余的变量，v2 保持不变：

```bash
# Databricks 认证与连接
DATABRICKS_HOST=
DATABRICKS_TOKEN=            # 或走 OAuth profile，二选一，代码需都支持
DATABRICKS_CONFIG_PROFILE=   # 可选，走 ~/.databrickscfg profile 时使用

# Unity Catalog（结构化数据，复用已有目录）
UC_CATALOG=adventureworks_dataagent
UC_SCHEMA_SALES=sales
UC_SCHEMA_PERSON=person
UC_SCHEMA_PRODUCTION=production
UC_FUNCTION_SCHEMA=          # 存放 UC Function（业务规则计算）的 schema，建议新建一个 agent_tools schema

# SQL Warehouse（Genie 查询用）
SQL_WAREHOUSE_ID=

# Genie Agent（结构化查询）
GENIE_SPACE_ID=

# 非结构化数据 / Vector Search
DOCS_SOURCE_DIR=documents_generated
UC_VOLUME_PATH=               # 存放原始 docx 及解析中间产物的 UC Volume 路径
DELTA_TABLE_DOCS_CHUNKS=       # 存放切块后文本的 Delta 表，格式 catalog.schema.table
VECTOR_SEARCH_ENDPOINT=
VECTOR_SEARCH_INDEX=
EMBEDDING_MODEL_ENDPOINT=      # 走 Databricks Foundation Model API 的 embedding 模型

# 编排用的 LLM
LLM_SERVING_ENDPOINT=          # 走 Databricks Foundation Model API（不要硬编码具体模型名在代码里，走这个变量）

# MLflow / 评测 / 部署
MLFLOW_EXPERIMENT_PATH=
MODEL_SERVING_ENDPOINT_NAME=   # 部署后的 endpoint 名（如走 Model Serving）
DATABRICKS_APP_NAME=           # 部署后的 Databricks App 名（如走 Apps）

# 编排安全阀
MAX_ROUTER_LOOPS=5             # router 循环上限，防止死循环
```

**【v2 新增说明】**：本地跑建仓/部署脚本时，除了这份 `.env` 之外，还需要注意两点（详见第
7 节风险点 8、9）：
- 这份 `.env` 只有**本地 Python 代码**能读到；`databricks` CLI（跑 `bundle validate`/
  `bundle deploy`）不会自动加载它，需要在跑 CLI 命令前手动 `export` 对应的
  `DATABRICKS_HOST`/`DATABRICKS_TOKEN`。
- 被部署到 Model Serving 的模型运行在隔离容器里，同样看不到这份 `.env`——部署脚本必须把
  非密钥的配置项通过 `ServedEntityInput(environment_vars={...})` 显式传给 serving
  endpoint，DATABRICKS_HOST/TOKEN 不需要传（走 `resources` 自动鉴权）。

---

## 4. 实施步骤(严格按顺序执行,每步完成后自行验证再进入下一步,不要跳步,也不要在中途停下来问我)

### Step 1 — 环境与认证

- 用 Databricks SDK(`databricks-sdk`)读取环境变量完成认证,写一个 `src/db_client.py` 里统一封装 `get_workspace_client()`,全项目其他地方都从这里拿 client,不要在多处重复初始化认证逻辑。
- 验证连通性:能列出 `UC_CATALOG` 下的 schema。

### Step 2 — 结构化数据侧:UC Function + Genie Agent

- 检查 `UC_FUNCTION_SCHEMA` 指定的 schema 是否存在,不存在则创建(用 SDK,不要手工建)。
- 用 UC Function(SQL 或 Python)实现业务规则计算逻辑(具体规则以 `documents_generated/` 文档内容为准 —— 先解析文档确认规则细节,再决定 UC Function 的参数和返回结构;不要在没读文档前臆造规则)。
- **【v2 硬性验收标准】如果用 SQL Function 且需要返回多个字段，`RETURN` 语句必须用
  `SELECT STRUCT(col1, col2, ...) FROM ...` 包裹，不能直接写多列 `SELECT`**——后者会在
  真正被调用时报 `SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN`，而且
  `CREATE OR REPLACE FUNCTION` 本身不会在创建时校验出这个问题，会让你误以为函数建成功了。
- **【v2 硬性验收标准】UC Function 建完之后，必须执行以下两步验证，不能只看
  建表/建函数语句的返回状态是 SUCCEEDED 就当作完成**：
  1. 查 `information_schema.routines` 确认函数记录真实存在。
  2. 直接 `SELECT catalog.schema.function_name(测试参数)` 调用一次，人工核对返回的
     STRUCT 字段值和手算结果一致。
- 用 Databricks SDK 创建/配置 Genie Agent(原 Genie Space),数据源指向 AdventureWorksLT 相关 schema(`sales`/`person`/`production` 等,按实际问题需要选择,不要不加选择地全部挂上)。
- **【v2 修正】不需要在 Genie UI 里寻找"挂载 UC Function 为工具"的入口**——这个概念不
  存在（`instructions.sql_functions` 这个 API 字段实际是"认证答案"功能用的，跟"注册一个
  可调用函数"是两回事）。正确做法：把两个 UC Function 的**完整三段式全限定名**和**准确
  的返回 STRUCT 字段名**直接写进 Genie Space 的 Instructions 文本里，Genie 生成 SQL 时
  会自动用全限定名正确调用，不需要任何额外的"注册/挂载"步骤。
- **【v2 新增】Instructions 文本里除了函数信息，还必须包含**：
  1. 关键表之间的关联路径提示（比如 `store`→`customer`→`salesorderheader` 要经过哪个
     字段关联），防止 Genie 生成跳过中间表的错误 JOIN。
  2. `DATEDIFF`（时间单位不加引号）和 `DATE_TRUNC`（时间单位要加引号）这两个函数写法
     不同的提示——这两个函数的参数约定刚好相反，实测 Genie 会把其中一个的写法错误地
     套用到另一个上。
  3. 明确告知业务规则函数是**标量函数**，只能用在 `SELECT` 的列表达式里，不能写进
     `FROM`/`LATERAL` 子句当表函数调用。
- 记录 `GENIE_SPACE_ID` 到环境变量。

### Step 3 — 非结构化数据侧:文档解析 → Delta 表 → Vector Search

- 解析 `documents_generated/` 下的两份 docx,切块(chunk),写入 `DELTA_TABLE_DOCS_CHUNKS` 指定的 Delta 表(至少包含:chunk_id、文本内容、来源文件名、chunk 顺序)。
- **【v2 硬性规则】切块时默认排除文档头部那种 key-value 元数据表（Policy ID、Effective
  Date、Approved By 这类字段）**——实测这类短小通用文本在向量检索里会拿到异常高的相似度
  分数，把真正需要精确匹配的长段落挤出 top-k，导致检索不到目标内容。判断方法：如果一个
  表格只有 2 列且整体是"字段名-值"结构，就不索引这个表格，只在 Delta 表落一份存档即可
  （不需要连原始 docx 都不存，Volume 里还是要留一份原始文件）。
- 用 Databricks SDK 创建 Vector Search endpoint(如不存在)和 Delta Sync Index,embedding 走 `EMBEDDING_MODEL_ENDPOINT`。**第一次在这个 workspace 里创建 Vector Search endpoint 可能需要十几到几十分钟（主要卡在 endpoint 自身的 PROVISIONING_ENDPOINT 阶段，不是索引同步慢），这是正常现象，不代表卡住了。**
- 写一个轻量的 retriever 封装(输入 query,返回 top-k 相关 chunk),作为 `unstructured_agent` 节点内部调用的工具。不需要额外包装成 Knowledge Assistant(no-code 产品),直接代码里调 Vector Search 查询接口即可 —— 这是代码优先路径,不必绕经 no-code 层。**默认 `top_k=8`**（实测 `top_k=5` 在排除元数据噪音之后，仍然会漏检部分目标段落；这不是一个穷举调过的最优值，只是针对本项目两份文档验证过"够用"，如果文档换了，需要重新验证这个值）。
- **【v2 硬性验收标准】索引建完并同步完成后，必须用至少 3-5 个真实业务问题跑一遍
  `similarity_search`，人工检查 top-k 结果里是否包含真正回答该问题所需的段落**——不能
  只看索引的 `status.ready == True` 就当作这一步完成。如果目标段落没进 top-k，先检查是
  否是元数据噪音问题（见上面那条规则），再考虑调大 `top_k`。

### Step 4 — 编排:LangGraph StateGraph

- 按第 2 节的架构图实现:`router`(条件判断)、`structured_agent`(调 Genie)、`unstructured_agent`(调 Vector Search)、`finalize`(综合输出)四个节点,`router` 用带循环的条件边连接。
- State 至少包含:`messages`、`user_query`、`credit_info`、`business_rule_result`、`structured_result`、`genie_conversation_id`、`loop_count`、`next_step`、**`trace`（见第 2 节设计原则 5）**。
- `router` 节点每次判断前必须检查 `loop_count`,超过 `MAX_ROUTER_LOOPS` 强制走 `finalize`。
- `router` 的输出必须走结构化输出(强制枚举 `next_step` 取值),不要靠解析自由文本判断下一步,避免路由解析出错。
- **【v2 硬性规则】所有对外部服务（Genie、Vector Search、UC Function 等）的调用，必须
  捕获具体的失败原因并写入 `trace`，禁止裸抛不带诊断信息的异常**。具体做法：不要用 SDK
  里"一步到位等结果"的便捷方法（比如 Genie 的 `start_conversation_and_wait`）如果它在
  失败时会丢弃详细错误——改成手动轮询状态，失败时把服务端返回的具体错误信息（比如 Genie
  的 `message.error`）记录下来再往上抛/返回，不要让调用方只看到一个"操作失败"却不知道
  具体是权限问题、SQL 语法问题还是别的。
- **【v2 硬性规则】路由/生成用的 LLM 调用如果走强制结构化输出（`with_structured_output`
  之类），必须加重试（建议 2-3 次）+ 失败后的安全降级路径（比如降级为 `finalize` 并说明
  信息可能不完整），不能让模型偶发的格式错误直接让整个请求崩溃**。

### Step 5 — 包装为 MLflow ResponsesAgent

- 实现 `predict`/`predict_stream`,内部调用 `graph.invoke()`/`graph.stream()`,输入输出严格走标准 schema。
- 配置 `MLFLOW_EXPERIMENT_PATH`,确保每次调用自动产生 trace(结构化/非结构化各节点的调用、耗时、中间结果都应可在 trace 里查到)。
- **【v2 新增】`predict`/`predict_stream` 返回值必须把 `AgentState["trace"]` 塞进
  `ResponsesAgentResponse`/`ResponsesAgentStreamEvent` 的 `custom_outputs` 字段里**，
  这是部署上线之后唯一能看到内部执行细节的渠道。

### Step 6 — 本地验证

- 至少覆盖以下三类测试 case,且必须包含"先非结构化 → 结构化 → 再非结构化"这种多跳 case(这是本项目区别于简单并列查询的核心难点,必须重点验证 router 判断是否正确、循环终止是否正常触发、`loop_count` 兜底是否生效):
  1. 只需结构化数据的问题
  2. 只需非结构化数据的问题
  3. 需要多跳(非结构化→计算→结构化→可能再非结构化)的问题
- 用 Agent Evaluation 跑一遍上述测试集,记录基本的正确性/相关性指标。不需要现在就搭建复杂的评测体系,能验证核心路由逻辑正确即可。
- **【v2 补充】** 如果搭建了独立于 pytest 之外的评测集（比如一个带 ground truth 的 JSON
  文件 + 自动跑批脚本），评测题的 ground truth **必须**基于真实拉取的 sandbox 数据算出
  来，不能编造；建议至少 10 题，按"结构化 only / 非结构化 only / 多跳"分类；跑批结果要
  把每题的完整 `trace` 一起存下来，不要只存最终答案和分数——出问题时靠 trace 诊断，不是
  靠猜。同时要接受：Genie 的 SQL 生成有固有的非确定性（见第 7 节风险点 10），评测分数会
  在不同批次之间波动，不追求也不可能稳定达到 100%，能定性验证"核心链路能跑通"即可。

### Step 7 — 部署

- 用 Databricks Apps 部署(不要用裸 Model Serving,因为最终交付形态是聊天框 UI,Apps 自带基础 chat 界面骨架,更贴合需求;不要额外自建前端框架,除非 Databricks Apps 默认模板明显无法满足"简单聊天框"这个最低要求)。
- **【v2 前提提醒】Model Serving 在 Databricks 试用（trial）版工作区里不可用**，会在创建
  serving endpoint 时报 `Model serving is not available for trial workspaces`。如果
  当前是试用账号，这一步会被硬性阻塞，需要升级到正式版（如 Premium）之后才能继续，这不
  是代码或配置能绕开的限制。
- **【v2 硬性规则】用本地脚本（非 Databricks Notebook/Job 环境）跑 `mlflow.pyfunc.
  log_model(..., registered_model_name=...)` 之前，必须显式执行**：
  ```python
  mlflow.set_tracking_uri("databricks")
  mlflow.set_registry_uri("databricks-uc")
  ```
  不设置的话 mlflow 会默默把模型"注册"到本地一个 SQLite 文件里，脚本会打印"注册成功"但
  Databricks workspace 里根本没有这个模型，后续创建 serving endpoint 时会报模型不存在。
- **【v2 硬性规则】`log_model` 必须传 `code_paths=[".../src"]`**（把 `src/` 整个目录
  作为一个 code_path），否则 `agent.py` 里 `from src.xxx import yyy` 这类 import 在被
  加载到隔离的 Serving 容器时会找不到模块。
- **【v2 环境相关提醒】如果这个 workspace 的 Unity Catalog 底层存储是 Azure（Data Lake
  Storage），本地跑部署脚本上传模型 artifact 需要额外安装 `azure-core` 和
  `azure-storage-file-datalake`**，这两个包只是本地上传步骤需要，不影响被部署模型的
  运行时依赖。
- **【v2 硬性规则】`ServedEntityInput` 必须显式配置 `environment_vars`**，把
  `src/config.py` 用到的非密钥配置项（`UC_CATALOG`、`GENIE_SPACE_ID`、
  `VECTOR_SEARCH_INDEX` 等，不含 `DATABRICKS_HOST`/`DATABRICKS_TOKEN`）传给 serving
  endpoint，否则容器内读不到这些配置，运行时会报"缺少必需的环境变量"。
- **【v2 硬性规则】用代码调用已部署的 Serving Endpoint 时，不要用 `databricks-sdk` 的
  `client.serving_endpoints.query()` 解析响应**——这个方法的返回类型是为通用
  chat/completions/embeddings 端点设计的，识别不了自定义 ResponsesAgent 的 `output`
  字段，会把内容丢掉。改用原始 REST 调用：`client.api_client.do("POST",
  f"/serving-endpoints/{name}/invocations", body={"input": [...]})`，自己解析
  `output` 字段。
- 部署产物、配置全部走 Databricks CLI / Asset Bundle(`databricks bundle deploy`),不要用一次性手工点选部署,保证可重复、可回滚。
- **【v2 硬性规则】对于 `apps` 类型的资源，`databricks bundle deploy` 只上传代码/注册
  资源定义，不会启动 App**。必须额外执行 `databricks bundle run <app_resource_key>`
  才会真正启动 App 的 compute 并部署代码上去。验收标准是用
  `client.apps.get(app_name).app_status.state == "RUNNING"` 且
  `compute_status.state == "ACTIVE"`，不能只看 `bundle deploy` 命令本身返回成功。
- 部署完成后,用真实的多跳 case 走一遍完整链路(user 提问 → 聊天框出结果),确认端到端可用。**如果这一步发现"本地能跑通、部署后跑不通"的差异（尤其是 Genie 查表报权限错误），大概率是 Serving Endpoint 自身身份和本地开发者 token 权限不一致，需要专门排查 Serving Endpoint/App 的 service principal 对 Genie Space 和底层 UC 表的授权，不要假设两边权限环境是一致的。**

---

## 5. 代码规范与工程原则

- **只实现目标必须的功能**,不要因为"以后可能用得上"而添加：不做 A2A、不做多语言 UI、不做自定义 embedding 模型训练、不做除本项目描述之外的任何数据源接入、不做除 `router` 循环上限之外的额外 guardrails(内容安全过滤、PII 脱敏、prompt injection 防护等如未来另有需求再加,现在不做,但代码结构不要因此写死到无法后续插入的程度)。
- **合理复用,不为复用而复用**:`structured_agent` 和 `unstructured_agent` 如果调用模式(错误处理、重试、超时)高度一致,可以抽一个公共的"tool 调用包装函数",但不要为了"看起来优雅"强行抽象出不必要的基类/接口层。两个 agent 节点本身的业务逻辑(调 Genie vs 调 Vector Search)保持独立实现,不要合并成一个参数化的通用节点 —— 会牺牲可读性换来的复用价值不大。
- 目录结构建议(可根据实际需要微调,但保持清晰的职责分层):
  ```
  SALESDUO/
  ├── .env.example
  ├── CLAUDE.md
  ├── CLAUDE_v1.md                   # 历史版本，供对比差异
  ├── DEVELOPMENT_JOURNAL.md         # 开发复盘/学习笔记
  ├── documents_generated/          # 已有的原始文档，只读，不要修改
  ├── src/
  │   ├── db_client.py              # Databricks SDK 认证统一封装
  │   ├── setup/                    # 建仓相关脚本（UC Function、Genie配置、文档解析建索引、模型部署）
  │   ├── tools/                    # 运行时节点调用的外部服务封装（Genie client、retriever）
  │   ├── graph/                    # LangGraph 节点与图定义
  │   ├── agent.py                  # ResponsesAgent 包装
  │   └── config.py                 # 统一读取环境变量，全项目唯一的 env 读取入口
  ├── tests/
  │   └── eval/                     # 带 ground truth 的评测集 + 自动跑批脚本（可选但推荐）
  ├── chat.py                       # 本地交互式调试用 CLI（可选但推荐）
  ├── databricks.yml                # Asset Bundle 配置
  └── app/                          # Databricks Apps 入口（自己的轻量 requirements.txt，不要跟主项目共用重依赖）
  ```
- 遇到不确定的实现细节(比如具体规则数值、chunk 大小、top-k 数量等),按"先读文档 / 先用行业常见默认值实现完整闭环 / 优先保证端到端能跑通"的优先级自行决定,**不要中途停下来询问**,决定后在对应代码注释里简要说明选择依据即可。
- 全程只在 `SALESDUO/` 目录内操作文件系统。

---

## 6. Databricks 资源命名规范

所有新建的 workspace 资源(schema、Genie Space、Vector Search endpoint/index、Delta 表、UC Function、App)统一加前缀 `salesduo_`,避免和其他项目资源混淆,例如:`salesduo_agent_tools`(UC Function schema)、`salesduo_docs_chunks`(Delta 表)、`salesduo-vs-endpoint`(Vector Search endpoint)、`salesduo-agent`(Databricks App 名)。

---

## 7. 已知风险点(实现时提前规避,不要等踩坑后再补)

1. UC Function 作为 agent 工具执行需要 **serverless generic compute**(不是 SQL warehouse),如果 workspace 未开启,调用会报权限类错误 —— 建仓阶段先确认这项已开启。**【v2 备注】本项目实际用的是 SQL Function（不是 Python UC Function），执行走的是 SQL Warehouse，没有触发这条风险；如果选择 Python UC Function 实现规则计算，需要单独确认这一项。**
2. Genie 多轮对话必须复用 `conversation_id`,否则跨轮上下文会丢失。
3. Router 存在判断错误导致无限循环的风险,`MAX_ROUTER_LOOPS` 必须落地并测试触发路径(故意构造一个会持续判断"信息不够"的 case,确认第 5 次强制走 `finalize` 且不报错)。
4. Vector Search 的 Delta Sync Index 依赖源 Delta 表,不能直接对原始 docx 建索引 —— 必须先落表。
5. **【v2 新增】Genie Space 的 `serialized_space` 配置是不透明的内部 JSON 格式，没有
   公开的字段级 API 文档**。第一次配置某个新字段（比如本项目的 Instructions 文本）时，
   如果 API 直接写入失败或行为反常，靠"猜字段名"效率很低——正确做法是先在 UI 里手动配置
   一次、保存，再用 `get_space(include_serialized_space=True)` 读出真实存的 JSON 结构，
   照着这个真实结构去改，而不是照搬网上找到的示例（不同 Databricks 版本/部署的字段结构
   可能不一致）。
6. **【v2 新增】UC SQL Function 的 `CREATE OR REPLACE FUNCTION` 语句不会在创建时校验
   `RETURN` 表达式的类型是否真的匹配 `RETURNS` 声明**——多列 `SELECT` 配合
   `RETURNS STRUCT<...>` 这种不匹配的写法，可能在创建时不报错，只有真正被调用时才报
   `SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN`。不要用"CREATE 语句返回成功"
   作为函数可用的证据，必须实际调用一次验证。
7. **【v2 新增】Genie 的 NL2SQL 生成本质上是非确定性的**——同一类业务问题，不同轮次的
   对话可能生成不同写法（甚至不同错误方式）的 SQL（本项目实测遇到过：跳过中间表的错误
   JOIN、`DATE_TRUNC`/`DATEDIFF` 引号用法搞反、把标量函数当表函数调用三种不同错误）。
   应对方式是持续通过 Instructions 文本给出更具体的纠正指引（表关联路径、时间函数用法、
   函数类型说明），但不要期望能穷举式地"修完"所有可能的生成错误，评测通过率会有正常波动。
8. **【v2 新增】本地跑 `databricks` CLI 命令（`bundle validate`/`bundle deploy`）时，
   CLI 不会读取项目的 `.env` 文件**，需要在当前 shell 里单独 `export
   DATABRICKS_HOST`/`DATABRICKS_TOKEN`，或者配置 `~/.databrickscfg` profile。
9. **【v2 新增】mlflow 从本地环境（非 Databricks Notebook/Job）注册模型/建实验时，
   默认会写到本地文件（`mlflow.db`/`mlruns/`），而不是真正的 Databricks workspace**，
   除非显式 `set_tracking_uri("databricks")` + `set_registry_uri("databricks-uc")`。
   这个坑的隐蔽之处在于：本地"注册成功"的日志是真的，只是注册到了错误的地方，不会有任何
   报错提示你配错了。
10. **【v2 新增，未解决】Model Serving Endpoint 运行时调用 Genie 查询底层 UC 表，可能
    遇到 `PERMISSION_DENIED` / `TABLES_MISSING_EXCEPTION`，即使本地用个人 token 测试
    完全正常**。已知这跟 Serving Endpoint 使用的身份/服务主体与本地开发者身份不同有关，
    已尝试对 `account users` 组授予 UC 表的 `SELECT`/`USE SCHEMA` 权限、以及 Genie
    Space 的 `CAN_RUN` 权限，均未能解决。建仓阶段应该提前规划一个"用非个人身份实测调用
    Genie 是否能访问底层表"的验证步骤（而不是等部署上线才发现），必要时准备好联系
    Databricks 支持或深入排查 workspace 的身份权限模型。

---

## 8. 版本变更说明（v1 → v2）

v2 相对 v1 的修改，全部来自一次完整实现+部署过程中的真实经验，详细的排查过程见
`DEVELOPMENT_JOURNAL.md`（对应章节在括号里标出）：

| 变更 | 对应真实问题 |
|---|---|
| 架构设计原则新增第 5 条：`AgentState` 必须有累加式 `trace` 字段 | 多跳场景中间结果被覆盖，出问题无法诊断；部署后完全黑盒（Journal 案例 11、技术方法 8） |
| Step 2：UC SQL Function 返回多字段必须用 `STRUCT()` 包裹 + 建完必须用 `information_schema` 验证 | UC Function 从建仓起就从未真正创建成功，直到评测阶段才发现（Journal 案例 2） |
| Step 2：删除"去 UI 找挂载工具入口"的要求，改为"全限定名+字段名写进 instructions 文本" | 花了大量时间找一个不存在的 UI 功能（Journal 案例 1） |
| Step 2：Instructions 文本新增表关联路径、时间函数用法、标量函数调用方式三条具体提示 | Genie 生成 SQL 的三种不同错误（Journal 案例 3） |
| Step 3：切块默认排除 key-value 元数据表；默认 `top_k=8` | 向量检索漏检真正相关的段落（Journal 案例 4） |
| Step 3：新增"索引建完必须用真实问题验证 top-k 命中"的硬性验收标准 | 同上，且发现"索引 ready"不等于"检索质量够用" |
| Step 4：新增"外部服务调用必须捕获具体失败原因写入 trace，禁止裸抛异常"的硬性规则 | Genie 调用失败时原来的代码会丢弃具体报错（Journal 案例 11 的排查过程） |
| Step 4：新增"结构化输出 LLM 调用必须加重试+安全降级"的硬性规则 | 路由模型在 reason 较长时会复现地生成格式错误的 JSON（Journal 案例 5） |
| Step 5：新增"trace 必须通过 custom_outputs 暴露"的硬性规则 | 同 trace 相关问题，部署后需要能看到内部执行细节 |
| Step 7：新增 mlflow tracking/registry URI、code_paths、Azure SDK 依赖、Serving Endpoint environment_vars、REST 调用解析响应、`bundle run` 六条硬性规则 | 部署阶段连续遇到的六个独立问题（Journal 案例 6-10、12） |
| Step 7：新增"trial workspace 不支持 Model Serving"的前提提醒 | 第一次尝试部署时被硬性阻塞，升级 Premium 后才解决 |
| 第 7 节风险点新增 5-10 共六条 | 汇总以上所有新发现的风险 |
| 目录结构建议新增 `CLAUDE_v1.md`、`DEVELOPMENT_JOURNAL.md`、`tests/eval/`、`chat.py` | 本次复盘和评测体系产出的实际文件 |

**v2 没有回答的问题**（如实标注，不是遗漏）：第 7 节风险点 10（Serving Endpoint 下 Genie
权限问题）目前**没有解决方案**，只有"提前规划验证步骤"这条流程建议——如果你是下一个接手
这个项目的人，这是最值得优先攻克的已知缺口。
