"""
Custom build hook: copies .agents/ from the project root into
dftracer-agents/.agents/ before each build so the skills are
always in sync and included in the installed package.
"""
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
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        super().run()


setup(cmdclass={"build_py": BuildPy})
