"""Machine-local checks on every push to a public destination.

The pre-push hook calls check_push() for every push. A destination counts as
public exactly as it does for the leak check (leak_guard): anything that is
not a path on this machine or a GitHub repo confirmed private. Two optional
checks, both configured in the GLOBAL git config only, so nothing about them
lives in, or can be switched off by, the repo being pushed:

  ci-policy.forbiddenIdentity <text>   (repeatable)
      No pushed commit may carry <text> (case-insensitive substring) in its
      author name, author email, committer name or committer email, and no
      pushed annotated tag may carry it anywhere (tagger line or message).
      GitHub shows all of these, and no content scan sees them.

  ci-policy.publicPushScanner <absolute path>   (repeatable)
      An extra scanner run once per pushed ref as
          <path> --push <repo top level> <rev-list words...>
      where the words name exactly the commits that ref brings (the same
      range the leak check reads, for example "<old>..<new>", or
      "<new> --not --remotes=origin"). Exit 0 passes; anything else refuses
      the push. A configured scanner that is missing, not executable, not an
      absolute path, or that times out refuses the push too (fail closed).

  ci-policy.publicPushScannerSkip <owner/name glob>   (repeatable)
      Destinations the extra scanner is not run for, matched without regard
      to case, for example "someone-else/*" for an upstream whose content is
      not this machine's to police. The scanner is skipped only when EVERY
      destination still counted as public is a GitHub repo matching one of
      these. The identity check still runs there.

Neither check is set by default; with neither set this module does nothing. Humans
can break glass for one push with CI_POLICY_ALLOW_LEAK=1 (the same switch as
the leak check).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

from ci_policy import leak_guard
from ci_policy.allowlist import redact

ZERO = leak_guard.ZERO
SCANNER_KEY = "ci-policy.publicPushScanner"
IDENTITY_KEY = "ci-policy.forbiddenIdentity"
SKIP_KEY = "ci-policy.publicPushScannerSkip"
SCANNER_TIMEOUT = 900
MAX_HITS = 20
FIELDS = ("author name", "author email", "committer name", "committer email")


class ConfigError(Exception):
    """The global git config could not be read: the checks must not turn off."""


def _global_all(key: str, path: bool = False) -> List[str]:
    args = ["git", "config", "--global"] + (["--type=path"] if path else []) + ["--get-all", key]
    p = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if p.returncode == 1:
        return []                                   # simply not set
    if p.returncode != 0:
        raise ConfigError(f"git config --global --get-all {key} failed: "
                          + (p.stderr.strip()[:200] or f"exit {p.returncode}"))
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def scanners() -> List[str]:
    return _global_all(SCANNER_KEY, path=True)


def identities() -> List[str]:
    return _global_all(IDENTITY_KEY)


def scanner_skipped(still_open: Sequence[Tuple[Optional[str], bool]]) -> bool:
    """Every public destination is a GitHub repo on the skip list."""
    globs = [g.lower() for g in _global_all(SKIP_KEY)]
    return bool(globs) and bool(still_open) and all(
        is_github and repo and any(fnmatch.fnmatchcase(repo.lower(), g) for g in globs)
        for repo, is_github in still_open)


def repo_root(cwd: Optional[str]) -> str:
    """The work tree top level, or the git dir of a bare repo."""
    for args in (["rev-parse", "--show-toplevel"], ["rev-parse", "--absolute-git-dir"]):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    raise RuntimeError("cannot find the repository this push comes from")


def commit_identities(words: Sequence[str], cwd: Optional[str]
                      ) -> List[Tuple[str, str, str]]:
    """(sha, field, value) for the four identity fields of every commit the
    rev-list words name. Raw values (%an, not %aN): .mailmap does not change
    what GitHub shows. A git failure raises."""
    out = leak_guard._git_out(["-c", "log.showSignature=false", "log", "--no-color",
                               "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", *words, "--"], cwd)
    found = []
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) != 5:
            raise RuntimeError(f"cannot read the identity of a commit: {line[:80]!r}")
        sha, *values = parts
        found.extend((sha, f, v) for f, v in zip(FIELDS, values))
    return found


def _matches(value: str, forbidden: Sequence[str]) -> Optional[str]:
    low = value.lower()
    return next((f for f in forbidden if f.lower() in low), None)


def run_scanner(scanner: str, root: str, words: Sequence[str]) -> Optional[str]:
    """Run one configured scanner. Returns why it refused, or None."""
    if not os.path.isabs(scanner):
        return (f"{SCANNER_KEY} is {scanner!r}, which is not an absolute path (a relative "
                "path would run a file from the repo being pushed)")
    if not os.path.isfile(scanner) or not os.access(scanner, os.X_OK):
        return f"{SCANNER_KEY} {scanner} is missing or not executable"
    try:
        p = subprocess.run([scanner, "--push", root, *words], cwd=root,
                           stdin=subprocess.DEVNULL, stdout=sys.stderr.fileno(),
                           timeout=SCANNER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"{scanner} did not finish in {SCANNER_TIMEOUT} s"
    except OSError as e:
        return f"{scanner} could not run: {e}"
    if p.returncode != 0:
        return f"{scanner} refused this push (exit {p.returncode}; its output is above)"
    return None


def check_push(targets: Sequence[Tuple[Optional[str], bool]], remote: str,
               updates: Sequence[Tuple[str, str, str, str]],
               cwd: Optional[str] = None, trust_tracking: bool = False,
               dest: str = "") -> Tuple[bool, List[str]]:
    """Same arguments as leak_guard.check_push. Returns (ok, lines)."""
    if not targets:
        return True, []                            # only paths on this machine
    try:
        scans = scanners()
        forbidden = identities()
        if not scans and not forbidden:
            return True, []
        still_open = leak_guard.open_targets(targets, leak_guard.load_visibility())
        if not still_open:
            return True, []                        # every destination is private
        if scans and scanner_skipped(still_open):
            print(f"ci-policy pre-push: {SCANNER_KEY} skipped: every destination is on "
                  f"{SKIP_KEY}", file=sys.stderr)
            scans = []
    except ConfigError as e:
        return False, [f"cannot tell which public push checks are on: {e}"]
    if not scans and not forbidden:
        return True, []
    where = ", ".join(r for r, _ in targets if r) or redact(remote) or "this remote"
    found: List[str] = []
    root = repo_root(cwd) if scans else ""
    for _local_ref, local_sha, remote_ref, remote_sha in updates:
        if not local_sha or local_sha == ZERO:
            continue                               # a deletion adds nothing
        words = leak_guard.range_words(local_sha, remote_sha, remote, cwd, trust_tracking, dest)
        if forbidden:
            for sha, field, value in commit_identities(words, cwd):
                hit = _matches(value, forbidden)
                if hit:
                    found.append(f"commit {sha[:9]} {field} {value!r} matches "
                                 f"{IDENTITY_KEY} {hit!r}")
            for text in leak_guard.tag_texts(local_sha, cwd):
                hit = _matches(text, forbidden)
                if hit:
                    found.append(f"tag {remote_ref!r} (tagger or message) matches "
                                 f"{IDENTITY_KEY} {hit!r}")
        for scanner in scans:
            why = run_scanner(scanner, root, words)
            if why:
                found.append(f"{remote_ref}: {why}")
    if not found:
        return True, []
    more = [f"  ... and {len(found) - MAX_HITS} more"] if len(found) > MAX_HITS else []
    return False, ([f"{where} is public (or not known to be private), and this push fails "
                    "this machine's public push checks:"]
                   + [f"  {f}" for f in found[:MAX_HITS]] + more)
