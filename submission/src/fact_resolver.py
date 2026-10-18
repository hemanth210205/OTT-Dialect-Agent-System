"""Timestamp math, freshness, precedence rules and conflict logging.

Every resolved fact is a dict: {"value", "source", "as_of", "freshness_s", "conflicts"}.

Time rules (README "Conventions"):
  * received_at is server time and trustworthy; occurred_at / sent_at come from the phone clock.
  * A phone with a wrong clock shifts occurred_at and sent_at by the same amount, so
    (sent_at - occurred_at) is the offline-buffering delay and is immune to clock skew.
  * Estimated true occurrence time = received_at - (sent_at - occurred_at).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
STALE_AFTER_S = 600          # a trigger buffered on the phone for >10 min is not a live visit
QUIET_START_H, QUIET_END_H = 22, 8
SUBSCRIPTION_EVENTS = ('purchase_completed', 'subscription_renewed', 'subscription_lapsed')


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if dt else None


def ist_day(dt):
    return dt.astimezone(IST).date()


def in_quiet_hours(dt):
    h = dt.astimezone(IST).hour
    return h >= QUIET_START_H or h < QUIET_END_H


def next_quiet_end(dt):
    """The first 08:00 IST at or after dt."""
    local = dt.astimezone(IST)
    eight = local.replace(hour=QUIET_END_H, minute=0, second=0, microsecond=0)
    if local > eight:
        eight += timedelta(days=1)
    return eight.astimezone(UTC)


def ist_at(day, hour, minute=0):
    """UTC datetime for an IST calendar day + local time."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def event_timing(e):
    """Buffering age (skew-proof), phone clock skew, and best estimate of when it really happened."""
    rec, sent, occ = parse_ts(e['received_at']), parse_ts(e.get('sent_at')), parse_ts(e.get('occurred_at'))
    age = (sent - occ).total_seconds() if sent and occ else 0.0
    skew = (rec - sent).total_seconds() if sent else 0.0
    return {'age_s': max(0.0, age), 'skew_s': skew, 'est_occurred_at': rec - timedelta(seconds=max(0.0, age)),
            'received_at': rec}


def fact(value, source, as_of, now, conflicts=None):
    return {'value': value, 'source': source, 'as_of': iso(as_of) if isinstance(as_of, datetime) else as_of,
            'freshness_s': int((now - as_of).total_seconds()) if isinstance(as_of, datetime) else None,
            'conflicts': conflicts or []}


def _bool(v):
    if v is None or v == '':
        return None
    return str(v).strip().lower() in ('true', '1', 'yes')


def parse_deeplink_dialect(link):
    """ott://c/<campaign>/<dialect>/title/<title_id> -> (dialect, title_id) or (None, None). Untrusted input."""
    if not isinstance(link, str):
        return None, None
    parts = link.split('/')
    if len(parts) == 7 and link.startswith('ott://c/') and parts[5] == 'title':
        return parts[4], parts[6]
    return None, None


class FactResolver:
    def __init__(self, pack):
        self.pack = pack

    def resolve(self, user_id, profile, events, trigger, now):
        """profile: deduped snapshot row or None. events: the user's deduped events known at `now`."""
        pack = self.pack
        snap = pack.snapshot_at
        conflicts_log = []
        timed = []
        for e in events:
            t = event_timing(e)
            if t['received_at'] <= now:
                timed.append((t['est_occurred_at'], t['received_at'], e))
        timed.sort(key=lambda x: (x[0], x[1]))

        facts = {}
        # signup_at: warehouse profile is the account system of record; else the signup event (server time).
        sig_events = [r for _, r, e in timed if e['event'] == 'signup_completed']
        sig_ev = min(sig_events) if sig_events else None
        if profile and profile.get('signup_at'):
            p_sig = parse_ts(profile['signup_at'])
            c = []
            if sig_ev and abs((sig_ev - p_sig).total_seconds()) > 86400:
                c = ['event:signup_completed@%s' % iso(sig_ev)]
            facts['signup_at'] = fact(iso(p_sig), 'profile_snapshot', snap, now, c)
        elif sig_ev:
            facts['signup_at'] = fact(iso(sig_ev), 'event:signup_completed', sig_ev, now)
        else:
            facts['signup_at'] = fact(None, 'unknown', now, now)

        # subscription: profile, overridden by any subscription event received after the snapshot.
        p_status = (profile or {}).get('subscription_status') or None
        status, src, as_of, c = p_status, 'profile_snapshot', snap, []
        paid_evidence = p_status in ('active', 'lapsed')
        last_lapse = None
        for est, rec, e in sorted(timed, key=lambda x: x[1]):
            ev = e['event']
            if ev not in SUBSCRIPTION_EVENTS:
                continue
            is_trial = bool(e['properties'].get('is_trial'))
            if ev in ('purchase_completed', 'subscription_renewed') and not is_trial:
                paid_evidence = True
            if ev == 'subscription_lapsed':
                last_lapse = est
            if rec > snap or profile is None:
                new = 'trial_active' if (ev == 'purchase_completed' and is_trial) else (
                    'lapsed' if ev == 'subscription_lapsed' else 'active')
                if status and new != status:
                    c.append('profile:%s' % p_status if src == 'profile_snapshot' else '%s:%s' % (src, status))
                status, src, as_of = new, 'event:%s:%s' % (ev, e['event_id']), rec
        if status is None:
            status, src, as_of = ('never', 'inferred:signup_without_purchase', sig_ev) if sig_ev else ('unknown', 'unknown', now)
        facts['subscription_status'] = fact(status, src, as_of, now, sorted(set(c)))
        facts['has_ever_paid'] = fact(paid_evidence, 'profile+events', now, now)
        facts['last_lapse_at'] = fact(iso(last_lapse), 'event:subscription_lapsed' if last_lapse else 'none', now, now)

        # consent: profile opt-in, and any opt-out event at any time wins (stricter of the two). Unknown -> False.
        for key, channel in (('notifications_opt_in', 'push'), ('whatsapp_opt_in', 'whatsapp')):
            pv = _bool((profile or {}).get(key))
            val, src, as_of, c = (pv if pv is not None else False), ('profile_snapshot' if pv is not None else 'missing:fail_closed'), snap, []
            for est, rec, e in timed:
                if e['event'] == 'notification_opt_out' and e['properties'].get('channel') == channel:
                    if val:
                        c.append('profile:true')
                    val, src, as_of = False, 'event:notification_opt_out:%s' % e['event_id'], rec
            facts[key] = fact(val, src, as_of, now, c)

        # dialect: the trigger's app setting wins (it decides what the app shows).
        valid = pack.valid_dialects
        app_d = trigger.get('app_dialect') if trigger else None
        trig_rec = parse_ts(trigger['received_at']) if trigger else now
        c = []
        p_d = (profile or {}).get('preferred_dialect') or None
        dl_d, _ = parse_deeplink_dialect((trigger or {}).get('properties', {}).get('deeplink'))
        last_change = None
        last_valid_app = None
        for est, rec, e in timed:
            if e['event'] == 'app_dialect_changed':
                last_change = e['properties'].get('to')
            if e.get('app_dialect') in valid and (not trigger or e['event_id'] != trigger['event_id']):
                last_valid_app = e['app_dialect']
        displayable = True
        if app_d in valid:
            used, src = app_d, 'app_setting'
        elif app_d is None and (last_valid_app or p_d in valid) and \
                not (last_valid_app and p_d in valid and last_valid_app != p_d):
            # A missing value is a telemetry gap, not a setting: fall back to the app settings feed, then the profile,
            # but only when they do not disagree. No page can be matched to a session that reports no dialect.
            used = last_valid_app or p_d
            src = 'fallback:last_app_setting' if last_valid_app else 'fallback:profile'
            displayable = False
            c.append('app_setting:None')
        else:
            # 'NA' / 'd7' are values the app actually reports; no catalogue title exists in them.
            used, src = None, 'unresolved'
            c.append('app_setting:%s' % app_d)
            if last_valid_app:
                c.append('last_known_app_setting:%s' % last_valid_app)
        if p_d and p_d != used:
            c.append('profile:%s' % p_d)
        if dl_d and dl_d != used:
            c.append('deeplink:%s' % dl_d)
        if last_change and last_change != used and last_change != app_d:
            c.append('dialect_changed_event:%s' % last_change)
        facts['dialect'] = fact(used, src, trig_rec, now, c)
        facts['dialect']['page_displayable'] = displayable
        facts['language'] = fact((trigger or {}).get('app_language') or (profile or {}).get('preferred_language') or 'hin',
                                 'app_setting' if (trigger or {}).get('app_language') else 'profile_snapshot', trig_rec, now)
        if c:
            conflicts_log.append({'fact': 'dialect', 'used': used, 'conflicts': c})

        facts['watch'] = self._watch_features(timed, now, trigger)
        facts['observed_since'] = fact(iso(timed[0][0]) if timed else None, 'events', now, now)
        return facts

    def _watch_features(self, timed, now, trigger):
        pack = self.pack
        trig_session = trigger.get('session_id') if trigger else None
        plays, completions = [], []
        slug_state = defaultdict(lambda: {'furthest': 0, 'last_play': None, 'plays': 0, 'title_id': None,
                                          'completed': 0})
        genres = defaultdict(int)
        last_open = None
        for est, rec, e in timed:
            ev, p = e['event'], e['properties']
            if ev in ('app_open', 'login_success') and e.get('session_id') != trig_session:
                last_open = est if last_open is None or est > last_open else last_open
            if ev not in ('play_started', 'episode_completed', 'play_progress'):
                continue
            t = pack.catalogue.get(p.get('title_id'))
            if not t:
                continue
            ordinal = pack.episode_ordinal.get(p.get('episode_id'), 1)
            s = slug_state[t['slug']]
            s['furthest'] = max(s['furthest'], ordinal)
            if ev == 'play_started':
                plays.append({'at': est, 'title_id': p.get('title_id'), 'slug': t['slug']})
                s['plays'] += 1
                s['title_id'] = p.get('title_id')
                s['last_play'] = est if s['last_play'] is None or est > s['last_play'] else s['last_play']
                for g in t['genres']:
                    genres[g] += 1
            elif ev == 'episode_completed':
                completions.append({'at': est, 'title_id': p.get('title_id'), 'slug': t['slug']})
                s['completed'] += 1
        play_days_14 = {ist_day(p['at']) for p in plays if now - timedelta(days=14) <= p['at'] < now}
        unfinished = []
        for slug, s in slug_state.items():
            t = pack.catalogue.get(s['title_id']) if s['title_id'] else None
            if not t or t['type'] not in ('show', 'micro_drama') or not s['last_play']:
                continue
            total = len(pack.episodes.get(slug, []))
            if s['furthest'] < total:
                unfinished.append({'slug': slug, 'title_id': s['title_id'], 'dialect': t['dialect'],
                                   'furthest_episode': s['furthest'], 'total_episodes': total,
                                   'episodes_completed': s['completed'],
                                   'last_play_at': iso(s['last_play']),
                                   'idle_h': round((now - s['last_play']).total_seconds() / 3600, 1)})
        unfinished.sort(key=lambda x: x['last_play_at'], reverse=True)
        return {
            'plays': len(plays), 'completions': len(completions),
            'played_slugs': sorted({p['slug'] for p in plays}),
            'recent_titles': [p['title_id'] for p in sorted(plays, key=lambda x: x['at'], reverse=True)[:5]],
            'play_days_last_14': len(play_days_14),
            'top_genre': max(sorted(genres), key=genres.get) if genres else None,
            'unfinished_series': unfinished,
            'last_app_open_at': iso(last_open),
        }
