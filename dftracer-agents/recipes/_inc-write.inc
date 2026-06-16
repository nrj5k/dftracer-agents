══════════════════════════════════════════════════════
STEP 5 — WRITE BACK
══════════════════════════════════════════════════════

session_write_file(run_id="{{ run_id }}", filepath="{{ filepath }}",
  content=<COMPLETE file content>, subfolder="annotated")

Written file MUST have MORE lines than the original.
If it is shorter, you truncated it — re-read the original and rewrite.

