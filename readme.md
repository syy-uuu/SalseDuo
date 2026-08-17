# SalesDuo

This repo is an engineering reference implementation / practice project built from
real-world work experience, using the open AdventureWorks dataset to simulate a real
business scenario — a "structured + unstructured hybrid retrieval, dynamic multi-hop
Agent."

**One-sentence summary of what makes this project distinctive**: implemented as a
"LangGraph routing + Genie + Vector Search, choose-one-or-loop" combination.

**From the perspective of Databricks' official app-templates**: SalesDuo ≈ the skeleton
of the [`agent-langgraph`](https://github.com/databricks/app-templates/tree/main/agent-langgraph)
template (MLflow ResponsesAgent + LangGraph + Databricks Apps deployment) +
[`streamlit-chatbot-app`](https://github.com/databricks/app-templates/tree/main/streamlit-chatbot-app)'s
frontend calling pattern, but with the Genie/Vector Search/UC Function tool integration —
left "commented out, for the user to fill in" in the template — actually built out for
real, using a hand-written multi-node routing graph (rather than the template's default
single-node ReAct loop) to implement dynamic multi-hop between "structured ↔
unstructured." The closest thing to "structured+unstructured fusion" in `app-templates`
is `rag-chat` in the Showcase section (pure RAG, pgvector+Lakebase, no Genie/structured
querying) and `inventory-intelligence`/`agentic-support-console` (a Genie analytics
panel + Lakebase CRUD, but not a LangGraph multi-hop Agent architecture) — SalesDuo's
particular combination has no direct counterpart among the official templates.

In particular, as the engineer building this, this exercise deliberately emphasizes a
**white-box implementation**, to build a real understanding of the agent framework and
more control over it — so that when something goes wrong, it's clear exactly what and
where, building experience useful for scaling and improving agent performance in future
work.

---

## Architecture

```
                     ┌───────────────────────────────────────────┐
                     │  MLflow ResponsesAgent (predict / predict_stream)
                     │  ← the sole external contract; the Databricks Apps chat box only knows this interface
                     └──────────────────┬──────────────────────────┘
                                        │
                          LangGraph StateGraph (router node + looping edge)
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
         router node               structured_agent          unstructured_agent
   (decides each step:            (calls the Genie Space:        (calls Vector Search:
    keep querying structured /     AdventureWorksLT tables +      chunk retrieval over
    keep querying unstructured     UC Function business-rule       documents_generated/)
    / finish)                      computation)
              │                         │                         │
              └──── loops back to router until it judges the information sufficient (or hits the loop cap) ──┘
                                        │
                                    finalize node
                              (synthesizes all intermediate results into a final answer)
```

- `router` uses Pydantic forced structured output at every step to decide `next_step`
  (`structured` / `unstructured` / `finalize`), never parsing free text, avoiding a
  routing-result parse failure making the state machine's behavior unpredictable;
  `loop_count` exceeding `MAX_ROUTER_LOOPS` forces a move to `finalize`, a safety net
  against an infinite loop.
- `structured_agent` and `unstructured_agent` are two **completely independent, mutually
  unaware** nodes — the "hybrid query" capability comes from LangGraph's graph
  structure, not from any built-in capability of Genie or Vector Search themselves.

### An implementation detail worth calling out: "tool" calling

The structured agent is handed entirely to the Genie Space — no question about that;
what's interesting is how it "knows how to compute business rules." The approach is to
pre-create two UC SQL Functions (`calculate_credit_terms`,
`check_large_transaction_compliance`) and use them as Genie's "tools." But this doesn't
go through the Genie Space's formal "attach a function as a tool" feature — that was
tried, and the `update_space` API fails outright whenever the payload includes an
`instructions.sql_functions` field (the UI can save it, but the API can't write it — a
confirmed platform limitation on this workspace, not a code bug). The approach that
actually works is writing the function's fully-qualified name, along with the exact
field names of its return STRUCT, directly into Genie's free-text Instructions — Genie
then calls it correctly per that description when generating SQL.

**This technique solves a business-rule computation problem on the structured-data
side — it has nothing to do with unstructured document retrieval.** Genie never touches
the content of the documents under `documents_generated/` at any point in this project.
Unstructured retrieval is handled entirely by the `unstructured_agent` node calling
Vector Search on its own: this experiment's unstructured text volume is small (two docx
files), and the business logic can be distilled into just 2 functions, so a lightweight,
self-built Vector Search retrieval path was chosen instead of relying on Genie's native
document-retrieval capability — partly also to set up a comparison against the
alternative path of "handing both structured and unstructured entirely to LangGraph
orchestration." That comparison was originally planned as a formal A/B test, but it was
never actually completed — the code logic is implemented, what's missing is a
systematic comparison result (see "Development retrospective" below).

At the orchestration level overall, `router` uses forced structured output to decide
`next_step`, and `build_graph.py`'s conditional edge then dispatches to
`structured_agent` (calls Genie) or `unstructured_agent` (calls Vector Search), looping
until the information is sufficient.

---

## Repository structure at a glance

```
SalesDuo/
├── src/                   【Runtime】the deployment boundary for mlflow log_model — only code that gets packaged into the Serving container lives here
│   ├── config.py          The project's single environment-variable reading entry point
│   ├── db_client.py       The single entry point for Databricks SDK auth (get_workspace_client())
│   ├── agent.py           The MLflow ResponsesAgent wrapper: predict / predict_stream
│   ├── graph/              LangGraph orchestration: router / structured_agent / unstructured_agent / finalize
│   └── clients/            External-service clients: LLM (ChatDatabricks) / Genie / Vector Search
│
├── ops/                    【Non-runtime】one-off provisioning/deployment scripts, triggered manually, not part of serving
│   ├── structured/          UC SQL Function provisioning + Genie Space data-source/Instructions configuration
│   ├── rag/                 Document parsing/chunking → Delta table → Vector Search endpoint/index
│   └── deploy_model.py     Packages the agent as an MLflow Model, registers it in UC, deploys the Model Serving Endpoint
│
├── app/                    The Databricks Apps frontend: a Streamlit chat box, calls the already-deployed Serving Endpoint
├── prompts/                System prompts used by each node (.prompt files + a shared loader)
├── documents_generated/    The unstructured data source (two synthetic policy documents — see "Data source and disclaimer" below)
├── chat.py                 A local interactive CLI for manually debugging the full LangGraph graph; not packaged into the deployment artifact
├── tests/                   Unit tests + integration tests + eval/ (LLM-as-judge evaluation set and results)
└── docs/                    Development-process documentation: retrospective notes, code-review records, real-environment verification records, a directory-structure deep dive
```

The complete breakdown of directory responsibilities, plus a "which file to change for a
given need" quick-reference table, is in [`docs/REPOSITORY_STRUCTURE.md`](docs/REPOSITORY_STRUCTURE.md).

---

## Tech stack

- **Orchestration**: [LangGraph](https://github.com/langchain-ai/langgraph) (`StateGraph` + a looping conditional edge)
- **Agent contract**: MLflow `ResponsesAgent` (`predict`/`predict_stream`, compatible with the OpenAI Responses API shape)
- **Structured querying**: a Databricks Genie Space (NL2SQL) + Unity Catalog SQL Functions (business-rule computation)
- **Unstructured retrieval**: Databricks Vector Search (a Delta Sync Index) + a self-built top-k retriever
- **LLM**: `ChatDatabricks` (`databricks-langchain`), via the Databricks Foundation Model API
- **Observability**: `mlflow.langchain.autolog()` + a custom white-box `trace` (passed through in `custom_outputs.trace`)
- **Deployment**: a Databricks Model Serving Endpoint (hosting the agent) + Databricks Apps (the Streamlit frontend), with App resources managed via an Asset Bundle (`databricks bundle deploy`)
- **Auth**: native Azure auth (`az login` + Azure resource coordinates resolving the workspace host), no bare PAT used
- **Testing/evaluation**: pytest (offline unit tests + integration tests needing real credentials), LLM-as-judge automated evaluation (`tests/eval/`)
- **Runtime**: Python 3.11

---

## Data source and disclaimer

- **Structured data**: `AdventureWorksLT` in Unity Catalog is Microsoft's own publicly
  released sample database (fictional sales/customer/product business data), not
  corresponding to any real company or real customer information.
- **Unstructured data** (two `.docx` files under `documents_generated/`): AI-generated
  **fictional** company credit-policy/compliance documents, used to simulate a
  "company internal policy document" style of unstructured data source. The company
  names, policy IDs (e.g. `AW-FIN-POL-003`), and specific clause values mentioned in
  them are all invented for practice purposes, don't correspond to any real company's
  real policy, and should not be used as a reference for actual business rules.
- **Test artifacts** (`tests/eval/results/`): a few files under here retain real Genie
  `conversation_id` values left over from actual test runs against the live workspace
  used during development. The Azure resources behind that workspace have since been
  deleted, so these IDs are orphaned and not usable to access anything — they were left
  as-is rather than scrubbed, so the recorded runs stay an accurate, verifiable record
  of what was actually observed.

---

## How to replicate this project

1. **The workspace must be at least Premium tier** (not the free/14-day trial) — Model
   Serving isn't available on a trial workspace, a pitfall actually hit during this
   project (deployment simply hung during the trial period, and could only continue
   after upgrading to Premium). Unity Catalog, Genie, Vector Search, Model Serving, and
   Databricks Apps all need to be enabled (not necessarily on by default depending on
   region/contract). You need an LLM and an embedding endpoint available via the
   Foundation Model API (`.env.example`'s defaults are
   `databricks-meta-llama-3-3-70b-instruct` and `databricks-gte-large-en` — on a
   different workspace, confirm these endpoint names actually exist/are available).

2. **Authentication** (currently native Azure auth, not a universal prerequisite): the
   local machine needs the Azure CLI installed with `az login` already run, and that
   Azure AD identity needs access to the target workspace — the code doesn't accept a
   bare PAT/token, it resolves the host from the three Azure resource coordinates
   `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME`
   ([src/config.py](src/config.py)). This means the workspace must be deployed on Azure
   (this auth approach doesn't apply to a Databricks workspace on AWS/GCP — you'd need
   to switch back to a PAT or change the auth code).

3. **A prerequisite dataset (the easiest one to overlook)**: the
   `UC_CATALOG=adventureworks_dataagent` catalog itself is not created by this repo — it
   reuses an existing catalog that already has the AdventureWorksLT tables imported
   (the `sales`/`person`/`production`/`humanresources`, etc. schemas). This repo only
   creates its own `salesduo_agent_tools` schema to hold UC Functions/Delta tables — it
   won't import the AdventureWorksLT sample dataset for you. Before replicating, you
   need this structured data to already exist, or modify which tables are attached in
   `ops/structured/setup_genie.py`. The two docx policy documents under
   `documents_generated/` ship with the repo as sample unstructured data — no extra
   preparation needed there.

4. **Permissions**: permission to create schemas/Functions/Volumes under `UC_CATALOG`,
   and to create a SQL Warehouse, Genie Space, Vector Search endpoint, Model Serving
   Endpoint, and Databricks App — essentially requiring this Azure AD identity to be an
   admin on the target workspace, or to have CREATE permission on the relevant
   resources.

5. **Local development environment**: Python 3.11 + venv/uv, install `requirements.txt`
   (includes `databricks-connect`, `azure-core`, `azure-storage-file-datalake`), and
   have the Databricks CLI installed for `databricks bundle deploy`.

Once the prerequisites are met, provisioning/deployment runs in dependency order (each
step's output is the next step's required environment variable):

```bash
python -m ops.structured.setup_uc_functions   # Create the UC SQL Functions
python -m ops.structured.setup_genie          # Configure the Genie Space (attach tables + write Instructions)
python -m ops.rag.setup_vs_endpoint           # Create the Vector Search endpoint
python -m ops.rag.ingest_docs                 # Chunk the documents → Delta table → build the index
python -m ops.deploy_model                    # Package the agent, deploy the Model Serving Endpoint
databricks bundle deploy && databricks bundle run salesduo_agent   # Deploy and start the Databricks App
```

---

## Known issues

In production (Databricks App → Model Serving Endpoint), a Genie query against a
structured table fails with `PERMISSION_DENIED`; running directly locally (`chat.py`,
with a personal Azure credential) and Genie Space's own native UI both work completely
normally.

- Directions investigated and confirmed **not to work**: granting schema-level
  `SELECT`/`USE SCHEMA` to the App's service principal, granting ACL access on the
  Genie Space. (Bumping `workload_size` up is a necessary change in its own right — it
  fixes a separate concurrency-related OOM issue, unrelated to this permission
  problem.)
- The identity the Serving Endpoint uses internally to call Genie is
  **unenumerable** — `service_principals.list()`, the Serving Endpoint's own
  `get_permissions()`, and the Genie Space's ACL all fail to surface a grantable
  principal that corresponds to it. The theoretical fix path (changing the Genie
  Space's execution mode to "Run as owner") doesn't even exist as a setting in this
  workspace's Genie Space UI.
- **Current state**: pure policy-document Q&A (going only through `unstructured_agent`)
  works completely in production; questions involving structured data/business-rule
  computation hit `MAX_ROUTER_LOOPS` in production and force a `finalize`, answering
  "information may be incomplete." Local `chat.py` and Genie's native UI are currently
  the effective ways to verify the agent's full logic (including the structured half).
- If replicating this repo with the goal of "getting the full structured+unstructured
  multi-hop path working in production," this problem will very likely reproduce on
  your workspace too (unless the permission model differs) — worth expecting ahead of
  time. The real fix direction is configuring a dedicated service principal for the
  Genie call, authenticated explicitly via client credentials (bypassing the implicit
  automatic authorization from `resources=[...]`), but that requires creating a new SP —
  a sensitive IAM action — so this exercise chose to document it honestly and leave it
  open, rather than keep investigating.

---

## Evaluation results

**1. Offline unit tests**: pure-logic tests under `tests/` (docx chunking, the router
loop-cap safety net, multi-hop retrieval query construction, App error handling not
polluting history) are all runnable offline, with no real Databricks credentials
needed:

```bash
pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py \
       tests/test_unstructured_agent_query.py tests/test_app_error_handling.py -v
```

**2. LLM-as-judge end-to-end evaluation** (`tests/eval/`, 10 questions, covering
`structured_only` / `unstructured_only` / `multi_hop` — `multi_hop` being the core
difficulty this project set out to validate):

| Run | CORRECT | PARTIALLY_CORRECT | INCORRECT | ERROR |
|---|---|---|---|---|
| First run | 6/10 | 1 | 1 | 2 |
| Second run | 8/10 | 0 | 2 | 0 |

The second run fixed the specific problems exposed by the first (three categories of
Genie SQL-generation syntax errors, router's occasional structured-output format
errors), bringing `ERROR` down from 2 to 0. Full raw results (including each question's
model output and white-box trace) are in [`tests/eval/results/`](tests/eval/results/).

**3. Multi-turn memory verification** (3 real calls, covering two independent code
paths — the graph's internal invocation and the `ResponsesAgent` protocol layer):
`genie_conversation_id` reuse across turns, and pronoun resolution ("their credit
limit" → correctly linked to the previous turn's customer) held 3/3 times; 1/3 times it
triggered the known LLM structured-output occasional format error (router degraded to
`finalize`, inferring from historical numbers rather than recomputing — the result
happened to be correct, but the methodology wasn't reliable). Full details in
[`tests/eval/results/multi_turn_memory_verification_20260728.md`](tests/eval/results/multi_turn_memory_verification_20260728.md).

**Limitations, stated honestly**: a 10-question evaluation set is a small sample —
the same question's verdict fluctuated between the two runs, which by itself means 10
questions isn't enough to draw a statistically meaningful conclusion about "what the
overall accuracy is" — only a qualitative statement that "the core multi-hop chain runs
end-to-end, and known issues are clearly documented" is warranted. Parameters like
`top_k=8` are empirical values tuned for these two specific documents, not an optimum
found via grid search. A formal A/B test comparing "Genie handling unstructured data on
its own" vs. "LangGraph orchestration splitting it out" was originally planned — the
code logic for both is implemented, but the comparison itself was never actually run,
which remains a clear, explicitly-acknowledged gap in this project. The full list of
known limitations is in [`docs/DEVELOPMENT_JOURNAL.md`](docs/DEVELOPMENT_JOURNAL.md)
Part 4.

---

## Development retrospective

This project followed a self-authored 7-step plan (auth → structured-data side UC
Function/Genie → unstructured-data side document parsing/Vector Search → LangGraph
orchestration → wrapping as an MLflow ResponsesAgent → local verification →
deployment). More pitfalls were hit along the way than expected; the full troubleshooting
case log (12 cases) is kept in
[`docs/DEVELOPMENT_JOURNAL.md`](docs/DEVELOPMENT_JOURNAL.md) — here are a few
representative ones:

- **"The statement executed successfully" doesn't mean "the resource actually got
  created"**: the two UC SQL Functions silently failed for several rounds because the
  `RETURN` clause's multi-column `SELECT` wasn't wrapped in `STRUCT()` —
  `CREATE OR REPLACE FUNCTION` itself reported success, and only querying
  `information_schema.routines` directly revealed the functions didn't actually exist.
  The lesson: verifying provisioning results can't rely on the statement's return
  status alone.
- **Platform limitations need to be confirmed by testing, not guessed at**: the formal
  API field for attaching a UC Function as a Genie Space "tool" simply couldn't be
  written on this workspace — initially suspected to be a permissions or payload-format
  issue, it took several rounds of trying different shapes to confirm it was a platform
  limitation; the eventual workaround was writing the fully-qualified function name
  directly into the free-text Instructions (see the "Architecture" section above).
- **Switching auth methods once dragged in a dependency incompatibility**: partway
  through the project, auth was switched from a PAT to native Azure auth, which
  surfaced that the Vector Search client package `databricks-ai-search` only supports a
  static token and not the Azure CLI's dynamically-refreshed token — the only option
  was migrating entirely to `databricks-sdk`'s native API.
- **Working locally doesn't mean it works once deployed**: once deployed to Model
  Serving, a string of problems surfaced one after another — mlflow's tracking URI
  defaulting to local SQLite, missing `code_paths` causing import failures, a missing
  Azure storage SDK dependency chain, `workload_size` too small causing a
  multi-process OOM — all of them triggered by real calls, not things that could have
  been anticipated just by reading documentation.
- **One problem was left without being dug into further** (see "Known issues" above):
  the permission error Genie hits under the Serving Endpoint's identity — investigation
  stopped at "this identity itself is unenumerable anywhere in the workspace's IAM
  system"; fixing it further requires creating a dedicated service principal (a
  sensitive IAM action). This time, the choice was to document it honestly rather than
  force a "resolved" conclusion that wasn't true.
- **A planned but unfinished piece**: the structured+unstructured combination could
  have been implemented two ways — "hand it all to Genie's native capability" or "the
  current explicit LangGraph orchestration split" — the original plan was to run a
  formal A/B comparison between the two (effectiveness, latency, observability); the
  code logic for both is implemented, but that comparison test was never actually run
  to completion — honestly flagged as a leftover item rather than pretending it was
  done.

More than "getting every feature fully done," this exercise cared about whether, at
every point something went wrong, the root cause could be pinned down using a white-box
trace and real testing (rather than guessing or reading documentation alone) — this is
also why the whole architecture insists on not using the no-code Agent Bricks, and
insists on accumulating every node's real input/output in `AgentState.trace`.

---

## License

MIT License — see [`LICENSE`](LICENSE).
