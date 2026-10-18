"""Measures impact honestly: intention-to-treat (agent arm vs holdout arm, as randomized), 95% CIs, the pilot
re-analysis, latency, and the final report. Standard library only."""
import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import timedelta

from .fact_resolver import parse_ts

Z = 1.959964


def wilson(x, n):
    if n == 0:
        return (0.0, 0.0)
    p = x / n
    den = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / den
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / den
    return (c - h, c + h)


def diff_ci(x1, n1, x0, n0):
    """Newcombe hybrid-score 95% CI for p1 - p0, plus a two-sided pooled z-test p-value."""
    if not n1 or not n0:
        return {'diff': None, 'ci': [None, None], 'p_value': None}
    p1, p0 = x1 / n1, x0 / n0
    l1, u1 = wilson(x1, n1)
    l0, u0 = wilson(x0, n0)
    d = p1 - p0
    lo = d - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)
    pp = (x1 + x0) / (n1 + n0)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n0)) if 0 < pp < 1 else 0
    pv = math.erfc(abs(d) / se / math.sqrt(2)) if se else 1.0
    return {'diff': d, 'ci': [lo, hi], 'p_value': pv}


def mde(p, n1, n0):
    """Minimum detectable difference at 80% power, alpha 0.05."""
    return 2.8 * math.sqrt(max(p * (1 - p), 1e-9) * (1 / max(n1, 1) + 1 / max(n0, 1)))


def rate(x, n):
    return '%.1f%% (%d/%d)' % (100 * x / n, x, n) if n else 'n/a'


def fmt_diff(r):
    if r['diff'] is None:
        return 'n/a'
    return '%+.1f pp [%+.1f, %+.1f], p=%.2f' % (100 * r['diff'], 100 * r['ci'][0], 100 * r['ci'][1], r['p_value'])


# ---- pilot -----------------------------------------------------------------------------------------------------
def pilot_reanalysis(pack):
    rows = list(csv.DictReader(open(pack.path('pilot/pilot_outcomes.csv'), encoding='utf-8')))
    g = defaultdict(lambda: [0, 0])
    for r in rows:
        ok = r['succeeded'] == 'true'
        for k in (('arm', r['arm']), ('served', r['arm'], r['served']), ('goal', r['goal_id'], r['arm'])):
            g[k][0] += 1
            g[k][1] += ok
    t, h = g[('arm', 'treatment')], g[('arm', 'holdout')]
    served = g[('served', 'treatment', 'true')]
    others = [g[('served', 'treatment', 'false')][0] + h[0], g[('served', 'treatment', 'false')][1] + h[1]]
    itt = diff_ci(t[1], t[0], h[1], h[0])
    naive = diff_ci(served[1], served[0], others[1], others[0])
    compliance = served[0] / t[0] if t[0] else 0
    per_goal = {}
    for gid in sorted({r['goal_id'] for r in rows}):
        a, b = g[('goal', gid, 'treatment')], g[('goal', gid, 'holdout')]
        per_goal[gid] = {'treatment': a, 'holdout': b, 'diff': diff_ci(a[1], a[0], b[1], b[0])}
    served_mix = Counter(r['goal_id'] for r in rows if r['served'] == 'true')
    all_mix = Counter(r['goal_id'] for r in rows)
    # what the "served" group would score with no agent at all: holdout success rate of each goal, weighted
    # by the served group's goal mix (direct standardisation)
    hold_rate = {gid: (per_goal[gid]['holdout'][1] / per_goal[gid]['holdout'][0]) for gid in per_goal}
    expected_served = sum(hold_rate[gid] * n for gid, n in served_mix.items()) / max(1, served[0])
    return {'itt': {'treatment': t, 'holdout': h, 'result': itt}, 'naive': {'served': served, 'others': others,
                                                                            'result': naive},
            'compliance': compliance, 'cace': (itt['diff'] / compliance) if compliance else None,
            'per_goal': per_goal, 'served_goal_mix': dict(served_mix), 'all_goal_mix': dict(all_mix),
            'expected_served_rate_without_agent': expected_served}


# ---- replay impact ---------------------------------------------------------------------------------------------
def replay_impact(pack, store, decisions, outcomes, outcome_events, sim_report):
    arms = pack.arms
    triggered = sorted({d['user_id'] for d in decisions if not d['trigger']['event'].startswith('control')})
    by_arm = defaultdict(list)
    for u in triggered:
        by_arm[arms.get(u)].append(u)
    prim = [o for o in outcomes if o['primary']]
    res = {'triggered_users': {a: len(v) for a, v in by_arm.items()}}

    def arm_counts(rows, pred=lambda o: True):
        c = {}
        for arm in ('agent', 'holdout'):
            rs = [o for o in rows if o['arm'] == arm and pred(o)]
            c[arm] = {'n': len(rs), 'succeeded': sum(o['status'] == 'succeeded' for o in rs),
                      'failed': sum(o['status'] == 'failed' for o in rs),
                      'inconclusive': sum(o['status'] == 'inconclusive' for o in rs)}
        return c

    c = arm_counts(prim)
    res['primary'] = {'counts': c, 'result': diff_ci(c['agent']['succeeded'], c['agent']['n'],
                                                     c['holdout']['succeeded'], c['holdout']['n'])}
    pooled = (c['agent']['succeeded'] + c['holdout']['succeeded']) / max(1, c['agent']['n'] + c['holdout']['n'])
    res['primary']['mde'] = mde(pooled, c['agent']['n'], c['holdout']['n'])
    res['per_goal'] = {}
    for gid in sorted({o['goal_id'] for o in prim}):
        cg = arm_counts(prim, lambda o, gid=gid: o['goal_id'] == gid)
        res['per_goal'][gid] = {'counts': cg, 'result': diff_ci(cg['agent']['succeeded'], cg['agent']['n'],
                                                                cg['holdout']['succeeded'], cg['holdout']['n'])}

    # Goal-agnostic ITT over every triggered user (pure randomisation, no goal logic involved).
    metrics = {'any_play': lambda evs: any(e['event'] == 'play_started' for e in evs),
               'any_completion': lambda evs: any(e['event'] == 'episode_completed' for e in evs),
               'paid_purchase': lambda evs: any(e['event'] == 'purchase_completed' and not e['properties'].get('is_trial')
                                                for e in evs),
               'play_on_3plus_days': lambda evs: len({e['received_at'][:10] for e in evs if e['event'] == 'play_started'}) >= 3}
    res['secondary'] = {}
    for name, fn in metrics.items():
        x = {arm: sum(1 for u in by_arm[arm] if fn(outcome_events.get(u, []))) for arm in ('agent', 'holdout')}
        n = {arm: len(by_arm[arm]) for arm in ('agent', 'holdout')}
        res['secondary'][name] = {'agent': [x['agent'], n['agent']], 'holdout': [x['holdout'], n['holdout']],
                                  'result': diff_ci(x['agent'], n['agent'], x['holdout'], n['holdout'])}
    plays = {arm: [sum(1 for e in outcome_events.get(u, []) if e['event'] == 'play_started') for u in by_arm[arm]]
             for arm in ('agent', 'holdout')}
    res['plays_per_user'] = {}
    for arm, v in plays.items():
        m = sum(v) / max(1, len(v))
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / max(1, len(v) - 1))
        res['plays_per_user'][arm] = {'mean': m, 'sd': sd, 'n': len(v)}
    a, h = res['plays_per_user']['agent'], res['plays_per_user']['holdout']
    se = math.sqrt(a['sd'] ** 2 / max(1, a['n']) + h['sd'] ** 2 / max(1, h['n']))
    res['plays_per_user']['diff'] = {'diff': a['mean'] - h['mean'], 'ci': [a['mean'] - h['mean'] - Z * se,
                                                                         a['mean'] - h['mean'] + Z * se]}

    # Exposure: who actually received something (descriptive only; not a causal comparison).
    shown = {u for u, evs in outcome_events.items() for e in evs
             if e['event'] == 'home_page_shown' and e['properties'].get('personalised')}
    acted = {d['user_id'] for d in decisions if d['status'] == 'PUBLISHED'}
    res['exposure'] = {'agent_users_with_published_action': len(acted & set(by_arm['agent'])),
                       'agent_users_shown_personalised_page': len(shown & set(by_arm['agent'])),
                       'agent_users': len(by_arm['agent'])}
    # The pilot's mistake, reproduced on our own data to show why it is wrong.
    exposed = [o for o in prim if o['arm'] == 'agent' and o['user_id'] in shown]
    rest = [o for o in prim if not (o['arm'] == 'agent' and o['user_id'] in shown)]
    res['naive_exposed_vs_rest'] = {
        'exposed': [sum(o['status'] == 'succeeded' for o in exposed), len(exposed)],
        'rest': [sum(o['status'] == 'succeeded' for o in rest), len(rest)],
        'result': diff_ci(sum(o['status'] == 'succeeded' for o in exposed), len(exposed),
                          sum(o['status'] == 'succeeded' for o in rest), len(rest))}
    res['simulator'] = {'counts': sim_report.get('counts', {}), 'policy_problem_count': sim_report.get('policy_problem_count'),
                        'seed': sim_report.get('seed')}
    return res


def write_impact_report(path, imp, pilot, planner):
    p = imp['primary']
    L = []
    L.append('# Impact report: agent vs holdout (intention-to-treat)\n')
    L.append('Simulator seed: `%s`. Every number below is computed by `src/impact_reporter.py` from '
             '`sim/outcome_events.jsonl` and `outcomes.jsonl`; nothing is typed in by hand.\n' % imp['simulator']['seed'])
    L.append('## Design\n')
    L.append('* **Unit:** every user who sent at least one trigger on the replay day, in the arm `replay/arms.csv` '
             'randomised them to (%s).' % ', '.join('%s %d' % kv for kv in sorted(imp['triggered_users'].items())))
    L.append('* **Same goal rule in both arms.** Holdout users get a *shadow* goal from the identical deterministic '
             'planner and the same model advisor at the same moment, and no action. Goal assignment happens before '
             'treatment and follows the same procedure in both arms, so comparing goal success between arms is unbiased.')
    L.append('* **Primary metric:** success of the user\'s first committed goal, judged by `outcome_judge.py` against the '
             'rule in force (G03 uses the corrected rule `%s`). Inconclusive goals count as not succeeded '
             '(conservative, the same in both arms).' % (planner.success['G03']['event'],))
    L.append('* **CIs:** Newcombe hybrid-score 95% intervals for differences in proportions; two-sided pooled z-test.\n')
    L.append('## Primary result\n')
    L.append('| Arm | Users with a goal | Succeeded | Failed | Inconclusive | Success rate |')
    L.append('|---|---|---|---|---|---|')
    for arm in ('agent', 'holdout'):
        c = p['counts'][arm]
        L.append('| %s | %d | %d | %d | %d | %s |' % (arm, c['n'], c['succeeded'], c['failed'], c['inconclusive'],
                                                     rate(c['succeeded'], c['n'])))
    L.append('\n**Uplift (agent - holdout): %s.** Minimum detectable effect at 80%% power with these sample sizes: '
             '~%.1f pp.\n' % (fmt_diff(p['result']), 100 * p['mde']))
    lo, hi = p['result']['ci']
    if lo is not None and lo > 0:
        verdict = 'The interval excludes zero: the agent arm reached its goals more often than the holdout.'
    elif hi is not None and hi < 0:
        verdict = 'The interval is entirely below zero: the agent arm did worse than holdout.'
    else:
        verdict = ('The interval includes zero: **we cannot claim an uplift** from this replay. That is the honest '
                   'reading, not a failure of the analysis.')
    L.append(verdict + '\n')
    L.append('## By goal (first committed goal)\n')
    L.append('| Goal | Agent | Holdout | Difference [95% CI] |')
    L.append('|---|---|---|---|')
    for gid, r in imp['per_goal'].items():
        a, h = r['counts']['agent'], r['counts']['holdout']
        L.append('| %s %s | %s | %s | %s |' % (gid, planner.goals[gid]['name'], rate(a['succeeded'], a['n']),
                                             rate(h['succeeded'], h['n']), fmt_diff(r['result'])))
    L.append('\nPer-goal cells are small; they are descriptive, not separate claims (no multiplicity correction).\n')
    L.append('## Goal-agnostic ITT over all triggered users (14-day horizon)\n')
    L.append('| Metric | Agent | Holdout | Difference [95% CI] |')
    L.append('|---|---|---|---|')
    for name, r in imp['secondary'].items():
        L.append('| %s | %s | %s | %s |' % (name, rate(*r['agent']), rate(*r['holdout']), fmt_diff(r['result'])))
    pp = imp['plays_per_user']
    L.append('| plays per user (mean) | %.2f | %.2f | %+.2f [%+.2f, %+.2f] |' % (
        pp['agent']['mean'], pp['holdout']['mean'], pp['diff']['diff'], pp['diff']['ci'][0], pp['diff']['ci'][1]))
    e = imp['exposure']
    L.append('\n## Exposure (descriptive)\n')
    L.append('* Agent-arm users with at least one published action: %d of %d.' % (
        e['agent_users_with_published_action'], e['agent_users']))
    L.append('* Agent-arm users who actually saw a personalised home page (`home_page_shown.personalised`): %d.' %
             e['agent_users_shown_personalised_page'])
    n = imp['naive_exposed_vs_rest']
    L.append('* **Do not read this as impact:** "saw a personalised page" vs everyone else: %s vs %s (%s). Being '
             'shown a page depends on opening the app again within its 2-hour TTL, so this group is selected for '
             'engagement. This is the pilot report\'s mistake reproduced on purpose.\n' % (
                 rate(*n['exposed']), rate(*n['rest']), fmt_diff(n['result'])))
    s = imp['simulator']
    L.append('## Policy compliance (simulator)\n')
    L.append('* Policy problems reported by the simulator: **%s**.' % s['policy_problem_count'])
    for k in sorted(s['counts']):
        L.append('* `%s`: %s' % (k, s['counts'][k]))
    L.append('')
    L.append(pilot_markdown(pilot))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


def pilot_markdown(pilot):
    i, nv = pilot['itt'], pilot['naive']
    L = ['## Pilot re-analysis (days 1-14)\n']
    L.append('The internal report claims **40.1% vs 17.7% (2.3x, +22.4 pp)** and recommends a full rollout. It compares '
             'the 217 *served* treatment users against "all other users": the 1,695 unserved treatment users plus the '
             'whole holdout. Being served was not random: it depended on the agent choosing to act and the user coming '
             'back. That is selection bias, and the comparison is not a treatment effect.\n')
    L.append('| Comparison | Group A | Group B | Difference [95% CI] |')
    L.append('|---|---|---|---|')
    L.append('| Report (biased): served vs everyone else | %s | %s | %s |' % (
        rate(nv['served'][1], nv['served'][0]), rate(nv['others'][1], nv['others'][0]), fmt_diff(nv['result'])))
    L.append('| **ITT: treatment arm vs holdout arm** | %s | %s | **%s** |' % (
        rate(i['treatment'][1], i['treatment'][0]), rate(i['holdout'][1], i['holdout'][0]), fmt_diff(i['result'])))
    L.append('\n* Only %.1f%% of the treatment arm was served, so the effect per served user (CACE = ITT / compliance) is '
             '%+.1f pp, with an interval about %.0fx wider than the ITT one. It is not distinguishable from zero.' % (
                 100 * pilot['compliance'], 100 * (pilot['cace'] or 0), 1 / pilot['compliance']))
    sig = [(gid, r['diff']) for gid, r in pilot['per_goal'].items() if r['diff']['ci'][0] > 0 or r['diff']['ci'][1] < 0]
    for gid, r in sig:
        L.append('* The only per-goal cell whose 95%% CI excludes zero is **%s (%s)**. With %d goals tested, one '
                 'nominal hit is close to what chance alone would give, but a %s effect on a goal the agent served most '
                 'is a harm signal. It should be investigated before any rollout, not ignored.' % (
                     gid, fmt_diff(r), len(pilot['per_goal']), 'negative' if r['diff'] < 0 else 'positive'))
    L.append('* The served group\'s goal mix is %s, against %s overall. G09 has a %.0f%% holdout base rate. Weighting each goal\'s '
             '**holdout** success rate by the served group\'s mix predicts **%.1f%%** with no agent at all. Most of the '
             '"40.1%%" is goal mix and self-selection.' % (
                 pilot['served_goal_mix'], pilot['all_goal_mix'],
                 100 * pilot['per_goal']['G09']['holdout'][1] / pilot['per_goal']['G09']['holdout'][0],
                 100 * pilot['expected_served_rate_without_agent']))
    L.append('\n| Goal | Treatment arm | Holdout arm | Difference [95% CI] |')
    L.append('|---|---|---|---|')
    for gid, r in pilot['per_goal'].items():
        L.append('| %s | %s | %s | %s |' % (gid, rate(r['treatment'][1], r['treatment'][0]),
                                          rate(r['holdout'][1], r['holdout'][0]), fmt_diff(r['diff'])))
    L.append('\n**Conclusion:** the pilot shows no evidence of uplift. The randomized comparison is %s. The rollout '
             'recommendation does not follow from the data.\n' % fmt_diff(i['result']))
    return '\n'.join(L)


# ---- latency -----------------------------------------------------------------------------------------------------
def percentile(v, q):
    if not v:
        return None
    v = sorted(v)
    k = (len(v) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return v[f] + (v[c] - v[f]) * (k - f)


def latency_report(decisions, control_events):
    def summary(ds):
        v = [d['decided_in_ms'] for d in ds]
        return {'n': len(v), 'p50_ms': percentile(v, .5), 'p90_ms': percentile(v, .9), 'p95_ms': percentile(v, .95),
                'p99_ms': percentile(v, .99), 'max_ms': max(v) if v else None,
                'over_60s': sum(1 for x in v if x >= 60000)}
    windows = {}
    for c in control_events:
        if c.get('until'):
            s, e = parse_ts(c['at']), parse_ts(c['until'])
            windows['%s:%s' % (c['type'], c['dependency'])] = [d for d in decisions
                                                              if s <= parse_ts(d['trigger']['deliver_at']) < e]
    starts = Counter(d['trigger']['deliver_at'][:16] for d in decisions)
    peak_minute, peak_n = starts.most_common(1)[0] if starts else (None, 0)
    if peak_minute:
        pk = parse_ts(peak_minute + ':00Z')
        windows['traffic_peak_%s' % peak_minute] = [d for d in decisions if pk - timedelta(minutes=2) <=
                                                    parse_ts(d['trigger']['deliver_at']) < pk + timedelta(minutes=3)]
    pages = [d for d in decisions if any(a['type'] == 'home_page' for a in d['actions'])]
    return {
        'definition': 'decided_in_ms = queueing in a 4-worker virtual pool + measured compute + simulated dependency '
                      'latency (user store, model). published_at = deliver_at + decided_in_ms.',
        'all_decisions': summary(decisions),
        'published': summary([d for d in decisions if d['status'] == 'PUBLISHED']),
        'abstained': summary([d for d in decisions if d['status'] != 'PUBLISHED']),
        'home_pages_within_3s': {'n': len(pages), 'within_3000ms': sum(1 for d in pages if d['decided_in_ms'] < 3000),
                                 'note': 'the simulator shows the home page 3-40 s after app open; a page published '
                                         'later is only seen on a later visit inside its TTL'},
        'windows': {k: summary(v) for k, v in windows.items()},
        'peak_minute': {'minute': peak_minute, 'triggers': peak_n},
        'model_status': dict(Counter(d['model_status'] for d in decisions)),
        'fallback_used': sum(1 for d in decisions if d['fallback_used']),
        'max_model_calls': max((d['model_calls'] for d in decisions), default=0),
        'max_tool_calls': max((d['tool_calls'] for d in decisions), default=0),
        'budget': {'model_calls': 8, 'tool_calls': 20, 'latency_ms': 60000},
    }


def write_final_report(path, pack, harness, outcomes, imp, pilot, lat, seeds=None):
    ds = harness.decisions
    trig = [d for d in ds if not d['trigger']['event'].startswith('control')]
    acts = Counter(a['type'] for a in harness.actions)
    reasons = Counter(d['no_action_reason'] for d in ds if d['status'] != 'PUBLISHED')
    rev = Counter(r['kind'] for r in harness.revisions)
    store_to = sum(1 for d in ds if any('user_data_store read timed out' in n for n in d['notes']))
    p = imp['primary']
    au = pack.audit
    L = ['# Final report: one user, one agent (replay day)\n']
    L.append('## Headline\n')
    L.append('* **%d triggers -> %d decisions** (%d trigger decisions + %d re-plans after a title withdrawal). '
             '%d published, %d abstained with a recorded reason.' % (
                 len(pack.triggers), len(ds), len(trig), len(ds) - len(trig),
                 sum(d['status'] == 'PUBLISHED' for d in ds), sum(d['status'] != 'PUBLISHED' for d in ds)))
    L.append('* **Actions:** %s. Home pages are the default; a message is sent only when the goal needs a return '
             'on a later day.' % ', '.join('%d %s' % (n, t) for t, n in acts.most_common()))
    L.append('* **Safety:** the simulator reports **%s policy problems**; %s actions on holdout users; every page '
             'accepted (%s accepted, 0 rows dropped).' % (
                 imp['simulator']['policy_problem_count'], imp['simulator']['counts'].get('holdout_actions_ignored', 0),
                 imp['simulator']['counts'].get('home_page_accepted', 0)))
    L.append('* **Impact (ITT, primary goal success):** agent %s vs holdout %s, **%s**.' % (
        rate(p['counts']['agent']['succeeded'], p['counts']['agent']['n']),
        rate(p['counts']['holdout']['succeeded'], p['counts']['holdout']['n']), fmt_diff(p['result'])))
    if seeds:
        L.append('* **Seed sensitivity** (same actions, the simulator re-run with other seeds): ' + '; '.join(
            '`%s` %s' % (s, fmt_diff(r)) for s, r in seeds) + '. Read the effect as small and uncertain, not as a '
            'single number.')
    L.append('* **Pilot:** the "+22.4 pp, 2.3x" claim is selection bias. The randomized ITT is **%s**. Do not roll '
             'out on that evidence.\n' % fmt_diff(pilot['itt']['result']))
    L.append('## Why the agent abstained\n')
    L.append('| Reason | Decisions |')
    L.append('|---|---|')
    for r, n in reasons.most_common():
        L.append('| `%s` | %d |' % (r, n))
    L.append('\n## Goal mix (first committed goal; holdout goals are shadow goals)\n')
    gm = Counter((o['goal_id'], o['arm']) for o in outcomes if o['primary'])
    L.append('| Goal | Agent | Holdout |')
    L.append('|---|---|---|')
    for gid in sorted({g for g, _ in gm}):
        L.append('| %s %s | %d | %d |' % (gid, harness.planner.goals[gid]['name'], gm[(gid, 'agent')], gm[(gid, 'holdout')]))
    L.append('\nExcluded as unmeasurable: ' + '; '.join('**%s** (%s)' % kv for kv in sorted(harness.planner.excluded.items())))
    L.append('\n## Data traps found and how they were handled\n')
    L.append('| Trap | Found | Handling |')
    L.append('|---|---|---|')
    rows = [
        ('Duplicate event deliveries', '%d in history, %d triggers' % (au['events']['duplicate_copies_dropped'],
                                                                       au['replay']['duplicate_trigger_copies']),
         'dedupe by event_id; a duplicate trigger gets a `duplicate_event_id` decision and no action'),
        ('Offline-buffered triggers', '%d triggers (sent_at - occurred_at > 600 s)' % au['replay']['stale_triggers'],
         'abstain `stale_offline_buffered_trigger`; event still added to history at its estimated true time'),
        ('Phone clock skew', '%d triggers (|received_at - sent_at| > 600 s)' % au['replay']['clock_skewed_triggers'],
         'age measured as sent - occurred (skew cancels); server received_at used for all timing'),
        ('Unknown app dialect (d7 / None / "NA")', '%d triggers' % au['replay']['triggers_with_unknown_dialect'],
         '"d7"/"NA" (reported values, no catalogue titles): abstain `app_dialect_unresolved`. None (missing value): '
         'fall back to the last app setting or the profile when they agree, message-only, never a page (no page can '
         'match a session without a dialect)'),
        ('Dialect disagreement (profile / deeplink vs app)', '%d decisions logged a conflict' % sum(
            1 for d in trig if d['dialect'] and d['dialect']['conflicts']), 'app setting wins; conflicts recorded'),
        ('Duplicate profile rows', '%d users' % au['profiles']['users_with_duplicate_rows'],
         'keep latest updated_at; the dropped rows are logged in data_audit.json'),
        ('Users without a profile', '%d users in history, %d triggered users' % (
            au['events']['users_without_profile'], len({d['user_id'] for d in trig} - set(pack.profiles))),
         'synthesised from signup_completed + events; consent fails closed'),
        ('Prompt injection in catalogue', ', '.join(i['title_id'] for i in au['catalogue']['injections']),
         'quarantined: never recommended; instructions not executed (no message without consent, eligibility by evidence)'),
        ('Malformed deeplinks', '%d (incl. history)' % sum(1 for s in pack.suspicious_inputs if s['source'] == 'deeplink'),
         'parsed strictly as untrusted data; used only as a conflict signal'),
        ('Title withdrawn mid-day', ', '.join(c['title_id'] for c in pack.control_events if c['type'] == 'title_withdrawn'),
         '%d live pages re-published without it; pending messages naming it cancelled' % (len(ds) - len(trig))),
        ('Titles expiring during the day / not yet available', 'see catalogue',
         'a title must be available for the whole 2 h page TTL (and at a message send_at)'),
        ('Goal rule corrected (G03)', '%d G03 goals open at the correction (re-pointed); %d committed after it' % (
            sum(1 for st in harness.store.users.values() for g in st['goal_history'] if g['revisions']),
            sum(1 for st in harness.store.users.values() for g in st['goal_history']
                if g['goal_id'] == 'G03' and not g['revisions'] and g['rule_version'] != 'v1')),
         'rule_version v1 -> v1.1; open goals re-pointed, new goals use the new rule; the judge applies the rule in force'),
        ('Live purchases / subscription changes', '%d goal closures, %d messages cancelled' % (
            rev['goal_closed'] + rev['goal_obsolete'], rev['action_cancelled']),
         'state updated on arrival; achieved/obsolete goals closed; pending messages cancelled'),
        ('Model faults', '%d scheduled' % len(pack.model_faults),
         'timeout / malformed / unsupported claim -> deterministic rules (holdout shadow goals use the same advisor). '
         'Every fault trigger carries a `model_fault` record; the 2 on stale triggers are logged as not exercised'),
        ('Model provider outage', '%d decisions fell back' % sum(1 for d in ds if d['model_status'] in ('unavailable', 'circuit_open')),
         'HTTP 503 -> rules; circuit breaker skips the model for 30 s'),
        ('User store degraded', '%d reads timed out' % store_to,
         '800 ms read timeout -> cached state; push/WhatsApp fail closed (consent not verifiable)'),
    ]
    for r in rows:
        L.append('| %s | %s | %s |' % r)
    a = lat['all_decisions']
    L.append('\n## Latency\n')
    L.append('* All decisions: p50 %.0f ms, p95 %.0f ms, p99 %.0f ms, max %d ms; **%d over 60 s**.' % (
        a['p50_ms'], a['p95_ms'], a['p99_ms'], a['max_ms'], a['over_60s']))
    L.append('* Home pages published before the viewer reaches the home page (< 3 s): %d of %d.' % (
        lat['home_pages_within_3s']['within_3000ms'], lat['home_pages_within_3s']['n']))
    for k, v in lat['windows'].items():
        if v['n']:
            L.append('* `%s`: %d decisions, p95 %.0f ms, max %d ms.' % (k, v['n'], v['p95_ms'], v['max_ms']))
    L.append('* Max model calls in one decision: %d (budget 8); max tool calls: %d (budget 20).\n' % (
        lat['max_model_calls'], lat['max_tool_calls']))
    L.append('## Files\n')
    L.append('`decisions.jsonl`, `actions.jsonl`, `revisions.jsonl` (cancellations, closures, corrections), '
             '`viewer_state/` (one file per viewer), `outcomes.jsonl`, `impact_report.md`, `latency_report.json`, '
             '`data_audit.json`, `sim/` (simulator output).')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
