"""
DFAnalyzer MCP service wrapper.

Docs:
https://dftracer.readthedocs.io/projects/analyzer/en/latest/getting-started.html
https://dftracer.readthedocs.io/projects/analyzer/en/latest/configuration.html
"""

from __future__ import annotations

import subprocess
from typing import List, Optional

from fastmcp import FastMCP

from ...mcp_service_factory import MCPService, MCPServiceFactory


def _hydra_args(
    trace_path: Optional[str] = None,
    view_types: Optional[List[str]] = None,
    debug: bool = False,
    verbose: bool = False,
    analyzer: str = "dftracer",
    analyzer_preset: str = "posix",
    analyzer_checkpoint: Optional[bool] = None,
    analyzer_checkpoint_dir: Optional[str] = None,
    analyzer_time_approximate: Optional[bool] = None,
    analyzer_time_granularity: Optional[float] = None,
    analyzer_time_resolution: Optional[float] = None,
    output_format: str = "console",
    output_compact: Optional[bool] = None,
    output_root_only: Optional[bool] = None,
    output_name: Optional[str] = None,
    output_run_db_path: Optional[str] = None,
    cluster_type: str = "local",
    cluster_n_workers: Optional[int] = None,
    cluster_memory_limit: Optional[str] = None,
    cluster_processes: Optional[int] = None,
    cluster_cores: Optional[int] = None,
    cluster_memory: Optional[str] = None,
) -> List[str]:
    """Build the ``dfanalyzer`` Hydra-style override command.

    dfanalyzer uses Hydra for configuration, so all overrides are passed as
    positional ``key=value`` arguments, not ``--key value`` flags.

    Example:
        dfanalyzer analyzer/preset=dlio trace_path=/data view_types=[time_range]
    """
    cmd: List[str] = ["dfanalyzer"]

    # Core parameters
    if trace_path:
        cmd.append(f"trace_path={trace_path}")

    if view_types is not None:
        # Hydra list syntax: view_types=[file_name,proc_name]
        cmd.append(f"view_types=[{','.join(view_types)}]")

    if debug:
        cmd.append("debug=true")
    if verbose:
        cmd.append("verbose=true")

    # Analyzer selection and preset
    if analyzer != "dftracer":
        cmd.append(f"analyzer={analyzer}")
    cmd.append(f"analyzer/preset={analyzer_preset}")

    # Analyzer sub-options
    if analyzer_checkpoint is not None:
        cmd.append(f"analyzer.checkpoint={'true' if analyzer_checkpoint else 'false'}")
    if analyzer_checkpoint_dir is not None:
        cmd.append(f"analyzer.checkpoint_dir={analyzer_checkpoint_dir}")
    if analyzer_time_approximate is not None:
        cmd.append(f"analyzer.time_approximate={'true' if analyzer_time_approximate else 'false'}")
    if analyzer_time_granularity is not None:
        cmd.append(f"analyzer.time_granularity={analyzer_time_granularity}")
    if analyzer_time_resolution is not None:
        cmd.append(f"analyzer.time_resolution={analyzer_time_resolution}")

    # Output configuration
    cmd.append(f"output={output_format}")
    if output_compact is not None:
        cmd.append(f"output.compact={'true' if output_compact else 'false'}")
    if output_root_only is not None:
        cmd.append(f"output.root_only={'true' if output_root_only else 'false'}")
    if output_name and output_name.strip():
        cmd.append(f"output.name={output_name}")
    if output_run_db_path and output_run_db_path.strip():
        cmd.append(f"output.run_db_path={output_run_db_path}")

    # Cluster configuration
    cmd.append(f"cluster={cluster_type}")
    if cluster_n_workers is not None:
        cmd.append(f"cluster.n_workers={cluster_n_workers}")
    if cluster_memory_limit is not None:
        cmd.append(f"cluster.memory_limit={cluster_memory_limit}")
    if cluster_processes is not None:
        cmd.append(f"cluster.processes={cluster_processes}")
    if cluster_cores is not None:
        cmd.append(f"cluster.cores={cluster_cores}")
    if cluster_memory is not None:
        cmd.append(f"cluster.memory={cluster_memory}")

    return cmd


class DFAnalyzerService(MCPService):
    """MCP tools wrapping the ``dfanalyzer`` executable."""

    def __init__(self) -> None:
        self.analyzer_subservice = FastMCP("DFAnalyzer")
        self._register_analyze()
        self._register_list_presets()

    def _register_analyze(self) -> None:
        @self.analyzer_subservice.tool()
        def analyze(
            trace_path: str,
            view_types: Optional[List[str]] = None,
            debug: bool = False,
            verbose: bool = False,
            analyzer: str = "dftracer",
            analyzer_preset: str = "posix",
            analyzer_checkpoint: Optional[bool] = None,
            analyzer_checkpoint_dir: Optional[str] = None,
            analyzer_time_approximate: Optional[bool] = None,
            analyzer_time_granularity: Optional[float] = None,
            analyzer_time_resolution: Optional[float] = None,
            output_format: str = "console",
            output_compact: Optional[bool] = None,
            output_root_only: Optional[bool] = None,
            output_name: Optional[str] = None,
            output_run_db_path: Optional[str] = None,
            cluster_type: str = "local",
            cluster_n_workers: Optional[int] = None,
            cluster_memory_limit: Optional[str] = None,
            cluster_processes: Optional[int] = None,
            cluster_cores: Optional[int] = None,
            cluster_memory: Optional[str] = None,
        ) -> str:
            """Analyze an I/O trace using dfanalyzer and return a performance summary.

            Use this tool when the user wants to analyze I/O traces captured by dftracer,
            darshan, or recorder. It produces breakdowns of I/O performance by file, process,
            time range, or other dimensions. Call list_presets first if you are unsure which
            analyzer or preset to use.

            For a first look at any trace, only trace_path is required. The defaults work
            well for general POSIX I/O workloads. For deep learning workloads (e.g. DLIO),
            set analyzer_preset="dlio". For non-dftracer traces, set the analyzer accordingly.

            QUICK START EXAMPLES:
              # Minimal — just inspect a trace directory
              analyze(trace_path="/path/to/trace")

              # Deep-learning workload with DLIO preset, save results to SQLite
              analyze(
                  trace_path="/path/to/trace",
                  analyzer_preset="dlio",
                  output_format="sqlite",
                  output_run_db_path="/results/run.db",
              )

              # Darshan trace on a Slurm cluster, 2 nodes × 16 cores
              analyze(
                  trace_path="/path/to/darshan.darshan",
                  analyzer="darshan",
                  cluster_type="slurm",
                  cluster_processes=2,
                  cluster_cores=16,
                  cluster_memory="64GB",
              )

            PARAMETER GUIDE:

            --- WHAT TO ANALYZE ---
            trace_path (required):
                Absolute path to the I/O trace data. For dftracer this is a directory;
                for darshan it is a single .darshan file; for recorder it is a directory.

            analyzer (default "dftracer"):
                Which trace format to read.
                  "dftracer"  — traces produced by the dftracer library (default)
                  "darshan"   — Darshan HPC I/O characterization logs
                  "recorder"  — Recorder I/O tracing tool output

            analyzer_preset (default "posix"):
                Selects the analysis lens. Determines which I/O layers and metrics are
                extracted. Must match the workload type:
                  "posix" — general POSIX file I/O (read/write/open/close/seek/stat)
                  "dlio"  — deep-learning I/O patterns (PyTorch/TensorFlow data pipelines)
                If you are unsure, start with "posix".

            --- WHAT VIEWS TO SHOW ---
            view_types (default ["file_name", "proc_name", "time_range"]):
                Controls which breakdown dimensions appear in the output. Each entry
                produces a separate aggregation table.
                  "file_name"  — per-file I/O statistics (bandwidth, ops, size)
                  "proc_name"  — per-process I/O statistics
                  "time_range" — I/O activity bucketed over time
                Pass a subset to focus the output, e.g. ["file_name"] for file-only view.

            --- TIMING PRECISION ---
            analyzer_time_approximate (default true):
                Use fast approximate timestamps. Set false only if exact timestamps are
                needed — it significantly increases analysis time.

            analyzer_time_granularity (float, seconds):
                Width of time buckets used in time_range views.
                Defaults: dftracer=1.0, darshan=1.0, recorder=1.0.
                Decrease to see finer-grained time slices; increase to reduce noise.

            analyzer_time_resolution (float, nanoseconds):
                Minimum resolvable event duration. Events shorter than this are collapsed.
                Defaults: dftracer=1e6, darshan=1e3, recorder=1e7.
                Lower values capture brief events; higher values reduce clutter.

            --- CHECKPOINTING (for large or long-running traces) ---
            analyzer_checkpoint (default true):
                Save intermediate analysis state so a failed or interrupted run can
                resume without reprocessing from scratch. Recommended for large traces.

            analyzer_checkpoint_dir:
                Where to write checkpoint files.
                Default: <hydra_output_dir>/checkpoints.
                Override when you need checkpoints on a shared filesystem.

            --- OUTPUT FORMAT ---
            output_format (default "console"):
                Where to send the analysis results:
                  "console" — print tables to stdout (good for interactive inspection)
                  "csv"     — write CSV files per view (good for downstream processing)
                  "sqlite"  — write all views into a SQLite database (good for querying)

            output_compact (default false):
                Condense output tables to fewer rows. Useful for very wide result sets.

            output_root_only (default true):
                In MPI/multi-process runs, suppress output from non-root ranks. Leave
                true unless you specifically need per-rank output.

            output_name:
                Label attached to the output artifact (file prefix or DB table prefix).
                Useful when saving multiple analyses to the same directory or database.

            output_run_db_path:
                Path to the SQLite database file. ONLY used when output_format="sqlite".
                The file is created if it does not exist.
                Example: "/results/analysis.db"

            --- CLUSTER (for distributed / HPC analysis) ---
            cluster_type (default "local"):
                Dask cluster backend to use for parallel analysis:
                  "local" — use local CPU cores (default, works everywhere)
                  "slurm" — submit Dask workers as Slurm jobs
                  "lsf"   — submit Dask workers as LSF jobs
                  "pbs"   — submit Dask workers as PBS jobs
                Use "local" unless you are on an HPC system and the trace is too large
                for a single node.

            -- local cluster options --
            cluster_n_workers (int):
                Number of Dask worker processes. Default: number of CPU cores.
                Reduce if memory is constrained.

            cluster_memory_limit (str, e.g. "4GB"):
                Memory cap per worker process. Default: unlimited.
                Set this to avoid OOM when traces are large.

            -- HPC cluster options (slurm / lsf / pbs only) --
            cluster_processes (int, default 1):
                Number of compute nodes to request.

            cluster_cores (int, default 16):
                CPU cores per node/job.

            cluster_memory (str, e.g. "64GB"):
                Total memory per node/job to request from the scheduler.

            --- DEBUGGING ---
            debug (default false):
                Enable debug-level logging. Use when the tool returns an error and you
                need stack traces or internal state to diagnose it.

            verbose (default false):
                Print extra progress information during analysis. Useful for monitoring
                long-running analyses without full debug output.
            """
            cmd = _hydra_args(
                trace_path=trace_path,
                view_types=view_types,
                debug=debug,
                verbose=verbose,
                analyzer=analyzer,
                analyzer_preset=analyzer_preset,
                analyzer_checkpoint=analyzer_checkpoint,
                analyzer_checkpoint_dir=analyzer_checkpoint_dir,
                analyzer_time_approximate=analyzer_time_approximate,
                analyzer_time_granularity=analyzer_time_granularity,
                analyzer_time_resolution=analyzer_time_resolution,
                output_format=output_format,
                output_compact=output_compact,
                output_root_only=output_root_only,
                output_name=output_name,
                output_run_db_path=output_run_db_path,
                cluster_type=cluster_type,
                cluster_n_workers=cluster_n_workers,
                cluster_memory_limit=cluster_memory_limit,
                cluster_processes=cluster_processes,
                cluster_cores=cluster_cores,
                cluster_memory=cluster_memory,
            )
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return (
                    f"dfanalyzer exited with code {result.returncode}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            return result.stdout or "(no output)"

    def _register_list_presets(self) -> None:
        @self.analyzer_subservice.tool()
        def list_presets() -> str:
            """Return all valid values for every dfanalyzer configuration option.

            Call this tool before calling analyze when you are unsure which
            analyzer, preset, output format, cluster type, or view types to use.
            It returns the full option matrix so you can pick the right combination
            without guessing. The output is human-readable and can be shown directly
            to the user or used to answer questions like "what presets are available?"
            """
            lines = [
                "dfAnalyzer Presets and Configuration",
                "====================================================",
                "",
                "ANALYZER PRESETS (analyzer/preset=)",
                "- posix (default)",
                "- dlio",
                "",
                "ANALYZER TYPES (analyzer=)",
                "- dftracer (default)",
                "- darshan",
                "- recorder",
                "",
                "CLUSTER TYPES (cluster=)",
                "- local (default)",
                "- slurm",
                "- lsf",
                "- pbs",
                "",
                "OUTPUT FORMATS (output=)",
                "- console (default)",
                "- csv",
                "- sqlite",
                "",
                "VIEW TYPES (view_types=[...])",
                "- file_name (default)",
                "- proc_name (default)",
                "- time_range (default)",
                "",
                "TIME RESOLUTION DEFAULTS",
                "- dftracer: time_granularity=1s, time_resolution=1e6 ns",
                "- darshan:  time_granularity=1s, time_resolution=1e3 ns",
                "- recorder: time_granularity=1s, time_resolution=1e7 ns",
            ]
            return "\n".join(lines)

    def execute(self, data: dict) -> Optional[str]:
        """Compatibility entrypoint required by the MCPService abstract base."""
        cmd_string = " ".join(_hydra_args(**{k: v for k, v in data.items() if k != "command"}))
        return f"Would run: {cmd_string}"

    @property
    def name(self) -> str:
        return "dfanalyzer"


MCPServiceFactory.register("dfanalyzer", DFAnalyzerService())


def run() -> None:
    """Run the standalone DFAnalyzer MCP server."""
    service = DFAnalyzerService()
    service.analyzer_subservice.run()


if __name__ == "__main__":
    run()
