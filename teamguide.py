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
    ap.add_argument("--days-back", type=int, default=1,
                    help="days of finished games to keep; the provider renames "
                         "its slots daily, so long history has nothing to show")
    ap.add_argument("--days-ahead", type=int, default=2,
                    help="days ahead to cover; a slot is named for the game it "
                         "airs today, so a long horizon invents programmes the "
                         "channel will never show and costs a fetch per day per "
                         "league")
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
