#!/usr/bin/env python3
"""Regenerate MANUSCRIPT.md from the real sources.

The manuscript is never hand-edited. It embeds teamguide.py, the workflow and
the smoke test verbatim, so a new project can bootstrap from a single file and
the embedded copy can never drift from the copy that is tested.

    python build_manuscript.py           # write MANUSCRIPT.md
    python build_manuscript.py --check   # verify the embedded copies match disk
"""

import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "MANUSCRIPT.md")

# (path in repo, language for the fence)
EMBEDDED = [
    ("teamguide.py", "python"),
    (".github/workflows/guide.yml", "yaml"),
    ("test/smoke.py", "python"),
    (".gitignore", "text"),
]


def read(path):
    with open(os.path.join(HERE, path), "r", encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed(path, lang):
    body = read(path)
    # Four-backtick fence: the sources are prose-free but this keeps any
    # triple-backtick the code happens to contain from closing the block.
    return "\n".join(["````" + lang, body, "````"])


def manifest_rows():
    rows = ["| File | Bytes | SHA-256 (first 16) |", "| --- | --- | --- |"]
    for path, _ in EMBEDDED:
        body = read(path)
        raw = len(body.encode("utf-8"))
        rows.append("| `%s` | %d | `%s` |" % (path, raw, digest(body)[:16]))
    return "\n".join(rows)


HEADER = """\
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
"""

AGENT = """\
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
"""

RULES = """\
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
"""

HOW = """\
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
"""

LAYOUT = """\
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
"""

BOOTSTRAP = """\
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
"""

VERIFY = """\
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
"""

PLAYER = """\
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
"""

OPERATE = """\
## Operating it

| Task | How |
| --- | --- |
| Scheduled run | hourly at `:17`; an unchanged guide is not republished |
| Run now | Actions → EPG guide → Run workflow |
| Change the horizon | `--days-ahead` (default 2) and `--days-back` (default 1); a slot is named for the game it airs today, so a long horizon publishes programmes the channel will never show |
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
"""

TROUBLE = """\
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
"""

LIMITS = """\
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
"""

PROVENANCE = """\
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

{manifest}
"""


def compose():
    parts = [
        HEADER,
        AGENT,
        RULES,
        HOW,
        LAYOUT,
        BOOTSTRAP,
        "## The files\n\nThese blocks are the source of truth for a fresh project.\n",
    ]
    for path, lang in EMBEDDED:
        parts.append("### `%s`\n\n%s\n" % (path, embed(path, lang)))
    parts.extend([VERIFY, PLAYER, OPERATE, TROUBLE, LIMITS,
                  PROVENANCE.format(manifest=manifest_rows())])
    return "\n".join(p.strip("\n") + "\n" for p in parts)


def check(text):
    """Every embedded block must match its file on disk."""
    problems = []
    for path, _ in EMBEDDED:
        expected = read(path)
        # find the block introduced by this path's heading
        pattern = re.compile(r"### `%s`\n\n````[a-z]*\n(.*?)\n````" % re.escape(path), re.S)
        match = pattern.search(text)
        if not match:
            problems.append("%s: block not found" % path)
        elif match.group(1) != expected:
            problems.append("%s: embedded copy differs from disk" % path)
    return problems


def main(argv):
    text = compose()

    if "--check" in argv:
        # Verify the file on disk, not merely the in-memory composition.
        if not os.path.exists(OUT):
            print("STALE: MANUSCRIPT.md does not exist")
            return 1
        with open(OUT, "r", encoding="utf-8") as fh:
            on_disk = fh.read()
        problems = check(on_disk)
        if on_disk != text:
            problems.append("MANUSCRIPT.md prose differs from the generator")
        if problems:
            for problem in problems:
                print("STALE: " + problem)
            return 1
        for path, _ in EMBEDDED:
            body = read(path)
            print("ok  %-30s %6d bytes  %s" % (path, len(body.encode()), digest(body)[:16]))
        print("MANUSCRIPT IN SYNC WITH DISK")
        return 0

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    raw = len(text.encode("utf-8"))
    print("wrote MANUSCRIPT.md (%d lines, %d bytes)" % (text.count("\n"), raw))
    for path, _ in EMBEDDED:
        body = read(path)
        print("  embedded %-30s %6d bytes  %s" % (path, len(body.encode()), digest(body)[:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
