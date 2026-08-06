# jobseeker

A job search agent built around one specific profile — a Lead QA Engineer / SDET
based in Jakarta — instead of around a keyword box.

It sweeps 11 job sources, normalises everything into one shape, scores each
posting against that profile, enforces the compensation rules that actually
matter, and hands back a ranked shortlist with a dashboard.

The rules it enforces, straight from the brief:

| Region | Salary rule |
|---|---|
| **Indonesia** | Hard floor of **IDR 30,000,000 / month**. Below it, the job is dropped. Not stated? Kept, but flagged for you to verify. |
| **Asia** (excl. Indonesia) | Salary is **not a filter**. A role is judged on the work. |
| **Europe** | Salary is **not a filter**. |
| Elsewhere | No floor, but must be **fully remote**. |

Remote work is preferred and scored accordingly. Onsite roles are only kept for
Indonesia — there is no relocation appetite for a desk in another country.

---

## Quick start

```bash
git clone https://github.com/ariefrahakim/jobseeker.git
cd jobseeker

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py search          # scrape, score, report
python main.py dashboard --open
```

That is the whole loop. No API key needed — the scoring engine is deterministic
Python, and the eight JSON/RSS sources need no credentials.

Two optional extras:

```bash
cp .env.example .env          # then fill in what you have
playwright install chromium   # for the LinkedIn / JobStreet / Glints sources
```

---

## What a run looks like

```
$ python main.py search

  sources    : remoteok, remotive, jobicy, himalayas, arbeitnow,
               weworkremotely, greenhouse, lever, linkedin, jobstreet, glints
  scraped    : 201 postings (193 unique)
  matched    : 16 above threshold
  new to you : 16
  duration   : 39.8s

------------------------------------------------------------------------------
  Europe — no salary floor  (6)
------------------------------------------------------------------------------

  NEW [ 84.2] Senior Quality Engineer
        Lendable · Remote · remote
        salary: not stated (no floor for this region)
        skills: ci/cd, contract testing, docker, github actions, graphql, ...
        https://...

  NEW [ 79.7] Senior QA Engineer with Automation
        Alex Staff Agency · Germany · remote
        salary: EUR 65,000–75,000/year (no floor for this region)
                ≈ IDR 95,333,333/month
        skills: api testing, ci/cd, cypress, jira, playwright, selenium, sql
        https://...
```

Reports land in `output/` as Markdown, HTML, JSON and CSV.

---

## The dashboard

`python main.py dashboard` writes a single self-contained `output/dashboard.html`
— no server, no build step, works offline, fine to open on a phone.

It answers the four questions you actually have each morning:

- **Ready to apply** — scored 70+, not yet acted on, salary already clear
- **New since last run** — what changed since yesterday
- **Salary not stated** — Indonesian roles you need to ask about before applying
- **Applied / interviewing** — what is already in flight

Below the numbers is one sortable, searchable table. Click any column heading to
sort; filter by region, status, work setup or minimum score; search across
titles, companies and matched skills.

---

## How the matching works

Six dimensions, each scored 0–100, then blended by the weights in
`config/profile.yaml`:

| Dimension | Weight | What it measures |
|---|---:|---|
| Skills | 35 | Weighted hits across your stack. Cypress and Playwright count for more than Jira. |
| Title | 30 | Exact match on a target title, an accepted variant, or token overlap. |
| Work setup | 15 | Remote > hybrid > onsite. |
| Seniority | 10 | Reads the posting's level; penalises anything below your floor, rewards real leadership scope. |
| Domain | 5 | Overlap with fintech, logistics, media, banking. |
| Freshness | 5 | A 30-day-old posting is usually already filled. |

Four things bypass scoring entirely and drop the posting outright:

1. **A rejected title** — "Manual Tester", "QA Intern", "Game Tester".
2. **A non-QA title.** A posting that name-drops your whole toolchain under a
   title like "Senior Graphic Designer" is a generalist listing, not a QA role.
   This is a real pattern on aggregator boards, and it out-scores genuine
   matches on skills alone if you let it.
3. **Indonesian salary below IDR 30jt/month**, converted from any currency.
4. **Onsite outside Indonesia**, or a non-target region that is not remote.

Every score is explained in the report, so a surprising ranking is debuggable
rather than mysterious.

### Salary parsing

The floor is only as good as the parser feeding it, so this part gets the most
test coverage. It handles what Indonesian and European postings actually write:

| Posting says | Read as |
|---|---|
| `Rp 35.000.000 - Rp 45.000.000 per bulan` | IDR 35,000,000/month |
| `IDR 30jt - 40jt/bulan` | IDR 30,000,000/month |
| `Gaji 32 juta per bulan` | IDR 32,000,000/month — currency inferred from the Indonesian wording |
| `$120k – $150k a year` | USD 120,000/year → ≈ IDR 163,000,000/month |
| `€75.000 p.a.` | EUR 75,000/year — European thousands separator, not a decimal |
| `$85 per hour` | USD 85/hour → normalised via 173.33 h/month |

Ranges are compared on their **lower bound**, never the midpoint: a floor check
should use the worst case the posting commits to.

---

## Sources

| Source | Type | Notes |
|---|---|---|
| RemoteOK | JSON | Remote-only, strong on engineering |
| Remotive | JSON | Has a real QA category |
| Jobicy | JSON | Geo-filtered: Europe, APAC, Singapore, Germany, NL, UK |
| Himalayas | JSON | ~97k postings, paged in 20s |
| Arbeitnow | JSON | Europe-heavy, exposes visa-sponsorship flags |
| WeWorkRemotely | RSS | Two engineering feeds |
| Greenhouse | ATS | 16 named company boards — the highest-signal source here |
| Lever | ATS | Spotify, Binance |
| LinkedIn | Browser | Guest search needs no account; login only as a fallback |
| JobStreet ID | Browser | Its JSON endpoint sits behind Cloudflare; login enables the fallback |
| Glints | Browser | The one Indonesian board that publishes monthly IDR on the card |

The ATS sources are worth calling out: you choose the companies, so every hit is
somewhere you would actually work, with no ranking algorithm in between. Add
yours to `config/sources.yaml`:

```yaml
greenhouse:
  boards:
    - gitlab
    - xendit
    - your-target-company    # from boards.greenhouse.io/<name>
```

A board that 404s is logged and skipped. **One dead source never fails a run** —
scrapers run concurrently and are isolated from each other.

---

## Optional: Claude on top

The ranking is deterministic and works with no API key. Claude adds judgement
where rules genuinely fall short — reading a wall of prose and noticing that a
posting titled "SDET" describes manual test execution.

```bash
python main.py search --llm      # fit assessment on the top matches
python main.py apply <job-id>    # tailored CV bullets + a cover letter
```

Two design decisions worth knowing:

- **The model never produces the ranking**, only commentary on it. A scoring
  engine you can read and unit-test is what makes the output trustworthy.
- **The application drafter is forbidden from inventing experience.** A
  fabricated bullet is worse than no bullet when it surfaces in an interview.

Uses `claude-opus-5` by default; override with `ANTHROPIC_MODEL`. The system
prompt and profile are cached, so repeat runs are cheap.

---

## Commands

```bash
python main.py search                          # all enabled sources
python main.py search --sources greenhouse lever
python main.py search --llm --min-score 65
python main.py search --format markdown html csv json

python main.py dashboard --open                # build and open the dashboard
python main.py list --status new               # what is waiting
python main.py list --region indonesia --min-score 70
python main.py apply a1b2c3d4                  # draft an application
python main.py status a1b2c3d4 applied         # track progress
python main.py stats                           # database summary
python main.py sources                         # what is on and off
```

Job ids are shown in the reports and on the dashboard; a prefix is enough.

Statuses: `new`, `seen`, `shortlisted`, `applied`, `interviewing`, `rejected`,
`skipped`. A status you set by hand survives every later run — the agent stops
resurfacing roles you have already decided about.

---

## Configuration

Two files, no code changes needed:

**`config/profile.yaml`** — who you are and what you want. Titles, weighted
skills, seniority floor, work preferences, the salary rules per region, FX rates,
scoring weights and thresholds.

**`config/sources.yaml`** — which boards to hit, company ATS boards, search
queries, page limits and politeness delays.

Two knobs worth knowing:

```yaml
scoring:
  min_score: 55          # lower it for a wider net on a quiet week
  min_title_score: 25    # the non-QA title veto
```

---

## Tests

```bash
python -m pytest -q      # 101 tests
```

Coverage is concentrated where mistakes are expensive: salary parsing and floor
enforcement, the hard filters, cross-source dedupe, location normalisation and
storage.

Several of these tests exist because they caught real bugs during development:

- `rm` inside "platform" was matching as Malaysian ringgit, pricing a Berlin
  salary at IDR 14m/month.
- `qa` inside "Qatar" was scoring a Business Analyst role as testing-adjacent.
- "senior" alone gave a Graphic Designer role 50% title overlap with
  "Senior Test Engineer".

---

## Automation

`.github/workflows/daily-search.yml` runs the API-only sources every weekday
morning at 07:00 Jakarta, caches the database between runs so "new since last
run" stays meaningful, and uploads the report as an artifact. Add
`ANTHROPIC_API_KEY` as a repository secret to get fit assessments too.

Locally, cron works just as well:

```cron
0 7 * * 1-5 cd ~/jobseeker && .venv/bin/python main.py search && .venv/bin/python main.py dashboard
```

---

## Layout

```
config/profile.yaml       your profile and all the rules
config/sources.yaml       which boards, which companies
main.py                   CLI
src/models.py             Job, Salary, MatchResult
src/salary.py             parsing, FX normalisation, the floor check
src/matcher.py            the six-dimension scorer and hard filters
src/profile.py            config loading, country and arrangement inference
src/pipeline.py           orchestration, concurrency, dedupe
src/storage.py            SQLite: cross-run dedupe, application tracking
src/dashboard.py          the single-file HTML dashboard
src/report.py             terminal, Markdown, JSON, CSV, HTML writers
src/llm.py                optional Claude enrichment
src/credentials.py        .env credential resolution
src/scrapers/             one module per source family
tests/                    pytest suite
```

---

## Notes on scraping

The JSON and RSS sources are public APIs used as intended. The browser sources
(LinkedIn, JobStreet, Glints) drive a real browser against public pages, with a
politeness delay on every request and low page counts — for one person's own job
search. Respect each site's terms of service, keep the volume low, and remember
that credentials in `.env` are yours: `.env` is gitignored and nothing in this
codebase logs or transmits them.

Inspired by [sreekar2858/JobSearch-Agent](https://github.com/sreekar2858/JobSearch-Agent),
rebuilt around a profile-driven scorer, region-aware compensation rules,
cross-run state and a much wider source set.

MIT licensed.
