# Agent Multi-Turn Memory: Design Rationale (2026-07-28)

> This document only covers the design thinking and methodology, not the actual diff —
> for exactly which lines changed, see the code and inline comments in
> `src/graph/state.py`/`router.py`/`finalize.py`, `src/agent.py`, and `app/app.py`
> directly. The test added for this change is
> `tests/test_integration_cases.py::test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns`.

## Starting point: the memory gap wasn't one problem, it was three stacked on top of each other

Before this round of changes, the question "does the agent have memory" had come up
across several earlier discussions, and gradually got teased apart into three
independent gaps that are easy to mistake for the same thing:

1. **Genie's own session memory** — `genie_conversation_id`, which only takes effect
   inside the Genie API, governing "does Genie remember what the last question asked
   when generating SQL" — unrelated to LangGraph or to any other node.
2. **The full conversation history was never sent to the backend at all** — the deployed
   App sent only the current question each time, `agent.py` started from a brand-new
   `initial_state` every time, so no node in the graph could see "what the user asked
   before" from the very start.
3. **Even once history was sent through, router/finalize didn't read it** — the
   `messages` field in `AgentState` had always existed, and the local `chat.py` had
   always correctly accumulated it, but `router.py`/`finalize.py`, when assembling the
   LLM prompt, only used `user_query`/`credit_info`/`structured_result` — `messages` was
   effectively a storeroom nobody looked in.

All three gaps had to be closed together — fixing only one or two of them "looks like it
works" but doesn't hold up under scrutiny. For example, fixing only layer 1 makes
`chat.py` *appear* to "remember," but only because it's a long-running process and
`messages` happens to be fed in full into every `graph.invoke()` call — not because
router/finalize actually made use of that history. This is also why, when asked directly
"does `chat.py` already implement memory at the agent level," the answer was "no" — layer
3 had never been done.

## Why "client-side state passthrough" was chosen over a LangGraph checkpointer

Two paths were compared earlier:

- **Option A (the one adopted)**: state authority lives on the client (the App's
  `st.session_state`, `chat.py`'s local variables); each request carries whatever state
  is needed, and the server processes each request statelessly.
- **Option B (rejected)**: wire a LangGraph checkpointer into `build_graph()`, letting
  the server auto-persist the entire `AgentState` keyed by `thread_id`, with the client
  only needing to remember an id.

The reasoning for Option A is straightforward: the project's actual requirement right
now is just "being able to ask a follow-up within the same browser session," which
Option A satisfies without introducing any new persistence backend; Option B, to be
truly reliable (not just tested within a single process), needs a persistence store
wired in — and this project's config, auth, SQL execution, model registration, and
deployment are all already entirely within the Databricks ecosystem, so introducing a
whole new piece of infrastructure (whether Postgres or something else) just for this one
feature would be disproportionately costly. If cross-session/cross-device memory is
genuinely needed later, Option B should be re-evaluated then, and at that point a Delta
table should be preferred over an external database — this conclusion was already
recorded in an earlier conversation summary, and hasn't changed for this implementation.

## Three specific design decisions, and why they were made this way

**1. The "recent history" formatting logic lives in `state.py` as a shared function,
not written separately in router/finalize.** Both nodes need the same history and the
same truncation rule; writing it twice risks one place getting updated later (e.g.
adjusting the truncation count) while the other is forgotten — the same reasoning as
earlier, for whether `ops/grant_app_permissions.py` should stay linked to
`setup_genie.py`'s table list: whenever two places must logically stay in sync, prefer
making them physically share the same code, rather than relying on someone remembering
to keep them in sync by hand.

**2. History only takes the most recent few messages, not the entire thing stuffed into
the prompt.** This number wasn't picked arbitrarily — it's the intersection of two
constraints: first, prompt length can't grow unbounded (a risk already called out in
`DEVELOPMENT_JOURNAL.md` Part 4 item 8); second, "the last few turns" is usually already
enough to "understand what a reference in the current message points to," and history
further back is usually less and less relevant to the current question anyway. This
truncation only affects "the summary fed to the LLM for its judgment" — it doesn't
affect the completeness of `messages` itself; the full history is still kept as-is in
state and displayed as-is in the chat box — only "the condensed text fed to router/
finalize for judgment" is truncated.

**3. `genie_conversation_id` and `messages` travel through two different passthrough
channels, not arbitrarily, but because they're genuinely two different kinds of thing
under the MLflow ResponsesAgent contract.** `messages` (more precisely,
`request.input`) is the "conversation content" channel the protocol was designed with —
a client sending the full history as part of the input is standard usage;
`genie_conversation_id` isn't conversation content, it's a state handle internal to our
own business logic, and the protocol specifically reserves the `custom_inputs`/
`custom_outputs` field pair for exactly this kind of "custom state unrelated to
conversation content, but that needs to be carried back across requests." Putting these
two different kinds of thing into the places they each actually belong, rather than
cramming both into one field, was a deliberate design choice here.

## A deliberate constraint that didn't fully hold

Both router's and finalize's system prompts got an explicit reminder added: history is
only for understanding references in the question, and specific values that appeared in
history must not be treated as "already retrieved this turn" and answered from directly
— the needed data still has to be re-queried/recomputed this turn. The reason for adding
this: once router/finalize can see history, there's a risk of "taking a shortcut" — the
LLM might think "the numbers are right there, no need to query again," but a number in
history was "retrieved last turn," which doesn't mean "this turn's question doesn't need
recomputation" (e.g. last turn queried purchase volume, this turn asks about a credit
limit that has to be run back through the business-rule function to compute — the two
aren't interchangeable).

This constraint was in fact observed being violated once during verification (see the
"Verification methodology" section below) — the LLM router judged "information is
already sufficient," skipping a structured computation that should have been
re-triggered, and finalize had to guess the tier from historical numbers on its own,
guessing wrong. This isn't a logic bug in this change itself (across two independent
reruns, it reproduced once and didn't the other time) — rather, this change introduced a
failure mode that didn't previously exist: **without history, router never had the
option to "take a shortcut"; now that history is right there, router has a new shortcut
available, and that shortcut is sometimes the wrong choice.** This risk, and whether the
prompt should be tightened further, has been recorded as a new finding in
`docs/CODE_REVIEW_FINDINGS.md`, and isn't being resolved directly within the scope of
this change — the reason being that this falls into the category of "non-determinism
inherent to the LLM's own judgment," a class of problem that exists elsewhere in this
project too (router's routing decisions, Genie's SQL generation), long-standing and
without a clean, complete solution — not something fixable in one pass here.

## Verification methodology: why tested this way, not just run ad hoc

Verification has two layers, corresponding to the two call paths actually used in
practice — missing either one wouldn't be enough:

1. **Calling `build_graph().invoke()` directly, twice in a row**, simulating how
   `chat.py` calls it — this path verifies whether the memory mechanism itself (history
   concatenation + genie_conversation_id passthrough) is correct at the graph layer,
   without involving the MLflow/HTTP wrapper layer, making it easier to pin down whether
   a problem is in the graph's internal logic or in the outer wrapper.
2. **Instantiating `SalesDuoResponsesAgent` directly and calling `predict()` twice in a
   row**, simulating how the App calls it via the Serving Endpoint — this path
   specifically verifies whether the `custom_inputs`/`custom_outputs` wrapper layer
   itself is wired correctly, something path 1 doesn't cover at all.

These two paths are tested separately because their failure modes differ: testing only
path 1 wouldn't catch it even if `agent.py`'s `custom_inputs`/`custom_outputs` reads/
writes were swapped backwards; testing only path 2, a pure-logic problem in
`recent_history_text()` inside the graph would get masked by problems in the MLflow
wrapper layer, making it harder to pin down.

Neither path went through an actually deployed Serving Endpoint (no fresh
`bundle deploy`/`bundle run` was done) — the logic changed in `agent.py`/`app.py` this
round is unrelated to "where the code is deployed"; instantiating directly and running
locally exercises the exact same `src/agent.py` code, so redeploying wouldn't verify
anything new, it would just cost an extra ten-odd minutes waiting for the Serving
Endpoint's rolling update. If this change is formally shipped later, it will still need
a run of `ops.deploy_model` + `bundle deploy`/`bundle run` to actually take effect in
production — that step wasn't done this round.
