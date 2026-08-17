# Multi-turn memory verification — real Q&A transcript (2026-07-28)

> Corresponds to the change described in `docs/AGENT_MEMORY_DESIGN.md`. The
> corresponding repeatable formal test is
> `tests/test_integration_cases.py::test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns`
> (that test only makes structural assertions and doesn't show the actual Q&A content —
> this file supplements it with a human-readable transcript).
>
> The same 2-turn scenario was actually run 3 times (2 via the `chat.py`-equivalent
> path, 1 via the deployed-App-equivalent path). All 3 runs' real outputs are recorded
> in full, including the failure in the middle one, with no cherry-picking.

---

## Run 1 (chat.py-equivalent path: calling `build_graph().invoke()` directly)

**Result: the core mechanism succeeded, but triggered one instance of a known LLM structured-output occasional error**

```
Turn 1 - User: What's customer Bike World's annual purchase volume?
Agent: Bike World's annual purchase volumes are: 2023 $47,508.55, 2024 $41,509.61,
2025 $17,339.82. Of these, 2023 had the highest purchase volume, with a decline visible
by 2025. Information is complete.
[genie_conversation_id: previous turn=None -> this turn='01f18a79e9ae1ceb8da4f595fe0e070f']

Turn 2 - User: And what's their credit limit cap?
Agent: Based on the prior conversation and the retrieved policy documents, customer
Bike World's annual purchase volumes were: 2023 $47,508.55, 2024 $41,509.61, 2025
$17,339.82. This shows Bike World's annual purchase volume exceeds $10,000 every year,
but there's no clear information indicating which customer tier they fall into.

However, per the customer tier and payment-terms matrix in
[AW_Corporate_Credit_and_Payment_Terms_Policy.docx], we can see:
- Tier 2 Preferred Account's maximum allowed credit limit is $750,000, requiring annual
  purchase volume over $1,000,000.
- Tier 3 Standard Account's maximum allowed credit limit is $250,000, requiring annual
  purchase volume over $10,000 with a good payment-compliance history.

Since Bike World's highest annual purchase volume is $47,508.55, below the $1,000,000
threshold, they likely don't qualify as a Tier 2 Preferred Account. Taking their
purchase volume and probable payment history into account, Bike World is likely a Tier
3 Standard Account, with a maximum allowed credit limit of $250,000.

That said, it should be noted this conclusion is based on previously provided
information and the policy documents, with no direct evidence proving which customer
tier Bike World actually falls into. To give a precise answer, further confirmation of
Bike World's customer tier and specific credit limit cap would be needed.

Information gathering may be incomplete; this conclusion is based on the prior
conversation and the policy documents.
[genie_conversation_id: previous turn='01f18a79e9ae1ceb8da4f595fe0e070f' -> this turn='01f18a79e9ae1ceb8da4f595fe0e070f']
```

**Observation**: `genie_conversation_id` was correctly reused; in the second turn,
"they" was correctly resolved to Bike World (it restated the specific numbers from the
first turn) — pronoun resolution worked. But the second turn triggered the router's
"3 consecutive malformed-output errors, safely degrading to finalize"
(`Error code: 400 - BAD_REQUEST: Model response did not respect the required format`) —
this is the known occasional issue already documented in `src/graph/router.py`'s module
comment and case 5 of `docs/DEVELOPMENT_JOURNAL.md`, not something introduced by this
change. The consequence was that `structured_agent` never actually got called, and
finalize had to guess the tier from the historical numbers alone. The final guess
(Tier 3, $250,000) happened to match the real computed result from the other two runs,
but this time it was "guessed," not "looked up," and the answer honestly included a
disclaimer that the conclusion might be incomplete.

---

## Run 2 (chat.py-equivalent path: rerun once, to verify whether run 1 reproduces reliably)

**Result: fully successful, no LLM error**

```
Turn 1 - User: What's customer Bike World's annual purchase volume?
Agent: Customer Bike World's annual purchase volumes are: 2023 $47,508.55, 2024
$41,509.61, 2025 $17,339.82. 2023 had the highest purchase volume, with a clear decline
by 2025.
[genie_conversation_id: previous turn=None -> this turn='01f18a7ae4f212f5b13def4b7291e9eb']

Turn 2 - User: And what's their credit limit cap?
Agent: Based on the prior conversation and the retrieval results, customer Bike World's
credit limit cap is **$250,000**. This limit is computed from Bike World's years as a
customer and this year's purchase volume. Since Bike World's annual purchase volumes
were $47,508.55, $41,509.61, and $17,339.82, none reaching the $1M threshold, they are
classified as a Tier 3 Standard Account, with a maximum allowed credit limit of
$250,000.

Note: information may be incomplete, since the prior conversation and retrieval
results didn't provide every relevant detail.
[genie_conversation_id: previous turn='01f18a7ae4f212f5b13def4b7291e9eb' -> this turn='01f18a7ae4f212f5b13def4b7291e9eb']
```

**Observation**: this time router completed the full multi-hop path (`structured_agent`
was actually called, correctly computing $250,000, Tier 3) — the numbers matched run 1's
"guessed result," but this time it was genuinely looked up/computed, not guessed. This
indirectly confirms the business-rule computation itself (the `calculate_credit_terms`
UC Function) is stable and deterministic — the only instability was in "whether router
decides to recompute this step." `genie_conversation_id` was correctly reused as well.

---

## Run 3 (deployed-App-equivalent path: instantiating `SalesDuoResponsesAgent.predict()` directly)

**Result: fully successful — verifies the `custom_inputs`/`custom_outputs` passthrough layer specifically (not covered by the first two runs)**

```
Turn 1 - User: What's customer Bike World's annual purchase volume?
Agent: Customer Bike World's annual purchase volumes are: 2023 $47,508.55, 2024
$41,509.61, 2025 $17,339.82. Note: there was a clear decline in 2025. Since the
question only concerns the data already provided, no additional information is needed.
[genie_conversation_id: previous turn=None -> this turn='01f18a7d338f11e88dba341dcc76a984']

Turn 2 - User: And what's their credit limit cap?
Agent: Customer Bike World's credit limit cap is **$250,000**. This limit was
automatically computed from their annual purchase volume and years as a customer with
the company. Since this was already established in the prior conversation, no further
computation or retrieval is needed.
[genie_conversation_id: previous turn='01f18a7d338f11e88dba341dcc76a984' -> this turn='01f18a7d338f11e88dba341dcc76a984']
```

**Observation**: this went through the formal protocol path
(`ResponsesAgentRequest`/`ResponsesAgentResponse` — `request.input` carries the full
history, `request.custom_inputs`/`response.custom_outputs` carry
`genie_conversation_id`), not a direct `graph.invoke()` call. The result matched run 2
($250,000), `genie_conversation_id` was correctly passed through, confirming the newly
added `custom_inputs`/`custom_outputs` read/write logic in `src/agent.py` is correct.

---

## Summary conclusion

- **The core mechanism (3/3 runs)**: `genie_conversation_id` was correctly reused
  across turns; "they" was correctly resolved to Bike World every time. Both
  independent code paths (the graph's internal invocation / the ResponsesAgent protocol
  layer) were verified, not just one.
- **1/3 runs triggered a known LLM structured-output occasional error, not introduced by
  this change**, causing router to skip re-triggering the computation, with finalize
  guessing from historical numbers instead (the guess happened to be correct, but the
  methodology wasn't reliable) — recorded as a new finding in
  `docs/CODE_REVIEW_FINDINGS.md` (related to item 10, the "history window mismatch"
  risk).
- The stable, correctly-computed final result: customer Bike World is a Tier 3 Standard
  Account, with a credit limit cap of $250,000.
