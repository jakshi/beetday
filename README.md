# beetday

A small, single-file client for **your own** Workday tenant's org chart.

Workday's web UI can tell you who someone reports to — slowly, four clicks at a
time. This drives the same undocumented endpoints the browser uses, so you can do
it from a shell, and it can crawl the supervisory-org tree once into a local file
you then search instantly and offline.

```console
$ beetday search "ada"
Ada Lovelace  —  Senior Tech Lead  —  Engineering : Platform II (Grace Hopper)  —  London   →  Grace Hopper <grace@example.com>

$ beetday chain "Ada Lovelace"
Ada Lovelace — Senior Tech Lead
  →Grace Hopper — Senior Engineering Manager
    →Katherine Johnson — VP Engineering
```

## Before you use it

This talks to your employer's HR system with your own logged-in session. It sees
exactly what you can already see in the Workday UI — there is no privilege
escalation here — but `crawl` bulk-exports your whole staff directory, including
email addresses, to a file on your disk. That is the kind of thing security teams
care about, reasonably.

- Check your employer's acceptable-use policy first. Automated access and
  reverse-engineering are commonly restricted, including by Workday's own terms.
- `orgchart.json` is real personal data about your colleagues. Keep it local,
  keep it out of git (the shipped `.gitignore` covers it), don't share it.
- Use a polite `--delay`. The default is 0.3s between calls.

## Install

Needs Python 3.14+ and [uv](https://docs.astral.sh/uv/). The script declares its
own dependencies inline (PEP 723), so there is nothing to install:

```console
$ chmod +x beetday.py
$ ./beetday.py --help
```

Drop it on your `PATH` as `beetday` if you want the short name.

## Auth

There is no API key. Auth is a copy of your browser's live session, which Workday
drops after roughly 90 minutes idle — expect to redo this.

1. Open Workday, press F12, go to **Network**.
2. Right-click any request to your tenant → **Copy → Copy as cURL**.
3. Pipe it in:

```console
$ pbpaste | beetday auth
saved session for tenant 'acme' (worker 247$77) -> ~/.local/share/beetday/session.json
```

Or point it at a saved HAR capture: `beetday auth --har ~/Downloads/workday.har`.

The session file holds live credentials and is written `0600`. Both it and the
cache live in `~/.local/share/beetday`; override with `BEETDAY_HOME`.

> A `.har` capture also contains your session cookie. Delete it when you're done.

## Commands

| Command | What it does |
|---|---|
| `auth` | Save or refresh the browser session |
| `search <query>` | Live people/org search |
| `manager <name>` | Who someone reports to |
| `chain <name>` | The full management chain above someone |
| `crawl` | Walk the whole org tree into the local cache |
| `find <query>` | Search the cache — offline, instant, diacritic-insensitive |

`find` folds diacritics, so `find nguyen` matches `Nguyễn` and `find cong`
matches `Công`.

## Tenant-specific constants

The org-chart navigator endpoint wants three opaque ids that **differ per
tenant**. The defaults at the top of `beetday.py` are placeholders and
won't match yours. To find your own:

1. Open your org chart in Workday with devtools **Network** recording.
2. Find the `POST` to `/<tenant>/navigable/<id>.htmld`.
3. Read `initial-step` and `navigable-instance-set-id` off the form body, and the
   root org from the `<id>` in the path.

```console
$ beetday crawl --root 2500$9 --initial-step 2997$42 --instance-set-id 1$99
```

The `247$` / `2500$` prefixes in the source are Workday instance *class* ids
(Worker, Supervisory Organization) and appear to be the same everywhere.

## Development

```console
$ python3 -m unittest test_beetday -v
```

Tests cover the pure parsing, caching and session-extraction layers; the network
layer is exercised by hand against a live session. Every fixture is invented — no
real person, tenant or host appears in this repo.

## Caveats

Undocumented endpoints, reverse-engineered from browser traffic. Workday can
change them without notice and nothing here is supported by anyone. `crawl`
across a large org takes a while: one call per sub-org, then one per person for
email enrichment. It checkpoints as it goes and resumes from cached emails.

## License

[GNU AGPL-3.0-or-later](LICENSE). Forks and modified versions stay under the same
license, and if you offer a modified version to users over a network you must
offer them its source too.
