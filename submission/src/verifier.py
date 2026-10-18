"""Independent verifier with veto power. Nothing is published unless every applicable check passes.

Independence: the verifier does not trust the planner's resolved facts. It re-derives holdout, consent and the
frequency ledger from raw sources (arms.csv, the raw profile row, the raw event log) and keeps its own ledgers
of what it has approved. It never repairs an action: it vetoes, and the decision records which check failed.
"""
import re
from collections import defaultdict
from datetime import timedelta

from .data_loader import scan_injection
from .fact_resolver import in_quiet_hours, ist_day, parse_ts

LATENCY_LIMIT_MS = 60000
PRICE_RE = re.compile(r'(?:₹|\brs\.?|\binr)\s*([0-9][0-9,]*)', re.I)
OFFER_RE = re.compile(r'\b(free|discount|offer|cashback|% off|off on|sale|deal)\b', re.I)
DEEPLINK_RE = re.compile(r'^ott://title/(T[0-9]{4})$')


class Verifier:
    def __init__(self, pack, store, max_model_calls=8, max_tool_calls=20):
        self.pack = pack
        self.store = store                       # read-only use of raw events and raw profiles
        self.cfg = pack.interventions
        self.max_model_calls, self.max_tool_calls = max_model_calls, max_tool_calls
        self.withdrawn = {}
        self.approved_triggers = set()           # idempotency ledger (own copy)
        self.msg_ledger = defaultdict(int)       # (user, ist_day) -> push+whatsapp approved
        self.in_app_ledger = defaultdict(int)
        self.templates = {t['template_id']: t for t in self.cfg['whatsapp']['approved_templates']}

    def on_control(self, ctrl):
        if ctrl['type'] == 'title_withdrawn':
            self.withdrawn[ctrl['title_id']] = parse_ts(ctrl['at'])

    def release(self, action):
        """A scheduled message that was cancelled before sending gives its cap slot back."""
        if action['type'] in ('push', 'whatsapp'):
            self.msg_ledger[(action['user_id'], ist_day(parse_ts(action['send_at'])))] -= 1
        elif action['type'] == 'in_app_message':
            self.in_app_ledger[(action['user_id'], ist_day(parse_ts(action['send_at'])))] -= 1

    # ---- raw re-derivations ----------------------------------------------------------------------------------
    def _consent(self, uid, channel, at):
        prof = self.pack.profiles.get(uid) or {}
        key = 'notifications_opt_in' if channel == 'push' else 'whatsapp_opt_in'
        if str(prof.get(key, '')).lower() != 'true':
            return False
        for e in self.store.events.get(uid, []):
            if e['event'] == 'notification_opt_out' and e['properties'].get('channel') == channel \
                    and parse_ts(e['received_at']) <= at:
                return False
        return True

    def _title_ok(self, tid, dialect, at):
        t = self.pack.catalogue.get(tid)
        return bool(t and t['dialect'] == dialect and self.pack.title_available(tid, at, self.withdrawn))

    # ---- main entry -------------------------------------------------------------------------------------------
    def verify(self, action, ctx):
        """ctx: user_id, trigger_event_id, publish_at (datetime), shown_dialect, decided_in_ms, model_calls,
        tool_calls, is_replan (bool)."""
        passed, failed = [], []

        def check(name, ok, detail=None):
            (passed if ok else failed).append(name if ok or not detail else '%s:%s' % (name, detail))

        uid = action.get('user_id')
        check('holdout', self.pack.arms.get(uid) == 'agent', 'user arm is %s' % self.pack.arms.get(uid))
        key = (ctx['trigger_event_id'], action.get('type'))
        check('idempotency', ctx.get('is_replan') or key not in self.approved_triggers, 'trigger already acted on')
        check('latency', ctx['decided_in_ms'] < LATENCY_LIMIT_MS, '%dms' % ctx['decided_in_ms'])
        check('budget', ctx['model_calls'] <= self.max_model_calls and ctx['tool_calls'] <= self.max_tool_calls)
        typ = action.get('type')
        if typ == 'home_page':
            self._verify_page(action, ctx, check)
        elif typ in ('push', 'whatsapp', 'in_app_message'):
            self._verify_message(action, ctx, check)
        else:
            check('schema', False, 'unknown action type %s' % typ)
        return {'passed': passed, 'failed': failed, 'ok': not failed}

    def commit(self, action, ctx):
        self.approved_triggers.add((ctx['trigger_event_id'], action['type']))
        if action['type'] in ('push', 'whatsapp'):
            self.msg_ledger[(action['user_id'], ist_day(parse_ts(action['send_at'])))] += 1
        elif action['type'] == 'in_app_message':
            self.in_app_ledger[(action['user_id'], ist_day(parse_ts(action['send_at'])))] += 1

    def _verify_page(self, a, ctx, check):
        rules = self.cfg['home_page']['rows']
        at, d = ctx['publish_at'], a.get('dialect')
        check('schema', all(k in a for k in ('dialect', 'rows', 'published_at')) and isinstance(a['rows'], list))
        check('dialect', d is not None and d == ctx['shown_dialect'] and d in self.pack.valid_dialects,
              'page %s vs app %s' % (d, ctx['shown_dialect']))
        rows = a.get('rows') or []
        check('row_bounds', len(rows) <= rules['max_rows'] and all(
            rules['min_titles_per_row'] <= len(r.get('title_ids') or []) <= rules['max_titles_per_row'] for r in rows),
            'rows=%d sizes=%s' % (len(rows), [len(r.get('title_ids') or []) for r in rows]))
        check('row_types', all(r.get('row_type') in rules['row_types'] for r in rows))
        check('no_continue_watching', not any('continue' in str(r.get('row_type', '')).lower() for r in rows))
        ids = ([a['hero_title_id']] if a.get('hero_title_id') else []) + [t for r in rows for t in r.get('title_ids') or []]
        check('hero', a.get('hero_title_id') is None or isinstance(a.get('hero_title_id'), str))
        check('content_non_empty', bool(ids))
        bad_dialect = [t for t in ids if self.pack.catalogue.get(t, {}).get('dialect') != d]
        check('title_dialect', not bad_dialect, ','.join(bad_dialect[:3]))
        unavailable = [t for t in ids if not self.pack.title_available(t, at)]
        check('availability', not unavailable, ','.join(unavailable[:3]))
        withdrawn = [t for t in ids if t in self.withdrawn and at >= self.withdrawn[t]]
        check('not_withdrawn', not withdrawn, ','.join(withdrawn))
        quarantined = [t for t in ids if t in self.pack.quarantined_titles]
        check('injection_quarantine', not quarantined, ','.join(quarantined))
        slugs = [self.pack.catalogue[t]['slug'] for t in ids if t in self.pack.catalogue]
        check('content_slug_dedup', len(slugs) == len(set(slugs)))

    def _verify_message(self, a, ctx, check):
        uid, typ = a['user_id'], a['type']
        send_at = parse_ts(a.get('send_at') or a.get('published_at'))
        check('schema', send_at is not None and bool(a.get('title_id')))
        if send_at is None:
            return
        check('send_after_publish', send_at >= ctx['publish_at'])
        tid = a.get('title_id')
        check('title_dialect', tid in self.pack.catalogue and self.pack.catalogue[tid]['dialect'] == ctx['shown_dialect'],
              '%s not in %s' % (tid, ctx['shown_dialect']))
        check('availability', self._title_ok(tid, ctx['shown_dialect'], send_at), 'at send_at')
        check('not_withdrawn', tid not in self.withdrawn)
        check('injection_quarantine', tid not in self.pack.quarantined_titles)
        day = ist_day(send_at)
        if typ in ('push', 'whatsapp'):
            check('consent', self._consent(uid, typ, ctx['publish_at']), '%s consent missing or revoked' % typ)
            check('quiet_hours', not in_quiet_hours(send_at), 'send_at in 22:00-08:00 IST')
            check('frequency_cap', self.msg_ledger[(uid, day)] + self._delivered_on(uid, day) < 1,
                  'already 1 push/whatsapp on %s' % day)
        else:
            check('in_app_daily_cap', self.in_app_ledger[(uid, day)] < self.cfg['in_app_message']['max_per_day'])
        if typ in ('push', 'in_app_message'):
            text = a.get('text') or {}
            copy = self.cfg['push']['copy']
            title, body = text.get('title', ''), text.get('body', '')
            check('copy_length', 0 < len(title) <= copy['title_max_chars'] and 0 < len(body) <= copy['body_max_chars'],
                  'title %d body %d' % (len(title), len(body)))
            low = (title + ' ' + body).lower()
            hits = [p for p in copy['forbidden_phrases'] if p in low]
            check('forbidden_phrases', not hits, ','.join(hits))
            self._price_check(title + ' ' + body, check)
            check('copy_not_instruction', not scan_injection(title + ' ' + body))
            m = DEEPLINK_RE.match(a.get('deeplink') or '')
            check('deeplink_format', bool(m) and m.group(1) == tid, a.get('deeplink'))
        if typ == 'whatsapp':
            tpl = self.templates.get(a.get('template_id'))
            check('template_approved', tpl is not None, a.get('template_id'))
            if tpl:
                v = a.get('variables') or {}
                check('template_variables', set(v) == set(tpl['variables']), sorted(v))
                check('variables_from_catalogue_or_pricing', self._vars_ok(v, tid))

    def _delivered_on(self, uid, day):
        """Messages the platform already delivered that IST day (raw event log), whoever sent them."""
        return sum(1 for e in self.store.events.get(uid, [])
                   if e['event'] == 'message_delivered' and e['properties'].get('channel') in ('push', 'whatsapp')
                   and ist_day(parse_ts(e['received_at'])) == day)

    def _price_check(self, text, check):
        prices = [int(p.replace(',', '')) for p in PRICE_RE.findall(text)]
        check('pricing_honesty', all(p in self.pack.allowed_prices for p in prices) and not OFFER_RE.search(text),
              'prices %s' % prices)

    def _vars_ok(self, v, tid):
        t = self.pack.catalogue.get(tid, {})
        for k, val in v.items():
            if k == 'title_name' and val != t.get('title'):
                return False
            if k == 'dialect_name' and val != t.get('dialect'):
                return False
            if k == 'episode_number' and not (str(val).isdigit() and 1 <= int(val) <= len(self.pack.episodes.get(t.get('slug'), []))):
                return False
            if k == 'plan_price':
                m = PRICE_RE.fullmatch(str(val).strip())
                if not m or int(m.group(1).replace(',', '')) not in self.pack.allowed_prices:
                    return False
        return True
