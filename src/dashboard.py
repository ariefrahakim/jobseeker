"""A simple dashboard: one self-contained HTML file built from the database.

Deliberately plain. No server, no build step, no dependencies — `python main.py
dashboard` writes `output/dashboard.html` and you open it in a browser or send it
to your phone. Filtering and sorting happen client-side on data embedded in the
page, so it keeps working offline.

It answers the four questions you actually have each morning:
  1. How many roles are worth looking at, and how many are new?
  2. Which ones clear the IDR 30jt floor, and which need checking?
  3. What have I already applied to?
  4. What should I open first?
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .profile import Profile
from .storage import Store

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Plain-language labels. The internal vocabulary ("not_enforced") means nothing
# at a glance; these are what a person reads.
_SALARY_LABEL = {
    "pass": "Clears 30jt",
    "unknown": "Not stated",
    "n/a": "No floor",
    "fail": "Below floor",
}
_REGION_LABEL = {
    "indonesia": "Indonesia",
    "asia": "Asia",
    "europe": "Europe",
    "fallback": "Elsewhere",
}


def _breakdown(raw: str | None) -> dict[str, float]:
    """Per-dimension scores, as stored by the matcher. `{}` if unavailable."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {k: v for k, v in parsed.items() if k != "total"} if isinstance(parsed, dict) else {}


def build(
    store: Store,
    path: str | Path | None = None,
    limit: int = 500,
    per_page: int = 25,
    profile: Profile | None = None,
) -> Path:
    """Render the dashboard from everything in the database."""
    target = Path(path) if path else OUTPUT_DIR / "dashboard.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = store.list_jobs(limit=limit, order="recent")
    stats = store.stats()

    # Weights come from the profile so the tooltip explains the *live* scoring
    # rules rather than a copy that silently drifts out of date.
    try:
        weights = (profile or Profile.load()).scoring.get("weights", {})
    except Exception:  # a missing or broken profile must not break the page
        weights = {}

    jobs = [
        {
            "id": r["fingerprint"],
            "score": round(r["score"] or 0, 1),
            "breakdown": _breakdown(r["breakdown_json"]),
            "title": r["title"],
            "company": r["company"],
            "location": r["location_raw"] or "—",
            "region": _REGION_LABEL.get(r["region"] or "fallback", "Elsewhere"),
            "setup": r["arrangement"] or "unknown",
            "salary": r["salary_human"] or "not stated",
            "salaryIdr": r["salary_monthly_idr"],
            "salaryLabel": _SALARY_LABEL.get(r["salary_verdict"] or "unknown", "—"),
            "salaryVerdict": r["salary_verdict"] or "unknown",
            "status": r["status"],
            "source": r["source"],
            "skills": (r["matched_skills"] or "").split(", ") if r["matched_skills"] else [],
            "warnings": [w for w in (r["warnings"] or "").split(" | ") if w],
            "fit": r["llm_fit"] or "",
            "url": r["url"],
            "posted": (r["posted_at"] or "")[:10],
            "found": (r["first_seen_at"] or "")[:10],
            # Numeric key so sorting is chronological, not lexical.
            "foundTs": r["first_seen_at"] or "",
        }
        for r in rows
    ]

    # Headline numbers. "Ready to apply" is the one that matters: high score,
    # not yet acted on, and not carrying a salary caveat.
    new_count = stats["by_status"].get("new", 0)
    applied = sum(stats["by_status"].get(s, 0) for s in ("applied", "interviewing"))
    ready = sum(
        1
        for j in jobs
        if j["score"] >= 70
        and j["status"] in ("new", "seen", "shortlisted")
        and j["salaryVerdict"] != "unknown"
    )
    needs_check = sum(1 for j in jobs if j["salaryVerdict"] == "unknown")

    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    target.write_text(
        _TEMPLATE.format(
            generated=html.escape(generated),
            total=stats["total"],
            new_count=new_count,
            ready=ready,
            needs_check=needs_check,
            applied=applied,
            per_page=per_page,
            data=_json_for_script(jobs),
            weights=_json_for_script(
                {k: float(v) for k, v in weights.items()}
            ),
        ),
        encoding="utf-8",
    )
    return target


def _json_for_script(payload: object) -> str:
    """Serialise data for embedding inside a <script> block.

    `json.dumps` does not escape `<` or `>`, so a job title containing
    `</script>` — and titles come from third-party boards, not from us — would
    close the block early and turn the rest of the payload into live HTML.
    Escaping the three characters as \\uXXXX keeps the JSON valid and inert.
    """
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# The `{{` / `}}` escapes are for str.format — CSS and JS braces must survive it.
_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job dashboard</title>
<style>
  :root {{
    --bg:#fbfaf8; --card:#ffffff; --fg:#1c1b19; --muted:#6b6862; --line:#e4e0d8;
    --accent:#0f766e; --ok:#166534; --okbg:#dcfce7; --warn:#92400e; --warnbg:#fef3c7;
    --info:#1e40af; --infobg:#dbeafe;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#161513; --card:#1e1d1a; --fg:#eceae6; --muted:#9a958c; --line:#302e2a;
      --accent:#5eead4; --ok:#86efac; --okbg:#14371f; --warn:#fcd34d; --warnbg:#3a2f10;
      --info:#93c5fd; --infobg:#152a4a;
    }}
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; padding:1.5rem 1rem 4rem; background:var(--bg); color:var(--fg);
    font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif }}
  main {{ max-width:70rem; margin:0 auto }}
  h1 {{ font-size:1.5rem; margin:0 0 .2rem }}
  .sub {{ color:var(--muted); margin:0 0 1.5rem; font-size:.9rem }}

  /* Headline numbers */
  .cards {{ display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));
    margin-bottom:1.5rem }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:.6rem;
    padding:.9rem 1rem }}
  .card b {{ display:block; font-size:1.9rem; line-height:1.1;
    font-variant-numeric:tabular-nums }}
  .card span {{ color:var(--muted); font-size:.8rem }}
  .card.hi b {{ color:var(--accent) }}

  /* Controls */
  .controls {{ display:flex; flex-wrap:wrap; gap:.5rem; margin-bottom:1rem }}
  input, select {{ font:inherit; padding:.5rem .6rem; border:1px solid var(--line);
    border-radius:.4rem; background:var(--card); color:var(--fg) }}
  input {{ flex:1 1 14rem }}

  /* Table */
  .wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:.6rem;
    background:var(--card) }}
  table {{ border-collapse:collapse; width:100%; min-width:52rem }}
  th, td {{ text-align:left; padding:.6rem .75rem; border-bottom:1px solid var(--line);
    vertical-align:top }}
  th {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); cursor:pointer; white-space:nowrap; user-select:none }}
  th:hover {{ color:var(--fg) }}
  tbody tr:last-child td {{ border-bottom:none }}
  tbody tr:hover {{ background:color-mix(in srgb, var(--card) 92%, var(--fg)) }}
  .score {{ font-weight:700; font-variant-numeric:tabular-nums; color:var(--accent);
    cursor:help; border-bottom:1px dotted currentColor }}
  .score:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px }}

  /* Score tooltip. Lives on <body>, positioned by JS — an absolutely
     positioned tooltip inside .wrap would be clipped by its overflow-x. */
  #tip {{ position:fixed; z-index:50; max-width:20rem; padding:.7rem .8rem;
    background:var(--card); color:var(--fg); border:1px solid var(--line);
    border-radius:.5rem; box-shadow:0 6px 24px rgba(0,0,0,.18);
    font-size:.8rem; pointer-events:none; opacity:0; transition:opacity .1s }}
  #tip[data-show] {{ opacity:1 }}
  #tip h4 {{ margin:0 0 .4rem; font-size:.8rem }}
  #tip table {{ min-width:0; width:100%; border-collapse:collapse }}
  #tip td {{ border:none; padding:.12rem 0; font-size:.75rem }}
  #tip td:first-child {{ padding-right:.5rem; white-space:nowrap }}
  #tip td:last-child {{ text-align:right; font-variant-numeric:tabular-nums;
    color:var(--muted) }}
  #tip .bar {{ display:block; height:4px; border-radius:2px; background:var(--line) }}
  #tip .bar i {{ display:block; height:100%; border-radius:2px; background:var(--accent) }}
  #tip .foot {{ margin:.5rem 0 0; color:var(--muted); font-size:.7rem; line-height:1.4 }}

  /* Pagination */
  .pager {{ display:flex; align-items:center; gap:.5rem; flex-wrap:wrap;
    margin-top:.75rem; font-size:.85rem; color:var(--muted) }}
  .pager button {{ font:inherit; padding:.35rem .7rem; border:1px solid var(--line);
    border-radius:.4rem; background:var(--card); color:var(--fg); cursor:pointer }}
  .pager button:hover:not(:disabled) {{ border-color:var(--accent); color:var(--accent) }}
  .pager button:disabled {{ opacity:.4; cursor:default }}
  .pager .spacer {{ flex:1 }}
  a {{ color:var(--fg) }}
  a:hover {{ color:var(--accent) }}
  .co {{ color:var(--muted); font-size:.85rem }}
  .pill {{ display:inline-block; font-size:.7rem; padding:.1rem .4rem;
    border-radius:.25rem; white-space:nowrap }}
  .pill.ok {{ background:var(--okbg); color:var(--ok) }}
  .pill.warn {{ background:var(--warnbg); color:var(--warn) }}
  .pill.info {{ background:var(--infobg); color:var(--info) }}
  .new {{ background:var(--accent); color:var(--bg); font-size:.65rem;
    padding:.05rem .3rem; border-radius:.2rem; margin-left:.3rem }}
  .note {{ color:var(--muted); font-size:.8rem; margin-top:.2rem }}
  .empty {{ padding:2.5rem 1rem; text-align:center; color:var(--muted) }}
  footer {{ color:var(--muted); font-size:.8rem; margin-top:1.5rem }}
  code {{ background:var(--card); border:1px solid var(--line); border-radius:.25rem;
    padding:.05rem .3rem; font-size:.85em }}
</style></head>
<body><main>

<h1>Job dashboard</h1>
<p class="sub">Updated {generated} · click a column heading to sort ·
  hover a score to see how it was calculated</p>

<div class="cards">
  <div class="card hi"><b>{ready}</b><span>Ready to apply<br>(70+, salary clear)</span></div>
  <div class="card"><b>{new_count}</b><span>New since last run</span></div>
  <div class="card"><b>{needs_check}</b><span>Salary not stated<br>— check first</span></div>
  <div class="card"><b>{applied}</b><span>Applied / interviewing</span></div>
  <div class="card"><b>{total}</b><span>Total tracked</span></div>
</div>

<div class="controls">
  <input id="q" type="search" placeholder="Search title, company, skill…">
  <select id="region"><option value="">All regions</option></select>
  <select id="status"><option value="">All statuses</option></select>
  <select id="setup">
    <option value="">Any setup</option>
    <option value="remote">Remote only</option>
    <option value="hybrid">Hybrid</option>
    <option value="onsite">Onsite</option>
  </select>
  <select id="min">
    <option value="0" selected>Any score</option>
    <option value="60">60+</option>
    <option value="70">70+</option>
    <option value="80">80+</option>
  </select>
</div>

<div class="wrap">
  <table>
    <thead><tr>
      <th data-k="score">Score</th>
      <th data-k="title">Role</th>
      <th data-k="region">Where</th>
      <th data-k="salaryIdr">Salary</th>
      <th data-k="status">Status</th>
      <th data-k="foundTs">Found</th>
      <th data-k="posted">Posted</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>Nothing matches these filters.</div>
</div>

<div class="pager">
  <span id="range">—</span>
  <span class="spacer"></span>
  <label>Per page
    <select id="per">
      <option value="10">10</option>
      <option value="25">25</option>
      <option value="50">50</option>
      <option value="100">100</option>
      <option value="0">All</option>
    </select>
  </label>
  <button id="prev" type="button">‹ Prev</button>
  <span id="pageinfo">1 / 1</span>
  <button id="next" type="button">Next ›</button>
</div>

<div id="tip" role="tooltip"></div>

<footer>
  Mark progress from the terminal: <code>python main.py status &lt;id&gt; applied</code> ·
  draft an application: <code>python main.py apply &lt;id&gt;</code> ·
  refresh: <code>python main.py search &amp;&amp; python main.py dashboard</code>
</footer>

<script>
const JOBS = {data};
const WEIGHTS = {weights};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => (
  {{ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }}[c]));

// Plain-language names for the scoring dimensions, in the order they are shown.
const DIMENSIONS = [
  ["skills",      "Skills matched"],
  ["title",       "Title match"],
  ["arrangement", "Work setup"],
  ["seniority",   "Seniority"],
  ["domain",      "Industry"],
  ["freshness",   "How recent"],
];

// Populate the region and status dropdowns from the data itself, so they never
// list a filter that would return nothing.
for (const [id, key] of [["region","region"], ["status","status"]]) {{
  const seen = [...new Set(JOBS.map(j => j[key]))].filter(Boolean).sort();
  document.getElementById(id).insertAdjacentHTML("beforeend",
    seen.map(v => `<option value="${{esc(v)}}">${{esc(v)}}</option>`).join(""));
}}

// Newest-scraped first: the point of a daily run is seeing what just
// arrived, so that is the default view rather than the highest score.
let sortKey = "foundTs", sortDesc = true;
let page = 1, perPage = {per_page};
let shown = [];

function pill(job) {{
  const cls = job.salaryVerdict === "pass" ? "ok"
            : job.salaryVerdict === "unknown" ? "warn" : "info";
  return `<span class="pill ${{cls}}">${{esc(job.salaryLabel)}}</span>`;
}}

/* ---------------------------------------------------------------- tooltip --
   Explains a score by showing each dimension's own 0-100 result next to how
   much it counts toward the total. Without the weight the numbers are
   misleading: a perfect "Industry" score moves the total by 5, not by 100. */
const tip = document.getElementById("tip");

function tooltipHtml(job) {{
  const b = job.breakdown || {{}};
  if (!Object.keys(b).length) {{
    return `<h4>Score ${{job.score.toFixed(1)}} / 100</h4>
      <p class="foot">No breakdown was stored for this job. Re-run
      <code>search</code> to record one.</p>`;
  }}
  const totalWeight = Object.values(WEIGHTS).reduce((a, c) => a + c, 0) || 1;
  const rows = DIMENSIONS.filter(([k]) => k in b).map(([k, label]) => {{
    const score = b[k] ?? 0;
    const share = ((WEIGHTS[k] ?? 0) / totalWeight * 100).toFixed(0);
    return `<tr>
      <td>${{esc(label)}}<span class="bar"><i style="width:${{Math.max(0, Math.min(100, score))}}%"></i></span></td>
      <td>${{score.toFixed(0)}}<br><small>${{share}}% weight</small></td>
    </tr>`;
  }}).join("");
  return `<h4>Score ${{job.score.toFixed(1)}} / 100</h4>
    <table>${{rows}}</table>
    <p class="foot">Each dimension is scored 0-100, then blended by the weights
    above. Anything under 55 is not reported at all.</p>`;
}}

function showTip(el, job) {{
  tip.innerHTML = tooltipHtml(job);
  tip.setAttribute("data-show", "");
  // Measure after filling, then keep the box inside the viewport. Flipping
  // above the row is what stops it being cut off at the bottom of the page.
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let left = Math.min(r.left, window.innerWidth - t.width - 12);
  let top = r.bottom + 8;
  if (top + t.height > window.innerHeight - 8) top = r.top - t.height - 8;
  tip.style.left = `${{Math.max(8, left)}}px`;
  tip.style.top = `${{Math.max(8, top)}}px`;
}}

function hideTip() {{ tip.removeAttribute("data-show"); }}

/* -------------------------------------------------------------- rendering -- */
function render() {{
  const q = document.getElementById("q").value.toLowerCase().trim();
  const region = document.getElementById("region").value;
  const status = document.getElementById("status").value;
  const setup = document.getElementById("setup").value;
  const min = Number(document.getElementById("min").value);

  shown = JOBS.filter(j =>
    j.score >= min &&
    (!region || j.region === region) &&
    (!status || j.status === status) &&
    (!setup || j.setup === setup) &&
    (!q || [j.title, j.company, j.location, j.source, ...j.skills]
            .join(" ").toLowerCase().includes(q))
  ).sort((a, b) => {{
    const x = a[sortKey] ?? "", y = b[sortKey] ?? "";
    const cmp = typeof x === "number" && typeof y === "number"
      ? x - y : String(x).localeCompare(String(y));
    return sortDesc ? -cmp : cmp;
  }});

  // Clamp the page: filtering down to fewer results must not leave you
  // stranded on a page that no longer exists.
  const size = perPage || shown.length || 1;
  const pages = Math.max(1, Math.ceil(shown.length / size));
  page = Math.min(Math.max(1, page), pages);
  const start = (page - 1) * size;
  const slice = shown.slice(start, start + size);

  document.getElementById("rows").innerHTML = slice.map(j => `
    <tr>
      <td class="score" tabindex="0" data-id="${{esc(j.id)}}">${{j.score.toFixed(0)}}</td>
      <td>
        <a href="${{esc(j.url)}}" target="_blank" rel="noopener">${{esc(j.title)}}</a>
        ${{j.status === "new" ? '<span class="new">NEW</span>' : ""}}
        <div class="co">${{esc(j.company)}} · ${{esc(j.setup)}} · via ${{esc(j.source)}}</div>
        ${{j.fit ? `<div class="note">${{esc(j.fit)}}</div>` : ""}}
        ${{j.warnings.length ? `<div class="note">⚠ ${{esc(j.warnings[0])}}</div>` : ""}}
        <div class="note">id ${{esc(j.id)}}</div>
      </td>
      <td>${{esc(j.region)}}<div class="co">${{esc(j.location)}}</div></td>
      <td>${{esc(j.salary)}}<div>${{pill(j)}}</div>
        ${{j.salaryIdr ? `<div class="co">≈ IDR ${{Math.round(j.salaryIdr).toLocaleString()}}/mo</div>` : ""}}</td>
      <td>${{esc(j.status)}}</td>
      <td class="co">${{esc(j.found || "—")}}</td>
      <td class="co">${{esc(j.posted || "—")}}</td>
    </tr>`).join("");

  document.getElementById("empty").hidden = shown.length > 0;

  // Pager state
  const first = shown.length ? start + 1 : 0;
  document.getElementById("range").textContent =
    `${{first}}–${{start + slice.length}} of ${{shown.length}}`;
  document.getElementById("pageinfo").textContent = `${{page}} / ${{pages}}`;
  document.getElementById("prev").disabled = page <= 1;
  document.getElementById("next").disabled = page >= pages;

  hideTip();

  // Wire the tooltip to the rows we just drew. Mouse and keyboard both, so a
  // score is inspectable without a pointer.
  document.querySelectorAll("#rows .score").forEach(cell => {{
    const job = slice.find(j => j.id === cell.dataset.id);
    if (!job) return;
    cell.onmouseenter = () => showTip(cell, job);
    cell.onfocus = () => showTip(cell, job);
    cell.onmouseleave = hideTip;
    cell.onblur = hideTip;
  }});
}}

document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {{
  const key = th.dataset.k;
  // Same column toggles direction; a new column starts descending, which is
  // what you want for every column here except the text ones.
  if (key === sortKey) sortDesc = !sortDesc; else {{ sortKey = key; sortDesc = true; }}
  page = 1;
  render();
}});

// Any filter change resets to page 1 — staying on page 4 of a freshly
// narrowed list looks like an empty result.
document.querySelectorAll("#q, #region, #status, #setup, #min").forEach(el => {{
  const reset = () => {{ page = 1; render(); }};
  el.oninput = reset; el.onchange = reset;
}});

document.getElementById("per").onchange = e => {{
  perPage = Number(e.target.value);
  page = 1;
  render();
}};
document.getElementById("prev").onclick = () => {{ page--; render(); window.scrollTo({{top:0}}); }};
document.getElementById("next").onclick = () => {{ page++; render(); window.scrollTo({{top:0}}); }};

// Left/right arrows page through the list when focus is not in a text field.
document.addEventListener("keydown", e => {{
  // `e.target` is not always an Element — a keydown with nothing focused
  // targets the document itself, which has no .matches() and would throw.
  const t = e.target;
  if (t instanceof Element && t.matches("input, select, textarea")) return;
  if (e.key === "ArrowLeft") document.getElementById("prev").click();
  if (e.key === "ArrowRight") document.getElementById("next").click();
}});

window.addEventListener("scroll", hideTip, {{ passive: true }});

document.getElementById("per").value = String(perPage);
render();
</script>
</main></body></html>
"""
