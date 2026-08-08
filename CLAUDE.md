# Event Scanner

Discovers US student-facing tech events (hackathons, summits, insight programs,
fellowships, externships) and emails Karthik a digest of new ones. Runs on a
GitHub Actions cron, not on his machine.

## Stack

Python 3.12, `requests` only for fetching (no headless browser by design, it
burns Actions minutes). Extraction uses `gemini-2.5-flash` via the `google-genai` SDK.
Delivery is Gmail SMTP with an app password.

## Commands

```bash
python scanner.py --dry-run --source=greenhouse   # print, no email, no state
python scanner.py --dry-run                       # all sources, no email
python scanner.py                                 # full run, sends digest
python -m pytest tests/ -q                        # run before committing
```

Useful flags: `--max-companies` caps ATS board targets, `--source-budget` caps
wall clock seconds per source, `-v` for debug logging.

## Layout

| File | Role |
| --- | --- |
| `scanner.py` | Orchestration and CLI |
| `models.py` | `Event` data contract, stable ID hashing |
| `filters.py` | Deterministic prefilter and the three hard filters |
| `extract.py` | Batched LLM call, also enforces hard filters |
| `state.py` | `seen.json` load/save/prune, the dedupe guarantee |
| `digest.py` | Email rendering and SMTP delivery |
| `sources/` | One module per discovery source |

Run order: discover, prefilter, drop already-seen, extract, email, record state.
The state check runs **before** extraction, so a repeat run spends no tokens.

## Boundaries

- **Never commit a secret.** Secrets come from Actions secrets or a gitignored
  `.env`. See `.env.example`.
- **Never use an em dash** in anything Karthik reads, including subject lines.
  `digest.scrub()` strips them and `digest.assert_no_em_dash()` fails loudly.
  Use commas, periods, or restructure the sentence.
- **Never silently drop an out-of-Texas event** for lacking travel credit info.
  Flag it. His stated preference is "prefer travel credit," not "require."
- **Never let one broken source crash the run.** Sources go through
  `sources.base.run_source`, which swallows and logs any exception.
- **Ask first** before creating the repo or pushing the first commit, before
  setting any Actions secret, and before switching email away from SMTP.
- **Always** run tests before committing, and keep each source its own module.

## Gotchas learned the hard way

- **Devpost** 403s without a browser `User-Agent`. `sources/base.py` sets one
  globally. Its dates are human strings ("May 19 - Aug 17, 2026"), not ISO.
- **MLH** is not lazy-loaded despite appearances. The season page embeds the
  full season as escaped JSON with real event names, `status`, and
  `venueAddress.country`. There is also schema.org microdata, but it lacks
  event names, so the JSON blob is the primary path and microdata is fallback.
- **Lever** tokens 404 often; token guessing must tolerate that.
- **Ashby** payloads can exceed 2MB, hence the size guard in `get_json`.
- **speedyapply** ships no `listings.json`, only TS build scripts. It yields no
  company names. SimplifyJobs alone supplies the universe (~600 names, ~240
  with detectable ATS boards).
- **Multi-location strings** like `"London, UK; ...; San Francisco, CA"` are
  common. One US option qualifies, so US signals are checked before non-US.
- **Job postings with event words** are the hard case. `looks_like_job_posting`
  checks the *head* of the title (before the first comma or dash), which is what
  keeps "University Recruiter, Hackathon and Campus Events" out.
- **Handshake is off limits** in any form. Their ToS forbids automated
  collection. Eventbrite's search API is dead and Meetup's requires a paid plan.

## Testing

`tests/fixtures/labeled_postings.json` holds real-shaped events and non-events,
including adversarial ones ("Senior Software Engineer, Summit Platform"). The
binding rule: **zero false negatives on the true events**. False positives are
tolerable since Karthik reads the digest as the final check.

## Actions budget

The repo is **public**, so Actions minutes are unlimited and the cron frequency
is not budget constrained. A full run still measures only ~36s at the 4-hour
cron. (For reference, on a private repo this would be ~220 of 2000 free minutes.)

## Third-party API budgets

Two sources hit a metered API. Both are sized to stay inside their free tier at
zero cost, since that is a hard preference, not a target to optimize toward.

- **Gemini** (`gemini-2.5-flash`, extraction): free tier is generous for this
  volume, well above what six small-batch runs a day needs.
- **Tavily** (`discovery.py`, broad web search): free tier is **1,000 API
  credits/month, recurring, no card required** (verified against their
  pricing page directly, not assumed). A basic search costs 1 credit.
  `QUERIES_PER_RUN = 5` in `sources/discovery.py` keeps 6 runs/day at 900
  credits/month, leaving margin. Raising that constant raises the monthly
  credit spend proportionally, check the math before changing it.

Note: an earlier version of this file assumed Brave Search's free tier was a
flat 2,000 queries/month. That was stale, Brave's current terms are $5/month
in free credits at $5 per 1,000 requests, not nearly enough for this
workload's query volume without paying. The discovery source was switched
to Tavily for that reason.

## Public repo implications

`seen.json` is committed on every run, so it is world readable. It therefore
stores **only opaque event ID hashes and timestamps**, never event names,
companies, or URLs. Dedupe needs nothing else. Set `STATE_VERBOSE=1` locally if
you want readable entries while debugging, but never in the workflow.

`seen.json` **cannot be gitignored.** Each Actions run starts on a clean machine
with only the repo, so the committed file is the only thing carrying state
between runs. Ignoring it would make every run re-email everything.
