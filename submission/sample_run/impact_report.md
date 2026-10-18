# Impact report: agent vs holdout (intention-to-treat)

Simulator seed: `oua-practice`. Every number below is computed by `src/impact_reporter.py` from `sim/outcome_events.jsonl` and `outcomes.jsonl`; nothing is typed in by hand.

## Design

* **Unit:** every user who sent at least one trigger on the replay day, in the arm `replay/arms.csv` randomised them to (agent 863, holdout 883).
* **Same goal rule in both arms.** Holdout users get a *shadow* goal from the identical deterministic planner and the same model advisor at the same moment, and no action. Goal assignment happens before treatment and follows the same procedure in both arms, so comparing goal success between arms is unbiased.
* **Primary metric:** success of the user's first committed goal, judged by `outcome_judge.py` against the rule in force (G03 uses the corrected rule `play_started`). Inconclusive goals count as not succeeded (conservative, the same in both arms).
* **CIs:** Newcombe hybrid-score 95% intervals for differences in proportions; two-sided pooled z-test.

## Primary result

| Arm | Users with a goal | Succeeded | Failed | Inconclusive | Success rate |
|---|---|---|---|---|---|
| agent | 714 | 123 | 591 | 0 | 17.2% (123/714) |
| holdout | 710 | 96 | 614 | 0 | 13.5% (96/710) |

**Uplift (agent - holdout): +3.7 pp [-0.0, +7.5], p=0.05.** Minimum detectable effect at 80% power with these sample sizes: ~5.4 pp.

The interval includes zero: **we cannot claim an uplift** from this replay. That is the honest reading, not a failure of the analysis.

## By goal (first committed goal)

| Goal | Agent | Holdout | Difference [95% CI] |
|---|---|---|---|
| G01 first_play_24h | 42.1% (24/57) | 30.6% (15/49) | +11.5 pp [-6.8, +28.5], p=0.22 |
| G02 first_full_episode_3d | 16.2% (27/167) | 12.3% (19/155) | +3.9 pp [-3.9, +11.5], p=0.32 |
| G03 second_day_return_2d | 0.0% (0/1) | n/a | n/a |
| G04 trial_to_paid_7d | 0.0% (0/1) | 0.0% (0/1) | +0.0 pp [-79.3, +79.3], p=1.00 |
| G05 first_paid_7d | 5.9% (3/51) | 4.0% (2/50) | +1.9 pp [-8.3, +12.3], p=0.66 |
| G06 reactivate_lapsed_14d | 7.1% (1/14) | 7.1% (1/14) | +0.0 pp [-25.0, +25.0], p=1.00 |
| G07 resume_series_3d | 4.8% (14/289) | 3.8% (12/312) | +1.0 pp [-2.4, +4.5], p=0.55 |
| G08 try_new_release_7d | 33.3% (26/78) | 29.9% (23/77) | +3.5 pp [-11.0, +17.7], p=0.64 |
| G09 weekly_habit_14d | 41.2% (7/17) | 40.0% (4/10) | +1.2 pp [-33.6, +33.7], p=0.95 |
| G12 return_after_absence_7d | 53.8% (21/39) | 47.6% (20/42) | +6.2 pp [-14.9, +26.6], p=0.58 |

Per-goal cells are small; they are descriptive, not separate claims (no multiplicity correction).

## Goal-agnostic ITT over all triggered users (14-day horizon)

| Metric | Agent | Holdout | Difference [95% CI] |
|---|---|---|---|
| any_play | 77.4% (668/863) | 75.8% (669/883) | +1.6 pp [-2.3, +5.6], p=0.42 |
| any_completion | 57.0% (492/863) | 56.9% (502/883) | +0.2 pp [-4.5, +4.8], p=0.95 |
| paid_purchase | 4.2% (36/863) | 4.1% (36/883) | +0.1 pp [-1.8, +2.0], p=0.92 |
| play_on_3plus_days | 41.6% (359/863) | 38.3% (338/883) | +3.3 pp [-1.3, +7.9], p=0.16 |
| plays per user (mean) | 3.07 | 2.92 | +0.15 [-0.15, +0.45] |

## Exposure (descriptive)

* Agent-arm users with at least one published action: 710 of 863.
* Agent-arm users who actually saw a personalised home page (`home_page_shown.personalised`): 624.
* **Do not read this as impact:** "saw a personalised page" vs everyone else: 18.1% (113/624) vs 13.2% (106/800) (+4.9 pp [+1.1, +8.7], p=0.01). Being shown a page depends on opening the app again within its 2-hour TTL, so this group is selected for engagement. This is the pilot report's mistake reproduced on purpose.

## Policy compliance (simulator)

* Policy problems reported by the simulator: **0**.
* `actions_total`: 1187
* `home_page_accepted`: 933
* `home_page_published_but_not_shown`: 15
* `home_page_shown_personalised`: 834
* `in_app_message_queued`: 19
* `in_app_message_requested`: 19
* `organic_purchases_scripted`: 36
* `push_clicked`: 4
* `push_delivered`: 196
* `push_requested`: 222
* `push_undeliverable_token`: 26
* `whatsapp_clicked`: 1
* `whatsapp_delivered`: 13
* `whatsapp_requested`: 13

## Pilot re-analysis (days 1-14)

The internal report claims **40.1% vs 17.7% (2.3x, +22.4 pp)** and recommends a full rollout. It compares the 217 *served* treatment users against "all other users": the 1,695 unserved treatment users plus the whole holdout. Being served was not random: it depended on the agent choosing to act and the user coming back. That is selection bias, and the comparison is not a treatment effect.

| Comparison | Group A | Group B | Difference [95% CI] |
|---|---|---|---|
| Report (biased): served vs everyone else | 40.1% (87/217) | 17.7% (649/3674) | +22.4 pp [+16.0, +29.2], p=0.00 |
| **ITT: treatment arm vs holdout arm** | 18.3% (349/1912) | 19.6% (387/1979) | **-1.3 pp [-3.8, +1.2], p=0.30** |

* Only 11.3% of the treatment arm was served, so the effect per served user (CACE = ITT / compliance) is -11.5 pp, with an interval about 9x wider than the ITT one. It is not distinguishable from zero.
* The only per-goal cell whose 95% CI excludes zero is **G09 (-6.4 pp [-12.5, -0.3], p=0.04)**. With 5 goals tested, one nominal hit is close to what chance alone would give, but a negative effect on a goal the agent served most is a harm signal. It should be investigated before any rollout, not ignored.
* The served group's goal mix is {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1}, against {'G05': 2118, 'G06': 331, 'G09': 953, 'G02': 360, 'G04': 129} overall. G09 has a 67% holdout base rate. Weighting each goal's **holdout** success rate by the served group's mix predicts **34.4%** with no agent at all. Most of the "40.1%" is goal mix and self-selection.

| Goal | Treatment arm | Holdout arm | Difference [95% CI] |
|---|---|---|---|
| G02 | 19.2% (34/177) | 19.7% (36/183) | -0.5 pp [-8.6, +7.8], p=0.91 |
| G04 | 23.8% (15/63) | 22.7% (15/66) | +1.1 pp [-13.4, +15.6], p=0.88 |
| G05 | 1.2% (13/1045) | 0.6% (6/1073) | +0.7 pp [-0.1, +1.6], p=0.09 |
| G06 | 0.6% (1/157) | 2.9% (5/174) | -2.2 pp [-5.9, +1.1], p=0.13 |
| G09 | 60.9% (286/470) | 67.3% (325/483) | -6.4 pp [-12.5, -0.3], p=0.04 |

**Conclusion:** the pilot shows no evidence of uplift. The randomized comparison is -1.3 pp [-3.8, +1.2], p=0.30. The rollout recommendation does not follow from the data.

