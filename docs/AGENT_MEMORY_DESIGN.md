# Agent 多轮记忆：设计思路（2026-07-28）

> 本文档只讲设计思想和方法论，不贴改动代码——具体改了哪几行，直接看
> `src/graph/state.py`/`router.py`/`finalize.py`、`src/agent.py`、`app/app.py` 的
> 代码和内联注释。这次改动新增的测试在
> `tests/test_integration_cases.py::test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns`。

## 起点：记忆缺口不是一个问题，是三层问题叠在一起

这一轮改动之前，"agent 有没有记忆"这个问题被讨论过好几轮，逐步拆出了三个互相独立、
容易被误当成同一件事的缺口：

1. **Genie 自己的会话记忆**——`genie_conversation_id`，只在 Genie API 内部生效，管的是
   "Genie 生成 SQL 时记不记得上一句问的是什么"，跟 LangGraph、跟其他节点都无关。
2. **完整对话历史根本没被传给后端**——部署后的 App 每次只发当前这一句问题，`agent.py`
   每次都是全新的 `initial_state`，图里的任何节点从一开始就看不到"用户之前问过什么"。
3. **就算历史传过去了，router/finalize 也不读它**——`AgentState` 里 `messages` 字段
   一直都在，本地 `chat.py` 也一直在正确累加它，但 `router.py`/`finalize.py` 拼 LLM
   prompt 时只用 `user_query`/`credit_info`/`structured_result` 这几个字段，`messages`
   等于是个没人看的仓库。

三层缺口必须一起补，只补其中一两层看起来"有效果"但经不起追问——比如只修第 1 层，
`chat.py` 表面上"记得住"，但那只是因为它是常驻进程、`messages` 恰好被完整地喂进了每次
`graph.invoke()`，而不是因为 router/finalize 真的用上了这份历史。这也是为什么这次直接
问"chat.py 是不是已经实现了 agent 层面的记忆"这个问题，答案是"没有"——第 3 层从来没做过。

## 为什么选"客户端状态透传"，不选 LangGraph checkpointer

之前对比过两条路：

- **方案 A（这次采用的）**：状态权威在客户端（App 的 `st.session_state`、`chat.py` 的
  局部变量），每次请求把需要的状态带过去，服务端只是无状态地处理这一次请求。
- **方案 B（放弃）**：给 `build_graph()` 接一个 LangGraph checkpointer，服务端按
  `thread_id` 自动持久化整个 `AgentState`，客户端只需要记一个 id。

选方案 A 的理由很直接：这个项目现在的实际需求，就是"同一次浏览器会话里能追问"，方案 A
不需要引入任何新的持久化后端就能满足；方案 B 要做到真正可靠（不是只在单进程里测试），
必须接一个持久化存储，这个项目从 config、认证、SQL 执行、模型注册到部署全在 Databricks
生态里，为了这一个功能单独引入一套新基础设施（不管是 Postgres 还是别的），成本明显
不成比例。如果以后真的要做跨会话/跨设备记忆，再重新评估方案 B，且到时候优先选 Delta 表
而不是外部数据库——这个结论已经记在之前的对话总结里，这次实现时没有变。

## 三个具体设计决定，以及为什么这么定

**1. "最近历史"这段格式化逻辑，放在 `state.py` 里做成共享函数，不在 router/finalize
里各写一份。** 两个节点都要用同一份历史、同样的截断规则，写两份的风险是以后改一处
（比如调整截断条数）另一处忘了同步改——这跟之前"`ops/grant_app_permissions.py` 该不该
跟 `setup_genie.py` 的表清单联动"是同一个道理：凡是"两个地方逻辑上必须保持一致"的东西，
尽量让它们物理上共享同一份代码，而不是靠人记得同步维护。

**2. 历史只截取最近几条，不是全量塞进 prompt。** 这不是随便定的数字，是两个约束的交集：
一是 prompt 长度不能无限增长（这是`DEVELOPMENT_JOURNAL.md` Part 4 第 8 条早就点出来的
风险）；二是"最近几轮"对于"理解当前这句里的指代"通常已经够用，再往前的历史对当前问题的
相关性通常也在下降。这个截断只影响"喂给 LLM 判断用的摘要"，不影响 `messages` 本身的完整
性——完整历史还是会原样保留在 state 里、原样显示在聊天框里，被截断的只是"喂给 router/
finalize 做判断的那一段浓缩文本"。

**3. `genie_conversation_id` 和 `messages` 走两条不同的透传通道，不是因为随意，是因为
它们在 MLflow ResponsesAgent 的契约里本来就属于两类不同的东西。** `messages`（准确说是
`request.input`）是这个协议原本就设计好的"对话内容"通道，客户端把完整历史当成输入的一
部分发过去是标准用法；`genie_conversation_id` 不是对话内容，是我们自己业务逻辑内部的一个
状态句柄，协议里专门留了 `custom_inputs`/`custom_outputs` 这一对字段就是给这种"跟对话
内容无关，但需要跨请求带回来的自定义状态"用的。把两种性质不同的东西分别放进它们各自该
待的位置，而不是都塞进一个字段里，是这次设计上比较刻意的一点。

## 一个刻意加的、但没能完全生效的约束

router 和 finalize 的 system prompt 里都加了一条明确提醒：历史对话只用来理解问题里的
指代，不能把历史里出现过的具体数值当成"本轮已经查到的结果"直接拿来回答，需要的数据仍然
要在本轮重新查/算一遍。加这条的原因：一旦 router/finalize 能看到历史，就存在"偷懒"的
风险——LLM 可能觉得"数字都在上面了，不用再查一次"，但历史里的数字是"上一轮查到的"，不代表
"这一轮问的问题不需要重新计算"（比如上一轮查的是采购额，这一轮问的是要重新跑业务规则
函数才能算出来的信用额度，两者不能划等号）。

这条约束在验证阶段（详见下面"验证方法论"）确实被观察到过一次没被遵守的情况——LLM
router 判断"信息已经够了"，跳过了本该重新触发的结构化计算，finalize 只能从历史数字里
自己推、还推错了分类。这不是这次改动本身的逻辑 bug（两次独立重跑，一次复现、一次没有），
而是这次改动新增了一个此前不存在的失败模式：**没有历史的时候，router 没有"偷懒"这个
选项；现在历史摆在眼前，router 多了一条捷径可以选，而这条捷径有时候会选错。** 这个风险
和后续要不要进一步收紧 prompt，作为一条新发现记录进了 `docs/CODE_REVIEW_FINDINGS.md`，
不在这次改动范围内直接解决——原因是这属于"LLM 判断本身的非确定性"这一类，项目里其他
地方（router 的路由判断、Genie 的 SQL 生成）都有同样性质的、长期存在且没有彻底解决方案
的问题，不是这次能一次性修完的。

## 验证方法论：为什么这么测，不是随便跑跑

验证分两层，对应两条实际会被用到的调用路径，缺一个都不够：

1. **直接 `build_graph().invoke()` 连续调两次**，模拟 `chat.py` 的调用方式——这条路径
   验证的是"记忆机制本身（历史拼接 + genie_conversation_id 传递）在图这一层是否正确"，
   不涉及 MLflow/HTTP 这层包装，出问题更容易定位是图内部逻辑的问题还是外层包装的问题。
2. **直接实例化 `SalesDuoResponsesAgent` 连续调两次 `predict()`**，模拟 App 通过
   Serving Endpoint 调用的方式——这条路径专门验证 `custom_inputs`/`custom_outputs` 这一层
   包装本身有没有接对，是第 1 条路径完全覆盖不到的。

两条路径分开测，是因为它们各自的失败模式不一样：如果只测第 1 条，`agent.py` 里的
`custom_inputs`/`custom_outputs` 读写就算写反了也测不出来；如果只测第 2 条，图内部
`recent_history_text()` 这类纯逻辑的问题会被 MLflow 包装层的问题掩盖，不好定位。

两条路径都没有走真正部署的 Serving Endpoint（没有重新 `bundle deploy`/`bundle run`）——
`agent.py`/`app.py` 这次改的逻辑跟"代码部署到哪"无关，本地直接实例化跑的就是同一份
`src/agent.py` 代码，重新部署一次不会验证出新的信息，只会多花十几分钟等 Serving Endpoint
滚动更新。如果之后要正式发布这个改动，仍然需要走一次 `ops.deploy_model` +
`bundle deploy`/`bundle run` 才能让线上真正生效，这一步这次没做。
