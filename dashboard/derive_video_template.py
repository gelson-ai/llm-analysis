#!/usr/bin/env python3
"""
Derive dashboard/video_template.html from dashboard/image_template.html.

WHY THIS EXISTS
    The video page is meant to be structurally parallel to the image page, and the
    image template is the reference implementation: ~940 lines, four <style>
    blocks that tests assert are a VERBATIM copy of the chat template's, and
    several lines longer than 2000 characters. Hand-copying that would guarantee
    drift, so the video template is derived from it by an explicit, reviewable
    substitution list instead of by eye.

    Every anchor is asserted to appear exactly once. A template edit that renames
    or removes one therefore fails this script loudly rather than quietly
    producing a page missing a section. That is the only reason this is a script
    and not a paragraph of instructions.

WHAT IT CHANGES
    * prose, titles and aria-labels: image -> video, "cost per generation" ->
      "cost per clip";
    * the Design Arena section (HTML + JS) is REPLACED by a prompt-coverage
      section, because no video preference/arena score exists;
    * the hero card carries the unpublished-clip-length caveat itself, and its
      missing-rate flag names the pricing scheme instead of saying "$0.00";
    * the reference table's columns become the video field set;
    * coverage gains the evidence-gate bucket, and the unrated table reports why
      a rate is missing.
    Everything else - styles, shell wiring, scatter geometry, budget table - is
    carried across unchanged.

USAGE
    python dashboard/derive_video_template.py [--check]

    --check  verify the derived file matches what is on disk, without writing
             (used by tests/test_video_dashboard_invariants.py)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent
SOURCE_PATH = DASHBOARD_DIR / "image_template.html"
TARGET_PATH = DASHBOARD_DIR / "video_template.html"


def replace_once(html: str, old: str, new: str, *, label: str) -> str:
    count = html.count(old)
    if count != 1:
        raise SystemExit(
            f"anchor '{label}' appears {count} time(s) in {SOURCE_PATH.name}, expected exactly 1. "
            f"The image template changed - update this script deliberately."
        )
    return html.replace(old, new)


def replace_span(html: str, start: str, end: str, new: str, *, label: str) -> str:
    """Replace everything from `start` (inclusive) to the first `end` after it."""
    first = html.find(start)
    if first < 0:
        raise SystemExit(f"span start '{label}' not found in {SOURCE_PATH.name}")
    if html.find(start, first + 1) >= 0:
        raise SystemExit(f"span start '{label}' is not unique in {SOURCE_PATH.name}")
    last = html.find(end, first)
    if last < 0:
        raise SystemExit(f"span end for '{label}' not found in {SOURCE_PATH.name}")
    return html[:first] + new + html[last + len(end):]


# ---------------------------------------------------------------------------
# 1. head, masthead, banner
# ---------------------------------------------------------------------------
HEAD_EDITS = [
    (
        "<title>OpenRouter Image Generation Price vs Performance</title>",
        "<title>OpenRouter Video Generation Price vs Performance</title>",
        "title",
    ),
    (
        '<meta name="description" content="Editorial analysis of OpenRouter image-generation '
        'model pricing against OpenRouter media prompt benchmarks and Design Arena preference scores.">',
        '<meta name="description" content="Editorial analysis of OpenRouter video-generation '
        'model pricing against OpenRouter media prompt benchmarks.">',
        "meta description",
    ),
    (
        '<p class="eyebrow">Model economics · Image generation</p>',
        '<p class="eyebrow">Model economics · Video generation</p>',
        "masthead eyebrow",
    ),
    (
        "<h1>Image Generation Price vs. Performance</h1>",
        "<h1>Video Generation Price vs. Performance</h1>",
        "masthead h1",
    ),
    (
        "<span><b>Source</b>OpenRouter <code>/api/v1/images/models</code></span>",
        "<span><b>Source</b>OpenRouter <code>/api/v1/videos/models</code></span>",
        "masthead source span",
    ),
    (
        "no Artificial Analysis data is included for image models.",
        "no Artificial Analysis data is included for video models.",
        "snapshot line",
    ),
]

# The banner is the methodology caveat in the page chrome; the payload's
# methodology_note carries the full text, so this block only needs the units to
# stop describing images.
BANNER_EDITS = [
    (
        "<span><b>Value</b>pass rate ÷ benchmark cost</span>",
        "<span><b>Value</b>pass rate ÷ observed cost per clip</span>",
        "banner value definition",
    ),
]

# ---------------------------------------------------------------------------
# 2. the hero card
# ---------------------------------------------------------------------------
HERO_EDITS = [
    (
        "  const row=record?ROWS.find(item=>item.model_id===record.model_id):null;\n",
        "  // With no locked pick yet (a fresh history file), the hero describes today's\n"
        "  // leader - so the row must come from the same place the leader does. Inherited\n"
        "  // from the image template, where a null `row` made the card announce \"no rank\n"
        "  // today\" about the very model it was presenting as #1.\n"
        "  const row=record?ROWS.find(item=>item.model_id===record.model_id):(ranked[0]||null);\n",
        "hero row fallback",
    ),
    (
        '<div class="label">Cost per generation</div><div class="value" id="heroCost"></div>'
        '<div class="detail">mean benchmark cost</div>',
        '<div class="label">Cost per clip</div><div class="value" id="heroCost"></div>'
        '<div class="detail">observed cost of one generation</div>',
        "hero cost tile",
    ),
    # The missing-rate flag: it must name the pricing scheme, never imply $0.00.
    # This also removes the image page's preference-survey comment, which would
    # otherwise survive as a reference to a source that does not exist for video.
    (
        "  // Item 3: never let a cost-efficiency win read as a human-preference win. The\n"
        "  // Design Arena score is a separate source and is never blended into value, so\n"
        "  // a leader without one is flagged rather than quietly presented as a winner.\n"
        '  const arenaFlag=byId("heroArenaFlag");\n'
        '  if(arenaFlag){arenaFlag.hidden=Boolean(leader.design_arena);arenaFlag.textContent="No preference data"}\n',
        "  // The flag that needs surfacing on this page is a MISSING CATALOGUE RATE, and it\n"
        "  // names the scheme the model does publish rather than showing a zero.\n"
        '  const rateFlag=byId("heroArenaFlag");\n'
        '  if(rateFlag){\n'
        '    const absence=(row&&row.rate_absence_label)||null;\n'
        '    rateFlag.hidden=Boolean(leader.has_catalog_rate);\n'
        '    rateFlag.textContent=absence||"no catalogue rate";\n'
        '  }\n',
        "hero flag",
    ),
    # There is no Design Arena row to flag on this page; drop the block rather
    # than leave a note about a source that does not exist for video.
    (
        '  if(!leader.design_arena){\n'
        '    notes.push("This model has no Design Arena row, so this is a cost-efficiency result only: it is not evidence of stronger human preference. Design Arena is a separate survey and is never blended into the value score.");\n'
        "  }\n",
        "",
        "hero arena note",
    ),
    (
        '  if(!leader.has_catalog_rate){\n'
        '    notes.push("Note: this model publishes no resolvable catalogue rate, so its price comes from its own benchmark generations and cannot be cross-checked against a list price (see the full table below).");\n'
        "  }\n",
        '  if(!leader.has_catalog_rate){\n'
        '    notes.push(`Catalogue rate: ${(row&&row.rate_absence_label)||"not published"} - the price above is the observed cost of one generated clip, so it cannot be cross-checked against a published list rate (see the full table below).`);\n'
        "  }\n"
        '  // The caveat that qualifies the headline pick lives ON the card, not only in\n'
        '  // the provenance panel: the benchmark pages publish no clip length, so two\n'
        '  // models\' observed costs may describe clips of different durations.\n'
        '  notes.push("Clip length is not published for these benchmark rows, so this observed cost may describe a clip of a different length from a rival\'s - it is the price of what was actually generated, not a controlled like-for-like.");\n'
        "  // Rankability is gated on evidence, so state the gate and where this model sits\n"
        "  // against it, rather than leaving the gate to the methodology panel alone.\n"
        "  if(CONST.evidence_min_prompts!=null){\n"
        "    notes.push(`Ranking requires at least ${num(CONST.evidence_min_prompts)} benchmarked prompts and ${num(CONST.evidence_min_checks)} judged checks; this model has ${num(leader.prompts)} prompt(s).`);\n"
        "  }\n",
        "hero clip-length caveat",
    ),
    (
        '  setText("heroRank",row?`Rank ${row.value_rank} of ${ranked.length} rankable models today · ${num(leader.prompts)} prompts benchmarked`:`${num(leader.prompts)} prompts benchmarked; no rank today, so it is not in the current value ranking`);',
        '  setText("heroRank",row?`Rank ${row.value_rank} of ${ranked.length} rankable models today · ${num(leader.prompts)} prompts and ${num(row.checks_total)} judged checks`:`${num(leader.prompts)} prompts benchmarked; no rank today, so it is not in the current value ranking`);',
        "hero rank line",
    ),
    (
        '  setText("heroCost",money(leader.avg_cost_usd));',
        '  // Always the OBSERVED per-clip cost: the catalogue rate is a different unit\n'
        '  // (per output second) and is shown separately, never merged into this figure.\n'
        '  setText("heroCost",money(leader.avg_cost_usd));',
        "hero cost",
    ),
]

VALUE_LEADER_EDITS = [
    (
        "Top image-generation models by value score",
        "Top video-generation models by value score",
        "value leaders aria",
    ),
    (
        "<p>Value is pass rate divided by mean benchmark cost. Bars are shown as a share of the leader.</p>",
        "<p>Value is pass rate divided by the observed cost of one generated clip. Bars are shown as a share of the leader.</p>",
        "value leaders panel text",
    ),
]

# ---------------------------------------------------------------------------
# 3. scatter + kpis + budget
# ---------------------------------------------------------------------------
SCATTER_EDITS = [
    (
        '<h2 id="scatterTitle">Pass rate vs. cost per generation</h2>',
        '<h2 id="scatterTitle">Pass rate vs. cost per clip</h2>',
        "scatter title",
    ),
    (
        '<svg id="imageScatter" viewBox="0 0 1100 470" role="img" '
        'aria-label="Pass rate versus cost per generation scatter chart"></svg>',
        '<svg id="videoScatter" viewBox="0 0 1100 470" role="img" '
        'aria-label="Pass rate versus cost per clip scatter chart"></svg>',
        "scatter svg",
    ),
    (
        '  const svg=byId("imageScatter");if(!svg||!plotted.length)return;',
        '  const svg=byId("videoScatter");if(!svg||!plotted.length)return;',
        "scatter lookup",
    ),
    (
        '  xTitle.textContent="mean cost per generation (USD)";',
        '  xTitle.textContent="mean cost per clip (USD)";',
        "scatter axis title",
    ),
    (
        '+`<div class="tooltip-row"><span>Cost</span><span>${esc(money(row.avg_cost_usd))}</span></div>`',
        '+`<div class="tooltip-row"><span>Cost per clip</span><span>${esc(money(row.avg_cost_usd))}</span></div>`\n'
        '        +`<div class="tooltip-row"><span>Output</span><span>${(row.output_resolutions||[]).length?esc(row.output_resolutions.join(", ")):\'<span class="num-dim">—</span>\'}</span></div>`\n'
        '        +`<div class="tooltip-row"><span>Mean gen. time</span><span>${row.mean_generation_seconds==null?\'—\':esc(seconds(row.mean_generation_seconds*1000))}</span></div>`',
        "scatter tooltip rows",
    ),
]

KPI_EDITS = [
    (
        '["Median cost",money(median),"per benchmark generation"],',
        '["Median cost",money(median),"per generated clip"],',
        "kpi median cost",
    ),
]

BUDGET_EDITS = [
    (
        "<thead><tr><th>Model</th><th>Pass rate</th><th>Cost per generation</th><th>Value</th>"
        "<th>Checks attempted</th></tr></thead>",
        "<thead><tr><th>Model</th><th>Pass rate</th><th>Cost per clip</th><th>Value</th>"
        "<th>Checks attempted</th></tr></thead>",
        "budget table head",
    ),
    (
        "of their attempted checks, ranked by benchmark cost.",
        "of their attempted checks, ranked by observed cost per clip. Only models clearing "
        "the evidence gate are eligible.",
        "budget subhead",
    ),
]

# ---------------------------------------------------------------------------
# 4. coverage + unrated table
# ---------------------------------------------------------------------------
COVERAGE_EDITS = [
    (
        '["Rankable by value",COVERAGE.rankable_models,"Has both a pass rate and a benchmark cost"],',
        '["Rankable by value",COVERAGE.rankable_models,"Has a benchmark cost AND clears the evidence gate"],',
        "coverage rankable bucket",
    ),
    (
        '["Ranked without a catalogue rate",COVERAGE.benchmarked_unpriced_models,"Benchmark rows carry cost even when no list rate was published"],',
        '["Below the evidence gate",COVERAGE.gated_out_models,"Benchmarked, but on too few prompts or checks to be ranked"],\n'
        '    ["Ranked without a catalogue rate",COVERAGE.benchmarked_unpriced_models,"Value uses the observed clip cost, so a missing list rate does not remove a model from the ranking"],',
        "coverage gate bucket",
    ),
    (
        '["Publish a resolvable rate",COVERAGE.priced_models,"Endpoint pricing parsed into a comparable rate"],',
        '["Publish a resolvable rate",COVERAGE.priced_models,"A rate in USD per output second; per-token and per-megapixel-second schemes cannot be converted"],',
        "coverage priced bucket",
    ),
    # The unrated table: a missing rate renders as an em dash WITH its reason,
    # never as a number and never as "$0.00".
    (
        "<thead><tr><th>Model</th><th>Catalogue rate</th><th>Rate basis</th><th>Design Arena</th></tr></thead>",
        "<thead><tr><th>Model</th><th>Catalogue $/s</th><th>Cost per 5s</th><th>Rate note</th></tr></thead>",
        "unrated table head",
    ),
    (
        "    +`<td>${row.comparable_price==null?'<span class=\"num-dim\">not published</span>':esc(money(row.comparable_price))}</td>`\n"
        "    +`<td>${row.pricing_unit?esc(row.pricing_unit):'<span class=\"num-dim\">—</span>'}</td>`\n"
        "    +`<td>${row.design_arena?'<span class=\"num-dim\">has a score</span>':'<span class=\"num-dim\">—</span>'}</td></tr>`).join(\"\"));",
        "    +`<td>${row.comparable_price==null?'<span class=\"num-dim\">—</span>':esc(money(row.comparable_price))}</td>`\n"
        "    +`<td>${row.cost_per_5s_clip==null?'<span class=\"num-dim\">—</span>':esc(money(row.cost_per_5s_clip))}</td>`\n"
        "    +`<td class=\"name-cell num-dim\">${row.rate_absence_label?esc(row.rate_absence_label):(row.comparable_price_basis?esc(row.comparable_price_basis):'—')}</td></tr>`).join(\"\"));",
        "unrated table row",
    ),
    (
        'notes.push(`${unrated.length} priced models have no benchmark rows yet, so they cannot be rated — they are listed above rather than removed.`);',
        'notes.push(`${unrated.length} catalogue model(s) have no judged benchmark rows yet, so they cannot be rated - they are listed above rather than removed, which is not the same as scoring zero.`);',
        "coverage note unrated",
    ),
    (
        "because Artificial Analysis publishes no image-generation benchmark at all and the OpenRouter media endpoints publish no judging data. ",
        "because no source publishes a video arena score and OpenRouter's video catalogue carries no benchmarks block. ",
        "unrated note source",
    ),
    (
        "image generation has no equivalent, because",
        "video generation has no equivalent, because",
        "unrated note subject",
    ),
]

# ---------------------------------------------------------------------------
# 5. reference table
# ---------------------------------------------------------------------------
TABLE_EDITS = [
    (
        '<p class="card-sub">Every catalogue model, ranked by value. The catalogue rate column is the published list rate and is <em>not</em> comparable across models — the units differ per row. The benchmark cost column is the money the value score actually uses. <span id="pricingCount"></span></p>',
        '<p class="card-sub">Every catalogue model, ranked by value. <b>Cost per clip</b> is the observed cost of one generated clip and is the money the value score uses. <b>Catalogue $/s</b> is the published rate per output second, and it is <em>not</em> comparable across models without its basis (plain rate, a 720p tier, an audio-qualified rate). <span id="pricingCount"></span></p>',
        "pricing card sub",
    ),
    (
        "<thead><tr><th>Model</th><th>Value</th><th>Benchmark cost</th><th>Generated at</th>"
        "<th>Gen. time</th><th>Catalogue rate</th><th>Rate basis</th><th>Endpoint pricing lines</th></tr></thead>",
        "<thead><tr><th>Model</th><th>Value</th><th>Cost per clip</th><th>Output res</th>"
        "<th>Mean gen. time</th><th>Catalogue $/s</th><th>Cost per 5s</th><th>Rate basis</th>"
        "<th>Max duration</th><th>Audio</th><th>Frame control</th></tr></thead>",
        "pricing table head",
    ),
]

TREND_EDITS = [
    (
        'setText("trendSub","Value and cost per generation across snapshots, once there is more than one.");',
        'setText("trendSub","Value and cost per clip across snapshots, once there is more than one.");',
        "trend subhead",
    ),
]

PROVENANCE_EDITS = [
    (
        "`Coverage in this snapshot: ${COVERAGE.catalog_models} catalogue models, ${COVERAGE.priced_models} with a resolvable catalogue rate, ${COVERAGE.benchmarked_models} benchmarked over ${COVERAGE.prompt_count_min}–${COVERAGE.prompt_count_max} prompts each, ${COVERAGE.unrated_models} unrated, ${COVERAGE.design_arena_models} with Design Arena scores.`,",
        "`Coverage in this snapshot: ${COVERAGE.catalog_models} catalogue models, ${COVERAGE.priced_models} with a resolvable per-second rate, ${COVERAGE.benchmarked_models} benchmarked over ${COVERAGE.prompt_count_min}–${COVERAGE.prompt_count_max} prompts each, ${COVERAGE.rankable_models} rankable, ${COVERAGE.gated_out_models} below the evidence gate, ${COVERAGE.unrated_models} unrated.`,",
        "provenance coverage line",
    ),
    (
        '"Prompt counts per model are not uniform, so a point or bar backed by more attempted checks rests on more evidence than one backed by fewer.",',
        '"Prompt counts per model are not uniform, so a point or bar backed by more attempted checks rests on more evidence than one backed by fewer.",\n'
        '    CONST.cost_unit_note,',
        "provenance cost unit note",
    ),
    (
        'setText("footerProvenance",`Generated ${dateOnly(PAYLOAD.generated_at)} from data retrieved ${dateOnly(PAYLOAD.data_retrieved_at)}. Value = pass rate ÷ mean benchmark cost (${CONST.value_definition||""}).`);',
        'setText("footerProvenance",`Generated ${dateOnly(PAYLOAD.generated_at)} from data retrieved ${dateOnly(PAYLOAD.data_retrieved_at)}. Value = pass rate ÷ observed cost per clip (${CONST.value_definition||""}).`);',
        "footer provenance",
    ),
]

# ---------------------------------------------------------------------------
# 6. the replaced sections
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 7. the chrome and head-to-head leftovers that still describe images or a
#    preference survey. These are applied AFTER the section swaps, because one
#    of them is the stale section comment the swap leaves behind.
# ---------------------------------------------------------------------------
LEFTOVER_EDITS = [
    (
        "<span><b>Benchmarks</b>OpenRouter media prompt benchmarks · Design Arena</span>",
        "<span><b>Benchmarks</b>OpenRouter media prompt benchmarks (video)</span>",
        "masthead benchmarks span",
    ),
    (
        "<p class=\"card-sub\">Coverage across the full image-generation catalogue in this embedded snapshot. Models without benchmark rows are counted, never dropped.</p>",
        "<p class=\"card-sub\">Coverage across the full video-generation catalogue in this embedded snapshot. Models without benchmark rows are counted, never dropped.</p>",
        "inventory subhead",
    ),
    (
        "<!-- 05 — design arena -------------------------------------------------------->",
        "<!-- 05 — prompt coverage ---------------------------------------------------->",
        "section 05 comment",
    ),
    (
        '<h2 id="tableTitle">Catalogue pricing vs. benchmark cost</h2>',
        '<h2 id="tableTitle">Catalogue rate vs. observed cost per clip</h2>',
        "reference table title",
    ),
    (
        "<p>Source: OpenRouter <code>/api/v1/images/models</code> and <code>/api/v1/images/models/&lt;id&gt;/endpoints</code> for the catalogue, OpenRouter's media prompt benchmark pages for checks and cost, and OpenRouter's <code>/api/v1/models?output_modalities=image</code> filtered catalogue for Design Arena. This report embeds a single data snapshot and never re-fetches at view time.</p>",
        "<p>Source: OpenRouter <code>/api/v1/videos/models</code> for the catalogue and its published rates, and OpenRouter's media prompt benchmark pages for video for judged checks and observed cost. This report embeds a single data snapshot and never re-fetches at view time.</p>",
        "footer sources",
    ),
    (
        "const ROWS=PAYLOAD.rows||[],ARENA=PAYLOAD.design_arena||{categories:[],models:[]},COVERAGE=PAYLOAD.coverage||{},CONST=PAYLOAD.constants||{};",
        "const ROWS=PAYLOAD.rows||[],COVERAGE=PAYLOAD.coverage||{},CONST=PAYLOAD.constants||{};",
        "payload destructuring",
    ),
    (
        "  parts.push(`${COVERAGE.design_arena_models} with Design Arena`);\n",
        "",
        "snapshot line arena count",
    ),
    (
        '. Value = pass rate ÷ mean benchmark cost; no Artificial Analysis data is included for video models.',
        '. Value = pass rate ÷ observed cost per clip; no Artificial Analysis data is included for video models.',
        "snapshot line value definition",
    ),
    (
        "  const notes=[`Value ${value(leader.value)} = pass rate ${pct(leader.pass_rate)} ÷ mean benchmark cost ${money(leader.avg_cost_usd)} across ${num(leader.prompts)} benchmark prompts.`];",
        "  const notes=[`Value ${value(leader.value)} = pass rate ${pct(leader.pass_rate)} ÷ observed cost per clip ${money(leader.avg_cost_usd)} across ${num(leader.prompts)} benchmark prompts.`];",
        "hero value sentence",
    ),
    (
        "  const rateCell=row=>row.has_catalog_rate?`Yes (${esc(row.comparable_price_basis||\"catalogue rate\")})`:'<span class=\"num-dim\">none published</span>';",
        "  // A missing rate is an em dash WITH its reason, never a zero and never a bare\n"
        "  // \"none\" - the reader is told which scheme the model does publish.\n"
        "  const rateCell=row=>row.comparable_price==null?`<span class=\"num-dim\">— ${esc(row.rate_absence_label||\"no published rate\")}</span>`:`${esc(money(row.comparable_price))}/s (${esc(row.comparable_price_basis||\"catalogue rate\")})`;\n"
        "  const durationCell=row=>row.max_duration_seconds==null?'<span class=\"num-dim\">—</span>':`${num(row.max_duration_seconds)}s`;",
        "rate cell",
    ),
    (
        "  // Item 3, restated in the comparison: the whole point of the Design Arena\n"
        "  // cross-check is that a cheaper model is not thereby a better-liked one.\n"
        "  function describeArena(baseline,other){\n"
        "    const left=arenaEntry(baseline.model_id),right=arenaEntry(other.model_id);\n"
        "    if(Boolean(left)===Boolean(right))return null;\n"
        "    const withArena=left?baseline:other,without=left?other:baseline;\n"
        "    return `<strong>${nameOf(withArena)}</strong> has Design Arena (human-preference) rows and <strong>${nameOf(without)}</strong> has none, so the figures above are cost-efficiency and judged-check results only: they say nothing about which model people prefer.`;\n"
        "  }",
        "  // The clip-length caveat, said in the comparison as well as on the hero card:\n"
        "  // the benchmark pages publish no clip duration, so an observed-cost difference\n"
        "  // may be a duration difference rather than a unit-price difference.\n"
        "  function describeClipLength(baseline,other){\n"
        "    if(baseline.avg_cost_usd==null||other.avg_cost_usd==null)return null;\n"
        "    return \"Clip length is not published for these benchmark rows, so the cost difference above may reflect different generated durations as well as different unit prices.\";\n"
        "  }",
        "describe arena",
    ),
    (
        "      describeArena(baseline,other),",
        "      describeClipLength(baseline,other),",
        "summary call site",
    ),
    (
        '      ["Benchmark cost per generation",money(baseline.avg_cost_usd),money(other.avg_cost_usd)],',
        '      ["Cost per clip",money(baseline.avg_cost_usd),money(other.avg_cost_usd)],',
        "comparison cost row",
    ),
    (
        '      ["Generated at",list(baseline.output_resolutions),list(other.output_resolutions)],',
        '      ["Output resolution",list(baseline.output_resolutions),list(other.output_resolutions)],',
        "comparison resolution row",
    ),
    (
        '      ["Design Arena (human preference)",arenaCell(baseline),arenaCell(other)],',
        '      ["Max duration (catalogue)",durationCell(baseline),durationCell(other)],\n'
        '      ["Audio (catalogue)",audioCell(baseline),audioCell(other)],',
        "comparison arena row",
    ),
    (
        "is ${percent}% cheaper per benchmark generation",
        "is ${percent}% cheaper per generated clip",
        "describe cost sentence",
    ),
    (
        "and benchmark cost is not normalised for resolution, so read the cost difference with that in mind.",
        "and cost per clip is not resolution-normalised, so read the cost difference with that in mind.",
        "describe resolution sentence",
    ),
    (
        "  const durationCell=row=>row.max_duration_seconds==null?'<span class=\"num-dim\">—</span>':`${num(row.max_duration_seconds)}s`;",
        "  const durationCell=row=>row.max_duration_seconds==null?'<span class=\"num-dim\">—</span>':`${num(row.max_duration_seconds)}s`;\n"
        "  // Tri-state: true, false, or unknown when the catalogue does not publish it.\n"
        "  const audioCell=row=>row.generate_audio===true?\"Yes\":(row.generate_audio===false?\"No\":'<span class=\"num-dim\">—</span>');",
        "audio cell",
    ),
]

VIDEO_BANNER_PROSE = """<strong>Same metric, different units.</strong> Performance here is every judged check a model passed
  divided by every check it attempted, pooled flat across its benchmark prompts. Price is the mean
  <code>cost_usd</code> of those same generations — the observed cost of one generated clip, <em>not</em>
  the catalogue's per-output-second rate — so performance and price come from identical rows and are
  directly comparable to each other. Two caveats travel with that cost, and both are structural. The
  catalogue rate is quoted per output second on a resolution- and audio-dependent basis, so it is shown
  beside the observed cost and never merged into it. And the benchmark pages do not publish the clip
  length they generated, so two models' observed costs may describe clips of different durations — a
  cheaper model is not necessarily cheaper for the same output. Generation time is captured per row and
  is shown separately from cost and quality. There is no video preference or arena score to show: judged
  prompt results are the whole of the quality evidence, and no Artificial Analysis data exists for video
  models at all."""

PROMPT_COVERAGE_SECTION_HTML = """<!-- 05 — prompt coverage ----------------------------------------------------->
<section class="card" aria-labelledby="promptCoverageTitle">
  <div class="card-head">
    <div>
      <p class="eyebrow">Benchmark coverage</p>
      <h2 id="promptCoverageTitle">Judged prompts, per model</h2>
      <p class="card-sub">Every prompt OpenRouter judged for video, and how each benchmarked model scored on it. This stands where a preference survey would: no video arena or Elo data is published for these models, so judged prompt results are the whole of the quality evidence, and they are deliberately kept out of the value score.</p>
    </div>
  </div>
  <div class="table-scroll">
    <table id="promptCoverageTable">
      <thead><tr id="promptCoverageHead"><th>Model</th></tr></thead>
      <tbody id="promptCoverageBody"></tbody>
    </table>
  </div>
  <p class="card-sub" id="promptCoverageNote" style="margin-top:14px;"></p>
</section>

"""

PROMPT_COVERAGE_JS = """// ----------------------------------------------------------- prompt coverage --
// The panel that stands in for a preference survey. Rows are the benchmark rows
// themselves, so it cannot tell a different story from the pooled pass rate.
(()=>{
  const panel=PAYLOAD.prompt_coverage||{prompts:[],models:[]};
  const prompts=panel.prompts||[];
  const byModel={};
  (panel.models||[]).forEach(m=>{byModel[m.model_id]=m.cells||{}});

  const head=byId("promptCoverageHead");
  if(head){
    prompts.forEach(p=>{
      const th=document.createElement("th");
      th.textContent=p.name||p.slug;
      th.title=`${p.name||p.slug}: ${num(p.row_count)} judged generation(s)`;
      head.appendChild(th);
    });
  }

  const ordered=ROWS.slice().filter(r=>r.benchmarked).sort((a,b)=>{
    const ar=a.value_rank==null?1e9:a.value_rank,br=b.value_rank==null?1e9:b.value_rank;
    return ar-br||(a.model_id||"").localeCompare(b.model_id||"");
  });
  setHtml("promptCoverageBody",ordered.map(row=>{
    const cells=byModel[row.model_id]||{};
    let out=`<td class="name-cell"><span class="model-name">${esc(row.model_name||row.model_id)}</span>`
      +`<div class="model-provider">${esc(row.provider||"")}${row.rankable?"":" · not rankable"}</div></td>`;
    prompts.forEach(p=>{
      const cell=cells[p.slug];
      out+=cell&&cell.pass_rate!=null
        ?`<td title="cost ${esc(money(cell.cost_usd))}">${esc(pct(cell.pass_rate))}<div class="model-provider">${esc(num(cell.checks_passed))}/${esc(num(cell.checks_total))}</div></td>`
        :'<td><span class="num-dim">—</span></td>';
    });
    return `<tr>${out}</tr>`;
  }).join("")||'<tr><td class="num-dim">No judged prompt rows in this snapshot.</td></tr>');

  setText("promptCoverageNote",`${esc(panel.note||"")} ${num(panel.model_count)} model(s), ${num(panel.prompt_count)} prompt(s), ${num(panel.row_count)} judged row(s). A dash means that model was not judged on that prompt - it is missing evidence, not a failed attempt.`);
})();

"""

# ---------------------------------------------------------------------------
# the reference-table row builder (11 video columns)
# ---------------------------------------------------------------------------
PRICING_ROW_JS = """  setHtml("pricingBody",ordered.map(row=>{
    const audio=row.generate_audio===true?"Yes":(row.generate_audio===false?"No":'<span class="num-dim">—</span>');
    const frames=(row.frame_images||[]).length?row.frame_images.join(", "):'<span class="num-dim">—</span>';
    return `<tr><td class="name-cell"><span class="model-name">${esc(row.model_name||row.model_id)}</span><div class="model-provider">${esc(row.provider||"")}</div></td>`
      +`<td>${row.value==null?'<span class="num-dim">Unrated</span>':esc(value(row.value))}</td>`
      +`<td>${row.avg_cost_usd==null?'<span class="num-dim">—</span>':esc(money(row.avg_cost_usd))}</td>`
      // The resolution the benchmark generation ran at - the cost caveat's
      // subject, shown per model so the cost can be read against it. A model
      // whose rows span several sizes is marked rather than silently averaged.
      +`<td>${(row.output_resolutions||[]).length?esc(row.output_resolutions.join(", "))+(row.resolution_mixed?' <span class="num-dim">(mixed)</span>':''):'<span class="num-dim">not recorded</span>'}</td>`
      +`<td>${row.duration_ms_mean==null?'<span class="num-dim">—</span>':esc(seconds(row.duration_ms_mean))}</td>`
      // A missing catalogue rate is an em dash WITH its reason. Never a zero:
      // a model that publishes only per-token rates has no per-second price at
      // all, which is a different statement from "it is free".
      +`<td>${row.comparable_price==null?'<span class="num-dim">—</span>':esc(money(row.comparable_price))}</td>`
      +`<td>${row.cost_per_5s_clip==null?'<span class="num-dim">—</span>':esc(money(row.cost_per_5s_clip))}</td>`
      +`<td class="name-cell num-dim">${row.comparable_price==null?esc(row.rate_absence_label||"no published rate"):(row.comparable_price_basis?esc(row.comparable_price_basis):'—')}</td>`
      +`<td>${row.max_duration_seconds==null?'<span class="num-dim">—</span>':esc(row.max_duration_seconds+"s")}</td>`
      +`<td>${audio}</td>`
      +`<td class="name-cell num-dim">${frames}</td></tr>`;
  }).join(""));"""


def derive(source: str) -> str:
    html = source

    for edits in (HEAD_EDITS, BANNER_EDITS, HERO_EDITS, VALUE_LEADER_EDITS, SCATTER_EDITS,
                  KPI_EDITS, BUDGET_EDITS, COVERAGE_EDITS, TABLE_EDITS, TREND_EDITS,
                  PROVENANCE_EDITS, LEFTOVER_EDITS):
        for old, new, label in edits:
            html = replace_once(html, old, new, label=label)

    # Design Arena section (HTML) -> prompt coverage section.
    html = replace_span(
        html,
        '<section class="card" aria-labelledby="arenaTitle">',
        '</section>\n\n<!-- 06 — coverage',
        PROMPT_COVERAGE_SECTION_HTML + "<!-- 06 — coverage",
        label="arena section",
    )

    # The two arena helper closures are dead once the arena section is gone, and
    # they read ARENA, which the payload no longer carries.
    html = replace_span(
        html,
        "const arenaEntry=modelId=>",
        "const rankCell=",
        "  const rankCell=",
        label="arena helpers",
    )

    # The banner is this page's methodology note in the page chrome. The payload's
    # methodology_note carries the long form, but the units here still described
    # images and a preference survey.
    html = replace_span(
        html,
        "<strong>Same metric, different units.</strong>",
        "models at all.",
        VIDEO_BANNER_PROSE,
        label="banner prose",
    )

    # Design Arena JS -> prompt coverage JS.
    html = replace_span(
        html,
        "// --------------------------------------------------------------- design arena --",
        "// ----------------------------------------------------------------- coverage --",
        PROMPT_COVERAGE_JS + "// ----------------------------------------------------------------- coverage --",
        label="arena script",
    )

    # The reference table's row builder.
    html = replace_span(
        html,
        'setHtml("pricingBody",ordered.map(row=>{',
        '}).join(""));',
        PRICING_ROW_JS,
        label="pricing rows",
    )

    return html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="Verify the derived template matches the file on disk; write nothing.")
    args = parser.parse_args()

    if not SOURCE_PATH.exists():
        print(f"missing {SOURCE_PATH}", file=sys.stderr)
        return 1

    derived = derive(SOURCE_PATH.read_text(encoding="utf-8"))

    if args.check:
        current = TARGET_PATH.read_text(encoding="utf-8") if TARGET_PATH.exists() else ""
        if current != derived:
            print(f"{TARGET_PATH.name} is stale relative to {SOURCE_PATH.name} - "
                  f"run: python dashboard/derive_video_template.py", file=sys.stderr)
            return 1
        print(f"{TARGET_PATH.name} matches its derivation from {SOURCE_PATH.name}")
        return 0

    TARGET_PATH.write_text(derived, encoding="utf-8")
    print(f"Wrote {TARGET_PATH} ({len(derived)} bytes) from {SOURCE_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
