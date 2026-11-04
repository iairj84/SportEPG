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
    test_xtream()

    print()
    if failures:
        print("FAILED (%d): %s" % (len(failures), "; ".join(failures)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
