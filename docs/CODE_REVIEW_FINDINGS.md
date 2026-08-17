# 代码审查记录（2026-07-27 首次，持续更新）

一次针对 `src/` 现有实现的批判性复查,由用户要求"列举所有不合理的地方"触发。按严重程度排列,
每条记录现象、根因、计划的修复方式(或延后原因)。**这份文档会随着后续每一轮改动持续复查、
更新状态、追加新发现**,标题里的日期是首次创建时间,不代表内容只到那天为止——每条的状态
标注(【已修复,日期】/【待修复】/【延后,不修】)才是当前真实状态的来源。

**路径提醒**:1-7 条引用的文件路径(`src/tools/`、`src/setup/` 等)是 2026-07-27 首次审查
当时的真实路径,反映的是目录重构前的结构,不做追溯性修改;8 条之后新增的发现用的是重构后
的当前路径。统一的当前路径参照 `docs/REPOSITORY_STRUCTURE.md`。

**修复状态总览(最近一次核对:2026-07-29)**:1、2、3、5、8、11 已修复;4、7、9、10 用户
明确决定暂不修(个人练习项目/附加功能/优先级不够,理由见各条);6 延后不修。

---

## 1.【已修复,2026-07-28】聊天框的"多轮对话"是假的,每句话对 Genie 来说都是全新会话

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

**2026-07-28 已按上述方案实现**,而且实现过程中发现"只修 genie_conversation_id 一个字段"
并不够——`router.py`/`finalize.py` 一直都没读过 `state["messages"]`,就算历史正确传过去了
也没人用。完整修复范围、设计取舍、以及验证过程中额外发现的一个"router 有时会跳过重新计算、
直接从历史数值推断"的非确定性风险,写在 `docs/AGENT_MEMORY_DESIGN.md`。对应的可重复运行
用例:`tests/test_integration_cases.py::
test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns`（2 次独立运行,
核心机制 2/2 次生效;曾有 1 次因为 router 判断偏差断言失败,重跑后通过,详见设计文档"一个
刻意加的、但没能完全生效的约束"一节）。

---

## 2.【已修复,2026-07-27 目录重构时一并完成】requirements.txt 没有区分"运行时依赖"和"建仓脚本依赖"

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

**已按上述方案实现**(目录重构那一轮一并做的,见 `docs/REPOSITORY_STRUCTURE.md`):
`requirements-runtime.txt` 现在只有 7 行运行时依赖,`ops/deploy_model.py` 的
`_REQUIREMENTS_FILE` 确认指向它。后续 Vector Search 迁移到原生 SDK 时还顺带把
`databricks-ai-search` 这一行从 `requirements-runtime.txt` 里删掉了(那个包本身也不再
被任何运行时代码 import)。

---

## 3.【已修复,2026-07-28】unstructured_agent 在多跳场景里,检索用的 query 永远不变

**现象**:`unstructured_agent.py` 里 `query = state.get("user_query", "")`,不管这是第几次
被路由进来、不管 router 这次为什么又把它派过来(`router_reason` 完全没被用上),检索字符串
跟第一次一模一样。对比 `structured_agent.py` 的 `_build_question` 会把已有的 `credit_info`
拼进问题里,两个节点"根据已有信息调整下一步"的实现深度不对称——第二次被路由回
unstructured 时,大概率检索到跟第一次相同的 chunk,查不到新信息,容易在 router 判断"信息还
不够"和"检索不到新东西"之间空转,直到撞上 `MAX_ROUTER_LOOPS`。

**计划修复**:`unstructured_agent.py` 新增 `_build_query`,把 `router_reason`(router 这次为什么
又派过来)和已有的 `structured_result`(如果有)拼进检索文本,让第二次检索的 query 实际上
携带了"这次具体还缺什么"的信息,而不是重复第一次的原始问题。

**已按上述方案实现**:[src/graph/unstructured_agent.py](src/graph/unstructured_agent.py) 新增
`_build_query()`,把 `user_query`/`router_reason`/`structured_result` 按顺序拼成检索文本,
`unstructured_agent_node` 从 `_build_query(state)` 取 query,不再直接用 `user_query`。
离线单测(`tests/test_unstructured_agent_query.py`,4 个用例全部通过)见
`tests/eval/results/items_3_8_verification_20260728.md`;没有做额外的实时检索质量对比
(需要真的触发多跳场景才能观察到区别,不在这次离线验证范围内)。

---

## 4.【用户决定暂不修,2026-07-28】没有整体请求超时,只有循环次数上限

**用户决定**:个人练习项目,没有真实用户在用,不考虑这个风险,暂不修。

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

## 5.【已修复,2026-07-28】retriever.py 的默认参数是一个"已知验证过错误的值"

**现象**:`retrieve(query, k=5)` 默认值是 5,但 CLAUDE.md 和 DEVELOPMENT_JOURNAL 都记录了
"实测 top_k=5 会漏检目标段落,必须用 8"。现在唯一调用方 `unstructured_agent.py` 传了
`_TOP_K = 8` 覆盖掉了默认值,暂时没暴露问题,但这个默认值本身是过时且验证过有问题的,未来
新增调用方(评测脚本、debug CLI)一旦漏传 `k` 会静默退化。

**已修复**:[src/clients/retriever.py](src/clients/retriever.py) 的 `retrieve()` 默认值
从 5 改成了 8。`unstructured_agent.py` 里 `_TOP_K = 8` 的显式声明保留不动(可读性更好,
不依赖隐式默认值),这次只是把默认值本身的"陷阱"拆除,行为没有变化,import 验证通过,
没有做额外的实时检索验证(不需要——这个改动不改变任何现有调用方的实际参数值)。

---

## 6.【延后,不修】src/graph/ 目录混装了"节点实现"和"编排骨架"两种粒度的文件

`router.py`/`build_graph.py`/`state.py`/`finalize.py`(编排骨架)和
`structured_agent.py`/`unstructured_agent.py`(具体节点实现)放在同一层,没有子目录区分。

**为什么不改**:这是"值得商榷的取舍",不是缺陷——项目现在只有 4 个节点、2 个外部工具,拆
`graph/nodes/` 子目录只会增加跨文件跳转成本,收益不明显。等第三个数据源/节点加入时再拆更
合理。本次不做改动,记录在案供以后参考。

---

## 7.【用户决定暂不修,2026-07-28】ingest_docs.py 手工拼接转义 SQL,不是参数化查询

**用户决定**:个人练习项目,输入源可信,不构成真实注入风险,暂不修。

**现象**:`create_and_populate_delta_table` 用 `_escape()` 手动转义单引号后,把文档切块内容
直接拼进 `INSERT ... VALUES (...)` 语句字符串里。当前输入源可信(本地 docx 解析出的文本),
不构成真实注入风险,但手工转义只处理了单引号,是脆弱的手工安全实现。

**计划修复**:`sql_utils.run_statement` 新增可选的 `parameters` 参数,透传给 SDK
`execute_statement(parameters=...)`;`ingest_docs.py` 的批量 INSERT 改用命名参数占位符
(`:content_i` 等)+ `StatementParameterListItem` 列表传值,不再手工拼接/转义字符串。

`setup_uc_functions.py` 里 `.format(catalog=..., schema=...)` 拼 catalog/schema 名称的
写法保留不改——这两个是 SQL 标识符(表名/schema 名的一部分),不是数据值,大多数 SQL 方言
的参数化查询本来就不支持给标识符做参数绑定,且来源是受信任的环境变量,不是自由文本输入。

---

## 8.【已修复,2026-07-28】App 把后端调用失败的错误信息当成"assistant"消息存进对话历史

**现象**:[app/app.py:87-93](app/app.py#L87-L93):
```python
try:
    answer, genie_conversation_id = ask(st.session_state.history)
    st.session_state.genie_conversation_id = genie_conversation_id
except Exception as exc:
    answer = f"调用后端出错: {exc}"
st.markdown(answer)
st.session_state.history.append({"role": "assistant", "content": answer})
```
不管 `ask()` 成功还是抛异常,`answer` 最后都会被当成一条正常的 assistant 消息 append 进
`st.session_state.history`——这是这次加了"完整历史回传"这个功能之后才产生的新副作用:
在这次改动之前,历史消息不会被发回后端,错误信息留在聊天记录里只影响这次显示,不会有
后续影响;现在完整历史会被发给 `router`/`finalize` 当上下文,一条"调用后端出错:
ConnectionError(...)"这样的 assistant 消息混进去,可能让后续几轮的路由判断/最终回答把这条
异常堆栈信息当成"之前 agent 说过的话"去理解,产生不可预测的干扰。

**计划修复**:失败时不要把错误信息当成 assistant 消息存进 `history`——单独维护一个只用于
本次页面渲染的错误提示(比如用 `st.error(...)` 单独展示),不写入会被发回后端的
`st.session_state.history`。

**已按上述方案实现**:[app/app.py](app/app.py) 把 `try/except` 改成 `try/except/else`——
成功时才 `st.markdown(answer)` + append 进 `st.session_state.history`;失败时只
`st.error(f"调用后端出错: {exc}")` 展示这一次,不碰 `history`,下一轮请求不会带上这条
错误信息。离线单测(`tests/test_app_error_handling.py`,3 个用例全部通过,含"失败后紧接着
成功一轮"的场景)见 `tests/eval/results/items_3_8_verification_20260728.md`。

---

## 9.【用户决定暂不修,2026-07-28】聊天框没有"开始新对话"的入口

**用户决定**:附加功能,不是缺陷,先不修。

**现象**:`st.session_state.genie_conversation_id` 一旦在某一轮被设置,会在整个浏览器
会话生命周期内一直复用(见 [app/app.py:88-89](app/app.py#L88-L89)),没有任何 UI 元素或
逻辑可以让用户主动清空它、开始一个全新的、跟之前话题无关的对话。这是加了跨轮记忆之后才
出现的新问题:改动之前每句话本来就是独立的,不存在"想摆脱历史包袱"这个需求;现在如果用户
中途想换一个完全不相关的话题("刚才问的信用额度不用管了,我想问别的"),Genie 和
router/finalize 仍然会带着之前的对话上下文去理解新问题,可能出现不必要的过度关联。

**计划修复**:在 `app/app.py` 加一个"新对话"按钮(`st.button`),点击后清空
`st.session_state.history` 和 `st.session_state.genie_conversation_id`。

---

## 10.【用户决定暂不修,2026-07-29】"最近几轮"历史窗口和 Genie 自己的会话记忆窗口不是同一个尺度

**用户决定**:2026-07-28 的"部分缓解"(把 `_MAX_HISTORY_MESSAGES` 从 6 调到 10)先维持现状,
根本修复(方向 a/b,见下)暂不继续推进,和第 4/7/9 条一样归为主动暂不修,不再单列"待修复"。

**现象**:[src/graph/state.py](src/graph/state.py) 的 `recent_history_text()` 只截取
最近 `_MAX_HISTORY_MESSAGES=6` 条消息喂给 router/finalize;但 `genie_conversation_id`
对应的 Genie 内部会话记忆没有这个截断,只要 `genie_conversation_id` 没变,Genie 自己会
记住这个会话里出现过的**全部**历史(具体记多久、记多少是 Databricks 内部实现,我们看不到
也控制不了)。长会话场景下(超过几轮之后),这两层记忆的"能记住多远"不再是同一个尺度——
Genie 可能还记得第 1 轮提到的某个客户,但 router/finalize 只能看到最近 3 轮,已经不知道
这件事了,两层判断依据不一致,可能导致回答自相矛盾(比如 Genie 生成的 SQL 隐含假设了第
1 轮的某个条件,但 finalize 组织最终回答时完全不知道这个假设从哪来)。

**计划修复(暂未细化,记录风险为主)**:两个方向都可以考虑,还没有定下来选哪个——(a) 每隔
固定轮数强制开一个新的 `genie_conversation_id`,让两层记忆窗口重新对齐;(b) 把
`_MAX_HISTORY_MESSAGES` 调大到接近 Genie 实际会记住的量级(需要先搞清楚 Genie 自己的
会话记忆窗口有多大,目前没有相关文档)。这个问题在验证阶段(2 轮对话)没有被触发,是分析
设计时发现的潜在风险,不是实测复现的 bug。

**已部分处理**:[src/graph/state.py](src/graph/state.py) 的 `_MAX_HISTORY_MESSAGES` 从
6(3 轮)调到 10(5 轮)——这只是把窗口调大缓解问题出现的概率,**不是方向 (a)/(b) 里任何
一个的真正实现**,两层记忆窗口本身仍然不是同一个尺度,只是"不一致"这件事变得没那么容易
触发。根本解决(强制对齐 conversation_id,或者先搞清楚 Genie 自己的记忆窗口有多大再对齐
`_MAX_HISTORY_MESSAGES`)仍然没有做,但按上面的用户决定,暂不继续推进。

**相关参考:Genie Space 的几个平台硬性配额**(2026-07-29 记录,供以后评估这条以及扩容时
参考,不是这次改动触发的新发现):
- 每个 Genie Space 最多挂载 **30 张表/视图**(可跨 schema、跨 catalog,只要在 Unity Catalog
  里注册即可)——当前 space 挂了 20 张(见 `docs/` 或 memory 里的资源清单),还有余量。
- 每个 Agent 最多 **10,000 场对话**,每场对话最多 **10,000 条消息**——远超个人练习项目的
  实际使用量,不构成当前风险,但如果第 10 条以后要做"定期强制开新 conversation_id"的方案,
  这两个数字是可用的硬上限参考。
- Instructions 上限 **100 条**(每条示例 SQL、每个函数、每段通用说明各算一条)。
- Knowledge store snippets 上限 **200 条**(表描述、join 关系、SQL 表达式共享同一配额)。

---

## 11.【已修复,2026-07-28】config.py 的注释举例已经过时

**现象**:`src/config.py` 的注释举 `databricks.ai_search.client.VectorSearchClient` 作为
"不经过 `db_client.py` 就自己建裸 `WorkspaceClient()`"的例子——这是当初为了修 Vector
Search 认证问题时写的,但后来 Vector Search 已经整个迁移到 `databricks-sdk` 原生 API
(见第 3 节 Vector Search 相关记录 / `docs/VERIFICATION_2026-07-27.md`),这个包现在已经
不在依赖列表里了,注释里举的例子对不上现状。`DATABRICKS_AZURE_RESOURCE_ID` 环境变量注入
这个修复本身仍然是必要的(mlflow 自己的 `get_databricks_host_creds()` 内部同样会建裸
`WorkspaceClient()`,只是换了个触发场景),不是这条注释过时就代表这个修复本身可以删掉,
只是举例要换一个更准确的场景。

**已修复**:[src/config.py:100-107](src/config.py#L100-L107) 的注释把举例换成了
`mlflow.utils.databricks_utils.get_databricks_host_creds()`,不再提已经不存在的依赖,
`__post_init__` 的实际逻辑没有变。
