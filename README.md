# teamguide

Builds a real TV guide for IPTV sports event channels.

Your provider names channels `UNC vs MIA` and gives them no EPG data at all; the
channels that *do* have data show a generic `College Football`. This tool fixes
both: it matches each event channel to the actual ESPN fixture and emits XMLTV
with the real matchup and start time.

- **No dependencies.** Python standard library only, runs on Python 3.8+.
- **No database, no daemon, no Docker.** One script, one cron entry.
- **No aliases table.** ESPN's abbreviations *are* the channel names (`UNC` is
  North Carolina, `MIA` is Miami), so matching is a dictionary lookup.
- **Emits the playlist too.** It rewrites your M3U with `tvg-id`s injected, so
  TiviMate attaches the guide with zero manual mapping.

## Quick start

```bash
# report only, writes nothing
python3 teamguide.py --m3u 'http://provider/get.php?username=...&password=...&type=m3u_plus' --dry-run

# write out/guide.xml and out/playlist.m3u
python3 teamguide.py --m3u 'http://provider/...m3u_plus' \
                     --merge 'http://provider/xmltv.php?...' \
                     --out-dir out --days-ahead 2
```

`--merge` is optional but recommended: it takes the provider's own XMLTV as the
base, keeps everything (news, movies, entertainment), and **replaces** the
generic entry wherever a real matchup exists. The result is one guide file that
covers your whole lineup.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--m3u` | required | playlist URL or local file |
| `--merge` | off | provider XMLTV to merge and correct |
| `--out-dir` | `out` | where `guide.xml` / `playlist.m3u` go |
| `--leagues` | `auto` (inferred from the channels) | `auto`, `all`, or a list of `cfb nfl nba ncaab wnba nhl mlb mls epl ucl` |
| `--days-back` / `--days-ahead` | `1` / `2` | event window; a slot only airs the game it is named for |
| `--aliases` | none | JSON `{"BAMA": "ALA"}` for provider-specific shorthand |
| `--no-m3u` | off | don't write the rewritten playlist |
| `--dry-run` | off | report matches only |

The run prints what matched and, more usefully, **what didn't and why** — feed
those names back into `--aliases` if your provider uses shorthand ESPN doesn't.

## Xtream Codes

Most panels expose Xtream rather than a plain M3U URL. Point the tool at the
server and it handles the rest:

```bash
python3 teamguide.py \
  --xtream 'http://host:8080' --username 'USER' --password 'PASS' \
  --out-dir out --dry-run
```

With `--xtream`:

- channels come from `player_api.php?action=get_live_streams`, and
  `epg_channel_id` is used **directly** as the `tvg-id` — no guessing from `EXTINF`
- `group-title` is filled from `get_live_categories`
- stream URLs are generated as `/live/<user>/<pass>/<stream_id>.<fmt>`
  (`--stream-format ts|m3u8`)
- `--merge` **defaults to the provider's own `xmltv.php`**, so you get one file
  containing everything plus the corrected sports entries (`--no-merge` opts out)

The generated playlist is complete and self-contained, so TiviMate can use it as
a normal M3U playlist. That is the reliable path: event channels usually have an
empty `epg_channel_id`, so only the rewritten playlist gives TiviMate something
to match the guide against.

Keep credentials out of shell history by putting them in a chmod-600 env file and
sourcing it from a wrapper script (see Scheduling).

## TiviMate

1. Add playlist → **M3U playlist** → point at `http://<pi>:<port>/playlist.m3u`
2. Add EPG → **XMLTV** → point at `http://<pi>:<port>/guide.xml`
3. EPG auto-assignment matches on `tvg-id`, which the rewritten playlist now has.

Serve `out/` over HTTP with whatever you already run. With the nginx instance on
this Pi (port 7136), a `location /epg/ { alias /home/matt/epg/out/; }` block is
enough. A throwaway alternative for testing:

```bash
cd out && python3 -m http.server 9196
```

## Scheduling

```cron
0 */6 * * * cd /home/matt/epg && /usr/bin/python3 teamguide.py \
  --m3u "$M3U_URL" --merge "$XMLTV_URL" --out-dir out >> teamguide.log 2>&1
```

ESPN rejects date ranges and must be queried one day at a time, so a cold run is
about one request per day per league. Results are cached per day: past days for a
week (they never change), upcoming days for an hour. Six-hourly is the sweet spot;
running more often just re-reads the cache.

Keep provider credentials out of the crontab by putting them in a chmod-600 env
file and sourcing it in a small wrapper script.

## Removing Dispatcharr

This tool replaces the reason Dispatcharr was installed. Once `guide.xml` looks
right in TiviMate:

```bash
cd ~/dispatcharr && docker compose down      # stop, keeps the volume
docker compose down -v                        # later: also drop the database
```

That frees roughly 450 MB of the Pi's 905 MB.

## Tests

```bash
python3 test/smoke.py
```

Builds a playlist from live ESPN fixtures, then verifies the guide, the injected
`tvg-id`s, and that a provider's generic `College Football` entry is replaced
while unrelated programmes survive.
