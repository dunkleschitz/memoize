# memoize CHANGELOG

## v1 — 2026-10-22

Initial build.

- Single-file stdlib CLI (`memoize.py`): `run`, `invalidate`, `bust`,
  `status`.
- `run --ttl SECS -- CMD...`: caches stdout+stderr+exit of exit-0 runs;
  wrapped command's exit code propagated; memo's own codes 0/1/2.
- Cache key = argv + resolved cwd + stdin bytes + `--env` selections +
  content hashes of tracked input files (default: cwd tree minus
  `.git`/`__pycache__`/cache dir; `--inputs`/`--exclude` globs;
  stat fast-path, mtime excluded from the key so pure touches still hit).
- Stale-cache hazard designed out: HIT marker on stderr for every hit
  (`--quiet` to suppress), explicit bounded TTLs (1..86400s, required),
  only exit-0 cached, secret-like output never written to cache,
  mutable-command deny-list (git mutating subcommands included) unless
  `--force-mutable`, `--check-deterministic` double-run gate,
  `--max-output` cap (default 5MB).
- `invalidate` by argv prefix or `--all`; superseded entries pruned on
  rewrite; atomic entry/index writes; dirs `0700`, files `0600`.
- `--version` (`memoize 1.0.0`, module-level VERSION constant).
- 38 tests passing (`test_memoize.py`).
