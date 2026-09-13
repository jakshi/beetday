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

## Tenant-specific ids

The org-chart navigator needs two ids that **differ per tenant**: an "Org Chart"
task, and your root supervisory organization. `beetday auth` finds both and stores
them in the session, so there is nothing to configure:

```console
$ pbpaste | beetday auth
saved session for tenant 'acme' (worker 247$77) -> ~/.local/share/beetday/session.json
discovered org chart task 2997$42, root org 2500$9
```

It searches your tenant for an Org Chart task, confirms the navigator actually
accepts it, then walks `PARENT` up from your own org until there is no parent
left. If discovery fails — an unusual tenant, a renamed task — override it:

```console
$ beetday crawl --root 2500$9 --initial-step 2997$42
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
