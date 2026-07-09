# params/

One config per case in `results/results.csv`.

## Paths are redacted placeholders — substitute before use

These files were scrubbed of identifying content before publication. Wherever you see:

| placeholder | replace with |
| --- | --- |
| `$LUSTRE_ROOT` | your parallel-filesystem scratch root (e.g. `/p/lustre<N>/<you>`) |
| `$PROJECT_ROOT` | your checkout of `dftracer-agents` |
| `$USER`, `$HOME` | the obvious |
| `<flux-jobid>` | a real Flux allocation id |

**YAML does not expand shell variables.** `base_run_dir: "$LUSTRE_ROOT/..."` is a literal string.
Substitute it (`sed -i "s#\$LUSTRE_ROOT#/p/lustre5/$USER#g" params/*.yml`) or set the paths by hand
before running a case.

## Sizing rule (or the app raises ValueError)

```
volumes = n_categories * n_instances_used_per_fractal / n_fracts_per_vol
```
then `val_split`% become validation. **Both** train and val counts must exceed the rank count.
For 32 ranks: `n_categories=20, n_instances_used_per_fractal=24` -> 160 volumes -> 112 train / 48 val.
`batch_size` is **per rank**: `batch_size * world_size <= n_train` (`4 x 32 = 128 > 112` fails).
