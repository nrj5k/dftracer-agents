"""
Custom build hook: links .agents/ from the project root into
dftracer-agents/.agents/ before each build so the skills are
included in the installed package without duplicating the files
on disk (a symlink, not a copy).
"""
import os
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        root = Path(__file__).parent
        src = root / ".agents"
        dst = root / "dftracer-agents" / ".agents"
        if src.exists():
            if dst.is_symlink() or dst.exists():
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                else:
                    shutil.rmtree(dst)
            dst.symlink_to(os.path.relpath(src, dst.parent), target_is_directory=True)
        super().run()


setup(
    cmdclass={"build_py": BuildPy},
    scripts=["dftracer-agents/dftracer_mcp_server.sh"],
)
