# 代码审查记录（2026-07-27）

一次针对 `src/` 现有实现的批判性复查,由用户要求"列举所有不合理的地方"触发。按严重程度排列,
每条记录现象、根因、计划的修复方式(或延后原因)。

**当前状态:仅记录,代码未改动**——用户明确要求"只保存 md 文件,不修改代码",下面每条的
"计划修复"部分是设计方案,不是已落地的变更。以后如果要动手实现,直接照这份文档执行即可。

**路径提醒**:下面引用的文件路径(`src/tools/`、`src/setup/` 等)是本次审查当时的真实路径,
反映的是目录重构前的结构,本文档不做追溯性修改。重构后的当前路径见
`docs/REPOSITORY_STRUCTURE.md`。

---

## 1.【待修复】聊天框的"多轮对话"是假的,每句话对 Genie 来说都是全新会话

**现象**:`app/app.py` 每次只把当前这一句 `question` 发给后端,不带历史;`agent.py` 的
`_run_graph` 也没有任何字段能接住"上一轮请求返回的 `genie_conversation_id`"——每次
`predict()` 调用都是全新的 `loop_count: 0`、`genie_conversation_id=None`。CLAUDE.md
设计原则 3 要求的"同一用户请求内第二次调 Genie 复用 conversation_id"只在单次 HTTP 请求内部
的 router 循环里做到了,聊天框层面的跨轮记忆完全没实现,UI 却显示历史消息制造了"有记忆"的假象。

补充:本地调试用的 `chat.py`(根目录)其实是对的——它是一个常驻进程,`genie_conversation_id`
是循环体外的普通局部变量,天然跨轮持续([chat.py:21](chat.py#L21)、[chat.py:53](chat.py#L53))。
问题只出在"每次 HTTP 请求都是无状态的新进程调用"这个部署形态下,`agent.py`/`app.py` 没有
把 `chat.py` 里"局部变量"起的作用换成等价的跨请求传递机制。

**计划修复**:利用 MLflow `ResponsesAgentRequest.custom_inputs` / `ResponsesAgentResponse.
custom_outputs`(两者都是预留的自定义字段透传通道)做跨请求状态回传:
- `src/agent.py`:`predict`/`predict_stream` 从 `request.custom_inputs` 读取上一轮的
  `genie_conversation_id` 塞进 `initial_state`;返回时把本轮最终的 `genie_conversation_id`
  一并放进 `custom_outputs`。
- `app/app.py`:`st.session_state` 新增 `genie_conversation_id` 字段,发请求时带上
  `custom_inputs`,收到响应后从 `custom_outputs` 里取出来存回 session,供下一轮使用。

---

## 2.【待修复】requirements.txt 没有区分"运行时依赖"和"建仓脚本依赖"

**现象**:`deploy_model.py` 把根目录 `requirements.txt` 整份传给
`mlflow.pyfunc.log_model(pip_requirements=...)`。这份文件混了 `databricks-connect`
(本地开发连接用)、`azure-core`/`azure-storage-file-datalake`(仅 `ingest_docs.py` 上传
文件用)、`python-docx`(仅 `chunk_docs.py` 用)、`pytest`(测试专用)——真正运行时的
`agent.py → graph/*.py → tools/*.py` 一个都用不到,却被打进每次部署的 serving 容器镜像。

**计划修复**:新增 `requirements-runtime.txt`,只列运行时代码实际 import 的包(逐个核对
`src/agent.py`、`src/config.py`、`src/db_client.py`、`src/graph/*.py`、`src/tools/*.py`
的 import 语句得出);根目录 `requirements.txt` 改为 `-r requirements-runtime.txt` +
建仓/测试专用的补充依赖,避免重复维护两份版本号;`deploy_model.py` 的
`_REQUIREMENTS_FILE` 改指向 `requirements-runtime.txt`。

---

## 3.【待修复】unstructured_agent 在多跳场景里,检索用的 query 永远不变

**现象**:`unstructured_agent.py` 里 `query = state.get("user_query", "")`,不管这是第几次
被路由进来、不管 router 这次为什么又把它派过来(`router_reason` 完全没被用上),检索字符串
跟第一次一模一样。对比 `structured_agent.py` 的 `_build_question` 会把已有的 `credit_info`
拼进问题里,两个节点"根据已有信息调整下一步"的实现深度不对称——第二次被路由回
unstructured 时,大概率检索到跟第一次相同的 chunk,查不到新信息,容易在 router 判断"信息还
不够"和"检索不到新东西"之间空转,直到撞上 `MAX_ROUTER_LOOPS`。

**计划修复**:`unstructured_agent.py` 新增 `_build_query`,把 `router_reason`(router 这次为什么
又派过来)和已有的 `structured_result`(如果有)拼进检索文本,让第二次检索的 query 实际上
携带了"这次具体还缺什么"的信息,而不是重复第一次的原始问题。

---

## 4.【待修复】没有整体请求超时,只有循环次数上限

**现象**:Genie 单次调用最多轮询 300 秒(`genie_client.py` 的 `_POLL_TIMEOUT_SECONDS`),
`MAX_ROUTER_LOOPS=5` 意味着最坏情况一次用户提问要连续调 5 次 Genie/Vector Search——理论
上界能拖到 25 分钟才触发 `finalize`。整条链路都没有比这更短的总耗时上限。

**计划修复**:比照 `loop_count` 安全阀的思路,新增一个墙钟时间预算:
- `src/config.py` 新增 `MAX_TOTAL_SECONDS`(默认 240 秒)。
- `src/graph/state.py` 的 `AgentState` 新增 `started_at: float | None`。
- `src/agent.py` 在构造 `initial_state` 时写入 `started_at = time.time()`。
- `src/graph/router.py` 在检查 `loop_count` 上限的同一处,一并检查
  `time.time() - started_at >= MAX_TOTAL_SECONDS`,命中则和 loop_count 超限一样强制走
  `finalize` 并记录原因,不引入新的失败路径。
- 兼容旧行为:`started_at` 缺失时(比如现有离线单测手写的 state 没有这个字段)不做时间检查,
  只检查 loop_count,不影响 `tests/test_router_loop_limit.py` 现有用例。

---

## 5.【待修复】retriever.py 的默认参数是一个"已知验证过错误的值"

**现象**:`retrieve(query, k=5)` 默认值是 5,但 CLAUDE.md 和 DEVELOPMENT_JOURNAL 都记录了
"实测 top_k=5 会漏检目标段落,必须用 8"。现在唯一调用方 `unstructured_agent.py` 传了
`_TOP_K = 8` 覆盖掉了默认值,暂时没暴露问题,但这个默认值本身是过时且验证过有问题的,未来
新增调用方(评测脚本、debug CLI)一旦漏传 `k` 会静默退化。

**计划修复**:把 `retriever.py` 里 `retrieve()` 的默认值从 5 改成 8,和验证过的正确值保持一致;
`unstructured_agent.py` 里 `_TOP_K = 8` 的显式声明予以保留(可读性更好,不依赖隐式默认值),
但默认值本身不再是一个"陷阱"。

---

## 6.【延后,不修】src/graph/ 目录混装了"节点实现"和"编排骨架"两种粒度的文件

`router.py`/`build_graph.py`/`state.py`/`finalize.py`(编排骨架)和
`structured_agent.py`/`unstructured_agent.py`(具体节点实现)放在同一层,没有子目录区分。

**为什么不改**:这是"值得商榷的取舍",不是缺陷——项目现在只有 4 个节点、2 个外部工具,拆
`graph/nodes/` 子目录只会增加跨文件跳转成本,收益不明显。等第三个数据源/节点加入时再拆更
合理。本次不做改动,记录在案供以后参考。

---

## 7.【待修复】ingest_docs.py 手工拼接转义 SQL,不是参数化查询

**现象**:`create_and_populate_delta_table` 用 `_escape()` 手动转义单引号后,把文档切块内容
直接拼进 `INSERT ... VALUES (...)` 语句字符串里。当前输入源可信(本地 docx 解析出的文本),
不构成真实注入风险,但手工转义只处理了单引号,是脆弱的手工安全实现。

**计划修复**:`sql_utils.run_statement` 新增可选的 `parameters` 参数,透传给 SDK
`execute_statement(parameters=...)`;`ingest_docs.py` 的批量 INSERT 改用命名参数占位符
(`:content_i` 等)+ `StatementParameterListItem` 列表传值,不再手工拼接/转义字符串。

`setup_uc_functions.py` 里 `.format(catalog=..., schema=...)` 拼 catalog/schema 名称的
写法保留不改——这两个是 SQL 标识符(表名/schema 名的一部分),不是数据值,大多数 SQL 方言
的参数化查询本来就不支持给标识符做参数绑定,且来源是受信任的环境变量,不是自由文本输入。
