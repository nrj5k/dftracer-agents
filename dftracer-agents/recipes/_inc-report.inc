  ══════════════════════════════════════════════════════
  STEP 6.5 — FLUSH NEW PITFALLS TO LESSONS FILE
  ══════════════════════════════════════════════════════
  
  If you discovered any pitfalls during the annotation loop that you have NOT yet
  written to the lessons file (because you wrote them inline during Step 4), verify
  they are present now:
  
  ```bash
  SKILL=/workspaces/dftracer-agents/.agents/skills/dftracer-annotation-lessons/SKILL.md
  grep -c "^---" "$SKILL" 2>/dev/null || echo "0 lessons"
  ```
  
  If any new patterns from this file are missing, append them NOW using the
  template from the PER-FUNCTION INCREMENTAL ANNOTATION LOOP section.
  Include: exact error text, root cause, fix or skip reason, and relevant tags.
  
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
  PG9  Wrong tool name (-32002)  Dot notation not valid: use todo__todo_write not
                                 todo.todoWrite; use load not read_file; use
                                 dftracer__session_read_file not session_read_file
  PG10 Revert-all on build error Do NOT revert the whole file on a syntax/build error.
                                 Strip macros from the FAILING FUNCTION ONLY, mark it
                                 PENDING, and continue annotating the remaining functions.
                                 Write the pitfall to lessons-learned immediately.
  
