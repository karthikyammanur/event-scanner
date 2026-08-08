# Event Scanner

Finds US tech company events that students can actually apply to (hackathons,
summits, insight programs, fellowships, externships) and emails a digest of new
ones every few hours. Internship-leading programs are listed first.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill it in for local runs. For the
   scheduled runs, set the same values as GitHub Actions repository secrets
   under Settings, Secrets and variables, Actions:

   | Secret | Needed for | Without it |
   | --- | --- | --- |
   | `GMAIL_USER` | SMTP delivery | No email is sent |
   | `GMAIL_APP_PASSWORD` | SMTP delivery | No email is sent |
   | `DIGEST_TO` | Recipient | Falls back to `GMAIL_USER` |
   | `ANTHROPIC_API_KEY` | Extraction | Falls back to deterministic filtering |
   | `BRAVE_API_KEY` | Broad discovery | The `brave` source is skipped |

   Use a Gmail **app password**, not the account password. It requires 2FA on
   the account.

3. Try it without sending anything:

   ```bash
   python scanner.py --dry-run --source=greenhouse
   ```

## Running

```bash
python scanner.py --dry-run     # print the digest, send nothing, save nothing
python scanner.py               # full run, sends the digest, updates state
```

The scheduled workflow runs every 4 hours. You can also trigger it by hand from
the Actions tab with "Run workflow".

## How it decides what to send

Four discovery sources feed a two-stage filter. A cheap deterministic pass drops
obvious job postings, then one LLM call per batch adjudicates the rest and fills
in dates and location. Three hard filters apply: US-based or virtual, tech
related, and an actual event rather than a job posting.

Events already emailed are recorded in `seen.json`, which the workflow commits
back after each run. That file is what guarantees nothing is emailed twice, so
avoid deleting it. It cannot be gitignored: each scheduled run starts on a fresh
machine with only the repo, so the committed file is the only state that
survives between runs.

Because this repo is public, `seen.json` stores only opaque ID hashes and
timestamps. It never records event names, companies, or links, so nobody can
read it to see what you are applying to.

Out-of-state in-person events with no stated travel support are **flagged, not
dropped**, so you can decide for yourself.

## Sources

| Source | What it covers |
| --- | --- |
| `greenhouse`, `lever`, `ashby` | Event-shaped listings on public ATS boards |
| `brave` | Broad web discovery for companies outside those feeds |
| `devpost` | Public hackathon feed |
| `mlh` | Major League Hacking season events, US only |

Company names are pulled fresh from the SimplifyJobs internship feed each run,
so there is no hardcoded company list to maintain.

Run a single source with `--source=NAME`. A source that breaks is logged and
skipped, it never takes down the run.
