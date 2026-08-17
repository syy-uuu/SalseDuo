# Repository Structure (as actually laid out after the 2026-07-27 refactor)

> The directory structure described in this document has already landed — files have
> been moved to match it, import paths have been updated accordingly, and
> `pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py` (the offline-runnable
> cases) all pass; every runtime/provisioning module has also been individually
> verified at the `import` level. **This refactor did not include any verification that
> requires a real Databricks connection** (no behavior changed — this was purely moving
> files around plus fixing import paths; see the "Verification scope for this refactor"
> section at the end of this document for details).
>
> The detailed reasoning behind this structural decision (which concerns were valid,
> which weren't, and why things are split this way) lives in the conversation record; the
> specific list of code-level issues (legacy issues this refactor *didn't* fix along the
> way) is in `CODE_REVIEW_FINDINGS.md`.

```
SalesDuo/
├── chat.py                         # Local interactive CLI for manually debugging the agent; not packaged into the deployment artifact
├── requirements.txt                 # -r requirements-runtime.txt + ops/-specific dependencies (databricks-connect, azure-core/azure-storage-file-datalake, python-docx, pytest)
├── requirements-runtime.txt          # Lists only the packages src/ runtime code actually imports; deploy_model.py uses this file
├── databricks.yml                   # Asset Bundle config (app resource definition)
├── .env / .env.example
├── .gitignore
│
├── docs/                            # Non-auto-loaded documentation, collected in one place
│   ├── CLAUDE_v1.md                 # Historical version, kept for comparison
│   ├── DEVELOPMENT_JOURNAL.md       # Development retrospective/pitfalls log (includes Part 5: content migrated from the now-deleted CLAUDE.md)
│   ├── CODE_REVIEW_FINDINGS.md      # List of issues found in code review (open/deferred)
│   └── REPOSITORY_STRUCTURE.md      # This file
│
├── documents_generated/             # Read-only source documents (unstructured data source, two docx files)
│
├── src/                             # 【Runtime】the boundary for mlflow log_model's code_paths — only code that gets deployed lives here
│   ├── config.py                    # The project's single environment-variable reading entry point
│   ├── db_client.py                 # The single entry point for Databricks SDK auth (get_workspace_client())
│   ├── agent.py                     # MLflow ResponsesAgent wrapper: predict / predict_stream
│   │
│   ├── graph/                       # LangGraph orchestration: node implementations + the orchestration scaffolding, at the same layer
│   │   ├── state.py                 # AgentState definition (includes accumulating trace fields)
│   │   ├── build_graph.py           # Assembles the StateGraph: the router's looping edge + the four nodes
│   │   ├── router.py                # The router node: forced structured output decides next_step
│   │   ├── structured_agent.py      # Node: calls Genie (structured data + UC Function business rules)
│   │   ├── unstructured_agent.py    # Node: calls Vector Search (document fragment retrieval)
│   │   └── finalize.py              # Node: synthesizes intermediate results into a final answer; the only node connected to END
│   │
│   └── clients/                     # Low-level external-service clients called by the nodes (infrastructure layer, no business logic)
│       ├── llm.py                   # The LLM client used for orchestration (ChatDatabricks)
│       ├── genie_client.py          # Genie conversation calls (manual polling, failure reasons aren't swallowed)
│       └── retriever.py             # A lightweight Vector Search top-k retrieval wrapper
│
├── ops/                              # 【Non-runtime】one-off provisioning/deployment scripts, triggered manually/by CI, not part of serving
│   ├── rag/                          # Unstructured-data pipeline
│   │   ├── chunk_docs.py             # docx parsing + chunking (pure local logic, no Databricks connection)
│   │   ├── ingest_docs.py            # Uploads raw docs → writes a Delta table → builds the Vector Search index
│   │   └── setup_vs_endpoint.py      # Vector Search endpoint provisioning (split out of ingest_docs.py)
│   │
│   ├── structured/                    # Structured-data / Genie pipeline
│   │   ├── setup_uc_functions.py      # Creates the UC SQL Functions (credit-term calculation, large-transaction compliance)
│   │   ├── sql/
│   │   │   ├── calculate_credit_terms.sql
│   │   │   └── check_large_transaction_compliance.sql
│   │   └── setup_genie.py             # Configures the Genie Space's data-source tables + Instructions text
│   │
│   ├── sql_utils.py                   # Imported by both rag/ and structured/: submits SQL to the Warehouse and polls for the result; lives in the shared parent of both
│   ├── verify_connection.py           # Unrelated to either pipeline — a pure connectivity check
│   └── deploy_model.py                # Deploys the whole agent, spanning both pipelines — log_model + creates the Serving Endpoint
│
├── app/                              # The Databricks Apps frontend, its own lightweight dependencies, doesn't share the main project's requirements
│   ├── app.py                        # The Streamlit chat box, calls the already-deployed Serving Endpoint
│   ├── app.yaml
│   └── requirements.txt
│
└── tests/
    ├── test_chunk_docs.py            # Pure local test: docx chunking logic
    ├── test_router_loop_limit.py     # Verifies the MAX_ROUTER_LOOPS safety net (runnable offline)
    ├── test_integration_cases.py     # Three categories of end-to-end cases (needs real workspace credentials, skipped otherwise)
    └── eval/
        ├── eval_set.json             # The evaluation question set with ground truth
        ├── run_eval.py                # Runs the batch automatically + LLM-judge scoring
        └── results/                  # Results from past runs (with full traces)
```

## Changes relative to the old structure

| Old path | New path | Why |
|---|---|---|
| `src/tools/genie_client.py`, `src/tools/retriever.py` | `src/clients/` | A more accurate name: these hold "low-level external-service clients," the same kind of thing as `db_client.py` |
| `src/graph/llm.py` | `src/clients/llm.py` | `get_llm()` is pure infrastructure, not graph business logic — `router.py`/`finalize.py` just happen to use it |
| `src/setup/chunk_docs.py`, `src/setup/ingest_docs.py` | `ops/rag/` | Moved entirely out of the "runtime code directory," and grouped together as the "RAG pipeline" so it's visible at a glance |
| `src/setup/setup_uc_functions.py`, `src/setup/sql/`, `src/setup/setup_genie.py` | `ops/structured/` | Same as above, grouped together as the "structured/Genie pipeline," symmetric with `ops/rag/` |
| The part of `src/setup/ingest_docs.py` that created the VS endpoint | `ops/rag/setup_vs_endpoint.py` (new file) | The endpoint is shared infrastructure, while the index is an asset bound to a specific data source — splitting them makes responsibilities clearer |
| `src/setup/sql_utils.py`, `src/setup/verify_connection.py`, `src/setup/deploy_model.py` | `ops/` top level | Shared across both pipelines, or belonging to neither — kept at the parent level |
| Root-level `CLAUDE_v1.md`, `DEVELOPMENT_JOURNAL.md`, `CODE_REVIEW_FINDINGS.md` | `docs/` | Non-auto-loaded documentation collected in one place |
| Root-level `CLAUDE.md` | **Deleted** (not moved) | The provisioning task is complete; the content unique to v2 was merged into `docs/DEVELOPMENT_JOURNAL.md` Part 5 and then deleted; `CLAUDE.md` itself can't be moved into `docs/` — Claude Code depends on it being at the project root to auto-load it as project instructions |
| (every other file) | Unchanged | `chat.py`, `app/`, `documents_generated/`, `tests/`, the requirements files all stay at/get added at the project root |

The core constraint is still the same one: **`src/` = the runtime boundary,
`deploy_model.py`'s `code_paths` only points at `src/`** — under this structure, both
`ops/` and `docs/` are siblings of `src/`, naturally excluded from the deployment
artifact without any extra exclusion logic needed.

## Quick lookup: which file to change for a given need

| Need | File |
|---|---|
| Add/change an environment variable | `src/config.py` + `.env.example` |
| Change routing decision logic | `src/graph/router.py` |
| Change how Genie is called (retries, conversation reuse) | `src/clients/genie_client.py`, `src/graph/structured_agent.py` |
| Change retrieval top_k / embedding language handling | `src/clients/retriever.py`, `src/graph/unstructured_agent.py` |
| Change business-rule computation logic | `ops/structured/sql/*.sql` + rerun `python -m ops.structured.setup_uc_functions` |
| Change document-chunking strategy | `ops/rag/chunk_docs.py` (rerun `python -m ops.rag.ingest_docs` to rebuild the index after changing) |
| Change deployment environment variables/resource dependencies | `ops/deploy_model.py` |
| Change runtime dependencies | `requirements-runtime.txt` (change provisioning/test dependencies in `requirements.txt` instead) |
| Add a new test case | `tests/test_integration_cases.py` or `tests/eval/eval_set.json` |

## Verification scope for this refactor

Per the user's request, this refactor is structure-only — no verification requiring a
real Databricks connection was done. What was and wasn't actually verified:

- **Done**: all 20 runtime + provisioning modules individually verified via
  `importlib.import_module()` to confirm no import-path was missed (`src.*` × 3,
  `src.graph.*` × 6, `src.clients.*` × 3, `ops.*` × 3, `ops.rag.*` × 3,
  `ops.structured.*` × 2); the offline tests
  `pytest tests/test_chunk_docs.py tests/test_router_loop_limit.py` (6 cases) all pass;
  `pytest tests/ --collect-only` confirms `test_integration_cases.py` and
  `tests/eval/run_eval.py` collect/import normally, without failing on an import error
  when there are no real credentials (they're designed to skip, or to be triggered
  manually, in that case).
- **Not done, needs manual verification later**: any path requiring an actual call to
  Genie/Vector Search/SQL Warehouse/Model Serving — meaning every script's `main()`
  under `ops/` has never actually been run once; path calculations like `_PROJECT_ROOT`
  were derived by manually checking directory levels line by line, not by actually
  running the scripts and confirming they work. Recommend that next time there's a real
  workspace connection, at least run every script under `ops/` once via
  `python -m ops.xxx.yyy` (especially `deploy_model.py`, whose project-root-path
  calculation changed the most — from 3 levels of `.parent` down to 2).
- **`ops/rag/setup_vs_endpoint.py` + `ops/rag/ingest_docs.py`**: this is the one place
  in this refactor that added a bit of new logic (not purely moving files) — the
  original `create_vector_search_index()` was split into
  `setup_vs_endpoint.py::ensure_endpoint_exists()` + `ingest_docs.py::create_delta_sync_index()`,
  each keeping only the check/creation logic that existed before the split, with no
  change to the actual condition logic — but since it's a new file boundary, it's worth
  paying extra attention to when re-running against a real environment (especially the
  run order: `setup_vs_endpoint.py` must run before `ingest_docs.py` — this
  prerequisite is new, introduced by the split).
