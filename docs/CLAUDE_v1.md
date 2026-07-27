# SALESDUO — AdventureWorks 结构化+非结构化数据 Agent

## 0. 一句话目标

基于 Databricks 生态,构建一个能同时查询 **结构化数据**(Unity Catalog 里的 AdventureWorksLT 表)和 **非结构化数据**(`documents_generated/` 下的公司文档)的对话式 Agent,最终以一个简单的聊天框 UI(Databricks Apps)交付给用户。查询模式不是简单的并列查询汇总,而是**动态多跳**:例如先查非结构化文档得到用户 credit 信息 → 结合业务规则计算 → 再决定要不要查结构化数据 → 结果可能还需要回头再查一次非结构化数据。跳数和顺序**不固定**,由模型在运行时动态决定。

---

## 1. 现状与前提

- Unity Catalog 里已有目录 `adventureworks_dataagent`,包含 schema:`humanresources`、`information_schema`、`person`、`production`、`purchasing`、`sales`。**默认复用这个已有目录**,不要新建 catalog,除非发现表结构不满足需求。
- 仓库根目录下只有 `documents_generated/` 文件夹,里面是两份 `.docx` 公司文件(非结构化数据源,大概率是信用/业务规则类文档)。这两份文档需要被解析、切块、建索引。
- 开发全程在本地 IDE(Claude Code)中进行,通过 Databricks SDK / Databricks CLI / Databricks Connect 连接 workspace,不依赖手工在 Web UI 里点选。
- **只允许在 `SALESDUO/` 当前项目目录内创建/修改文件**。不要修改、访问、或探测这个目录之外的任何本地路径。对 Databricks workspace 侧的资源(catalog/schema/index/endpoint/app)有创建权限,但命名必须清晰打上项目前缀(见第 6 节),不要污染其他项目的资源命名空间。

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
2. **不要设计成"dispatcher 节点 + summarizer 节点"两段式固定结构**——分任务和总结/再分任务是同一个决策动作的反复发生,必须用**同一个 router 节点 + 条件边(conditional edge)+ 循环边**来实现,而不是两个角色互相调用。
3. Genie 是**有状态的多轮对话**(靠 `conversation_id` 维持上下文)。如果流程里同一个用户请求触发了对 Genie 的第二次调用,必须复用同一个 `conversation_id`,不要每次开新会话。
4. 必须设置 **`loop_count` 上限**(路由循环次数上限),防止 router 判断错误导致的无限循环。超过上限时强制走 `finalize`,并在最终回答中注明信息可能不完整。

---

## 3. 环境变量(唯一配置来源)

**所有可变参数一律走环境变量,代码中不允许出现任何硬编码的 catalog 名、schema 名、endpoint 名、workspace URL、token 等。** 在项目根目录维护一个 `.env.example`(不含真实值,仅做字段说明),真实值放本地 `.env`(需加入 `.gitignore`)。

参考以下变量(如实现过程中确有必要新增,遵循同样的命名规范,并同步补充到 `.env.example`,不要私自新增未使用到的占位变量，不要过多定义变量，只定义完成目标要用的):

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

---

## 4. 实施步骤(严格按顺序执行,每步完成后自行验证再进入下一步,不要跳步,也不要在中途停下来问我)

### Step 1 — 环境与认证

- 用 Databricks SDK(`databricks-sdk`)读取环境变量完成认证,写一个 `src/db_client.py` 里统一封装 `get_workspace_client()`,全项目其他地方都从这里拿 client,不要在多处重复初始化认证逻辑。
- 验证连通性:能列出 `UC_CATALOG` 下的 schema。

### Step 2 — 结构化数据侧:UC Function + Genie Agent

- 检查 `UC_FUNCTION_SCHEMA` 指定的 schema 是否存在,不存在则创建(用 SDK,不要手工建)。
- 用 UC Function(SQL 或 Python)实现业务规则计算逻辑(具体规则以 `documents_generated/` 文档内容为准 —— 先解析文档确认规则细节,再决定 UC Function 的参数和返回结构;不要在没读文档前臆造规则)。
- 用 Databricks SDK 创建/配置 Genie Agent(原 Genie Space),数据源指向 AdventureWorksLT 相关 schema(`sales`/`person`/`production` 等,按实际问题需要选择,不要不加选择地全部挂上),并把上面的 UC Function 挂载为 Genie 的工具(Instructions/Functions 配置),让 Genie 在需要计算时调用函数而不是要求它自己拼 SQL 硬算。
- 记录 `GENIE_SPACE_ID` 到环境变量。

### Step 3 — 非结构化数据侧:文档解析 → Delta 表 → Vector Search

- 解析 `documents_generated/` 下的两份 docx,切块(chunk),写入 `DELTA_TABLE_DOCS_CHUNKS` 指定的 Delta 表(至少包含:chunk_id、文本内容、来源文件名、chunk 顺序)。
- 用 Databricks SDK 创建 Vector Search endpoint(如不存在)和 Delta Sync Index,embedding 走 `EMBEDDING_MODEL_ENDPOINT`。
- 写一个轻量的 retriever 封装(输入 query,返回 top-k 相关 chunk),作为 `unstructured_agent` 节点内部调用的工具。不需要额外包装成 Knowledge Assistant(no-code 产品),直接代码里调 Vector Search 查询接口即可 —— 这是代码优先路径,不必绕经 no-code 层。

### Step 4 — 编排:LangGraph StateGraph

- 按第 2 节的架构图实现:`router`(条件判断)、`structured_agent`(调 Genie)、`unstructured_agent`(调 Vector Search)、`finalize`(综合输出)四个节点,`router` 用带循环的条件边连接。
- State 至少包含:`messages`、`user_query`、`credit_info`、`business_rule_result`、`structured_result`、`genie_conversation_id`、`loop_count`、`next_step`。
- `router` 节点每次判断前必须检查 `loop_count`,超过 `MAX_ROUTER_LOOPS` 强制走 `finalize`。
- `router` 的输出必须走结构化输出(强制枚举 `next_step` 取值),不要靠解析自由文本判断下一步,避免路由解析出错。

### Step 5 — 包装为 MLflow ResponsesAgent

- 实现 `predict`/`predict_stream`,内部调用 `graph.invoke()`/`graph.stream()`,输入输出严格走标准 schema。
- 配置 `MLFLOW_EXPERIMENT_PATH`,确保每次调用自动产生 trace(结构化/非结构化各节点的调用、耗时、中间结果都应可在 trace 里查到)。

### Step 6 — 本地验证

- 至少覆盖以下三类测试 case,且必须包含"先非结构化 → 结构化 → 再非结构化"这种多跳 case(这是本项目区别于简单并列查询的核心难点,必须重点验证 router 判断是否正确、循环终止是否正常触发、`loop_count` 兜底是否生效):
  1. 只需结构化数据的问题
  2. 只需非结构化数据的问题
  3. 需要多跳(非结构化→计算→结构化→可能再非结构化)的问题
- 用 Agent Evaluation 跑一遍上述测试集,记录基本的正确性/相关性指标。不需要现在就搭建复杂的评测体系,能验证核心路由逻辑正确即可。

### Step 7 — 部署

- 用 Databricks Apps 部署(不要用裸 Model Serving,因为最终交付形态是聊天框 UI,Apps 自带基础 chat 界面骨架,更贴合需求;不要额外自建前端框架,除非 Databricks Apps 默认模板明显无法满足"简单聊天框"这个最低要求)。
- 部署产物、配置全部走 Databricks CLI / Asset Bundle(`databricks bundle deploy`),不要用一次性手工点选部署,保证可重复、可回滚。
- 部署完成后,用真实的多跳 case 走一遍完整链路(user 提问 → 聊天框出结果),确认端到端可用。

---

## 5. 代码规范与工程原则

- **只实现目标必须的功能**,不要因为"以后可能用得上"而添加：不做 A2A、不做多语言 UI、不做自定义 embedding 模型训练、不做除本项目描述之外的任何数据源接入、不做除 `router` 循环上限之外的额外 guardrails(内容安全过滤、PII 脱敏、prompt injection 防护等如未来另有需求再加,现在不做,但代码结构不要因此写死到无法后续插入的程度)。
- **合理复用,不为复用而复用**:`structured_agent` 和 `unstructured_agent` 如果调用模式(错误处理、重试、超时)高度一致,可以抽一个公共的"tool 调用包装函数",但不要为了"看起来优雅"强行抽象出不必要的基类/接口层。两个 agent 节点本身的业务逻辑(调 Genie vs 调 Vector Search)保持独立实现,不要合并成一个参数化的通用节点 —— 会牺牲可读性换来的复用价值不大。
- 目录结构建议(可根据实际需要微调,但保持清晰的职责分层):
  ```
  SALESDUO/
  ├── .env.example
  ├── CLAUDE.md
  ├── documents_generated/          # 已有的原始文档，只读，不要修改
  ├── src/
  │   ├── db_client.py              # Databricks SDK 认证统一封装
  │   ├── setup/                    # 建仓相关脚本（UC Function、Genie配置、文档解析建索引）
  │   ├── graph/                    # LangGraph 节点与图定义
  │   ├── agent.py                  # ResponsesAgent 包装
  │   └── config.py                 # 统一读取环境变量，全项目唯一的 env 读取入口
  ├── tests/
  ├── databricks.yml                # Asset Bundle 配置
  └── app.py / app 相关文件          # Databricks Apps 入口
  ```
- 遇到不确定的实现细节(比如具体规则数值、chunk 大小、top-k 数量等),按"先读文档 / 先用行业常见默认值实现完整闭环 / 优先保证端到端能跑通"的优先级自行决定,**不要中途停下来询问**,决定后在对应代码注释里简要说明选择依据即可。
- 全程只在 `SALESDUO/` 目录内操作文件系统。

---

## 6. Databricks 资源命名规范

所有新建的 workspace 资源(schema、Genie Space、Vector Search endpoint/index、Delta 表、UC Function、App)统一加前缀 `salesduo_`,避免和其他项目资源混淆,例如:`salesduo_agent_tools`(UC Function schema)、`salesduo_docs_chunks`(Delta 表)、`salesduo-vs-endpoint`(Vector Search endpoint)、`salesduo-agent`(Databricks App 名)。

---

## 7. 已知风险点(实现时提前规避,不要等踩坑后再补)

1. UC Function 作为 agent 工具执行需要 **serverless generic compute**(不是 SQL warehouse),如果 workspace 未开启,调用会报权限类错误 —— 建仓阶段先确认这项已开启。
2. Genie 多轮对话必须复用 `conversation_id`,否则跨轮上下文会丢失。
3. Router 存在判断错误导致无限循环的风险,`MAX_ROUTER_LOOPS` 必须落地并测试触发路径(故意构造一个会持续判断"信息不够"的 case,确认第 5 次强制走 `finalize` 且不报错)。
4. Vector Search 的 Delta Sync Index 依赖源 Delta 表,不能直接对原始 docx 建索引 —— 必须先落表。