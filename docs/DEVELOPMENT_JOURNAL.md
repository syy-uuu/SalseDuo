# SalesDuo Development Retrospective — Databricks Agent Stack Learning Notes

> Written for: you, while learning the Databricks agent stack (Genie, Vector Search,
> LangGraph, MLflow ResponsesAgent, Model Serving, Databricks Apps).
>
> **An honest note on where this material comes from**: this project **is not a git
> repository** (`git log` fails outright with `fatal: not a git repository`), so this
> document isn't based on commit history — it's based on: (a) local files' last-modified
> timestamps (`stat`'s mtime, used to reconstruct the development order); (b) the
> complete traces + LLM grading records left behind by two evaluation runs under
> `tests/eval/results/` (this is the only structurally persisted "trace" in this project
> — MLflow's tracing is sent to the Databricks server side, no file is kept locally);
> (c) real error messages/return values actually seen by running diagnostic commands in
> the terminal during development (the full raw output of these commands was recorded in
> the session at the time; quotes in this document are verbatim excerpts, not
> paraphrased from memory). Anywhere **no original record survives and the only option
> is working backward from a code change** is explicitly labeled "the following is an
> inference" — this document doesn't pretend to have complete evidence where it doesn't.
>
> **Path note**: file paths referenced in Parts 1-4 of this document (`src/setup/`,
> `src/tools/`, etc.) are the real paths as of when this was written, reflecting the
> directory structure before the refactor — this document is not retroactively updated.
> See `docs/REPOSITORY_STRUCTURE.md` for the current, post-refactor paths.

---

## Part 1 — Project file map

### Directory tree + dependency relationships

```
SalesDuo/
├── CLAUDE.md                          # Project requirements doc (this retrospective produced v2 afterward)
├── .env / .env.example                # The single source of configuration; every script reads its parameters from here
├── documents_generated/               # Read-only source documents (2 docx files, credit/compliance policy)
│
├── src/
│   ├── config.py                      # 【Config layer】reads .env, the project's single environment-variable entry point
│   ├── db_client.py                   # 【Auth layer】get_workspace_client(), the project's single SDK auth entry point
│   │
│   ├── setup/                         # 【Provisioning scripts】one-off/idempotent resource initialization, never called at runtime
│   │   ├── sql_utils.py               #   Shared: submits SQL to the warehouse and waits for the result
│   │   ├── sql/
│   │   │   ├── calculate_credit_terms.sql            #   UC SQL Function definition (credit rule)
│   │   │   └── check_large_transaction_compliance.sql #  UC SQL Function definition (compliance rule)
│   │   ├── setup_uc_functions.py      #   Creates the schema + runs the two .sql files above
│   │   ├── setup_genie.py             #   Configures the Genie Space (tables, instructions text)
│   │   ├── chunk_docs.py              #   Pure local logic: docx → a list of chunks (no Databricks connection needed)
│   │   ├── ingest_docs.py             #   Creates the UC Volume/Delta table/Vector Search index, calls chunk_docs
│   │   ├── deploy_model.py            #   Registers the MLflow model + creates/updates the Model Serving Endpoint
│   │   └── verify_connection.py       #   Step 1 connectivity verification script
│   │
│   ├── tools/                         # 【Tool-wrapper layer】external-service clients called internally by graph nodes
│   │   ├── genie_client.py            #   ask_genie(): wraps the multi-turn Genie conversation call
│   │   └── retriever.py               #   retrieve(): wraps the Vector Search query
│   │
│   ├── graph/                         # 【Orchestration layer】the LangGraph StateGraph definition
│   │   ├── state.py                   #   The AgentState TypedDict (includes white-box trace fields)
│   │   ├── llm.py                     #   get_llm(): the ChatDatabricks instance used for orchestration
│   │   ├── router.py                  #   The router node: decides the next step + the loop-cap safety net
│   │   ├── structured_agent.py        #   The structured_agent node: calls ask_genie()
│   │   ├── unstructured_agent.py      #   The unstructured_agent node: calls retrieve()
│   │   ├── finalize.py                #   The finalize node: synthesizes information into a final answer
│   │   └── build_graph.py             #   Assembles the nodes above into a graph via conditional/looping edges
│   │
│   └── agent.py                       # 【External-contract layer】the MLflow ResponsesAgent wrapper, the single deployment entry point
│
├── app/
│   ├── app.py                         # 【Delivery layer】the Streamlit chat box, calls the already-deployed Serving Endpoint
│   ├── app.yaml                       #   Databricks Apps' static manifest (start command + env)
│   └── requirements.txt               #   The App's own lightweight dependencies (excludes heavy deps like mlflow/langgraph)
├── databricks.yml                     # Asset Bundle config, defines the App resource
│
├── chat.py                             # 【Local debugging tool】terminal interactive chat, calls build_graph() directly
├── tests/
│   ├── test_chunk_docs.py             #   Pure offline unit test
│   ├── test_router_loop_limit.py      #   Pure offline unit test (loop-cap safety net)
│   ├── test_integration_cases.py      #   Integration tests with a real Databricks connection
│   └── eval/
│       ├── eval_set.json              #   10 evaluation questions + ground truth
│       ├── run_eval.py                #   Runs the eval set automatically + LLM-judge scoring
│       └── results/*.json             #   Full results from each run (with white-box traces)
└── requirements.txt                    # The project's overall dependencies
```

### Mapping each file to a layer in CLAUDE.md's architecture diagram

CLAUDE.md's architecture diagram has four layers: `MLflow ResponsesAgent` →
`LangGraph StateGraph` → the four nodes (`router`/`structured_agent`/
`unstructured_agent`/`finalize`) → the external services each one calls.

| Layer in the architecture diagram | Corresponding file(s) |
|---|---|
| The sole external contract | `src/agent.py` |
| LangGraph orchestration | `src/graph/build_graph.py`, `src/graph/state.py` |
| The router node | `src/graph/router.py` |
| The structured_agent node | `src/graph/structured_agent.py` → calls `src/tools/genie_client.py` → calls the Genie Space (which indirectly calls the two UC Functions `salesduo_agent_tools.calculate_credit_terms` / `check_large_transaction_compliance`) |
| The unstructured_agent node | `src/graph/unstructured_agent.py` → calls `src/tools/retriever.py` → calls the Vector Search Index |
| The finalize node | `src/graph/finalize.py` |
| Provisioning (not in the architecture diagram — the step that "actually builds the resources shown in the diagram") | all of `src/setup/*.py` |
| Deployment (outside the architecture diagram — "how to get the whole graph running for others to use") | `src/setup/deploy_model.py`, `databricks.yml`, `app/*` |

### Call relationships (who calls whom)

```
User request
  └─> src/agent.py: SalesDuoResponsesAgent.predict()
        └─> src/graph/build_graph.py: build_graph().invoke()
              └─> LangGraph dispatches internally via conditional edges:
                    router_node (src/graph/router.py)
                      └─> src/graph/llm.py: get_llm().with_structured_output(RouterDecision)
                    structured_agent_node (src/graph/structured_agent.py)
                      └─> src/tools/genie_client.py: ask_genie()
                            └─> src/db_client.py: get_workspace_client()
                            └─> the Databricks Genie API (internally calls a UC Function)
                    unstructured_agent_node (src/graph/unstructured_agent.py)
                      └─> src/tools/retriever.py: retrieve()
                            └─> the Databricks Vector Search API
                    finalize_node (src/graph/finalize.py)
                      └─> src/graph/llm.py: get_llm()
        All configuration used throughout comes from src/config.py (the single env entry point)
```

The scripts under `src/setup/*.py` are **not part of this call chain** — they're
"provisioning" scripts, executed once only when you (the developer) manually run
`python -m src.setup.xxx`, unrelated to a runtime user request. This is the single
easiest thing to mix up in this project: `setup_genie.py` only **configures** the Genie
Space; the code that actually **uses** Genie lives in `genie_client.py`.

---

## Part 2 — Technical method catalog

Listed in actual development order.

### 1. Unified Databricks SDK auth wrapper

**File**: `src/db_client.py`

```python
@lru_cache(maxsize=1)
def get_workspace_client() -> WorkspaceClient:
    if settings.databricks_config_profile:
        return WorkspaceClient(profile=settings.databricks_config_profile)
    if settings.databricks_host and settings.databricks_token:
        return WorkspaceClient(host=settings.databricks_host, token=settings.databricks_token)
    return WorkspaceClient()
```

**Why written this way**: `databricks-sdk`'s `WorkspaceClient()` supports several auth
methods (PAT, profile, the default credential chain inside a native Databricks
environment). This builds a "priority chain": local development uses the host+token
from `.env` if present; if the code is running inside a Databricks environment (e.g.
inside a Model Serving container), `.env` doesn't exist, so it falls through to the
last, argument-less `WorkspaceClient()` — the SDK automatically detects the current
Databricks environment and picks up the matching credentials. This one line is the key
to this project's local-development and production-deployment code paths being able to
share the same code. `@lru_cache` is there because the client is needed everywhere in
the project — caching a single instance avoids re-authenticating every time.

### 2. UC SQL Function returning a STRUCT

**File**: `src/setup/sql/calculate_credit_terms.sql`

```sql
CREATE OR REPLACE FUNCTION {catalog}.{schema}.calculate_credit_terms(
  relationship_years DOUBLE, ...
)
RETURNS STRUCT<tier: STRING, max_credit_limit_usd: DOUBLE, ...>
RETURN (
  WITH tier_calc AS (...), matrix AS (...), exceed_calc AS (...)
  SELECT STRUCT(tier, ..., required_approval)   -- key: wrap it in STRUCT()
  FROM exceed_calc
);
```

**Why written this way**: a SQL scalar function's `RETURN` must be a **single
expression** (matching the type declared in `RETURNS`) — it can't be a multi-column
`SELECT`. The business rule needs to return 9 fields at once (credit tier, limit cap,
approval requirement, ...) — a direct `SELECT col1, col2, ... FROM x` gets flagged by
Spark SQL as "a scalar subquery returning 9 columns" and errors. The fix is to wrap
those columns in a `STRUCT(...)` expression, so the whole SELECT returns just "one
column" (whose type happens to be a STRUCT), satisfying `RETURN`'s requirement. The
caller can then pull values out via `.fieldname`, e.g.
`calculate_credit_terms(...).tier`.

### 3. Genie Space configuration: read-modify-write serialized_space

**File**: `src/setup/setup_genie.py`

```python
space = client.genie.get_space(settings.genie_space_id, include_serialized_space=True)
parsed = json.loads(space.serialized_space)
_merge_tables(parsed, table_fullnames)          # adds tables into parsed["data_sources"]["tables"]
_set_text_instructions(parsed, _build_instructions())  # modifies parsed["instructions"]["text_instructions"]
client.genie.update_space(space_id=..., serialized_space=json.dumps(parsed))
```

**Why written this way**: a Genie Space's configuration (which tables are attached, the
Instructions text) is, at the API level, one big opaque JSON string (`serialized_space`)
— there's no public field-level API (you can't update just one field), only "read the
whole thing → modify it in a Python dict → write the whole thing back." This JSON's
field names (`data_sources.tables`, `instructions.text_instructions`) weren't found in
any documentation — they were reverse-engineered by "configuring it once in the UI, then
reading back what actually got stored via `get_space`" (see Part 3 Case 1 for details).

### 4. Parsing paragraphs and tables in raw XML order with python-docx

**File**: `src/setup/chunk_docs.py`

```python
def _iter_block_items(document):
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)
```

**Why written this way**: `python-docx`'s default `document.paragraphs` and
`document.tables` are two **separate** lists, which loses the information of "which
came first, this paragraph or that table, in the original document" (e.g. "the table in
section 2" and "the table in section 3" both get flattened into `document.tables[0]`,
`document.tables[1]`, with no way to tell which belongs to which section). Iterating the
XML body's children directly, constructing objects dynamically based on tag type
(`w:p` = paragraph, `w:tbl` = table), preserves the true document-structure order — only
this way can chunking correctly attribute each table to the section heading it actually
belongs under.

### 5. UC Volume creation + raw file upload

**File**: `src/setup/ingest_docs.py`

```python
client.volumes.create(catalog_name=catalog, schema_name=schema, name=volume_name, volume_type=VolumeType.MANAGED)
client.files.upload(dest, f, overwrite=True)
```

**Why use it**: a Unity Catalog Volume is Databricks' standard location for "storing
unstructured files" (comparable to a directory in S3/ADLS, but governed by UC
permissions). This just archives a copy of the raw docx files — the content actually
used for retrieval is the chunked text landed in a Delta table in step 6 below, not
these two docx files themselves.

### 6. Vector Search Delta Sync Index

**File**: `src/setup/ingest_docs.py` / `src/tools/retriever.py`

```python
vsc.create_delta_sync_index(
    endpoint_name=..., index_name=..., primary_key="chunk_id",
    source_table_name=settings.delta_table_docs_chunks,
    pipeline_type="TRIGGERED",
    embedding_source_column="content",
    embedding_model_endpoint_name=settings.embedding_model_endpoint,
)
...
index.similarity_search(columns=[...], query_text=query, num_results=k)
```

**Why used this way**: a Delta Sync Index is the index type in Vector Search that
"automatically handles embedding + automatically syncs Delta table changes for you"
(as opposed to a "Direct Vector Access Index," where you compute the vectors yourself
and pass them in). `pipeline_type="TRIGGERED"` means it doesn't sync in real time —
instead, an incremental update only happens when `index.sync()` is called manually —
this project's documents barely change, so real-time sync isn't needed and `TRIGGERED`
saves resources. Querying calls `similarity_search`, passing the raw text directly as
`query_text` — the embedding computation happens automatically on the Vector Search
server side, no need to call an embedding model yourself first.

### 7. LangGraph StateGraph: a conditional edge implementing loop routing

**File**: `src/graph/build_graph.py`

```python
graph.set_entry_point("router")
graph.add_conditional_edges(
    "router", route_after_router,
    {"structured": "structured_agent", "unstructured": "unstructured_agent", "finalize": "finalize"},
)
graph.add_edge("structured_agent", "router")     # key: loops back to router when done, not straight to the next step
graph.add_edge("unstructured_agent", "router")
graph.add_edge("finalize", END)
```

**Why written this way**: this is the core code that puts CLAUDE.md's "router + looping
edge" design into practice. `add_conditional_edges`'s second argument,
`route_after_router`, is a plain Python function that reads `state["next_step"]` and
returns a string; LangGraph looks that string up in the third argument (a dict) to
decide the next node. **The loop** works because `structured_agent`/`unstructured_agent`
both unconditionally `add_edge` back to `router` when they finish, rather than
connecting straight to `finalize` or the next business node — router re-judges "is
there enough information now" every single time, so the same node can be visited any
number of times within one request (until `loop_count` hits its cap, or router
proactively judges it's enough).

### 8. LangGraph state accumulation: Annotated + operator.add

**File**: `src/graph/state.py`

```python
class AgentState(TypedDict, total=False):
    ...
    trace: Annotated[list[TraceStep], operator.add]
```

**Why written this way**: most `AgentState` fields (e.g. `structured_result`) are
"overwrite-style" — each node's return value simply replaces the old one. But `trace`
needs to be "append-style" — if router runs 5 times, we want to see 5 records, not just
the last one. Annotating a field as `Annotated[list[X], operator.add]` tells LangGraph
that, when merging a node's return value into the global state, it should call
`operator.add` on this field (i.e. list concatenation) instead of overwriting. So each
node only needs to `return {"trace": [one new record for this step]}` — it doesn't have
to manually concatenate the history itself; LangGraph automatically appends the new
record onto the existing list.

### 9. Pydantic + with_structured_output forces the routing output format

**File**: `src/graph/router.py`

```python
class RouterDecision(BaseModel):
    next_step: NextStep = Field(description="...")
    reason: str = Field(description="...")

llm = get_llm().with_structured_output(RouterDecision)
decision: RouterDecision = llm.invoke(messages)
```

**Why used this way**: letting the LLM output free text and then parsing "which branch
it wants to pick" with a regex/keyword search has a high error rate (the model might say
"I think we should check the structured data" rather than the exact word `structured`).
`with_structured_output(RouterDecision)` is a LangChain capability that converts the
Pydantic model into a "tool definition" passed to the model as forced tool-calling; the
model's output is parsed straight into a `RouterDecision` instance, and the `next_step`
field's type is `Literal["structured","unstructured","finalize"]` — the model **can
only** pick one of these three values, nothing else — this is exactly CLAUDE.md's
requirement that "router's output must go through structured output, not free-text
parsing."

### 10. Genie conversation_id passed across nodes to implement multi-turn memory

**File**: `src/graph/structured_agent.py` + `src/tools/genie_client.py`

```python
# structured_agent.py
answer = ask_genie(question, conversation_id=state.get("genie_conversation_id"))
update = {..., "genie_conversation_id": answer.conversation_id}

# genie_client.py
def ask_genie(question, conversation_id=None):
    if conversation_id:
        wait = client.genie.create_message(space_id=..., conversation_id=conversation_id, content=question)
    else:
        wait = client.genie.start_conversation(space_id=..., content=question)
```

**Why written this way**: Genie's multi-turn conversation is server-side state
maintained via `conversation_id` — the first question uses `start_conversation` (the
server assigns a new `conversation_id`); if the same user request needs to ask Genie
again later (e.g. a multi-hop scenario where router decides a second Genie query is
needed after the first), it must use `create_message` with the **same**
`conversation_id`, otherwise Genie treats the second question as a brand-new
conversation and all prior context is lost. The approach here: `ask_genie()`'s return
value carries the `conversation_id` used this time; `structured_agent_node` writes it
back into `AgentState`; the next time this node gets dispatched to by router, it reads
last time's `conversation_id` out of state to pass in — `AgentState` itself is already
"memory shared across nodes and across loop iterations," no extra storage needed.

### 11. MLflow ResponsesAgent: predict wraps a LangGraph invoke

**File**: `src/agent.py`

```python
class SalesDuoResponsesAgent(ResponsesAgent):
    def __init__(self):
        self._graph = build_graph()

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        result = self._run_graph(request)          # internally calls self._graph.invoke(initial_state)
        final_text = self._final_text(result)
        output_item = self.create_text_output_item(text=final_text, id=str(uuid.uuid4()))
        return ResponsesAgentResponse(output=[output_item], custom_outputs={"trace": result.get("trace", [])})
```

**Why written this way**: `mlflow.pyfunc.ResponsesAgent` is a standard protocol defined
by Databricks/MLflow "for deploying chat-style applications" (matching OpenAI's
Responses API shape) — Databricks Apps and Model Serving both only speak this
interface. This class is essentially an "adapter": externally it receives a standard
`ResponsesAgentRequest` (`.input` is a message list), internally converts it into our
own `AgentState` shape to call `graph.invoke()`, then converts LangGraph's output back
into a standard `ResponsesAgentResponse` once it finishes. `predict_stream` works the
same way but returns a generator, because the graph internally can't produce output
token by token — so it's "run the whole thing to completion, then package the result
into one delta event + one done event," impersonating a streaming interface without
being genuine token-by-token streaming. `custom_outputs` is a field the protocol
specifically reserves for "extra debugging information" — this is where the white-box
trace gets stuffed (see Part 3 Case 10).

### 12. mlflow.pyfunc.log_model: models-from-code + code_paths + resources

**File**: `src/setup/deploy_model.py`

```python
mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")
model_info = mlflow.pyfunc.log_model(
    name="agent",
    python_model=_AGENT_ENTRYPOINT,       # points at the file path src/agent.py
    pip_requirements=_REQUIREMENTS_FILE,
    code_paths=[str(_PROJECT_ROOT / "src")],   # bundles the entire src/ package into the model
    resources=_resources(),               # declares runtime dependencies on Genie/Vector Search/Warehouse/Function
    registered_model_name=registered_model_name,
)
```

**Why used this way**: `python_model=<file path>` is mlflow's more recent "models from
code" usage (as opposed to the older approach of passing a Python object serialized via
pickle) — the benefit is not depending on pickle-compatibility issues. `code_paths`
solves the problem of "will an import in agent.py like `from src.xxx import yyy`, which
depends on other local files, resolve in the deployment environment" (see Part 3 Case
7). `resources=[...]` is Databricks' extension to mlflow: it declares which Databricks
resources this model depends on at runtime (Genie Space, Vector Search Index, SQL
Warehouse, UC Function) — once deployed as Model Serving, Databricks automatically
authorizes the Serving Endpoint's identity for those services, no need to manually pass
tokens to them in code — though this mechanism currently still has an unresolved
permission issue specifically for Genie's underlying data access (see Part 3 Case 11).

### 13. Model Serving Endpoint: ServedEntityInput + environment_vars

**File**: `src/setup/deploy_model.py`

```python
served_entity = ServedEntityInput(
    entity_name=registered_model_name, entity_version=model_version,
    workload_size="Small", scale_to_zero_enabled=True,
    environment_vars=_serving_environment_vars(),   # explicitly passes every non-secret config value
)
client.serving_endpoints.create_and_wait(name=endpoint_name, config=EndpointCoreConfigInput(...))
```

**Why used this way**: `scale_to_zero_enabled=True` means this endpoint isn't billed
when there's no real traffic (compare with a Vector Search endpoint, which is a
long-running service with no such option). `environment_vars` exists because the
deployed model runs in an isolated container with no local `.env` file of ours — the
environment variables `src/config.py` reads must be explicitly passed in by whoever
deploys it (i.e. this code), or the server side has no way to know which Genie Space,
which Vector Search Index, to connect to.

### 14. Databricks Asset Bundle + Apps two-stage deployment

**File**: `databricks.yml` + the CLI commands used to deploy

```bash
databricks bundle validate     # read-only validation of whether the config is correct
databricks bundle deploy       # uploads code + creates/updates the resource definition, but doesn't start the App
databricks bundle run salesduo_agent   # actually deploys the code onto running compute and starts it
```

**Why it's two steps**: this is the general pattern Databricks Asset Bundle uses for
resource types like "Apps" and "Jobs" — `deploy` only syncs local files to the workspace
and registers/updates the resource's **definition** (similar to `git push` to a config
repo); actually making it run (allocating compute, installing dependencies, starting the
process) requires the extra `bundle run` step (for a Job this step means "trigger one
run"; for an App it means "start this App's long-running process"). Not knowing this
distinction the first time, `bundle deploy` finishing looked like "deployment complete,"
but querying `client.apps.get()` showed `compute_status.state` was `STOPPED`, and the
page wouldn't load — this is a real pitfall this project actually hit (see Part 3 Case
12).

### 15. LLM-as-judge automated evaluation

**File**: `tests/eval/run_eval.py`

```python
class Grade(BaseModel):
    verdict: Literal["CORRECT", "PARTIALLY_CORRECT", "INCORRECT"]
    reasoning: str

grade_result = get_llm().with_structured_output(Grade).invoke([...question + ground truth + grading notes + actual answer...])
```

**Why used this way**: the ground-truth answers for the 10 questions are natural-language
descriptions (e.g. "maximum credit limit $250,000, payment term Net 45 days"), which
can't be judged with simple string equality against the agent's answer (the agent might
phrase it differently but mean the same thing). Using the same LLM (the same
`get_llm()` used for routing) as a judge, feeding it "ground truth + grading notes +
the agent's actual answer" together and having it produce a three-way verdict, is
currently the most common approach in the field for automated evaluation of
"generative" answers — more flexible than string matching, faster than reviewing every
one by hand.

---

## Part 3 — Troubleshooting case log

### Case 1: The Genie Space UI's attach-function feature turned out to be unnecessary

**Symptom**: initially assumed there had to be an "attach a UC Function as a tool"
entry point somewhere in the Genie Space's Configure screen (something like a "SQL
Functions" tab expected under "Instructions"), but searching through both Instructions
(just a single General Instructions text box) and Examples (Example Query/Filter/
Measure/Field/Join, all requiring hand-filled SQL or semantic-layer definitions) never
turned up an entry point for picking a function directly from Unity Catalog.

**Investigation**:
1. Tried calling `client.genie.update_space(..., serialized_space=...)` directly with a
   guessed field name (`instructions.sql_functions`, taken from a field name seen in a
   Databricks solution doc on GitHub), which returned a strange error:
   `Certified answer 'xxx' does not exist`.
2. Retried with many different parameter shapes (a string array, an array of objects
   with ids, sorted differently...), all failing, and every failure pointed at the
   concept of "certified answer," which didn't seem to be the same thing as "function" at
   all.
3. Used a diagnostic script to first `get_space(include_serialized_space=True)`, print
   the current config, and found the top level only had `version` and `data_sources`
   fields — no `instructions` field at all, meaning the originally guessed field name had
   never taken effect.
4. Had the user manually fill in some General Instructions text in the Genie UI and save
   it, then re-ran `get_space` to read it back — this time seeing the real JSON
   structure: `instructions.text_instructions` is a list, elements shaped like
   `{"id": "<32-char hex>", "content": [...]}`.
5. Wrote the two functions' **fully-qualified names**
   (`catalog.schema.calculate_credit_terms`) directly into this text — Genie then used
   this fully-qualified name to call it when generating SQL on its own — no "attach as
   tool" UI feature needed at all.

**Root cause**: the `instructions.sql_functions` field (regardless of whether the
correct JSON structure had been found) actually belongs to Genie's "Certified Answer"
feature — a different thing entirely from "letting Genie know a function exists that it
can call." Whether Genie can call a given function only depends on that function's
fully-qualified name appearing in text it can see (instructions), and the function
itself being executable in Unity Catalog — no "registration" step is needed at all.

**Fix**: `_build_instructions()` in `src/setup/setup_genie.py` writes both functions'
fully-qualified names and their **exact return field names** into the instructions
text:

```python
def _build_instructions() -> str:
    fn_schema = f"{settings.uc_catalog}.{settings.uc_function_schema}"
    return f"""...
- {fn_schema}.calculate_credit_terms(...)
  Returns STRUCT fields: tier, advance_payment_min_pct, ..., required_approval
..."""
```

**Verification**: asked Genie a question directly that requires calling this function,
and checked whether the correct fully-qualified function call appeared in the
`attachment.query.query` it returned (the SQL actually generated and executed) —
confirmed `adventureworks_dataagent.salesduo_agent_tools.calculate_credit_terms(...)`
was called correctly and returned fields.

**Corresponding file**: `src/setup/setup_genie.py`

---

### Case 2: The UC Function was never actually created successfully, all the way back to Step 2

**Symptom**: Genie failed with
`UNRESOLVED_ROUTINE: Cannot resolve routine calculate_credit_terms` — initially assumed
this was a permission problem (the schema wasn't on Genie's search path).

**Investigation**:
1. Ran `SELECT adventureworks_dataagent.salesduo_agent_tools.calculate_credit_terms(3.0,
   200000.0)` directly on the SQL Warehouse with a personal token — got the **same**
   UNRESOLVED_ROUTINE error — meaning this wasn't Genie-specific at all, the function
   simply didn't exist.
2. Queried the `information_schema.routines` table with
   `WHERE routine_schema = 'salesduo_agent_tools'` — got 0 rows back — confirming the
   function had never actually been created.
3. Ran `CREATE OR REPLACE FUNCTION ...` again by hand — this time it didn't pass
   silently, it errored directly:
   `INVALID_SUBQUERY_EXPRESSION.SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN:
   Scalar subquery must return only one column, but got 9`.

**Root cause**: the function body was `RETURN (SELECT col1, col2, ..., col9 FROM x)` — a
scalar SQL function's `RETURN` can only be a single expression, and a multi-column
`SELECT` doesn't satisfy that. **More importantly**: the very first time Databricks'
`CREATE OR REPLACE FUNCTION` statement was run, for some reason (possibly related to
the SQL Warehouse's state at the time / whether it was the first compilation — the exact
mechanism was not further investigated) it did not surface this type error, and looked
like it "succeeded," which let this bug sit unnoticed for a long time — it was only
found, while re-running the evaluation set this time, by directly checking against
`information_schema` and discovering the function simply didn't exist. **This part —
why the very first run didn't error — has no surviving record of the original command
output; the following is an inference**: most likely that very first call went down a
different code path due to some environment/argument difference, or it genuinely did
error but the return status wasn't carefully checked before moving on to later steps.

**Fix**: wrap the multi-column `SELECT` in the `RETURN` body in `STRUCT()`:

```diff
- SELECT
-   tier,
-   advance_payment_min_pct,
-   ...
-   required_approval
- FROM exceed_calc
+ SELECT STRUCT(
+   tier,
+   advance_payment_min_pct,
+   ...
+   required_approval
+ )
+ FROM exceed_calc
```

**Verification**: after the fix, re-ran the `information_schema.routines` query and
confirmed both function records genuinely existed; then called the function directly
via `SELECT` and verified both the return field structure and the computed result were
correct (e.g. `relationship_years=3, annual_purchase_volume_usd=200000` should land in
Tier 3; `requested_credit_amount_usd=800000` exceeds Tier 3's $250,000 cap, with an
overage of 220%, requiring `VP_SALES_AND_CFO_SIGNOFF` — the actual output matched a
hand-computed check exactly).

**Corresponding files**: `src/setup/sql/calculate_credit_terms.sql`,
`src/setup/sql/check_large_transaction_compliance.sql`

---

### Case 3: Three different categories of errors in Genie-generated SQL (the same class of problem recurring)

**Symptom**: asking structurally similar multi-hop questions about the same store
(Brakes and Gears) across different runs, Genie's generated SQL failed in three
completely different places on different occasions.

**Investigation**: thanks to the white-box `trace` added in Part 2, every time it was
possible to see exactly where the problem was directly from
`trace[...]["sql_queries"]` (the raw SQL Genie actually generated) and
`trace[...]["error"]` (the specific error Genie returned), without needing to guess.

**Root causes (three independent examples, each backed by real error text)**:

1. **Skipping an intermediate table and joining incorrectly**: the SQL Genie generated
   joined `salesorderheader.customerid` directly to `store.businessentityid`
   (`h.customerid = s.businessentityid`), skipping the intermediate `customer` table.
   The correct join path should be `store.businessentityid = customer.storeid`,
   `customer.customerid = salesorderheader.customerid`. This incorrect join doesn't
   raise a SQL syntax error (both sides are valid columns) — it silently returns 0
   rows/NULL, which then made `calculate_credit_terms`, fed a NULL
   `annual_purchase_volume_usd`, compute the wrong "New Customer" tier (should have been
   Tier 3), which cascaded into computing a 500% overage (the correct answer was 20%).

2. **`DATE_TRUNC`'s and `DATEDIFF`'s quoting conventions swapped**: the generated SQL
   wrote `DATE_TRUNC(YEAR, CURRENT_DATE)` (unquoted), which errored with
   `UNRESOLVED_COLUMN.WITHOUT_SUGGESTION: A column ... with name YEAR cannot be
   resolved`. Testing confirmed `DATE_TRUNC`'s time unit needs a **quoted** string
   (`'YEAR'`), while `DATEDIFF` conversely needs an **unquoted** keyword
   (`DATEDIFF(YEAR, ...)`) — the two functions happen to have opposite parameter
   conventions, and Genie over-generalized from an earlier "don't quote DATEDIFF"
   instruction we'd given it, incorrectly applying the same rule to `DATE_TRUNC`.

3. **Calling a scalar function as if it were a table function**: the generated SQL used
   the table-function-call syntax `FROM order_stats, LATERAL calculate_credit_terms(...)
   AS ct` to call a scalar function, which errored with
   `NOT_A_TABLE_FUNCTION: ... appears as a table function here, but the function was
   defined as a scalar function`.

**Fix**: each time, the fix was adding a specific, targeted corrective note into
`setup_genie.py`'s instructions text — not trying to "generically" guard against every
possible SQL mistake:

```diff
+ Important table join path (don't skip the customer table and join store directly to salesorderheader):
+ store.businessentityid = customer.storeid
+ customer.customerid = salesorderheader.customerid
...
+ Note the time functions' quoting conventions differ — don't mix them up:
+ - DATEDIFF's time unit is an unquoted keyword: DATEDIFF(YEAR, start, end)
+ - DATE_TRUNC's time unit is a quoted string: DATE_TRUNC('YEAR', some_date)
...
+ This is a scalar function (SCALAR, not a table function) — it can only appear in
+ the SELECT clause's column expressions, never written into a FROM/LATERAL clause
+ as if it were a table, otherwise it fails with NOT_A_TABLE_FUNCTION.
```

**Verification**: after each guidance change, immediately re-asked the same real
question and checked whether the newly generated SQL still made the same mistake. All
three were verified as "no longer making this specific mistake after the fix," but it
was **not** verified that "no new SQL mistake of any kind would ever occur again" — this
is listed as a known limitation in Part 4.

**Corresponding file**: `src/setup/setup_genie.py`

---

### Case 4: Vector retrieval misses target passages — low-information metadata chunks crowd out the truly relevant passages

**Symptom**: for the evaluation-set question "if a customer's credit limit overage
exceeds 15%, who needs to sign off on approval?", the agent retrieved repeatedly 5 times
(exhausting `MAX_ROUTER_LOOPS`), and every retrieval result failed to include the
passage that actually covers the approval workflow, ultimately answering incorrectly
(citing the "Approved By: Chief Financial Officer & VP of Risk Management" line from the
document header metadata, mistaking it for the approver).

**Investigation**:
1. Looking at `unstructured_agent`'s `retrieved_chunks` for each step in the evaluation
   result's `trace`, found all 5 consecutive calls returned the same 5 chunks, all
   tagged `section_title: "Header"` (the document metadata lines: Policy ID, Effective
   Date, Approved By, Applicable To).
2. Called `retrieve(query, k=26)` manually (i.e. pulled out every chunk to see the full
   ranking), and confirmed the passage that actually covers the approval workflow
   ("3. Exception Handling and Special Approval Workflow") did exist in the index, but
   ranked 15th, with a score (0.462) lower than several Header metadata lines (scores
   0.48-0.50).

**Root cause**: these Header metadata lines are short and generic ("Effective Date:
July 1, 2026" and similar), and seem to be "tangentially relevant to anything" in vector
space, causing them to score moderately-to-highly on almost any query — crowding out
long passages that would only clearly win with a precise semantic match. This is a
common noise pattern for short/generic text in embedding-based retrieval, largely
independent of which specific embedding model is used.

**Fix**:
1. Excluded this kind of document-header metadata row from indexed chunks entirely
   (`src/setup/chunk_docs.py`):
   ```diff
     is_key_value_table = len(rows[0]) == 2
   + if is_key_value_table:
   +     # a metadata table, low retrieval value and high noise — skip indexing it entirely
   +     continue
   ```
2. Retested after removing it — the target passage climbed from 15th to 7th place —
   **still** not in the top-5, so also bumped retrieval's `top_k` from 5 to 8
   (`src/graph/unstructured_agent.py`):
   ```diff
   - _TOP_K = 5
   + _TOP_K = 8
   ```

**Verification**: reran the same question (`build_graph().invoke(...)`) and confirmed
the final answer correctly mentioned both "VP of Sales" and "CFO"; reran the full
evaluation set and this question went from INCORRECT to CORRECT.

**Corresponding files**: `src/setup/chunk_docs.py`, `src/graph/unstructured_agent.py`

---

### Case 5: The routing LLM reproducibly generates malformed JSON when the reason field is longer

**Symptom**: while running multi-hop evaluation questions, the router node failed with
`openai.BadRequestError: ... Model response did not respect the required format ...
Model Output: <function=RouterDecision>{"next_step": "finalize", "reason": "...)}` (note
an extra, unwanted closing parenthesis `)` at the end of the JSON string).

**Investigation**: reran the same question twice, and both times it failed identically
in the same place — confirming this wasn't random one-off noise, but that this specific
model (`databricks-meta-llama-3-3-70b-instruct`), when the `reason` field's content is
longer/more complex, unreliably emits one extra trailing character when forced into
tool-calling-format output.

**Root cause**: the underlying model's ability to follow the required format under
forced structured output (a tool-calling schema) is limited, and correlated with output
content length/complexity — this is a generation-quality issue with the model itself,
not a bug in our own code logic.

**Fix**: added a retry plus a safe fallback around this one LLM call, rather than trying
to "fix" the model's output (which isn't possible):

```diff
+ _MAX_DECISION_ATTEMPTS = 3
  ...
+ for _ in range(_MAX_DECISION_ATTEMPTS):
+     try:
+         decision = llm.invoke(messages)
+         break
+     except Exception as exc:
+         last_error = exc
+ if decision is None:
+     return {..., "next_step": "finalize", "router_reason": f"Routing model produced malformed output {...} times in a row; safely degrading to finalize"}
```

**Verification**: reran the same previously-failing question and confirmed it got a
valid `RouterDecision` this time (possibly on the 2nd or 3rd retry), with the flow
continuing normally; also confirmed that even if every retry fails, it falls through to
the "forced finalize" branch rather than crashing the whole request with an exception
surfaced to the user.

**Corresponding file**: `src/graph/router.py`

---

### Case 6: mlflow registered the model to local SQLite, not the real Databricks workspace

**Symptom**: `deploy_model.py` printed "registered model ... version 1," looking
successful, but immediately afterward, creating the Model Serving Endpoint failed with:
`ResourceDoesNotExist: Registered model ... does not exist. It might have been
deleted.`

**Investigation**:
1. Queried this model name directly with the Databricks SDK's
   `client.registered_models.get(...)` — also said "does not exist."
2. Reproduced the same "does not exist" using
   `mlflow.MlflowClient().get_registered_model(...)` (after first fixing the auth with
   the correct `databricks-uc` registry URI).
3. Checked the project directory and found two files/folders that hadn't been there
   before: `mlflow.db` (a SQLite database file) and `mlruns/` — these are mlflow's
   local default storage location when **no tracking/registry URI is explicitly set**.

**Root cause**: `deploy_model.py` had never called `mlflow.set_tracking_uri("databricks")`
/ `mlflow.set_registry_uri("databricks-uc")`. When running mlflow code inside a
Databricks Notebook/Job, these two URIs are usually auto-configured by the environment,
but this project runs this script **directly from a local IDE**, outside that
auto-configured context, so mlflow obediently followed its own default behavior and
wrote everything to local files — the "registered successfully" message was true
(relative to that fake local registry), it's just that the registry it wrote to wasn't
the real Databricks workspace at all.

**Fix**:

```diff
  def log_and_register_model() -> str:
+     mlflow.set_tracking_uri("databricks")
+     mlflow.set_registry_uri("databricks-uc")
      mlflow.set_experiment(settings.mlflow_experiment_path)
      ...
```
and cleaned up the accidentally-generated `mlflow.db`, `mlruns/`, adding them to
`.gitignore` to prevent them being committed later.

**Verification**: reran the script — this time the output showed a real Databricks
experiment URL (shaped like
`https://adb-xxx.azuredatabricks.net/ml/experiments/...`), then queried
`get_registered_model` directly via the mlflow client and confirmed it could actually
be found this time.

**Corresponding files**: `src/setup/deploy_model.py`, `.gitignore`

---

### Case 7: The model couldn't import the `src` package after deployment

**Background (this case has no real error record — it was designed around
preemptively, as noted honestly here)**: before ever actually hitting this error, it
was already clear that `mlflow.pyfunc.log_model(python_model="src/agent.py", ...)`'s
"models from code" style, if `agent.py` has an import like
`from src.config import settings` depending on other files in the same project, would
very likely fail to import once deployed into an isolated Serving container — this was
proactively deduced by reading mlflow's source (`mlflow/utils/model_utils.py`'s
`_add_code_to_system_path` / `_validate_and_copy_code_paths`), **without ever letting it
actually fail first and then fixing it**.

**Investigation (this was "prevention," not "troubleshooting")**: read mlflow's source
to confirm two things:
1. `code_paths=[X]` copies the whole of `X` into `code/<X's last directory name>` under
   the model artifact directory.
2. When the model loads, what gets added to `sys.path` is the `code/` level, **not**
   the `code/<X's directory name>` level.

**Root cause (avoided proactively, not fixed after the fact)**: without passing
`code_paths`, once `src/agent.py` is extracted and run on its own,
`from src.config import settings` would fail with `ModuleNotFoundError` because the
`src` package can't be found.

**Fix**:

```python
_CODE_PATHS = [str(_PROJECT_ROOT / "src")]
...
mlflow.pyfunc.log_model(..., code_paths=_CODE_PATHS, ...)
```
Because `_CODE_PATHS` holds `.../SalesDuo/src` (whose last directory name happens to be
`src`), after copying it lands at `code/src/`, and `code/` gets added to `sys.path`, so
`import src.config` can be found at `code/src/config.py` — the two line up exactly.

**Verification**: once deployed, called the Serving Endpoint directly with a simple
question ("how many days is a Tier 3 customer's payment term?"), and it returned the
correct answer instead of an import error — indirectly confirming the `src` package can
be imported normally inside the container.

**Corresponding file**: `src/setup/deploy_model.py`

---

### Case 8: Missing Azure storage dependency chain

**Symptom**: `log_model`, while uploading model version files to Unity Catalog, failed
with `ModuleNotFoundError: No module named 'azure'`. After installing `azure-core` and
rerunning, it then failed with `ModuleNotFoundError: No module named 'azure.storage'`
(more specifically, `azure.storage.filedatalake`).

**Investigation**: looked directly at the error stack, tracing it to mlflow's internal
`mlflow/utils/_unity_catalog_utils.py`, where the `get_artifact_repo_from_storage_info`
function, based on the credential type UC returned
(`azure_user_delegation_sas`), routed to `AzureDataLakeArtifactRepository`, a class
that internally does `from azure.storage.filedatalake import DataLakeServiceClient` —
meaning this workspace's underlying Unity Catalog storage is Azure Data Lake Storage,
and uploading a UC model artifact itself needs these two Azure SDK packages, unrelated
to whether "the deployed model at runtime" needs them (at runtime it only reads data, it
doesn't upload files itself).

**Root cause**: the local development environment didn't have these two Azure SDK
packages preinstalled (because the initial dependency list was prepared with an
AWS/generic scenario in mind, not anticipating this workspace's backend being Azure).

**Fix**: `pip install azure-core azure-storage-file-datalake`, added to
`requirements.txt`.

**Verification**: reran `deploy_model.py` — this time saw a real "Uploading artifacts:
100%" progress bar, printing "Created version 'N'".

**Corresponding file**: `requirements.txt`

---

### Case 9: The Serving Endpoint needs runtime environment variables passed explicitly

**Symptom**: the Serving Endpoint deployed successfully, state `READY`, but calling it
for real failed with:
`Missing required environment variable(s)/setting(s): vector_search_index. Add them to
.env and try again.`

**Investigation**: this error message itself was raised by our own code
(`src/config.py`'s `settings.require(...)`), so the cause was obvious at a glance: the
deployed model runs in a brand-new container, with no local `.env` file present.

**Root cause**: `ServedEntityInput` had no `environment_vars` configured, so the
container's `os.environ` had none of the `VECTOR_SEARCH_INDEX`-style variables in it at
all.

**Fix**:

```python
def _serving_environment_vars() -> dict:
    return {
        "UC_CATALOG": settings.uc_catalog,
        ...
        "VECTOR_SEARCH_INDEX": settings.vector_search_index,
        ...
    }   # note: DATABRICKS_HOST/TOKEN are not passed — auth goes through the automatic authorization from `resources`
```

**Verification**: after updating the endpoint config, called it again — the previous
error was gone, and it reached the business logic normally.

**Corresponding file**: `src/setup/deploy_model.py`

---

### Case 10: The SDK's `serving_endpoints.query()` can't parse the custom output

**Symptom**: calling the deployed Endpoint with `databricks-sdk`'s
`client.serving_endpoints.query(...).as_dict()` returned only
`{"served-model-name": "salesduo_agent-3"}`, with no actual answer content visible
anywhere.

**Investigation**: suspected the SDK's typed wrapper wasn't parsing the response
correctly, switched to the lowest-level REST call instead
(`client.api_client.do("POST", f"/serving-endpoints/{name}/invocations", body=...)`),
hitting it directly — this time got back the complete
`{"object": "response", "output": [{"type": "message",
"content": [{"type": "output_text", "text": "..."}]}]}`.

**Root cause**: `QueryEndpointResponse` (the return type of the SDK's `query()` method)
is designed for **generic** chat/completions/embeddings-type serving endpoints — none
of its fields (`choices`, `predictions`, `outputs`, ...) match our custom
ResponsesAgent's `output` field structure, so this content simply gets dropped during
`.as_dict()` serialization.

**Fix**: switched both the diagnostic script and `app/app.py` to call the raw REST
endpoint directly and parse the `output` field themselves:

```diff
- response = client.serving_endpoints.query(name=..., input=[...])
- return _extract_text(response.as_dict())
+ raw = client.api_client.do("POST", f"/serving-endpoints/{name}/invocations",
+                             body={"input": [...]})
+ return _extract_text(raw)
```

**Verification**: after the fix, asked the same question again and got the correct
text answer back.

**Corresponding file**: `app/app.py`

---

### Case 11 (resolved, see the 2026-07-27 addendum): Genie table queries failed with a permission error under the Serving Endpoint's identity

**Symptom**: running `build_graph().invoke(...)` locally with a personal token worked
completely fine, but the same question, called through the deployed Serving Endpoint,
made `structured_agent` fail with:

```
PERMISSION_DENIED: An error occurred accessing the schema. Failed to fetch tables for
the agent. Please resolve these errors to continue: No access to
'adventureworks_dataagent.sales.store'. To use this Genie agent, you must have SELECT
on each data asset, and at least USE CATALOG and USE SCHEMA on the containing catalog
and schema. ...(followed by a listing of all 20 tables attached to the Genie space)
```

**Investigation**:
1. Added `custom_outputs={"trace": ...}` to this deployment's `agent.py`, so the JSON
   the Serving Endpoint returns also carries the full white-box trace (without this,
   the production environment is a complete black box, impossible to diagnose).
2. Confirmed from the trace: `router` judged correctly, `unstructured_agent` worked
   normally, `structured_agent` failed with this exact same `PERMISSION_DENIED` every
   time, all the way until `loop_count` hit its cap.
3. Guessed the Serving Endpoint used a different identity than the local token, and ran
   `GRANT USE CATALOG/SCHEMA + SELECT ON SCHEMA sales/person TO `account users`` —
   **didn't resolve it**.
4. Checked the Genie Space's own ACL
   (`client.permissions.get(request_object_type="genie", ...)`), which only listed
   myself and the `admins` group, so also added `GRANT CAN_RUN` for `account users` —
   **still didn't resolve it**.
5. Checked SQL Warehouse permissions — the `users` group already had `CAN_USE`, ruling
   this out.
6. Queried `client.service_principals.list()`, which returned an empty list — couldn't
   pin down exactly who the identity the Serving Endpoint actually used was.
7. Searched Databricks' official documentation, confirming the general direction that
   "Model Serving's system identity needs to be separately granted CAN RUN on Genie,
   plus UC permissions on the underlying tables" was correct, but couldn't pin down
   which specific enumerable, grantable object to actually grant it to.

**Root cause**: **unidentified at the time**. Confirmed it wasn't as simple as
"forgetting to GRANT" (both categories of permission had been tried) — most likely
Databricks Model Serving's "automatic authorization" mechanism for a resource like
Genie uses some internal identity that can't be found via the existing APIs
(`permissions.get`/`service_principals.list`), or granting that identity requires going
through a different, not-yet-tried entry point (e.g. the workspace admin console's GUI,
or a more involved OAuth passthrough setup like `on_behalf_of_user=True`).

**Impact at the time**: the deployed Databricks App answered pure-policy questions (not
needing to query specific customer data) correctly; questions involving querying
specific AdventureWorksLT data returned a degraded "information may be incomplete"
answer once `loop_count` was exhausted.

**Corresponding file**: this problem itself had no corresponding code change (not yet
fixed at the time) — the related diagnostic code is in `src/agent.py` (the trace
exposed via `custom_outputs`).

**2026-07-27 addendum (problem resolved)**: at the time, investigation was stuck
between two dead ends — "grant the `account users` group" and "find the Serving
Endpoint's own system identity" — neither worked. The real answer was a **third path**:
**a Databricks App automatically generates its own separate service principal on
deploy (`client.apps.get(app_name).service_principal_client_id`), and this App's own
service principal is the identity actually used when executing the Genie query** — it
isn't a separate identity of the Serving Endpoint's own, nor an account-level group like
`account users`. The user found this lead themselves; verified by first running
`chat.py` under a personal local identity to confirm the structured query itself had no
problem (it always worked — not a new finding this time), then testing directly in the
deployed App's chat box, reproducing the permission error — after granting this App's
service principal access, the same question worked in the App and got structured data
back correctly.

Fix script: `ops/grant_app_permissions.py` (new), granting the App's service principal:
`USE CATALOG` on `adventureworks_dataagent`, `USE SCHEMA` + `SELECT` on the `sales`/
`person` schemas, `USE SCHEMA` + `EXECUTE` on the `salesduo_agent_tools` schema (the
latter is easy to miss — the SQL Genie generates calls the two business-rule functions,
which need `EXECUTE` permission, not `SELECT`; the two permission types are managed
separately, and missing either one fails at a different stage). Full details in the
addendum section of `docs/VERIFICATION_2026-07-27.md`.

One inference worth noting: **every time the App is deleted and recreated, it gets a
new service principal**, and this grant needs to be re-run then — it isn't a one-time,
permanently-in-effect thing. If this permission error resurfaces after rebuilding the
App later, check first whether `ops/grant_app_permissions.py` was simply forgotten for
the new service principal.

---

### Case 12: `databricks bundle deploy` doesn't automatically start the App

**Symptom**: the `databricks bundle deploy` command returned successfully with
"Deployment complete!", but `databricks bundle summary` showed the App's URL as
"(not deployed)"; querying via the SDK with `client.apps.get("salesduo-agent")` showed
`compute_status.state` as `STOPPED`.

**Investigation**: checked `databricks bundle run --help`, and found this command's
description was "Run the job, pipeline or app identified by KEY" — realized Asset
Bundle handles resource types like apps/jobs with a two-stage "deploy the definition +
run to start it" pattern, and `bundle deploy` only handles the first half.

**Root cause**: an incomplete understanding of the deployment flow for the Asset
Bundle's `apps` resource type — assumed `bundle deploy` was the finish line.

**Fix**: ran the extra command

```bash
databricks bundle run salesduo_agent
```

**Verification**: after running it, the terminal printed "App started successfully"
plus a genuinely reachable URL; then confirmed via `client.apps.get()` that
`app_status.state == RUNNING` and `compute_status.state == ACTIVE`.

**Corresponding file**: no code change — this was purely about the deployment
operational process itself.

---

## Part 4 — Known limitations that remain

The following are places this development **consciously** skipped, simplified, or
didn't strictly verify — listed here honestly:

1. ~~**The Genie permission problem under the Serving Endpoint is unresolved** (see Part
   3 Case 11 for details). This is currently the biggest functional gap: in production,
   only the "unstructured" half is actually fully working.~~
   **Resolved on 2026-07-27**, see the addendum to Part 3 Case 11 — the root cause was
   that the App's own service principal hadn't been granted SELECT on the underlying
   tables / EXECUTE on the business-rule functions, not the Serving Endpoint having some
   separate, untraceable system identity. Fix script: `ops/grant_app_permissions.py`.

2. **Genie's NL2SQL generation is inherently non-deterministic**. The three specific SQL
   mistakes fixed in Case 3 were each "specifically encountered this time, specifically
   fixed" — not an exhaustive enumeration of every mistake Genie could possibly make.
   Rephrasing a question, or a different model call, could in theory still produce a
   new failure mode never seen before. This isn't a bug list that can ever be fully
   "finished" — it's inherent uncertainty in this architectural approach (letting the
   LLM write SQL itself).

3. **`top_k=8` and the chunk-filtering rule are empirical values tuned specifically for
   these two documents and this handful of test questions**, not an optimum chosen by
   systematically sweeping a parameter grid (e.g. trying top_k at 5/8/10/15 and
   comparing overall effect). With a different batch of documents/questions, this value
   isn't necessarily still optimal.

4. **The evaluation set is only 10 questions**, a small coverage — the same question's
   verdict fluctuated between the two runs (e.g. `multi_hop_2`, `multi_hop_6` scored
   differently across the two runs), which by itself shows a sample size of 10 isn't
   enough to draw a stable conclusion about "what this system's overall accuracy is" —
   only a qualitative statement that "the core chain runs end-to-end, with known issues
   documented" is warranted.

5. **`MAX_ROUTER_LOOPS=5`, `EMBEDDING_MODEL_ENDPOINT=databricks-gte-large-en`,
   `LLM_SERVING_ENDPOINT=databricks-meta-llama-3-3-70b-instruct` were all chosen under
   CLAUDE.md v1's allowance to "use industry-common defaults for now"**, with no A/B
   testing or tuning done specifically for this scenario.

6. **The LLM judge's scoring itself may also be unstable** (the "the model occasionally
   generates malformed output under forced structured output" issue noted in Case 5
   uses the same `get_llm()` for grading too — in theory the grading itself could be
   subject to the same model-quality fluctuation, it's just that the eval script
   currently doesn't retry a failed grading call).

7. **No content-safety/PII-redaction/prompt-injection protection has been implemented
   at all** — this is explicitly scoped as "not doing this now, add later if needed" in
   CLAUDE.md v1, not an oversight this round — but it's noted honestly here as the
   current state, carried forward into this retrospective.

8. **`chat.py`'s multi-turn conversation history (the `messages` list) has no
   length/token limit** — in theory, if a single session asks many turns of questions,
   `messages` would grow unbounded until some call fails because the prompt is too
   long — this scenario has not been tested.

9. **`app.py` has no additional input validation beyond Databricks' built-in SSO
   login** (e.g. no limit on single-message input length, no rate limiting).

10. **This project never had git initialized throughout development**, so strictly
    speaking the "development timeline" was reconstructed from file mtimes and session
    records, not commit history — if this project is picked up for further development
    later, it's recommended to first add an initial commit, then follow a normal git
    workflow for subsequent changes, rather than continuing to rely on mtime for
    troubleshooting.

---

## Part 5 — Supplementary notes migrated from CLAUDE.md

`CLAUDE.md` (the original provisioning instruction document, v2) was deleted on
2026-07-27 — its task (guiding this project's from-scratch provisioning) was complete.
`CLAUDE_v1.md` is kept as a historical version for comparison, but v1/v2's content isn't
identical — two pieces of content unique to v2 hadn't been captured in this document at
the time, and were migrated here before deletion to avoid losing them:

### Known-risk checklist (originally CLAUDE.md section 7, each item annotated with where it's covered in this document)

1. Executing a UC Function as an agent tool requires serverless generic compute (not a
   SQL Warehouse) — not having this enabled causes a permission error — **this project
   actually uses a SQL Function, which executes via the SQL Warehouse, so this risk was
   never triggered**; if the rule computation is ever reimplemented as a Python UC
   Function instead, this needs to be separately confirmed as enabled.
2. Genie's multi-turn conversation must reuse `conversation_id` — see
   [Part 2 method 10](#10-genie-conversation_id-passed-across-nodes-to-implement-multi-turn-memory).
3. There's a risk of the router misjudging and causing an infinite loop —
   `MAX_ROUTER_LOOPS` must actually be implemented with its trigger path tested — covered
   by `tests/test_router_loop_limit.py` (runnable offline, constructs a state where
   `loop_count` has already hit the cap, verifies router forces `finalize` with no
   error).
4. Vector Search's Delta Sync Index depends on a source Delta table — you can't index a
   raw docx file directly — see
   [Part 2 method 6](#6-vector-search-delta-sync-index).
5. A Genie Space's `serialized_space` configuration is opaque, with no field-level API
   documentation — see
   [Part 3 Case 1](#case-1-the-genie-space-uis-attach-function-feature-turned-out-to-be-unnecessary).
6. A UC SQL Function's `CREATE OR REPLACE FUNCTION` doesn't validate whether `RETURN`'s
   type matches the `RETURNS` declaration — see
   [Part 3 Case 2](#case-2-the-uc-function-was-never-actually-created-successfully-all-the-way-back-to-step-2).
7. Genie's NL2SQL generation is inherently non-deterministic — see
   [Part 3 Case 3](#case-3-three-different-categories-of-errors-in-genie-generated-sql-the-same-class-of-problem-recurring)
   and [Part 4](#part-4--known-limitations-that-remain) item 2.
8. **Running the `databricks` CLI commands locally (`bundle validate`/`bundle deploy`),
   the CLI does not read the project's `.env` file** — you need to separately
   `export DATABRICKS_HOST`/`DATABRICKS_TOKEN` in the current shell, or configure a
   `~/.databrickscfg` profile. (This item was never recorded by any specific case
   before — it's the only genuinely "new" content added during this migration; the
   pitfall happened while running `databricks bundle`-related commands locally, and at
   the time the cause was obvious enough and the fix a one-line `export`, so it wasn't
   written up as its own case.)
9. mlflow, when registering a model from a local environment, defaults to writing to
   local SQLite, not the real Databricks workspace — see
   [Part 3 Case 6](#case-6-mlflow-registered-the-model-to-local-sqlite-not-the-real-databricks-workspace).
10. Calling Genie to query underlying UC tables at Model Serving Endpoint runtime may
    hit a permission error, even when a local personal token works completely fine —
    see [Part 3 Case 11 (resolved)](#case-11-resolved-see-the-2026-07-27-addendum-genie-table-queries-failed-with-a-permission-error-under-the-serving-endpoints-identity).
