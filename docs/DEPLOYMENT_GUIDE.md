# 部署手册：从零到线上 App

> 本文档假定 Unity Catalog 里已有 `adventureworks_dataagent` 目录（项目复用现有目录，不
> 新建），面向"仓库代码已经写好，要在一个新 workspace / 或重建当前 workspace 资源"的场景，
> 按实际依赖顺序逐步给出：跑哪个文件、为什么跑这一步、预期看到什么结果、大概要多久。每步
> 都可以直接照着"执行方式"栏复制命令。文末附一份精简版问题排查记录，完整版见
> `docs/DEVELOPMENT_JOURNAL.md`（开发过程全记录）和 `docs/VERIFICATION_2026-07-27.md`
> （认证切换后的重跑验证记录，含更细的耗时拆解）。

---

## 0. 前置准备

| 项目 | 说明 |
|---|---|
| Azure CLI | 本机需要安装并执行过 `az login`——本项目认证方式是 Azure 原生认证（不用 PAT），`databricks-sdk` 靠这个登录会话鉴权。 |
| Python 环境 | `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`（装的是本地开发全量依赖，不是部署给 Serving Endpoint 的那份）。 |
| `.env` | 复制 `.env.example` 为 `.env`，填好 `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME`（Azure 资源坐标，SDK 用它们解析出 workspace host）以及 `SQL_WAREHOUSE_ID`（下面所有步骤都要用）。其余变量(`GENIE_SPACE_ID` 等)会在对应步骤里产生，先留空。 |
| Databricks CLI | `brew install databricks-cli` 或官方安装脚本，Step 7 部署 App 要用。 |

预计耗时：10-20 分钟（大部分是环境安装，因人而异）。

---

## Step 1：验证连接

| | |
|---|---|
| **文件** | [ops/verify_connection.py](../ops/verify_connection.py) |
| **目的** | 确认 Azure CLI 认证生效、能连上 workspace、`UC_CATALOG` 这个目录确实存在且有权限访问。后面所有步骤都建立在"这一步先通过"的前提上。 |
| **执行方式** | `python -m ops.verify_connection` |
| **预期结果** | 打印 `UC_CATALOG` 下的全部 schema 名（目前是 7 个：`humanresources`/`information_schema`/`person`/`production`/`purchasing`/`sales`/`salesduo_agent_tools`）。 |
| **预计耗时** | 几秒 |

---

## Step 2：结构化数据侧建仓

### 2a. 建 UC Function

| | |
|---|---|
| **文件** | [ops/structured/setup_uc_functions.py](../ops/structured/setup_uc_functions.py) + [ops/structured/sql/](../ops/structured/sql/)（两个 `.sql` 模板） |
| **目的** | 建 `calculate_credit_terms`（信用条款计算）和 `check_large_transaction_compliance`（大额交易合规校验）两个 UC SQL Function，业务规则来自 `documents_generated/` 的两份政策文档。 |
| **执行方式** | `python -m ops.structured.setup_uc_functions` |
| **预期结果** | 打印"已创建/替换函数: calculate_credit_terms"/"check_large_transaction_compliance"。**光看这行打印不够**——建议紧接着用 SQL 查一次 `information_schema.routines` 确认函数真的存在，再实际调用一次确认返回值结构正确（历史上出现过"CREATE 没报错但函数其实调不通"的坑，见文末问题 1）。 |
| **预计耗时** | 几秒到几十秒（取决于 SQL Warehouse 是不是冷启动） |

### 2b. 创建 Genie Space（唯一必须手动做的一步）

| | |
|---|---|
| **目的** | Genie Space 本身**目前没有公开 API 可以创建**，必须先在 Databricks UI 里手动建一个空的 Genie Space（Genie → New），数据源随便挂几张表占位即可，后面 2c 会覆盖掉。这是平台限制，不是可以绕开的实现选择。 |
| **执行方式** | UI 操作：Databricks workspace 左侧栏 Genie → New Space，起个名字。 |
| **预期结果** | 拿到一个 Genie Space ID（URL 里能看到），填进 `.env` 的 `GENIE_SPACE_ID`。 |
| **预计耗时** | 几分钟 |

### 2c. 配置 Genie（挂表 + 写 instructions）

| | |
|---|---|
| **文件** | [ops/structured/setup_genie.py](../ops/structured/setup_genie.py)（表清单在 `GENIE_TABLES` 变量里显式列出，instructions 文本是 [prompts/genie_instructions.prompt](../prompts/genie_instructions.prompt)） |
| **目的** | 把 `GENIE_TABLES` 里列出的表(person.person + 19 张 sales 表)挂进 2b 建的 Genie Space，同时写入 instructions 文本(表关联路径提示、两个 UC Function 的全限定名和返回字段、时间函数用法提示)——这些提示是从实测踩过的 Genie 生成 SQL 错误里反推出来的(见文末问题 3)。 |
| **执行方式** | `python -m ops.structured.setup_genie` |
| **预期结果** | 打印本次新增了几张表 + 目前共多少张表(从零开始建的话会显示"本次新增 20 张表,目前共 20 张表")。**这一步每次跑都会覆盖 Genie Space 的 instructions 和 sql_functions 字段**（平台限制，见文末问题 2），跑完不需要额外去 UI 补挂什么。 |
| **预计耗时** | 几秒 |

---

## Step 3：非结构化数据侧建仓

### 3a. 建 Vector Search Endpoint

| | |
|---|---|
| **文件** | [ops/rag/setup_vs_endpoint.py](../ops/rag/setup_vs_endpoint.py) |
| **目的** | 建 Vector Search endpoint（长期存在的共享基础设施，一个 endpoint 可以挂多个 index，所以跟下面的建 index 步骤拆开）。 |
| **执行方式** | `python -m ops.rag.setup_vs_endpoint` |
| **预期结果** | 打印"已创建 Vector Search endpoint"。这一步只是**发起创建请求，不等待完成**——需要自己再查一下状态（`client.vector_search_endpoints.get_endpoint(name=...).endpoint_status.state`）确认变成 `ONLINE` 再往下走。 |
| **预计耗时** | 这个 workspace 里**第一次**建 Vector Search endpoint 可能要十几到几十分钟（卡在 `PROVISIONING_ENDPOINT`，不是索引同步慢）；如果之前建过、这次是重建，可能几分钟就好（实测过一次约 1-2 分钟）。不确定是哪种情况就按"可能要等很久"做心理准备。 |

### 3b. 文档解析 + 建索引

| | |
|---|---|
| **文件** | [ops/rag/ingest_docs.py](../ops/rag/ingest_docs.py)（调用 [ops/rag/chunk_docs.py](../ops/rag/chunk_docs.py) 做切块） |
| **目的** | 把 `documents_generated/` 下两份 docx 上传到 UC Volume 存档,解析切块写入 Delta 表,在 3a 建好的 endpoint 上创建 Delta Sync Index。 |
| **执行方式** | `python -m ops.rag.ingest_docs` |
| **前提** | 3a 的 endpoint 必须已经 `ONLINE`,否则建 index 这一步会失败。 |
| **预期结果** | 依次打印:UC Volume 已存在/已创建、两份文档已上传、写入 18 条 chunk、已创建 Vector Search index。**同样只是发起了建 index 的请求**,需要另外轮询 `client.vector_search_indexes.get_index(...).status.ready` 直到变成 `True`——中间会经历"pending endpoint provisioning → pending pipeline resources → syncing initial data → ready"几个阶段。 |
| **预计耗时** | 脚本本身跑完不到 1 分钟；index 从创建到 `ready=True` 另需约 10-15 分钟（异步同步，脚本不等它）。 |
| **索引建完之后**,强烈建议用几个真实业务问题跑一遍 `src/clients/retriever.py::retrieve()`,人工检查 top-k 结果里有没有真正相关的段落——不能只看 `ready=True` 就当完成(见文末问题 4)。 |

---

## Step 4-6：本地验证

### 4a. 离线单测

| | |
|---|---|
| **文件** | [tests/test_chunk_docs.py](../tests/test_chunk_docs.py)、[tests/test_router_loop_limit.py](../tests/test_router_loop_limit.py)、[tests/test_unstructured_agent_query.py](../tests/test_unstructured_agent_query.py)、[tests/test_app_error_handling.py](../tests/test_app_error_handling.py) |
| **目的** | 验证不依赖真实 Databricks 连接的纯逻辑(切块、router 循环上限兜底、多跳检索 query 拼接、App 错误处理),这几个改完代码应该随时能跑、随时该通过。 |
| **执行方式** | `pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py tests/test_unstructured_agent_query.py tests/test_app_error_handling.py -v` |
| **预期结果** | 13 passed |
| **预计耗时** | 几秒 |

### 4b. 端到端集成测试(需要真实连接)

| | |
|---|---|
| **文件** | [tests/test_integration_cases.py](../tests/test_integration_cases.py) |
| **目的** | 真实连 Genie + Vector Search + LLM,覆盖三类场景(纯结构化、纯非结构化、多跳)+ loop 上限兜底 + 跨轮记忆(`genie_conversation_id` 复用、代词指代)。没有配置真实凭据时这个文件里的用例会自动跳过,不会报错。 |
| **执行方式** | `pytest tests/test_integration_cases.py -v` |
| **预期结果** | 5 passed |
| **预计耗时** | 约 2-3 分钟(多次 Genie/LLM 往返) |

### 4c.（可选）交互式手动体验

| | |
|---|---|
| **文件** | [chat.py](../chat.py) |
| **目的** | 本地终端跟 agent 对话,人工感受一下回答质量,不用等部署上线才能测。输入 `/trace` 可以切换显示每一步的白盒追踪(router 判断理由、Genie 生成的 SQL、检索到的片段)。 |
| **执行方式** | `python chat.py` |
| **预计耗时** | 看自己想测多久 |

### 4d.（可选）评测集跑批

| | |
|---|---|
| **文件** | [tests/eval/run_eval.py](../tests/eval/run_eval.py) + [tests/eval/eval_set.json](../tests/eval/eval_set.json) |
| **目的** | 带 ground truth 的题集批量跑 + LLM 裁判打分,结果存进 `tests/eval/results/`(含每题完整 trace,不是只存最终答案)。 |
| **执行方式** | `python -m tests.eval.run_eval` |
| **预期结果** | 分数会有正常波动(Genie 的 NL2SQL 生成本质上非确定性,见文末问题 3),不追求 100%,能定性看出"核心链路能跑通"即可。 |
| **预计耗时** | 题量决定,10 题量级几分钟 |

---

## Step 7：部署

### 7a. 注册模型 + 建/更新 Serving Endpoint

| | |
|---|---|
| **文件** | [ops/deploy_model.py](../ops/deploy_model.py) |
| **目的** | 把 `src/agent.py`(整张 LangGraph 图的 ResponsesAgent 包装)注册成 Unity Catalog 里的一个模型版本,声明它运行时依赖的资源(Genie Space、Vector Search Index、SQL Warehouse、两个 UC Function),Databricks 会据此自动给 Serving Endpoint 授权访问这些资源;然后建/更新 Serving Endpoint 指向这个新版本。 |
| **执行方式** | `python -m ops.deploy_model` |
| **预期结果** | 打印"已注册模型: ... version N" + "已更新 serving endpoint"。脚本内部会等到 `update_config_and_wait`/`create_and_wait` 返回才打印完成,所以看到这行打印时 Serving Endpoint 已经是新版本、状态 `READY`。 |
| **预计耗时** | 打包上传模型 artifact 几秒;Serving Endpoint 滚动更新到新版本另需约 10-15 分钟(平台行为,不可压缩)。 |

### 7b. 部署 App 代码

| | |
|---|---|
| **文件** | [databricks.yml](../databricks.yml)（Asset Bundle 配置） |
| **目的** | 把 `app/` 目录的代码同步到 workspace、注册 App 这个资源的定义。 |
| **预期结果** | `Deployment complete!` |
| **预计耗时** | 几秒到几十秒 |

**执行方式**——必须在跑 `databricks bundle` 命令的这个 shell 里单独 `export`,CLI 是独立
进程,不会继承 Python 进程内部设置的环境变量(见文末问题 7):
```bash
export DATABRICKS_AZURE_RESOURCE_ID=$(python3 -c "
from src.config import settings
print(f'/subscriptions/{settings.azure_subscription_id}/resourceGroups/{settings.azure_resource_group_name}/providers/Microsoft.Databricks/workspaces/{settings.azure_databricks_workspace_name}')
")
databricks bundle deploy
```

### 7c. 启动 App

| | |
|---|---|
| **目的** | `bundle deploy` 只是注册资源定义,**不会启动 App**,必须额外这一步才会真正拉起 compute、把代码部署上去(见文末问题 8)。 |
| **执行方式** | `databricks bundle run salesduo_agent`（同一个 shell,`DATABRICKS_AZURE_RESOURCE_ID` 还生效着） |
| **预期结果** | 一连串"App is starting..."之后 `App started successfully`,给出访问 URL。**不要只信这行文字**——建议再用 SDK 查一次 `client.apps.get(app_name)`,确认 `app_status.state == "RUNNING"` 且 `compute_status.state == "ACTIVE"`。 |
| **预计耗时** | 约 3-4 分钟 |

### 7d. 授权 App 的 service principal（容易漏掉的一步）

| | |
|---|---|
| **文件** | [ops/grant_app_permissions.py](../ops/grant_app_permissions.py) |
| **目的** | Databricks App 部署后会自动生成一个独立的 service principal,这个身份才是 `structured_agent` 调 Genie 查底层表时实际用到的执行身份——不授权的话,结构化查询会在这一步报 `PERMISSION_DENIED`(完整排查过程见文末问题 9,这是本项目踩过的最大一个坑)。授权范围直接从 `ops/structured/setup_genie.py::GENIE_TABLES`/`REQUIRED_FUNCTIONS` 联动读取,不用手动同步维护第二份表清单。 |
| **执行方式** | `python -m ops.grant_app_permissions` |
| **前提** | 必须在 7c App **至少成功启动过一次之后**再跑——service principal 是 App 创建/启动后才有的,提前跑会报"没查到 service_principal_client_id"。 |
| **预期结果** | 打印 App 的 service principal 信息 + 依次执行约 26 条 `GRANT` 语句(`USE CATALOG` + 每个用到的 schema 的 `USE SCHEMA` + 每张表的 `SELECT` + 业务函数所在 schema 的 `USE SCHEMA`+每个函数的 `EXECUTE`)。 |
| **预计耗时** | 几秒 |
| **提醒** | App 每次被删除重建都会拿到一个**新的** service principal,这份授权要跟着重新跑一次,不是对"App 这个名字"永久生效。 |

### 7e. 最终验收

- SDK 确认:`client.serving_endpoints.get(...).state.ready == "READY"`、`client.apps.get(...)` 的 `app_status.state == "RUNNING"` 且 `compute_status.state == "ACTIVE"`。
- 实际打开 7c 给出的 URL,在聊天框里问一个真实的多跳问题(比如"客户 XX 的年采购额和信用额度上限分别是多少"),确认结构化 + 非结构化都能正常返回,不是只有政策类问题能答。

预计耗时:几分钟。

---

## 全流程预计总耗时

去掉可选的 4c/4d,Step 1 到 7e 大概 **50-90 分钟**,大头在三处不可压缩的异步等待上:3a(Vector Search endpoint 首次创建,十几到几十分钟)、3b(index 同步,10-15 分钟)、7a(Serving Endpoint 滚动更新,10-15 分钟)。代码本身的执行时间累计不到 10 分钟。

---

## 附:遇到过的问题与解决方式(精简版,完整过程见 `docs/DEVELOPMENT_JOURNAL.md`)

1. **UC SQL Function 多字段返回,`CREATE` 不报错,调用时报 `SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN`**——`RETURN` 语句返回多列 `SELECT` 必须用 `STRUCT(...)` 包一层,`CREATE OR REPLACE FUNCTION` 本身不校验这个,必须建完实际调用一次才能验证出来。

2. **Genie Space 的 `serialized_space` 是不透明格式,`instructions.sql_functions` 字段无法通过 API 写入**——不管内容是什么都会报 "Certified answer 'xxx' does not exist"。解决:把 UC Function 的全限定名和返回字段直接写进 `instructions.text_instructions` 文本里,Genie 生成 SQL 时就能正确调用,不需要那个失效的"挂载为工具"机制;每次跑 `setup_genie.py` 都要主动把 `sql_functions` 字段摘掉再提交,否则整个 update 失败。

3. **Genie 生成 SQL 的三种典型错误**:跳过中间表直接错误 JOIN、`DATEDIFF`(不加引号)和 `DATE_TRUNC`(加引号)的引号用法搞反、把标量函数当表函数写进 `FROM` 子句。解决:都是通过在 instructions 文本里加具体的纠正提示解决的(见 `prompts/genie_instructions.prompt`),这类问题没有一次性穷举修完的办法,是 Genie NL2SQL 生成本身的非确定性,换个问题措辞可能还会出新的错误写法。

4. **向量检索漏检目标段落**——文档头部的 Policy ID/Effective Date 这类 2 列 key-value 元数据表,在向量检索里相似度分数异常高,会把真正相关的长段落挤出 top-k。解决:切块时直接跳过这类 2 列表格不索引;`top_k` 从 5 调到 8(实测 5 会漏检)。

5. **Router 的 LLM 在 `reason` 字段较长时偶发生成格式非法的 JSON**——强制结构化输出(`with_structured_output`)也会偶发出错。解决:加 3 次重试,连续失败就安全降级为 `finalize` 并注明信息可能不完整,不让整个请求崩溃。

6. **本地跑部署脚本,mlflow 默认注册到本地 SQLite,不是真的 Databricks workspace**——本地"注册成功"的日志是真的,只是注册到了错误的地方,没有任何报错提示。解决:显式 `mlflow.set_tracking_uri("databricks")` + `mlflow.set_registry_uri("databricks-uc")`。

7. **`databricks` CLI 命令不会读取项目的 `.env` 文件,也不会继承 Python 进程内部设置的环境变量**——需要在跑 `databricks bundle` 命令的这个 shell 里单独 `export DATABRICKS_AZURE_RESOURCE_ID=...`(或早期 PAT 时代的 `DATABRICKS_HOST`/`TOKEN`)。

8. **`databricks bundle deploy` 不会自动启动 App**——只上传代码、注册资源定义,必须额外 `databricks bundle run <app_resource_key>` 才会真正启动 compute。验收标准是查 `client.apps.get(...)` 的真实状态字段,不能只看 CLI 打印的文字。

9. **(本项目最大的一个坑)Serving Endpoint/App 调 Genie 查底层表报 `PERMISSION_DENIED`,即使本地个人身份完全正常**——排查了很久,一度以为是某个查不到的"Serving Endpoint 系统身份",试过给 `account users` 组授权、给 Genie Space 加 `CAN_RUN` 权限,均未解决。真正原因:**Databricks App 部署后会自动生成一个独立的 service principal
(`client.apps.get(app_name).service_principal_client_id`),这个身份才是实际执行 Genie
查询用到的身份**,只要用 `ops/grant_app_permissions.py` 给它授权(底层表 `SELECT` + 业务规则函数 `EXECUTE`)就解决了。这个身份是 App 特有的,每次删除重建 App 都要重新跑一次授权脚本。

10. **认证方式从 PAT 切到 Azure CLI 原生认证后,`databricks-ai-search` 包(`VectorSearchClient`)完全不兼容**——这个包的认证逻辑硬编码只认静态 PAT/service principal token,Azure CLI 给的是会自动刷新的动态令牌,该包拿不到就直接报错。解决:整个 Vector Search 相关代码(`retriever.py`/`setup_vs_endpoint.py`/`ingest_docs.py`)迁移到 `databricks-sdk` 自带的 `client.vector_search_indexes`/`client.vector_search_endpoints` 原生 API,跟其他代码走同一套认证,不用再单独处理。

11. **mlflow 自己的凭据解析逻辑也会建一个不带参数的裸 `WorkspaceClient()`**——不止 `databricks-ai-search` 这一个第三方库有这个问题,`mlflow.utils.databricks_utils.get_databricks_host_creds()` 内部同样如此。解决:不能只在自己封装的 `db_client.py` 里处理认证参数,还要把 `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME` 拼成 databricks-sdk 官方认的环境变量 `DATABRICKS_AZURE_RESOURCE_ID` 写回 `os.environ`,这样任何代码路径新建的裸 `WorkspaceClient()` 都能自动认到,不用逐个库去适配。
