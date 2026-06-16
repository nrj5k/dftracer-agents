══════════════════════════════════════════════════════
STEP 7 — OUTPUT PER-FILE REPORT
══════════════════════════════════════════════════════

Output exactly this structure (pipeline agent collects it):

FILE: {{ filepath }}
STATUS: DONE | PARTIAL | FAILED
ANNOTATED: <N> functions  (io=<n>, comm=<n>, mem=<n>, cpu=<n>)
SKIPPED:   <function names with Rule 0 justification, one per line>
PENDING:   <functions not yet annotated with reason>
NEW_ERRORS: <any new compile/runtime errors discovered>
LESSONS:   <new patterns or pitfalls not yet in the lessons file>

══════════════════════════════════════════════════════
GENERAL PITFALLS (apply to all languages)
══════════════════════════════════════════════════════

PG1  File truncated            Written lines < original → re-read, rewrite complete file
PG2  Header file annotated     Macros in .h/.hpp → move to .c/.cpp source file
PG3  comp= missing             Annotation count ≠ comp count → fix each gap
PG4  Lifecycle fn skipped      *_init/*_finalize short body → always annotate
PG5  Vendor fn skipped         gpfs_*/beegfs_*/lustre_* → comp="io", always annotate
PG6  Re-annotating dirty file  File already has macros → restore original first
PG7  Coverage check skipped    Always run Step 6 before reporting DONE
PG8  UPDATE uses fwd-decl name 'param' undeclared → read names from definition body

