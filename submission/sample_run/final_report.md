# Final report: one user, one agent (replay day)

## Headline

* **2769 triggers -> 2781 decisions** (2769 trigger decisions + 12 re-plans after a title withdrawal). 934 published, 1847 abstained with a recorded reason.
* **Actions:** 933 home_page, 222 push, 19 in_app_message, 13 whatsapp. Home pages are the default; a message is sent only when the goal needs a return on a later day.
* **Safety:** the simulator reports **0 policy problems**; 0 actions on holdout users; every page accepted (933 accepted, 0 rows dropped).
* **Impact (ITT, primary goal success):** agent 17.2% (123/714) vs holdout 13.5% (96/710), **+3.7 pp [-0.0, +7.5], p=0.05**.
* **Seed sensitivity** (same actions, the simulator re-run with other seeds): `oua-practice` +3.7 pp [-0.0, +7.5], p=0.05; `seed-b` +4.0 pp [+0.3, +7.7], p=0.04; `seed-c` +2.5 pp [-1.1, +6.0], p=0.18; `seed-d` +0.9 pp [-2.8, +4.6], p=0.63. Read the effect as small and uncertain, not as a single number.
* **Pilot:** the "+22.4 pp, 2.3x" claim is selection bias. The randomized ITT is **-1.3 pp [-3.8, +1.2], p=0.30**. Do not roll out on that evidence.

## Why the agent abstained

| Reason | Decisions |
|---|---|
| `holdout_user` | 1042 |
| `stale_offline_buffered_trigger` | 424 |
| `personalised_page_already_live` | 147 |
| `app_dialect_unresolved` | 121 |
| `duplicate_event_id` | 90 |
| `app_dialect_missing_no_displayable_page` | 19 |
| `no_eligible_goal` | 4 |

## Goal mix (first committed goal; holdout goals are shadow goals)

| Goal | Agent | Holdout |
|---|---|---|
| G01 first_play_24h | 57 | 49 |
| G02 first_full_episode_3d | 167 | 155 |
| G03 second_day_return_2d | 1 | 0 |
| G04 trial_to_paid_7d | 1 | 1 |
| G05 first_paid_7d | 51 | 50 |
| G06 reactivate_lapsed_14d | 14 | 14 |
| G07 resume_series_3d | 289 | 312 |
| G08 try_new_release_7d | 78 | 77 |
| G09 weekly_habit_14d | 17 | 10 |
| G12 return_after_absence_7d | 39 | 42 |

Excluded as unmeasurable: **G10** (success.event is null ("engages meaningfully") - no event can confirm it); **G11** (success.event "tv_app_open" is not in the event schema; it can never be observed)

## Data traps found and how they were handled

| Trap | Found | Handling |
|---|---|---|
| Duplicate event deliveries | 2176 in history, 90 triggers | dedupe by event_id; a duplicate trigger gets a `duplicate_event_id` decision and no action |
| Offline-buffered triggers | 432 triggers (sent_at - occurred_at > 600 s) | abstain `stale_offline_buffered_trigger`; event still added to history at its estimated true time |
| Phone clock skew | 78 triggers (|received_at - sent_at| > 600 s) | age measured as sent - occurred (skew cancels); server received_at used for all timing |
| Unknown app dialect (d7 / None / "NA") | 185 triggers | "d7"/"NA" (reported values, no catalogue titles): abstain `app_dialect_unresolved`. None (missing value): fall back to the last app setting or the profile when they agree, message-only, never a page (no page can match a session without a dialect) |
| Dialect disagreement (profile / deeplink vs app) | 1052 decisions logged a conflict | app setting wins; conflicts recorded |
| Duplicate profile rows | 30 users | keep latest updated_at; the dropped rows are logged in data_audit.json |
| Users without a profile | 50 users in history, 103 triggered users | synthesised from signup_completed + events; consent fails closed |
| Prompt injection in catalogue | T0002, T0522 | quarantined: never recommended; instructions not executed (no message without consent, eligibility by evidence) |
| Malformed deeplinks | 96 (incl. history) | parsed strictly as untrusted data; used only as a conflict signal |
| Title withdrawn mid-day | T0001 | 12 live pages re-published without it; pending messages naming it cancelled |
| Titles expiring during the day / not yet available | see catalogue | a title must be available for the whole 2 h page TTL (and at a message send_at) |
| Goal rule corrected (G03) | 0 G03 goals open at the correction (re-pointed); 1 committed after it | rule_version v1 -> v1.1; open goals re-pointed, new goals use the new rule; the judge applies the rule in force |
| Live purchases / subscription changes | 5 goal closures, 1 messages cancelled | state updated on arrival; achieved/obsolete goals closed; pending messages cancelled |
| Model faults | 5 scheduled | timeout / malformed / unsupported claim -> deterministic rules (holdout shadow goals use the same advisor). Every fault trigger carries a `model_fault` record; the 2 on stale triggers are logged as not exercised |
| Model provider outage | 17 decisions fell back | HTTP 503 -> rules; circuit breaker skips the model for 30 s |
| User store degraded | 10 reads timed out | 800 ms read timeout -> cached state; push/WhatsApp fail closed (consent not verifiable) |

## Latency

* All decisions: p50 142 ms, p95 393 ms, p99 417 ms, max 1509 ms; **0 over 60 s**.
* Home pages published before the viewer reaches the home page (< 3 s): 933 of 933.
* `dependency_unavailable:model_provider`: 22 decisions, p95 82 ms, max 85 ms.
* `dependency_degraded:user_data_store`: 16 decisions, p95 1171 ms, max 1174 ms.
* `traffic_peak_2026-10-01T04:45`: 228 decisions, p95 24 ms, max 357 ms.
* Max model calls in one decision: 1 (budget 8); max tool calls: 9 (budget 20).

## Files

`decisions.jsonl`, `actions.jsonl`, `revisions.jsonl` (cancellations, closures, corrections), `viewer_state/` (one file per viewer), `outcomes.jsonl`, `impact_report.md`, `latency_report.json`, `data_audit.json`, `sim/` (simulator output).
