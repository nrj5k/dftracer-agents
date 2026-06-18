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
PROPOSAL FORMAT (use this exact structure each time)
══════════════════════════════════════════════════════

For every proposed optimization, print:

  PROPOSAL <ID>: <one-line title>
    Bottleneck:  <metric name> (score <N>/5, <severity>)
    Literature:  <paper title or "arXiv: <title>", section/insight if known>
    Change:      <what will be modified — file path, parameter name, value>
    Expected Δ:  <expected improvement in terms of METRIC — e.g. "+15–30% bandwidth">
    Risk:        LOW | MEDIUM | HIGH  and one-line rationale
    ────────────────────────────────────────────────
    Accept / Skip / Modify? [A/S/M]:

Wait for user input before applying. If user types M, ask what modification they want,
apply the modified version. If user types S, log it as skipped and move on.

If no proposals can be derived for this level, print:
  NO OPTIMIZATIONS AVAILABLE AT THIS LEVEL
  Reason: <bottleneck types do not map to layer-N changes, or papers found no relevant
           strategies for these metrics>

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
