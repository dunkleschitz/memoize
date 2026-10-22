#!/usr/bin/env python3
"""memoize - TTL command-result memoization for agent shells.

Agents re-run the same expensive read-only commands over and over
(test suites, linters, repo scans). `memo run --ttl 300 -- <cmd>`
serves cached stdout+stderr from a content-keyed store instead of
re-running, within a bounded TTL.

The stale-cache hazard is designed out, not documented away:

- Every cache HIT is announced on stderr (`[memo HIT age=.. ttl=..]`);
  staleness is always visible, never silent.
- The cache key covers argv, cwd, stdin bytes, selected env vars, and
  the *content hashes* of tracked input files (default: the cwd tree).
  A changed file is a different key, not a stale hit.
- TTLs are explicit and bounded (1..86400s). No infinite caching.
- Only exit-0 runs are cached. Output with secret-like literals is
  never written to the cache. Obviously-mutating commands are refused
  unless --force-mutable.
- `memo invalidate` / `memo bust` give the agent a big red button.

Single file, stdlib only. Cache lives in $MEMOIZE_DIR, $XDG_CACHE_HOME,
or ~/.cache/memoize (dirs 0700, files 0600).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import PurePath

VERSION = "1.0.0"

# Exit codes for memo's own failures. A successfully executed command
# (hit or miss) propagates the command's own exit code.
EXIT_OK = 0
EXIT_OPS = 1   # operational failure (unreadable cache, etc.)
EXIT_USAGE = 2

DEFAULT_MAX_OUTPUT = 5 * 1024 * 1024
MAX_TTL = 86400
MAX_TRACKED_FILES = 100_000

# Basenames that (almost) always mutate something. Refused unless
# --force-mutable. Heuristic, not a guarantee -- see README.
MUTABLE_BINARIES = {
    "sudo", "su", "doas", "rm", "rmdir", "mv", "dd", "mkfs", "shred",
    "shutdown", "reboot", "halt", "poweroff", "systemctl", "service",
    "useradd", "userdel", "usermod", "chown", "mkswap", "swapon",
    "iptables", "apt", "apt-get", "dnf", "yum", "pacman", "zypper",
    "docker", "podman", "kubectl", "terraform", "ansible-playbook",
}
# git subcommands that mutate the working tree / remotes.
MUTABLE_GIT_SUBCOMMANDS = {
    "push", "reset", "clean", "checkout", "restore", "rm", "mv",
    "stash", "commit", "am", "rebase", "merge", "pull", "fetch",
    "clone", "init", "branch", "tag", "worktree",
}

# Secret-like literals: never written to the cache. Fail-safe direction:
# a false positive just means "runs uncached".
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token)"
               r"\s*[:=]\s*\S{4,}"),
]

DEFAULT_EXCLUDES = [".git", ".hg", ".svn", "__pycache__"]


def die_usage(msg):
    sys.stderr.write(f"memoize: {msg}\n")
    return EXIT_USAGE


def cache_dir():
    d = (os.environ.get("MEMOIZE_DIR")
         or os.path.join(os.environ.get("XDG_CACHE_HOME",
                                        os.path.expanduser("~/.cache")),
                         "memoize"))
    return d


def ensure_dirs(base):
    entries = os.path.join(base, "entries")
    os.makedirs(entries, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    os.chmod(entries, 0o700)
    return entries


def index_path(base):
    return os.path.join(base, "index.json")


def load_index(base):
    p = index_path(base)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"[memo] warning: unreadable index ({e}); "
                         f"starting fresh\n")
        return {}


def save_index(base, index):
    p = index_path(base)
    fd, tmp = tempfile.mkstemp(dir=base, prefix="index-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entry_path(base, key):
    return os.path.join(base, "entries", key + ".json")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _matches_any(rel, patterns):
    return any(PurePath(rel).match(p) for p in patterns)


def scan_inputs(cwd, include_globs, exclude_globs):
    """Walk cwd and hash tracked files.

    Returns (inputs_map, inputs_hash) where inputs_map is
    {relpath: "size:mtime_ns:sha256"} and inputs_hash digests the whole
    set. Raises _TooManyFiles when the tree is absurdly large.
    """
    excludes = list(DEFAULT_EXCLUDES) + list(exclude_globs or [])
    # The cache dir itself must never be an input (self-reference loop).
    cdir = os.path.realpath(cache_dir())
    inputs = {}
    count = 0
    for root, dirs, files in os.walk(cwd, followlinks=False):
        # prune excluded directories
        dirs[:] = [d for d in dirs
                   if not _matches_any(os.path.relpath(
                       os.path.join(root, d), cwd), excludes)]
        # prune the cache dir if it sits under cwd
        dirs[:] = [d for d in dirs
                   if os.path.realpath(os.path.join(root, d)) != cdir]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, cwd)
            if _matches_any(rel, excludes):
                continue
            if include_globs and not _matches_any(rel, include_globs):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) and not os.path.islink(full):
                continue
            count += 1
            if count > MAX_TRACKED_FILES:
                raise _TooManyFiles(count)
            if os.path.islink(full):
                try:
                    target = os.readlink(full)
                except OSError:
                    continue
                digest = sha256_bytes(b"symlink:" + target.encode())
                inputs[rel] = f"0:0:{digest}"
            else:
                try:
                    with open(full, "rb") as f:
                        digest = sha256_bytes(f.read())
                except OSError:
                    continue
                inputs[rel] = f"{st.st_size}:{st.st_mtime_ns}:{digest}"
    return inputs, _inputs_fingerprint(inputs)


def _inputs_fingerprint(inputs):
    """Content fingerprint of an inputs map.

    The map values carry "size:mtime_ns:sha256" (mtime enables the stat
    fast-path), but the fingerprint deliberately drops mtime: a pure
    mtime touch with identical content must be the same key, not a miss.
    """
    h = hashlib.sha256()
    for rel in sorted(inputs):
        size, _, sha = inputs[rel].split(":", 2)
        h.update(rel.encode())
        h.update(b"\x00")
        h.update(size.encode())
        h.update(b"\x00")
        h.update(sha.encode())
        h.update(b"\x00")
    return h.hexdigest()


class _TooManyFiles(Exception):
    pass


def refresh_inputs(cwd, include_globs, exclude_globs, old_map):
    """Fast-path rescan: reuse stored hashes when size+mtime match.

    Returns (inputs_map, inputs_hash) or raises _TooManyFiles /
    _InputsChangedTooMuch (structural change beyond stat fast-path --
    caller should do a full rescan, which this function already did).
    Simpler: always do the full content scan but skip re-reading files
    whose stat matches. Implemented by scanning stats first, then
    hashing only changed/new files.
    """
    excludes = list(DEFAULT_EXCLUDES) + list(exclude_globs or [])
    cdir = os.path.realpath(cache_dir())
    current = {}  # rel -> (size, mtime_ns, is_link)
    count = 0
    for root, dirs, files in os.walk(cwd, followlinks=False):
        dirs[:] = [d for d in dirs
                   if not _matches_any(os.path.relpath(
                       os.path.join(root, d), cwd), excludes)]
        dirs[:] = [d for d in dirs
                   if os.path.realpath(os.path.join(root, d)) != cdir]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, cwd)
            if _matches_any(rel, excludes):
                continue
            if include_globs and not _matches_any(rel, include_globs):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            count += 1
            if count > MAX_TRACKED_FILES:
                raise _TooManyFiles(count)
            current[rel] = (st.st_size, st.st_mtime_ns,
                            os.path.islink(full), full)
    inputs = {}
    for rel, (size, mtime, is_link, full) in current.items():
        old = old_map.get(rel)
        if (not is_link and old
                and old.startswith(f"{size}:{mtime}:")):
            inputs[rel] = old  # fast path: stat unchanged
            continue
        if is_link:
            try:
                digest = sha256_bytes(
                    b"symlink:" + os.readlink(full).encode())
            except OSError:
                continue
            inputs[rel] = f"0:0:{digest}"
        else:
            try:
                with open(full, "rb") as f:
                    digest = sha256_bytes(f.read())
            except OSError:
                continue
            inputs[rel] = f"{size}:{mtime}:{digest}"
    return inputs, _inputs_fingerprint(inputs)


def read_stdin_bytes():
    # Never block waiting for stdin: only consume it when it is a pipe
    # or file with data/EOF already pending (zero-timeout select). A TTY
    # or a not-yet-fed pipe is treated as "no stdin" -- the alternative
    # is hanging forever on an inherited descriptor that never EOFs.
    if sys.stdin.isatty():
        return b""
    try:
        import select
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
    except (OSError, ValueError):
        return b""
    if not ready:
        return b""
    data = sys.stdin.buffer.read()
    return data if data else b""


def resolve_env(env_specs):
    """--env NAME (from environ) or --env NAME=VALUE. Returns dict."""
    env = {}
    for spec in env_specs or []:
        if "=" in spec:
            name, _, value = spec.partition("=")
            env[name] = value
        else:
            if spec in os.environ:
                env[spec] = os.environ[spec]
            else:
                sys.stderr.write(f"[memo] warning: --env {spec} not set; "
                                 f"ignored\n")
    return env


def compute_key(argv, cwd, stdin_bytes, env_map, inputs_hash):
    h = hashlib.sha256()
    h.update(b"memoize/1\x00")
    h.update(json.dumps(argv, sort_keys=True).encode())
    h.update(b"\x00")
    h.update(os.path.realpath(cwd).encode())
    h.update(b"\x00")
    h.update(sha256_bytes(stdin_bytes).encode())
    h.update(b"\x00")
    h.update(json.dumps(env_map, sort_keys=True).encode())
    h.update(b"\x00")
    h.update(inputs_hash.encode())
    return h.hexdigest()


def looks_mutable(argv):
    base = os.path.basename(argv[0]) if argv else ""
    if base in MUTABLE_BINARIES:
        return base
    if base == "git" and len(argv) > 1 and argv[1] in MUTABLE_GIT_SUBCOMMANDS:
        return f"git {argv[1]}"
    return None


def looks_secret(data):
    for blob in data:
        if not blob:
            continue
        text = blob.decode("utf-8", errors="replace")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                return pat.pattern
    return None


def write_entry(base, key, payload):
    p = entry_path(base, key)
    fd, tmp = tempfile.mkstemp(dir=os.path.join(base, "entries"),
                               prefix="entry-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def emit(entry, quiet):
    sys.stdout.buffer.write(base64.b64decode(entry["stdout_b64"]))
    sys.stderr.buffer.write(base64.b64decode(entry["stderr_b64"]))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.flush()


def cmd_run(args):
    if not args.cmd:
        return die_usage("run needs a command after `--`")
    if args.ttl is None:
        return die_usage("run needs --ttl SECONDS (1..86400)")
    if not (1 <= args.ttl <= MAX_TTL):
        return die_usage(f"--ttl must be 1..{MAX_TTL}")

    argv = args.cmd
    cwd = os.path.realpath(args.cwd or os.getcwd())
    stdin_bytes = read_stdin_bytes()
    if len(stdin_bytes) > DEFAULT_MAX_OUTPUT:
        sys.stderr.write("[memo] stdin too large; running uncached\n")
        return run_uncached(argv, cwd, stdin_bytes)
    env_map = resolve_env(args.env)

    mutable = looks_mutable(argv)
    if mutable and not args.force_mutable:
        sys.stderr.write(
            f"[memo] refusing to cache `{mutable}` (looks mutating); "
            f"running uncached. Use --force-mutable to override.\n")
        return run_uncached(argv, cwd, stdin_bytes)

    try:
        base = cache_dir()
        ensure_dirs(base)
    except OSError as e:
        sys.stderr.write(f"[memo] cache dir unusable ({e}); "
                         f"running uncached\n")
        return run_uncached(argv, cwd, stdin_bytes)

    index = load_index(base)

    # Fast path: if we have seen this exact argv+cwd+stdin+env before,
    # try to refresh inputs against the stored file-state; a full miss
    # (no candidate) does one full scan.
    candidate_key = None
    for key, meta in index.items():
        if (meta.get("argv") == argv and meta.get("cwd") == cwd
                and meta.get("stdin_sha") == sha256_bytes(stdin_bytes)
                and meta.get("env") == env_map):
            candidate_key = key
            break

    try:
        if candidate_key:
            old_map = index[candidate_key].get("inputs_map") or {}
            inputs_map, inputs_hash = refresh_inputs(
                cwd, args.inputs, args.exclude, old_map)
        else:
            inputs_map, inputs_hash = scan_inputs(
                cwd, args.inputs, args.exclude)
    except _TooManyFiles:
        sys.stderr.write("[memo] too many tracked files; running uncached\n")
        return run_uncached(argv, cwd, stdin_bytes)

    key = compute_key(argv, cwd, stdin_bytes, env_map, inputs_hash)
    now = time.time()
    meta = index.get(key)
    if meta:
        age = now - meta.get("created", 0)
        if age <= meta.get("ttl", 0):
            try:
                with open(entry_path(base, key), "r",
                          encoding="utf-8") as f:
                    entry = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass  # fall through to miss
            else:
                if not args.quiet:
                    sys.stderr.write(
                        f"[memo HIT age={int(age)}s ttl={meta['ttl']}s "
                        f"key={key[:12]}]\n")
                emit(entry, args.quiet)
                return entry.get("exit", 0)
        else:
            # lazy expiry
            try:
                os.unlink(entry_path(base, key))
            except OSError:
                pass
            index.pop(key, None)
            if args.verbose:
                sys.stderr.write("[memo MISS reason=expired]\n")
    elif args.verbose:
        sys.stderr.write("[memo MISS reason=cold]\n")

    # MISS: run the command.
    try:
        proc = subprocess.run(argv, input=stdin_bytes,
                              capture_output=True, cwd=cwd)
    except FileNotFoundError:
        sys.stderr.write(f"[memo] command not found: {argv[0]}\n")
        return EXIT_OPS
    except OSError as e:
        sys.stderr.write(f"[memo] failed to run command: {e}\n")
        return EXIT_OPS

    out, err, code = proc.stdout, proc.stderr, proc.returncode

    if args.check_deterministic:
        try:
            proc2 = subprocess.run(argv, input=stdin_bytes,
                                   capture_output=True, cwd=cwd)
        except OSError as e:
            sys.stderr.write(f"[memo] determinism re-run failed: {e}\n")
            return EXIT_OPS
        if (proc2.stdout, proc2.stderr, proc2.returncode) != (out, err, code):
            sys.stderr.write("[memo] output differed between two runs; "
                             "not caching (non-deterministic)\n")
            sys.stdout.buffer.write(out)
            sys.stderr.buffer.write(err)
            return code

    cacheable = True
    why_not = None
    if code != 0:
        cacheable, why_not = False, f"exit code {code}"
    elif len(out) + len(err) > args.max_output:
        cacheable, why_not = False, "output exceeds --max-output"
    else:
        hit_pat = looks_secret((out, err))
        if hit_pat:
            cacheable, why_not = False, "output looks secret-bearing"

    if cacheable:
        payload = {
            "argv": argv,
            "cwd": cwd,
            "created": now,
            "ttl": args.ttl,
            "exit": code,
            "stdout_b64": base64.b64encode(out).decode(),
            "stderr_b64": base64.b64encode(err).decode(),
            "inputs_hash": inputs_hash,
        }
        try:
            write_entry(base, key, payload)
            stdin_sha = sha256_bytes(stdin_bytes)
            # Prune superseded entries for the same command: a new key
            # means the inputs changed, so older snapshots of this exact
            # command are dead weight.
            for other in [k for k, m in index.items()
                          if k != key
                          and m.get("argv") == argv
                          and m.get("cwd") == cwd
                          and m.get("stdin_sha") == stdin_sha
                          and m.get("env") == env_map]:
                try:
                    os.unlink(entry_path(base, other))
                except OSError:
                    pass
                index.pop(other, None)
            index[key] = {
                "argv": argv,
                "cwd": cwd,
                "stdin_sha": stdin_sha,
                "env": env_map,
                "created": now,
                "ttl": args.ttl,
                "bytes": len(out) + len(err),
                "inputs_map": inputs_map,
            }
            save_index(base, index)
        except OSError as e:
            sys.stderr.write(f"[memo] cache write failed ({e}); "
                             f"serving fresh output\n")
    elif args.verbose or why_not != f"exit code {code}":
        # Always surface the interesting refusals; a plain nonzero exit
        # is normal and needs no lecture.
        if why_not and why_not != f"exit code {code}":
            sys.stderr.write(f"[memo] not caching: {why_not}\n")

    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return code


def run_uncached(argv, cwd, stdin_bytes):
    try:
        proc = subprocess.run(argv, input=stdin_bytes, cwd=cwd)
    except FileNotFoundError:
        sys.stderr.write(f"[memo] command not found: {argv[0]}\n")
        return EXIT_OPS
    except OSError as e:
        sys.stderr.write(f"[memo] failed to run command: {e}\n")
        return EXIT_OPS
    return proc.returncode


def cmd_invalidate(args):
    try:
        base = cache_dir()
        ensure_dirs(base)
    except OSError as e:
        sys.stderr.write(f"[memo] cache dir unusable: {e}\n")
        return EXIT_OPS
    index = load_index(base)
    if args.all:
        targets = list(index)
    elif args.cmd:
        prefix = args.cmd
        targets = [k for k, m in index.items()
                   if m.get("argv", [])[:len(prefix)] == prefix]
    else:
        return die_usage("invalidate needs --all or a command argv")
    removed = 0
    freed = 0
    for key in targets:
        meta = index.pop(key, None)
        freed += (meta or {}).get("bytes", 0)
        try:
            os.unlink(entry_path(base, key))
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            pass
    save_index(base, index)
    sys.stdout.write(f"invalidated {removed} entr"
                     f"{'y' if removed == 1 else 'ies'} "
                     f"({freed} bytes)\n")
    return EXIT_OK


def cmd_bust(args):
    args.all = True
    args.cmd = []
    return cmd_invalidate(args)


def cmd_status(args):
    try:
        base = cache_dir()
        ensure_dirs(base)
    except OSError as e:
        sys.stderr.write(f"[memo] cache dir unusable: {e}\n")
        return EXIT_OPS
    index = load_index(base)
    now = time.time()
    live = sum(1 for m in index.values()
               if now - m.get("created", 0) <= m.get("ttl", 0))
    total_bytes = sum(m.get("bytes", 0) for m in index.values())
    sys.stdout.write(f"cache dir: {base}\n")
    sys.stdout.write(f"entries: {len(index)} ({live} live)\n")
    sys.stdout.write(f"bytes: {total_bytes}\n")
    if args.json:
        sys.stdout.write(json.dumps(
            {"dir": base, "entries": len(index), "live": live,
             "bytes": total_bytes}, indent=2) + "\n")
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(
        prog="memoize",
        description="TTL command-result memoization for agent shells. "
                    "Cache hits are always announced on stderr; only "
                    "explicit, bounded TTLs; content-keyed on inputs.")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a command with memoization")
    r.add_argument("--ttl", type=int, default=None,
                   help="cache TTL in seconds, required, 1..86400")
    r.add_argument("--inputs", action="append", default=[],
                   help="glob limiting tracked input files "
                        "(repeatable; default: whole cwd tree)")
    r.add_argument("--exclude", action="append", default=[],
                   help="glob excluding files from input tracking "
                        "(repeatable)")
    r.add_argument("--env", action="append", default=[],
                   help="env var folded into the key: NAME or NAME=VALUE "
                        "(repeatable)")
    r.add_argument("--max-output", type=int, default=DEFAULT_MAX_OUTPUT,
                   help="max cached stdout+stderr bytes "
                        f"(default {DEFAULT_MAX_OUTPUT})")
    r.add_argument("--check-deterministic", action="store_true",
                   help="on a miss, run twice; cache only if outputs match")
    r.add_argument("--force-mutable", action="store_true",
                   help="allow caching commands on the mutable deny-list")
    r.add_argument("--cwd",
                   help="working directory (default: current directory)")
    r.add_argument("--quiet", action="store_true",
                   help="suppress HIT/MISS markers on stderr")
    r.add_argument("--verbose", action="store_true",
                   help="explain misses and refusals on stderr")
    r.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="command argv after `--`")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("invalidate",
                       help="drop cached entries for a command (or all)")
    i.add_argument("--all", action="store_true",
                   help="drop every entry")
    i.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="command argv prefix after `--`")
    i.set_defaults(func=cmd_invalidate)

    b = sub.add_parser("bust", help="drop every cached entry")
    b.set_defaults(func=cmd_bust)

    s = sub.add_parser("status", help="cache statistics")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    # argparse.REMAINDER keeps a leading `--`; drop it.
    if getattr(args, "cmd", None) and args.cmd[:1] == ["--"]:
        args.cmd = args.cmd[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
