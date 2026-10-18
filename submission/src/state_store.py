"""Durable per-user agent state, plus a client that models the user_data_store dependency.

One JSON document per viewer (viewer_state/<user_id>.json) holds: resolved facts with provenance, the active goal,
goal history, pending (scheduled, cancellable) messages, the live home page, the per-IST-day message ledger,
processed trigger ids (idempotency) and learnings. Raw events are not duplicated into the state file: they are
re-derivable from the pack and the replay feed, so the file keeps only a summary of them.
"""
import hashlib
import json
import os
from collections import defaultdict
from datetime import timedelta

from .fact_resolver import iso, parse_ts


def h01(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


def new_state(user_id, arm, profile):
    return {
        'user_id': user_id, 'arm': arm,
        'profile': profile, 'profile_source': 'profiles_snapshot' if profile else None,
        'facts': {}, 'active_goal': None, 'goal_history': [],
        'pending_actions': [], 'published_actions': [], 'cancelled_actions': [],
        'live_page': None, 'message_ledger': {}, 'in_app_ledger': {},
        'processed_trigger_ids': [], 'decisions': [], 'learnings': [], 'revisions': [],
    }


class StateStore:
    def __init__(self, pack):
        self.pack = pack
        self.users = {}
        self.events = defaultdict(list)          # user_id -> deduped events known so far (history + replay)
        self.event_ids = set()
        self.trigger_ids = set()                 # idempotency ledger across the whole replay
        for uid, evs in pack.history.items():
            self.events[uid] = list(evs)
            self.event_ids.update(e['event_id'] for e in evs)

    def get(self, uid):
        st = self.users.get(uid)
        if st is None:
            st = new_state(uid, self.pack.arms.get(uid), self.pack.profiles.get(uid))
            self.users[uid] = st
        return st

    def append_event(self, e):
        """Adds an event to the user's log. Returns False for a duplicate delivery."""
        if e['event_id'] in self.event_ids:
            return False
        self.event_ids.add(e['event_id'])
        self.events[e['user_id']].append(e)
        st = self.get(e['user_id'])
        if e['event'] == 'signup_completed' and st['profile'] is None and st['profile_source'] is None:
            st['profile_source'] = 'synthesized_from_signup_event:%s' % e['event_id']
        return True

    def messages_on(self, uid, day):
        return self.get(uid)['message_ledger'].get(day.isoformat(), 0)

    def save(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        for uid, st in sorted(self.users.items()):
            doc = dict(st)
            evs = self.events.get(uid, [])
            doc['event_summary'] = {'events_known': len(evs),
                                    'by_type': dict(sorted(_count(e['event'] for e in evs).items()))}
            with open(os.path.join(out_dir, '%s.json' % uid), 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=1, ensure_ascii=False, default=str)


def _count(it):
    c = defaultdict(int)
    for x in it:
        c[x] += 1
    return c


class DependencyRegistry:
    """Outage / degradation windows announced by control_events.jsonl."""

    def __init__(self):
        self.windows = []                     # (dependency, kind, start, end, detail)

    def add(self, ctrl):
        self.windows.append((ctrl['dependency'], ctrl['type'], parse_ts(ctrl['at']), parse_ts(ctrl['until']),
                             ctrl.get('detail')))

    def state(self, dependency, at):
        for dep, kind, start, end, _ in self.windows:
            if dep == dependency and start <= at < end:
                return kind
        return 'ok'


class UserDataStoreClient:
    """Reads a viewer's state with a hard timeout. Latency is simulated deterministically (no sleeping) and
    added to the decision's measured processing time.

    Normal: 4-25 ms. Degraded (control event): 20% of reads time out; the rest have a heavy-tailed latency with
    p95 around 8 s. Our client gives up after READ_TIMEOUT_MS and the caller falls back to the cached copy, marked
    stale, which fails closed for anything that needs fresh consent."""
    READ_TIMEOUT_MS = 800

    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    def read(self, uid, at, key):
        kind = self.registry.state('user_data_store', at)
        if kind == 'dependency_unavailable':
            return self.store.get(uid), self.READ_TIMEOUT_MS, True
        if kind == 'dependency_degraded':
            u = h01('store', key)
            latency = 400 + 7600 * h01('store-lat', key) ** 3
            if u < 0.20 or latency > self.READ_TIMEOUT_MS:
                return self.store.get(uid), self.READ_TIMEOUT_MS, True
            return self.store.get(uid), int(latency), False
        return self.store.get(uid), int(4 + 21 * h01('store-lat', key)), False


def ttl_end(page):
    return parse_ts(page['published_at']) + timedelta(minutes=page.get('ttl_minutes', 120))


def page_live(page, at):
    return page is not None and parse_ts(page['published_at']) <= at < ttl_end(page)


__all__ = ['StateStore', 'UserDataStoreClient', 'DependencyRegistry', 'page_live', 'ttl_end', 'h01', 'iso']
