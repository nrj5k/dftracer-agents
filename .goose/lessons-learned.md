# Lessons Learned

Cumulative log of non-trivial errors and their fixes encountered while working on dftracer-agents.
Read this before starting any build, install, configure, or annotation task.

---
date: 2026-06-14
context: pip install -e . for dftracer-agents with dftracer @ git+...@develop in dependencies
error: |
  /workspaces/dftracer-agents/venv/lib/python3.12/site-packages/dftracer/include/zconf.h:9:10:
  fatal error: zlib_name_mangling.h: No such file or directory
root_cause: >
  dftracer's C extension bundles a custom zlib with symbol name mangling.
  The CMake-generated header zlib_name_mangling.h is not included in the sdist/wheel,
  so building from the git URL fails on any platform without a spack-managed zlib.
  No pre-built wheel exists for aarch64 Linux.
fix: |
  Move dftracer out of hard dependencies and into optional extras:
    [project.optional-dependencies]
    dftracer = ["dftracer>=2.0.0"]
  Install dftracer separately via spack, conda, or a system with x86_64 wheels.
  The MCP session service only injects dftracer calls into user code — it does not
  import dftracer itself — so this is a safe optional dependency.
tags: [dftracer, pip, C-extension, zlib, aarch64, build-failure]

---
date: 2026-06-14
context: DftracerUtilsService.__init__ calling undefined methods
error: |
  AttributeError: 'DftracerUtilsService' object has no attribute '_register_index_tools'
root_cause: >
  _register_index_tools() and _register_comparator_tools() were called in __init__
  but never defined. They were dead references left over from an earlier refactor.
fix: |
  Remove the two undefined calls from __init__:
    # deleted: self._register_index_tools()
    # deleted: self._register_comparator_tools()
tags: [dftracer-utils, AttributeError, service-registration]

---
date: 2026-06-14
context: pip install -e . failing due to spurious top-level package discovery
error: |
  Multiple top-level packages discovered in a flat-layout:
  ['split', 'outputs', 'workspace', 'reconstructed'].
root_cause: >
  setuptools auto-discovery found directories at the repo root (split/, outputs/,
  workspaces/, reconstructed/) that looked like Python packages.
fix: |
  Add to pyproject.toml to pin exactly what gets packaged:
    [tool.setuptools]
    py-modules = ["dftracer_mcp_server"]
tags: [setuptools, pip, flat-layout, package-discovery, pyproject]

---
date: 2026-06-14
context: gen_fake_trace MCP tool failing while direct binary succeeds
error: |
  CalledProcessError: Command returned non-zero exit status 1
  stderr: "parse Error: pattern '100.0' does not match to the end"
root_cause: >
  The dftracer_gen_fake_trace binary's --step-duration-ms flag only accepts integers.
  The MCP tool was passing str(100.0) = "100.0" (float with decimal point).
fix: |
  Cast to int before passing to CLI:
    cmd += ["--step-duration-ms", str(int(step_duration_ms))]
tags: [dftracer-utils, gen_fake_trace, CLI-argument, float-vs-int]

---
date: 2026-06-14
context: pytest_addoption not available in test files
error: |
  ValueError / fixture error: pytest_addoption not recognized when defined in a test file
root_cause: >
  pytest only calls pytest_addoption from conftest.py files and registered plugins,
  not from regular test files.
fix: |
  Move pytest_addoption (and any pytest_configure / pytest_collection_modifyitems hooks)
  to test/conftest.py, not to the test file itself.
tags: [pytest, conftest, addoption, hooks]

---
date: 2026-06-14
context: session_status tool raising TypeError about duplicate keyword argument
error: |
  TypeError: _ok() got multiple values for keyword argument 'workspace'
root_cause: >
  The session state dict already contained a "workspace" key. When the dict was
  unpacked with **state and "workspace" was also passed explicitly, Python raised
  a duplicate keyword error.
fix: |
  Filter the conflicting key out before unpacking:
    extra = {k: v for k, v in state.items() if k not in {"workspace"}}
    return _ok("Session status", workspace=str(ws), **extra)
tags: [session-service, TypeError, kwargs, duplicate-key]

---
date: 2026-06-14
context: pip install -e . fails with ImportError from pip's own vendored resolvelib
error: |
  ImportError: cannot import name 'RequirementInformation' from
  'pip._vendor.resolvelib.structs'
  (venv/lib/python3.12/site-packages/pip/_vendor/resolvelib/structs.py)
root_cause: >
  pip 24.0 inside the venv had a corrupted vendored resolvelib bundle.
  ensurepip --upgrade does not fix it because it sees pip as already installed.
fix: |
  Force a clean pip reinstall via get-pip.py:
    curl -sL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    venv/bin/python3 /tmp/get-pip.py
  This uninstalls the broken pip 24.0 and installs a fresh pip (26.x).
  The leftover "~ip" directory warning afterwards is harmless.
tags: [pip, venv, resolvelib, ImportError, corrupted-install]

---
date: 2026-06-14
context: Building IOR with autotools - make install fails with corrupted .Po dependency files
error: |
  .deps/IOR-aiori-DUMMY.Po:1: *** missing separator.  Stop.
  make: *** [Makefile:382: install-recursive] Error 1
root_cause: Autotools dependency tracking files (.Po files in .deps/) can become corrupted during configure/build cycles, causing make install to fail even when build succeeds
fix: |
  1. Build succeeds with 'make -j4' - binaries are created in src/
  2. Skip 'make install' and manually copy binaries:
     mkdir -p install/bin && cp src/ior src/mdtest src/md-workbench install/bin/
  3. Alternatively: run 'make clean' then reconfigure from scratch before install
tags: [ior, autotools, make-install, dependency-files]
---
