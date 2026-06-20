---
name: dftracer-install
description: Install and privilege rules for dftracer sessions — never use sudo, always install to userspace paths; autotools pkg-config integration
---

## HDF5 Version Requirement

**Always use HDF5 1.14.x for h5bench and any parallel I/O project.**

HDF5 1.14 is required to enable:
- `H5Pset_page_buffer_size` with the MPI-IO VFD (broken/unsupported in 1.10.x)
- The async VOL connector (`H5Fcreate_async`, `H5Dwrite_async`, etc.)
- Full collective metadata API (`H5Pset_all_coll_metadata_ops`)
- Improved chunk cache and metadata cache flush control

Using HDF5 1.10.x silently degrades performance:
- `H5Pset_page_buffer_size` is a no-op with the MPIO VFD in 1.10.x
- `H5Fcreate_async` is a stub that falls back to synchronous I/O
- The posix_close_ops_slope bottleneck worsens because metadata is flushed per-operation

### How to install HDF5 1.14 from source (userspace, with MPI)

```bash
wget https://github.com/HDFGroup/hdf5/releases/download/hdf5_1.14.4/hdf5-1.14.4.tar.gz
tar xf hdf5-1.14.4.tar.gz && cd hdf5-1.14.4
CC=mpicc ./configure \
  --prefix=<session_ws>/hdf5_1.14 \
  --enable-parallel \
  --enable-shared \
  --enable-build-mode=production \
  --with-zlib=/usr
make -j$(nproc) && make install
export HDF5_DIR=<session_ws>/hdf5_1.14
export LD_LIBRARY_PATH=$HDF5_DIR/lib:$LD_LIBRARY_PATH
```

Then rebuild the application with `-I$HDF5_DIR/include -L$HDF5_DIR/lib -lhdf5`.

---

## Autotools + dftracer Integration

dftracer is **CMake-only** — no `dftracer.pc` ships by default.

### Call order for autotools projects

```
session_install_dftracer       # cmake mode → libdftracer_core.so in install_ann/lib/
session_generate_dftracer_pc   # generates install_ann/lib/pkgconfig/dftracer.pc
session_patch_build            # injects pkg-config lines into Makefile/Makefile.am
session_build_annotated        # auto-sets PKG_CONFIG_PATH and --disable-dependency-tracking
```

### Never do this in bash

```bash
# WRONG — Make := syntax is not bash
DFTRACER_INC := /path/to/include   # → "DFTRACER_INC: command not found"
```

Use `PKG_CONFIG_PATH` + the generated `.pc` file instead.

### configure failures

- Add `--disable-dependency-tracking` to every `./configure` call.
- Clean stale `.deps` before reconfiguring:
  ```bash
  find <dir> -name ".deps" -type d -exec rm -rf {} + 2>/dev/null
  ```

## Install and Privilege Rules

**Never use `sudo` or install into system paths.** All installs must be userspace only:

- Python packages: `pip install --user` or into a venv (`venv/bin/pip install`)
- npm/node: use `npm install --prefix ~/.local` or a project-local `node_modules/`
- CMake/make: always pass `-DCMAKE_INSTALL_PREFIX=<userspace path>` (e.g. `~/.local` or the session's `install/` dir)
- Autotools: always pass `--prefix=<userspace path>` to `./configure`
- Conda: use `conda install` into the active user env, never `sudo conda`
- Binaries: copy to `~/.local/bin/` or the session workspace, not `/usr/local/bin/`

If a build system defaults to `/usr/local` or requires root, override the prefix — do not run with elevated privileges.
