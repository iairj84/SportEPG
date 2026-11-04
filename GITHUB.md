# Running the guide from GitHub

The Pi is no longer required. GitHub Actions generates `guide.xml` every six
hours and publishes it to GitHub Pages, which is public HTTPS with no VPN and no
home hardware in the path.

## What is published, and what is not

| File | Contents | Published? |
| --- | --- | --- |
| `guide.xml` | Channel names, programme titles, times, ESPN logos | **Yes** |
| `playlist.m3u` | Every stream URL, each containing your provider **username and password** | **Never** |

The workflow runs with `--no-m3u`, so the playlist is not even generated. A
guard step greps the output for `password=`, `username=` and `/live/` and fails
the build rather than publish a leak. The provider's `generator-info-url`
attribute is stripped during the merge, so the guide does not advertise the
provider endpoint either.

Use your **provider's own playlist** in TiviMate. That never goes through
GitHub, and it keeps working when nothing of ours is running.

## Setup

1. Create a repository (public is required for Pages on a free account).
2. Push the contents of this directory as the repository root. `teamguide.py`,
   `.github/workflows/guide.yml`, `.gitignore` and this file are all that is
   needed — the tool is stdlib-only, so there is nothing to install.
   `MANUSCRIPT.md` is the long-form version of this document, complete enough to
   bootstrap a project from nothing.
3. **Settings → Secrets and variables → Actions → New repository secret**, three
   times:

   | Name | Value |
   | --- | --- |
   | `XTREAM_HOST` | your panel, e.g. `http://host:8080` |
   | `XTREAM_USER` | provider username |
   | `XTREAM_PASS` | provider password |

4. **Actions → EPG guide → Run workflow** to do the first build without waiting
   for the schedule. The run also creates the `gh-pages` branch Pages serves.
5. **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
   then **Branch: `gh-pages`**, folder **`/ (root)`**, and **Save**.

The guide then lives at:

```
https://<your-user>.github.io/<repo>/guide.xml
```

## TiviMate

- Playlist: your provider's playlist (Xtream login or M3U URL) — unchanged.
- EPG: provider EPG, **plus** the GitHub Pages URL above as a second source.
- The 18 event channels (`H01: 7:30pm OTT @ BUF` and friends) still arrive with
  no `tvg-id`, so assign their EPG once each, from the guide above.

That assignment is what `--id-mode slot` exists for. The provider renames those
channels nightly, so an id built from the full name would change every day and
the assignment would break with it. In slot mode the ids are stable:

```
H01: 7:30pm OTT @ BUF  ->  tg.h01
B14: 10:30pm MIA @ LAC ->  tg.b14
```

Assign once, and tomorrow's rename lands on the same id.

## Things to know

- Scheduled workflows can start late (usually minutes, occasionally longer).
  Irrelevant for EPG; run the workflow manually if you want it now.
- GitHub disables scheduled workflows in a **public** repository after 60 days
  with no repository activity, which would silently stop the guide. The
  `keepalive` job writes `.keepalive` and commits it when it is older than 40
  days, resetting that clock, so the repository can stay public and free instead
  of moving to a paid plan just to host Pages from a private repository. If the
  job ever fails, the run goes red while the guide still publishes.
- Publishing writes the `gh-pages` branch rather than calling the Pages
  deployment API. Each run force-replaces that branch with a single commit
  holding `guide.xml` and an empty `.nojekyll`, so it never accumulates history
  and never waits on a deployment queue that can wedge. The Pages source must be
  "Deploy from a branch" on `gh-pages`; "GitHub Actions" publishes nothing.
- The schedule runs hourly, because the provider replaces its event channels
  once a day at a time that moves; a wider interval can leave yesterday's
  matchup showing for hours. A run whose `guide.xml` is byte-identical to the
  published one skips the push, so Pages only rebuilds when something changed.
- Which leagues to fetch is inferred from the channel names, not configured. A
  channel like `B01: 7:30pm BOS @ NYK` fetches the NBA scoreboard and `BOS @
  TOR` the NHL one, and the category label ("Baseball" versus "MLB") never
  matters because resolution is by team. Seasons turning over need no edit.
- Pages on a **private** repository needs a paid plan. Public is fine here
  because the guide carries no credentials.
- The guide still contains whatever the provider's XMLTV supplies for channels
  we do not enhance — display names and, if the provider sets them, logo URLs
  pointing at the provider's own host. The guard blocks credentials, not that.
  If you want it gone, run with `--no-merge` at the cost of losing the
  provider's guide data for the non-event channels.
- Keep a copy of the repository private-local too if you add provider details to
  any tracked file. Secrets belong in repository secrets only.
