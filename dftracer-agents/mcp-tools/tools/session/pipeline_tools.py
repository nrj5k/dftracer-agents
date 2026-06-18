"""
Pipeline orchestration tools and run-ID management tools.
"""
from __future__ import annotations

import difflib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from .workspace import (
    _ws, _load_state, _save_state, _write_artifact_log,
    _ok, _err, _new_run_id, _create_run, _run, _workspaces_root, _derive_app_name,
)
from .detection import _detect_info
from .annotation import (
    _annotate_c_source, _annotate_python_source,
    _fix_dftracer_annotation_errors,
    _strip_mpi_launcher,
    _generate_annotation_report,
)
from .build import (
    _patch_cmake, _patch_setup_py, _patch_pyproject,
    _patch_autotools_makefile, _find_c_entry_points,
    _find_python_entry_points, _guess_smoke_test,
)
from .install import (
    _install_dftracer_autobuild, _dftracer_utils_split,
)


def register_pipeline_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def session_run_pipeline(
        url: str,
        ref: str = "main",
        smoke_test_command: Optional[str] = None,
        extra_cmake_flags: str = "",
        jobs: int = 4,
        run_id: Optional[str] = None,
        skip_annotation: bool = False,
        annotation_confirmed: bool = False,
        dftracer_ref: str = "v2.0.3",
    ) -> str:
        """
        Full dftracer annotation + smoke-test pipeline.

        Executes all steps in sequence and pauses after step 8 to show the
        user an annotation coverage report.  The user must confirm before the
        pipeline continues to build, run, and analyse.

        Normal flow:
          1.   Create session and clone source (run_id derived from url)
          2.   Detect language, build tool, and dftracer features
          3.   Configure build
          4.   Build and install
          5.   Run smoke test (with auto-detected command if not provided)
          6.   Copy source to annotated/
          7.   Patch build system for dftracer
          8.   Auto-annotate C/C++ and Python source
          >>>  PAUSE — show annotation report, ask user to confirm <<<
          8.5. Install dftracer into install_ann/ (C/C++) or venv (Python)
          9.   Build annotated source with dftracer (CMAKE_PREFIX_PATH set)
          10.  Run smoke test with dftracer (traces collected)
          11.  dftracer_split — compact traces
          12.  dftracer_info  — summarise traces

        To resume after confirming the annotation report:
          Call this tool again with the same run_id and annotation_confirmed=True.
          Steps 1–8 are skipped; the pipeline resumes from step 8.5.

        If a step fails the pipeline stops and reports which step failed.

        Args:
            url:                  Git URL to clone (also used to derive app name
                                  and run_id when annotation_confirmed=True).
            ref:                  Branch, tag, or commit (default: main).
            smoke_test_command:   Shell command to verify the build.
            extra_cmake_flags:    Extra cmake -D flags for both builds.
            jobs:                 Parallel make jobs.
            run_id:               Fixed RUN-ID.  Required when
                                  annotation_confirmed=True to identify the run.
            skip_annotation:      If True, stop after step 5 (original smoke test).
            annotation_confirmed: If True and run_id is set, skip steps 1–8 and
                                  resume from step 8.5 (dftracer install + build).
            dftracer_ref:         dftracer git tag/branch to install (default: v2.0.3).
        """
        report: Dict[str, Any] = {}

        # ── Normal path (steps 1–8): clone, build, annotate, then PAUSE ───────
        if not (annotation_confirmed and run_id):
            rid, ws = _create_run(url, run_id)
            src = ws / "source"
            src.mkdir(exist_ok=True)

            # Step 1: clone
            clone_r = _run(
                ["git", "clone", "--depth", "1", "--branch", ref, url, str(src)],
                timeout=300,
            )
            if not clone_r["success"]:
                shutil.rmtree(src, ignore_errors=True)
                src.mkdir(exist_ok=True)
                clone_r = _run(["git", "clone", "--depth", "1", url, str(src)], timeout=300)
                if not clone_r["success"]:
                    return _err("Step 1 failed: git clone", step=1, **clone_r)
                _run(["git", "checkout", ref], cwd=src)
            report["step_1_clone"] = {"status": "ok", "run_id": rid}
            _write_artifact_log(ws, 1, "session_create",
                                {"clone": clone_r, "run_id": rid, "url": url, "ref": ref}, rid)

            # Step 2: detect
            info = _detect_info(src)
            bt = info["build_tool"]
            _save_state(rid, {"run_id": rid, "url": url, "ref": ref,
                               "workspace": str(ws), "detection": info})
            report["step_2_detect"] = {
                "status": "ok",
                "languages": info["languages"],
                "build_tool": bt,
                "features": info["features"],
                "dftracer_cmake_flags": info["dftracer_cmake_flags"],
            }
            _write_artifact_log(ws, 2, "session_detect", report["step_2_detect"], rid)

            # Step 3: configure
            build = ws / "build"
            install = ws / "install"
            build.mkdir(exist_ok=True)
            install.mkdir(exist_ok=True)

            cmake_flags = [
                f"-DCMAKE_INSTALL_PREFIX={install}",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            ] + (extra_cmake_flags.split() if extra_cmake_flags else [])

            if bt == "cmake":
                cfg_r = _run(["cmake", "-S", str(src), "-B", str(build)] + cmake_flags, timeout=300)
            elif bt == "autotools":
                if (src / "configure.ac").exists() and not (src / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=src, timeout=120)
                cfg_r = _run([str(src / "configure"), f"--prefix={install}"], cwd=build, timeout=300)
            elif bt == "python":
                cfg_r = _run(["python3", "-m", "venv", str(install)], timeout=60)
                if cfg_r["success"]:
                    pip = install / "bin" / "pip"
                    cfg_r = _run([str(pip), "install", "-e", str(src)], timeout=300)
            else:
                cfg_r = {"success": False, "returncode": -1,
                          "stdout": "", "stderr": f"Unknown build tool: {bt}"}

            report["step_3_configure"] = cfg_r
            _write_artifact_log(ws, 3, "session_configure",
                                {"configure": cfg_r, "build_tool": bt}, rid)
            if not cfg_r["success"]:
                return _err("Step 3 failed: configure", step=3, report=report)

            # Step 4: build + install
            if bt in {"cmake", "autotools", "make"}:
                bld_r = _run(["make", f"-j{jobs}"], cwd=build, timeout=600)
                report["step_4_build"] = bld_r
                if not bld_r["success"]:
                    return _err("Step 4 failed: make", step=4, report=report)
                ins_r = _run(["make", "install"], cwd=build, timeout=300)
                report["step_4_install"] = ins_r
                if not ins_r["success"]:
                    return _err("Step 4 failed: make install", step=4, report=report)
            else:
                report["step_4_build"] = {"status": "skipped (python)"}
            _write_artifact_log(ws, 4, "session_build_install", {
                k: v for k, v in report.items() if k.startswith("step_4")
            }, rid)

            # Step 5: original smoke test
            smoke_cmd = smoke_test_command or _guess_smoke_test(src, bt, install)
            if smoke_cmd:
                sm_r = _run(["/bin/sh", "-c", smoke_cmd], cwd=build, timeout=300)
                report["step_5_smoke_test"] = {**sm_r, "command": smoke_cmd}
                if not sm_r["success"]:
                    report["step_5_smoke_test"]["warning"] = (
                        "Original smoke test failed — continuing to annotation phase"
                    )
            else:
                report["step_5_smoke_test"] = {"status": "no smoke test detected"}
            _write_artifact_log(ws, 5, "session_run_smoke_test",
                                report["step_5_smoke_test"], rid)
            _save_state(rid, {"step": "original_build_done", "detection": info})

            if skip_annotation:
                return _ok(
                    "Pipeline complete (annotation skipped)",
                    run_id=rid, workspace=str(ws), report=report,
                )

            # Step 6: copy to annotated/
            ann = ws / "annotated"
            if ann.exists():
                shutil.rmtree(ann)
            shutil.copytree(src, ann)
            report["step_6_copy_annotated"] = {"status": "ok", "path": str(ann)}
            _write_artifact_log(ws, 6, "session_copy_annotated",
                                report["step_6_copy_annotated"], rid)

            # Step 7: patch build system
            patched: List[str] = []
            if bt == "cmake":
                cml = ann / "CMakeLists.txt"
                if cml.exists():
                    cml.write_text(_patch_cmake(cml))
                    patched.append("CMakeLists.txt")
            elif bt == "autotools":
                for mf in ann.glob("Makefile*"):
                    mf.write_text(_patch_autotools_makefile(mf))
                    patched.append(mf.name)
            elif bt == "python":
                for pname, pfn in (("setup.py", _patch_setup_py),
                                   ("pyproject.toml", _patch_pyproject)):
                    pp = ann / pname
                    if pp.exists():
                        pp.write_text(pfn(pp))
                        patched.append(pname)
            report["step_7_patch_build"] = {"status": "ok", "patched": patched}
            _write_artifact_log(ws, 7, "session_patch_build",
                                report["step_7_patch_build"], rid)

            # Step 8: auto-annotate source
            c_entries = {str(p) for p in _find_c_entry_points(ann)}
            py_entries = {str(p) for p in _find_python_entry_points(ann)}
            annotated: List[str] = []
            langs = info.get("languages", [])

            if "c" in langs or "cpp" in langs:
                for ext in ("*.c", "*.h", "*.cpp", "*.cxx", "*.cc", "*.hpp"):
                    for f in ann.rglob(ext):
                        try:
                            old = f.read_text(errors="ignore")
                            new = _annotate_c_source(old, f, is_entry=str(f) in c_entries)
                            if new != old:
                                f.write_text(new)
                                annotated.append(str(f.relative_to(ann)))
                        except OSError:
                            pass

            if "python" in langs:
                for f in ann.rglob("*.py"):
                    try:
                        old = f.read_text(errors="ignore")
                        new = _annotate_python_source(old, is_entry=str(f) in py_entries)
                        if new != old:
                            f.write_text(new)
                            annotated.append(str(f.relative_to(ann)))
                    except OSError:
                        pass

            ann_patch = ws / "annotation.patch"
            ann_patch_chunks: List[str] = []
            for f_rel in annotated:
                src_f = ws / "source" / f_rel
                ann_f = ann / f_rel
                if src_f.exists() and ann_f.exists():
                    ann_patch_chunks.append("".join(difflib.unified_diff(
                        src_f.read_text(errors="ignore").splitlines(keepends=True),
                        ann_f.read_text(errors="ignore").splitlines(keepends=True),
                        fromfile=f"a/{f_rel}", tofile=f"b/{f_rel}",
                    )))
            ann_patch.write_text("".join(ann_patch_chunks))

            report["step_8_annotate"] = {
                "status": "ok",
                "files_annotated": len(annotated),
                "annotated": annotated,
                "patch_file": str(ann_patch),
            }
            _write_artifact_log(ws, 8, "session_annotate_source",
                                report["step_8_annotate"], rid)
            _save_state(rid, {"step": "annotation_done"})

            # ── PAUSE: show coverage report and ask user to confirm ────────────
            ann_report = _generate_annotation_report(ws, rid)
            s = ann_report.get("summary", {})
            annotatable = s.get("total_functions", 0) - s.get("skipped", 0)
            return _ok(
                f"Step 8 complete — annotation done. "
                f"{s.get('annotated', '?')}/{annotatable} functions annotated "
                f"({s.get('coverage_pct', '?')}% coverage, "
                f"{s.get('relevant_files', '?')} file(s) modified). "
                f"Review the annotation_report below. "
                f"If satisfied, call session_run_pipeline again with "
                f"annotation_confirmed=True and run_id='{rid}' to continue "
                f"to steps 8.5–12 (dftracer install, build, trace, analyse). "
                f"To add missing annotations first, use session_read_file / "
                f"session_write_file on the run_id='{rid}' workspace, then confirm.",
                awaiting_confirmation=True,
                annotation_report=ann_report,
                run_id=rid,
                workspace=str(ws),
                step_reports=report,
            )
        # ── end of normal path (always returns above) ─────────────────────────

        # ── Resume path: annotation_confirmed=True — load state, run 8.5–12 ───
        ws = _ws(run_id)
        if not ws.exists():
            return _err(f"Workspace not found for run_id: {run_id}")
        state = _load_state(run_id)
        rid = run_id
        info = state.get("detection", {})
        bt = info.get("build_tool", "unknown")
        ann = ws / "annotated"
        install = ws / "install"
        if not ann.exists():
            return _err(
                "annotated/ directory not found — complete steps 1–8 first "
                "(call session_run_pipeline without annotation_confirmed=True)",
                run_id=rid,
            )
        report["resumed_from"] = "step_8_5"

        # --- Step 8.5: install dftracer into install_ann/ (C/C++) or venv (Python) ---
        build_ann = ws / "build_ann"
        install_ann = ws / "install_ann"
        build_ann.mkdir(exist_ok=True)
        install_ann.mkdir(exist_ok=True)

        # Always use pip — cmake mode is fragile across autobuild.sh versions
        dft_r = _install_dftracer_autobuild(
            ws=ws,
            install_prefix=install_ann,
            dftracer_ref=dftracer_ref,
            jobs=jobs,
            install_mode="pip",
            features=info.get("features", {}),
            python_exe=sys.executable,
        )
        report["step_8_5_install_dftracer"] = {
            "ref": dftracer_ref,
            "steps": dft_r["steps"],
            "success": dft_r["success"],
        }
        _write_artifact_log(ws, 9, "session_install_dftracer", report["step_8_5_install_dftracer"], rid)
        if not dft_r["success"]:
            return _err("Step 8.5 failed: dftracer autobuild (pip mode)", step="8.5", report=report)
        dft_prefix = str(install_ann)

        _save_state(rid, {"dftracer_install_prefix": dft_prefix})

        # --- Steps 9-10: build annotated + run with dftracer (retry loop) ---
        # Automatically fixes dftracer annotation errors and retries up to
        # MAX_ANNOTATION_RETRIES times before giving up.
        traces_dir = ws / "traces"
        traces_dir.mkdir(exist_ok=True)
        dftracer_env = {
            "DFTRACER_ENABLE": "1",
            "DFTRACER_INC_METADATA": "1",
            "DFTRACER_LOG_FILE": str(traces_dir / "trace"),
            "DFTRACER_DATA_DIR": str(src),
            "DFTRACER_INIT": "FUNCTION",
        }
        ann_smoke_cwd = build_ann if build_ann.exists() else ann
        MAX_ANNOTATION_RETRIES = 3
        build_ok = False
        run_ok = not bool(smoke_cmd)  # trivially ok if there is no smoke command

        for attempt in range(1, MAX_ANNOTATION_RETRIES + 1):
            sfx = f"_attempt{attempt}"
            build_step: Dict[str, Any] = {"attempt": attempt}

            # Wipe the build dir on retries to avoid stale object files
            if attempt > 1:
                shutil.rmtree(build_ann, ignore_errors=True)
                build_ann.mkdir(exist_ok=True)

            # ---- build ----
            if bt == "cmake":
                ann_flags = [
                    f"-DCMAKE_INSTALL_PREFIX={install_ann}",
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                ]
                if dft_prefix:
                    ann_flags.append(f"-DCMAKE_PREFIX_PATH={dft_prefix}")
                ann_flags += (extra_cmake_flags.split() if extra_cmake_flags else [])
                r_ac = _run(["cmake", "-S", str(ann), "-B", str(build_ann)] + ann_flags, timeout=300)
                build_step["configure"] = r_ac
                if r_ac["success"]:
                    r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann, timeout=600)
                    build_step["build"] = r_ab
                    if r_ab["success"]:
                        _run(["make", "install"], cwd=build_ann, timeout=300)
                        build_ok = True

            elif bt == "autotools":
                if (ann / "configure.ac").exists() and not (ann / "configure").exists():
                    _run(["autoreconf", "-fi"], cwd=ann, timeout=120)
                env_ann: Dict[str, str] = {}
                if dft_prefix:
                    env_ann["PKG_CONFIG_PATH"] = f"{dft_prefix}/lib/pkgconfig"
                    env_ann["CPPFLAGS"] = f"-I{dft_prefix}/include"
                    env_ann["LDFLAGS"] = f"-L{dft_prefix}/lib -Wl,-rpath,{dft_prefix}/lib"
                r_ac = _run(
                    [str(ann / "configure"), f"--prefix={install_ann}"],
                    cwd=build_ann, env=env_ann or None, timeout=300,
                )
                build_step["configure"] = r_ac
                if r_ac["success"]:
                    r_ab = _run(["make", f"-j{jobs}"], cwd=build_ann,
                                env=env_ann or None, timeout=600)
                    build_step["build"] = r_ab
                    if r_ab["success"]:
                        _run(["make", "install"], cwd=build_ann, timeout=300)
                        build_ok = True

            elif bt == "python":
                pip = ws / "install" / "bin" / "pip"
                if not pip.exists():
                    pip = Path("pip3")
                r_ab = _run([str(pip), "install", "-e", str(ann)], timeout=300)
                build_step["build"] = r_ab
                build_ok = r_ab["success"]

            report[f"step_9{sfx}"] = build_step
            _write_artifact_log(ws, 10, f"session_build_annotated{sfx}", build_step, rid)

            if not build_ok:
                # Collect all stderr from this attempt
                build_stderr = "\n".join(
                    v.get("stderr", "") for v in build_step.values()
                    if isinstance(v, dict)
                )
                fixed = _fix_dftracer_annotation_errors(ann, build_stderr)
                report[f"step_9_fix{sfx}"] = {
                    "fixed_files": fixed,
                    "stderr_excerpt": build_stderr[:500],
                }
                if fixed and attempt < MAX_ANNOTATION_RETRIES:
                    # Write fix log and try again
                    _write_artifact_log(ws, 10, f"session_build_fix{sfx}",
                                        report[f"step_9_fix{sfx}"], rid)
                    continue
                return _err(
                    f"Step 9 failed after {attempt} attempt(s): build annotated",
                    step=9, attempts=attempt,
                    fixed_files=fixed,
                    hint=(
                        "Automated fix was unable to resolve all errors. "
                        "Use session_read_file + session_write_file to manually "
                        "correct the annotation in annotated/, then call "
                        "session_build_annotated to rebuild."
                    ),
                    report=report,
                )

            # ---- run with dftracer ----
            if smoke_cmd:
                # Clear stale traces from previous attempts
                for tf in traces_dir.glob("*.pfw*"):
                    tf.unlink()

                sm2_r = _run(
                    ["/bin/sh", "-c", smoke_cmd],
                    cwd=ann_smoke_cwd, env=dftracer_env, timeout=300,
                )
                report[f"step_10{sfx}"] = {**sm2_r, "command": smoke_cmd, "attempt": attempt}
                _write_artifact_log(ws, 11, f"session_run_with_dftracer{sfx}",
                                    report[f"step_10{sfx}"], rid)
                run_ok = sm2_r["success"]

                if run_ok:
                    break  # both build and run succeeded

                # Run failed — try annotation fix and rebuild
                run_stderr = sm2_r.get("stderr", "")
                fixed = _fix_dftracer_annotation_errors(ann, run_stderr)
                report[f"step_10_fix{sfx}"] = {
                    "fixed_files": fixed,
                    "stderr_excerpt": run_stderr[:500],
                }
                if fixed and attempt < MAX_ANNOTATION_RETRIES:
                    _write_artifact_log(ws, 11, f"session_run_fix{sfx}",
                                        report[f"step_10_fix{sfx}"], rid)
                    build_ok = False  # force rebuild on next iteration
                    continue
                # No fixable dftracer issue — stop retrying
                return _err(
                    f"Step 10 failed after {attempt} attempt(s): smoke test with dftracer",
                    step=10, attempts=attempt,
                    hint=(
                        "Use session_read_file + session_write_file to manually "
                        "fix annotation in annotated/, then call "
                        "session_build_annotated and session_run_with_dftracer."
                    ),
                    report=report,
                )
            else:
                report["step_10_smoke_with_dftracer"] = {"status": "no smoke command provided", "attempt": attempt}
                _write_artifact_log(ws, 11, f"session_run_with_dftracer{sfx}",
                                    report["step_10_smoke_with_dftracer"], rid)
                break  # no smoke test — build alone is enough

        # Promote the last successful attempt's data to canonical report keys
        for k in list(report.keys()):
            if k.startswith("step_9_attempt") or k.startswith("step_10_attempt"):
                base = "step_9_build_ann" if "step_9" in k else "step_10_smoke_with_dftracer"
                report.setdefault(base, report[k])

        _save_state(rid, {"step": "annotated_built"})
        # Write final canonical logs with fixed step numbers
        _write_artifact_log(ws, 10, "session_build_annotated",
                             report.get("step_9_build_ann", {}), rid)
        _write_artifact_log(ws, 11, "session_run_with_dftracer",
                             report.get("step_10_smoke_with_dftracer", {}), rid)

        # --- Step 11: split traces (via dftracer-utils MCP service) ---
        traces_split = ws / "traces_split"
        traces_split.mkdir(exist_ok=True)
        trace_files = list(traces_dir.glob("*.pfw")) + list(traces_dir.glob("*.pfw.gz"))

        if trace_files:
            sp_r = _dftracer_utils_split(
                directory=str(traces_dir),
                output_dir=str(traces_split),
                app_name=rid,
            )
            report["step_11_split"] = sp_r
            if not sp_r["success"]:
                report["step_11_split"]["warning"] = "dftracer_split (utils service) failed — proceeding"
        else:
            report["step_11_split"] = {"status": "no trace files found"}
            traces_split = traces_dir  # fall back
        _write_artifact_log(ws, 12, "session_split_traces", report["step_11_split"], rid)

        # --- Step 12: analyze traces ---
        idx_dir = traces_split / "idx"
        idx_dir.mkdir(exist_ok=True)
        an_r = _run(
            [
                "dftracer_info",
                "-d", str(traces_split),
                "--query", "summary",
                "--index-dir", str(idx_dir),
            ],
            timeout=600,
        )
        report["step_12_analyze"] = an_r
        _write_artifact_log(ws, 13, "session_analyze_traces", an_r, rid)

        # --- Step 13: diagnose bottlenecks (dfanalyzer checkpoint → dfdiagnoser) ---
        checkpoint_dir = ws / "dfanalyzer_checkpoint"
        diagnosis_dir  = ws / "diagnosis"
        scored_dir     = diagnosis_dir / "scored"
        checkpoint_dir.mkdir(exist_ok=True)
        diagnosis_dir.mkdir(exist_ok=True)
        scored_dir.mkdir(exist_ok=True)

        diag_phases: Dict[str, Any] = {}

        # Phase 13a: dfanalyzer with checkpoint output
        ana13_r = _run(
            [
                "dfanalyzer",
                f"trace_path={traces_split}",
                "analyzer.checkpoint=True",
                f"analyzer.checkpoint_dir={checkpoint_dir}",
                "analyzer/preset=posix",
                "view_types=[time_range]",
            ],
            timeout=600,
        )
        diag_phases["dfanalyzer"] = ana13_r
        if not ana13_r["success"]:
            report["step_13_diagnose"] = {
                "status": "warning",
                "message": "dfanalyzer failed — bottleneck diagnosis skipped",
                "stderr": ana13_r.get("stderr", ""),
                "hint": "Install dfanalyzer: pip install dfanalyzer-utils",
                "phases": diag_phases,
            }
        else:
            flat_views = list(checkpoint_dir.glob("_flat_view_*.parquet"))
            if not flat_views:
                report["step_13_diagnose"] = {
                    "status": "warning",
                    "message": "dfanalyzer produced no _flat_view_*.parquet — bottleneck diagnosis skipped",
                    "phases": diag_phases,
                }
            else:
                # Phase 13b: dfdiagnoser (Python API → CLI fallback)
                diag_r: Optional[Dict[str, Any]] = None
                try:
                    from dfdiagnoser.diagnoser import Diagnoser   # type: ignore
                    from dfdiagnoser.output import FileOutput     # type: ignore
                    _dg = Diagnoser()
                    _res = _dg.diagnose_checkpoint(checkpoint_dir=str(checkpoint_dir))
                    FileOutput(output_dir=str(scored_dir), output_format="json").handle_result(_res)
                    diag_r = {
                        "returncode": 0,
                        "stdout": f"Scored {len(_res.scored_flat_views)} view(s) via Python API",
                        "stderr": "",
                        "success": True,
                    }
                except ImportError:
                    diag_r = _run(
                        [
                            "dfdiagnoser",
                            "input=checkpoint",
                            f"input.checkpoint_dir={checkpoint_dir}",
                            "output=file",
                            f"output.output_dir={scored_dir}",
                            "output.output_format=json",
                        ],
                        timeout=600,
                    )
                    if not diag_r["success"] and "not found" in diag_r.get("stderr", "").lower():
                        diag_r["stderr"] += " — install with: pip install dfdiagnoser"
                except Exception as exc:
                    diag_r = {"returncode": -1, "stdout": "", "stderr": str(exc), "success": False}
                diag_phases["dfdiagnoser"] = diag_r

                # Parse scored outputs
                _score_labels = {1: "trivial", 2: "low", 3: "medium", 4: "high", 5: "critical"}
                severity_counts: Dict[str, int] = {
                    "critical": 0, "high": 0, "medium": 0, "low": 0, "trivial": 0
                }
                bottlenecks: List[Dict[str, Any]] = []
                for _sp in sorted(scored_dir.glob("*_scored.json")):
                    try:
                        with open(_sp) as _f:
                            _rows = json.load(_f)
                        _vname = _sp.stem
                        for _rk, _row in (_rows.items() if isinstance(_rows, dict) else []):
                            for _col, _val in _row.items():
                                if not _col.endswith("_score") or _val is None:
                                    continue
                                _metric = _col[:-6]
                                try:
                                    _score = int(_val)
                                except (TypeError, ValueError):
                                    continue
                                _label = _score_labels.get(_score, "unknown")
                                if _label in severity_counts:
                                    severity_counts[_label] += 1
                                if _score >= 4:
                                    bottlenecks.append({
                                        "view":     _vname,
                                        "scope":    str(_rk),
                                        "metric":   _metric,
                                        "score":    _score,
                                        "severity": _label,
                                        "value":    _row.get(_metric),
                                    })
                    except Exception:
                        pass
                bottlenecks.sort(key=lambda x: x["score"], reverse=True)

                diagnosis_summary = {
                    "run_id":          rid,
                    "checkpoint_dir":  str(checkpoint_dir),
                    "diagnosis_dir":   str(diagnosis_dir),
                    "severity_counts": severity_counts,
                    "bottlenecks":     bottlenecks[:50],
                    "phases":          diag_phases,
                }
                (ws / "diagnosis.json").write_text(json.dumps(diagnosis_summary, indent=2))

                total_issues  = sum(severity_counts.values())
                critical_high = severity_counts["critical"] + severity_counts["high"]
                report["step_13_diagnose"] = {
                    "status":          "ok",
                    "total_scored":    total_issues,
                    "high_critical":   critical_high,
                    "severity_counts": severity_counts,
                    "bottlenecks":     bottlenecks[:10],
                    "phases":          diag_phases,
                }
                _write_artifact_log(ws, 14, "session_diagnose_bottlenecks", {
                    "total_metrics_scored": total_issues,
                    "high_critical":        critical_high,
                    "severity_counts":      severity_counts,
                }, rid)

        _save_state(rid, {
            "step": "pipeline_complete",
            "traces": str(traces_dir),
            "traces_split": str(traces_split),
        })

        return _ok(
            "Pipeline complete",
            run_id=rid,
            workspace=str(ws),
            report=report,
        )


def register_run_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def pipeline_create_run(
        app: str,
        description: Optional[str] = None,
    ) -> str:
        """
        Create a deterministic run directory for a pipeline and remember the
        active run for the given application.

        The run ID is composed as ``<app_name>/<YYYYMMDD_HHMMSS>`` where
        ``app_name`` is derived from the ``app`` argument by extracting the
        basename, lower-casing it, and replacing non-alphanumeric characters
        with underscores.

        The workspace is created at::

            workspaces/<app_name>/<YYYYMMDD_HHMMSS>/

        A pointer file ``workspaces/<app_name>/.current_run`` is written so
        that ``pipeline_get_run_id`` can recall the active run without the
        caller having to track the ID themselves.

        Args:
            app:         Application name or path (e.g. ``ior``, ``/path/to/ior``,
                         ``https://github.com/org/myapp``).
            description: Optional free-text note stored in session.json.

        Returns:
            JSON with ``run_id``, ``app_name``, ``workspace``, ``created_at``.
        """
        run_id, ws = _create_run(app, description=description)
        state = _load_state(run_id)

        return _ok(
            f"Run {run_id} created",
            run_id=run_id,
            app_name=state.get("app_name", _derive_app_name(app)),
            workspace=str(ws),
            created_at=state.get("created_at", ""),
        )

    @mcp.tool()
    def pipeline_get_run_id(app: str) -> str:
        """
        Return the active run ID for the given application.

        Reads the pointer written by ``pipeline_create_run``.  If no run has
        been created yet for this application, lists the available run
        directories so the caller can pick one or call
        ``pipeline_create_run`` first.

        Args:
            app: Application name or path — same value passed to
                 ``pipeline_create_run``.

        Returns:
            JSON with ``run_id``, ``app_name``, ``workspace``, and
            ``created_at`` from the active run's session.json.
        """
        app_name = _derive_app_name(app)
        pointer = _workspaces_root() / app_name / ".current_run"

        if not pointer.exists():
            # Fall back: list available runs for this app so the caller can choose
            app_dir = _workspaces_root() / app_name
            if app_dir.is_dir():
                runs = sorted(
                    d.name for d in app_dir.iterdir()
                    if d.is_dir() and (d / "session.json").exists()
                )
            else:
                runs = []
            if runs:
                return _err(
                    f"No active run pointer for app '{app_name}'. "
                    f"Call pipeline_create_run first, or use one of the existing runs.",
                    app_name=app_name,
                    available_runs=[f"{app_name}/{r}" for r in runs],
                )
            return _err(
                f"No runs found for app '{app_name}'. "
                f"Call pipeline_create_run to start one.",
                app_name=app_name,
            )

        run_id = pointer.read_text().strip()
        ws = _ws(run_id)
        state = _load_state(run_id)
        return _ok(
            f"Active run for '{app_name}'",
            run_id=run_id,
            app_name=app_name,
            workspace=str(ws),
            created_at=state.get("created_at", "unknown"),
            step=state.get("step", "unknown"),
            description=state.get("description"),
        )

    @mcp.tool()
    def pipeline_list_runs(app: str) -> str:
        """
        List all run directories that exist for the given application.

        Args:
            app: Application name or path.

        Returns:
            JSON with ``app_name``, ``current_run_id``, and ``runs`` list
            (each entry has ``run_id``, ``created_at``, ``step``).
        """
        app_name = _derive_app_name(app)
        app_dir = _workspaces_root() / app_name

        pointer = app_dir / ".current_run"
        current_run_id = pointer.read_text().strip() if pointer.exists() else None

        if not app_dir.is_dir():
            return _ok(
                f"No runs found for app '{app_name}'",
                app_name=app_name,
                current_run_id=None,
                runs=[],
            )

        runs = []
        for d in sorted(app_dir.iterdir()):
            if not d.is_dir():
                continue
            sj = d / "session.json"
            if not sj.exists():
                continue
            state = json.loads(sj.read_text())
            runs.append({
                "run_id": f"{app_name}/{d.name}",
                "created_at": state.get("created_at", "unknown"),
                "step": state.get("step", "unknown"),
                "description": state.get("description"),
                "is_current": f"{app_name}/{d.name}" == current_run_id,
            })

        return _ok(
            f"{len(runs)} run(s) for app '{app_name}'",
            app_name=app_name,
            current_run_id=current_run_id,
            runs=runs,
        )
