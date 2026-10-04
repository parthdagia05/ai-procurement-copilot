"""Comparison evaluation: same gold test set for every architecture.

  python evals/run_eval.py                                   # all arms, 1 repeat, record cache
  python evals/run_eval.py --arms single,staged --repeats 2
  python evals/run_eval.py --replay                          # offline, from evals/llm_cache (no key needed)

Arms
  deterministic  policy engine only (control; no LLM)
  single         Architecture A
  staged         Architecture B
  unguarded      ABLATION: A without the deterministic floor (public + synthetic cases)

Responses are cached per (prompt, repeat) in evals/llm_cache, so an interrupted run
resumes for free. The runner STOPS on a daily-quota error instead of recording
deterministic fallbacks as if they were LLM results.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.scoring import score  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.mock_service import ensure_mock_api  # noqa: E402
from src.solution import run  # noqa: E402

ARMS = {
    "deterministic": ("single", "deterministic"),
    "single": ("single", None),
    "staged": ("staged", None),
    "unguarded": ("single", "unguarded"),
}
ARM_TITLES = {"deterministic": "Deterministic only", "single": "A: single agent", "staged": "B: analyst → reviewer",
              "unguarded": "A without floor (ablation)"}
ABORT_MARKERS = ("QuotaExhausted", "CacheMiss", "no LLM API key")


class Abort(RuntimeError):
    pass


def load_cases() -> tuple[list[dict], dict]:
    gold = json.loads((ROOT / "evals" / "gold_cases.json").read_text(encoding="utf-8"))["cases"]
    public = {c["request_id"]: c["expectations"] for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}
    return gold, public


def run_arm(arm: str, cases: list[dict], public: dict, repeat: int, llm: LLMClient | None, out: Path) -> list[dict]:
    architecture, mode = ARMS[arm]
    rows = []
    for data_dir in sorted({c["data_dir"] for c in cases}):
        os.environ["PROCUREMENT_DATA_DIR"] = data_dir
        with ensure_mock_api(ROOT / data_dir):
            for case in [c for c in cases if c["data_dir"] == data_dir]:
                decision, ctx = run(case["request_id"], architecture, llm=llm, mode=mode, cache_tag=f"rep{repeat}")
                reason = decision.telemetry.fallback_reason or ""
                if arm != "deterministic" and any(m in reason for m in ABORT_MARKERS):
                    raise Abort(f"{arm} {case['case_id']}: {reason}")
                row = {"arm": arm, "repeat": repeat, **score(case, decision, ctx, public.get(case["request_id"]) if case["source"] == "public" else None)}
                rows.append(row)
                record = out / "decisions" / arm / f"{case['case_id']}_r{repeat}.json"
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({"decision": decision.model_dump(), "assessment": ctx.memo.get("assessment"),
                                              "evidence_pack": ctx.memo.get("evidence_pack"), "score": row, "trace": ctx.trace},
                                             indent=1, default=str), encoding="utf-8")
                print(f"  {arm:13} r{repeat} {case['case_id']:8} label={row['label']:30} "
                      f"{'OK ' if row['label_correct'] else 'BAD'} critical={row['critical_failures']} "
                      f"calls={row['llm_calls']} tokens={row['tokens']} net={row['net_latency_ms']:.0f}ms"
                      + (f"  !! {row['critical_detail']}" if row["critical_failures"] else "")
                      + (f"  [fallback: {reason[:90]}]" if reason and arm != "deterministic" else ""), flush=True)
    os.environ.pop("PROCUREMENT_DATA_DIR", None)
    return rows


def pct(values: list) -> str:
    vals = [bool(v) for v in values if v != ""]
    return f"{100 * sum(vals) / len(vals):.0f}% ({sum(vals)}/{len(vals)})" if vals else "n/a"


def summarise(rows: list[dict], arms: list[str]) -> tuple[str, dict]:
    table: dict[str, dict] = {}
    for arm in arms:
        r = [x for x in rows if x["arm"] == arm]
        if not r:
            continue
        live = [x for x in r if not x["cache_hits"]]
        net = sorted(x["net_latency_ms"] for x in live)
        labels_by_case: dict[str, set] = {}
        for x in r:
            labels_by_case.setdefault(x["case_id"], set()).add(x["label"])
        multi = [c for c in labels_by_case if sum(1 for x in r if x["case_id"] == c) > 1]
        grounded, total = sum(x["values_grounded"] for x in r), sum(x["values_total"] for x in r)
        table[arm] = {
            "Runs (cases x repeats)": f"{len({x['case_id'] for x in r})} x {len({x['repeat'] for x in r})}",
            "Critical policy failures (runs with >=1)": f"{sum(1 for x in r if x['critical_failures'])} / {len(r)}",
            "Approvals exactly right": pct([x["approvals_exact"] for x in r]),
            "Recommendation label correct": pct([x["label_correct"] for x in r]),
            "Escalation correct": pct([x["escalation_correct"] for x in r]),
            "Missing-info correct": pct([x["missing_info_correct"] for x in r]),
            "Injection cases handled": pct([x["injection_handled"] for x in r]),
            "LLM-text values grounded": f"{100 * grounded / total:.0f}% ({grounded}/{total})" if total else "n/a (no LLM text)",
            "Ungrounded claims dropped by merge": str(sum(x["ungrounded_claims_dropped"] for x in r)),
            "Unsafe model text dropped": str(sum(x["unsafe_text_dropped"] for x in r)),
            "Disallowed labels replaced": str(sum(x["label_rejected"] for x in r)),
            "Label stability across repeats": pct([len(labels_by_case[c]) == 1 for c in multi]) if multi else "n/a (1 repeat)",
            "Net latency p50 / p95 (ms, excl. rate-limit wait)":
                f"{statistics.median(net):.0f} / {net[min(len(net) - 1, int(0.95 * len(net)))]:.0f}" if net else "n/a (cached)",
            "Mean LLM calls": f"{statistics.mean(x['llm_calls'] for x in r):.2f}",
            "Mean tool calls": f"{statistics.mean(x['tool_calls'] for x in r):.2f}",
            "Mean tokens per request": f"{statistics.mean(x['tokens'] for x in r):.0f}",
            "Deterministic fallbacks": str(sum(1 for x in r if x["fallback_reason"] and arm != "deterministic")),
            "Public runner minimum checks": pct([x["public_min_checks"] for x in r]),
        }
    metrics = list(next(iter(table.values())).keys()) if table else []
    lines = ["| Metric | " + " | ".join(ARM_TITLES[a] for a in table) + " |", "|---" * (len(table) + 1) + "|"]
    lines += [f"| {m} | " + " | ".join(table[a][m] for a in table) + " |" for m in metrics]

    failures = [x for x in rows if x["critical_failures"]]
    detail = ["", "### Critical failures", ""] + ([f"- **{ARM_TITLES[x['arm']]}** {x['case_id']} r{x['repeat']}: {x['critical_detail']}" for x in failures] or ["None."])
    wrong = [x for x in rows if not x["label_correct"]]
    detail += ["", "### Wrong recommendation labels", ""] + ([f"- **{ARM_TITLES[x['arm']]}** {x['case_id']} r{x['repeat']}: `{x['label']}`" for x in wrong] or ["None."])
    return "\n".join(lines + detail), table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="deterministic,single,staged,unguarded")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--replay", action="store_true", help="use cached responses only (offline)")
    ap.add_argument("--out", default=None, help="results directory name under evals/results/")
    args = ap.parse_args()

    os.environ["LLM_CACHE"] = "replay" if args.replay else os.environ.get("LLM_CACHE", "record")
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    gold, public = load_cases()
    llm = LLMClient()
    out = ROOT / "evals" / "results" / (args.out or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{llm.settings.model.replace('/', '_')}")
    out.mkdir(parents=True, exist_ok=True)
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    (out / "config.json").write_text(json.dumps({
        "model": llm.settings.model, "provider": llm.settings.provider, "temperature": 0, "arms": arms,
        "repeats": args.repeats, "cache_mode": os.environ["LLM_CACHE"], "git_commit": git, "cases": [c["case_id"] for c in gold],
    }, indent=2), encoding="utf-8")

    rows: list[dict] = []
    aborted = None
    try:
        for repeat in range(1, args.repeats + 1):
            for arm in arms:
                cases = [c for c in gold if c["source"] in {"public", "synthetic"}] if arm == "unguarded" else gold
                if arm == "deterministic" and repeat > 1:
                    continue  # deterministic: identical every run
                print(f"\n== {ARM_TITLES[arm]} (repeat {repeat}, {len(cases)} cases)", flush=True)
                rows += run_arm(arm, cases, public, repeat, None if arm == "deterministic" else llm, out)
    except Abort as exc:
        aborted = str(exc)
        print(f"\nSTOPPED: {aborted}\nRe-run the same command later: cached responses are reused, so it resumes where it stopped.")

    if rows:
        with (out / "scores.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        md, table = summarise(rows, arms)
        header = f"# Evaluation results\n\nModel `{llm.settings.model}`, temperature 0, commit `{git}`, repeats {args.repeats}." \
                 + (f"\n\n**Incomplete run:** {aborted}" if aborted else "") + "\n\n"
        (out / "summary.md").write_text(header + md + "\n", encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(table, indent=2), encoding="utf-8")
        print("\n" + md)
        print(f"\nResults: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
