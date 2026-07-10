# patches/

* `annotated.patch` — `source/` -> `annotated/` delta, RELATIVE paths, apply with `patch -p1`. Build artifacts, caches and binaries excluded (they pulled in upstream author emails and absolute paths).
  whole tree (100/166 functions).
* `opt_checkpointing.patch`, `opt_trainer.patch` — `source/` -> `annotated/` for those two files.
  **These contain BOTH the dftracer annotations AND the optimization edits**, because the
  optimization work was done in the annotated tree. The optimization content is:
  * `checkpointing.py`
    * cache the best val loss (`self._best_loss`) instead of `torch.load`-ing the entire 256 MB
      best checkpoint every epoch to read one float;
    * write to a temp path + `os.replace` (atomic, fresh inode per epoch) — **required** before
      hardlinking, since `torch.save` truncates in place;
    * `hardlink_best`: `os.link(last, best)` instead of `shutil.copyfile`, with a copy fallback on
      `OSError` (cross-device);
    * `stage_dir`: optional node-local staging (**measured a regression, do not enable**).
  * `trainer.py` — plumb `hardlink_best` and `ckpt_stage_dir` through from config.
* `opt2.record.diff` — parameter delta captured by the pipeline for its own `opt2` run record.

Config knobs introduced (all default to previous behaviour):
`async_save`, `hardlink_best`, `ckpt_stage_dir`.
