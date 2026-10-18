"""Chooses at most one evidence-backed goal per viewer from config/goals.json.

* A goal is selectable only if its success signal is an event that exists in the event schema. G10 (event null,
  "engages meaningfully") and G11 (tv_app_open, an event that is never emitted) are detected and excluded.
* Ambiguous rules get an explicit, versioned interpretation that is copied into each decision's `assumptions`.
* goal_rule_corrected control events bump the rule version and re-point open goals at the new success rule.
* Success matching lives here (not in the judge) so live re-planning and outcome judging share one definition.
"""
import copy
from datetime import timedelta

from .fact_resolver import ist_day, iso, parse_ts

SCHEMA_EVENTS = {
    'signup_completed', 'login_success', 'app_open', 'app_dialect_changed', 'home_viewed', 'title_viewed', 'search',
    'play_started', 'play_progress', 'episode_completed', 'paywall_viewed', 'purchase_completed',
    'subscription_renewed', 'subscription_lapsed', 'message_delivered', 'message_clicked', 'notification_opt_out',
    'home_page_shown', 'message_failed'}

# Most specific, evidence-backed need first. Conversion goals for "never" users come last: the pilot shows a ~1%
# base rate there, and picking an easy engagement goal just to inflate success would be dishonest the other way,
# so the order is fixed per user state, identical for agent and holdout (shadow) users.
PRIORITY = ['G04', 'G01', 'G03', 'G07', 'G12', 'G02', 'G09', 'G08', 'G06', 'G05']

INTERPRETATIONS = {
    'G01': "'Never started a play' is judged on the 30 days of observable history plus the replay feed.",
    'G02': "'Never completed an episode' is judged on the 30 days of observable history (older history is not in the pack).",
    'G03': "Signed-up time is the profile signup_at (system of record), else the server receipt time of signup_completed.",
    'G04': "Success is purchase_completed with is_trial=false; renewals are not conversions.",
    'G05': "'Never had a paid plan' = status 'never' AND no non-trial purchase/renewal anywhere in the events.",
    'G06': "AMBIGUOUS 'recently lapsed' interpreted as: status lapsed AND a subscription_lapsed event in the 30 days "
           "before the trigger (the only window we can observe). Lapsed users without an observed lapse are not targeted.",
    'G07': "AMBIGUOUS rule interpreted as: a show/micro_drama the viewer invested in (reached episode 2+ or completed "
           "an episode; sampling one episode and leaving is a bounce, not an unfinished series) whose furthest started "
           "episode is before its last, with no play_started of that content_slug in the 72h before the trigger "
           "(estimated true event time), in the dialect the app currently shows.",
    'G08': "'Recent release' = catalogue release_date within the 30 days before the goal start.",
    'G09': "Play days are IST calendar days with at least one play_started (estimated true event time).",
    'G12': "Absence needs 14 days of observability: signup at least 14 days before the trigger.",
}


class GoalPlanner:
    def __init__(self, goals_cfg, catalogue, rule_version='v1'):
        self.catalogue = catalogue
        self.rule_version = rule_version
        self.goals = {g['goal_id']: g for g in goals_cfg['goals']}
        self.success = {gid: copy.deepcopy(g['success']) for gid, g in self.goals.items()}
        self.excluded = {}
        for gid, g in self.goals.items():
            ok, why = self.measurable(g)
            if not ok:
                self.excluded[gid] = why
        self.corrections = []

    @staticmethod
    def measurable(goal):
        ev = goal['success'].get('event')
        if ev is None:
            return False, 'success.event is null ("%s") - no event can confirm it' % goal['success'].get('condition')
        if ev not in SCHEMA_EVENTS:
            return False, 'success.event "%s" is not in the event schema; it can never be observed' % ev
        return True, None

    def apply_correction(self, ctrl):
        gid = ctrl['goal_id']
        new = dict(ctrl['new_success'])
        new.setdefault('window_hours', self.success[gid].get('window_hours'))
        self.success[gid] = new
        major, minor = (self.rule_version.lstrip('v').split('.') + ['0'])[:2]
        self.rule_version = 'v%s.%d' % (major, int(minor) + 1)
        self.corrections.append({'at': ctrl['at'], 'goal_id': gid, 'new_success': new,
                                 'rule_version': self.rule_version, 'applies_to': ctrl.get('applies_to')})
        return self.rule_version

    # ---- eligibility -------------------------------------------------------------------------------------------
    def eligible(self, facts, now):
        out = []
        for gid in PRIORITY:
            if gid in self.excluded:
                continue
            res = getattr(self, '_elig_' + gid)(facts, now)
            if res:
                evidence, target = res
                out.append({'goal_id': gid, 'evidence': evidence, 'target': target,
                            'assumptions': [INTERPRETATIONS[gid]] if gid in INTERPRETATIONS else []})
        return out

    @staticmethod
    def _signup(facts):
        return parse_ts(facts['signup_at']['value']) if facts['signup_at']['value'] else None

    def _elig_G01(self, f, now):
        s = self._signup(f)
        if s and now - s <= timedelta(days=7) and f['watch']['plays'] == 0:
            return ['signup_at=%s (%s)' % (iso(s), f['signup_at']['source']), 'play_started observed: 0'], {}

    def _elig_G02(self, f, now):
        if f['watch']['completions'] == 0:
            return ['episode_completed observed: 0', 'play_started observed: %d' % f['watch']['plays']], {}

    def _elig_G03(self, f, now):
        s = self._signup(f)
        if s and now - s <= timedelta(days=2):
            return ['signup_at=%s (%s)' % (iso(s), f['signup_at']['source'])], {}

    def _status(self, f):
        return f['subscription_status']['value']

    def _elig_G04(self, f, now):
        if self._status(f) == 'trial_active':
            return ['subscription_status=trial_active (%s)' % f['subscription_status']['source']], {}

    def _elig_G05(self, f, now):
        if self._status(f) == 'never' and not f['has_ever_paid']['value']:
            return ['subscription_status=never (%s)' % f['subscription_status']['source'], 'no paid purchase observed'], {}

    def _elig_G06(self, f, now):
        lapse = parse_ts(f['last_lapse_at']['value']) if f['last_lapse_at']['value'] else None
        if self._status(f) == 'lapsed' and lapse and now - lapse <= timedelta(days=30):
            return ['subscription_status=lapsed', 'subscription_lapsed observed at %s' % iso(lapse)], {}

    def _elig_G07(self, f, now):
        dialect = f['dialect']['value']
        for s in f['watch']['unfinished_series']:
            invested = s['furthest_episode'] >= 2 or s['episodes_completed'] >= 1
            if s['dialect'] == dialect and s['idle_h'] >= 72 and invested:
                return (['series %s: furthest episode %d of %d, %d completed, idle %.0fh' % (
                    s['slug'], s['furthest_episode'], s['total_episodes'], s['episodes_completed'], s['idle_h'])],
                        {'content_slug': s['slug'], 'title_id': s['title_id'],
                         'furthest_episode': s['furthest_episode'], 'next_episode': s['furthest_episode'] + 1})

    def _elig_G08(self, f, now):
        if self._status(f) == 'active':
            return ['subscription_status=active (%s)' % f['subscription_status']['source']], {}

    def _elig_G09(self, f, now):
        if self._status(f) == 'active' and f['watch']['play_days_last_14'] <= 1:
            return ['subscription_status=active', 'play days in last 14: %d' % f['watch']['play_days_last_14']], {}

    def _elig_G12(self, f, now):
        s = self._signup(f)
        last = parse_ts(f['watch']['last_app_open_at']) if f['watch']['last_app_open_at'] else None
        if s and now - s >= timedelta(days=14) and (last is None or now - last >= timedelta(days=14)):
            return ['last app_open before trigger: %s' % (iso(last) or 'none in 30-day history'),
                    'signup_at=%s' % iso(s)], {}

    # ---- commitment --------------------------------------------------------------------------------------------
    def commit(self, candidate, start):
        gid = candidate['goal_id']
        g, rule = self.goals[gid], self.success[gid]
        deadline = start + timedelta(hours=rule['window_hours'])
        return {'id': g['name'], 'goal_id': gid, 'start': iso(start), 'deadline': iso(deadline),
                'success_signal': rule['event'], 'success_condition': rule['condition'],
                'rule_version': self.rule_version, 'target': candidate['target'], 'status': 'open',
                'evidence': candidate['evidence'], 'assumptions': candidate['assumptions'], 'revisions': []}

    def revise_open_goal(self, goal, at):
        """Re-point an open goal at the current success rule (a correction applies to open goals too)."""
        rule = self.success[goal['goal_id']]
        if goal['success_signal'] == rule['event'] and goal['success_condition'] == rule['condition']:
            return False
        goal['revisions'].append({'at': iso(at), 'from': {'event': goal['success_signal'],
                                                          'condition': goal['success_condition'],
                                                          'rule_version': goal['rule_version']},
                                  'to': {'event': rule['event'], 'condition': rule['condition'],
                                         'rule_version': self.rule_version}})
        goal['success_signal'], goal['success_condition'] = rule['event'], rule['condition']
        goal['rule_version'] = self.rule_version
        return True


def success_evidence(goal, events, catalogue):
    """Returns the event_ids that prove the goal succeeded inside its window, or None."""
    start, deadline = parse_ts(goal['start']), parse_ts(goal['deadline'])
    gid, sig = goal['goal_id'], goal['success_signal']
    win = [e for e in events if e['event'] == sig and start <= parse_ts(e['received_at']) <= deadline]
    if gid in ('G04', 'G05', 'G06'):
        hits = [e for e in win if not e['properties'].get('is_trial') and e['properties'].get('price_inr', 0) > 1]
        return [hits[0]['event_id']] if hits else None
    if gid == 'G03':
        hits = [e for e in win if ist_day(parse_ts(e['received_at'])) > ist_day(start)]
        return [hits[0]['event_id']] if hits else None
    if gid == 'G07':
        slug = goal['target'].get('content_slug')
        hits = [e for e in win if catalogue.get(e['properties'].get('title_id'), {}).get('slug') == slug]
        return [hits[0]['event_id']] if hits else None
    if gid == 'G08':
        lo = (ist_day(start) - timedelta(days=30)).isoformat()
        hi = ist_day(start).isoformat()
        hits = [e for e in win if lo <= catalogue.get(e['properties'].get('title_id'), {}).get('release_date', '') <= hi]
        return [hits[0]['event_id']] if hits else None
    if gid == 'G09':
        days = {}
        for e in win:
            days.setdefault(ist_day(parse_ts(e['received_at'])), e['event_id'])
        return list(days.values())[:3] if len(days) >= 3 else None
    return [win[0]['event_id']] if win else None
