# Architecture

## The loop

```
 triggers.jsonl ─┐                ┌──────────────────────── one decision (budget: 8 model / 20 tool calls, 60 s) ───────────────────────┐
 live_events ────┼─► replay ─────►│ idempotency ─► store read ─► resolve facts ─► freshness ─► dialect ─► goal ─► intervention ─► VERIFY │──► publish / abstain
 control_events ─┘   harness      │  (event_id)   (timeout)     (provenance)    (>600 s?)   (valid?)   (1 max)   (smallest)    (veto)  │      │
   (time-ordered)     │           └──────────────────────────────────────────────────────────────────────────────────────────────────────┘      │
                      │ live event: update state, close achieved/obsolete goals, cancel pending messages                                          ▼
                      │ control: withdraw title (replace live pages), correct goal rule (re-point open goals), outage windows   decisions.jsonl / actions.jsonl
                      ▼                                                                                                                           │
               viewer_state/<user>.json ◄── learnings ── outcome_judge ◄── simulator outcome_events ◄────────────────────────────────────────────┘
                                                            │
                                                            └─► impact_reporter: ITT agent vs holdout, 95% CI, pilot re-analysis, latency
```

## Ownership: one viewer, one agent

Each viewer has one logical agent. Its state is one document in `viewer_state/<user_id>.json`, and that state is
never shared between viewers. The harness serialises a viewer's decisions: a viewer's next decision cannot start
before their previous one has published, which is modelled by `user_busy` in the worker pool.

Across viewers, decisions run on a pool of 4 workers in virtual time. A traffic spike therefore shows up as queueing
delay inside `decided_in_ms` instead of being hidden.

Global facts, such as a withdrawn title, a corrected goal rule or an outage window, are broadcast by the harness.
Each agent then acts on them for its own viewer.

## Understanding the viewer (`fact_resolver.py`)

Every fact is `{value, source, as_of, freshness_s, conflicts}`.

**Time.** All maths is on server `received_at`. Phone clocks are wrong in two different ways:

- **Skew** shifts `occurred_at` and `sent_at` together.
- **Offline buffering** delays `sent_at`.

So `age = sent_at − occurred_at` measures buffering without being fooled by skew, and the true event time is
estimated as `received_at − age`. Applying the brief's suggested `received_at − occurred_at` rule to this pack would
have abstained on 23 fresh triggers and acted on 2 stale ones. A trigger buffered for more than 600 s is history,
not a visit. It is recorded, and the agent abstains.

**Precedence rules:**

| Fact | Rule |
|---|---|
| Dialect | The trigger's `app_dialect` wins, because the app shows the page for that dialect. Profile, deeplink and `app_dialect_changed` disagreements are written to `dialect.conflicts`. `d7` and `"NA"` are values the app actually reports and have no catalogue titles, so the dialect is **unresolved** and the agent abstains. A missing value (`None`) is a telemetry gap. The agent falls back to the last valid app setting, then the profile, but only if they don't disagree. That dialect is used for a message only, never a page: no page can match a session that reports no dialect (`simulate_outcomes.py` line 249). |
| Subscription | A purchase, renewal or lapse received after `snapshot_at` overrides the snapshot. Viewers with no profile get their status from events. |
| Consent | Profile opt-in AND no `notification_opt_out` for that channel at any time (the stricter reading). Missing means false. |
| Profiles | Duplicate rows: keep the latest `updated_at`. Missing profiles are synthesised from `signup_completed`. |

## Planning (`goal_planner.py`)

**Measurability first.** A goal whose `success.event` is null (G10) or not in the event schema (G11,
`tv_app_open`) can never be judged. Both are excluded at load time, and the reason is written to `data_audit.json`.

**Priority.** Eligibility runs in a fixed priority order:

```
G04 > G01 > G03 > G07 > G12 > G02 > G09 > G08 > G06 > G05
```

The most specific, evidence-backed need comes first. Low-base-rate conversion goals come last. The first eligible
goal is the rule choice.

**Interpretations.** Ambiguous rules get one written interpretation each, copied into every decision's
`assumptions`:

- **G06** ("recently lapsed") means a lapse observed in the last 30 days.
- **G07** ("unfinished, not played for 3+ days") means the viewer reached episode 2 or completed an episode, has
  episodes left, has not played the series for 72 h, and the series is in the app dialect.

**Commitment.** One goal per viewer at a time. The goal window starts when the decision publishes. Later triggers
continue the open goal; they do not re-plan it.

**Corrections.** `goal_rule_corrected` bumps `rule_version` (v1 → v1.1) and re-points every open goal at the new
rule, with a revision record. The outcome judge uses the rule in force.

**Live re-planning.** A live event that satisfies the open goal closes it as `achieved_live`. A subscription change
that invalidates a conversion goal closes it as `obsolete`. In both cases, pending messages are cancelled. An opt-out
cancels that channel's pending messages. A dialect change cancels messages naming titles in another dialect.

## Acting (`intervention_planner.py`)

**Home page.** This is the default action.

- **Hero.** The goal's own target if it has one (the unfinished series for G07, a new release for G08, a premium
  title for conversion goals). Otherwise, the most popular unwatched title.
- **Rows.** Up to 5 rows, from `next_episode`, `because_you_watched`, `popular_in_dialect`, `new_releases` and
  `genre` (×2), in an order that depends on the goal.
- **Valid titles.** One title per `content_slug` across the whole page, in the app-language variant. A title must be
  in the dialect, and available both at publish time and for the whole 2-hour TTL, so a title expiring at 12:30Z is
  never promoted at 11:00Z. It must not be withdrawn or quarantined.
- **Row size.** Rows with fewer than 3 titles are not built. Row length adapts to thin catalogues (`d3` has 15
  titles).

**Message.** At most one per viewer, and only for goals that need a return on a later day (G03, G07, G09). It is
sent at 19:00 IST the next day, which is outside quiet hours by construction.

- Channel preference is push, then WhatsApp (approved template `wa_recommend_title_v1`, variables copied verbatim
  from the catalogue), then an in-app message when there is no channel consent.
- The copy names only real episodes and real titles, with no prices or offers.
- A scheduled message stays cancellable until its `send_at`.

**No action.** Doing nothing is a normal decision. The agent abstains in these cases:

- duplicate trigger
- stale trigger
- unresolved dialect
- no eligible goal
- personalised page still live (more than 30 minutes left)
- holdout user
- verifier veto
- budget exhausted

## The model's role (`model_provider.py`)

The model is used in one place: choosing among goals that the rules have already found eligible. That is the only
real judgement call in the loop. Its output is treated as untrusted:

- The JSON is parsed strictly.
- The chosen goal must be in the eligible set.
- Its `confidence` and `reason` are recorded but never used as evidence.
- A reason that cites a source the agent does not have (for example "confirmed in billing") is rejected.

Failures fall back to the rule choice:

| Failure | Handling |
|---|---|
| Timeout | Client timeout of 1.5 s |
| HTTP 503 | Rule choice, and a 30-second circuit breaker |
| Malformed output, schema violation, unsupported claim | Rule choice, with `fallback_used: true` |

Holdout viewers go through the same advisor. Their shadow goal is chosen exactly as an agent viewer's would be, so
goal assignment stays identical across arms even once a real model is plugged in.

The model is not called for decisions that continue an open goal, for decisions that end earlier (duplicate, stale,
unresolved dialect), or under load shedding (more than 10 s of queueing, applied to both arms). Every trigger named
in `model_faults.jsonl` carries a `model_fault` record in `decisions.jsonl` saying whether the fault was exercised
and how it was handled. In mock mode, the "model" answers deterministically with the rule choice.

## Verification (`verifier.py`): independent, with veto power

The verifier does not trust the planner. It re-derives what it needs from raw sources:

- holdout from `arms.csv`
- consent from the raw profile row and the raw event log
- the frequency cap from its own approval ledger and delivered messages
- withdrawals from its own copy of the control events

It never repairs an action. It vetoes it, and the failed check is written to `checks.failed`. The checks are:

| Area | Checks |
|---|---|
| Every action | holdout, idempotency (trigger × type), latency < 60 s, model/tool budget |
| Home page | schema, page dialect = app dialect, ≤ 6 rows, 3–10 titles per row, allowed row types, no "continue watching", every title in dialect + available + not withdrawn + not quarantined, `content_slug` unique across hero and rows |
| Push / in-app | consent and opt-out, quiet hours at `send_at`, 1 push+WhatsApp per IST day, title ≤ 40 / body ≤ 120 characters, forbidden phrases, only ₹1 / ₹149 / ₹399 and no offer words, no instruction-like text, deeplink `ott://title/<id>` matching the title, title in dialect and available at `send_at` |
| WhatsApp | approved template only, exact variable set, values equal to catalogue or pricing values |

## Memory (`state_store.py`)

The viewer state holds:

- resolved facts with provenance
- the active goal and goal history, including revisions
- pending, published and cancelled actions
- the live page, with its TTL and title ids
- the message and in-app ledgers per IST day
- processed trigger ids
- decision summaries
- revisions
- **learnings**: rule corrections applied, each goal's judged outcome, and push tokens found invalid by the simulator
  (the learning is to prefer home page or WhatsApp next time)

Raw events are not copied into the state file. They can be re-derived from the pack and the replay feed, and the
state keeps a summary.

## Latency

The README asks for measured processing time. Here:

```
decided_in_ms = queueing in the virtual worker pool + measured wall-clock compute + simulated dependency latency
```

- The simulated dependencies are the user store (4–25 ms) and the model (120–400 ms).
- `published_at = deliver_at + decided_in_ms`, and a test checks this for every decision.
- The simulator shows the home page 3–40 s after the app opens, so the real target is under 3 s, not under 60 s.
  All 933 pages met it. The p95 for the whole run is about 370 ms, and the worst case (store degraded) is about
  1.2 s.

## Failure recovery

| Failure | Behaviour |
|---|---|
| Model timeout / 503 / garbage / hallucinated eligibility | Rule choice, `fallback_used: true`, circuit breaker on 503 |
| User data store degraded (p95 8 s, 20% timeouts) | 800 ms read timeout, then cached state. Confidence −0.2. Push and WhatsApp **fail closed**, because consent cannot be re-verified. The home page still ships. |
| Traffic spike | Worker-pool queueing is measured. The optional model call is shed above 10 s of queue. |
| Title withdrawn | Every live page that contains it is re-published without it at the withdrawal time. Pending messages naming it are cancelled. The verifier blocks it from then on. |
| Rule corrected mid-day | Version bump, open goals re-pointed, judge applies the rule in force |
| Purchase arrives after a decision | Goal closed (achieved or obsolete), pending messages cancelled |
| Budget exceeded | `BudgetExceeded`, then abstain with `budget_exceeded` |

## Trust boundaries

| Source | Trust |
|---|---|
| `received_at`, the catalogue's structured fields, `arms.csv`, config | Trusted |
| Phone clocks (`occurred_at`, `sent_at`) | Not trusted for time. Used only for their difference. |
| Free text: titles, descriptions, search queries, deeplinks | **Data, never instruction.** Each is scanned with injection patterns. Flagged titles (T0002, T0522) are quarantined from every page and message, and logged in `data_audit.json`. Deeplinks are parsed with a strict regex and used only as a conflict signal. Title text goes into fixed copy slots only after sanitising. |
| Model output | Advisory only, inside the eligible set. Never evidence. |
| Personal data | Everything runs locally. The mock model sends nothing anywhere, as messaging policy §7 requires. |

## Measurement (`outcome_judge.py`, `impact_reporter.py`)

**Shadow goals.** Holdout viewers get a shadow goal from the identical rules at the identical moment, and no action.
Goal assignment is therefore pre-treatment and identically distributed in both arms. Comparing goal success between
the arms as randomized is an unbiased intention-to-treat estimate.

**Statistics:**

- Newcombe hybrid-score 95% CIs and a two-sided z-test.
- A minimum detectable effect is stated.
- A goal-agnostic ITT (any play, completion, purchase, play on 3+ days, plays per user) over every triggered user
  needs no goal logic at all.
- Results from extra simulator seeds are reported next to the headline.

**Biased comparisons, labelled.** The "served vs everyone else" comparison is reproduced on purpose and marked as
biased, which is the mistake in the pilot report. The pilot is re-analysed as ITT, CACE, per-goal results, and a
direct standardisation of the served group's goal mix.
