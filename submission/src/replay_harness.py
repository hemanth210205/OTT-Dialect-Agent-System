"""Replays the day: merges triggers, live events and control events in delivery order and runs one agent decision
per trigger:  understand -> choose goal -> decide -> act within budget -> verify -> publish (or abstain).

Time model: the harness runs faster than real time. Each decision's processing time is the measured wall-clock
compute time plus the simulated latency of the dependencies it called (user store, model). Decisions are served
by a fixed worker pool in virtual time and a viewer's decisions never overlap, so a traffic spike shows up as
queueing delay. published_at = deliver_at + decided_in_ms, where decided_in_ms includes that queueing.
"""
import heapq
import json
import os
import time
from datetime import timedelta

from . import RULE_VERSION
from .fact_resolver import STALE_AFTER_S, FactResolver, event_timing, ist_day, iso, parse_ts
from .goal_planner import GoalPlanner, success_evidence
from .intervention_planner import InterventionPlanner
from .model_provider import MockModelProvider, ModelAdvisor
from .state_store import DependencyRegistry, StateStore, UserDataStoreClient, page_live, ttl_end
from .verifier import Verifier

MAX_MODEL_CALLS, MAX_TOOL_CALLS = 8, 20
N_WORKERS = 4
LOAD_SHED_QUEUE_MS = 10000       # under heavy queueing skip the optional model call
PAGE_REFRESH_MIN = 30            # republish a live page only if it has less than this left
TYPE_LABEL = {'home_page': 'HOME_PAGE', 'push': 'PUSH', 'whatsapp': 'WHATSAPP', 'in_app_message': 'IN_APP'}


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self):
        self.model_calls = 0
        self.tool_calls = 0

    def tool(self, n=1):
        self.tool_calls += n
        if self.tool_calls > MAX_TOOL_CALLS:
            raise BudgetExceeded('tool call budget %d exceeded' % MAX_TOOL_CALLS)


class ReplayHarness:
    def __init__(self, pack, n_workers=N_WORKERS, clock=time.perf_counter):
        self.pack, self.clock = pack, clock
        self.store = StateStore(pack)
        self.registry = DependencyRegistry()
        self.store_client = UserDataStoreClient(self.store, self.registry)
        self.resolver = FactResolver(pack)
        self.planner = GoalPlanner(pack.goals, pack.catalogue, RULE_VERSION)
        self.interventions = InterventionPlanner(pack)
        self.provider = MockModelProvider(pack.model_faults, self.registry)
        self.advisor = ModelAdvisor(self.provider, MAX_MODEL_CALLS)
        self.verifier = Verifier(pack, self.store, MAX_MODEL_CALLS, MAX_TOOL_CALLS)
        self.withdrawn = {}
        self.decisions, self.actions, self.revisions = [], [], []
        self.pending = {}
        self.n_workers = n_workers
        self.workers = None
        self.user_busy = {}
        self._aid = 0
        self.counters = {'live_duplicates_dropped': 0, 'live_events_applied': 0}

    # ---- timeline ----------------------------------------------------------------------------------------------
    def timeline(self):
        items = []
        for i, c in enumerate(self.pack.control_events):
            items.append((parse_ts(c['at']), 0, i, 'control', c))
        for i, e in enumerate(self.pack.live_events):
            items.append((parse_ts(e.get('deliver_at') or e['received_at']), 1, i, 'live', e))
        for i, t in enumerate(self.pack.triggers):
            items.append((parse_ts(t['deliver_at']), 2, i, 'trigger', t))
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        return items

    def run(self):
        items = self.timeline()
        if items:
            self.workers = [items[0][0]] * self.n_workers
            heapq.heapify(self.workers)
        for at, _, _, kind, obj in items:
            if kind == 'control':
                self.on_control(obj, at)
            elif kind == 'live':
                self.on_live(obj, at)
            else:
                self.decide(obj)
        for a in sorted(self.pending.values(), key=lambda x: x['action_id']):
            self.actions.append(a)
            self.store.get(a['user_id'])['published_actions'].append(
                {'action_id': a['action_id'], 'type': a['type'], 'send_at': a['send_at']})
        self.pending.clear()
        self.actions.sort(key=lambda a: int(a['action_id'][2:]))
        return self

    # ---- live events: update state, re-plan ----------------------------------------------------------------------
    def on_live(self, e, at):
        if not self.store.append_event(e):
            self.counters['live_duplicates_dropped'] += 1
            return
        self.counters['live_events_applied'] += 1
        uid = e['user_id']
        st = self.store.get(uid)
        goal = st['active_goal']
        if goal and goal['status'] == 'open':
            ev = success_evidence(goal, [e], self.pack.catalogue)
            if ev:
                goal['status'] = 'achieved_live'
                goal['closed_at'] = iso(at)
                goal['closed_by'] = e['event_id']
                self._revise(uid, at, 'goal_closed', '%s achieved by live %s %s; goal closed, pending messages cancelled'
                             % (goal['goal_id'], e['event'], e['event_id']))
                self._cancel_pending(uid, at, 'goal_achieved_before_send')
            elif e['event'] in ('purchase_completed', 'subscription_renewed', 'subscription_lapsed') \
                    and goal['goal_id'] in ('G04', 'G05', 'G06'):
                goal['status'] = 'obsolete'
                goal['closed_at'] = iso(at)
                self._revise(uid, at, 'goal_obsolete', 'subscription changed by %s; conversion goal no longer valid'
                             % e['event_id'])
                self._cancel_pending(uid, at, 'goal_obsolete')
        if e['event'] == 'notification_opt_out':
            ch = e['properties'].get('channel')
            self._cancel_pending(uid, at, 'consent_revoked:%s' % ch, lambda a: a['type'] == ch)
        if e['event'] == 'app_dialect_changed':
            to = e['properties'].get('to')
            self._cancel_pending(uid, at, 'dialect_changed:%s' % to,
                                 lambda a: self.pack.catalogue.get(a.get('title_id'), {}).get('dialect') != to)

    def _cancel_pending(self, uid, at, reason, pred=lambda a: True):
        for aid, a in list(self.pending.items()):
            if a['user_id'] == uid and pred(a) and parse_ts(a['send_at']) > at:
                del self.pending[aid]
                self.verifier.release(a)
                st = self.store.get(uid)
                day = ist_day(parse_ts(a['send_at'])).isoformat()
                ledger = st['message_ledger'] if a['type'] in ('push', 'whatsapp') else st['in_app_ledger']
                ledger[day] = ledger.get(day, 1) - 1
                st['cancelled_actions'].append({'action_id': aid, 'type': a['type'], 'at': iso(at), 'reason': reason})
                self._revise(uid, at, 'action_cancelled', '%s %s cancelled: %s' % (a['type'], aid, reason))

    def _revise(self, uid, at, kind, detail):
        r = {'at': iso(at), 'user_id': uid, 'kind': kind, 'detail': detail}
        self.revisions.append(r)
        if uid != '*':
            self.store.get(uid)['revisions'].append(r)

    # ---- control events ----------------------------------------------------------------------------------------
    def on_control(self, c, at):
        t = c['type']
        if t == 'title_withdrawn':
            tid = c['title_id']
            self.withdrawn[tid] = at
            self.verifier.on_control(c)
            self._revise('*', at, 'title_withdrawn', '%s withdrawn: %s' % (tid, c.get('reason')))
            for uid, st in sorted(self.store.users.items()):
                self._cancel_pending(uid, at, 'title_withdrawn:%s' % tid, lambda a: a.get('title_id') == tid)
                page = st['live_page']
                if page and page_live(page, at) and tid in page['title_ids'] and st['arm'] == 'agent':
                    # the replacement overrides that page, so it is built for that page's dialect
                    self.decide(dict(st['last_trigger'], app_dialect=page['dialect']),
                                replan_reason='title_withdrawn:%s' % tid, at_override=at)
        elif t == 'goal_rule_corrected':
            version = self.planner.apply_correction(c)
            n = 0
            for uid, st in sorted(self.store.users.items()):
                g = st['active_goal']
                if g and g['goal_id'] == c['goal_id'] and g['status'] == 'open':
                    if self.planner.revise_open_goal(g, at):
                        n += 1
                        st['learnings'].append({'at': iso(at), 'learning': '%s success rule corrected to %s (%s)'
                                                % (g['goal_id'], g['success_signal'], version)})
            self._revise('*', at, 'goal_rule_corrected', '%s -> %s; %d open goals re-pointed; rule_version %s'
                         % (c['goal_id'], c['new_success'], n, version))
        elif t in ('dependency_unavailable', 'dependency_degraded'):
            self.registry.add(c)
            self._revise('*', at, t, '%s until %s: %s' % (c['dependency'], c['until'], c.get('detail')))

    # ---- one decision ------------------------------------------------------------------------------------------
    def decide(self, trig, replan_reason=None, at_override=None):
        t0 = self.clock()
        pack = self.pack
        deliver = at_override or parse_ts(trig['deliver_at'])
        uid = trig['user_id']
        timing = event_timing(trig)
        budget, sim_ms, notes = Budget(), 0, []
        eid = trig['event_id'] if not replan_reason else 'replan:%s:%s' % (replan_reason, trig['event_id'])
        d = {
            'decision_id': 'D-%04d' % (len(self.decisions) + 1), 'viewer_id': uid, 'user_id': uid,
            'arm': pack.arms.get(uid),
            'trigger': {'event': trig['event'] if not replan_reason else 'control:' + replan_reason.split(':')[0],
                        'event_id': eid, 'source_event_id': trig['event_id'],
                        'received_at': trig['received_at'] if not replan_reason else iso(deliver),
                        'deliver_at': iso(deliver), 'event_age_s': int(timing['age_s']) if not replan_reason else 0,
                        'clock_skew_s': int(timing['skew_s'])},
            'goal': None, 'action': 'NO_ACTION', 'actions': [], 'no_action_reason': None,
            'dialect': None, 'content': [], 'confidence': None, 'confidence_reason': None,
            'evidence': [], 'assumptions': [], 'checks': {'passed': [], 'failed': []},
            'fallback_used': False, 'model_calls': 0, 'tool_calls': 0, 'model_status': 'not_called',
            'rule_version': self.planner.rule_version, 'decided_in_ms': None, 'published_at': None,
            'status': 'ABSTAINED', 'notes': notes,
        }
        st = None
        try:
            # 1. idempotency (before any state change)
            if not replan_reason:
                if eid in self.store.trigger_ids:
                    d['checks']['failed'].append('idempotency:duplicate_event_id')
                    return self._finish(d, t0, deliver, uid, 0, budget, abstain='duplicate_event_id')
                self.store.trigger_ids.add(eid)
                d['checks']['passed'].append('idempotency')
                self.store.append_event(trig)
            # 2. understand the viewer
            st, lat, store_timed_out = self.store_client.read(uid, deliver, eid)
            budget.tool()
            sim_ms += lat
            if store_timed_out:
                notes.append('user_data_store read timed out after %dms; cached state used, consent-gated '
                             'channels fail closed' % lat)
            if not replan_reason:
                st['processed_trigger_ids'].append(eid)
                st['last_trigger'] = trig
            facts = self.resolver.resolve(uid, st['profile'], self.store.events[uid], trig, deliver)
            budget.tool()
            st['facts'] = {k: v for k, v in facts.items() if k != 'watch'}
            st['facts']['watch_summary'] = {k: facts['watch'][k] for k in ('plays', 'completions', 'play_days_last_14',
                                                                           'top_genre', 'last_app_open_at')}
            dia = facts['dialect']
            d['dialect'] = {'used': dia['value'], 'source': dia['source'], 'conflicts': dia['conflicts']}
            d['evidence'].append('event:%s (freshness: %ds)' % (trig['event_id'], int((deliver - timing['received_at']).total_seconds())))
            if st['profile']:
                d['evidence'].append('profile:%s (freshness: %ds, updated_at %s)' % (
                    uid, int((deliver - pack.snapshot_at).total_seconds()), st['profile']['updated_at']))
            else:
                d['evidence'].append('profile:missing (%s)' % (st['profile_source'] or 'no signup event either'))
                d['assumptions'].append('no warehouse profile: consent treated as false (fail closed); '
                                        'subscription from events (%s)' % facts['subscription_status']['source'])
            if not replan_reason and timing['age_s'] > STALE_AFTER_S:
                d['checks']['failed'].append('freshness:buffered_%ds' % timing['age_s'])
                return self._finish(d, t0, deliver, uid, sim_ms, budget, abstain='stale_offline_buffered_trigger')
            d['checks']['passed'].append('freshness')
            if abs(timing['skew_s']) > STALE_AFTER_S:
                notes.append('phone clock skew %ds ignored: received_at used' % timing['skew_s'])
            if dia['value'] is None:
                d['checks']['failed'].append('dialect:unresolved(%s)' % trig.get('app_dialect'))
                return self._finish(d, t0, deliver, uid, sim_ms, budget, abstain='app_dialect_unresolved')
            d['checks']['passed'].append('dialect_resolved')

            # 3. one goal
            goal, new_goal, cand = st['active_goal'], False, None
            if goal and (goal['status'] != 'open' or parse_ts(goal['deadline']) <= deliver):
                goal = None
            if goal is None:
                eligible = self.planner.eligible(facts, deliver)
                budget.tool()
                if not eligible:
                    d['assumptions'].append('excluded as unmeasurable: %s' % ', '.join(sorted(self.planner.excluded)))
                    return self._finish(d, t0, deliver, uid, sim_ms, budget, abstain='no_eligible_goal')
                queue_ms = self._queue_estimate(deliver, uid)
                # Holdout viewers go through the same advisor: their shadow goal must be chosen exactly as an agent
                # viewer's would be, or goal assignment would differ by arm once a real model is plugged in.
                if queue_ms > LOAD_SHED_QUEUE_MS:
                    choice = {'goal_id': eligible[0]['goal_id'], 'fallback_used': True, 'model_calls': 0,
                              'latency_ms': 0, 'model_status': 'skipped_load_shedding', 'notes': []}
                else:
                    choice = self.advisor.choose_goal(eligible, trig['event_id'], deliver, budget)
                sim_ms += choice['latency_ms']
                d['fallback_used'] = choice['fallback_used']
                d['model_status'] = choice['model_status']
                notes.extend(choice['notes'])
                cand = next(c for c in eligible if c['goal_id'] == choice['goal_id'])
                new_goal = True
                goal_preview = {'goal_id': cand['goal_id'], 'target': cand['target']}
                d['assumptions'].extend(cand['assumptions'])
                d['evidence'].extend('goal_evidence:%s' % x for x in cand['evidence'])
            else:
                goal_preview = goal
                d['model_status'] = 'not_needed_goal_continuing'
                d['assumptions'].extend(goal['assumptions'])

            # 4. decide the intervention
            planned = []
            page_now = st['live_page']
            page_fresh = (page_now and page_live(page_now, deliver) and page_now['dialect'] == dia['value']
                          and ttl_end(page_now) - deliver >= timedelta(minutes=PAGE_REFRESH_MIN))
            if not dia['page_displayable']:
                notes.append('app reported no dialect: no page can be shown this session; message-only evaluation '
                             'in fallback dialect %s (%s)' % (dia['value'], dia['source']))
            elif page_fresh and not replan_reason:
                notes.append('personalised page %s still live for %s' % (page_now['action_id'], dia['value']))
            else:
                page, pnotes = self.interventions.build_home_page(facts, goal_preview, deliver, self.withdrawn)
                budget.tool()
                notes.extend(pnotes)
                if page:
                    planned.append(page)
            already_messaged = any(a['type'] != 'home_page' for a in st['published_actions']) or \
                any(a['user_id'] == uid for a in self.pending.values())
            if new_goal and not replan_reason and not already_messaged:
                msg, why = self.interventions.plan_message(facts, goal_preview, planned[0] if planned else None,
                                                           deliver, self.withdrawn, consent_fresh=not store_timed_out)
                budget.tool()
                notes.append('message: %s' % why)
                if msg:
                    planned.append(msg)

            # 5. timing, then independent verification of every planned action
            processing_ms = (self.clock() - t0) * 1000 + sim_ms
            queue_ms = self._queue_estimate(deliver, uid)
            decided_ms = int(round(queue_ms + processing_ms)) + 1   # +1 ms reserve for verify + publish
            d['decided_in_ms'] = decided_ms
            publish_at = deliver + timedelta(milliseconds=decided_ms)
            if new_goal:
                goal = self.planner.commit(cand, publish_at)
                goal['shadow'] = d['arm'] != 'agent'
                goal['decision_id'] = d['decision_id']
                st['active_goal'] = goal
                st['goal_history'].append(goal)
            d['goal'] = {'id': goal['id'], 'goal_id': goal['goal_id'], 'start': goal['start'],
                         'deadline': goal['deadline'], 'success_signal': goal['success_signal'],
                         'committed': 'new' if new_goal else 'continuing', 'shadow': goal.get('shadow', False)}
            ctx = {'user_id': uid, 'trigger_event_id': eid, 'publish_at': publish_at,
                   'shown_dialect': dia['value'], 'decided_in_ms': decided_ms, 'model_calls': budget.model_calls,
                   'tool_calls': budget.tool_calls, 'is_replan': bool(replan_reason)}
            approved, vetoes = [], []
            for p in planned:
                a = dict(p, user_id=uid, decision_id=d['decision_id'], trigger_event_id=eid,
                         published_at=iso(publish_at))
                budget.tool()
                res = self.verifier.verify(a, ctx)
                d['checks']['passed'].extend(x for x in res['passed'] if x not in d['checks']['passed'])
                d['checks']['failed'].extend(res['failed'])
                (approved if res['ok'] else vetoes).append((a, res))
            d['confidence'], d['confidence_reason'] = self._confidence(facts, goal, store_timed_out, d['fallback_used'], st)
            if not approved:
                if d['arm'] != 'agent':
                    reason = 'holdout_user'
                elif vetoes:
                    reason = 'verifier_veto:' + ';'.join(f for _, r in vetoes for f in r['failed'])
                elif not dia['page_displayable']:
                    reason = 'app_dialect_missing_no_displayable_page'
                elif page_fresh:
                    reason = 'personalised_page_already_live'
                else:
                    reason = 'no_valid_intervention'
                return self._finish(d, t0, deliver, uid, sim_ms, budget, abstain=reason)

            # 6. publish
            for a, _ in approved:
                budget.tool()
                self._publish(a, st, ctx)
                d['actions'].append({'action_id': a['action_id'], 'type': a['type'], 'send_at': a.get('send_at')})
                ids = ([a['hero_title_id']] if a.get('hero_title_id') else []) + \
                    [t for r in a.get('rows', []) for t in r['title_ids']] + ([a['title_id']] if a.get('title_id') else [])
                d['content'].extend('title:%s' % t for t in ids if 'title:%s' % t not in d['content'])
            d['evidence'].extend('catalogue:%s' % c.split(':', 1)[1] for c in d['content'][:3])
            for a, r in vetoes:
                notes.append('vetoed %s: %s' % (a['type'], r['failed']))
            d['action'] = TYPE_LABEL[approved[0][0]['type']]
            d['status'] = 'PUBLISHED'
            return self._finish(d, t0, deliver, uid, sim_ms, budget)
        except BudgetExceeded as ex:
            notes.append(str(ex))
            return self._finish(d, t0, deliver, uid, sim_ms, budget, abstain='budget_exceeded')

    def _publish(self, a, st, ctx):
        self._aid += 1
        a['action_id'] = 'A-%04d' % self._aid
        self.verifier.commit(a, ctx)
        if a['type'] == 'home_page':
            ids = ([a['hero_title_id']] if a.get('hero_title_id') else []) + [t for r in a['rows'] for t in r['title_ids']]
            st['live_page'] = {'action_id': a['action_id'], 'published_at': a['published_at'], 'dialect': a['dialect'],
                               'title_ids': ids, 'ttl_minutes': a.get('ttl_minutes', 120)}
            out = {k: a[k] for k in ('action_id', 'decision_id', 'user_id', 'trigger_event_id', 'type', 'published_at',
                                     'dialect', 'hero_title_id', 'rows')}
            self.actions.append(out)
            st['published_actions'].append({'action_id': a['action_id'], 'type': 'home_page',
                                            'published_at': a['published_at']})
        else:
            day = ist_day(parse_ts(a['send_at'])).isoformat()
            ledger = st['message_ledger'] if a['type'] in ('push', 'whatsapp') else st['in_app_ledger']
            ledger[day] = ledger.get(day, 0) + 1
            keys = ['action_id', 'decision_id', 'user_id', 'trigger_event_id', 'type', 'published_at', 'send_at']
            keys += {'push': ['title_id', 'text', 'deeplink'], 'in_app_message': ['title_id', 'text', 'deeplink'],
                     'whatsapp': ['template_id', 'variables', 'title_id']}[a['type']]
            self.pending[a['action_id']] = {k: a[k] for k in keys}

    def _queue_estimate(self, deliver, uid):
        free = self.workers[0] if self.workers else deliver
        start = max(deliver, free, self.user_busy.get(uid, deliver))
        return max(0.0, (start - deliver).total_seconds() * 1000)

    def _finish(self, d, t0, deliver, uid, sim_ms, budget, abstain=None):
        processing_ms = (self.clock() - t0) * 1000 + sim_ms
        queue_ms = self._queue_estimate(deliver, uid)
        if d['decided_in_ms'] is None:
            d['decided_in_ms'] = int(round(queue_ms + processing_ms))
        done = deliver + timedelta(milliseconds=d['decided_in_ms'])
        if self.workers:
            heapq.heapreplace(self.workers, max(done, self.workers[0]))
        self.user_busy[uid] = done
        d['published_at'] = iso(done)
        d['latency_breakdown_ms'] = {'queue': round(queue_ms, 1),
                                     'compute': round(d['decided_in_ms'] - queue_ms - sim_ms, 2),
                                     'dependencies_simulated': sim_ms}
        d['model_calls'], d['tool_calls'] = budget.model_calls, budget.tool_calls
        if abstain:
            d['action'], d['status'], d['no_action_reason'] = 'NO_ACTION', 'ABSTAINED', abstain
        if d['confidence'] is None:
            d['confidence'] = 1.0 if abstain in ('duplicate_event_id', 'holdout_user') else 0.9
            d['confidence_reason'] = 'confidence that abstaining is correct: %s' % abstain
        fault = self.pack.model_faults.get(d['trigger']['source_event_id'])
        if fault and not d['trigger']['event_id'].startswith('replan:'):
            exercised = d['model_calls'] > 0
            d['model_fault'] = {
                'scheduled': fault.get('fault'), 'exercised': exercised,
                'handling': ('model output rejected -> deterministic rule choice (%s)' % d['model_status']) if exercised
                else 'not exercised: decision ended before the model stage (%s); no model call was needed' % (
                    abstain or d['model_status'])}
        self.decisions.append(d)
        st = self.store.get(uid)
        st['decisions'].append({'decision_id': d['decision_id'], 'action': d['action'], 'status': d['status'],
                                'reason': d['no_action_reason'], 'at': d['published_at']})
        return d

    @staticmethod
    def _confidence(facts, goal, store_timed_out, fallback, st):
        c, why = 0.5, []
        w = facts['watch']
        if facts['subscription_status']['source'] != 'unknown' and st['profile']:
            c += 0.1
            why.append('profile present')
        else:
            why.append('no profile: facts from events only')
        if not facts['dialect']['conflicts']:
            c += 0.05
            why.append('no dialect conflict')
        else:
            why.append('dialect conflicts %s resolved to app setting' % facts['dialect']['conflicts'])
        if w['plays'] >= 3:
            c += 0.1
            why.append('%d plays observed' % w['plays'])
        else:
            why.append('thin watch history (%d plays)' % w['plays'])
        if goal['target']:
            c += 0.1
            why.append('goal has a concrete target %s' % goal['target'].get('content_slug'))
        if store_timed_out:
            c -= 0.2
            why.append('user store degraded: cached state')
        if fallback:
            c -= 0.05
            why.append('model unavailable or rejected: rule choice')
        c = round(max(0.05, min(0.95, c)), 2)
        return c, '; '.join(why)

    # ---- outputs -----------------------------------------------------------------------------------------------
    def write(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, 'decisions.jsonl'), 'w', encoding='utf-8') as f:
            for d in self.decisions:
                f.write(json.dumps(d, ensure_ascii=False) + '\n')
        with open(os.path.join(out_dir, 'actions.jsonl'), 'w', encoding='utf-8') as f:
            for a in self.actions:
                f.write(json.dumps(a, ensure_ascii=False) + '\n')
        with open(os.path.join(out_dir, 'revisions.jsonl'), 'w', encoding='utf-8') as f:
            for r in self.revisions:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        self.store.save(os.path.join(out_dir, 'viewer_state'))
        audit = dict(self.pack.audit)
        audit['goals_excluded_unmeasurable'] = self.planner.excluded
        audit['rule_corrections'] = self.planner.corrections
        audit['profile_duplicates'] = self.pack.profile_duplicates
        audit['suspicious_inputs_sample'] = self.pack.suspicious_inputs[:50]
        audit['replay_counters'] = self.counters
        with open(os.path.join(out_dir, 'data_audit.json'), 'w', encoding='utf-8') as f:
            json.dump(audit, f, indent=2, ensure_ascii=False, default=str)
