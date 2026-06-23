## Autotools + dftracer Integration

dftracer is a **CMake-only project** — it does NOT install a `dftracer.pc` file
by default.  Autotools projects that link dftracer require a `.pc` file so that
`pkg-config --cflags dftracer` and `pkg-config --libs dftracer` return the
correct flags.

### Required call order for autotools projects

```
session_install_dftracer      # builds libdftracer_core.so in install_ann/lib/
session_generate_dftracer_pc  # writes install_ann/lib/pkgconfig/dftracer.pc
session_patch_build           # injects pkg-config lines into Makefile/Makefile.am
session_build_annotated       # sets PKG_CONFIG_PATH=install_ann/lib/pkgconfig and builds
```

`session_build_annotated` automatically sets `PKG_CONFIG_PATH`, `CPPFLAGS`,
and `LDFLAGS` in the subprocess environment, so once the `.pc` file exists the
autotools build finds dftracer without any manual env export.

### Manual environment export (if running configure/make by hand)

```bash
WS=/workspaces/dftracer-agents/workspaces/<RUN_ID>
export PKG_CONFIG_PATH=$WS/install_ann/lib/pkgconfig:$PKG_CONFIG_PATH
export CPPFLAGS="-I$WS/install_ann/include"
export LDFLAGS="-L$WS/install_ann/lib -Wl,-rpath,$WS/install_ann/lib"
```

### dftracer.pc content (for reference)

```
prefix=<workspace>/install_ann
exec_prefix=${prefix}
libdir=${exec_prefix}/lib
includedir=${prefix}/include

Name: dftracer
Description: DFTracer I/O tracing library
Version: 4.1.0
Libs: -L${libdir} -ldftracer_core -Wl,-rpath,${libdir}
Cflags: -I${includedir}
```

### configure --disable-dependency-tracking

Always pass `--disable-dependency-tracking` to `./configure` for annotated builds.
Without it, `config.status` fails when `.deps` directories contain stale state.
`session_configure` and `session_build_annotated` add this flag automatically.
To clean stale `.deps` manually:

```bash
find <annotated_dir> -name ".deps" -type d -exec rm -rf {} + 2>/dev/null
```

### Makefile patch note

The injected block in Makefile/Makefile.am is:
```makefile
DFTRACER_CFLAGS  := $(shell pkg-config --cflags dftracer 2>/dev/null)
DFTRACER_LDFLAGS := $(shell pkg-config --libs   dftracer 2>/dev/null)
CFLAGS   += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
CXXFLAGS += $(DFTRACER_CFLAGS)   -DDFTRACER_ENABLE
LDFLAGS  += $(DFTRACER_LDFLAGS)
```

**Do NOT** try to set `DFTRACER_INC` or `DFTRACER_LIB` as shell variables —
Make `:=` syntax is not valid bash and produces `command not found` errors.
Use `PKG_CONFIG_PATH` env var instead.
