# AI collaboration

## How AI was used

This submission was built with an AI coding assistant (Claude, in Claude Code), with a human directing it and
reviewing the work. The AI:

- audited the pack
- read the simulator source to learn its exact contract
- drafted the modules, tests and documents
- ran the pipeline

The human set the direction: a deterministic core, honesty over uplift, NO_ACTION as a first-class decision. The
human also owned every judgement call recorded here.

## Verification workflow

1. **Audit before code.** Throwaway scripts counted every trap before any agent code existed: duplicate ids, stale
   and skewed triggers, invalid dialects, profile duplicates, missing profiles, injections, the pilot numbers. Those
   counts are re-derived by `data_loader.py` and written to `data_audit.json`, so the code and the audit can be
   compared.
2. **Read the simulator as the specification.** Its source showed what actually matters:
   - a page must land within about 3 s to be seen on the triggering visit
   - "stale" means `sent_at − occurred_at > 600 s`
   - pages only show in the app's dialect
   - quiet-hour and over-cap messages cause opt-outs
   - in-app messages do nothing
3. **No hand-typed numbers.** Every figure in `impact_report.md` and `final_report.md` is computed from the run's
   outputs by `impact_reporter.py`.
4. **Gates.** `pytest` (20 tests). The simulator must report `policy_problem_count == 0`, with no dropped rows and no
   rejected pages. The headline is shown next to three extra seeds, so a lucky seed cannot carry the result.

## Where we rejected or corrected AI or brief suggestions

**1. "One goal has no measurable success event."** The brief we started from said exactly one goal was
unmeasurable. The audit found two:

- G10 has `success.event: null`.
- G11's `tv_app_open` is not an event anywhere in the schema, so it can never be observed.

Selecting G11 would have produced goals that can only ever be "failed". Both are excluded, and the reason is written
down for each.

**2. Staleness rule.** The suggested rule was that a trigger is stale when `received_at − occurred_at` is large.
That treats phone clock skew as staleness. Measured on the replay triggers, it would have abstained on **23 fresh
triggers** from phones with wrong clocks, and acted on **2 stale ones** from phones whose clocks run behind. We use
`sent_at − occurred_at` instead, because skew shifts both phone timestamps equally and cancels out. It matches the
simulator's own definition.

**3. Over-messaging in the first G07 draft.** The first full run treated "played episode 1 of a series a week ago"
as an unfinished series. That made G07 60% of all goals and queued **306 resume pushes**. Every one was
policy-compliant, but most were not proportionate. We rejected that version: a resume goal now requires evidence
of investment (episode 2+ or a completed episode). We also say plainly in `KNOWN_LIMITATIONS.md` that this was
decided after a practice run.

**4. Bug caught by a test, in AI-written re-planning code.** When T0001 was withdrawn, the replacement page for one
viewer (U-01487) was built from that viewer's *latest* trigger. That trigger's dialect was `None`, so the agent
abstained and **left the old page live with the withdrawn title on it**. The withdrawal test failed. The replacement
is now built for the dialect of the page it replaces.

**5. Faults forced on purpose: rejected, then a review found a better design.** We rejected calling the model on
every trigger just to make all five faults fire. That would mean model calls on stale triggers, where there is no
decision to make.

A later review question pointed out a real problem: the first version skipped the model for holdout viewers. With a
real model, holdout shadow goals would then be chosen differently from agent goals, which biases the comparison.
Holdout viewers now go through the same advisor. That also exercises the timeout fault, which sits on a holdout
trigger. The two faults on stale triggers are still not exercised, and every fault trigger now carries an explicit
`model_fault` record.

**5b. Dialect fallback, checked against the simulator.** We first abstained on all 185 unknown-dialect triggers.
Reading `simulate_outcomes.py` showed the distinction. `"NA"` and `d7` become the viewer's dialect string, so nothing
can match. `None` keeps the earlier dialect or falls back to the profile, so a message can still be judged correct.
`None` now falls back for messages only, and pages are never published, because none can be shown.

**6. Verifier independence.** The first draft of the frequency-cap check read the planner's own state-store ledger.
A verifier that trusts the planner's bookkeeping is not independent. It now uses its own approval ledger plus
`message_delivered` events from the raw event log. Consent and holdout are re-derived from raw sources in the same
way.

## What the AI was not allowed to decide

- It could not change what counts as success to improve the numbers. The only change made after a run (item 3)
  made the agent send *fewer* messages, and it is disclosed.
- It could not treat model output as evidence.
- It could not send anything to an external service. The pack is treated as personal data (policy §7), and the model
  is a local mock.
