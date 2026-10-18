# One User, One Agent: STAGE take-home

A deterministic **decide → act → verify → measure** agent for STAGE's replay day. For each `app_open` or
`login_success` trigger, it works out who the viewer is, commits to at most one evidence-backed goal, and picks the
smallest useful intervention (home page, push, WhatsApp, in-app message, or nothing). An independent verifier checks
every action before it is published. Impact is then measured against the randomized holdout.

The agent uses only the Python 3.9+ standard library. The tests need `pytest`. A mock/replay model provider stands
in for a real model, so no API key, network or third-party package is required.

## Run everything (one command)

From this `submission/` directory:

```bash
python -m src.run_replay
```

This one command:

1. Loads and audits the pack.
2. Replays the day: 2,769 triggers plus the live and control events.
3. Writes `decisions.jsonl` and `actions.jsonl`.
4. Runs `simulator/simulate_outcomes.py` on the actions.
5. Judges every goal from the simulator output.
6. Writes the impact, latency and final reports.
7. Re-runs the simulator with three more seeds as a sensitivity check.

It takes about 45 seconds.

The pack is found automatically if it sits next to `submission/` (as `../one_user_one_agent_pack`) or if
`submission/` is inside the pack. Otherwise, point at it:

```bash
python -m src.run_replay --pack /path/to/one_user_one_agent_pack --seed oua-practice
python -m src.run_replay --robustness-seeds ""      # skip the extra-seed sensitivity runs
python -m src.run_replay --skip-sim                 # agent replay only
```

Nothing is hard-coded to the practice day. Dates, the snapshot time, dialects, withdrawn titles, faults and outage
windows are all read from the pack, so a second replay day runs with the same command.

## Tests

```bash
python -m pytest -q          # 20 tests, ~25 s (runs the real replay once, then the simulator once)
```

These four are mandatory:

1. Holdout users never get an action.
2. A duplicate trigger never produces a duplicate action.
3. Model timeout, malformed output and unsupported claims fall back to rules (unit tests and the replayed faults).
4. Quiet hours, consent and the frequency cap are enforced.

The extra tests cover:

- Stale offline triggers vs. clock-skewed ones.
- Dialect precedence and conflict logging.
- Home-page platform rules.
- Title withdrawal and live-page replacement.
- Prompt-injection quarantine.
- Exclusion of unmeasurable goals.
- The G03 rule correction.
- Re-planning after a live purchase.
- Copy and pricing honesty.
- Latency and call budgets.
- One decision line per trigger.
- An end-to-end check that the simulator reports 0 policy problems.

## Outputs (`sample_run/`)

| File | What it is |
|---|---|
| `decisions.jsonl` | One line per trigger (including every `NO_ACTION`, with its reason), plus re-plans after a title withdrawal |
| `actions.jsonl` | Only published actions. This is the simulator's input |
| `revisions.jsonl` | Goal closures, cancelled scheduled messages, rule corrections, dependency incidents |
| `viewer_state/` | One durable JSON state per viewer: facts with provenance, goal, pending actions, ledgers, learnings |
| `outcomes.jsonl` | Every committed goal judged `succeeded` / `failed` / `inconclusive`, with the evidence event ids |
| `impact_report.md` | ITT uplift with 95% CIs, per-goal results, goal-agnostic ITT, exposure, and the pilot re-analysis |
| `latency_report.json` | Latency percentiles overall, during each outage window and at the traffic peak, plus budget use |
| `final_report.md` | One-page summary: headline numbers, abstention reasons, every data trap and how it was handled |
| `data_audit.json` | Duplicates, missing profiles, injections, malformed deeplinks, excluded goals, rule corrections |
| `sim/`, `sim_seeds/` | Raw simulator output for the practice seed and the sensitivity seeds |

## Code map

| Module | Responsibility |
|---|---|
| `src/data_loader.py` | Loads the pack, dedupes events and profiles, scans free text for prompt injection |
| `src/fact_resolver.py` | IST and clock math, freshness, precedence rules, conflict logging |
| `src/state_store.py` | Durable per-viewer state; the user-data-store client with timeout/degradation |
| `src/goal_planner.py` | Eligibility from `goals.json`, unmeasurable-goal detection, interpretations, rule versions, success matching |
| `src/intervention_planner.py` | Builds the home page, push, WhatsApp or in-app candidate |
| `src/model_provider.py` | Mock/replay model, fault reproduction, circuit breaker, output validation |
| `src/verifier.py` | Independent policy checker with veto power |
| `src/replay_harness.py` | Event-ordered replay, per-decision budget, worker-pool latency model, live re-planning |
| `src/outcome_judge.py` | Goal judgement from `outcome_events.jsonl` |
| `src/impact_reporter.py` | ITT and CIs, pilot re-analysis, latency report, final report |
| `src/run_replay.py` | The single CLI entrypoint |

Read next: [ARCHITECTURE.md](ARCHITECTURE.md), [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md),
[AI_COLLABORATION.md](AI_COLLABORATION.md).
