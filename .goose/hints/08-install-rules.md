## Install and Privilege Rules

**Never use `sudo` or install into system paths.** All installs must be userspace only:

- Python packages: `pip install --user` or into a venv (`venv/bin/pip install`)
- npm/node: use `npm install --prefix ~/.local` or a project-local `node_modules/`
- CMake/make: always pass `-DCMAKE_INSTALL_PREFIX=<userspace path>` (e.g. `~/.local` or the session's `install/` dir)
- Autotools: always pass `--prefix=<userspace path>` to `./configure`
- Conda: use `conda install` into the active user env, never `sudo conda`
- Binaries: copy to `~/.local/bin/` or the session workspace, not `/usr/local/bin/`

If a build system defaults to `/usr/local` or requires root, override the prefix — do not run with elevated privileges.
