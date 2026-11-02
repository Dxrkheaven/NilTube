"""
ranking.py - YouTube-style ranking for NilTube's feed and search results.

Pure Python (no Qt / yt_dlp imports), so it can be tested on its own.

Pipeline, mirroring how YouTube structures recommendations:
  1. Taste profile from watch history: time-decayed, and the most recent
     watches count extra (the "up next" signal).
  2. Candidate generation from several sources (up-next, favourite channels,
     topics, exploration) instead of one blended search.
  3. Scoring: source relevance + channel affinity + topic affinity +
     popularity, minus penalties (already watched, very short).
  4. Diversity re-rank: spread channels out instead of flooding the feed.
"""
import math
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --------------------------------------------------------------------------
# Tuning knobs - change these to taste
# --------------------------------------------------------------------------
HALF_LIFE_DAYS = 7.0           # how fast old watches fade
SESSION_BOOST = 2.0            # newest watch counts (1 + 2) = 3x ...
SESSION_DECAY = 0.7            # ... next 2.4x, then 1.98x, and so on

W_CHANNEL = 4.0                # channel affinity (log-saturated)
W_TOPIC = 2.0                  # topic affinity (normalised by title length)
BIGRAM_BONUS = 1.5             # two-word topics are more specific than one word
W_POPULARITY = 0.7             # multiplied by log10(views)
HIT_BONUS = 1.0                # candidate surfaced by more than one source
REPEAT_CHANNEL_PENALTY = 3.5   # per earlier pick from the same channel
SEARCH_PERSONALIZATION = 0.25  # typed searches: mostly relevance, light nudge
EXPLORE_JITTER = 1.5           # random wobble so the feed isn't identical each time

WATCHED_PENALTY_FEED = 200.0
WATCHED_PENALTY_SEARCH = 3.0
MIN_FRESH = 12                 # hide watched videos from the feed if at least this many unwatched remain
SHORT_SECONDS = 60
SHORT_PENALTY = 4.0

SOURCE_BASE = {"search": 12.0, "upnext": 8.0, "channel": 5.0, "topic": 4.0, "explore": 3.0}
RANK_FALLOFF = 0.08            # relevance = base / (1 + 0.08 * position in that query's results)

BLOCKED_TEXT = re.compile(r'[\u0e00-\u0e7f]')   # Thai script, same filter as before
UNKNOWN_AUTHOR = 'Unknown Author'

_STOP = {
    "ft", "feat", "official", "video", "videos", "lyrics", "lyric", "audio", "music",
    "live", "full", "part", "shorts", "with", "from", "this", "that", "what", "your",
    "when", "they", "have", "will", "about", "just", "like", "into", "over",
    "visualizer", "hd", "the", "and", "or", "to", "of", "in", "a", "an",
}


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def title_tokens(title):
    """Unigrams + adjacent bigrams, de-duplicated, in title order."""
    cleaned = re.sub(r'[^\w\s]', '', (title or '').lower())
    words = [w for w in cleaned.split() if len(w) > 3 and w not in _STOP]
    bigrams = [f"{a} {b}" for a, b in zip(words, words[1:])]
    return list(dict.fromkeys(words + bigrams))


def _query_from_title(title):
    """'Artist - Song (Official Video) [4K]' -> 'Artist Song'."""
    t = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', title or '')
    t = re.sub(r'[^\w\s]', ' ', t)
    return ' '.join(t.split()[:8])


def _age_days(ts, now):
    try:
        dt = datetime.strptime(str(ts).split('.')[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 1.0


# --------------------------------------------------------------------------
# Taste profile
# --------------------------------------------------------------------------
@dataclass
class Profile:
    watched_ids: set = field(default_factory=set)
    channels: dict = field(default_factory=dict)
    topics: Counter = field(default_factory=Counter)
    last_title: str = ""
    last_author: str = ""

    @property
    def has_history(self):
        return bool(self.watched_ids)


def load_history_rows(db_path, limit=200):
    """Newest first: (video_id, title, author, watched_at)."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT video_id, title, author, watched_at FROM persistent_history "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return cur.fetchall()
    finally:
        conn.close()


def build_profile(rows, now=None):
    """rows must be newest first."""
    now = now or datetime.now(timezone.utc)
    p = Profile()
    for i, row in enumerate(rows):
        video_id, title, author, watched_at = row[0], row[1], row[2], row[3]
        p.watched_ids.add(video_id)
        weight = (0.5 ** (_age_days(watched_at, now) / HALF_LIFE_DAYS)
                  * (1.0 + SESSION_BOOST * SESSION_DECAY ** i))
        if i == 0:
            p.last_title, p.last_author = title or "", author or ""
        if author and author != UNKNOWN_AUTHOR:
            p.channels[author] = p.channels.get(author, 0.0) + weight
        for tok in title_tokens(title):
            p.topics[tok] += weight
    return p


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------
def build_queries(profile, rng=random):
    """List of (source, query). One search per entry."""
    if not profile.has_history:
        return [("explore", "trending")]

    plan = []
    q = _query_from_title(profile.last_title)
    if q:
        plan.append(("upnext", q))

    channels = sorted(profile.channels.items(), key=lambda kv: kv[1], reverse=True)
    plan += [("channel", name) for name, _ in channels[:3]]

    ranked = [t for t, _ in profile.topics.most_common()]
    bigrams = [t for t in ranked if ' ' in t]
    unigrams = [t for t in ranked if ' ' not in t]
    topics = bigrams[:2]
    topics += unigrams[:max(0, 2 - len(topics))]
    plan += [("topic", t) for t in topics]

    tail = bigrams[3:40] + unigrams[3:40]          # weaker interests = exploration
    plan.append(("explore", rng.choice(tail) if tail else "trending"))

    seen, deduped = set(), []
    for source, query in plan:
        key = query.lower()
        if key not in seen:
            seen.add(key)
            deduped.append((source, query))
    return deduped


@dataclass
class Candidate:
    video_id: str
    title: str
    author: str
    views: Optional[float]
    duration: Optional[float]
    rel: float
    hits: int = 1
    score: float = 0.0


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def add_entries(pool, entries, source):
    """Merge one query's yt-dlp entries into pool {video_id: Candidate}."""
    base = SOURCE_BASE.get(source, 3.0)
    for pos, entry in enumerate(entries):
        if not entry:
            continue
        vid = entry.get('id')
        if not vid:
            continue
        title = entry.get('title') or 'Unknown Title'
        # keep this exact order: it must match what history stored as `author`
        author = entry.get('uploader') or entry.get('channel') or UNKNOWN_AUTHOR
        if BLOCKED_TEXT.search(title) or BLOCKED_TEXT.search(author):
            continue
        rel = base / (1.0 + RANK_FALLOFF * pos)
        cand = pool.get(vid)
        if cand is None:
            pool[vid] = Candidate(vid, title, author,
                                  _num(entry.get('view_count')), _num(entry.get('duration')), rel)
        else:
            cand.hits += 1
            cand.rel = max(cand.rel, rel)


# --------------------------------------------------------------------------
# Scoring + re-ranking
# --------------------------------------------------------------------------
def _pop(views):
    if views is None:
        return 3.0                                  # neutral (~1k views)
    return min(math.log10(max(views, 0.0) + 1.0), 8.0)


def _score(c, profile, searching, rng):
    scale = SEARCH_PERSONALIZATION if searching else 1.0
    s = c.rel
    if not searching:
        s += HIT_BONUS * (c.hits - 1)

    aff = profile.channels.get(c.author)
    if aff:
        s += scale * W_CHANNEL * math.log1p(aff)

    toks = title_tokens(c.title)
    if toks:
        t = 0.0
        for tok in toks:
            w = profile.topics.get(tok)
            if w:
                t += math.log1p(w) * (BIGRAM_BONUS if ' ' in tok else 1.0)
        s += scale * W_TOPIC * t / math.sqrt(len(toks))

    s += W_POPULARITY * _pop(c.views)

    if c.duration is not None and 0 < c.duration < SHORT_SECONDS:
        s -= SHORT_PENALTY
    if c.video_id in profile.watched_ids:
        s -= WATCHED_PENALTY_SEARCH if searching else WATCHED_PENALTY_FEED
    if not searching:
        s += rng.uniform(0.0, EXPLORE_JITTER)
    return s


def _diversify(cands, limit):
    """Greedy: each pick lowers the score of further videos from the same channel."""
    pool = list(cands)
    picked = []
    per_channel = Counter()
    while pool and len(picked) < limit:
        best_i, best_v = 0, -math.inf
        for i, c in enumerate(pool):
            pen = 0.0 if c.author == UNKNOWN_AUTHOR else REPEAT_CHANNEL_PENALTY * per_channel[c.author]
            v = c.score - pen
            if v > best_v:
                best_i, best_v = i, v
        c = pool.pop(best_i)
        picked.append(c)
        per_channel[c.author] += 1
    return picked


def rank(pool, profile, mode, limit, rng=random):
    """mode: 'feed' (recommendations) or 'search' (typed query)."""
    searching = (mode == "search")
    cands = list(pool.values())
    if not searching:
        fresh = [c for c in cands if c.video_id not in profile.watched_ids]
        if len(fresh) >= MIN_FRESH:
            cands = fresh
    for c in cands:
        c.score = _score(c, profile, searching, rng)
    cands.sort(key=lambda c: c.score, reverse=True)

    unique, seen = [], set()                        # drop re-uploads with identical titles
    for c in cands:
        key = re.sub(r'\W+', '', c.title.lower())
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(c)

    if searching:
        return unique[:limit]
    return _diversify(unique, limit)
