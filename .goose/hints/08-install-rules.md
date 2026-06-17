## Install and Privilege Rules

**Never use `sudo` or install into system paths.** All installs must be userspace only:

- Python packages: `pip install --user` or into a venv (`venv/bin/pip install`)
- npm/node: use `npm install --prefix ~/.local` or a project-local `node_modules/`
- CMake/make: always pass `-DCMAKE_INSTALL_PREFIX=<userspace path>` (e.g. `~/.local` or the session's `install/` dir)
- Autotools: always pass `--prefix=<userspace path>` to `./configure`
- Conda: use `conda install` into the active user env, never `sudo conda`
- Binaries: copy to `~/.local/bin/` or the session workspace, not `/usr/local/bin/`

If a build system defaults to `/usr/local` or requires root, override the prefix — do not run with elevated privileges.

## dftracer Backend Support — MPI and HDF5

dftracer must be built with explicit backend flags to trace MPI-IO and HDF5 I/O.
The default build enables only POSIX hooking.

**CMake build:**
```bash
cmake -DCMAKE_INSTALL_PREFIX=<install_path> \
      -DDFTRACER_ENABLE_MPI=ON \
      -DDFTRACER_ENABLE_HDF5=ON \
      -DDFTRACER_ENABLE_FTRACING=ON \
      <dftracer_src>/
make -j4 install
```

**pip/setup.py build (Python wrapper):**
```bash
DFTRACER_ENABLE_MPI=ON DFTRACER_ENABLE_HDF5=ON pip install .
```

**Verify the installed build has MPI support:**
```bash
grep DFTRACER_MPI_ENABLE <install_path>/include/dftracer/core/dftracer_config.hpp
# Expected: #define DFTRACER_MPI_ENABLE 1
```
If the output shows `0` or the grep returns nothing, MPI support was not compiled in.
Rebuild with the flags above before annotating MPI applications.

**When to rebuild dftracer:**
- Application uses MPI-IO (MPIIO_* functions, MPI_File_*)
- Application uses HDF5 with parallel I/O (h5py parallel, HDF5 collective I/O)
- Trace shows only POSIX hook events even though MPI or HDF5 functions are annotated

## Autotools Stale State Cleanup

When reconfiguring an autotools project (e.g., after changing `--with-hdf5` flags),
stale generated files cause `./configure` to silently reuse old settings:

```bash
make distclean
rm -rf .deps src/.deps autom4te.cache config.status config.log Makefile
autoreconf -if   # only if configure.ac was modified
```

Then re-run `./configure` with the new flags. Without this cleanup, the build may
silently ignore the new flags and produce a binary without HDF5/MPI support.

## HDF5 Autotools Configure Flag

For autotools projects (IOR, etc.) that detect HDF5 via `--with-hdf5`:

**Wrong (does not enable HDF5 in IOR):**
```bash
./configure --with-hdf5=/path/to/hdf5 ...
```
With an explicit path, IOR's configure may find the prefix but not set `USE_HDF5_AIORI`.

**Correct:**
```bash
export CPPFLAGS="-I${HDF5_PREFIX}/include"
export LDFLAGS="-L${HDF5_PREFIX}/lib -Wl,-rpath,${HDF5_PREFIX}/lib"
export LIBS="-lhdf5 -lz"
./configure --with-hdf5 ...   # bare flag, no path — paths come from env
```

Use `grep USE_HDF5_AIORI config.h` after configure to confirm it was set to 1.
If it shows 0 or is absent, the HDF5 backend was not enabled.
