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


def build(store: Store, path: str | Path | None = None, limit: int = 500) -> Path:
    """Render the dashboard from everything in the database."""
    target = Path(path) if path else OUTPUT_DIR / "dashboard.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = store.list_jobs(limit=limit)
    stats = store.stats()

    jobs = [
        {
            "id": r["fingerprint"],
            "score": round(r["score"] or 0, 1),
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
            data=_json_for_script(jobs),
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
  .score {{ font-weight:700; font-variant-numeric:tabular-nums; color:var(--accent) }}
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
<p class="sub">Updated {generated} · click a column heading to sort</p>

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
    <option value="0">Any score</option>
    <option value="60">60+</option>
    <option value="70" selected>70+</option>
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
      <th data-k="posted">Posted</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>Nothing matches these filters.</div>
</div>

<footer>
  Mark progress from the terminal: <code>python main.py status &lt;id&gt; applied</code> ·
  draft an application: <code>python main.py apply &lt;id&gt;</code> ·
  refresh: <code>python main.py search &amp;&amp; python main.py dashboard</code>
</footer>

<script>
const JOBS = {data};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => (
  {{ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }}[c]));

// Populate the region and status dropdowns from the data itself, so they never
// list a filter that would return nothing.
for (const [id, key] of [["region","region"], ["status","status"]]) {{
  const seen = [...new Set(JOBS.map(j => j[key]))].filter(Boolean).sort();
  document.getElementById(id).insertAdjacentHTML("beforeend",
    seen.map(v => `<option value="${{esc(v)}}">${{esc(v)}}</option>`).join(""));
}}

let sortKey = "score", sortDesc = true;

function pill(job) {{
  const cls = job.salaryVerdict === "pass" ? "ok"
            : job.salaryVerdict === "unknown" ? "warn" : "info";
  return `<span class="pill ${{cls}}">${{esc(job.salaryLabel)}}</span>`;
}}

function render() {{
  const q = document.getElementById("q").value.toLowerCase().trim();
  const region = document.getElementById("region").value;
  const status = document.getElementById("status").value;
  const setup = document.getElementById("setup").value;
  const min = Number(document.getElementById("min").value);

  const shown = JOBS.filter(j =>
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

  document.getElementById("rows").innerHTML = shown.map(j => `
    <tr>
      <td class="score">${{j.score.toFixed(0)}}</td>
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
      <td class="co">${{esc(j.posted || "—")}}</td>
    </tr>`).join("");

  document.getElementById("empty").hidden = shown.length > 0;
}}

document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {{
  const key = th.dataset.k;
  // Same column toggles direction; a new column starts descending, which is
  // what you want for every column here except the text ones.
  if (key === sortKey) sortDesc = !sortDesc; else {{ sortKey = key; sortDesc = true; }}
  render();
}});
document.querySelectorAll("input, select").forEach(el => {{
  el.oninput = render; el.onchange = render;
}});
render();
</script>
</main></body></html>
"""
