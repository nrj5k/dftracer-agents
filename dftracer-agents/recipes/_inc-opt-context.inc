══════════════════════════════════════════════════════
TOOL NAMES — GOOSE ENVIRONMENT (read before calling anything)
══════════════════════════════════════════════════════

MCP tool prefix: all dftracer tools use `dftracer__` prefix.
Developer tools: `shell`, `load`, `write`, `edit`, `tree` — no prefix.

══════════════════════════════════════════════════════
LOAD OPTIMIZATION CONTEXT
══════════════════════════════════════════════════════

WS=/workspaces/dftracer-agents/workspaces/{{ run_id }}

Read all context files before proposing anything:

```bash
cat ${WS}/optimization_context.json
cat ${WS}/diagnosis.json
cat ${WS}/optimization_papers.json 2>/dev/null || echo "(no papers file yet)"
```

Extract from optimization_context.json:
  METRIC        ← chosen_metric  (time | bandwidth | iops | metadata_ops)
  BOTTLENECKS   ← list under "bottlenecks" (high/critical metrics, score ≥ 4)
  PAPERS        ← list under "relevant_papers"
  APP_LANG      ← application language (C/C++/Python) from context or status

METRIC-to-goal mapping:
  time          → minimize I/O latency; target: read_time_pct, write_time_pct, metadata_time_pct
  bandwidth     → maximize throughput; target: read_bw_mean, write_bw_mean, seq_pct
  iops          → maximize I/O operations/sec; target: small_io_pct, rand_pct, intensity_mean
  metadata_ops  → reduce metadata overhead; target: metadata_time_pct, metadata_time_frac_parent

══════════════════════════════════════════════════════
CITATION RULE — MANDATORY FOR EVERY PROPOSAL
══════════════════════════════════════════════════════

BEFORE building any proposal, search for supporting literature using the
dftracer paper MCP tools.  A proposal with no verifiable citation MUST NOT
be presented to the user.

Citation search order (run for each unique bottleneck type):

  1. Check PAPERS from optimization_context.json first (already fetched).
  2. Search arXiv:
       dftracer__search_arxiv(
         query="<bottleneck_type> optimization HPC parallel I/O MPI",
         max_results=5, category="cs.DC"
       )
  3. Search Semantic Scholar / combined:
       dftracer__search_papers_combined(
         query="<bottleneck_type> I/O tuning parallel filesystem",
         max_results=5
       )
  4. For any promising arXiv hit, fetch the full record for URL + abstract:
       dftracer__get_arxiv_paper(paper_id="<arxiv_id>")

For every citation you use, record:
  - authors, title, venue/journal, year
  - URL: arXiv link, DOI, or page URL (e.g. https://arxiv.org/abs/XXXX.XXXXX)
  - the specific finding or section that supports the proposed change

If the search returns no relevant results and the built-in references below
also do not apply, write "no L<N> fix — no citation available" and skip
the proposal entirely.

Built-in citable references (always available — use when search confirms them):

  WisIO (Yildirim et al., ICS 2025)
    Title:  "WisIO: Automated I/O Bottleneck Detection with Multi-Perspective
             Views for HPC Workflows"
    Venue:  Proc. 39th ACM International Conference on Supercomputing, pp. 749–763
    URL:    https://dl.acm.org/doi/10.1145/3721145.3730395
    Covers: small_io, rand/seq, read_time, write_time, metadata_time, fetch_pressure

  Drishti (Bez et al., PDSW 2022)
    Title:  "Drishti: Guiding end-users in the I/O optimization journey"
    Venue:  2022 IEEE/ACM International Parallel Data Systems Workshop (PDSW), pp. 1–6
    URL:    https://ieeexplore.ieee.org/document/10027503
    Covers: small-io buffering, metadata caching, sequentiality hints, collective I/O

══════════════════════════════════════════════════════
PROPOSAL FORMAT (use this exact structure each time)
══════════════════════════════════════════════════════

For every proposed optimization, print:

  PROPOSAL <ID>: <one-line title>
    Bottleneck:  <metric name> (score <N>/5, <severity>)
    Citation:    <Author(s), "Title", Venue Year>
                 URL: <https://... — arXiv link, DOI, or webpage>
                 Finding: <the specific result/section/recommendation that
                           justifies this change — 1–2 sentences>
    Change:      <what will be modified — file path, parameter name, value>
    Expected Δ:  <expected improvement in terms of METRIC — e.g. "+15–30% bandwidth">
    Risk:        LOW | MEDIUM | HIGH  and one-line rationale
    ────────────────────────────────────────────────
    Accept / Skip / Modify? [A/S/M]:

  ⚠ If you cannot provide a Citation with a URL for a proposal, do NOT
    present it. Run more searches or mark the bottleneck as "no L<N> fix".

Wait for user input before applying. If user types M, ask what modification they want,
apply the modified version. If user types S, log it as skipped and move on.

If no proposals can be derived for this level, print:
  NO OPTIMIZATIONS AVAILABLE AT THIS LEVEL
  Reason: <bottleneck types do not map to layer-N changes, or papers found no relevant
           strategies for these metrics, or no citation could be found>

══════════════════════════════════════════════════════
WRITE RESULTS FILE
══════════════════════════════════════════════════════

After applying all accepted proposals, write a JSON summary:

```bash
cat > ${WS}/opt_{{ level }}_results.json << 'EOF'
{
  "level": "{{ level }}",
  "metric": "<METRIC>",
  "proposals": [
    {
      "id": "<ID>",
      "title": "<title>",
      "bottleneck": "<metric>",
      "status": "applied | skipped | modified",
      "change_description": "<what was done>",
      "files_modified": ["<path>", ...],
      "expected_delta": "<string>"
    }
  ],
  "validation": {
    "rerun_done": true | false,
    "before_summary": "<key metrics from original analysis>",
    "after_summary": "<key metrics from re-analysis>"
  }
}
EOF
```

Then print:
  LEVEL <N> COMPLETE
  Applied: <N> optimizations | Skipped: <N>
  See: ${WS}/opt_{{ level }}_results.json
