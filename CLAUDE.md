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
| `board.py` | Renders the README events table from `seen.json` |
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
- **Gemini retires model IDs for new API keys** while leaving them listed and
  documented. `gemini-2.5-flash` returned 404 "no longer available to new
  users" on a key created after its retirement, and listing models did not
  reveal it, so `resolve_model` verifies by making a real one-token call.
  Prefer the rolling `-latest` aliases over pinned IDs.
- **Gemini structured output is unreliable with nullable type arrays**
  (`["string", "null"]`), so `SCHEMA` in `extract.py` is single-typed
  throughout. Empty string means unknown, and the tri-state travel field is a
  string enum; both convert back in `_one_batch`.
- **The extraction step must log the server's message.** A bare
  `log.exception` hid the 404 above and made a total wipeout look like a clean
  run that simply found nothing. Failed batches now log status plus message,
  back off, and stop after `CIRCUIT_BREAK_AFTER` consecutive failures so a
  systematic problem does not burn the daily quota.
- **date_posted** comes from `first_published` (Greenhouse), `createdAt`
  (Lever, epoch millis), and `publishedAt` (Ashby), normalized by
  `models.to_iso_date`. Devpost and MLH expose no posting date, so it stays
  null there; the LLM is also asked to look for one in free text.
- **Event IDs key on the normalized URL, not the title.** The LLM rephrases
  titles and company names between runs ("JPMorgan Chase" vs "JPMorganChase"),
  which used to mint a new ID for an already-emailed event and re-send it.
  `models.content_key` is a second, looser key (title and company with year and
  filler words stripped) that catches the same event syndicated to several
  university job boards under different URLs. `state.split_new` checks both.

## Freshness

Events that already happened, or listings that went stale, are the other big
noise source. `Event.freshness()` in `models.py` returns `upcoming`, `past`, or
`unknown`, in that precedence:

1. a `start_date` or `application_deadline` in the future wins outright
2. either one in the past means `past`
3. no event date, but `date_posted` older than `STALE_AFTER_DAYS` (60), is `past`
4. no dates at all is `unknown`, which is **kept**, since dropping an event we
   merely could not date is worse than showing it

60 days matches SimplifyJobs, whose `list_updater/listings.py` marks a listing
inactive at `age_in_months >= 2`. Of the 14.4k records they keep, only ~1.5k
stay active, median age 34 days.

`passes_hard_filters` rejects `past`, `digest.send` holds it back from email,
and `board.py` moves it to a collapsed archive rather than deleting it.

Sources differ wildly in what dates they give: Greenhouse, Lever, and Ashby
supply a real posting date for every row, Devpost and MLH supply none but do
give event start dates, and Tavily gives neither by default. That is why
`sources/discovery.py` now asks Tavily for `topic: news` plus `time_range:
year` and reads `published_date`, and decodes LinkedIn activity IDs (the high
41 bits are a Unix ms timestamp) as a fallback. A LinkedIn post from June 2024
was showing up as a current event until that decode landed.

## Audience filters

Target reader is a **US university upperclassman**, so `filters.py` also rejects:

- **Pre-college**: high school, middle school, K-12, teen and youth events.
  Careful with "junior" and "senior", which mean college year levels here, so
  they are not pre-college markers.
- **Non-US organizers on virtual events**: a hackathon listed as "Online" run
  by a non-US university or foundation is out of scope. The location field says
  "Online" and hides the country, so the name and company are checked instead.
- **Unrecognized venues on open feeds**: Devpost and the Tavily discovery
  source accept organizers worldwide, so an in-person event there must show a
  positive US signal ("VITM, Indore" is rejected). ATS boards and MLH keep the
  benefit of the doubt, since those are US company boards and MLH sets a real
  country code, and vague-but-real venues like "RWC HQ" or "SF" appear there.

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

## Public repo and the board

This is a **public board by design**, like SimplifyJobs. `board.py` renders
every event in `seen.json` into a table in README.md between the
`<!-- EVENTS:START -->` / `<!-- EVENTS:END -->` markers, and the workflow
commits README.md alongside seen.json each run. Content outside those markers
is hand written and preserved.

Because the board is rendered from state, `seen.json` stores the **full**
record (name, company, URL, dates, location). It was briefly anonymized to
hash-only when the repo went public with email-only delivery; that was
reversed when the board was added. Entries written during that window have no
details and are skipped by the renderer, which is why state was cleared once
and backfilled.

`seen.json` **cannot be gitignored.** Each Actions run starts on a clean machine
with only the repo, so the committed file is the only thing carrying state
between runs. Ignoring it would make every run re-email everything.

Table cells are escaped in `board._cell`, since event names come from scraped
pages and a stray `|` would otherwise break the table.
