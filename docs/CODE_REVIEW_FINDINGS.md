# Code Review Findings (first pass 2026-07-27, updated on an ongoing basis)

A critical review of the existing `src/` implementation, triggered by the user asking to
"list everything unreasonable." Sorted by severity; each item records the symptom, root
cause, and planned fix (or reason for deferring). **This document keeps getting
re-reviewed, updated, and appended to as later changes land** — the date in the title is
when it was first created, not a cutoff for its content; each item's status tag
(【FIXED, date】/【OPEN】/【DEFERRED, not fixing】) is the source of truth for its current
state.

**Path note**: the file paths referenced in items 1-7 (`src/tools/`, `src/setup/`, etc.)
are the real paths as of the first review on 2026-07-27, reflecting the structure before
the directory refactor — they are not retroactively updated. Items added after item 8
use the current, post-refactor paths. See `docs/REPOSITORY_STRUCTURE.md` for the single
source of truth on current paths.

**Fix status overview (last checked 2026-07-29)**: items 1, 2, 3, 5, 8, 11 are fixed;
items 4, 7, 9, 10 the user explicitly decided not to fix for now (personal practice
project / nice-to-have / not high enough priority — see each item for the reasoning);
item 6 is deferred.

---

## 1. 【FIXED, 2026-07-28】 The chat box's "multi-turn conversation" was fake — every message was a brand-new session as far as Genie was concerned

**Symptom**: `app/app.py` only sent the current `question` to the backend each time,
with no history; `agent.py`'s `_run_graph` had no field to receive "the
`genie_conversation_id` returned by the previous request" either — every `predict()`
call started from a fresh `loop_count: 0`, `genie_conversation_id=None`. CLAUDE.md
design principle 3's requirement to "reuse conversation_id when Genie is called a second
time within the same user request" was only honored inside a single HTTP request's
router loop — cross-turn memory at the chat-box level was never implemented at all,
even though the UI displayed message history, creating a false impression of memory.

Note: the local debugging tool `chat.py` (repo root) was actually correct — it's a
long-running process, and `genie_conversation_id` is an ordinary local variable outside
the loop body, naturally persisting across turns
([chat.py:21](chat.py#L21), [chat.py:53](chat.py#L53)). The problem was specific to the
deployed shape, "every HTTP request is a stateless call to a fresh process" —
`agent.py`/`app.py` never translated the role `chat.py`'s "local variable" played into
an equivalent cross-request handoff mechanism.

**Planned fix**: use MLflow's `ResponsesAgentRequest.custom_inputs` /
`ResponsesAgentResponse.custom_outputs` (both reserved custom-field passthrough
channels) to carry state across requests:
- `src/agent.py`: `predict`/`predict_stream` read the previous turn's
  `genie_conversation_id` from `request.custom_inputs` into `initial_state`; on return,
  put this turn's final `genie_conversation_id` into `custom_outputs`.
- `app/app.py`: add a `genie_conversation_id` field to `st.session_state`, include it in
  `custom_inputs` when sending a request, and store it back into session from
  `custom_outputs` on response, for the next turn to use.

**Implemented per the plan above on 2026-07-28**, and along the way it turned out
"only fixing the genie_conversation_id field" wasn't enough — `router.py`/`finalize.py`
had never read `state["messages"]` at all, so even with history correctly wired through,
nothing was consuming it. The full fix scope, design trade-offs, and a non-determinism
risk discovered during verification ("router sometimes skips recomputing and infers
straight from historical values instead") are written up in
`docs/AGENT_MEMORY_DESIGN.md`. The corresponding repeatable test case:
`tests/test_integration_cases.py::test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns`
(run independently twice; the core mechanism held on 2/2 runs — one run did hit an
assertion failure due to a router judgment slip, which passed on rerun; see the "A
deliberate constraint that didn't fully hold" section of the design doc for details).

---

## 2. 【FIXED, done alongside the 2026-07-27 directory refactor】 requirements.txt didn't distinguish "runtime dependencies" from "provisioning-script dependencies"

**Symptom**: `deploy_model.py` passed the entire root `requirements.txt` to
`mlflow.pyfunc.log_model(pip_requirements=...)`. That file mixed together
`databricks-connect` (used for local dev connections), `azure-core`/
`azure-storage-file-datalake` (used only by `ingest_docs.py` for file uploads),
`python-docx` (used only by `chunk_docs.py`), and `pytest` (test-only) — none of which
the actual runtime path `agent.py → graph/*.py → tools/*.py` ever uses, yet all of it
got baked into the serving container image on every deploy.

**Planned fix**: add `requirements-runtime.txt`, listing only packages the runtime code
actually imports (derived by checking the import statements in `src/agent.py`,
`src/config.py`, `src/db_client.py`, `src/graph/*.py`, `src/tools/*.py` one by one); the
root `requirements.txt` becomes `-r requirements-runtime.txt` plus
provisioning/test-only extras, avoiding maintaining two separate version lists;
`deploy_model.py`'s `_REQUIREMENTS_FILE` points at `requirements-runtime.txt` instead.

**Implemented per the plan above** (done as part of the directory refactor round, see
`docs/REPOSITORY_STRUCTURE.md`): `requirements-runtime.txt` now has just 7 lines of
runtime dependencies, and `ops/deploy_model.py`'s `_REQUIREMENTS_FILE` is confirmed to
point at it. When Vector Search was later migrated to the native SDK, the
`databricks-ai-search` line was also dropped from `requirements-runtime.txt` along the
way (that package is no longer imported by any runtime code either).

---

## 3. 【FIXED, 2026-07-28】 unstructured_agent's retrieval query never changed across multi-hop calls

**Symptom**: `unstructured_agent.py` did `query = state.get("user_query", "")` —
regardless of how many times this node had been routed into, or why router dispatched
here again this time (`router_reason` was never used at all), the retrieval string was
identical to the first call. Compared with `structured_agent.py`'s `_build_question`,
which folds the existing `credit_info` into the question, the two nodes had
asymmetrically shallow "adjust the next step based on what's already known"
implementations — the second time routed back to unstructured, it would very likely
retrieve the same chunks as the first pass, find no new information, and risk spinning
between router judging "not enough information yet" and retrieval "finding nothing new"
until it hit `MAX_ROUTER_LOOPS`.

**Planned fix**: add `_build_query` to `unstructured_agent.py`, folding `router_reason`
(why router dispatched here again this time) and any existing `structured_result` into
the retrieval text, so the second retrieval's query actually carries "specifically
what's still missing this time" instead of repeating the original first question.

**Implemented per the plan above**: [src/graph/unstructured_agent.py](src/graph/unstructured_agent.py)
adds `_build_query()`, concatenating `user_query`/`router_reason`/`structured_result` in
order into the retrieval text; `unstructured_agent_node` now gets its query from
`_build_query(state)` instead of using `user_query` directly. Offline unit tests
(`tests/test_unstructured_agent_query.py`, all 4 cases passing) are recorded in
`tests/eval/results/items_3_8_verification_20260728.md`; no additional live
retrieval-quality comparison was done (that requires actually triggering a multi-hop
scenario to observe the difference, out of scope for this offline verification).

---

## 4. 【User decided not to fix for now, 2026-07-28】 No overall request timeout, only a loop-count cap

**User's decision**: a personal practice project with no real users, this risk isn't a
concern right now — not fixing for now.

**Symptom**: a single Genie call polls for up to 300 seconds
(`genie_client.py`'s `_POLL_TIMEOUT_SECONDS`), and `MAX_ROUTER_LOOPS=5` means the worst
case for a single user question is 5 consecutive Genie/Vector Search calls — in theory
that upper bound could stretch to 25 minutes before `finalize` triggers. Nothing in the
whole chain has a total-duration cap shorter than that.

**Planned fix**: mirroring the `loop_count` safety-valve approach, add a wall-clock time
budget:
- `src/config.py` adds `MAX_TOTAL_SECONDS` (default 240 seconds).
- `src/graph/state.py`'s `AgentState` adds `started_at: float | None`.
- `src/agent.py` sets `started_at = time.time()` when constructing `initial_state`.
- `src/graph/router.py`, in the same place it checks the `loop_count` cap, also checks
  `time.time() - started_at >= MAX_TOTAL_SECONDS`; hitting it forces `finalize` and
  records the reason the same way as exceeding loop_count, with no new failure path
  introduced.
- Backward compatible: when `started_at` is missing (e.g. existing offline unit tests
  that hand-construct state without this field), skip the time check and only check
  loop_count, so existing `tests/test_router_loop_limit.py` cases are unaffected.

---

## 5. 【FIXED, 2026-07-28】 retriever.py's default parameter was "a value already confirmed by testing to be wrong"

**Symptom**: `retrieve(query, k=5)` defaulted to 5, but both CLAUDE.md and
DEVELOPMENT_JOURNAL recorded that "testing showed top_k=5 misses target passages, must
use 8." The only caller today, `unstructured_agent.py`, passes `_TOP_K = 8`, overriding
the default, so this hasn't surfaced a problem yet — but the default value itself is
stale and confirmed problematic, and any future caller (an eval script, a debug CLI)
that forgets to pass `k` would silently degrade.

**Fixed**: [src/clients/retriever.py](src/clients/retriever.py)'s `retrieve()` default
changed from 5 to 8. The explicit `_TOP_K = 8` declaration in `unstructured_agent.py` is
kept as-is (more readable, doesn't rely on an implicit default) — this change just
removes the "trap" in the default value itself; behavior is unchanged, import
verification passed, and no additional live retrieval verification was done (not
needed — this change doesn't alter the actual parameter value for any existing caller).

---

## 6. 【Deferred, not fixing】 The `src/graph/` directory mixes two different granularities of file — "node implementations" and "orchestration scaffolding" — together

`router.py`/`build_graph.py`/`state.py`/`finalize.py` (orchestration scaffolding) and
`structured_agent.py`/`unstructured_agent.py` (concrete node implementations) sit at the
same level, with no subdirectory distinguishing them.

**Why not fix**: this is "a debatable trade-off," not a defect — the project currently
has only 4 nodes and 2 external tools; splitting out a `graph/nodes/` subdirectory would
only add cross-file navigation cost with no clear benefit. It'll make more sense to
split once a third data source/node is added. Not changed this round; recorded here for
future reference.

---

## 7. 【User decided not to fix for now, 2026-07-28】 ingest_docs.py hand-assembles escaped SQL instead of using parameterized queries

**User's decision**: a personal practice project with a trusted input source — doesn't
constitute a real injection risk, not fixing for now.

**Symptom**: `create_and_populate_delta_table` manually escapes single quotes via
`_escape()`, then concatenates the document-chunk content directly into an
`INSERT ... VALUES (...)` statement string. The current input source is trusted (text
parsed locally from docx files), so it doesn't constitute a real injection risk, but
manual escaping only handles single quotes and is a fragile hand-rolled approach to
safety.

**Planned fix**: add an optional `parameters` argument to `sql_utils.run_statement`,
passed through to the SDK's `execute_statement(parameters=...)`; `ingest_docs.py`'s bulk
INSERT switches to named parameter placeholders (`:content_i` etc.) plus a
`StatementParameterListItem` list of values, instead of hand-concatenating/escaping
strings.

The `.format(catalog=..., schema=...)` pattern in `setup_uc_functions.py`, used to
assemble catalog/schema names, is kept unchanged — those are SQL identifiers (part of a
table/schema name), not data values; most SQL dialects' parameterized queries don't
support binding identifiers as parameters anyway, and the source is a trusted
environment variable, not free-text input.

---

## 8. 【FIXED, 2026-07-28】 The App stored a backend-call failure's error message into conversation history as an "assistant" message

**Symptom**: [app/app.py:87-93](app/app.py#L87-L93):
```python
try:
    answer, genie_conversation_id = ask(st.session_state.history)
    st.session_state.genie_conversation_id = genie_conversation_id
except Exception as exc:
    answer = f"Backend call failed: {exc}"
st.markdown(answer)
st.session_state.history.append({"role": "assistant", "content": answer})
```
Whether `ask()` succeeded or raised, `answer` ended up appended to
`st.session_state.history` as a normal assistant message either way — this was a new
side effect introduced only after adding "send full history back to the backend": before
this change, history messages were never sent back to the backend, so an error message
left in the chat log only affected that one render, with no downstream impact; now that
the full history gets sent to `router`/`finalize` as context, an assistant message like
"Backend call failed: ConnectionError(...)" mixed in could cause routing decisions/final
answers in later turns to interpret that exception stack trace as "something the agent
said before," producing unpredictable interference.

**Planned fix**: don't store the error message into `history` as an assistant message
on failure — maintain a separate error notice used only for this one page render (e.g.
shown via `st.error(...)`), never written into the `st.session_state.history` that gets
sent back to the backend.

**Implemented per the plan above**: [app/app.py](app/app.py) changed `try/except` to
`try/except/else` — `st.markdown(answer)` plus appending to
`st.session_state.history` only happen on success; on failure it only shows
`st.error(f"Backend call failed: {exc}")` for this one render, never touching `history`,
so the next request never carries this error message. Offline unit tests
(`tests/test_app_error_handling.py`, all 3 cases passing, including a "success
immediately following a failure" scenario) are recorded in
`tests/eval/results/items_3_8_verification_20260728.md`.

---

## 9. 【User decided not to fix for now, 2026-07-28】 The chat box has no "start a new conversation" entry point

**User's decision**: a nice-to-have, not a defect — not fixing for now.

**Symptom**: once `st.session_state.genie_conversation_id` is set in some turn, it keeps
getting reused for the entire lifetime of the browser session (see
[app/app.py:88-89](app/app.py#L88-L89)), with no UI element or logic letting the user
actively clear it and start a brand-new conversation unrelated to the previous topic.
This is a new problem introduced only after cross-turn memory was added: before that
change, every message was independent by construction, so there was never a need to
"shed the baggage of history"; now, if a user wants to switch to a completely unrelated
topic mid-session ("never mind the credit limit I just asked about, I want to ask about
something else"), Genie and router/finalize will still interpret the new question
carrying the previous conversation's context, potentially causing unwanted over-linking.

**Planned fix**: add a "new conversation" button (`st.button`) to `app/app.py`; clicking
it clears both `st.session_state.history` and `st.session_state.genie_conversation_id`.

---

## 10. 【User decided not to fix for now, 2026-07-29】 The "recent turns" history window and Genie's own session-memory window aren't on the same scale

**User's decision**: the 2026-07-28 "partial mitigation" (bumping `_MAX_HISTORY_MESSAGES`
from 6 to 10) stays as-is for now; the root-cause fix (direction a/b, below) isn't being
pursued further for now — grouped with items 4/7/9 as a deliberate "not fixing for now,"
no longer separately tracked as "open."

**Symptom**: [src/graph/state.py](src/graph/state.py)'s `recent_history_text()` only
takes the most recent `_MAX_HISTORY_MESSAGES=6` messages to feed router/finalize; but
Genie's own internal session memory tied to `genie_conversation_id` has no such
truncation — as long as `genie_conversation_id` stays the same, Genie remembers **all**
history that's appeared in that conversation on its own (exactly how much/how long it
remembers is Databricks' internal implementation, which we can't see or control). In
long-conversation scenarios (past a few turns), "how far back can it remember" is no
longer on the same scale between the two memory layers — Genie might still remember some
customer mentioned in turn 1, while router/finalize can only see the last 3 turns and
has already lost track of it, so the two layers' basis for judgment diverges, which can
produce self-contradictory answers (e.g. SQL Genie generates implicitly assumes some
condition from turn 1, but finalize, composing the final answer, has no idea where that
assumption came from).

**Planned fix (not yet fleshed out, mainly recording the risk)**: two directions worth
considering, not yet decided between — (a) force-open a new `genie_conversation_id`
every fixed number of turns, realigning the two memory windows; (b) bump
`_MAX_HISTORY_MESSAGES` up to something closer to the order of magnitude Genie actually
remembers (first requires figuring out how large Genie's own session-memory window
actually is, which isn't documented anywhere we've found). This issue wasn't triggered
during verification (a 2-turn conversation) — it's a potential risk found during design
analysis, not a bug reproduced through actual testing.

**Partially addressed**: [src/graph/state.py](src/graph/state.py)'s
`_MAX_HISTORY_MESSAGES` was bumped from 6 (3 turns) to 10 (5 turns) — this only widens
the window to reduce the odds of the problem surfacing, it is **not** an actual
implementation of either direction (a) or (b); the two memory windows still aren't on
the same scale, it's just less easy to trigger the "inconsistency" now. The real fix
(force-align conversation_id, or first figure out how big Genie's own memory window is
before aligning `_MAX_HISTORY_MESSAGES` to it) still hasn't been done, but per the user
decision above, it isn't being pursued further for now.

**Related reference: a few hard platform quotas on Genie Space** (recorded 2026-07-29,
for future reference when evaluating this item or planning to scale up — not a new
finding triggered by this change):
- Each Genie Space can have at most **30 tables/views** attached (can span schemas/
  catalogs, as long as they're registered in Unity Catalog) — the current Space has 20
  attached (see the resource inventory in `docs/` or memory), still some headroom.
- Each Agent supports at most **10,000 conversations**, each conversation at most
  **10,000 messages** — far beyond a personal practice project's actual usage, not a
  risk right now, but if a "periodically force-open a new conversation_id" scheme from
  item 10 is pursued later, these two numbers are the usable hard ceiling to reference.
- Instructions cap: **100 entries** (each example SQL query, each function, each general
  note each counts as one entry).
- Knowledge store snippets cap: **200 entries** (table descriptions, join relationships,
  and SQL expressions all share the same quota).

---

## 11. 【FIXED, 2026-07-28】 config.py's comment example was stale

**Symptom**: `src/config.py`'s comment cited
`databricks.ai_search.client.VectorSearchClient` as an example of "constructing a bare
`WorkspaceClient()` without going through `db_client.py`" — this was written back when
fixing the Vector Search auth problem, but Vector Search was later migrated entirely to
the native `databricks-sdk` API (see the Vector Search records in section 3 /
`docs/VERIFICATION_2026-07-27.md`), and that package is no longer in the dependency
list, so the comment's example no longer matches reality. The
`DATABRICKS_AZURE_RESOURCE_ID` environment-variable injection fix itself is still
necessary (mlflow's own `get_databricks_host_creds()` internally constructs a bare
`WorkspaceClient()` too, just triggered in a different scenario) — the comment being
stale doesn't mean this fix can be removed, just that the example needs to point at a
more accurate scenario.

**Fixed**: [src/config.py:100-107](src/config.py#L100-L107)'s comment now cites
`mlflow.utils.databricks_utils.get_databricks_host_creds()` as the example instead, no
longer mentioning a dependency that no longer exists; `__post_init__`'s actual logic is
unchanged.
