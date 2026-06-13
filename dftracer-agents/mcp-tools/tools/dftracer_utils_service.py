#!/usr/bin/env python3
"""
DFTracer Utils MCP Service — tools aligned with the official CLI docs.

https://dftracer.readthedocs.io/projects/utils/en/latest/cli.html

Each tool wraps a ``dftracer_*`` binary.  The mapping follows
every documented command: reader, info, merge, split, event_count,
pgzip, stats, aggregator, call_tree, call_tree_mpi, comparator, view,
index, organize, reconstruct, replay, tar, gen_dlio_config,
gen_fake_trace, server, aggregator_mpi.

TOOL SELECTION GUIDE
--------------------
Inspect a trace without running analysis:
  info            — file metadata, event counts, gzip structure
  event_count     — fast count of valid events only
  tar             — inspect a TAR.GZ archive without extracting it

Prepare / reshape trace files:
  pgzip           — compress .pfw → .pfw.gz in parallel
  merge           — combine many .pfw/.pfw.gz into one file
  split           — split a large trace into equal-size chunks
  organize        — partition events into named groups by query
  reconstruct     — reverse an organize operation

Query and filter:
  view            — extract a filtered subset of events (use presets or query DSL)
  index           — pre-build bloom indices to speed up repeated queries
  reader          — read raw bytes/lines from a compressed file

Analyze:
  stats           — statistical summaries, histograms, duration distributions
  aggregator      — bucket events into time-series counters
  call_tree       — hierarchical call graph from trace files
  comparator      — A/B comparison between two runs with significance scoring

Simulate / generate:
  replay          — re-execute I/O operations from a trace with optional timing
  gen_dlio_config — derive a DLIO YAML config from raw traces
  gen_fake_trace  — create synthetic traces for testing tools

Serve:
  server          — HTTP REST API over trace files

Distributed (requires MPI build):
  aggregator_mpi  — multi-rank aggregation
  call_tree_mpi   — multi-rank call-tree construction
"""

import subprocess
from typing import List, Optional
from fastmcp import FastMCP

from ...mcp_service_factory import MCPService


# ── shared / default helpers ───────────────────────────────────────────

def _build_watchdog_flags(
    disable_watchdog: bool = False,
    watchdog_global_timeout: Optional[float] = None,
    watchdog_task_timeout: Optional[float] = None,
    watchdog_interval: Optional[float] = None,
    watchdog_warning_threshold: Optional[float] = None,
    watchdog_idle_timeout: Optional[float] = None,
    watchdog_deadlock_timeout: Optional[float] = None,
) -> list[str]:
    """Return the shared watchdog flags used by many dftracer_* tools."""
    args: list[str] = []
    if disable_watchdog:
        args.append("--disable-watchdog")
    if watchdog_global_timeout is not None:
        args.extend(["--watchdog-global-timeout", str(watchdog_global_timeout)])
    if watchdog_task_timeout is not None:
        args.extend(["--watchdog-task-timeout", str(watchdog_task_timeout)])
    if watchdog_interval is not None:
        args.extend(["--watchdog-interval", str(watchdog_interval)])
    if watchdog_warning_threshold is not None:
        args.extend(["--watchdog-warning-threshold", str(watchdog_warning_threshold)])
    if watchdog_idle_timeout is not None:
        args.extend(["--watchdog-idle-timeout", str(watchdog_idle_timeout)])
    if watchdog_deadlock_timeout is not None:
        args.extend(["--watchdog-deadlock-timeout", str(watchdog_deadlock_timeout)])
    return args


class DftracerUtilsService(MCPService):
    """MCP tools wrapping every documented ``dftracer_*`` binary."""

    def __init__(self):
        self.core_subservice = FastMCP("DFTracerCore")       # read, merge, split…
        self.analysis_subservice = FastMCP("DFTracerAnalysis")  # stats, aggregator…
        self.query_subservice = FastMCP("DFTracerQuery")        # view, index…
        self.utility_subservice = FastMCP("DFTracerUtility")    # replay, reconstruct…
        self.dlio_subservice = FastMCP("DFTracerDLIO")          # gen_dlio_config
        self.synthetic_subservice = FastMCP("DFTracerSynthetic")  # gen_fake_trace
        self.mpi_subservice = FastMCP("DFTracerMPI")            # _mpi variants

        self._register_core_tools()
        self._register_analysis_tools()
        self._register_query_tools()
        self._register_index_tools()
        self._register_organize_tools()
        self._register_replay_tools()
        self._register_comparator_tools()
        self._register_dlio_tools()
        self._register_synthetic_tools()
        self._register_server_tool()
        self._register_mpi_tools()

    # ── Core tools (reader, info, merge, split, event_count, pgzip) ───

    def _register_core_tools(self):
        @self.core_subservice.tool()
        def reader(
            file: str,
            index: Optional[str] = None,
            start: Optional[int] = None,
            end: Optional[int] = None,
            checkpoint_size: Optional[int] = None,
            force_rebuild: bool = False,
            check: bool = False,
            read_buffer_size: Optional[int] = None,
            mode: str = "bytes",
            index_dir: Optional[str] = None,
        ) -> str:
            """Read raw content from a GZIP or TAR.GZ compressed DFTracer file.

            Use this tool when you need to extract specific byte or line ranges from a
            compressed trace file without fully decompressing it. It uses a gzip index
            for efficient random access — the index is built on first use and cached.

            Prefer this tool over shell-level zcat/gunzip when you only need a slice of
            a large file, or when you want to validate the index with --check.

            Args:
                file: Path to the compressed file (.pfw.gz or .tar.gz). Required.

                mode: What to output.
                    "bytes"      — raw decompressed bytes (default)
                    "line_bytes" — each line prefixed with its byte offset
                    "lines"      — plain text lines with no offset

                start: Byte offset to begin reading from (default: beginning of file).
                end:   Byte offset to stop reading at (default: end of file).
                    Together, start/end allow slicing without reading the full file.

                index: Path to an existing gzip index file. Auto-generated next to
                    the input file when omitted; pass an explicit path to share one
                    index across tools or store it on a different filesystem.

                index_dir: Directory in which to store the auto-generated index when
                    no explicit index path is given.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).
                    Smaller values make random access faster but produce a larger index.

                force_rebuild: Ignore any cached index and rebuild from scratch.
                    Use when the file has changed since the index was built.

                check: Validate the index against the file without returning data.
                    Use to verify index integrity before a large extraction job.

                read_buffer_size: Internal read buffer in bytes (default: 1 MB).
                    Increase for large sequential reads to reduce syscall overhead.
            """
            cmd = ["dftracer_reader", file]
            if index:
                cmd += ["-i", index]
            if start is not None:
                cmd += ["-s", str(start)]
            if end is not None:
                cmd += ["-e", str(end)]
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if force_rebuild:
                cmd.append("--force-rebuild")
            if check:
                cmd.append("--check")
            if read_buffer_size is not None:
                cmd += ["--read-buffer-size", str(read_buffer_size)]
            cmd += ["--mode", mode]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

        @self.core_subservice.tool()
        def info(
            files: Optional[str] = None,
            directory: Optional[str] = None,
            query_type: str = "summary",
            verbose: bool = False,
            force_rebuild: bool = False,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Display metadata and index information for .pfw.gz DFTracer files.

            Use this tool for a fast sanity-check on trace files — event counts,
            gzip structure, and index state — without running a full analysis.
            Prefer info over stats when you just need to know what is in the files,
            not detailed statistics about the events.

            Provide either files (space-separated list of paths) or directory, not both.

            Args:
                files: Space-separated list of .pfw or .pfw.gz file paths.
                    Mutually exclusive with directory.

                directory: Directory containing .pfw/.pfw.gz files to inspect.
                    Mutually exclusive with files.

                query_type: Output style.
                    "summary"  — aggregate stats across all files (default)
                    "detailed" — per-file breakdown; use when files differ significantly

                verbose: Include internal index details (checkpoint offsets, bloom state).
                    Useful for debugging index corruption or unexpected query performance.

                force_rebuild: Discard cached index and rebuild before displaying info.
                    Use after modifying or replacing trace files.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for storing .dftindex files (default: same as data).

                executor_threads: Worker threads for parallel file processing.
                    Default: number of CPU cores.
            """
            cmd = ["dftracer_info"]
            if files is not None:
                for f in files.split():
                    cmd += ["--files", f]
            if directory is not None:
                cmd += ["-d", directory]
            cmd += ["--query", query_type]
            if verbose:
                cmd.append("-v")
            if force_rebuild:
                cmd.append("--force-rebuild")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

        @self.core_subservice.tool()
        def merge(
            directory: Optional[str] = None,
            output_file: str = "combined.pfw",
            force: bool = False,
            compress: bool = False,
            gzip_only: bool = False,
            verbose: bool = False,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Merge multiple .pfw/.pfw.gz trace files into a single JSON-array file.

            Use this tool before analysis when you have many per-process or per-rank
            trace files and want to treat them as one dataset. The output is a single
            file containing a JSON array of all events.

            Tip: set compress=True to produce a .pfw.gz output that is smaller and
            readable by all other dftracer tools. Use gzip_only=True to skip any
            uncompressed .pfw files in the directory (e.g. when .pfw.gz are the
            canonical copies and .pfw are temporary artifacts).

            Args:
                directory: Input directory containing .pfw/.pfw.gz files (default: .).

                output_file: Output file path (default: combined.pfw).
                    Use a .pfw.gz extension when compress=True.

                compress: gzip the output file. Recommended for large merges.

                gzip_only: Process only .pfw.gz files; skip plain .pfw files.
                    Use when .pfw files are incomplete or intermediate artifacts.

                force: Recreate gzip indices even if they already exist.

                verbose: Print per-file progress during the merge.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_merge"]
            if directory:
                cmd += ["-d", directory]
            cmd += ["-o", output_file]
            if force:
                cmd.append("-f")
            if compress:
                cmd.append("-c")
            if gzip_only:
                cmd.append("-g")
            if verbose:
                cmd.append("-v")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Merged trace files from {directory} → {output_file}"

        @self.core_subservice.tool()
        def split(
            app_name: Optional[str] = None,
            directory: str = ".",
            output_dir: Optional[str] = None,
            chunk_size_mb: float = 4.0,
            force: bool = False,
            compress: bool = True,
            verify: bool = False,
            verbose: bool = False,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Split a directory of trace files into equal-size chunks.

            Use this tool to partition large traces for parallel downstream processing.
            Chunks are written as <app_name>_<n>.pfw[.gz] files in the output directory.
            The default chunk size is 4 MB; increase it for fewer, larger chunks or
            decrease it for finer-grained parallelism.

            Use verify=True to confirm that the split output contains exactly the same
            events as the input — recommended for production pipelines.

            Args:
                app_name: Prefix for output chunk files (default: "app").
                    Set this to a meaningful application name so output files are
                    self-describing, e.g. "pytorch_resnet".

                directory: Input directory containing .pfw/.pfw.gz files (default: .).

                output_dir: Output directory for chunks (default: ./split).

                chunk_size_mb: Target chunk size in MB (default: 4.0).
                    Smaller values → more files, better parallelism.
                    Larger values → fewer files, less overhead.

                compress: gzip each output chunk (default: True).
                    Keep True unless downstream tools require plain .pfw.

                verify: After splitting, compare output event IDs against input to
                    confirm no events were lost or duplicated.

                force: Recreate indices even if they already exist.

                verbose: Print per-chunk progress.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_split"]
            if app_name:
                cmd += ["-n", app_name]
            else:
                cmd += ["-n", "app"]
            if directory:
                cmd += ["-d", directory]
            if output_dir is not None:
                cmd += ["-o", output_dir]
            else:
                cmd += ["-o", "./split"]
            if chunk_size_mb != 4.0:
                cmd += ["-s", str(chunk_size_mb)]
            if verify:
                cmd.append("--verify")
            if force:
                cmd.append("-f")
            if not compress:
                cmd.append("--compress=false")
            if verbose:
                cmd.append("-v")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Split trace files → {output_dir}"

        @self.core_subservice.tool()
        def event_count(
            directory: Optional[str] = None,
            force: bool = False,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Count valid events in .pfw/.pfw.gz files without running full analysis.

            Use this tool for a fast sanity check on how many events are in a trace
            directory before committing to an expensive analysis run. It is much faster
            than stats because it only scans gzip checkpoints, not individual events.

            Args:
                directory: Directory containing .pfw/.pfw.gz files (default: .).

                force: Force index rebuild before counting.
                    Use after adding or replacing trace files.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads for parallel scanning
                    (default: CPU core count).
            """
            cmd = ["dftracer_event_count"]
            if directory is not None:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "."]
            if force:
                cmd.append("-f")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

        @self.core_subservice.tool()
        def pgzip(
            directory: Optional[str] = None,
            verbose: bool = False,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Parallel-compress .pfw files to .pfw.gz in a directory.

            Use this tool to compress uncompressed trace files before archiving or
            transferring them. It processes all .pfw files in the directory in parallel
            and produces .pfw.gz files that are compatible with all other dftracer tools.
            The original .pfw files are removed after successful compression.

            Prefer this over the merge --compress flag when you want to keep files
            separate rather than combining them into one archive.

            Args:
                directory: Directory containing .pfw files to compress (default: .).

                verbose: Print per-file progress during compression.

                executor_threads: Worker threads for parallel compression
                    (default: CPU core count). Increase for directories with many
                    small files; leave at default for a few large files.
            """
            cmd = ["dftracer_pgzip"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "."]
            if verbose:
                cmd.append("-v")
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return "Compressed .pfw files in directory (check for *.pfw.gz)"

        @self.core_subservice.tool()
        def tar(
            file: str,
            index: Optional[str] = None,
            checkpoint_size: Optional[int] = None,
            force_rebuild: bool = False,
            list_files: bool = False,
            show_info: bool = False,
        ) -> str:
            """Inspect or list files inside a TAR.GZ archive of DFTracer trace data.

            Use this tool to work with TAR.GZ-packaged trace archives without fully
            extracting them. It builds a TAR index for efficient random access.
            Use list_files to see what is inside, show_info for archive metadata, or
            neither to just build/validate the index.

            Args:
                file: Path to the .tar.gz archive file. Required.

                list_files: Print a listing of all files in the archive.
                    Use to check what trace files are inside before extracting.

                show_info: Print archive-level metadata (entry count, sizes, offsets).
                    Use to understand the archive structure without listing every file.

                index: Path to an existing TAR index file. Auto-generated next to
                    the archive when omitted.

                checkpoint_size: Index checkpoint spacing in bytes (default: 33 MB).
                    Smaller values enable faster seeks at the cost of a larger index.

                force_rebuild: Discard and rebuild the index even if one exists.
                    Use after the archive has been modified.
            """
            cmd = ["dftracer_tar", file]
            if index:
                cmd += ["-i", index]
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if force_rebuild:
                cmd.append("-f")
            if list_files:
                cmd.append("--list-files")
            if show_info:
                cmd.append("--info")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

    # ── Analysis tools (stats, aggregator, call_tree, comparator) ─────

    def _register_analysis_tools(self):
        @self.analysis_subservice.tool()
        def stats(
            directory: Optional[str] = None,
            files: Optional[str] = None,
            index_dir: Optional[str] = None,
            report: str = "summary",
            top_n: int = 10,
            top_n_pid_tid: int = 10,
            query: Optional[str] = None,
            group_by_dims: Optional[str] = None,
            json_output: bool = False,
            no_auto_index: bool = False,
            checkpoint_size: Optional[int] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Compute event statistics from DFTracer trace files.

            Use this tool to get operation histograms, duration distributions, and
            per-process breakdowns from a trace directory. It is the primary tool for
            understanding what I/O operations are happening and how long they take.

            For a first look, run with defaults (report="summary"). For deeper
            investigation, switch to "detailed" or "top-names" reports, or add a
            --query filter to focus on a specific category or operation.

            REPORT TYPES:
              "summary"        — aggregate counts, total/avg/max duration across all events
              "categories"     — breakdown by event category (POSIX, STDIO, APP, etc.)
              "names"          — breakdown by operation name (read, write, open, etc.)
              "pid_tids"       — breakdown by process/thread
              "time_range"     — events bucketed over time
              "duration"       — duration distribution histogram
              "top-names"      — top N slowest operation names
              "top-categories" — top N slowest categories
              "detailed"       — full per-event output (can be very large)

            EXAMPLES:
              # Quick summary of a trace directory
              stats(directory="/traces/run1")

              # Top 20 slowest POSIX operations
              stats(directory="/traces", report="top-names", top_n=20,
                    query='cat == "POSIX"')

              # Per-process breakdown, JSON output for downstream processing
              stats(directory="/traces", report="pid_tids", json_output=True)

            Args:
                directory: Directory containing .pfw/.pfw.gz files (default: .).
                    Mutually exclusive with files.

                files: Space-separated list of explicit .pfw/.pfw.gz paths.
                    Mutually exclusive with directory.

                report: Which statistical view to produce (see REPORT TYPES above).
                    Default: "summary".

                top_n: Number of top entries to show in top-* reports (default: 10).
                    Set to 0 to show all entries.

                top_n_pid_tid: Number of top PID:TID pairs to show (default: 10).
                    Only relevant for report="pid_tids".

                query: Query DSL filter applied before computing stats.
                    Syntax: 'cat == "POSIX" and dur > 1000'
                    Field names: cat, name, pid, tid, dur, ts, fhash, hhash.
                    Leave None to include all events.

                group_by_dims: Space-separated dimension names to group by.
                    Options: name, cat, pid, tid, fhash, hhash, pid_tid.
                    Default: name.

                json_output: Emit results as JSON instead of a human-readable table.
                    Use when piping output to another tool or storing results.

                no_auto_index: Disable automatic bloom index building.
                    Use when you have already built indices with the index tool
                    and do not want them rebuilt.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_stats"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "."]
            if files is not None:
                for f in files.split():
                    cmd += ["--files", f]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            cmd += ["--report", report]
            cmd += ["--top-n", str(top_n)]
            cmd += ["--top-n-pid-tid", str(top_n_pid_tid)]
            if query is not None:
                cmd += ["--query", query]
            if group_by_dims is not None:
                for dim in group_by_dims.split():
                    cmd += ["--group-by", dim]
            if json_output:
                cmd.append("--json")
            if no_auto_index:
                cmd.append("--no-auto-index")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

        @self.analysis_subservice.tool()
        def aggregator(
            directory: Optional[str] = None,
            output_file: str = "aggregated_output.json",
            time_interval_ms: float = 5000.0,
            group_keys: Optional[str] = None,
            metric_fields: Optional[str] = None,
            query: Optional[str] = None,
            force: bool = False,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
            compress: bool = False,
            compression_level: int = 6,
            event_format: str = "counter",
            boundary_events: Optional[str] = None,
            no_track_process_parents: bool = False,
            chunk_size_mb: int = 4,
            read_batch_size_mb: int = 4,
            compute_percentiles: bool = False,
            percentiles: Optional[str] = None,
            relative_accuracy: float = 0.01,
            format_type: str = "json",
        ) -> str:
            """Aggregate DFTracer events into time-bucketed counter streams.

            Use this tool to convert raw per-event trace data into time-series counters
            suitable for plotting, dashboards, or further statistical analysis. Each
            bucket contains aggregated metrics (count, total duration, bandwidth) for
            the selected time interval.

            The output is a JSON file in Perfetto counter format (default) or Apache
            Arrow IPC format (format_type="arrow", requires DFTRACER_UTILS_ENABLE_ARROW_IPC=ON).

            EVENT FORMAT SELECTION — controls which events are aggregated:
              "counter"         — counter-type events where category is "sys" (default)
              "regular"         — regular non-counter events
              "profile-counter" — counter-type events where category is NOT "sys"

            EXAMPLES:
              # Basic 5-second bucketing of a trace directory
              aggregator(directory="/traces/run1", output_file="agg.json")

              # 1-second buckets, POSIX events only, with 95th-percentile latency
              aggregator(
                  directory="/traces",
                  time_interval_ms=1000,
                  query='cat == "POSIX"',
                  compute_percentiles=True,
                  percentiles="0.5,0.95,0.99",
              )

            Args:
                directory: Input directory containing trace files (default: .).

                output_file: Output file path (default: aggregated_output.json).
                    Use a .json.gz extension when compress=True.

                time_interval_ms: Bucket width in milliseconds (default: 5000).
                    Decrease for finer time resolution; increase to reduce noise.

                event_format: Which event rows to aggregate (see EVENT FORMAT above).
                    Default: "counter".

                query: Query DSL filter applied before aggregation.
                    Example: 'cat == "POSIX" and name == "read"'

                group_keys: Additional grouping keys from event args fields.
                    Passed as a single comma-separated string.

                metric_fields: Custom metric fields to extract from event args.
                    Passed as a single comma-separated string.

                compress: gzip the output file. Use for large aggregations.

                compression_level: gzip compression level 0–9 (default: 6).
                    0 = no compression (fastest), 9 = maximum compression (slowest).

                format_type: Output format.
                    "json"  — Perfetto counter format (default, always available)
                    "arrow" — Apache Arrow IPC (.arrows); requires Arrow IPC build flag.

                compute_percentiles: Compute DDSketch latency percentiles per bucket.
                    Adds p50/p95/p99 (or custom) columns to the output.
                    Significantly increases memory and compute cost.

                percentiles: Comma-separated percentile fractions to compute.
                    Example: "0.25,0.5,0.75,0.90,0.99"
                    Only used when compute_percentiles=True.

                relative_accuracy: DDSketch relative accuracy for percentile estimation
                    (default: 0.01 = 1% error). Lower values are more accurate but
                    use more memory.

                boundary_events: Configuration for boundary event detection.
                    Advanced use — leave None unless you know the boundary event format.

                no_track_process_parents: Disable parent-process tracking.
                    Use when process hierarchy is not meaningful for your workload.

                chunk_size_mb: Target output chunk size in MB (default: 4).

                read_batch_size_mb: Input read batch size in MB (default: 4).
                    Increase on high-memory systems for better throughput.

                force: Force index rebuild before aggregating.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_aggregator"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "."]
            cmd += ["-o", output_file]
            cmd += ["-t", str(time_interval_ms)]
            if group_keys is not None:
                cmd += ["-g", group_keys]
            if metric_fields is not None:
                cmd += ["-m", metric_fields]
            if query is not None:
                cmd += ["--query", query]
            if force:
                cmd.append("-f")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            if compress:
                cmd.append("--compress")
            cmd += ["--compression-level", str(compression_level)]
            cmd += ["--event-format", event_format]
            if boundary_events is not None:
                cmd += ["--boundary-events", boundary_events]
            if no_track_process_parents:
                cmd.append("--no-track-process-parents")
            if chunk_size_mb != 4:
                cmd += ["--chunk-size", str(chunk_size_mb)]
            if read_batch_size_mb != 4:
                cmd += ["--read-batch-size", str(read_batch_size_mb)]
            if compute_percentiles:
                cmd.append("--compute-percentiles")
            if percentiles is not None:
                cmd += ["--percentiles", percentiles]
            cmd += ["--relative-accuracy", str(relative_accuracy)]
            cmd += ["--format", format_type]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Aggregated traces → {output_file}"

        @self.analysis_subservice.tool()
        def call_tree(
            inputs: Optional[str] = None,
            recursive: bool = False,
            pattern: str = "*.pfw.gz",
            output: Optional[str] = None,
            save_json: bool = False,
            text_export: Optional[str] = None,
            max_depth: int = 0,
            analyze: bool = False,
            verbose: bool = False,
            stats_only: bool = False,
            no_save: bool = False,
        ) -> str:
            """Build and analyze a hierarchical call tree from DFTracer trace files.

            Use this tool to understand execution flow and critical paths in a traced
            application. It constructs a parent-child call graph from event nesting
            relationships in the trace and can output it as a binary .pfw file,
            a Chrome Tracing JSON, or a plain text tree.

            Use stats_only=True for a quick structural summary without writing files.
            Use analyze=True to get detailed metrics on each node (hotspots, depths).
            Use no_save=True to print the tree to stdout only (useful for inspection).

            Args:
                inputs: Space-separated list of trace files or directories to process.
                    Defaults to current directory when omitted.

                recursive: Search directories recursively for trace files.
                    Required when trace files are nested in subdirectories.

                pattern: Glob pattern for selecting files in directories
                    (default: "*.pfw.gz"). Change to "*.pfw" for uncompressed files.

                output: Output file path for the call-tree binary (.pfw format).
                    Auto-generated from the input name when omitted.

                save_json: Also save the call tree in Chrome Tracing JSON format.
                    Use when you want to visualize the tree in chrome://tracing or
                    Perfetto UI.

                text_export: Path to write a plain-text tree representation.
                    Useful for diffs or quick human review without a UI.

                max_depth: Maximum call-tree depth to include (default: 0 = unlimited).
                    Set a value to prune deeply nested calls for large traces.

                analyze: Compute per-node metrics (duration, child counts, hotspots).
                    Adds significant compute but gives richer output.

                stats_only: Print tree statistics only; do not build or save the tree.
                    Use for a fast structural overview without file I/O.

                no_save: Print analysis to stdout and skip writing all output files.
                    Use for interactive inspection.

                verbose: Print progress and intermediate results during construction.
            """
            cmd = ["dftracer_call_tree"]
            if inputs is not None:
                cmd += inputs.split()
            if recursive:
                cmd.append("-r")
            cmd += ["--pattern", pattern]
            if output:
                cmd += ["-o", output]
            if save_json:
                cmd.append("--json")
            if text_export:
                cmd += ["--text", text_export]
            if not stats_only and not no_save:
                cmd += ["--max-depth", str(max_depth)]
            if analyze:
                cmd.append("--analyze")
            if verbose:
                cmd.append("-v")
            if stats_only:
                cmd.append("--stats-only")
            if no_save:
                cmd.append("--no-save")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

        @self.analysis_subservice.tool()
        def comparator(
            baseline: Optional[str] = None,
            variant: Optional[str] = None,
            config_path: Optional[str] = None,
            query: str = 'cat == "POSIX" OR cat == "STDIO"',
            group_by_dims: str = "cat,name",
            output_format: str = "table",
            time_interval_ms: float = 5000.0,
            threshold_pct: float = 0.0,
            no_color: bool = False,
            executor_threads: Optional[int] = None,
            index_dir: Optional[str] = None,
            force: bool = False,
            checkpoint_size: Optional[int] = None,
        ) -> str:
            """Compare I/O performance metrics between a baseline and a variant trace run.

            Use this tool when you want to measure the impact of a code change, config
            tweak, or hardware difference on I/O performance. It reports per-operation
            delta% and Cohen's d significance (NEGLIGIBLE / SMALL / MEDIUM / LARGE) so
            you can distinguish real regressions from noise.

            You must provide either (baseline + variant) OR config_path, not all three.

            EXAMPLES:
              # Compare two run directories, table output
              comparator(baseline="/traces/run_baseline", variant="/traces/run_new")

              # Focus on APP-layer events, show only changes > 5%
              comparator(
                  baseline="/traces/before",
                  variant="/traces/after",
                  query='cat == "APP"',
                  threshold_pct=5.0,
              )

              # Hierarchical multi-pair comparison via config file
              comparator(config_path="/configs/compare_runs.json")

            Args:
                baseline: Path to the baseline trace file or directory.
                    Required unless config_path is provided.

                variant: Path to the variant trace file or directory.
                    Required unless config_path is provided.

                config_path: Path to a JSON config file for hierarchical comparisons
                    involving more than one baseline/variant pair.
                    Mutually exclusive with baseline/variant.

                query: Query DSL filter selecting which events to compare
                    (default: 'cat == "POSIX" OR cat == "STDIO"').
                    Change to 'cat == "APP"' for application-level events or
                    use a broader filter to include all categories.

                group_by_dims: Comma-separated dimensions for grouping comparison rows
                    (default: "cat,name"). The result table has one row per
                    unique (cat, name) combination.

                output_format: Output style.
                    "table" — human-readable ANSI table (default)
                    "json"  — machine-readable JSON for further processing

                time_interval_ms: Time-bucketing interval in milliseconds (default: 5000).
                    Used when computing per-interval statistics before aggregation.

                threshold_pct: Hide rows where the absolute delta% is below this
                    value (default: 0.0 = show all). Set to 5.0 to suppress noise.

                no_color: Disable ANSI color codes in table output.
                    Use when writing to a file or a terminal that does not support colors.

                force: Force index rebuild before comparing.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for temporary .dftindex files.
                    Default: system temp directory.

                executor_threads: Worker threads (default: auto-detected).
            """
            cmd = ["dftracer_comparator"]
            if baseline is not None:
                cmd += ["--baseline", baseline]
            if variant is not None:
                cmd += ["--variant", variant]
            if config_path is not None:
                cmd += ["--config", config_path]
            cmd += ["--query", query]
            for g in group_by_dims.split(","):
                cmd += ["--group-by", g.strip()]
            cmd += ["--format", output_format]
            cmd += ["-t", str(time_interval_ms)]
            cmd += ["--threshold", str(threshold_pct)]
            if no_color:
                cmd.append("--no-color")
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if force:
                cmd.append("--force")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

    # ── Query tools (view, index) ─────────────────────────────────────

    def _register_query_tools(self):
        @self.query_subservice.tool()
        def view(
            files: Optional[str] = None,
            directory: Optional[str] = None,
            preset: Optional[str] = None,
            recipe: Optional[str] = None,
            save_recipe: Optional[str] = None,
            query: Optional[str] = None,
            time_range: Optional[str] = None,
            min_duration: Optional[int] = None,
            max_duration: Optional[int] = None,
            output_file: Optional[str] = None,
            stream: bool = False,
            no_metadata: bool = False,
            index_dir: Optional[str] = None,
            no_auto_index: bool = False,
            checkpoint_size: Optional[int] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Extract a filtered subset of trace events using query-based chunk pruning.

            Use this tool to create a focused sub-trace from a larger dataset. It applies
            bloom-filter chunk pruning so only relevant chunks are decompressed, making
            it much faster than scanning all events. Use it before stats or aggregator
            to pre-filter a trace by time window, category, or duration.

            FILTER OPTIONS (combine freely):
              preset   — named preset: "io" (POSIX+STDIO), "compute" (APP), "dlio"
              query    — arbitrary DSL: 'cat == "POSIX" and name == "read"'
              time_range — timestamp window: "1000000,2000000" (microseconds)
              min/max_duration — duration bounds in microseconds

            RECIPES: A recipe is a JSON file describing a saved view configuration.
            Use save_recipe to persist a complex filter for reuse with recipe on future calls.

            EXAMPLES:
              # Extract all POSIX I/O events to a new file
              view(directory="/traces/run1", preset="io", output_file="io_only.pfw.gz")

              # Stream events matching a query directly to stdout (NDJSON)
              view(directory="/traces", query='name == "read" and dur > 10000', stream=True)

              # Narrow to a 1-second time window
              view(directory="/traces", time_range="1000000000,2000000000",
                   output_file="window.pfw.gz")

            Args:
                files: Space-separated list of .pfw/.pfw.gz paths.
                    Mutually exclusive with directory.

                directory: Directory containing trace files.
                    Mutually exclusive with files.

                preset: Named built-in view filter.
                    "io"      — POSIX and STDIO events
                    "compute" — APP-category events
                    "dlio"    — deep-learning I/O events
                    Overridden by an explicit query when both are given.

                recipe: Path to a saved view JSON file (produced by save_recipe).
                    Use to replay a previously saved complex filter exactly.

                save_recipe: Path to write the constructed view as a JSON recipe.
                    Use to save a complex filter for future reuse.

                query: Query DSL filter.
                    Fields: cat, name, pid, tid, dur (nanoseconds), ts (microseconds),
                    fhash, hhash.
                    Example: 'cat == "POSIX" and dur > 100000'

                time_range: Timestamp window as "min_us,max_us" in microseconds.
                    Example: "1000000,5000000" selects events in [1s, 5s].

                min_duration: Minimum event duration in microseconds (inclusive).
                    Use to filter out very short events.

                max_duration: Maximum event duration in microseconds (inclusive).
                    Use to filter out abnormally long outlier events.

                output_file: Write filtered events to this file (.pfw or .pfw.gz).
                    When omitted, events are written to stdout.

                stream: Stream matching events to stdout as NDJSON (one JSON per line).
                    Use for pipeline processing without writing an intermediate file.

                no_metadata: Exclude metadata events (ph=M) from the output.
                    Use when downstream tools do not handle metadata events.

                no_auto_index: Disable automatic bloom index construction.
                    Use when indices are pre-built via the index tool.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_view"]
            if files is not None:
                for f in files.split():
                    cmd += ["--files", f]
            if directory is not None:
                cmd += ["-d", directory]
            if preset is not None:
                cmd += ["--preset", preset]
            if recipe is not None:
                cmd += ["--recipe", recipe]
            if save_recipe is not None:
                cmd += ["--save-recipe", save_recipe]
            if query is not None:
                cmd += ["--query", query]
            if time_range is not None:
                cmd += ["--time-range", time_range]
            if min_duration is not None:
                cmd += ["--min-duration", str(min_duration)]
            if max_duration is not None:
                cmd += ["--max-duration", str(max_duration)]
            if output_file:
                cmd += ["-o", output_file]
            else:
                cmd.append("--no-metadata")
            if stream:
                cmd.append("--stream")
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if no_auto_index:
                cmd.append("--no-auto-index")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

    def _register_index_tools(self):
        @self.query_subservice.tool()
        def index(
            directory: Optional[str] = None,
            dimensions: Optional[str] = None,
            force: bool = False,
            checkpoint_size: Optional[int] = None,
            executor_threads: Optional[int] = None,
            index_dir: Optional[str] = None,
            expected_entries: int = 1024,
            false_positive_rate: float = 0.01,
            read_batch_size_mb: int = 4,
            manifest: bool = False,
            rebuild_summaries: bool = False,
        ) -> str:
            """Build per-chunk bloom filter indices to accelerate future queries.

            Use this tool once before running repeated stats, view, or comparator
            queries on the same trace directory. The bloom indices allow those tools
            to skip irrelevant chunks entirely, dramatically reducing query time on
            large traces.

            You do NOT need to call this manually if you only run a query once — most
            tools auto-build indices on first use. Call it explicitly when:
              - You want to pre-build indices before a batch of queries
              - You need to store indices in a separate directory (shared FS, SSD)
              - You want to tune bloom filter sizing for your trace characteristics
              - You need to rebuild stale indices after trace files changed

            Args:
                directory: Directory containing .pfw/.pfw.gz files (default: .).

                dimensions: Additional dimension keys from event args to index.
                    These are field names present in your trace events' args objects.
                    Leave None to index only the standard dimensions (cat, name, pid, tid).

                force: Discard existing indices and rebuild from scratch.
                    Required after trace files are modified or replaced.

                expected_entries: Expected number of unique entries per chunk for
                    bloom filter sizing (default: 1024). Set higher if chunks have
                    many unique values to reduce false positive rate drift.

                false_positive_rate: Target bloom filter false positive rate
                    (default: 0.01 = 1%). Lower values reduce unnecessary chunk
                    reads but produce a larger index file.

                read_batch_size_mb: How many MB to read at once (default: 4 MB).
                    Increase on high-memory systems for better throughput.

                manifest: Build manifest tables with per-checkpoint event routing.
                    Enables more efficient routing of specific event types.

                rebuild_summaries: Rebuild aggregated chunk summaries.
                    Disabled by default; enable when summaries are stale.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Where to store .dftindex files (default: same as data dir).
                    Set this to a fast local SSD when trace files are on slow NFS.

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_index"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd.append("-d")
                cmd.append(".")
            if dimensions is not None:
                cmd += ["--dimensions", dimensions]
            if force:
                cmd.append("-f")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            cmd += ["--expected-entries", str(expected_entries)]
            cmd += ["--false-positive-rate", str(false_positive_rate)]
            cmd += ["--read-batch-size", str(read_batch_size_mb)]
            if manifest:
                cmd.append("--manifest")
            if rebuild_summaries:
                cmd.append("--rebuild-summaries")
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Bloom index built in directory '{directory or '.'}'"

    def _register_comparator_tools(self):
        # comparator is registered inside _register_analysis_tools
        pass

    # ── Organize / Reconstruct tools ──────────────────────────────────

    def _register_organize_tools(self):
        @self.query_subservice.tool()
        def organize(
            files: Optional[str] = None,
            directory: Optional[str] = None,
            output_dir: str = "",
            groups: str = "",
            chunk_size_mb: int = 256,
            checkpoint_size: Optional[int] = None,
            index_dir: Optional[str] = None,
            force: bool = False,
            no_compress: bool = False,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Partition trace events into named query-based groups with provenance tracking.

            Use this tool to split a trace into semantic categories — for example,
            separating I/O events from compute events — while retaining the ability to
            reconstruct the original trace later via reconstruct.

            Each group produces a subdirectory in output_dir containing the matching
            events. A provenance sidecar (.pidx) is written alongside each chunk so
            reconstruct can reverse the operation.

            This is a lossy-free partition: every event goes to exactly one group.
            Events not matched by any group query are discarded.

            GROUPS FORMAT:
              groups is a space-separated string of "name:query" pairs.
              Example: 'io:cat == "POSIX"' 'compute:cat == "APP"'
              Each pair routes matching events into output_dir/<name>/.

            EXAMPLES:
              organize(
                  directory="/traces/run1",
                  output_dir="/organized/run1",
                  groups='io:cat == "POSIX" OR cat == "STDIO"  compute:cat == "APP"',
              )

            Args:
                files: Space-separated list of explicit .pfw/.pfw.gz paths.
                    Mutually exclusive with directory.

                directory: Directory containing trace files.
                    Mutually exclusive with files.

                output_dir: Output directory for group subdirectories. Required.
                    A subdirectory is created per group name.

                groups: Space-separated "name:query" pairs defining groups. Required.
                    Example: 'io:cat == "POSIX"' 'compute:cat == "APP"'

                chunk_size_mb: Target output chunk size in MB (default: 256).
                    Larger chunks reduce file count but require more memory.

                no_compress: Write plain .pfw output instead of .pfw.gz.
                    Leave False (default) for compressed output.

                force: Force index rebuild before organizing.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex sidecar files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_organize"]
            if files is not None:
                for f in files.split():
                    cmd += ["--files", f]
            if directory is not None:
                cmd += ["-d", directory]
            if output_dir:
                cmd += ["-o", output_dir]
            if groups:
                for g in groups.split():
                    cmd += ["--groups", g]
            cmd += ["--chunk-size", str(chunk_size_mb)]
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if force:
                cmd.append("-f")
            if no_compress:
                cmd.append("--no-compress")
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Reorganized traces → {output_dir or 'N/A'}"

        @self.query_subservice.tool()
        def reconstruct(
            directory: str = "",
            output: str = "reconstructed",
            index_dir: Optional[str] = None,
            checkpoint_size: Optional[int] = None,
            no_compress: bool = False,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Reconstruct original traces from organize output using provenance sidecars.

            Use this tool to reverse a previous organize operation and recover the
            original per-file trace structure. It reads the .pidx provenance sidecar
            files written by organize to route events back to their original files.

            This requires that the organize output directory still contains the .pidx
            sidecar files. Do not delete those files if you may need to reconstruct.

            Args:
                directory: Directory containing the organize output (group subdirs
                    and .pidx sidecar files). Required. This is the output_dir you
                    passed to organize.

                output: Output directory for the reconstructed trace files
                    (default: "reconstructed").

                no_compress: Write plain .pfw output instead of .pfw.gz.
                    Should match the compression setting used in organize.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory containing .pidx sidecar files if they were
                    stored separately from the organized trace data.

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_reconstruct"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "."]
            cmd += ["-o", output]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if no_compress:
                cmd.append("--no-compress")
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Reconstructed traces → {output}"

    # ── Replay tool ───────────────────────────────────────────────────

    def _register_replay_tools(self):
        @self.utility_subservice.tool()
        def replay(
            inputs: str = "",
            no_timing: bool = False,
            dry_run: bool = False,
            dftracer_mode: bool = False,
            no_sleep: bool = False,
            verbose: bool = False,
            recursive: bool = False,
            use_call_tree: bool = False,
            hierarchical_replay: bool = False,
            respect_call_hierarchy: bool = False,
            filter_pid: Optional[str] = None,
            exclude_pid: Optional[str] = None,
            filter_tid: Optional[str] = None,
            exclude_tid: Optional[str] = None,
            filter_function: Optional[str] = None,
            exclude_function: Optional[str] = None,
            filter_category: Optional[str] = None,
            exclude_category: Optional[str] = None,
            start_timestamp: Optional[int] = None,
            end_timestamp: Optional[int] = None,
            min_size: Optional[int] = None,
            max_size: Optional[int] = None,
            sample_rate: Optional[float] = None,
            sample_seed: Optional[int] = None,
            max_events: Optional[int] = None,
        ) -> str:
            """Replay I/O operations from trace files with optional timing and filtering.

            Use this tool to reproduce I/O workloads from a captured trace. It reads
            recorded operations (read, write, open, close, etc.) and re-executes them
            against the actual filesystem.

            REPLAY MODES:
              default          — re-issue real I/O syscalls with original inter-event timing
              no_timing=True   — re-issue real I/O but ignore timestamps (as fast as possible)
              dftracer_mode    — sleep-based replay using recorded durations, no real I/O
              dry_run=True     — parse trace and plan replay without executing anything

            CALL HIERARCHY MODES (for applications with nested function calls):
              use_call_tree          — build a call-tree structure to drive replay order
              hierarchical_replay    — respect parent-child nesting in event ordering
              respect_call_hierarchy — replay child events only after their parent returns

            FILTER SYNTAX:
              All filter_* and exclude_* parameters accept comma-separated values.
              Example: filter_pid="1234,5678"  exclude_category="APP"

            EXAMPLES:
              # Replay all I/O from a trace directory at original speed
              replay(inputs="/traces/run1")

              # Dry run to verify what would be replayed
              replay(inputs="/traces/run1", dry_run=True, verbose=True)

              # Replay only POSIX read events from process 1234, max 10000 events
              replay(inputs="/traces", filter_category="POSIX",
                     filter_function="read", filter_pid="1234", max_events=10000)

            Args:
                inputs: Space-separated paths to trace files or directories. Required.
                    Falls back to current directory when omitted.

                no_timing: Ignore original event timestamps; replay as fast as possible.
                    Use for throughput benchmarks where timing fidelity is not needed.

                dry_run: Parse the trace and plan replay without executing any I/O.
                    Use to verify filter correctness or estimate event counts.

                dftracer_mode: Sleep-based replay using recorded durations.
                    No real I/O is issued; only sleeps matching event durations.
                    Use when you want to reproduce timing without filesystem side effects.

                no_sleep: Disable sleep delays when dftracer_mode=True.
                    Makes dftracer_mode equivalent to a dry_run at full speed.

                recursive: Search input directories recursively for trace files.

                use_call_tree: Build a call tree to determine replay ordering.
                hierarchical_replay: Order events by parent-child nesting depth.
                respect_call_hierarchy: Replay parent before its children.

                filter_pid / exclude_pid: Comma-separated PIDs to include/exclude.
                filter_tid / exclude_tid: Comma-separated TIDs to include/exclude.
                filter_function / exclude_function: Function names to include/exclude.
                filter_category / exclude_category: Event categories to include/exclude.

                start_timestamp: Only replay events at or after this timestamp (microseconds).
                end_timestamp:   Only replay events at or before this timestamp (microseconds).

                min_size: Only replay I/O operations of at least this many bytes.
                max_size: Only replay I/O operations of at most this many bytes.

                sample_rate: Replay only a random fraction of events (0.0–1.0).
                    Example: 0.1 replays ~10% of events.

                sample_seed: Random seed for reproducible sampling.
                    Set to get the same sampled subset across runs.

                max_events: Stop after replaying this many events (0 = unlimited).
                    Use with dry_run=True to estimate replay scope.

                verbose: Print per-event statistics and summary after replay.
            """
            cmd = ["dftracer_replay"]
            if inputs:
                cmd += inputs.split()
            else:
                cmd.append(".")
            if no_timing:
                cmd.append("--no-timing")
            if dry_run:
                cmd.append("--dry-run")
            if dftracer_mode:
                cmd.append("--dftracer-mode")
            if no_sleep:
                cmd.append("--no-sleep")
            if verbose:
                cmd.append("-v")
            if recursive:
                cmd.append("-r")
            if use_call_tree:
                cmd.append("--use-call-tree")
            if hierarchical_replay:
                cmd.append("--hierarchical-replay")
            if respect_call_hierarchy:
                cmd.append("--respect-call-hierarchy")
            if filter_pid is not None:
                cmd += ["--filter-pid", filter_pid]
            if exclude_pid is not None:
                cmd += ["--exclude-pid", exclude_pid]
            if filter_tid is not None:
                cmd += ["--filter-tid", filter_tid]
            if exclude_tid is not None:
                cmd += ["--exclude-tid", exclude_tid]
            if filter_function is not None:
                cmd += ["--filter-function", filter_function]
            if exclude_function is not None:
                cmd += ["--exclude-function", exclude_function]
            if filter_category is not None:
                cmd += ["--filter-category", filter_category]
            if exclude_category is not None:
                cmd += ["--exclude-category", exclude_category]
            if start_timestamp is not None:
                cmd += ["--start-timestamp", str(start_timestamp)]
            if end_timestamp is not None:
                cmd += ["--end-timestamp", str(end_timestamp)]
            if min_size is not None:
                cmd += ["--min-size", str(min_size)]
            if max_size is not None:
                cmd += ["--max-size", str(max_size)]
            if sample_rate is not None:
                cmd += ["--sample-rate", str(sample_rate)]
            if sample_seed is not None:
                cmd += ["--sample-seed", str(sample_seed)]
            if max_events is not None:
                cmd += ["--max-events", str(max_events)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout

    # ── Utility tools (server) ────────────────────────────────────────

    def _register_server_tool(self):
        @self.utility_subservice.tool()
        def server(
            bind_address: str = "0.0.0.0",
            port: int = 8080,
            directory: Optional[str] = None,
            index_dir: Optional[str] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Start the DFTracer HTTP REST server for remote trace querying.

            Use this tool to expose trace files via a REST API so that remote clients
            or web UIs can query them without direct filesystem access.

            NOTE: This tool starts the server process synchronously. For long-running
            deployments, run dftracer_server directly as a daemon or systemd service.
            The tool will return immediately after the process starts or fails.

            The server exposes endpoints for querying events, streaming filtered
            subsets, and retrieving metadata from the trace directory.

            Args:
                bind_address: IP address to bind to (default: "0.0.0.0" = all interfaces).
                    Use "127.0.0.1" to restrict to localhost only.

                port: TCP port to listen on (default: 8080).
                    Change if 8080 is already in use on the system.

                directory: Directory containing .pfw/.pfw.gz trace files to serve.
                    Required on the CLI; defaults to "./traces" in this wrapper.

                index_dir: Directory for .dftindex files (default: same as directory).
                    Set to a fast local disk when traces are on slow NFS.

                executor_threads: Worker threads for handling requests
                    (default: CPU core count).
            """
            cmd = ["dftracer_server", "-b", bind_address, "-p", str(port)]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd += ["-d", "traces"]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=False, capture_output=True, text=True)
            return f"Started DFTracer server on {bind_address}:{port} (trace dir={directory or './traces'}) — run as daemon for long-running instance."

    # ── DLIO tools ────────────────────────────────────────────────────

    def _register_dlio_tools(self):
        @self.dlio_subservice.tool()
        def gen_dlio_config(
            output: str = "",
            directory: Optional[str] = None,
            max_bound_percentile: int = 95,
            simulation_iterations: int = 5,
            target_e2e_error: float = 0.05,
            target_cdf_similarity: float = 0.90,
            patience: int = 10,
            epsilon: float = 1.0,
            momentum: float = 0.9,
            min_percentile: int = 50,
            num_workers: int = 8,
            prefetch_factor: int = 2,
            seed: int = 42,
            max_samples_per_entry: int = 100,
            time_interval_ms: float = 5000.0,
            index_dir: Optional[str] = None,
            checkpoint_size: Optional[int] = None,
            executor_threads: Optional[int] = None,
            force: bool = False,
        ) -> str:
            """Generate a DLIO YAML configuration file from raw DFTracer traces.

            Use this tool when you have captured traces from a deep-learning training
            job and want to produce a DLIO benchmark configuration that reproduces the
            observed I/O behaviour. It fits statistical distributions to the trace and
            uses an internal simulator to converge on a max_bound value that matches
            the end-to-end error and CDF similarity targets.

            REQUIRED TRACE EVENTS:
              The trace must contain events with:
                cat="dataloader", name="fetch.block" or "fetch.iter"
                cat="data",       name="preprocess" or "item"
              Without these events the tool will fail or produce an empty config.

            TUNING TIPS:
              - Start with defaults; only adjust simulation parameters if the output
                YAML produces significant benchmark deviation from the original trace.
              - Increase simulation_iterations or patience if the tool exits before
                converging (check stderr for convergence messages).
              - Reduce max_samples_per_entry to speed up fitting on large traces.

            Args:
                output: Output path for the generated DLIO YAML file. Required.
                    Example: "/configs/dlio_benchmark.yaml"

                directory: Directory containing .pfw/.pfw.gz trace files (default: .).

                max_bound_percentile: Starting percentile for max_bound estimation
                    (default: 95). The simulator refines this value downward.

                simulation_iterations: Maximum simulator iterations to find the
                    best max_bound (default: 5). Increase for tighter convergence.

                target_e2e_error: Acceptable fractional error between simulated and
                    observed end-to-end time (default: 0.05 = 5%).

                target_cdf_similarity: Minimum CDF similarity score between simulated
                    and observed distributions (default: 0.90 = 90%).

                patience: Iterations without improvement before early stopping
                    (default: 10).

                epsilon: Step size for percentile adjustment per iteration
                    (default: 1.0).

                momentum: Momentum factor for step-size adaptation [0, 1)
                    (default: 0.9).

                min_percentile: Floor on the max_bound percentile search
                    (default: 50). Prevents the optimizer from choosing too
                    aggressive a bound.

                num_workers: DataLoader worker count to emit in the YAML
                    (default: 8). Set to match your training job's worker count.

                prefetch_factor: DataLoader prefetch factor to emit in the YAML
                    (default: 2).

                seed: Random seed for reproducible distribution sampling (default: 42).

                max_samples_per_entry: Cap on synthesized samples per distribution
                    entry (default: 100). Set 0 for unlimited (slower but more accurate).

                time_interval_ms: Aggregation interval in ms used internally
                    (default: 5000).

                force: Force index rebuild before generating the config.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                index_dir: Directory for .dftindex files (default: same as data).

                executor_threads: Worker threads (default: CPU core count).
            """
            cmd = ["dftracer_gen_dlio_config"]
            if directory:
                cmd += ["-d", directory]
            else:
                cmd.append("-d")
                cmd.append(".")
            cmd += ["-o", output]
            cmd += ["--max-bound-percentile", str(max_bound_percentile)]
            cmd += ["--simulation-iterations", str(simulation_iterations)]
            cmd += ["--target-e2e-error", str(target_e2e_error)]
            cmd += ["--target-cdf-similarity", str(target_cdf_similarity)]
            cmd += ["--patience", str(patience)]
            cmd += ["--epsilon", str(epsilon)]
            cmd += ["--momentum", str(momentum)]
            cmd += ["--min-percentile", str(min_percentile)]
            cmd += ["--num-workers", str(num_workers)]
            cmd += ["--prefetch-factor", str(prefetch_factor)]
            cmd += ["--seed", str(seed)]
            cmd += ["--max-samples-per-entry", str(max_samples_per_entry)]
            cmd += ["-t", str(time_interval_ms)]
            if index_dir is not None:
                cmd += ["--index-dir", index_dir]
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            if force:
                cmd.append("-f")
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"DLIO config → {output}"

    # ── Synthetic trace tools ─────────────────────────────────────────

    def _register_synthetic_tools(self):
        @self.utility_subservice.tool()
        def gen_fake_trace(
            output_dir: str = "",
            num_processes: int = 8,
            num_hosts: int = 4,
            num_epochs: int = 500,
            steps_per_epoch: int = 1000,
            checkpoint_every: int = 5,
            validation_every: int = 2,
            num_train_files: int = 8,
            num_val_files: int = 2,
            step_duration_ms: int = 100,
            seed: int = 42,
            verify: bool = False,
            checkpoint_size_mb: int = 2,
        ) -> str:
            """Generate realistic synthetic DFTracer trace files for testing purposes.

            Use this tool to create test data for validating indexing, filtering, view,
            and analysis tools without needing a real application trace. The generated
            traces mimic a deep-learning training workload with configurable scale.

            Use verify=True to automatically build bloom indices on the output and run
            a set of test queries to confirm chunk-skipping is working correctly end-to-end.

            The generated traces include training steps, validation, checkpointing, and
            POSIX I/O events distributed across the configured processes and hosts.

            Args:
                output_dir: Output directory for generated trace files. Required.
                    Example: "/tmp/test_traces"

                num_processes: Number of simulated MPI ranks / processes (default: 8).
                    Each process produces its own trace file.

                num_hosts: Number of simulated compute nodes (default: 4).
                    Processes are distributed across hosts round-robin.

                num_epochs: Number of simulated training epochs (default: 500).

                steps_per_epoch: Training steps per epoch (default: 1000).
                    Total events ≈ num_processes × num_epochs × steps_per_epoch.

                checkpoint_every: Emit a checkpoint event every N epochs (default: 5).

                validation_every: Emit validation events every N epochs (default: 2).

                num_train_files: Number of training data shards to simulate (default: 8).

                num_val_files: Number of validation data shards (default: 2).

                step_duration_ms: Base duration of each training step in milliseconds
                    (default: 100). Add jitter by changing the seed.

                seed: Random seed for reproducible trace generation (default: 42).
                    Change to produce a different but equally valid synthetic trace.

                verify: After generation, build bloom indices and run test queries
                    to confirm chunk-skipping correctness. Recommended when testing
                    a new dftracer build.

                checkpoint_size_mb: Gzip checkpoint spacing in MB for generated files
                    (default: 2 MB). Smaller values make random access faster.
            """
            cmd = ["dftracer_gen_fake_trace"]
            cmd += ["-o", output_dir]
            cmd += ["-p", str(num_processes)]
            cmd += ["-H", str(num_hosts)]
            cmd += ["-e", str(num_epochs)]
            cmd += ["-s", str(steps_per_epoch)]
            cmd += ["--checkpoint-every", str(checkpoint_every)]
            cmd += ["--validation-every", str(validation_every)]
            cmd += ["--num-train-files", str(num_train_files)]
            cmd += ["--num-val-files", str(num_val_files)]
            cmd += ["--step-duration-ms", str(step_duration_ms)]
            cmd += ["--seed", str(seed)]
            if verify:
                cmd.append("--verify")
            cmd += ["--checkpoint-size", str(checkpoint_size_mb * 1024 * 1024)]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return f"Synthetic traces → {output_dir}"

    # ── MPI tools (aggregator_mpi, call_tree_mpi) ─────────────────────

    def _register_mpi_tools(self):
        """Register distributed tools that must be run via mpirun."""

        @self.mpi_subservice.tool()
        def aggregator_mpi(
            directory: Optional[str] = None,
            output_file: str = "aggregated_output.json.gz",
            time_interval_ms: float = 5000.0,
            staging_dir: Optional[str] = None,
            shared_staging: Optional[str] = None,
            keep_staging: bool = False,
            checkpoint_size: Optional[int] = None,
            executor_threads: Optional[int] = None,
        ) -> str:
            """Distributed aggregation of DFTracer events across MPI ranks.

            Use this tool instead of aggregator when:
              - The trace is too large for single-node processing
              - You are running on an HPC cluster with MPI available
              - The build was configured with DFTRACER_UTILS_ENABLE_MPI=ON

            IMPORTANT: This tool must be invoked via mpirun, not directly.
            This wrapper will attempt to run it but the recommended invocation is:
              mpirun -n <NP> dftracer_aggregator_mpi [options]

            PIPELINE: Each MPI rank independently runs a 5-stage DAG:
              scan → phase_a → phase_b → phase_c → merge
            Each rank produces per-rank SST files in staging_dir; rank 0 then
            bulk-ingests all SSTs and writes the final gzipped JSON output.

            STAGING DIRECTORIES:
              staging_dir     — per-rank local SST staging root (node-local fast disk)
              shared_staging  — shared filesystem staging for cross-node SST transfer
            Use both when ranks are on different nodes: ranks write to their local
            staging_dir, then move artefacts to shared_staging before rank-0 ingest.

            Args:
                directory: Input directory containing .pfw/.pfw.gz trace files
                    (default: .).

                output_file: Output gzipped JSON file path
                    (default: aggregated_output.json.gz).

                time_interval_ms: Aggregation bucket width in milliseconds
                    (default: 5000). Matches the same parameter in aggregator.

                staging_dir: Node-local directory for per-rank SST staging.
                    Default: <index_dir>/_staging. Use a fast local SSD path.

                shared_staging: Shared filesystem directory for cross-node SST
                    transfer before rank-0 ingest. Required in multi-node deployments.

                keep_staging: Retain per-rank SST directories after ingest.
                    Use for debugging or when you want to inspect intermediate files.

                checkpoint_size: Gzip checkpoint spacing in bytes (default: 33 MB).

                executor_threads: Per-rank worker threads (default: auto-scaled
                    based on processes-per-node to avoid oversubscription).
            """
            cmd = ["dftracer_aggregator_mpi"]
            if directory is not None:
                cmd += ["-d", directory]
            else:
                cmd.append("-d")
                cmd.append(".")
            cmd += ["-o", output_file]
            cmd += ["-t", str(time_interval_ms)]
            if staging_dir is not None:
                cmd += ["--staging-dir", staging_dir]
            if shared_staging is not None:
                cmd += ["--shared-staging", shared_staging]
            if keep_staging:
                cmd.append("--keep-staging")
            if checkpoint_size is not None:
                cmd += ["--checkpoint-size", str(checkpoint_size)]
            if executor_threads is not None:
                cmd += ["--executor-threads", str(executor_threads)]
            subprocess.run(cmd, check=False, capture_output=True, text=True)
            return (
                f"Note: dftracer_aggregator_mpi must be run via mpirun.\n"
                f"  mpirun -n <NP> dftracer_aggregator_mpi " + " ".join(cmd[1:])
            )

        @self.mpi_subservice.tool()
        def call_tree_mpi(
            input_dir: str = "",
            output_file: str = "call_tree.pfw",
            staging_dir: Optional[str] = None,
            gzip_: bool = False,
            verbose: bool = False,
            keep_staging: bool = False,
        ) -> str:
            """Distributed call-tree construction across MPI ranks.

            Use this tool instead of call_tree when the trace is from a multi-rank
            application and is too large to process on a single node. Requires a build
            configured with DFTRACER_UTILS_ENABLE_MPI=ON.

            IMPORTANT: Must be invoked via mpirun, not directly.
            Recommended invocation:
              mpirun -n <NP> dftracer_call_tree_mpi <input_dir> [options]

            Each rank owns a disjoint slice of PIDs from the trace. Ranks independently
            build per-PID call-tree shards, write them to staging_dir, and rank 0 merges
            all shards into the final output file.

            Args:
                input_dir: Directory containing .pfw/.pfw.gz trace files. Required.
                    This is the sole positional argument on the CLI.

                output_file: Path for the merged call-tree output (default: call_tree.pfw).
                    Use a .json extension when you want Chrome Tracing / Perfetto format.

                staging_dir: Shared filesystem directory for per-rank call-tree shards.
                    Default: <output_file>.shards/. Rank 0 reads from here during merge.
                    Must be accessible by all ranks — use a shared NFS or Lustre path.

                gzip_: gzip the merged output file. Reduces file size but requires
                    decompression before viewing in Chrome Tracing UI.

                keep_staging: Retain per-rank shard directories after merge.
                    Use for debugging or when you want to inspect individual rank outputs.

                verbose: Print per-rank progress during construction and merge.
            """
            cmd = ["dftracer_call_tree_mpi"]
            if input_dir:
                cmd.append(input_dir)
            else:
                cmd.append(".")
            cmd += ["-o", output_file]
            if staging_dir is not None:
                cmd += ["--staging-dir", staging_dir]
            if gzip_:
                cmd.append("--gzip")
            if verbose:
                cmd.append("-v")
            if keep_staging:
                cmd.append("--keep-staging")
            subprocess.run(cmd, check=False, capture_output=True, text=True)
            return (
                f"Note: dftracer_call_tree_mpi should be invoked via mpirun.\n"
                f"  mpirun -n <NP> " + " ".join(cmd)
            )

    # ── Execute / name (abstract contract) ────────────────────────────

    def execute(self, data: dict):
        """Legacy router — kept to satisfy the MCPService abstract base."""
        command = data.get("command")
        return None

    @property
    def name(self) -> str:
        return "dftracer-utils"


# ── Registration (entry-point for MCPServiceFactory) ───────────────────

from ...mcp_service_factory import MCPServiceFactory

MCPServiceFactory.register("dftracer-utils", DftracerUtilsService())


def run():
    """Run the combined MCP server with all tools."""
    from fastmcp import FastMCP

    combined = FastMCP("DFTracerCombinedServer")

    for attr_name in [
        "core_subservice",
        "analysis_subservice",
        "query_subservice",
        "utility_subservice",
        "dlio_subservice",
        "synthetic_subservice",
        "mpi_subservice",
    ]:
        sub = getattr(DftracerUtilsService(), attr_name, None)
        if sub:
            for cmd in sub.commands.values():
                combined.add_tool(cmd)

    combined.run()


if __name__ == "__main__":
    run()
