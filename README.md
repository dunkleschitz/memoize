# memoize — TTL command-result memoization for agent shells

Agents re-run the same expensive read-only commands over and over —
test suites, linters, repo scans, API probes. `memoize` wraps one:

```bash
memo run --ttl 300 -- pytest -x -q
```

The first run executes normally; repeats within the TTL are served from
a local content-keyed cache instead of re-running. Stdout, stderr, and
the exit code are replayed exactly.

Exit codes: memo's own `0` ok / `1` operational failure / `2` usage
error — but when the wrapped command runs (hit or miss), **memo exits
with the command's exit code**, so it drops into scripts transparently.

## Commands

| Command | Effect |
|---|---|
| `run --ttl SECS [--inputs G ...] [--exclude G ...] [--env NAME[=V] ...] [--max-output N] [--check-deterministic] [--force-mutable] [--cwd DIR] [--quiet] [--verbose] -- CMD [ARGS...]` | Run with memoization. `--ttl` (1–86400) is required; there is no infinite caching. |
| `invalidate [--all \| -- CMD...]` | Drop cached entries: every entry, or entries whose argv starts with the given prefix. |
| `bust` | Drop every cached entry. |
| `status [--json]` | Entry counts and bytes. |

Cache location: `--` isn't a flag for this; set `$MEMOIZE_DIR`, else
`$XDG_CACHE_HOME/memoize`, else `~/.cache/memoize`. Directories are
created `0700`, entries `0600`.

## The stale-cache hazard, designed out

Caching command output is only safe if staleness can never be silent.
memoize treats that as the core design problem:

- **Every hit is announced.** `[memo HIT age=12s ttl=300s key=abc123…]`
  goes to stderr on every single cache hit (`--quiet` suppresses it).
  You always know when you are reading cached output.
- **Content-keyed, not argv-keyed.** The key covers argv, the resolved
  cwd, piped stdin bytes, `--env` selections, and the **content hashes**
  of tracked input files (default: the whole cwd tree, minus `.git`,
  `__pycache__`, and the cache dir itself). Editing a file is a
  different key, not a stale hit. A pure mtime touch with identical
  content still hits (stat fast-path; mtime is not part of the key).
  `--inputs` narrows tracking to globs when you know the dependency
  set; `--exclude` widens the ignore list.
- **Bounded TTLs, explicit opt-in.** `--ttl` is required and capped at
  24h. `invalidate`/`bust` is the big red button.
- **Only exit-0 is cached.** A failing command re-runs next time.
- **Secret-bearing output is never cached.** Stdout/stderr matching
  secret-like patterns (cloud key ids, `ghp_`/`xox` tokens, private-key
  headers, `password=`-style assignments) is served fresh and never
  written to disk. Fail-safe direction: a false positive just means
  "runs uncached".
- **Mutating commands are refused.** A deny-list of binaries (`rm`,
  `mv`, `dd`, `sudo`, `docker`, `kubectl`, …) and mutating `git`
  subcommands (`push`, `reset`, `clean`, …) runs uncached with a
  warning unless `--force-mutable` is passed. This is a heuristic, not
  a sandbox — see Limitations.
- **`--check-deterministic`.** On a miss, runs the command twice and
  caches only if both outputs agree. For commands you suspect of
  hiding `date`/randomness in their output.
- **Size caps.** `--max-output` (default 5MB) bounds cached output;
  oversized output streams through uncached.

Writes are atomic (temp file + rename); concurrent `memo run`
processes may race on the index, last-writer-wins, entries themselves
stay consistent.

## Examples

```bash
# expensive suite, fresh at most every 5 minutes
memo run --ttl 300 -- pytest -q

# key also on an env var the command reads
memo run --ttl 600 --env DJANGO_SETTINGS_MODULE -- ./manage.py check

# only these inputs matter; ignore the noisy log dir
memo run --ttl 120 --inputs 'src/**' --exclude 'logs/**' -- make build

# prove a flaky-looking command is deterministic before trusting cache
memo run --ttl 60 --check-deterministic -- ./scripts/probe.sh

# something changed upstream; drop this command's entries
memo invalidate -- pytest -q
```

## Limitations

- Input tracking assumes the command's dependencies live under the cwd
  (or the `--inputs` globs). A command that reads `/etc/config.yaml`
  or a database while its cwd tree is unchanged will serve a stale hit
  inside the TTL — the HIT marker and the TTL bound are the backstop,
  not a guarantee. Declare `--inputs` for anything outside the cwd.
- The mutable-command deny-list is a speed bump, not a sandbox: `sh -c
  "rm -rf /"` is not caught by basename matching. Only wrap commands
  you have read.
- Environment differences are the caller's responsibility: only `--env`
  selections enter the key. Two runs with different `PATH`s but the
  same argv share a cache entry.
- TTL is a staleness bound, not freshness: within the TTL, memoize
  trusts its key. Keep TTLs short for fast-moving inputs.
- `--check-deterministic` doubles the cost of a miss and only compares
  two consecutive runs.
- Input scanning costs one full content hash of the tracked tree per
  fresh key (later runs reuse a stat fast-path). Run from the project
  directory the command belongs to — not from `$HOME` — and use
  `--inputs` to narrow tracking for big trees. Beyond 100,000 tracked
  files memoize declines to cache and runs uncached.

## Pre-publish checklist

Per the project charter, nothing ships publicly until the maintainer
approves the publishing account/org and the license. Before
first publication:

1. **Secrets hygiene** — run a secrets scan over the repo and its history
   (`gitleaks detect --source .` or `trufflehog git file://.`), resolve
   every hit, and grep once more for obvious keys/tokens by hand.
2. **History review** — read `git log` and the full diff end-to-end; no
   credentials, private paths, or personal data may be committed.
3. **License** — replace the TBD notice with the maintainer-approved
   LICENSE file.

License: TBD (maintainer approval pending).
