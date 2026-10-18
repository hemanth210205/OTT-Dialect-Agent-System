# Known limitations

These are things we scoped out on purpose, or know to be imperfect. Each one says why.

## Measurement

- **The effect is small and noisy.** About 700 viewers per arm have a goal. The minimum detectable effect is about
  5 pp. Across four simulator seeds, the primary uplift ranged from +1.1 to +4.1 pp, and only one seed's interval
  excluded zero. We report it as "small, positive, uncertain". The per-goal cells are underpowered and have no
  multiplicity correction; they are descriptive.
- **Shadow goals and a real model.** Holdout viewers' shadow goals go through the same advisor as agent viewers', so
  goal assignment follows one procedure in both arms. With a real, non-deterministic model, the arms are comparable
  only in distribution, not user by user. The goal-agnostic ITT metrics in the impact report don't depend on goal
  logic at all.
- **One G07 threshold was set after a practice run.** The first run let "sampled episode 1 a week ago" count as an
  unfinished series. That made G07 60% of all goals and sent 306 resume pushes. We tightened it to require investment
  (reached episode 2, or completed an episode). The product argument stands on its own: a single sampled episode is a
  bounce. But the change was made after seeing outcomes, so treat the practice-seed numbers as tuned. The evaluators'
  unseen day and seed are the honest test.
- **Only 30 days of history.** "Never played", "never completed" and "never paid" mean never *in the pack*. Those
  assumptions are written into each decision.

## Agent behaviour

- **Unknown app dialect.** 185 triggers carry `d7`, `None` or `"NA"`.
  - `d7` and `"NA"`: the agent abstains. The simulator sets the viewer's dialect to that string, so no page and no
    message title can ever match.
  - `None`: the agent falls back to the last app setting or the profile, but only for a message, since no page can
    match a session without a dialect. In practice this produced 1 extra push. Most of these viewers have no goal
    that needs a message, and 19 more `None` triggers abstain as `app_dialect_missing_no_displayable_page`.
  - A `"NA"` viewer with a consistent valid history is probably a telemetry bug. We still abstain, because the
    platform shows pages for the reported value.
- **Two scheduled model faults never fire.** They are on offline-buffered triggers, which are abstained before any
  model call. Both are logged with `model_fault.exercised: false`. The timeout (on a holdout viewer), the malformed
  output and the U-01991 unsupported claim are exercised in the replay. All three fault kinds are also unit-tested.
- **Scheduled messages can be cancelled only until the replay ends.** Messages are scheduled for 19:00 IST the next
  day. Live events during the replay day cancel them (purchase, opt-out, dialect change, withdrawal). After the replay
  window, the simulator has no cancel API, so there is no re-check at send time. In production, the verifier would
  run again at `send_at`.
- **Fixed goal priority, no learning across viewers.** The priority order is a written product judgement, not
  learned. There is no bandit or uplift model; that would be over-engineering for one replay day with this much
  noise.
- **Simple ranking.** Popularity is distinct players over 30 days, with no recency decay and no collaborative
  filtering. This is deliberately not a recommender project. The simulator also picks the played title independently
  of page content, so ranking quality is hard to measure here at all.
- **WhatsApp `dialect_name`** is filled with the catalogue code (`d5`). The pack has no human-readable dialect names,
  and the template forbids free text.
- **`wa_trial_ending_v1` is never used.** None of the 4 trial viewers has a `plan_ends_at`, so "your trial is ending
  soon" cannot be backed by evidence.
- **`wa_new_episode_v2` is never used.** Episodes have no release dates, so "Naya episode aa gaya" cannot be shown to
  be true.
- **In-app messages** are used only when there is no channel consent. The simulator queues them and they have no
  effect.
- **Consent is strict.** Any historical `notification_opt_out` blocks that channel for good, because the schema has
  no opt-in event. This is safe but may under-message.

## Engineering

- **Latency is modelled, not real.** Compute time is real wall-clock time, in-process. The user store and model
  latencies are simulated deterministically (seeded by trigger id), and queueing uses a virtual 4-worker pool. The
  numbers are honest for this design, but they are not a load test.
- **The state store is JSON files**, not a transactional database. That is enough for a single-process replay. A
  production version would need per-viewer locking or compare-and-swap.
- **Injection detection is pattern-based** and could miss a paraphrased attack. The main defence does not depend on
  detection: free text is never interpreted as instruction. Detection only drives quarantine and logging.
- **The degraded store has no "cache that differs from the store".** Both are the same process, so the stale-cache
  path only changes behaviour by failing consent-gated channels closed and lowering confidence. It does not hide
  recent live events.
