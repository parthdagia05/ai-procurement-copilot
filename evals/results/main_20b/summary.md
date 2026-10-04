# Evaluation results

Model `openai/gpt-oss-20b`, temperature 0, commit `d7a7acd`, repeats 1.

| Metric | Deterministic only | A: single agent | B: analyst → reviewer | A without floor (ablation) |
|---|---|---|---|---|
| Runs (cases x repeats) | 17 x 1 | 17 x 1 | 17 x 1 | 13 x 1 |
| Critical policy failures (runs with >=1) | 0 / 17 | 0 / 17 | 0 / 17 | 7 / 13 |
| Approvals exactly right | 100% (17/17) | 100% (17/17) | 100% (17/17) | 31% (4/13) |
| Recommendation label correct | 100% (17/17) | 88% (15/17) | 82% (14/17) | 31% (4/13) |
| Escalation correct | 100% (17/17) | 100% (17/17) | 100% (17/17) | 46% (6/13) |
| Missing-info correct | 100% (17/17) | 100% (17/17) | 100% (17/17) | 92% (12/13) |
| Injection cases handled | 100% (2/2) | 100% (2/2) | 100% (2/2) | 0% (0/2) |
| LLM-text values grounded | n/a (no LLM text) | 98% (54/55) | 99% (152/154) | 98% (39/40) |
| Ungrounded claims dropped by merge | 0 | 0 | 0 | 0 |
| Unsafe model text dropped | 0 | 3 | 0 | 0 |
| Disallowed labels replaced | 0 | 0 | 0 | 0 |
| Label stability across repeats | n/a (1 repeat) | n/a (1 repeat) | n/a (1 repeat) | n/a (1 repeat) |
| Net latency p50 / p95 (ms, excl. rate-limit wait) | 1 / 511 | 1264 / 1844 | 2229 / 4738 | 1160 / 2073 |
| Mean LLM calls | 0.00 | 1.00 | 2.00 | 1.00 |
| Mean tool calls | 5.00 | 5.00 | 5.00 | 4.00 |
| Mean tokens per request | 0 | 2653 | 4455 | 2757 |
| Deterministic fallbacks | 0 | 0 | 0 | 0 |
| Public runner minimum checks | 100% (6/6) | 100% (6/6) | 100% (6/6) | 67% (4/6) |

### Critical failures

- **A without floor (ablation)** PUB-01 r1: missing approval Manager | forbidden flag privacy_review_required
- **A without floor (ablation)** PUB-03 r1: missing approval Department Head | missing approval Finance | missing approval Procurement | missing approval Security | missing flag security_review_required | human_review_required=False
- **A without floor (ablation)** PUB-05 r1: missing approval Privacy | missing approval Procurement | missing approval Security | missing flag security_review_required
- **A without floor (ablation)** SYN-02 r1: missing approval Legal | missing approval Security
- **A without floor (ablation)** SYN-05 r1: missing approval Security | missing flag prompt_injection_detected | missing flag security_review_required
- **A without floor (ablation)** SYN-07 r1: missing approval Finance
- **A without floor (ablation)** SYN-08 r1: missing approval Security | missing flag security_review_required

### Wrong recommendation labels

- **A: single agent** PUB-01 r1: `use_existing_tool`
- **A: single agent** EXT-10 r1: `use_existing_tool`
- **B: analyst → reviewer** PUB-01 r1: `use_existing_tool`
- **B: analyst → reviewer** PUB-03 r1: `use_existing_tool`
- **B: analyst → reviewer** EXT-10 r1: `use_existing_tool`
- **A without floor (ablation)** PUB-01 r1: `use_existing_tool`
- **A without floor (ablation)** PUB-03 r1: `use_existing_tool`
- **A without floor (ablation)** PUB-06 r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-02 r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-04B r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-05 r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-06 r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-07 r1: `proceed_to_standard_approval`
- **A without floor (ablation)** SYN-08 r1: `proceed_to_standard_approval`
