"""Judges every committed goal (agent and holdout shadow goals alike) from the simulator's outcome_events.jsonl.

succeeded    a success event inside [goal start, deadline] under the goal's (possibly corrected) rule
failed       the whole window is observed and no success event happened
inconclusive the window is not fully observed, the goal became obsolete, or the user has no outcome data
"""
import json
import os
from collections import defaultdict
from datetime import timedelta

from .fact_resolver import IST, iso, parse_ts
from .goal_planner import success_evidence

HORIZON_DAYS = 14


def load_outcome_events(path):
    by_user = defaultdict(list)
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                by_user[e['user_id']].append(e)
    for v in by_user.values():
        v.sort(key=lambda e: e['received_at'])
    return by_user


def horizon_end(triggers):
    first = min(parse_ts(t['deliver_at']) for t in triggers).astimezone(IST)
    day0 = first.replace(hour=0, minute=0, second=0, microsecond=0)
    return day0 + timedelta(days=1 + HORIZON_DAYS)       # replay day + the simulator's 14 days


def judge(store, pack, outcome_path, record_learnings=True):
    events = load_outcome_events(outcome_path)
    end = horizon_end(pack.triggers)
    out = []
    for uid in sorted(store.users):
        st = store.users[uid]
        for i, g in enumerate(st['goal_history']):
            row = {'user_id': uid, 'arm': st['arm'], 'decision_id': g.get('decision_id'), 'goal_id': g['goal_id'],
                   'goal': g['id'], 'primary': i == 0, 'shadow': g.get('shadow', False),
                   'start': g['start'], 'deadline': g['deadline'], 'success_signal': g['success_signal'],
                   'rule_version': g['rule_version'], 'revised': bool(g['revisions']),
                   'goal_state_at_replay_end': g['status']}
            evs = events.get(uid)
            ev = success_evidence(g, evs or [], pack.catalogue)
            if ev:
                row.update(status='succeeded', evidence=ev, reason=None)
            elif g['status'] == 'obsolete':
                row.update(status='inconclusive', evidence=[], reason='goal_obsolete_subscription_changed')
            elif evs is None:
                row.update(status='inconclusive', evidence=[], reason='no_outcome_data_for_user')
            elif parse_ts(g['deadline']) > end:
                row.update(status='inconclusive', evidence=[], reason='window_extends_past_observed_horizon')
            else:
                row.update(status='failed', evidence=[], reason='no_success_event_in_window')
            out.append(row)
            if record_learnings and not g.get('shadow'):
                st['learnings'].append({'at': iso(end), 'learning': 'goal %s %s' % (g['goal_id'], row['status'])})
        for e in events.get(uid, []) if record_learnings else []:
            if e['event'] == 'message_failed' and e['properties'].get('reason') == 'token_invalid':
                st['learnings'].append({'at': e['received_at'], 'learning': 'push token invalid: prefer home page / '
                                        'WhatsApp for this viewer'})
    return out


def write_outcomes(rows, out_dir):
    with open(os.path.join(out_dir, 'outcomes.jsonl'), 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
