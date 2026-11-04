# Team guide: a self-contained manuscript

Everything needed to build, test and host an enhanced EPG for an IPTV provider,
from an empty project. Read this file alone; nothing else has to survive.

It exists because two problems are common and both are fixable with data rather
than a heavier product:

1. **Generic programme titles.** The provider's guide knows a channel has *a*
   game and emits `College Football`, so the grid is useless for deciding what
   to watch.
2. **Event channels with no guide at all.** Channels named for the matchup of
   the day — `UNC vs MIA`, `H01: 7:30pm OTT @ BUF` — carry no `tvg-id`, so no
   EPG source can match them. They are exactly the channels most worth having a
   guide for.

The tool below resolves each event channel to the teams actually playing, asks
ESPN for that game, and emits a guide containing the real matchup. It replaces
nothing: the provider's guide is merged in, and only the generic entries covering
event channels are overwritten.

Status of the reference implementation this manuscript was generated from:
776 playlist channels, 18 event-style channels, **18/18 matched**, 542 events
fetched, and a merged guide of 38,745 programmes. Every assertion in
`test/smoke.py` passes on Python 3.13 and 3.14.

## If you are an agent standing this up in a new project

Do these in order and stop only where a human's credentials are required.

1. Create `teamguide.py`, `.github/workflows/guide.yml`, `test/smoke.py` and
   `.gitignore` with the contents in [The files](#the-files). The tool is
   standard library only — do not add dependencies.
2. Run `python -m py_compile teamguide.py test/smoke.py`.
3. Run `python test/smoke.py`. It must print `ALL CHECKS PASSED`. It needs
   outbound HTTPS to `site.api.espn.com` and nothing else. Do not weaken an
   assertion to make it pass.
4. Confirm `playlist.m3u` is absent from everything you are about to publish,
   and that `teamguide.env` is ignored. See [Non-negotiables](#non-negotiables).
5. Hand back to the human for the three repository secrets, the repository
   itself, and the Pages source toggle. You cannot supply those.

## Non-negotiables

These are not style preferences. Each one has already been the cause of a real
failure or a real leak.

- **The playlist is never published.** `playlist.m3u` embeds the provider
  username and password in *every* stream URL. `guide.xml` embeds neither. The
  CI job runs with `--no-m3u` for this reason, and a guard step fails the build
  if a credential pattern appears in the output.
- **The guard step is not removable.** It is the only thing between a mistake
  and a public credential dump. If it fails, find the leak; do not delete it.
- **`--id-mode slot` is required** for the event channels. The provider renames
  them nightly; an id derived from the full name changes every day and silently
  breaks any per-channel EPG assignment in the player.
- **The player uses the provider's playlist**, not a generated one. That keeps
  playback working when nothing of ours is running, which is the whole point of
  not making one small machine load-bearing.
- **Aliases are applied last.** A seed like `NYK -> NY` must never shadow a real
  match, or `CHI` resolves to the wrong team on a channel where Chicago's other
  club is playing.

## How it works

```
 playlist / Xtream API ──┐
                         ├─> strip decoration ─> split matchup ─> resolve per league ─> ESPN scoreboard
 channel names ──────────┘                                                                    │
                                                                                              v
 provider XMLTV ─────────────────────> merge, generic titles replaced ─────────────> guide.xml ──> Pages
```

**1. Strip decoration.** `H01: 7:30pm OTT @ BUF` loses the slot code, the time
and the noise words before anything else looks at it. Without this the tokens
`7` and `30` get treated as team abbreviations.

**2. Split the matchup.** Both `X vs Y` and `X @ Y` forms are recognised. A name
that does not split (`ESPN`, `NBA League Pass 1`) is left alone rather than
guessed at.

**3. Resolve per league.** This is the part that matters. A single shared index
makes `CHI` ambiguous — it resolves to the Blackhawks on a Bulls channel. The
index is built per league, so `CHI` inside `nhl` is only ever Chicago's hockey
club. Which leagues are fetched at all is inferred from the channels: the index
covers every supported league, and scoreboards are requested only for the
leagues that can host both teams of a matchup. A provider relabelling its
categories for the season — "Baseball" instead of "MLB" — needs no
configuration, because nothing keys on the label.

**4. Fetch ESPN per day.** The scoreboard endpoint rejects date *ranges* with
HTTP 400; it must be queried one day at a time. Days in the past are cached for
a week, future days for an hour, and requests run in a small thread pool.

**5. Merge, do not replace.** The provider's XMLTV is the base. Only the
programmes whose titles are generic and whose channels are event channels are
overwritten. Everything else — CNN, the regional sports networks — is preserved
untouched.

## Repository layout

```
<repo>/
├── teamguide.py                 the tool (stdlib only)
├── test/smoke.py                assertions, incl. real provider channel names
├── .github/workflows/guide.yml  scheduled generation + gh-pages publish
├── .gitignore                   keeps caches, output and credentials out of git
├── GITHUB.md                    short operator notes
├── MANUSCRIPT.md                this file
└── build_manuscript.py          regenerates this file from the sources (authoring only;
                                 a fresh project does not need it)
```

There is deliberately no requirements file, no lockfile, no container and no
server. The scheduled job is the runtime.

## Bootstrapping a new project

Assumes a GitHub account and provider credentials already exist.

1. **Create a public repository.** Public is required for Pages on a free plan.
   The guide carries no credentials, so public is appropriate here.

2. **Push the contents of this directory as the repository root.**

   ```sh
   git init
   git add teamguide.py test .github .gitignore GITHUB.md MANUSCRIPT.md
   git commit -m "Enhanced EPG guide for the provider playlist"
   git branch -M main
   git remote add origin git@github.com:<user>/<repo>.git
   git push -u origin main
   ```

3. **Add three repository secrets** — Settings → Secrets and variables →
   Actions → New repository secret:

   | Secret | Value |
   | --- | --- |
   | `XTREAM_HOST` | panel address, e.g. `http://host:8080` |
   | `XTREAM_USER` | provider username |
   | `XTREAM_PASS` | provider password |

   These are consumed as environment variables by the workflow. They are never
   written to a file, never echoed, and are masked in logs.

4. **Actions → EPG guide → Run workflow.** Do not wait for the schedule; verify
   the first build by hand. The run also creates the `gh-pages` branch Pages
   serves.

5. **Settings → Pages → Build and deployment → Source: Deploy from a branch**
   → Branch `gh-pages`, folder `/ (root)` → **Save**.

The guide is then served at:

```
https://<user>.github.io/<repo>/guide.xml
```

If the provider password is ever rotated, only `XTREAM_PASS` changes. Nothing in
the repository is affected, because nothing in the repository holds a credential.

## The files

These blocks are the source of truth for a fresh project.

### `teamguide.py`

````python
#!/usr/bin/env python3
"""teamguide - TiviMate-friendly XMLTV for IPTV sports event channels.

Reads an M3U playlist, finds channels named like "UNC vs MIA" or "ALA @ UGA",
matches them against real ESPN events, and writes:

  guide.xml      XMLTV with a programme for every matched channel
  playlist.m3u   (optional) the same playlist with tvg-ids injected, so the
                 guide attaches with no manual mapping at all

Standard library only. Runs on Python 3.8+ (tested target: Pi 3, Debian 13).

Why this exists: teamarr/Dispatcharr solve this with a Postgres + uwsgi stack
and still need a separate step to tie guide data to channels. The actual job is
one HTTP GET per league, a dictionary lookup on team abbreviation, and some XML.
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{slug}"

# key -> (sport path, league slug, default duration in hours)
LEAGUES = {
    "cfb":   ("football",   "college-football",         3.5),
    "nfl":   ("football",   "nfl",                      3.25),
    "nba":   ("basketball", "nba",                      2.5),
    "ncaab": ("basketball", "mens-college-basketball",  2.25),
    "wnba":  ("basketball", "wnba",                     2.25),
    "nhl":   ("hockey",     "nhl",                      2.5),
    "mlb":   ("baseball",   "mlb",                      3.0),
    "mls":   ("soccer",     "usa.1",                    2.0),
    "epl":   ("soccer",     "eng.1",                    2.0),
    "ucl":   ("soccer",     "uefa.champions",           2.0),
}

# Separators used by providers to join two teams in a channel name.
SPLIT_RE = re.compile(r"\s+(?:vs\.?|v\.?|@)\s+", re.IGNORECASE)

# Providers decorate event channel names: "H01: 7:30pm OTT @ BUF". Peel the
# channel code, the kick-off time and broadcast noise off each side.
CODE_RE = re.compile(r"^\s*[A-Za-z]{1,3}\s*\d{1,4}\s*[:.\-]\s*")
SLOT_RE = re.compile(r"^\s*([A-Za-z]{1,3}\d{1,4})\s*[:.\-]", re.IGNORECASE)
TIME_RE = re.compile(
    r"\b\d{1,2}\s*[:.]\s*\d{2}\s*(?:a\.?m\.?|p\.?m\.?)\b"
    r"|\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE)
NOISE_RE = re.compile(r"\b(?:live|hd|fhd|uhd|4k|sd|ppv|event|feed|game)\b",
                      re.IGNORECASE)

# Tried only after exact, prefix and fuzzy resolution all fail, so these can
# never shadow a real match. Provider shorthand -> ESPN abbreviation.
DEFAULT_ALIASES = {
    "NYK": "NY", "BRK": "BKN", "OKL": "OKC",
    "WAS": "WSH", "TB": "TBL", "NJ": "NJD", "SJ": "SJS",
    "GSW": "GS", "NOP": "NO",
}


def clean_token(text):
    """Reduce a decorated side ("H01: 7:30pm OTT") to the team token ("OTT")."""
    token = CODE_RE.sub(" ", text)
    token = TIME_RE.sub(" ", token)
    token = NOISE_RE.sub(" ", token)
    token = re.sub(r"\s+", " ", token)
    return token.strip(" \t-\u2013\u2014:|\u00b7\u2022()[]{},")
EXTINF_RE = re.compile(r"^#EXTINF:(?P<dur>-?\d+(?:\.\d+)?)\s*(?P<attrs>[^,]*),(?P<name>.*)$")
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
XMLTV_FMT = "%Y%m%d%H%M%S +0000"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def mask_url(url):
    """Keep provider credentials out of logs, tracebacks and captured output."""
    masked = re.sub(r"(://)[^/]+", r"\1<host>", str(url))
    masked = re.sub(r"((?:username|password)=)[^&\s]*", r"\1***", masked,
                    flags=re.IGNORECASE)
    return masked


def norm(value):
    """Normalise a team/channel token for lookup: alphanumerics, uppercased."""
    if value is None:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value)).upper()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


# --------------------------------------------------------------------------- io

def fetch(url, timeout=30, cache_dir=None, cache_ttl=1800, cache_name=None):
    """Fetch bytes, optionally caching to disk. Never sends Accept-Encoding."""
    if cache_dir and cache_name:
        path = os.path.join(cache_dir, cache_name + ".cache")
        try:
            age = time.time() - os.path.getmtime(path)
            if age < cache_ttl:
                with open(path, "rb") as fh:
                    return fh.read()
        except OSError:
            pass

    req = urllib.request.Request(url, headers={"User-Agent": "teamguide/1.0 (+iptv epg)"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                if cache_dir and cache_name:
                    os.makedirs(cache_dir, exist_ok=True)
                    tmp = os.path.join(cache_dir, cache_name + ".tmp")
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp, os.path.join(cache_dir, cache_name + ".cache"))
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("fetch failed for %s: %s" % (mask_url(url), last_err))


def load_source(value, cache_dir=None, cache_ttl=1800):
    """Load a URL or a local path as text."""
    if re.match(r"^https?://", value, re.IGNORECASE):
        name = hashlib.sha1(value.encode()).hexdigest()[:16]
        raw = fetch(value, cache_dir=cache_dir, cache_ttl=cache_ttl, cache_name=name)
    else:
        with open(value, "rb") as fh:
            raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


# ------------------------------------------------------------------------ m3u

def parse_m3u(text):
    """Return (channels, header). Per-channel directives like #EXTVLCOPT are kept."""
    channels, pending, header = [], None, "#EXTM3U"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = line          # keep url-tvg / x-tvg-url attributes
            continue
        if line.startswith("#EXTINF:"):
            match = EXTINF_RE.match(line)
            attrs, name = {}, ""
            if match:
                attrs = {k: v for k, v in ATTR_RE.findall(match.group("attrs"))}
                name = (match.group("name") or "").strip()
            if not name:
                name = (attrs.get("tvg-name") or "").strip()
            pending = {"attrs": attrs, "name": name, "extinf": line,
                       "url": "", "extra": []}
        elif line.startswith("#"):
            if pending is not None:
                pending["extra"].append(line)
        elif pending is not None:
            pending["url"] = line
            channels.append(pending)
            pending = None
    return channels, header


def xtream_urls(base, username, password):
    """Build the standard Xtream Codes endpoints from server + credentials."""
    base = base.strip().rstrip("/")
    if not re.match(r"^https?://", base, re.IGNORECASE):
        base = "http://" + base
    user, secret = quote(username, safe=""), quote(password, safe="")
    return {
        "api": "%s/player_api.php?username=%s&password=%s" % (base, user, secret),
        "xmltv": "%s/xmltv.php?username=%s&password=%s" % (base, user, secret),
        "live": "%s/live/%s/%s/" % (base, user, secret),
    }


def load_xtream(base, username, password, stream_format, cache_dir):
    """Channels from the Xtream Codes API. epg_channel_id is the native tvg-id."""
    if not username or not password:
        raise SystemExit("--xtream requires --username and --password")
    urls = xtream_urls(base, username, password)

    info = json.loads(fetch(urls["api"], cache_dir=cache_dir, cache_ttl=3600,
                            cache_name="xc-info"))
    if not isinstance(info, dict) or not ({"user_info", "server_info"} & set(info)):
        raise SystemExit("xtream: unexpected player_api.php response: %s" % str(info)[:200])

    groups = {}
    try:
        for node in json.loads(fetch(urls["api"] + "&action=get_live_categories",
                                     cache_dir=cache_dir, cache_ttl=3600,
                                     cache_name="xc-cats")):
            groups[str(node.get("category_id"))] = node.get("category_name") or ""
    except Exception as exc:
        log("  ! xtream categories unavailable: %s" % exc)

    streams = json.loads(fetch(urls["api"] + "&action=get_live_streams",
                               cache_dir=cache_dir, cache_ttl=3600,
                               cache_name="xc-live"))
    if not isinstance(streams, list):
        raise SystemExit("xtream: get_live_streams did not return a list")

    channels = []
    for stream in streams:
        name = (stream.get("name") or "").strip()
        stream_id = stream.get("stream_id")
        if not name or stream_id in (None, ""):
            continue
        channels.append({
            "attrs": {
                "tvg-id": (stream.get("epg_channel_id") or "").strip(),
                "tvg-name": name,
                "tvg-logo": (stream.get("stream_icon") or "").strip(),
                "group-title": groups.get(str(stream.get("category_id")), ""),
            },
            "name": name,
            "url": "%s%s.%s" % (urls["live"], stream_id, stream_format),
            "extra": [],
        })

    log("xtream: %d live channels" % len(channels))
    return channels, "#EXTM3U"


def channel_key(channel, id_mode):
    """Stable guide channel id for a playlist channel.

    Providers rename event channels nightly ("H01: 7:30pm OTT @ BUF"), so an id
    derived from the full name changes every day and any manual EPG assignment
    in the player breaks with it. In slot mode the id follows the stable code.
    """
    existing = (channel["attrs"].get("tvg-id") or "").strip()
    if existing:
        return existing
    if id_mode == "slot":
        match = SLOT_RE.match(channel["name"])
        if match:
            return "tg." + match.group(1).lower()
    return "tg." + slug(channel["name"])


def split_matchup(name):
    """'UNC vs MIA' -> ('UNC', 'MIA'). Returns None when not an event name."""
    parts = SPLIT_RE.split(name.strip())
    if len(parts) != 2:
        return None
    left, right = clean_token(parts[0]), clean_token(parts[1])
    if not left or not right or len(left) > 40 or len(right) > 40:
        return None
    # Reject obvious non-teams ("NBA League Pass 1 vs 2" style noise handled by lookup)
    if left.isdigit() or right.isdigit():
        return None
    return left, right


# ----------------------------------------------------------------------- espn

def build_team_index(league_keys, cache_dir):
    """league -> (index, fuzzy, fuzzy_teams).

    Kept per league on purpose: 'CHI', 'BOS', 'DET', 'PHI', 'TOR', 'NY' and 'WAS'
    all exist in several leagues, so one shared index silently resolves a Bulls
    channel to the Blackhawks.
    """
    per_league = {}

    for lk in league_keys:
        sport, lg, duration = LEAGUES[lk]
        url = ESPN_BASE.format(sport=sport, slug=lg) + "/teams?limit=1000"
        try:
            payload = json.loads(fetch(url, cache_dir=cache_dir,
                                       cache_ttl=86400, cache_name="teams-" + lk))
        except Exception as exc:  # a dead league must not kill the run
            log("  ! teams/%s unavailable: %s" % (lk, exc))
            continue

        try:
            wrappers = payload["sports"][0]["leagues"][0]["teams"]
        except (KeyError, IndexError):
            log("  ! teams/%s unexpected shape" % lk)
            continue

        index, keys_for_fuzzy = {}, []
        for wrap in wrappers:
            team = wrap.get("team") or {}
            if not team.get("id"):
                continue
            team = dict(team)
            team["_league"] = lk
            team["_duration"] = duration
            words = set()
            for field in ("abbreviation", "shortDisplayName", "name",
                          "location", "displayName", "nickname"):
                value = team.get(field)
                if value:
                    words.add(norm(value))
            # "North Carolina Tar Heels" -> also index "NorthCarolina"
            if team.get("location") and team.get("name"):
                words.add(norm(str(team["location"]) + str(team["name"])))
            for word in words:
                if word:
                    index.setdefault(word, []).append(team)
            keys_for_fuzzy.append((norm(team.get("abbreviation") or ""), team))

        fuzzy = [k for k, _ in keys_for_fuzzy if len(k) >= 3]
        fuzzy_teams = {k: t for k, t in keys_for_fuzzy if len(k) >= 3}
        per_league[lk] = (index, fuzzy, fuzzy_teams)
        log("  teams/%s: %d keys" % (lk, len(index)))

    return per_league


def resolve_team(token, index, fuzzy, fuzzy_teams, aliases):
    """Token -> team dict or None, widening only as far as it stays unambiguous."""
    key = norm(aliases.get(norm(token), token))
    if not key:
        return None
    if key in index:
        return index[key][0]

    # Provider shorthand is often a prefix of the ESPN key: BUFF -> BUF/BUFFALO.
    # Only accept it when every expansion belongs to a single team.
    if len(key) >= 3:
        loose = {}
        for candidate, teams in index.items():
            if len(candidate) < 3:
                continue
            if candidate.startswith(key) or key.startswith(candidate):
                for team in teams:
                    loose[team.get("id")] = team
        if len(loose) == 1:
            return next(iter(loose.values()))

    if fuzzy:
        near = difflib.get_close_matches(key, fuzzy, n=1, cutoff=0.82)
        if near:
            return fuzzy_teams[near[0]]

    mapped = DEFAULT_ALIASES.get(key)
    if mapped and mapped in index:
        return index[mapped][0]
    return None


def resolve_league_selection(value):
    """--leagues value -> the leagues to build a team index for.

    "auto" (the default) and "all" both mean every league the tool knows, because
    which leagues matter is decided from the channels themselves. A provider that
    relabels a category for the season ("Baseball" in October, "MLB" in April)
    changes nothing here.
    """
    value = (value or "auto").strip().lower()
    if value in ("auto", "all"):
        return list(LEAGUES)
    keys = [k.strip().lower() for k in value.split(",") if k.strip()]
    unknown = [k for k in keys if k not in LEAGUES]
    if unknown:
        raise SystemExit("unknown league(s): %s\nvalid: %s, auto, all"
                         % (", ".join(unknown), ", ".join(sorted(LEAGUES))))
    return keys


def infer_leagues(candidates, per_league, aliases):
    """Leagues worth fetching scoreboards for, from the channel names alone.

    Every matchup is resolved against every league's index first, and a league is
    only fetched when it can host *both* teams of a channel. A night of basketball
    and hockey therefore costs two league fetches instead of ten, and nothing keys
    on a category label, so seasonal relabelling needs no configuration.
    """
    needed = []
    for _channel, left, right in candidates:
        for league_key, (index, fuzzy, fuzzy_teams) in per_league.items():
            team_a = resolve_team(left, index, fuzzy, fuzzy_teams, aliases)
            team_b = resolve_team(right, index, fuzzy, fuzzy_teams, aliases)
            if team_a and team_b and team_a["id"] != team_b["id"]:
                if league_key not in needed:
                    needed.append(league_key)
    return needed


def parse_iso(value):
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_day(league_key, day, cache_dir, ttl):
    """One league, one calendar day. ESPN 400s on date ranges, so this is per-day."""
    sport, slug_name, duration = LEAGUES[league_key]
    stamp = day.strftime("%Y%m%d")
    url = ESPN_BASE.format(sport=sport, slug=slug_name) + "/scoreboard?dates=%s" % stamp
    payload = json.loads(fetch(url, cache_dir=cache_dir, cache_ttl=ttl,
                               cache_name="sb-%s-%s" % (league_key, stamp)))

    found = []
    for ev in payload.get("events") or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        sides = {}
        for side in comp.get("competitors") or []:
            team = side.get("team") or {}
            if not team.get("id"):
                continue
            sides[side.get("homeAway")] = {
                "id": str(team.get("id")),
                "name": team.get("displayName") or team.get("shortDisplayName") or "",
                "abbr": team.get("abbreviation") or "",
                "logo": team.get("logo") or "",
            }
        if "home" not in sides or "away" not in sides:
            continue
        start_dt = parse_iso(ev.get("date"))
        if start_dt is None:
            continue
        found.append({
            "id": ev.get("id"),
            "league": league_key,
            "start": start_dt,
            "end": start_dt + timedelta(hours=duration),
            "name": ev.get("name") or "%s vs %s" % (sides["away"]["name"], sides["home"]["name"]),
            "short": ev.get("shortName") or "",
            "away": sides["away"],
            "home": sides["home"],
            "venue": ((comp.get("venue") or {}).get("fullName") or ""),
            "status": ((ev.get("status") or {}).get("type") or {}).get("state") or "",
        })
    return found


def fetch_events(league_keys, start, end, cache_dir,
                 past_ttl=604800, future_ttl=3600, workers=6):
    """Per-day parallel fetch. Past days never change, so cache them for a week."""
    days, cursor = [], start.date()
    while cursor <= end.date():
        days.append(cursor)
        cursor += timedelta(days=1)

    today = datetime.now(timezone.utc).date()
    jobs = [(league_key, day) for league_key in league_keys for day in days]
    events = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for league_key, day in jobs:
            ttl = past_ttl if day < today else future_ttl
            pending[pool.submit(_fetch_day, league_key, day, cache_dir, ttl)] = (league_key, day)
        for future in as_completed(pending):
            league_key, day = pending[future]
            try:
                events.extend(future.result())
            except Exception as exc:
                log("  ! scoreboard/%s %s unavailable: %s" % (league_key, day, exc))

    events.sort(key=lambda e: e["start"])
    log("  events loaded: %d (%d day/league requests)" % (len(events), len(jobs)))
    return events


def match_events(team_a, team_b, events):
    """All events featuring both teams, soonest first."""
    hits = []
    for ev in events:
        ids = {ev["home"]["id"], ev["away"]["id"]}
        if team_a["id"] in ids and team_b["id"] in ids and team_a["id"] != team_b["id"]:
            hits.append(ev)
    hits.sort(key=lambda e: e["start"])
    return hits


# ----------------------------------------------------------------------- xmltv

def xmltv_time(dt):
    return dt.astimezone(timezone.utc).strftime(XMLTV_FMT)


def parse_xmltv_time(text):
    text = (text or "").strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def build_xmltv(assignments, base_root=None, generated_by="teamguide"):
    """assignments: [{key, channel_name, programmes: [event, ...]}]"""
    if base_root is not None:
        root = base_root
        for tag in ("generator-info-name", "generator-info-url"):
            for node in root.findall(tag):
                root.remove(node)
            # the provider also writes these as <tv> attributes; drop them so the
            # published guide does not advertise the provider's endpoint
            root.attrib.pop(tag, None)
    else:
        root = ET.Element("tv")

    gen = ET.Element("generator-info-name")
    gen.text = generated_by
    root.insert(0, gen)

    ours = {}          # channel key -> [(start, stop)]
    existing_ch = {c.get("id"): c for c in root.findall("channel")}
    existing_pr = list(root.findall("programme"))

    for entry in assignments:
        key = entry["key"]
        if key not in existing_ch:
            node = ET.SubElement(root, "channel", {"id": key})
            display = ET.SubElement(node, "display-name")
            display.text = entry["channel_name"]
            existing_ch[key] = node

        for ev in entry["programmes"]:
            span = (ev["start"], ev["end"])
            ours.setdefault(key, []).append(span)

            prog = ET.Element("programme", {
                "start": xmltv_time(ev["start"]),
                "stop": xmltv_time(ev["end"]),
                "channel": key,
            })
            title = ET.SubElement(prog, "title", {"lang": "en"})
            title.text = "%s vs %s" % (ev["away"]["name"], ev["home"]["name"])

            sub = ET.SubElement(prog, "sub-title", {"lang": "en"})
            sub.text = ev["league"].upper()

            desc = ET.SubElement(prog, "desc", {"lang": "en"})
            bits = [ev["name"]]
            if ev["venue"]:
                bits.append(ev["venue"])
            if ev["status"] == "post":
                bits.append("Final")
            desc.text = " - ".join(b for b in bits if b)

            cat = ET.SubElement(prog, "category", {"lang": "en"})
            cat.text = "Sports"
            league_cat = ET.SubElement(prog, "category", {"lang": "en"})
            league_cat.text = ev["league"].upper()

            if ev["home"].get("logo"):
                icon = ET.SubElement(prog, "icon", {"src": ev["home"]["logo"]})

            root.append(prog)

    # Drop the provider's generic entry wherever we have a real one.
    if ours:
        removed = 0
        for prog in existing_pr:
            key = prog.get("channel")
            spans = ours.get(key)
            if not spans:
                continue
            p_start = parse_xmltv_time(prog.get("start"))
            p_stop = parse_xmltv_time(prog.get("stop"))
            if p_start is None:
                continue
            if p_stop is None:
                p_stop = p_start + timedelta(hours=3)
            for start, stop in spans:
                if p_start < stop and start < p_stop:
                    root.remove(prog)
                    removed += 1
                    break
        if removed:
            log("  replaced %d generic provider programme(s)" % removed)

    return root


def write_xmltv(root, path):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(ET.tostring(root, encoding="utf-8"))
        fh.write(b"\n")
    os.replace(tmp, path)


def write_m3u(channels, keys, path, header="#EXTM3U"):
    lines = [header]
    for ch, key in zip(channels, keys):
        attrs = dict(ch["attrs"])
        if key:
            attrs["tvg-id"] = key
        order = ["tvg-id", "tvg-name", "tvg-logo", "group-title"]
        parts = []
        for field in order:
            if attrs.get(field):
                parts.append('%s="%s"' % (field, attrs[field]))
        for field, value in attrs.items():
            if field not in order and value:
                parts.append('%s="%s"' % (field, value))
        lines.append("#EXTINF:-1 %s,%s" % (" ".join(parts), ch["name"]))
        for extra in ch.get("extra") or []:
            lines.append(extra)
        lines.append(ch["url"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------- main

def load_aliases(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return {norm(k): v for k, v in raw.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build an XMLTV guide for IPTV event channels.")
    ap.add_argument("--m3u", default="", help="playlist URL or local file")
    ap.add_argument("--xtream", default="", help="Xtream Codes server, e.g. http://host:8080")
    ap.add_argument("--username", default="", help="Xtream username")
    ap.add_argument("--password", default="", help="Xtream password")
    ap.add_argument("--stream-format", default="ts", choices=("ts", "m3u8"),
                    help="container for generated Xtream stream URLs")
    ap.add_argument("--no-merge", action="store_true",
                    help="do not default --merge to the provider guide when using --xtream")
    ap.add_argument("--merge", default="", help="provider XMLTV URL/path to merge into the output")
    ap.add_argument("--out-dir", default="out", help="where to write guide.xml and playlist.m3u")
    ap.add_argument("--leagues", default="auto",
                    help="'auto' (default) infers the leagues from the "
                         "channel names; 'all' or a comma list of: "
                         + ", ".join(sorted(LEAGUES)))
    ap.add_argument("--days-back", type=int, default=2)
    ap.add_argument("--days-ahead", type=int, default=14)
    ap.add_argument("--aliases", default="", help="JSON {token: espn-abbreviation}")
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--no-m3u", action="store_true", help="do not write playlist.m3u")
    ap.add_argument("--id-mode", choices=("name", "slot"), default="name",
                    help="guide channel id from the channel name, or from the "
                         "stable slot code (H01 -> tg.h01)")
    ap.add_argument("--dry-run", action="store_true", help="report matches, write nothing")
    args = ap.parse_args(argv)

    league_keys = resolve_league_selection(args.leagues)

    if args.xtream:
        channels, m3u_header = load_xtream(args.xtream, args.username, args.password,
                                           args.stream_format, args.cache_dir)
        if not args.merge and not args.no_merge:
            args.merge = xtream_urls(args.xtream, args.username, args.password)["xmltv"]
            log("merge defaulted to provider guide: %s" % mask_url(args.merge))
    else:
        if not args.m3u:
            raise SystemExit("provide --m3u URL or --xtream server (+ --username / --password)")
        log("playlist: %s" % mask_url(args.m3u))
        playlist = load_source(args.m3u, cache_dir=args.cache_dir, cache_ttl=3600)
        channels, m3u_header = parse_m3u(playlist)
    log("channels parsed: %d" % len(channels))

    candidates = []
    for ch in channels:
        pair = split_matchup(ch["name"])
        if pair:
            candidates.append((ch, pair[0], pair[1]))
    log("event-style channel names: %d" % len(candidates))

    if not candidates:
        log("nothing looks like 'X vs Y' - nothing to do")
        return 0

    aliases = load_aliases(args.aliases)
    per_league = build_team_index(league_keys, args.cache_dir)

    now = datetime.now(timezone.utc)
    needed = infer_leagues(candidates, per_league, aliases)
    log("leagues inferred from the channel names: %s"
        % (", ".join(needed) if needed else "none"))
    if needed and len(needed) < len(league_keys):
        log("  skipping the scoreboards of %d irrelevant league(s)"
            % (len(league_keys) - len(needed)))
    events = fetch_events(needed,
                          now - timedelta(days=args.days_back),
                          now + timedelta(days=args.days_ahead),
                          args.cache_dir)

    assignments, unmatched = [], []
    matched_count = 0
    seen_keys = set()

    for ch, left, right in candidates:
        found, reason = None, None

        for league_key, (index, fuzzy, fuzzy_teams) in per_league.items():
            team_a = resolve_team(left, index, fuzzy, fuzzy_teams, aliases)
            team_b = resolve_team(right, index, fuzzy, fuzzy_teams, aliases)
            if not team_a or not team_b:
                continue
            hits = match_events(team_a, team_b, events)
            if hits:
                found = (league_key, hits)
                break
            if reason is None:
                reason = "no %s event for %s vs %s in window" % (
                    league_key.upper(), team_a.get("abbreviation"), team_b.get("abbreviation"))

        if not found:
            if reason is None:
                sample = next(iter(per_league.values()), ({}, [], {}))
                missing = [t for t in (left, right)
                           if not resolve_team(t, sample[0], sample[1], sample[2], aliases)]
                reason = ("unresolved team: " + " / ".join(missing)) if missing \
                    else "no league matched both teams"
            unmatched.append((ch["name"], reason))
            continue

        key = channel_key(ch, args.id_mode)
        if key in seen_keys:                 # two channels, one id: keep both
            key = "%s.%d" % (key, len(seen_keys))
        seen_keys.add(key)
        assignments.append({"key": key, "channel_name": ch["name"], "programmes": found[1]})
        matched_count += 1

    # fill keys for the rewritten playlist
    assigned = {a["channel_name"]: a["key"] for a in assignments}
    keys = [assigned.get(ch["name"], "") for ch in channels]

    total_progs = sum(len(a["programmes"]) for a in assignments)
    log("")
    log("matched channels: %d / %d" % (matched_count, len(candidates)))
    log("programmes: %d" % total_progs)
    if unmatched:
        log("unmatched (%d):" % len(unmatched))
        for name, why in unmatched[:40]:
            log("  - %-34s %s" % (name[:34], why))
        if len(unmatched) > 40:
            log("  ... and %d more" % (len(unmatched) - 40))

    if args.dry_run:
        log("\ndry run: nothing written")
        return 0

    base = None
    if args.merge:
        xml = load_source(args.merge, cache_dir=args.cache_dir, cache_ttl=3600)
        try:
            base = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError as exc:
            log("  ! provider XMLTV unparseable, writing unmerged: %s" % exc)
            base = None
        if base is not None:
            log("  merged base guide: %d channels, %d programmes"
                % (len(base.findall("channel")), len(base.findall("programme"))))

    root = build_xmltv(assignments, base_root=base)
    os.makedirs(args.out_dir, exist_ok=True)
    guide = os.path.join(args.out_dir, "guide.xml")
    write_xmltv(root, guide)
    log("wrote %s (%d channels, %d programmes)"
        % (guide, len(root.findall("channel")), len(root.findall("programme"))))

    if not args.no_m3u:
        out_m3u = os.path.join(args.out_dir, "playlist.m3u")
        write_m3u(channels, keys, out_m3u, m3u_header)
        log("wrote %s (%d channels, %d with tvg-id)" % (
            out_m3u, len(channels), sum(1 for k in keys if k)))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
````

### `.github/workflows/guide.yml`

````yaml
name: EPG guide

on:
  schedule:
    # The provider replaces its event channels once a day at a time that moves,
    # so this polls hourly. A run whose guide is unchanged does not republish,
    # which keeps the extra runs from turning into extra Pages builds.
    - cron: "17 * * * *"
  workflow_dispatch:

permissions:
  contents: read

# Never let two runs race for the publish.
concurrency:
  group: epg-guide
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    # Publishing commits to the gh-pages branch, so this job needs contents
    # write. It is the only write the build has; nothing else is writable.
    permissions:
      contents: write
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Generate guide
        env:
          XTREAM_HOST: ${{ secrets.XTREAM_HOST }}
          XTREAM_USER: ${{ secrets.XTREAM_USER }}
          XTREAM_PASS: ${{ secrets.XTREAM_PASS }}
        run: |
          set -euo pipefail
          test -n "$XTREAM_HOST" || { echo "::error::missing secret XTREAM_HOST"; exit 1; }
          test -n "$XTREAM_USER" || { echo "::error::missing secret XTREAM_USER"; exit 1; }
          test -n "$XTREAM_PASS" || { echo "::error::missing secret XTREAM_PASS"; exit 1; }
          # No --leagues here on purpose: the tool infers them from the channels,
          # so a season change or a relabelled provider category needs no edit.
          # --no-m3u is not optional: the playlist embeds the provider username
          # and password in every stream URL and must never be published.
          python3 teamguide.py \
            --xtream "$XTREAM_HOST" \
            --username "$XTREAM_USER" \
            --password "$XTREAM_PASS" \
            --id-mode slot \
            --cache-dir .cache \
            --no-m3u \
            --out-dir out

      - name: Refuse to publish credentials
        run: |
          set -euo pipefail
          if grep -rIilE 'password=|username=|/live/' out/; then
            echo "::error::generated output references provider credentials - not publishing"
            exit 1
          fi
          echo "clean: no credentials or stream URLs in out/"

      - name: Sanity check guide
        run: |
          set -euo pipefail
          python3 - <<'PY'
          import sys
          import xml.etree.ElementTree as ET

          root = ET.parse("out/guide.xml").getroot()
          channels = root.findall("channel")
          programmes = root.findall("programme")
          ids = {c.get("id") for c in channels}
          orphans = [p for p in programmes if p.get("channel") not in ids]
          enhanced = sorted(c.get("id") for c in channels if (c.get("id") or "").startswith("tg."))

          print("channels=%d unique=%d programmes=%d orphans=%d enhanced=%d"
                % (len(channels), len(ids), len(programmes), len(orphans), len(enhanced)))
          print("enhanced ids: %s" % ", ".join(enhanced[:8]))

          if not programmes:
              sys.exit("guide has no programmes")
          if orphans:
              sys.exit("guide has programmes for undeclared channels")
          if len(enhanced) < 10:
              sys.exit("only %d enhanced channels, expected the event slots" % len(enhanced))
          PY

      - name: Publish to the gh-pages branch
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          # Pages builds straight from this branch. Publishing through the Pages
          # deployment API instead can leave a first deployment sitting in
          # progress forever, which leaves the site unpublished with no error to
          # act on. A branch push always completes.
          url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
          stage=$(mktemp -d)
          cd "$stage"
          git init -q -b gh-pages
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # The schedule runs hourly to catch the provider's daily rename, so a
          # byte-identical guide must not force a Pages rebuild every hour.
          if git fetch -q --depth=1 "$url" gh-pages 2>/dev/null; then
            if git show FETCH_HEAD:guide.xml > published.xml 2>/dev/null \
               && cmp -s published.xml "$GITHUB_WORKSPACE/out/guide.xml"; then
              echo "guide.xml is identical to the published copy - not republishing"
              exit 0
            fi
          fi
          cp "$GITHUB_WORKSPACE/out/guide.xml" guide.xml
          : > .nojekyll
          git add --all
          git commit -q -m "guide: $(date -u +%FT%TZ)"
          # Force: the branch holds exactly one commit, replaced on every change.
          git push -q --force "$url" gh-pages
          echo "published guide.xml to the gh-pages branch of ${GITHUB_REPOSITORY}"

  # GitHub disables scheduled workflows in *public* repositories after 60 days
  # with no repository activity. Pages on a private repository avoids that rule
  # but needs a paid plan, so keep the repository public and reset the clock
  # instead: at most one trivial commit every 40 days, nothing to remember.
  keepalive:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Reset the inactivity clock
        run: |
          set -euo pipefail
          if [ -f .keepalive ]; then
            if last=$(date -u -d "$(cat .keepalive)" +%s 2>/dev/null); then
              age=$(( ( $(date -u +%s) - last ) / 86400 ))
              echo "keepalive last reset ${age} day(s) ago"
              if [ "$age" -lt 40 ]; then
                echo "nothing to do"
                exit 0
              fi
            fi
          fi
          date -u +%F > .keepalive
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add .keepalive
          git commit -m "chore: reset the inactivity clock so the schedule stays enabled"
          # A concurrent commit would reject the push; rebase once and retry.
          git push origin HEAD:main || {
            git pull --rebase --quiet origin main
            git push origin HEAD:main
          }
````

### `test/smoke.py`

````python
#!/usr/bin/env python3
"""Smoke test for teamguide.

Builds a playlist out of real ESPN matchups (so matching is exercised against
live data), then checks the generated guide and rewritten playlist, including
the merged-guide path where a provider's generic entry must be replaced.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import teamguide as tg

CACHE = os.path.join(HERE, "cache")
failures = []


def check(condition, label, detail=""):
    status = "PASS" if condition else "FAIL"
    print("  [%s] %s%s" % (status, label, ("  -- " + detail) if detail and not condition else ""))
    if not condition:
        failures.append(label)


def pick_events():
    now = datetime.now(timezone.utc)
    for league in ("cfb", "nfl", "nba", "nhl", "ncaab", "mlb"):
        try:
            events = tg.fetch_events([league], now - timedelta(days=2),
                                     now + timedelta(days=10), CACHE)
        except Exception as exc:
            print("  fetch %s failed: %s" % (league, exc))
            continue
        if events:
            return league, events
    return None, []


def test_naming():
    """Real-world provider channel names, taken from a live Xtream panel."""
    print("== provider naming ==")
    cases = [
        ("H01: 7:30pm OTT @ BUF", ("OTT", "BUF")),
        ("H02: 7:30pm CHI @ FLA", ("CHI", "FLA")),
        ("H03: 10pm SJ @ LA", ("SJ", "LA")),
        ("B01: 7pm ATL @ WAS", ("ATL", "WAS")),
        ("B05: 7:30pm MIL @ BOS", ("MIL", "BOS")),
        ("B12: 9:30pm DAL @ SA", ("DAL", "SA")),
        ("B14: 10:30pm MIA @ LAC", ("MIA", "LAC")),
        ("UNC vs MIA", ("UNC", "MIA")),
        ("NBA League Pass 1", None),
        ("ESPN", None),
    ]
    for name, expected in cases:
        got = tg.split_matchup(name)
        label = "%s/%s" % expected if expected else "None"
        check(got == expected, "split %r -> %s" % (name, label), "got %r" % (got,))

    index = {
        "NY": [{"id": "1", "abbreviation": "NY"}],
        "BKN": [{"id": "2", "abbreviation": "BKN"}],
        "OKC": [{"id": "3", "abbreviation": "OKC"}],
    }
    check(tg.resolve_team("NYK", index, [], {}, {})["id"] == "1", "alias: NYK -> NY")
    check(tg.resolve_team("BRK", index, [], {}, {})["id"] == "2", "alias: BRK -> BKN")
    check(tg.resolve_team("OKL", index, [], {}, {})["id"] == "3", "alias: OKL -> OKC")
    check(tg.resolve_team("ZZZ", index, [], {}, {}) is None, "unknown token stays unresolved")


def test_league_inference():
    """Only leagues that can host a matchup should be fetched."""
    print("== league inference ==")

    def league_index(teams):
        index, teams_by_abbr = {}, {}
        for abbr, team_id in teams.items():
            team = {"id": team_id, "abbreviation": abbr}
            index[abbr] = [team]
            teams_by_abbr[abbr] = team
        return index, list(teams_by_abbr), teams_by_abbr

    per_league = {
        "nba": league_index({"BOS": "1", "NYK": "2"}),
        "nhl": league_index({"BOS": "3", "TOR": "4"}),
        "mlb": league_index({"NYY": "5", "BAL": "6"}),
    }
    channel = {"attrs": {}, "name": "B01: 7:30pm BOS @ NYK"}

    check(tg.infer_leagues([(channel, "BOS", "NYK")], per_league, {}) == ["nba"],
          "shared abbreviation narrows to one league (BOS @ NYK -> nba)",
          str(tg.infer_leagues([(channel, "BOS", "NYK")], per_league, {})))
    check(tg.infer_leagues([(channel, "BOS", "TOR")], per_league, {}) == ["nhl"],
          "BOS @ TOR -> nhl")
    check(tg.infer_leagues([(channel, "NYY", "BAL")], per_league, {}) == ["mlb"],
          "NYY @ BAL -> mlb")
    check(tg.infer_leagues([(channel, "ZZZ", "QQQ")], per_league, {}) == [],
          "unresolvable matchup infers no league")
    both = tg.infer_leagues([(channel, "BOS", "NYK"), (channel, "BOS", "TOR")],
                            per_league, {})
    check(both == ["nba", "nhl"], "several matchups union their leagues", str(both))
    check(tg.infer_leagues([(channel, "NYK", "BRK")], per_league, {}) == [],
          "one unresolved token infers nothing")

    check(tg.resolve_league_selection("auto") == list(tg.LEAGUES),
          "auto builds the index for every league")
    check(tg.resolve_league_selection("all") == list(tg.LEAGUES), "all == auto")
    check(tg.resolve_league_selection("nba,nhl") == ["nba", "nhl"],
          "an explicit league list is honoured")
    try:
        tg.resolve_league_selection("nba,curling")
        check(False, "an unknown league is rejected")
    except SystemExit:
        check(True, "an unknown league is rejected")


def test_xtream():
    """Xtream mapping, using canned API payloads so no credentials are needed."""
    print("== xtream mapping ==")
    streams = [
        {"name": "UNC vs MIA", "stream_id": 101, "epg_channel_id": "",
         "stream_icon": "", "category_id": "5"},
        {"name": "ESPN", "stream_id": 202, "epg_channel_id": "espn.us",
         "stream_icon": "http://x/logo.png", "category_id": "1"},
    ]
    categories = [
        {"category_id": "5", "category_name": "NCAA Football"},
        {"category_id": "1", "category_name": "Sports"},
    ]
    info = {"user_info": {"username": "u"}, "server_info": {"url": "h"}}
    original = tg.fetch

    def fake_fetch(url, timeout=30, cache_dir=None, cache_ttl=1800, cache_name=None):
        import json as _json
        if "action=get_live_streams" in url:
            return _json.dumps(streams).encode()
        if "action=get_live_categories" in url:
            return _json.dumps(categories).encode()
        if "player_api.php" in url:
            return _json.dumps(info).encode()
        raise AssertionError("unexpected fetch: " + url)

    tg.fetch = fake_fetch
    try:
        urls = tg.xtream_urls("192.168.1.9:8080", "u", "p")
        check(urls["live"] == "http://192.168.1.9:8080/live/u/p/",
              "xtream: live base url built", urls["live"])
        check(urls["xmltv"] == "http://192.168.1.9:8080/xmltv.php?username=u&password=p",
              "xtream: xmltv url built")

        channels, header = tg.load_xtream("192.168.1.9:8080", "u", "p", "ts", None)
        check(len(channels) == 2, "xtream: channels parsed", str(len(channels)))
        check(channels[0]["url"] == "http://192.168.1.9:8080/live/u/p/101.ts",
              "xtream: stream url from stream_id", channels[0]["url"])
        check(channels[1]["attrs"]["tvg-id"] == "espn.us",
              "xtream: epg_channel_id used as tvg-id")
        check(channels[0]["attrs"]["tvg-id"] == "",
              "xtream: event channel keeps empty tvg-id")
        check(channels[0]["attrs"]["group-title"] == "NCAA Football",
              "xtream: category mapped to group-title")
        check(header.startswith("#EXTM3U"), "xtream: playlist header emitted")

        masked = tg.mask_url("http://h:8080/xmltv.php?username=Matty&password=s3cret&x=1")
        check("<host>" in masked, "mask_url hides the host", masked)
        check("Matty" not in masked, "mask_url hides the username", masked)
        check("s3cret" not in masked, "mask_url hides the password", masked)
        check(masked.endswith("&x=1"), "mask_url keeps non-secret query params", masked)
    finally:
        tg.fetch = original


def test_slot_ids():
    """Guide channel ids must survive the nightly event-channel rename."""
    print("== stable slot ids ==")
    cases = [
        ({"attrs": {}, "name": "H01: 7:30pm OTT @ BUF"}, "tg.h01"),
        ({"attrs": {}, "name": "H03: 10pm SJ @ LA"}, "tg.h03"),
        ({"attrs": {}, "name": "B14: 10:30pm MIA @ LAC"}, "tg.b14"),
        ({"attrs": {}, "name": "ESPN"}, "tg.espn"),
    ]
    for channel, expected in cases:
        got = tg.channel_key(channel, "slot")
        check(got == expected, "slot id for %r -> %s" % (channel["name"], expected), "got %r" % (got,))

    tagged = {"attrs": {"tvg-id": "espn.us"}, "name": "H01: 7:30pm OTT @ BUF"}
    check(tg.channel_key(tagged, "slot") == "espn.us",
          "provider tvg-id still wins over a slot code")
    plain = {"attrs": {}, "name": "ESPN"}
    check(tg.channel_key(plain, "name") == "tg.espn", "name mode unchanged")

    renamed_a = {"attrs": {}, "name": "H01: 7:30pm OTT @ BUF"}
    renamed_b = {"attrs": {}, "name": "H01: 8:00pm MIA @ NYR"}
    check(tg.channel_key(renamed_a, "slot") == tg.channel_key(renamed_b, "slot"),
          "rename does not change the slot id")
    check(tg.channel_key(renamed_a, "name") != tg.channel_key(renamed_b, "name"),
          "name mode does change across a rename (why slot mode exists)")


def main():
    print("== fixture ==")
    league, events = pick_events()
    if not events:
        print("FAIL: ESPN returned no events in the window")
        return 1
    print("  league=%s events=%d" % (league, len(events)))

    picks = events[:3]
    names = []
    lines = ['#EXTM3U url-tvg="http://provider.example/epg.xml"']
    for index, event in enumerate(picks, 1):
        name = "%s vs %s" % (event["away"]["abbr"], event["home"]["abbr"])
        names.append(name)
        lines.append('#EXTINF:-1 tvg-id="" tvg-name="%s" group-title="TEST",%s' % (name, name))
        if index == 1:
            lines.append("#EXTVLCOPT:http-user-agent=TestUA/1.0")
            lines.append("#EXTGRP:TEST")
        lines.append("http://example.invalid/stream/%d" % index)
    lines.append('#EXTINF:-1 tvg-id="cnn.us" tvg-name="CNN" group-title="News",CNN')
    lines.append("http://example.invalid/stream/cnn")
    lines.append('#EXTINF:-1 tvg-id="" tvg-name="NBA League Pass 1" group-title="NBA",NBA League Pass 1')
    lines.append("http://example.invalid/stream/lp1")
    print("  event channels under test: %s" % ", ".join(names))

    workdir = tempfile.mkdtemp(prefix="teamguide-test-")
    m3u_path = os.path.join(workdir, "in.m3u")
    with open(m3u_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    outdir = os.path.join(workdir, "out")

    print("== run ==")
    rc = tg.main(["--m3u", m3u_path, "--out-dir", outdir, "--leagues", league,
                  "--cache-dir", CACHE, "--days-back", "2", "--days-ahead", "10"])
    check(rc == 0, "tool exited cleanly", "rc=%s" % rc)

    guide_path = os.path.join(outdir, "guide.xml")
    playlist_path = os.path.join(outdir, "playlist.m3u")
    check(os.path.exists(guide_path), "guide.xml written")
    check(os.path.exists(playlist_path), "playlist.m3u written")
    if not os.path.exists(guide_path):
        return 1

    print("== guide ==")
    root = ET.parse(guide_path).getroot()
    channel_ids = {c.get("id") for c in root.findall("channel")}
    programmes = root.findall("programme")
    check(len(programmes) >= len(picks), "at least one programme per event channel",
          "%d programmes for %d picks" % (len(programmes), len(picks)))

    titles = {(p.findtext("title") or "") for p in programmes}
    for name in names:
        expected_key = "tg." + tg.slug(name)
        token = name.split(" vs ")[0].upper()
        check(expected_key in channel_ids, "channel id present for %r" % name, expected_key)
        check(any(token in t.upper() for t in titles),
              "programme title mentions a real team for %r" % name)

    stale = [t for t in titles if t.lower() in ("college football", "nba basketball", "")]
    check(not stale, "no generic placeholders emitted as titles", str(stale))

    print("== safe to publish ==")
    with open(guide_path, "r", encoding="utf-8", errors="replace") as fh:
        guide_text = fh.read().lower()
    check("password=" not in guide_text, "guide carries no password parameter")
    check("username=" not in guide_text, "guide carries no username parameter")
    check("/live/" not in guide_text, "guide carries no provider stream paths")

    print("== rewritten playlist ==")
    with open(playlist_path, "r", encoding="utf-8") as fh:
        out_text = fh.read()
    for name in names:
        expected_key = "tg." + tg.slug(name)
        check(('tvg-id="%s"' % expected_key) in out_text, "tvg-id injected for %r" % name)
    check('tvg-id="cnn.us"' in out_text, "existing provider tvg-id preserved (CNN)")
    check("NBA League Pass 1" in out_text, "non-event channel kept in playlist")
    check('url-tvg="http://provider.example/epg.xml"' in out_text,
          "EXTM3U header attributes preserved (url-tvg)")
    check("#EXTVLCOPT:http-user-agent=TestUA/1.0" in out_text,
          "per-stream directive preserved (#EXTVLCOPT)")
    check("#EXTGRP:TEST" in out_text, "group directive preserved (#EXTGRP)")

    print("== merge: provider generic entry must be replaced ==")
    first = picks[0]
    key = "tg." + tg.slug(names[0])
    provider = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
        '  <channel id="%s"><display-name>%s</display-name></channel>\n'
        '  <programme start="%s" stop="%s" channel="%s"><title lang="en">College Football</title></programme>\n'
        '  <channel id="cnn.us"><display-name>CNN</display-name></channel>\n'
        '  <programme start="%s" stop="%s" channel="cnn.us"><title lang="en">The Situation Room</title></programme>\n'
        '</tv>\n'
    ) % (key, names[0], tg.xmltv_time(first["start"]), tg.xmltv_time(first["end"]), key,
         tg.xmltv_time(first["start"]), tg.xmltv_time(first["end"]))
    provider_path = os.path.join(workdir, "provider.xml")
    with open(provider_path, "w", encoding="utf-8") as fh:
        fh.write(provider)

    merged_dir = os.path.join(workdir, "merged")
    rc = tg.main(["--m3u", m3u_path, "--out-dir", merged_dir, "--leagues", league,
                  "--cache-dir", CACHE, "--days-back", "2", "--days-ahead", "10",
                  "--merge", provider_path, "--no-m3u"])
    check(rc == 0, "merged run exited cleanly", "rc=%s" % rc)

    merged = ET.parse(os.path.join(merged_dir, "guide.xml")).getroot()
    merged_titles = [p.findtext("title") or "" for p in merged.findall("programme")]
    check("College Football" not in merged_titles,
          "provider's generic 'College Football' replaced", str(merged_titles[:6]))
    check("The Situation Room" in merged_titles,
          "unrelated provider programme preserved")
    lead = names[0].split(" vs ")[0].upper()
    check(any(lead in t.upper() for t in merged_titles),
          "real matchup present in merged guide")

    test_naming()
    test_slot_ids()
    test_league_inference()
    test_xtream()

    print()
    if failures:
        print("FAILED (%d): %s" % (len(failures), "; ".join(failures)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
````

### `.gitignore`

````text
# Generated output - never commit
out/
playlist.m3u
guide.xml

# ESPN day caches
cache/
.cache/

# Local credentials - never commit
teamguide.env
.env
.env.*

# SSH helper used for deployment
.askpass.sh

# Python
__pycache__/
*.pyc
````

## Verification

Three layers, in increasing order of what they prove.

**Static.** `python -m py_compile teamguide.py test/smoke.py` proves the files
parse. `yaml.safe_load` on the workflow proves the triggers, jobs and cron parse
and that `on:` was not silently read as the boolean `True`.

**Behavioural.** `python test/smoke.py` builds a playlist from live ESPN
matchups, runs the tool end to end, and then checks the result: that decoration
is stripped, that both matchup forms split, that aliases apply last, that a
generic provider entry is replaced while an unrelated one survives, that
per-stream directives and the `EXTM3U` header attributes survive the rewrite,
that the guide contains no credential pattern, and that stable slot ids do not
change across a rename. It prints `ALL CHECKS PASSED` on success.

**CI.** The workflow itself checks the artefact before publishing: valid XML,
every programme pointing at a declared channel, at least ten enhanced channels,
and no `password=`, `username=` or `/live/` anywhere in the output directory. A
build that fails any of these is not deployed, and the previous guide stays live.

The one thing none of this covers is whether a particular player persists a
manual EPG assignment across an id change. That is player behaviour, and it is
why the ids are made stable rather than assumed stable.

## Configuring the player

TiviMate is the reference; anything that reads M3U and XMLTV behaves similarly.

- **Playlist:** the provider's own (Xtream login or M3U URL). Never a generated
  one — see [Non-negotiables](#non-negotiables).
- **EPG source 1:** the provider's EPG.
- **EPG source 2:** the Pages URL above. Multiple sources per playlist are
  merged; this is a premium feature in some builds.
- **Event channels:** they arrive with no `tvg-id`, so each needs its EPG
  assigned once, from the guide above.

That last step is small but not optional, and it is why ids look like this:

| Provider channel name | Guide channel id |
| --- | --- |
| `H01: 7:30pm OTT @ BUF` | `tg.h01` |
| `H03: 10pm SJ @ LA` | `tg.h03` |
| `B14: 10:30pm MIA @ LAC` | `tg.b14` |
| `UNC vs MIA` (no slot code) | `tg.unc-vs-mia` |

Assign once. Tomorrow the provider renames `H01` to a different matchup, the
guide updates the programmes under `tg.h01`, and the assignment still holds.
Channels carrying a provider `tvg-id` keep it — an existing correct binding is
never overridden by a slot code.

## Operating it

| Task | How |
| --- | --- |
| Scheduled run | hourly at `:17`; an unchanged guide is not republished |
| Run now | Actions → EPG guide → Run workflow |
| Change the horizon | `--days-ahead` in the workflow (default 14) |
| Change the leagues | `--leagues` (default `auto`: inferred from the channel names; `all` or an explicit list also work) |
| Rotate the provider password | update the `XTREAM_PASS` secret only |
| Add a team alias | `--aliases '{"NYK": "NY"}'`; applied only after real matching fails |
| Diagnose a single channel | run locally against the same playlist and read the `matched channels: N / M` line |

Scheduled workflows can start late — usually minutes, occasionally longer. For
an EPG that is irrelevant; for a same-day change, dispatch manually.

The schedule is hourly rather than six-hourly because the provider replaces its
event channels once a day at a time that moves: with a wider interval a slot can
show yesterday's matchup for hours. The extra runs are not wasteful — the team
index is cached for a day, only the leagues a matchup can belong to are fetched,
and a run whose `guide.xml` is byte-identical to the live one skips the push, so
Pages rebuilds only when something actually changed.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Build fails at "Refuse to publish credentials" | something in the output matched a credential pattern | find what produced it; almost always `--no-m3u` was dropped. Do not delete the step |
| Build fails "guide has no programmes" | ESPN returned nothing for the window | check the panel is reachable and `XTREAM_HOST` includes the scheme |
| Build fails "only N enhanced channels" | event channels were not recognised | compare the names against the split cases in `test/smoke.py` |
| Pages URL 404 | Pages source is not "Deploy from a branch" on `gh-pages`, or no run has published yet | set the source, then re-run the workflow |
| Guide publishes but the grid is unchanged | the player is still pointing at the old EPG source | add the Pages URL and refresh the guide |
| Event channel still shows "No information" | its EPG is not assigned | assign it once; confirm the id is `tg.`-prefixed |
| Wrong team shown on a channel | token resolved in the wrong league | verify per-league resolution first; only then add an alias |
| Schedule never fires | repository inactive, or the workflow is disabled | re-enable it, or dispatch manually |

## Limits and non-goals

- **It enhances; it does not replace.** The provider's guide remains the source
  for every channel that is not an event channel.
- **ESPN is the only sport source.** Leagues it does not carry cannot be
  resolved, and the tool leaves those channels untouched rather than guessing.
- **Only the guide is hosted.** The playlist is generated locally when needed
  and never leaves the machine, because it cannot.
- **No player automation.** The 18 event channels need one manual assignment
  each; there is no API to script it.
- **Merged guides can carry duplicate channel declarations** from the provider
  feed. Players tolerate it; it is cosmetic.
- **Provider logo URLs may survive the merge.** The guard blocks credentials,
  not the provider's own host appearing in an icon URL. `--no-merge` removes it
  at the cost of the provider's guide data for non-event channels.

## Provenance

Generated from a working deployment against a live Xtream Codes panel. The
reference run: 776 channels parsed, 18 event-style channels, 18 matched, 23
programmes emitted for them, 542 events fetched across 102 day/league requests,
merged into a guide of 38,745 programmes in 10.4 MB.

The tool was written after evaluating two heavier alternatives. `teamarr` and
`Dispatcharr` both carry a data model built around channel management, DVR and
VOD, and key their channels in a way that does not bind in TiviMate. Neither was
forked: the required behaviour is a few hundred lines of data manipulation, and
wrapping it in a channel manager bought complexity that was never used.

### File manifest

The embedded copies are generated from disk and checked by
`python build_manuscript.py --check`. If that check fails, the manuscript is
stale and must be regenerated rather than edited.

| File | Bytes | SHA-256 (first 16) |
| --- | --- | --- |
| `teamguide.py` | 32089 | `5874e908f449b91f` |
| `.github/workflows/guide.yml` | 6474 | `71eb73502795afc7` |
| `test/smoke.py` | 14778 | `9495bb3f6701097f` |
| `.gitignore` | 232 | `05c7ad361ccd3924` |
