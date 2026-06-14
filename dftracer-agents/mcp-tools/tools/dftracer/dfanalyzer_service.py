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
    """Build the ``dfanalyzer`` command from Hydra-style overrides."""
    cmd: List[str] = ["dfanalyzer"]

    if trace_path:
        cmd.extend(["--trace-path", trace_path])

    default_view_types = ["file_name", "proc_name", "time_range"]
    if view_types is not None and view_types != default_view_types:
        for view_type in view_types:
            cmd.extend(["--view-type", view_type])

    if debug:
        cmd.append("--debug")
    if verbose:
        cmd.append("--verbose")

    if analyzer != "dftracer":
        cmd.append(f"-ahydra/analyzer={analyzer}")
    cmd.append(f"-ahydra.analyzer/preset={analyzer_preset}")

    if analyzer_checkpoint is not None:
        cmd.append(f"--analyzer.checkpoint={'true' if analyzer_checkpoint else 'false'}")
    if analyzer_time_approximate is not None:
        cmd.append(
            f"--analyzer.time_approximate={'true' if analyzer_time_approximate else 'false'}"
        )
    if analyzer_time_granularity is not None:
        cmd.extend(["--analyzer.time_granularity", str(analyzer_time_granularity)])
    if analyzer_time_resolution is not None:
        cmd.extend(["--analyzer.time_resolution", str(analyzer_time_resolution)])

    cmd.append(f"--output={output_format}")
    if output_compact is not None:
        cmd.append(f"--output.compact={'true' if output_compact else 'false'}")
    if output_root_only is not None:
        cmd.extend(["--output.root_only", str(output_root_only)])
    if output_name and output_name.strip():
        cmd.extend(["--output.name", output_name])
    if output_run_db_path and output_run_db_path.strip():
        cmd.extend(["--output.run_db_path", output_run_db_path])

    cmd.append(f"--cluster={cluster_type}")
    if cluster_n_workers is not None:
        cmd.extend(["--cluster.n_workers", str(cluster_n_workers)])
    if cluster_memory_limit is not None:
        cmd.extend(["--cluster.memory_limit", cluster_memory_limit])
    if cluster_processes is not None:
        cmd.extend(["--cluster.processes", str(cluster_processes)])
    if cluster_cores is not None:
        cmd.extend(["--cluster.cores", str(cluster_cores)])
    if cluster_memory is not None:
        cmd.extend(["--cluster.memory", cluster_memory])

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
            """Run dfanalyzer on the provided trace path."""
            cmd = _hydra_args(
                trace_path=trace_path,
                view_types=view_types,
                debug=debug,
                verbose=verbose,
                analyzer=analyzer,
                analyzer_preset=analyzer_preset,
                analyzer_checkpoint=analyzer_checkpoint,
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
            """List common presets and supported modes from the docs.

            Reference:
            https://dftracer.readthedocs.io/projects/analyzer/en/latest/getting-started.html
            https://dftracer.readthedocs.io/projects/analyzer/en/latest/configuration.html
            """
            lines = [
                "dfAnalyzer Presets and Configuration",
                "====================================================",
                "",
                "ANALYZER PRESETS (analyzer/preset)",
                "- posix (default)",
                "- dlio",
                "",
                "CLUSTER TYPES (cluster=)",
                "- local",
                "- slurm",
                "- lsf",
                "- pbs",
                "",
                "OUTPUT FORMATS (output=)",
                "- console (default)",
                "- csv",
                "- sqlite",
                "",
                "ANALYZER TYPES (analyzer=)",
                "- dftracer (default)",
                "- darshan",
                "- recorder",
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
