# SALESDUO — AdventureWorks Structured+Unstructured Data Agent

## 0. One-sentence goal

Build, on the Databricks ecosystem, a conversational Agent that can query both
**structured data** (the AdventureWorksLT tables in Unity Catalog) and **unstructured
data** (the company documents under `documents_generated/`), ultimately delivered to
the user via a simple chat-box UI (Databricks Apps). The query pattern isn't a simple
side-by-side query-and-summarize — it's **dynamic multi-hop**: for example, first query
an unstructured document to get the user's credit information → combine it with a
business-rule computation → then decide whether to query structured data → the result
may then require going back to query unstructured data once more. The number and order
of hops is **not fixed** — the model decides dynamically at runtime.

---

## 1. Current state and prerequisites

- Unity Catalog already has a catalog named `adventureworks_dataagent`, containing the
  schemas `humanresources`, `information_schema`, `person`, `production`, `purchasing`,
  `sales`. **Reuse this existing catalog by default** — don't create a new catalog
  unless the table structure turns out not to meet the requirements.
- The repo root only contains a `documents_generated/` folder, holding two `.docx`
  company files (the unstructured data source, most likely credit/business-rule
  documents). These two documents need to be parsed, chunked, and indexed.
- Development happens entirely in a local IDE (Claude Code), connecting to the workspace
  via the Databricks SDK / Databricks CLI / Databricks Connect — no manual clicking in
  the Web UI.
- **Only create/modify files within the current `SALESDUO/` project directory.** Don't
  modify, access, or probe any local path outside this directory. Creating resources on
  the Databricks workspace side (catalog/schema/index/endpoint/app) is allowed, but
  naming must clearly carry the project prefix (see section 6) — don't pollute other
  projects' resource namespace.

---

## 2. Architecture overview

```
                     ┌─────────────────────────────────────────┐
                     │  MLflow ResponsesAgent (predict/predict_stream)
                     │  ← the sole external contract; the Databricks Apps chat box only knows this interface
                     └──────────────────┬────────────────────────┘
                                        │
                          LangGraph StateGraph (supervisor-with-cycles)
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
      router node               structured_agent           unstructured_agent
 (decides each step:          (calls the Genie Agent:         (calls Vector Search /
  keep querying structured /   AdventureWorksLT tables +       Knowledge Assistant:
  keep querying unstructured   UC Function business-rule        docx document chunk
  / finish)                    computation)                     retrieval)
              │                         │                         │
              └──── loops back to router until it judges the information sufficient ──┘
                                        │
                                    finalize node
                              (synthesizes all intermediate results into a final answer)
```

**Key design principles (from prior discussion, must be followed):**

1. **Don't use Agent Bricks: Multi-Agent Supervisor (no-code)** — that product's
   routing is a one-shot classification, and doesn't support "dynamically deciding the
   next step, possibly looping, based on the result after running a sub-agent." This
   project must take a **code-first** path.
2. **Don't design it as a two-stage fixed "dispatcher node + summarizer node"
   structure** — dispatching a task and summarizing/re-dispatching are the same
   decision, happening repeatedly, and must be implemented with **one single router node
   + a conditional edge + a looping edge**, not two roles calling each other.
3. Genie is a **stateful, multi-turn conversation** (context maintained via
   `conversation_id`). If the same user request triggers a second call to Genie within
   the flow, it must reuse the same `conversation_id` — don't open a new conversation
   each time.
4. A **`loop_count` cap** (a limit on routing-loop iterations) must be set, to prevent
   an infinite loop caused by a router misjudgment. Once the cap is exceeded, force a
   move to `finalize`, and note in the final answer that the information may be
   incomplete.

---

## 3. Environment variables (the single source of configuration)

**All variable parameters must go through environment variables — no hardcoded catalog
name, schema name, endpoint name, workspace URL, token, etc. is allowed anywhere in the
code.** Maintain a `.env.example` in the project root (no real values, field
descriptions only), with real values kept in a local `.env` (must be added to
`.gitignore`).

Reference variables below (if new ones genuinely need to be added during
implementation, follow the same naming convention and add them to `.env.example` too —
don't add unused placeholder variables on your own initiative, and don't over-define
variables; only define what's actually needed to accomplish the goal):

```bash
# Databricks auth and connection
DATABRICKS_HOST=
DATABRICKS_TOKEN=            # or an OAuth profile, pick one — the code must support both
DATABRICKS_CONFIG_PROFILE=   # optional, used when going through a ~/.databrickscfg profile

# Unity Catalog (structured data, reusing an existing catalog)
UC_CATALOG=adventureworks_dataagent
UC_SCHEMA_SALES=sales
UC_SCHEMA_PERSON=person
UC_SCHEMA_PRODUCTION=production
UC_FUNCTION_SCHEMA=          # the schema holding UC Functions (business-rule computation); recommend creating a new agent_tools schema

# SQL Warehouse (used for Genie queries)
SQL_WAREHOUSE_ID=

# Genie Agent (structured queries)
GENIE_SPACE_ID=

# Unstructured data / Vector Search
DOCS_SOURCE_DIR=documents_generated
UC_VOLUME_PATH=               # the UC Volume path holding the raw docx files and intermediate parsing artifacts
DELTA_TABLE_DOCS_CHUNKS=       # the Delta table holding chunked text, in catalog.schema.table format
VECTOR_SEARCH_ENDPOINT=
VECTOR_SEARCH_INDEX=
EMBEDDING_MODEL_ENDPOINT=      # the embedding model, via the Databricks Foundation Model API

# LLM used for orchestration
LLM_SERVING_ENDPOINT=          # via the Databricks Foundation Model API (don't hardcode a specific model name in code, go through this variable)

# MLflow / evaluation / deployment
MLFLOW_EXPERIMENT_PATH=
MODEL_SERVING_ENDPOINT_NAME=   # the deployed endpoint name (if using Model Serving)
DATABRICKS_APP_NAME=           # the deployed Databricks App name (if using Apps)

# Orchestration safety valve
MAX_ROUTER_LOOPS=5             # the router loop cap, preventing an infinite loop
```

---

## 4. Implementation steps (execute strictly in order, self-verify after each step before moving to the next — don't skip steps, and don't stop mid-way to ask questions)

### Step 1 — Environment and authentication

- Use the Databricks SDK (`databricks-sdk`) to authenticate from environment variables,
  writing a single unified `get_workspace_client()` wrapper in `src/db_client.py` —
  every other part of the project should get its client from here; don't re-initialize
  auth logic in multiple places.
- Verify connectivity: can list the schemas under `UC_CATALOG`.

### Step 2 — Structured-data side: UC Function + Genie Agent

- Check whether the schema specified by `UC_FUNCTION_SCHEMA` exists, creating it via the
  SDK if not (don't create it by hand).
- Implement the business-rule computation logic with a UC Function (SQL or Python) —
  base the specific rules on the content of the `documents_generated/` documents (parse
  the documents first to confirm the rule details, then decide the UC Function's
  parameters and return structure — don't invent rules before reading the documents).
- Use the Databricks SDK to create/configure a Genie Agent (formerly Genie Space), with
  its data source pointing at the relevant AdventureWorksLT schemas (`sales`/`person`/
  `production`, etc. — chosen based on the actual questions needed, don't indiscriminately
  attach all of them), and attach the UC Function above as a Genie tool (Instructions/
  Functions configuration), so Genie calls the function when it needs to compute
  something rather than being asked to hand-write the SQL for the calculation itself.
- Record `GENIE_SPACE_ID` in the environment variables.

### Step 3 — Unstructured-data side: document parsing → Delta table → Vector Search

- Parse the two docx files under `documents_generated/`, chunk them, and write the
  result into the Delta table specified by `DELTA_TABLE_DOCS_CHUNKS` (at minimum
  including: chunk_id, text content, source file name, chunk order).
- Use the Databricks SDK to create a Vector Search endpoint (if it doesn't exist) and a
  Delta Sync Index, with embeddings going through `EMBEDDING_MODEL_ENDPOINT`.
- Write a lightweight retriever wrapper (takes a query, returns the top-k relevant
  chunks), used as a tool called internally by the `unstructured_agent` node. No need to
  additionally wrap it as a Knowledge Assistant (a no-code product) — call the Vector
  Search query API directly from code instead — this is the code-first path, no need to
  detour through the no-code layer.

### Step 4 — Orchestration: LangGraph StateGraph

- Implement per the architecture diagram in section 2: four nodes — `router`
  (conditional judgment), `structured_agent` (calls Genie), `unstructured_agent` (calls
  Vector Search), `finalize` (synthesizes output) — with `router` connected via a
  looping conditional edge.
- State must include at minimum: `messages`, `user_query`, `credit_info`,
  `business_rule_result`, `structured_result`, `genie_conversation_id`, `loop_count`,
  `next_step`.
- The `router` node must check `loop_count` before every decision; exceeding
  `MAX_ROUTER_LOOPS` forces a move to `finalize`.
- `router`'s output must go through structured output (forcing the `next_step` value to
  an enum) — don't rely on parsing free text to decide the next step, to avoid a routing
  parse error.

### Step 5 — Wrap as an MLflow ResponsesAgent

- Implement `predict`/`predict_stream`, internally calling `graph.invoke()`/
  `graph.stream()`, with input/output strictly following the standard schema.
- Configure `MLFLOW_EXPERIMENT_PATH`, ensuring every call automatically produces a trace
  (each node's calls, timing, and intermediate results, for both structured and
  unstructured, should all be inspectable in the trace).

### Step 6 — Local verification

- Cover at least the following three categories of test case, and must include a
  multi-hop case of the "unstructured → structured → unstructured again" shape (this is
  the core difficulty that sets this project apart from simple side-by-side querying —
  be sure to specifically verify whether router's judgment is correct, whether loop
  termination triggers properly, and whether the `loop_count` safety net takes effect):
  1. Questions needing only structured data
  2. Questions needing only unstructured data
  3. Questions needing multi-hop (unstructured → compute → structured → possibly
     unstructured again)
- Run this test set through Agent Evaluation once, recording basic correctness/
  relevance metrics. No need to build out a sophisticated evaluation framework right
  now — being able to verify the core routing logic is correct is enough.

### Step 7 — Deployment

- Deploy via Databricks Apps (not bare Model Serving — since the final deliverable shape
  is a chat-box UI, and Apps comes with a basic chat interface scaffold that fits the
  requirement better; don't build a separate custom frontend framework, unless the
  Databricks Apps default template clearly can't meet even the minimal "simple chat box"
  requirement).
- All deployment artifacts and configuration go through the Databricks CLI / Asset
  Bundle (`databricks bundle deploy`) — don't do a one-off manual-click deployment;
  ensure it's repeatable and reversible.
- After deployment, walk through the complete chain with a real multi-hop case (user
  asks a question → the chat box produces a result), confirming it works end to end.

---

## 5. Coding standards and engineering principles

- **Only implement what the goal actually requires** — don't add things because "might
  be useful later": no A2A, no multi-language UI, no custom embedding-model training, no
  data-source integrations beyond what this project describes, no extra guardrails
  beyond the `router` loop cap (content-safety filtering, PII redaction, prompt-injection
  protection, etc. — add these later if there's an actual future need; don't build them
  now, but don't design the code structure so rigidly that these couldn't be inserted
  later either).
- **Reuse where it makes sense, don't reuse for reuse's sake**: if `structured_agent` and
  `unstructured_agent`'s call patterns (error handling, retries, timeouts) turn out to be
  highly consistent, it's fine to factor out a shared "tool-call wrapper function," but
  don't force an unnecessary base class/interface layer just to "look elegant." Keep the
  two agent nodes' own business logic (calling Genie vs. calling Vector Search)
  implemented independently — don't merge them into one parameterized generic node; the
  reuse gained wouldn't be worth the readability sacrificed.
- Suggested directory structure (adjust as actually needed, but keep responsibilities
  clearly layered):
  ```
  SALESDUO/
  ├── .env.example
  ├── CLAUDE.md
  ├── documents_generated/          # existing raw documents, read-only, do not modify
  ├── src/
  │   ├── db_client.py              # unified Databricks SDK auth wrapper
  │   ├── setup/                    # provisioning scripts (UC Function, Genie config, document parsing/indexing)
  │   ├── graph/                    # LangGraph node and graph definitions
  │   ├── agent.py                  # ResponsesAgent wrapper
  │   └── config.py                 # unified environment-variable reader, the project's single env-reading entry point
  ├── tests/
  ├── databricks.yml                # Asset Bundle configuration
  └── app.py / app-related files    # Databricks Apps entry point
  ```
- When an implementation detail is uncertain (e.g. a specific rule value, chunk size,
  top-k count, etc.), decide it yourself following this priority: "read the documents
  first / use an industry-common default to get a complete closed loop working / prioritize
  getting the end-to-end path running" — **don't stop mid-way to ask** — once decided,
  briefly note the rationale in the relevant code comment.
- Only operate on the filesystem within the `SALESDUO/` directory, throughout.

---

## 6. Databricks resource naming convention

Every newly-created workspace resource (schema, Genie Space, Vector Search
endpoint/index, Delta table, UC Function, App) gets a uniform `salesduo_` prefix, to
avoid confusion with other projects' resources — for example: `salesduo_agent_tools`
(the UC Function schema), `salesduo_docs_chunks` (the Delta table),
`salesduo-vs-endpoint` (the Vector Search endpoint), `salesduo-agent` (the Databricks
App name).

---

## 7. Known risk points (proactively avoid these during implementation, don't wait to hit them first)

1. Executing a UC Function as an agent tool requires **serverless generic compute** (not
   a SQL warehouse) — if the workspace doesn't have this enabled, calls will fail with a
   permission-type error. Confirm this is enabled during the provisioning phase.
2. Genie's multi-turn conversation must reuse `conversation_id`, otherwise cross-turn
   context is lost.
3. There's a risk of the router misjudging and causing an infinite loop —
   `MAX_ROUTER_LOOPS` must actually be implemented and its trigger path tested
   (deliberately construct a case that keeps judging "information insufficient,"
   confirming the 5th attempt forces `finalize` with no error).
4. Vector Search's Delta Sync Index depends on a source Delta table — you can't index a
   raw docx file directly, it must be landed into a table first.
