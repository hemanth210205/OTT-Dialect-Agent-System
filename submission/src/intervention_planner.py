"""Builds the smallest intervention that serves the goal: a home page first, a message only when the goal needs the
viewer to come back later, NO_ACTION otherwise. Everything it emits is re-checked by the independent verifier.

Title text from the catalogue is untrusted data: it is only ever copied into fixed slots, after sanitising, and
titles whose metadata carries an injection attempt are never recommended.
"""
import re
from datetime import timedelta

from .fact_resolver import ist_at, ist_day, iso, parse_ts

PAGE_TTL_MIN = 120
MAX_ROWS = 5                     # platform max is 6; a 6th row adds little and dilutes the page
ROW_ORDER = {
    'G07': ['next_episode', 'because_you_watched', 'popular_in_dialect', 'new_releases', 'genre'],
    'G08': ['new_releases', 'because_you_watched', 'genre', 'popular_in_dialect', 'next_episode'],
    'G09': ['because_you_watched', 'next_episode', 'popular_in_dialect', 'new_releases', 'genre'],
    'default': ['popular_in_dialect', 'because_you_watched', 'new_releases', 'genre', 'next_episode'],
}
# Goals whose success needs a visit on a later day: a single reminder the next evening is proportionate.
REMINDER_GOALS = {'G03', 'G07', 'G09'}
REMINDER_HOUR_IST = 19
MAX_GENRE_ROWS = 2
SAFE_TEXT = re.compile(r"[^\w\s'\-:.,!?&]", re.UNICODE)


def clean_title(name, limit):
    s = SAFE_TEXT.sub('', name or '').strip()
    return s if len(s) <= limit else s[:limit].rstrip()


class InterventionPlanner:
    def __init__(self, pack):
        self.pack = pack
        self.cfg = pack.interventions

    # ---- catalogue -----------------------------------------------------------------------------------------------
    def pool(self, dialect, language, now, withdrawn):
        """One title_id per content_slug: in dialect, available now and for the whole page TTL, not withdrawn, not
        quarantined. Language variant matching the app language preferred."""
        pack, end = self.pack, now + timedelta(minutes=PAGE_TTL_MIN)
        by_slug = {}
        for tid, t in pack.catalogue.items():
            if t['dialect'] != dialect or tid in pack.quarantined_titles:
                continue
            if not pack.title_available(tid, now, withdrawn) or not pack.title_available(tid, end, withdrawn):
                continue
            cur = by_slug.get(t['slug'])
            if cur is None or (t['language'] == language and pack.catalogue[cur]['language'] != language):
                by_slug[t['slug']] = tid
        return by_slug

    def _dialect_genres(self, titles):
        score = {}
        for t in titles:
            for g in self.pack.catalogue[t]['genres']:
                score[g] = score.get(g, 0) + self.pack.popularity.get(t, 0)
        return sorted(score, key=lambda g: (-score[g], g))

    def _popular(self, tids):
        pop = self.pack.popularity
        return sorted(tids, key=lambda t: (-pop.get(t, 0), t))

    # ---- home page -----------------------------------------------------------------------------------------------
    def build_home_page(self, facts, goal, now, withdrawn):
        dialect, language = facts['dialect']['value'], facts['language']['value']
        cat = self.pack.catalogue
        pool = self.pool(dialect, language, now, withdrawn)
        if not pool:
            return None, ['no_available_titles_in_dialect']
        watch = facts['watch']
        watched = set(watch['played_slugs'])
        everything = self._popular(pool.values())
        unwatched = [t for t in everything if cat[t]['slug'] not in watched]
        gid = goal['goal_id'] if goal else None
        today = ist_day(now)
        recent_lo = (today - timedelta(days=30)).isoformat()

        cands = {
            'next_episode': [pool[s['slug']] for s in watch['unfinished_series'] if s['slug'] in pool],
            'popular_in_dialect': unwatched or everything,
            'new_releases': [t for t in unwatched if recent_lo <= cat[t]['release_date'] <= today.isoformat()],
        }
        recent = [cat[t] for t in watch['recent_titles'] if t in cat and cat[t]['dialect'] == dialect]
        if recent:
            seed = set(recent[0]['genres'])
            cands['because_you_watched'] = [t for t in unwatched if seed & set(cat[t]['genres'])]
        # Genre rows: the viewer's top genre first, then the dialect's most-watched genres.
        genre_order = ([watch['top_genre']] if watch['top_genre'] else []) + self._dialect_genres(everything)
        genres = []
        for g in genre_order:
            if g not in genres:
                genres.append(g)
        genre_rows = [(g, [t for t in unwatched if g in cat[t]['genres']]) for g in genres[:MAX_GENRE_ROWS]]

        # Hero: the goal's own target first, else the best title for the goal.
        hero = None
        target = (goal or {}).get('target', {})
        if target.get('content_slug') in pool:
            hero = pool[target['content_slug']]
        elif gid == 'G08' and cands['new_releases']:
            hero = cands['new_releases'][0]
        elif gid in ('G04', 'G05', 'G06'):
            hero = next((t for t in unwatched if cat[t]['premium']), None)
        if hero is None and everything:
            hero = (unwatched or everything)[0]

        used = {cat[hero]['slug']} if hero else set()
        per_row = max(3, min(8, (len(pool) - 1) // 5))
        plan = []
        for rtype in ROW_ORDER.get(gid, ROW_ORDER['default']):
            if rtype == 'genre':
                plan.extend(('genre', g, lst) for g, lst in genre_rows)
            else:
                plan.append((rtype, None, cands.get(rtype, [])))
        rows, notes = [], []
        for rtype, genre, lst in plan:
            if len(rows) >= MAX_ROWS:
                break
            keep = []
            for t in lst:
                if cat[t]['slug'] in used or len(keep) >= per_row:
                    continue
                keep.append(t)
            if len(keep) < 3:
                notes.append('row_skipped:%s%s(%d valid titles)' % (rtype, ':' + genre if genre else '', len(keep)))
                continue
            used.update(cat[t]['slug'] for t in keep)
            row = {'row_type': rtype, 'title_ids': keep}
            if genre:
                row['genre'] = genre
            rows.append(row)
        if not rows and not hero:
            return None, notes + ['no_valid_rows']
        page = {'type': 'home_page', 'dialect': dialect, 'hero_title_id': hero, 'rows': rows,
                'ttl_minutes': PAGE_TTL_MIN}
        return page, notes

    # ---- messages ------------------------------------------------------------------------------------------------
    def reminder_time(self, now):
        """Next IST evening (19:00) after today: outside quiet hours by construction."""
        return ist_at(ist_day(now) + timedelta(days=1), REMINDER_HOUR_IST)

    def plan_message(self, facts, goal, page, now, withdrawn, consent_fresh):
        """Returns (message or None, reason). Push > WhatsApp (both need consent) > in-app (no consent)."""
        if not goal or goal['goal_id'] not in REMINDER_GOALS:
            return None, 'goal_does_not_need_a_message'
        send_at = self.reminder_time(now)
        cat = self.pack.catalogue
        tid = goal['target'].get('title_id') if goal['goal_id'] == 'G07' else (page or {}).get('hero_title_id')
        if goal['goal_id'] == 'G07' and tid:
            t = cat[tid]
            variants = [v for v in self.pack.slug_titles[t['slug']] if cat[v]['dialect'] == facts['dialect']['value']]
            tid = next((v for v in variants if self.pack.title_available(v, send_at, withdrawn)
                        and v not in self.pack.quarantined_titles), None)
        if not tid or not self.pack.title_available(tid, send_at, withdrawn) or tid in self.pack.quarantined_titles:
            return None, 'no_title_available_at_send_time'
        t = cat[tid]
        copy = self.copy_for(goal, t)
        if consent_fresh and facts['notifications_opt_in']['value']:
            return {'type': 'push', 'send_at': iso(send_at), 'title_id': tid, 'text': copy,
                    'deeplink': 'ott://title/%s' % tid}, 'push_reminder'
        if consent_fresh and facts['whatsapp_opt_in']['value']:
            return {'type': 'whatsapp', 'send_at': iso(send_at), 'template_id': 'wa_recommend_title_v1',
                    'variables': {'title_name': t['title'], 'dialect_name': t['dialect']}, 'title_id': tid}, 'whatsapp_reminder'
        if not consent_fresh:
            return None, 'consent_not_verifiable_store_degraded'
        return {'type': 'in_app_message', 'send_at': iso(send_at), 'title_id': tid, 'text': copy,
                'deeplink': 'ott://title/%s' % tid}, 'in_app_no_channel_consent'

    @staticmethod
    def copy_for(goal, t):
        name = clean_title(t['title'], 24)
        if goal['goal_id'] == 'G07':
            n = goal['target'].get('next_episode')
            return {'title': clean_title('%s: episode %d' % (name, n), 40),
                    'body': 'Aapne %s beech mein chhoda tha. Episode %d aapka intezaar kar raha hai.' % (name, n)}
        return {'title': clean_title('Aaj dekhiye: %s' % name, 40),
                'body': 'Aapki app ki bhasha mein %s. Abhi dekhiye.' % name}
