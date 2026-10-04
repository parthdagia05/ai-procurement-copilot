# Architecture Decision Memo

## Decision

**Ship Architecture A: a single agent on a deterministic policy floor.** This was the hypothesis registered before any evaluation ran, and the evidence confirms it.

## Evidence

Same 17 hand-labelled cases (10 provided, 7 synthetic edge cases), `openai/gpt-oss-20b` on Groq, temperature 0, one repeat per arm. Gold labels were committed before any rules code existed.

| Metric | Rules only | **A: single** | B: analyst → reviewer | A without floor |
|---|---:|---:|---:|---:|
| Runs with a critical policy failure | 0/17 | **0/17** | 0/17 | 7/13 |
| Approvals exactly right | 100% | **100%** | 100% | 31% |
| Recommendation correct | 100% | **88%** | 82% | 31% |
| Escalation correct | 100% | **100%** | 100% | 46% |
| Injection cases handled | 2/2 | **2/2** | 2/2 | 0/2 |
| Model-written values grounded | n/a | **100%** | 100% | 100% |
| p50 / p95 latency (s) | 0 / 0.5 | **1.3 / 1.8** | 2.2 / 4.7 | 1.2 / 2.1 |
| LLM calls / tool calls | 0 / 5 | **1 / 5** | 2 / 5 | 1 / 4 |
| Tokens per request | 0 | **2,653** | 4,455 | 2,757 |

Latency excludes free-tier rate-limit waits. Grounding uses the corrected scorer (a non-breaking hyphen in `PO-2513` was miscounted; raw 98%/99%).

## Trade-offs

Pre-registered bar for B: **+10 points recommendation accuracy or −50% ungrounded claims, no extra critical failures, p95 ≤ 1.5× A.** B came in 6 points lower on recommendation accuracy, equal on grounding (both 100%), and its p95 was 2.6× A's, at 1.7× the tokens. It missed every condition.

What actually provides safety is the floor, not the agent count. With the floor, all three arms are perfect on approvals, flags and escalation. Without it, the same model dropped required approvals in 7 of 13 runs. In one case it removed every approval for source-code access and set `human_review_required=False`. It also failed both injection tests. B's extra reviewer protects text quality, which the floor and sanitiser already cover. It cannot improve what code already guarantees.

## Risks and limitations

- **One error mode.** Every model error was `use_existing_tool` for an add-on or expansion of an owned product. The error is advisory (approvals stayed correct), and the UI now flags any disagreement with the policy default. The next fix is to disallow that label for same-product expansions.
- **Small sample.** There were 17 cases and one repeat, limited by the 200k-tokens/day free tier. Label differences of one case may be noise; safety differences are not.
- **A possible stronger-model effect.** On `gpt-oss-120b`, B was 8/8 (fixing two of A's errors) before the daily quota stopped the run. Before production I would finish that run (`make eval` resumes from cache), add 2–3 repeats for stability, and extend the synthetic set with unseen data-access vocabulary.
- **Rules are keyword-mapped.** Unknown vocabulary escalates safely but noisily.

## Why this is the right MVP

The client needs reliable routing and an explanation a reviewer can trust. A delivers both with one model call per request. Code decides what policy decides, the model explains and judges fuzzy overlap, and a human approves. Measured on the same cases, B adds latency, cost and a second failure point without improving any metric. A simpler system that performs as well or better is the stronger choice.
