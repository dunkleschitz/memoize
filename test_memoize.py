#!/usr/bin/env python3
"""Tests for memoize. Run: python3 -m unittest test_memoize -v"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MEMO = os.path.join(HERE, "memoize.py")
PY = sys.executable


def run(cachedir, workdir, *argv, stdin=None, env_extra=None):
    env = dict(os.environ, MEMOIZE_DIR=cachedir)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY, MEMO, *argv], input=stdin,
                       capture_output=True, cwd=workdir, env=env)
    return p


class MemoizeTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.work = tempfile.TemporaryDirectory()
        self.cdir = self.cache.name
        self.wdir = self.work.name

    def tearDown(self):
        self.cache.cleanup()
        self.work.cleanup()

    def memo(self, *argv, stdin=None, env_extra=None):
        return run(self.cdir, self.wdir, *argv, stdin=stdin,
                   env_extra=env_extra)

    def write(self, rel, content):
        full = os.path.join(self.wdir, rel)
        os.makedirs(os.path.dirname(full) or self.wdir, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    # --- core hit/miss behavior ---

    def test_miss_then_hit(self):
        r1 = self.memo("run", "--ttl", "300", "--", "echo", "hello")
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r1.stdout, b"hello\n")
        self.assertNotIn(b"HIT", r1.stderr)
        r2 = self.memo("run", "--ttl", "300", "--", "echo", "hello")
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(r2.stdout, b"hello\n")
        self.assertIn(b"[memo HIT", r2.stderr)
        self.assertIn(b"ttl=300s", r2.stderr)

    def test_hit_marker_announces_age(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        time.sleep(1.1)
        r = self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.assertIn(b"age=1s", r.stderr)

    def test_ttl_expiry(self):
        self.memo("run", "--ttl", "1", "--", "echo", "x")
        r = self.memo("run", "--ttl", "1", "--", "echo", "x")
        self.assertIn(b"HIT", r.stderr)
        time.sleep(1.2)
        r = self.memo("run", "--ttl", "1", "--", "echo", "x")
        self.assertNotIn(b"HIT", r.stderr)

    def test_argv_change_is_miss(self):
        self.memo("run", "--ttl", "300", "--", "echo", "one")
        r = self.memo("run", "--ttl", "300", "--", "echo", "two")
        self.assertNotIn(b"HIT", r.stderr)
        self.assertEqual(r.stdout, b"two\n")

    def test_cwd_change_is_miss(self):
        self.memo("run", "--ttl", "300", "--", "pwd")
        other = tempfile.TemporaryDirectory()
        try:
            r = run(self.cdir, other.name, "run", "--ttl", "300",
                    "--", "pwd")
            self.assertNotIn(b"HIT", r.stderr)
        finally:
            other.cleanup()

    def test_quiet_suppresses_marker(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        r = self.memo("run", "--ttl", "300", "--quiet", "--", "echo", "x")
        self.assertEqual(r.stdout, b"x\n")
        self.assertNotIn(b"HIT", r.stderr)

    def test_exit_code_propagated(self):
        r = self.memo("run", "--ttl", "300", "--", "sh", "-c", "exit 3")
        self.assertEqual(r.returncode, 3)

    def test_nonzero_exit_never_cached(self):
        argv = ("run", "--ttl", "300", "--", "sh", "-c", "exit 3")
        self.memo(*argv)
        r = self.memo(*argv)
        self.assertNotIn(b"HIT", r.stderr)
        self.assertEqual(r.returncode, 3)

    def test_stderr_captured_and_replayed(self):
        argv = ("run", "--ttl", "300", "--", "sh", "-c",
                "echo out; echo err >&2")
        self.memo(*argv)
        r = self.memo(*argv)
        self.assertEqual(r.stdout, b"out\n")
        self.assertIn(b"err", r.stderr)
        self.assertIn(b"[memo HIT", r.stderr)

    # --- input content tracking ---

    def test_input_change_is_miss(self):
        self.write("data.txt", "v1")
        argv = ("run", "--ttl", "300", "--", "cat", "data.txt")
        r1 = self.memo(*argv)
        self.assertEqual(r1.stdout, b"v1")
        r2 = self.memo(*argv)
        self.assertIn(b"HIT", r2.stderr)
        self.write("data.txt", "v2")
        r3 = self.memo(*argv)
        self.assertNotIn(b"HIT", r3.stderr)
        self.assertEqual(r3.stdout, b"v2")

    def test_mtime_touch_without_content_change_is_hit(self):
        self.write("data.txt", "same")
        argv = ("run", "--ttl", "300", "--", "cat", "data.txt")
        self.memo(*argv)
        full = os.path.join(self.wdir, "data.txt")
        st = os.stat(full)
        os.utime(full, (st.st_atime + 50, st.st_mtime + 50))
        r = self.memo(*argv)
        self.assertIn(b"HIT", r.stderr)

    def test_added_file_is_miss(self):
        argv = ("run", "--ttl", "300", "--", "ls")
        self.memo(*argv)
        r = self.memo(*argv)
        self.assertIn(b"HIT", r.stderr)
        self.write("new.txt", "hello")
        r = self.memo(*argv)
        self.assertNotIn(b"HIT", r.stderr)

    def test_removed_file_is_miss(self):
        self.write("gone.txt", "x")
        argv = ("run", "--ttl", "300", "--", "ls")
        self.memo(*argv)
        os.unlink(os.path.join(self.wdir, "gone.txt"))
        r = self.memo(*argv)
        self.assertNotIn(b"HIT", r.stderr)

    def test_inputs_glob_narrows_tracking(self):
        self.write("tracked.txt", "v1")
        self.write("other.txt", "v1")
        argv = ("run", "--ttl", "300", "--inputs", "tracked.txt",
                "--", "cat", "tracked.txt")
        self.memo(*argv)
        self.write("other.txt", "v2")  # untracked change
        r = self.memo(*argv)
        self.assertIn(b"HIT", r.stderr)
        self.write("tracked.txt", "v2")
        r = self.memo(*argv)
        self.assertNotIn(b"HIT", r.stderr)

    def test_exclude_glob(self):
        self.write("skip.log", "v1")
        argv = ("run", "--ttl", "300", "--exclude", "*.log",
                "--", "echo", "x")
        self.memo(*argv)
        self.write("skip.log", "v2")
        r = self.memo(*argv)
        self.assertIn(b"HIT", r.stderr)

    # --- stdin / env keying ---

    def test_stdin_keyed(self):
        argv = ("run", "--ttl", "300", "--", "cat")
        r1 = self.memo(*argv, stdin=b"aaa")
        self.assertEqual(r1.stdout, b"aaa")
        r2 = self.memo(*argv, stdin=b"aaa")
        self.assertIn(b"HIT", r2.stderr)
        r3 = self.memo(*argv, stdin=b"bbb")
        self.assertNotIn(b"HIT", r3.stderr)
        self.assertEqual(r3.stdout, b"bbb")

    def test_env_keyed(self):
        argv = ("run", "--ttl", "300", "--env", "MODE",
                "--", "sh", "-c", "echo $MODE")
        r1 = self.memo(*argv, env_extra={"MODE": "a"})
        self.assertEqual(r1.stdout, b"a\n")
        r2 = self.memo(*argv, env_extra={"MODE": "a"})
        self.assertIn(b"HIT", r2.stderr)
        r3 = self.memo(*argv, env_extra={"MODE": "b"})
        self.assertNotIn(b"HIT", r3.stderr)
        self.assertEqual(r3.stdout, b"b\n")

    # --- safety refusals ---

    def test_mutable_command_refused(self):
        r = self.memo("run", "--ttl", "300", "--", "rm", "--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"refusing to cache", r.stderr)
        r = self.memo("run", "--ttl", "300", "--", "rm", "--version")
        self.assertNotIn(b"HIT", r.stderr)

    def test_mutable_git_subcommand_refused(self):
        # `git push --help` is harmless but must not be cached
        r = self.memo("run", "--ttl", "300", "--", "git", "push",
                      "--help")
        self.assertIn(b"refusing to cache", r.stderr)
        r = self.memo("run", "--ttl", "300", "--", "git", "push",
                      "--help")
        self.assertNotIn(b"HIT", r.stderr)

    def test_force_mutable_overrides(self):
        r = self.memo("run", "--ttl", "300", "--force-mutable",
                      "--", "rm", "--version")
        self.assertEqual(r.returncode, 0)
        r = self.memo("run", "--ttl", "300", "--force-mutable",
                      "--", "rm", "--version")
        self.assertIn(b"HIT", r.stderr)

    def test_secret_output_not_cached(self):
        argv = ("run", "--ttl", "300", "--", "echo",
                "ghp_faketoken1234567890abcdef")
        r1 = self.memo(*argv)
        self.assertEqual(r1.returncode, 0)
        r2 = self.memo(*argv)
        self.assertNotIn(b"HIT", r2.stderr)
        self.assertIn(b"secret", r2.stderr.lower())

    def test_check_deterministic_caches_stable(self):
        argv = ("run", "--ttl", "300", "--check-deterministic",
                "--", "echo", "stable")
        self.memo(*argv)
        r = self.memo(*argv)
        self.assertIn(b"HIT", r.stderr)

    def test_check_deterministic_refuses_volatile(self):
        argv = ("run", "--ttl", "300", "--check-deterministic",
                "--", "date", "+%N")
        r1 = self.memo(*argv)
        self.assertIn(b"non-deterministic", r1.stderr)
        r2 = self.memo(*argv)
        self.assertNotIn(b"HIT", r2.stderr)

    def test_max_output_refuses_cache(self):
        argv = ("run", "--ttl", "300", "--max-output", "10",
                "--", "echo", "this is longer than ten bytes")
        self.memo(*argv)
        r = self.memo(*argv)
        self.assertNotIn(b"HIT", r.stderr)
        self.assertIn(b"max-output", r.stderr)

    # --- invalidate / bust / status ---

    def test_invalidate_all(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        r = self.memo("invalidate", "--all")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"invalidated 1 entr", r.stdout)
        r = self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.assertNotIn(b"HIT", r.stderr)

    def test_invalidate_prefix(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.memo("run", "--ttl", "300", "--", "echo", "y")
        r = self.memo("invalidate", "--", "echo", "x")
        self.assertIn(b"invalidated 1 entr", r.stdout)
        r = self.memo("run", "--ttl", "300", "--", "echo", "y")
        self.assertIn(b"HIT", r.stderr)
        r = self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.assertNotIn(b"HIT", r.stderr)

    def test_bust(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.memo("bust")
        r = self.memo("run", "--ttl", "300", "--", "echo", "x")
        self.assertNotIn(b"HIT", r.stderr)

    def test_status(self):
        r = self.memo("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"entries: 0", r.stdout)
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        r = self.memo("status")
        self.assertIn(b"entries: 1 (1 live)", r.stdout)

    def test_superseded_entry_pruned(self):
        self.write("d.txt", "v1")
        argv = ("run", "--ttl", "300", "--", "cat", "d.txt")
        self.memo(*argv)
        self.write("d.txt", "v2")
        self.memo(*argv)
        r = self.memo("status")
        self.assertIn(b"entries: 1", r.stdout)

    # --- cache perms ---

    def test_cache_perms(self):
        self.memo("run", "--ttl", "300", "--", "echo", "x")
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(self.cdir).st_mode), 0o700)
        entries = os.path.join(self.cdir, "entries")
        for f in os.listdir(entries):
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(entries, f)).st_mode),
                0o600)

    # --- CLI surface ---

    def test_version(self):
        r = self.memo("--version")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith(b"memoize 1.0.0"))

    def test_help(self):
        r = self.memo("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"memoization", r.stdout)

    def test_run_help(self):
        r = self.memo("run", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"--ttl", r.stdout)

    def test_missing_ttl_is_usage_error(self):
        r = self.memo("run", "--", "echo", "x")
        self.assertEqual(r.returncode, 2)

    def test_missing_command_is_usage_error(self):
        r = self.memo("run", "--ttl", "10", "--")
        self.assertEqual(r.returncode, 2)

    def test_bad_ttl_is_usage_error(self):
        r = self.memo("run", "--ttl", "0", "--", "echo", "x")
        self.assertEqual(r.returncode, 2)
        r = self.memo("run", "--ttl", "99999999", "--", "echo", "x")
        self.assertEqual(r.returncode, 2)

    def test_unknown_command_nonzero(self):
        r = self.memo("frobnicate")
        self.assertNotEqual(r.returncode, 0)

    def test_missing_binary(self):
        r = self.memo("run", "--ttl", "300", "--",
                      "definitely-not-a-real-binary-xyz")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
