"""Loads and cleans the evidence pack.

* Deduplicates events by event_id (first delivery wins; replays are byte-identical in this pack).
* Deduplicates profile rows deterministically (latest updated_at, ties broken by row order).
* Scans every free-text field (catalogue title/description, search queries, deeplinks) for prompt injection.
  Text is data, never instruction: flagged titles are quarantined from recommendations and logged.
"""
import csv
import json
import os
import re
from collections import Counter, defaultdict

from .fact_resolver import parse_ts, parse_deeplink_dialect

INJECTION_PATTERNS = [
    (r'\bignore\b.{0,40}\b(rules|instructions|previous|policy|policies|checks|guidelines)\b', 'ignore_rules'),
    (r'\b(system|developer|assistant|admin)\s*(note|instruction|prompt|message|override)\b', 'role_marker'),
    (r'\bfor (ai|llm)s?\b|\b(ai|llm) (agents?|assistants?|models?)\b', 'addresses_ai'),
    (r'\bskip (all |the |every )?(checks|verification|validation|rules)\b', 'skip_checks'),
    (r'\bpre-?approved\b', 'claims_preapproval'),
    (r'\bmark (every|all)\b', 'bulk_eligibility'),
    (r'\b(send|message|notify|push|whatsapp)\b.{0,80}\b(every|all) (users?|viewers?|subscribers?)\b', 'bulk_messaging'),
    (r'\bincluding (users )?(who )?(have )?opted[- ]out\b', 'consent_bypass'),
    (r'\b(disregard|override)\b.{0,40}\b(rules|instructions|policy|consent)\b', 'override'),
]
_COMPILED = [(re.compile(p, re.I), name) for p, name in INJECTION_PATTERNS]
DEEPLINK_RE = re.compile(r'^ott://c/[A-Za-z0-9_-]+/d[0-9]+/title/T[0-9]{4}$')


def scan_injection(text):
    """Returns the names of injection patterns found in untrusted text."""
    if not text:
        return []
    return [name for rx, name in _COMPILED if rx.search(text)]


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def dedupe_events(events):
    seen, out, dups = set(), [], 0
    for e in events:
        if e['event_id'] in seen:
            dups += 1
            continue
        seen.add(e['event_id'])
        out.append(e)
    return out, dups


class Pack:
    """Everything the agent is allowed to know. Built once; read-only afterwards (except replay updates)."""

    def __init__(self, root):
        self.root = root
        self.audit = {}
        self._load_config()
        self._load_catalogue()
        self._load_profiles()
        self._load_history()
        self._load_replay()

    def path(self, *p):
        return os.path.join(self.root, *p)

    def _load_config(self):
        with open(self.path('config/goals.json'), encoding='utf-8') as f:
            self.goals = json.load(f)
        with open(self.path('config/interventions.json'), encoding='utf-8') as f:
            self.interventions = json.load(f)
        with open(self.path('config/pricing.json'), encoding='utf-8') as f:
            self.pricing = json.load(f)
        self.allowed_prices = {p['price_inr'] for p in self.pricing['plans']}

    def _load_catalogue(self):
        self.catalogue, self.slug_titles, self.injections = {}, defaultdict(list), []
        with open(self.path('data/catalogue.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                tid = r['title_id']
                self.catalogue[tid] = {
                    'title_id': tid, 'slug': r['content_slug'], 'title': r['title'], 'dialect': r['dialect'],
                    'language': r['language'], 'type': r['content_type'],
                    'genres': [g for g in r['genres'].split('|') if g], 'release_date': r['release_date'],
                    'from': parse_ts(r['available_from']),
                    'until': parse_ts(r['available_until']) if r['available_until'] else None,
                    'premium': r['is_premium'].strip().lower() == 'true', 'description': r['description']}
                self.slug_titles[r['content_slug']].append(tid)
                for field in ('title', 'description'):
                    hits = scan_injection(r[field])
                    if hits:
                        self.injections.append({'source': 'catalogue', 'title_id': tid, 'field': field,
                                                'patterns': hits, 'excerpt': r[field][:240]})
        self.quarantined_titles = {i['title_id'] for i in self.injections}
        self.valid_dialects = {t['dialect'] for t in self.catalogue.values()}
        self.episodes = defaultdict(list)
        with open(self.path('data/episodes.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.episodes[r['content_slug']].append((int(r['season']), int(r['episode']), r['episode_id'],
                                                         float(r['minutes'] or 0)))
        self.episode_ordinal = {}
        for slug, eps in self.episodes.items():
            eps.sort()
            for i, ep in enumerate(eps, 1):
                self.episode_ordinal[ep[2]] = i
        self.audit['catalogue'] = {
            'titles': len(self.catalogue), 'content_slugs': len(self.slug_titles),
            'slugs_with_language_variants': sum(1 for v in self.slug_titles.values() if len(v) > 1),
            'titles_by_dialect': dict(sorted(Counter(t['dialect'] for t in self.catalogue.values()).items())),
            'injections': self.injections}

    def _load_profiles(self):
        rows = defaultdict(list)
        with open(self.path('data/profiles_snapshot.csv'), encoding='utf-8') as f:
            for i, r in enumerate(csv.DictReader(f)):
                rows[r['user_id']].append((r['updated_at'], i, r))
        self.profiles, self.profile_duplicates = {}, {}
        snaps = set()
        for uid, lst in rows.items():
            lst.sort(key=lambda x: (x[0], x[1]))
            self.profiles[uid] = dict(lst[-1][2])
            snaps.add(lst[-1][2]['snapshot_at'])
            if len(lst) > 1:
                self.profile_duplicates[uid] = {'rows': len(lst), 'kept_updated_at': lst[-1][0],
                                                'dropped_updated_at': [x[0] for x in lst[:-1]]}
        self.snapshot_at = parse_ts(max(snaps))
        self.audit['profiles'] = {'rows': sum(len(v) for v in rows.values()), 'unique_users': len(self.profiles),
                                  'users_with_duplicate_rows': len(self.profile_duplicates),
                                  'snapshot_at': max(snaps)}

    def _load_history(self):
        raw = list(read_jsonl(self.path('data/events.jsonl')))
        events, dups = dedupe_events(raw)
        self.history = defaultdict(list)
        stale = 0
        self.popularity = defaultdict(set)
        self.suspicious_inputs = []
        for e in events:
            self.history[e['user_id']].append(e)
            if (parse_ts(e['sent_at']) - parse_ts(e['occurred_at'])).total_seconds() > 600:
                stale += 1
            p = e.get('properties') or {}
            if e['event'] == 'play_started' and p.get('title_id'):
                self.popularity[p['title_id']].add(e['user_id'])
            self._scan_event_text(e)
        for lst in self.history.values():
            lst.sort(key=lambda x: x['received_at'])
        self.popularity = {k: len(v) for k, v in self.popularity.items()}
        users = set(self.history)
        self.audit['events'] = {'lines': len(raw), 'unique_event_ids': len(events), 'duplicate_copies_dropped': dups,
                                'offline_buffered_over_600s': stale, 'users': len(users),
                                'users_without_profile': len(users - set(self.profiles))}

    def _scan_event_text(self, e):
        p = e.get('properties') or {}
        if e['event'] == 'search':
            hits = scan_injection(p.get('query'))
            if hits:
                self.suspicious_inputs.append({'source': 'search', 'event_id': e['event_id'], 'patterns': hits})
        link = p.get('deeplink')
        if link is not None and not DEEPLINK_RE.match(str(link)):
            self.suspicious_inputs.append({'source': 'deeplink', 'event_id': e['event_id'], 'value': str(link)[:120],
                                           'patterns': ['malformed_deeplink']})
        elif link:
            d, _ = parse_deeplink_dialect(link)
            if d not in self.valid_dialects:
                self.suspicious_inputs.append({'source': 'deeplink', 'event_id': e['event_id'], 'value': link,
                                               'patterns': ['unknown_dialect']})

    def _load_replay(self):
        raw_t = list(read_jsonl(self.path('replay/triggers.jsonl')))
        self.triggers = raw_t                          # duplicates kept: the harness must prove idempotency
        self.live_events = list(read_jsonl(self.path('replay/live_events.jsonl')))
        self.control_events = list(read_jsonl(self.path('replay/control_events.jsonl')))
        self.model_faults = {f['trigger_event_id']: f for f in read_jsonl(self.path('replay/model_faults.jsonl'))}
        self.arms = {}
        with open(self.path('replay/arms.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.arms[r['user_id']] = r['arm']
        for e in raw_t + self.live_events:
            self._scan_event_text(e)
        _, tdups = dedupe_events(raw_t)
        stale = sum(1 for e in raw_t if (parse_ts(e['sent_at']) - parse_ts(e['occurred_at'])).total_seconds() > 600)
        skewed = sum(1 for e in raw_t if abs((parse_ts(e['received_at']) - parse_ts(e['sent_at'])).total_seconds()) > 600)
        self.audit['replay'] = {
            'triggers': len(raw_t), 'duplicate_trigger_copies': tdups, 'stale_triggers': stale,
            'clock_skewed_triggers': skewed,
            'triggers_with_unknown_dialect': sum(1 for e in raw_t if e.get('app_dialect') not in self.valid_dialects),
            'live_events': len(self.live_events), 'control_events': len(self.control_events),
            'model_faults': len(self.model_faults),
            'suspicious_inputs': len(self.suspicious_inputs)}

    def title_available(self, tid, at, withdrawn=None):
        t = self.catalogue.get(tid)
        if not t or t['from'] is None or t['from'] > at:
            return False
        if t['until'] is not None and at >= t['until']:
            return False
        if withdrawn and tid in withdrawn and at >= withdrawn[tid]:
            return False
        return True
