# Deployment Guide: From Zero to a Live App

> This document assumes the `adventureworks_dataagent` catalog already exists in Unity
> Catalog (the project reuses an existing catalog rather than creating a new one). It's
> written for the scenario where the repo's code is already written and you need to
> provision resources on a new workspace, or rebuild resources on the current one — steps
> are laid out in actual dependency order: which file to run, why this step, what result
> to expect, and roughly how long it takes. Every step's command can be copied straight
> from the "How to run" column. A condensed troubleshooting log is appended at the end;
> the full version is in `docs/DEVELOPMENT_JOURNAL.md` (the complete development-process
> record) and `docs/VERIFICATION_2026-07-27.md` (the re-verification record after the
> auth switch, with a more detailed timing breakdown).

---

## 0. Prerequisites

| Item | Notes |
|---|---|
| Azure CLI | Must be installed locally with `az login` already run — this project uses native Azure auth (not a PAT); `databricks-sdk` authenticates via this login session. |
| Python environment | `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` (installs the full local-development dependency set, not the one deployed to the Serving Endpoint). |
| `.env` | Copy `.env.example` to `.env` and fill in `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/`DATABRICKS_WORKSPACE_NAME` (Azure resource coordinates the SDK uses to resolve the workspace host) plus `SQL_WAREHOUSE_ID` (needed by every step below). The remaining variables (`GENIE_SPACE_ID`, etc.) get produced by their corresponding step — leave them blank for now. |
| Databricks CLI | `brew install databricks-cli` or the official install script; needed for deploying the App in Step 7. |

Estimated time: 10-20 minutes (mostly environment setup, varies by person).

---

## Step 1: Verify the connection

| | |
|---|---|
| **File** | [ops/verify_connection.py](../ops/verify_connection.py) |
| **Purpose** | Confirm Azure CLI auth works, the workspace is reachable, and `UC_CATALOG` actually exists and is accessible. Every later step assumes this one passed first. |
| **How to run** | `python -m ops.verify_connection` |
| **Expected result** | Prints every schema name under `UC_CATALOG` (currently 7: `humanresources`/`information_schema`/`person`/`production`/`purchasing`/`sales`/`salesduo_agent_tools`). |
| **Estimated time** | A few seconds |

---

## Step 2: Provision the structured-data side

### 2a. Create the UC Functions

| | |
|---|---|
| **Files** | [ops/structured/setup_uc_functions.py](../ops/structured/setup_uc_functions.py) + [ops/structured/sql/](../ops/structured/sql/) (two `.sql` templates) |
| **Purpose** | Creates the two UC SQL Functions `calculate_credit_terms` (credit-term computation) and `check_large_transaction_compliance` (large-transaction compliance check); the business rules come from the two policy documents under `documents_generated/`. |
| **How to run** | `python -m ops.structured.setup_uc_functions` |
| **Expected result** | Prints "Created/replaced function: calculate_credit_terms"/"check_large_transaction_compliance". **This print line alone isn't sufficient** — follow up right away with a SQL query against `information_schema.routines` to confirm the functions actually exist, then actually call each once to confirm the return structure is correct (there's history of "CREATE didn't error but the function actually can't be called," see problem 1 at the end of this document). |
| **Estimated time** | A few seconds to tens of seconds (depends on whether the SQL Warehouse is cold-starting) |

### 2b. Create the Genie Space (the one step that must be done by hand)

| | |
|---|---|
| **Purpose** | A Genie Space itself **currently has no public API for creation** — you must first manually create an empty Genie Space in the Databricks UI (Genie → New), with any placeholder tables attached as the data source (step 2c below will overwrite this). This is a platform limitation, not an implementation choice that could be worked around. |
| **How to run** | UI action: in the Databricks workspace left sidebar, Genie → New Space, give it a name. |
| **Expected result** | You get a Genie Space ID (visible in the URL) — fill it into `.env`'s `GENIE_SPACE_ID`. |
| **Estimated time** | A few minutes |

### 2c. Configure Genie (attach tables + write instructions)

| | |
|---|---|
| **File** | [ops/structured/setup_genie.py](../ops/structured/setup_genie.py) (the table list is listed explicitly in the `GENIE_TABLES` variable; the instructions text is [prompts/genie_instructions.prompt](../prompts/genie_instructions.prompt)) |
| **Purpose** | Attaches the tables listed in `GENIE_TABLES` (person.person + 19 sales tables) to the Genie Space created in 2b, and writes the instructions text (table-join-path hints, the two UC Functions' fully-qualified names and return fields, date-function usage hints) — these hints were derived by working backward from actual Genie SQL-generation errors hit during testing (see problem 3 at the end of this document). |
| **How to run** | `python -m ops.structured.setup_genie` |
| **Expected result** | Prints how many tables were added this run + the current total table count (building from scratch will show "20 tables added this run, 20 tables total"). **This step overwrites the Genie Space's instructions and sql_functions fields every time it runs** (a platform limitation, see problem 2 at the end of this document) — no extra manual UI re-attachment is needed afterward. |
| **Estimated time** | A few seconds |

---

## Step 3: Provision the unstructured-data side

### 3a. Create the Vector Search Endpoint

| | |
|---|---|
| **File** | [ops/rag/setup_vs_endpoint.py](../ops/rag/setup_vs_endpoint.py) |
| **Purpose** | Creates the Vector Search endpoint (long-lived shared infrastructure — one endpoint can host multiple indexes, which is why it's split from the index-creation step below). |
| **How to run** | `python -m ops.rag.setup_vs_endpoint` |
| **Expected result** | Prints "Created Vector Search endpoint". This step only **fires the creation request, it doesn't wait for completion** — you need to check the status yourself (`client.vector_search_endpoints.get_endpoint(name=...).endpoint_status.state`) and confirm it reaches `ONLINE` before continuing. |
| **Estimated time** | The **first** time a Vector Search endpoint is created in this workspace it may take ten to several tens of minutes (stuck in `PROVISIONING_ENDPOINT`, not slow index syncing); if one was created before and this is a rebuild, it may only take a few minutes (one real run took about 1-2 minutes). If unsure which case applies, mentally prepare for "might take a while." |

### 3b. Parse the documents + build the index

| | |
|---|---|
| **File** | [ops/rag/ingest_docs.py](../ops/rag/ingest_docs.py) (calls [ops/rag/chunk_docs.py](../ops/rag/chunk_docs.py) to do the chunking) |
| **Purpose** | Uploads the two docx files under `documents_generated/` to a UC Volume for archival, parses and chunks them into a Delta table, and creates a Delta Sync Index on the endpoint built in 3a. |
| **How to run** | `python -m ops.rag.ingest_docs` |
| **Prerequisite** | 3a's endpoint must already be `ONLINE`, otherwise index creation will fail. |
| **Expected result** | Prints, in order: UC Volume already exists/created, both documents uploaded, 18 chunks written, Vector Search index created. **This is also just firing the index-creation request** — you need to separately poll `client.vector_search_indexes.get_index(...).status.ready` until it becomes `True`, passing through stages like "pending endpoint provisioning → pending pipeline resources → syncing initial data → ready" along the way. |
| **Estimated time** | The script itself finishes in under a minute; the index taking `ready=True` after creation needs roughly another 10-15 minutes (async sync, the script doesn't wait for it). |
| **After the index finishes building**, it's strongly recommended to run `src/clients/retriever.py::retrieve()` against a few real business questions and manually check whether the top-k results actually contain relevant passages — `ready=True` alone isn't proof of success (see problem 4 at the end of this document). |

---

## Step 4-6: Local verification

### 4a. Offline unit tests

| | |
|---|---|
| **Files** | [tests/test_chunk_docs.py](../tests/test_chunk_docs.py), [tests/test_router_loop_limit.py](../tests/test_router_loop_limit.py), [tests/test_unstructured_agent_query.py](../tests/test_unstructured_agent_query.py), [tests/test_app_error_handling.py](../tests/test_app_error_handling.py) |
| **Purpose** | Verifies pure logic that doesn't depend on a real Databricks connection (chunking, the router loop-cap safety net, multi-hop retrieval query construction, App error handling) — these should be runnable and passing at any time after a code change. |
| **How to run** | `pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py tests/test_unstructured_agent_query.py tests/test_app_error_handling.py -v` |
| **Expected result** | 13 passed |
| **Estimated time** | A few seconds |

### 4b. End-to-end integration tests (needs a real connection)

| | |
|---|---|
| **File** | [tests/test_integration_cases.py](../tests/test_integration_cases.py) |
| **Purpose** | Connects to real Genie + Vector Search + LLM, covering three scenario types (structured-only, unstructured-only, multi-hop) plus the loop-cap safety net and cross-turn memory (`genie_conversation_id` reuse, pronoun resolution). Without real credentials configured, the cases in this file are skipped automatically, no error is raised. |
| **How to run** | `pytest tests/test_integration_cases.py -v` |
| **Expected result** | 5 passed |
| **Estimated time** | About 2-3 minutes (multiple Genie/LLM round trips) |

### 4c. (Optional) Interactive manual trial

| | |
|---|---|
| **File** | [chat.py](../chat.py) |
| **Purpose** | Chat with the agent from a local terminal, getting a hands-on feel for answer quality without waiting for a deployment to test. Type `/trace` to toggle displaying the white-box trace for each step (router's reasoning, SQL Genie generated, retrieved passages). |
| **How to run** | `python chat.py` |
| **Estimated time** | However long you want to spend testing |

### 4d. (Optional) Run the evaluation set

| | |
|---|---|
| **Files** | [tests/eval/run_eval.py](../tests/eval/run_eval.py) + [tests/eval/eval_set.json](../tests/eval/eval_set.json) |
| **Purpose** | Runs a batch of ground-truth questions and grades them with an LLM judge, saving results into `tests/eval/results/` (including the full trace per question, not just the final answer). |
| **How to run** | `python -m tests.eval.run_eval` |
| **Expected result** | Scores will fluctuate somewhat normally (Genie's NL2SQL generation is inherently non-deterministic, see problem 3 at the end of this document) — 100% isn't the goal; being able to qualitatively confirm "the core chain runs end-to-end" is enough. |
| **Estimated time** | Depends on question count; a few minutes for around 10 questions |

---

## Step 7: Deployment

### 7a. Register the model + create/update the Serving Endpoint

| | |
|---|---|
| **File** | [ops/deploy_model.py](../ops/deploy_model.py) |
| **Purpose** | Registers `src/agent.py` (the ResponsesAgent wrapper around the whole LangGraph graph) as a model version in Unity Catalog, declaring the resources it depends on at runtime (Genie Space, Vector Search Index, SQL Warehouse, the two UC Functions) — Databricks uses this to automatically grant the Serving Endpoint access to these resources; then creates/updates the Serving Endpoint to point at this new version. |
| **How to run** | `python -m ops.deploy_model` |
| **Expected result** | Prints "Registered model: ... version N" plus "Updated serving endpoint". The script waits internally until `update_config_and_wait`/`create_and_wait` returns before printing that it's done, so by the time you see that line, the Serving Endpoint is already on the new version and in `READY` state. |
| **Estimated time** | Packaging/uploading the model artifact takes a few seconds; the Serving Endpoint's rolling update to the new version needs roughly another 10-15 minutes (platform behavior, not compressible). |

### 7b. Deploy the App code

| | |
|---|---|
| **File** | [databricks.yml](../databricks.yml) (Asset Bundle config) |
| **Purpose** | Syncs the `app/` directory's code to the workspace and registers the App resource definition. |
| **Expected result** | `Deployment complete!` |
| **Estimated time** | A few seconds to tens of seconds |

**How to run** — must `export` this separately in the same shell you run the
`databricks bundle` command in; the CLI is a separate process and doesn't inherit
environment variables set inside a Python process (see problem 7 at the end of this
document):
```bash
export DATABRICKS_AZURE_RESOURCE_ID=$(python3 -c "
from src.config import settings
print(f'/subscriptions/{settings.azure_subscription_id}/resourceGroups/{settings.azure_resource_group_name}/providers/Microsoft.Databricks/workspaces/{settings.azure_databricks_workspace_name}')
")
databricks bundle deploy
```

### 7c. Start the App

| | |
|---|---|
| **Purpose** | `bundle deploy` only registers the resource definition — **it does not start the App**; this extra step is required to actually spin up compute and deploy the code onto it (see problem 8 at the end of this document). |
| **How to run** | `databricks bundle run salesduo_agent` (same shell, `DATABRICKS_AZURE_RESOURCE_ID` still in effect) |
| **Expected result** | A series of "App is starting..." messages followed by `App started successfully`, with an access URL. **Don't just trust this text** — it's worth also querying via the SDK, `client.apps.get(app_name)`, to confirm `app_status.state == "RUNNING"` and `compute_status.state == "ACTIVE"`. |
| **Estimated time** | About 3-4 minutes |

### 7d. Grant permissions to the App's service principal (an easy step to miss)

| | |
|---|---|
| **File** | [ops/grant_app_permissions.py](../ops/grant_app_permissions.py) |
| **Purpose** | A Databricks App automatically generates its own service principal on deploy — this is the identity actually used when `structured_agent` calls Genie to query the underlying tables. Without granting it access, structured queries will fail with `PERMISSION_DENIED` at this step (full investigation in problem 9 at the end of this document — this is the single biggest pitfall hit in this project). The grant scope is read directly from `ops/structured/setup_genie.py::GENIE_TABLES`/`REQUIRED_FUNCTIONS`, no need to manually maintain a second table list in sync. |
| **How to run** | `python -m ops.grant_app_permissions` |
| **Prerequisite** | Must run this only **after** the App in 7c has successfully started at least once — the service principal only exists once the App is created/started; running this too early fails with "no service_principal_client_id found." |
| **Expected result** | Prints the App's service principal info + runs about 26 `GRANT` statements in sequence (`USE CATALOG` + `USE SCHEMA` for each schema used + `SELECT` per table + `USE SCHEMA` for the business-function schema + `EXECUTE` per function). |
| **Estimated time** | A few seconds |
| **Reminder** | Every time the App is deleted and recreated it gets a **new** service principal — this grant needs to be re-run then; it doesn't permanently apply to "the App with this name." |

### 7e. Final acceptance check

- Confirm via SDK: `client.serving_endpoints.get(...).state.ready == "READY"`, and
  `client.apps.get(...)`'s `app_status.state == "RUNNING"` with
  `compute_status.state == "ACTIVE"`.
- Actually open the URL given in 7c, and ask a real multi-hop question in the chat box
  (e.g. "what are customer XX's annual purchase volume and credit limit cap"), confirming
  both structured and unstructured queries return normally — not just policy-only
  questions working.

Estimated time: a few minutes.

---

## Overall estimated time end to end

Excluding the optional 4c/4d, Step 1 through 7e takes roughly **50-90 minutes**, with the
bulk of it in three uncompressible async waits: 3a (first-time Vector Search endpoint
creation, ten to several tens of minutes), 3b (index sync, 10-15 minutes), and 7a
(Serving Endpoint rolling update, 10-15 minutes). The code's own execution time adds up
to under 10 minutes total.

---

## Appendix: problems hit and how they were resolved (condensed — full account in `docs/DEVELOPMENT_JOURNAL.md`)

1. **A UC SQL Function returning multiple fields — `CREATE` doesn't error, but calling it
   fails with `SCALAR_SUBQUERY_RETURN_MORE_THAN_ONE_OUTPUT_COLUMN`** — a `RETURN`
   statement returning a multi-column `SELECT` must be wrapped in `STRUCT(...)`;
   `CREATE OR REPLACE FUNCTION` itself doesn't validate this, so it can only be caught by
   actually calling the function once after creation.

2. **A Genie Space's `serialized_space` is an opaque format, and the
   `instructions.sql_functions` field can't be written via the API** — regardless of
   content, it fails with "Certified answer 'xxx' does not exist." Resolution: write the
   UC Function's fully-qualified name and return fields directly into the
   `instructions.text_instructions` text, so Genie calls it correctly when generating
   SQL, without needing the broken "attach as tool" mechanism; every run of
   `setup_genie.py` must actively strip the `sql_functions` field before submitting,
   otherwise the whole update fails.

3. **Three typical categories of Genie SQL-generation errors**: skipping an intermediate
   table and joining incorrectly, mixing up `DATEDIFF`'s (unquoted) and `DATE_TRUNC`'s
   (quoted) quoting conventions, and writing a scalar function into a `FROM` clause as if
   it were a table function. Resolution: all resolved by adding specific corrective
   guidance into the instructions text (see `prompts/genie_instructions.prompt`) — there
   is no way to exhaustively fix this class of problem once and for all; it's inherent
   non-determinism in Genie's own NL2SQL generation, and rephrasing a question could
   surface a new failure mode.

4. **Vector retrieval missing target passages** — the 2-column key-value metadata table
   at the top of a document (Policy ID/Effective Date, etc.) gets an unusually high
   similarity score in vector search, crowding the genuinely relevant longer passages out
   of the top-k. Resolution: skip indexing this kind of 2-column table entirely during
   chunking; bumped `top_k` from 5 to 8 (testing showed 5 misses target passages).

5. **The router's LLM occasionally generates invalid-format JSON when the `reason` field
   is longer** — even forced structured output (`with_structured_output`) fails
   occasionally. Resolution: added 3 retries, safely degrading to `finalize` with a note
   that information may be incomplete after repeated failures, rather than letting the
   whole request crash.

6. **Running the deploy script locally, mlflow defaults to registering to a local
   SQLite DB instead of the real Databricks workspace** — the "registered successfully"
   log locally is genuinely printed, it's just registered in the wrong place, with no
   error to flag it. Resolution: explicitly set `mlflow.set_tracking_uri("databricks")`
   + `mlflow.set_registry_uri("databricks-uc")`.

7. **The `databricks` CLI command doesn't read the project's `.env` file, nor does it
   inherit environment variables set inside a Python process** — you need to separately
   `export DATABRICKS_AZURE_RESOURCE_ID=...` (or, in the earlier PAT era,
   `DATABRICKS_HOST`/`TOKEN`) in the same shell you run the `databricks bundle` command
   in.

8. **`databricks bundle deploy` does not automatically start the App** — it only uploads
   code and registers the resource definition; an extra
   `databricks bundle run <app_resource_key>` is required to actually start compute. The
   acceptance criterion is checking the real status fields via `client.apps.get(...)`,
   not just trusting what the CLI prints.

9. **(The single biggest pitfall in this project) The Serving Endpoint/App calling Genie
   to query underlying tables fails with `PERMISSION_DENIED`, even though the local
   personal identity works fine** — took a long time to investigate, at one point
   suspected as some untraceable "Serving Endpoint system identity," tried granting the
   `account users` group and adding `CAN_RUN` on the Genie Space, neither resolved it.
   Actual cause: **a Databricks App automatically generates its own separate service
   principal on deploy
   (`client.apps.get(app_name).service_principal_client_id`), and this is the identity
   actually used to execute the Genie query** — granting it access (`SELECT` on the
   underlying tables + `EXECUTE` on the business-rule functions) via
   `ops/grant_app_permissions.py` resolved it. This identity is specific to the App, and
   the grant script needs to be re-run every time the App is deleted and recreated.

10. **After switching auth from a PAT to native Azure CLI auth, the
    `databricks-ai-search` package (`VectorSearchClient`) became completely
    incompatible** — that package's auth logic hardcodes support only for a static
    PAT/service-principal token; the Azure CLI hands over a dynamically-refreshed token,
    which the package can't use and fails on outright. Resolution: migrated all the
    Vector-Search-related code (`retriever.py`/`setup_vs_endpoint.py`/`ingest_docs.py`)
    to `databricks-sdk`'s own `client.vector_search_indexes`/
    `client.vector_search_endpoints` native API, using the same auth as everything else,
    no separate handling needed.

11. **mlflow's own credential-resolution logic also constructs a bare, argument-less
    `WorkspaceClient()`** — not just the third-party `databricks-ai-search` library has
    this problem; `mlflow.utils.databricks_utils.get_databricks_host_creds()` does the
    same internally. Resolution: it's not enough to handle auth parameters only inside
    our own `db_client.py` wrapper — `AZURE_SUBSCRIPTION_ID`/`RESOURCE_GROUP_NAME`/
    `DATABRICKS_WORKSPACE_NAME` are also assembled into `DATABRICKS_AZURE_RESOURCE_ID`,
    the environment variable databricks-sdk officially recognizes, and written back into
    `os.environ`, so any bare `WorkspaceClient()` constructed anywhere in the code path
    picks it up automatically, without having to adapt each library individually.
