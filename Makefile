PY := .venv/bin/python

.PHONY: start test public eval eval-replay deterministic

start:            ## one-command start: venv, install, preflight, mock API + UI
	./start.sh

test:             ## unit + integration tests (no LLM calls)
	$(PY) -m unittest discover -s tests

public:           ## starter-pack public runner, both architectures
	$(PY) evals/run_public_evals.py --architecture single
	$(PY) evals/run_public_evals.py --architecture staged

eval:             ## live comparison eval (needs an LLM key; resumable)
	$(PY) evals/run_eval.py --arms deterministic,single,staged,unguarded

eval-replay:      ## reproduce the committed eval offline from the response cache (no key)
	$(PY) evals/run_eval.py --arms deterministic,single,staged,unguarded --replay --out replay

deterministic:    ## public runner with the policy engine only
	COPILOT_MODE=deterministic $(PY) evals/run_public_evals.py --architecture single
