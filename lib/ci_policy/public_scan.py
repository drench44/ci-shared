"""Machine-local checks on every push to a public destination.

The pre-push hook calls check_push() for every push. A destination counts as
public exactly as it does for the leak check (leak_guard): anything that is
not a path on this machine or a GitHub repo confirmed private. Two optional
checks, both configured in the GLOBAL git config only, so nothing about them
lives in, or can be switched off by, the repo being pushed:

  ci-policy.forbiddenIdentity <text>   (repeatable)
      No pushed commit may carry <text> (case-insensitive substring) in its
      author name, author email, committer name, committer email or message
      (trailers such as Co-authored-by included), and no pushed annotated tag
      may carry it anywhere (tagger line or message). GitHub shows all of
      these, and no content scan sees the identities.

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

Once either is configured, its values are also kept in
~/.config/ci-policy/public-checks.txt (mode 600), the way the leak check keeps
owners.txt, and the checks use the union of the two: unsetting the git config
alone (a fresh dotfile, a stray --unset) never turns them off silently.
Turning one off means removing it from both. A blank publicPushScanner value
or an unreadable kept file refuses the push.

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

SCANNER_KEY = "ci-policy.publicPushScanner"
IDENTITY_KEY = "ci-policy.forbiddenIdentity"
SKIP_KEY = "ci-policy.publicPushScannerSkip"
SCANNER_TIMEOUT = 900


def scanner_timeout() -> int:
    """SCANNER_TIMEOUT seconds; CI_POLICY_SCANNER_TIMEOUT may only lower it
    (tests), never raise it, so an env var cannot make a hung scanner wait
    longer than the documented limit."""
    try:
        want = int(os.environ.get("CI_POLICY_SCANNER_TIMEOUT", SCANNER_TIMEOUT))
    except ValueError:
        return SCANNER_TIMEOUT
    return max(1, min(want, SCANNER_TIMEOUT))


def is_zero(sha: str) -> bool:
    """git's "no object" id: 40 zeros, or 64 in a SHA-256 repo."""
    return bool(sha) and set(sha) == {"0"}
MAX_HITS = 20
FIELDS = ("author name", "author email", "committer name", "committer email")


class ConfigError(Exception):
    """The global git config could not be read: the checks must not turn off."""


def _global_all(key: str, path: bool = False, blank_ok: bool = True) -> List[str]:
    args = ["git", "config", "--global"] + (["--type=path"] if path else []) + ["--get-all", key]
    p = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if p.returncode == 1:
        return []                                   # simply not set
    if p.returncode != 0:
        raise ConfigError(f"git config --global --get-all {key} failed: "
                          + (p.stderr.strip()[:200] or f"exit {p.returncode}"))
    lines = p.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not blank_ok and any(not l.strip() for l in lines):
        raise ConfigError(f"{key} has an empty value; set a path or remove it")
    return [l.strip() for l in lines if l.strip()]


def kept_path() -> str:
    """The kept copy of the configured checks, like the leak check's
    owners.txt: unsetting the git config alone does not turn them off."""
    return os.path.join(leak_guard.config_dir(), "public-checks.txt")


def _read_kept() -> Tuple[List[str], List[str]]:
    path = kept_path()
    if not os.path.lexists(path):
        return [], []
    scans: List[str] = []
    ids: List[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                kind, _, value = line.partition(" ")
                if kind == "scanner" and value:
                    scans.append(value)
                elif kind == "identity" and value:
                    ids.append(value)
                else:
                    raise ConfigError(f"{path}: cannot read the line {line[:80]!r}")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    return scans, ids


def _union(a: Sequence[str], b: Sequence[str], fold: bool) -> List[str]:
    out: List[str] = []
    seen = set()
    for v in [*a, *b]:
        k = v.lower() if fold else v
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def configured() -> Tuple[List[str], List[str]]:
    """(scanners, forbidden identities): the global git config plus the kept
    copy, and the kept copy is brought up to date with the config. Raises
    ConfigError when either cannot be read."""
    cfg_scans = _global_all(SCANNER_KEY, path=True, blank_ok=False)
    cfg_ids = _global_all(IDENTITY_KEY)
    kept_scans, kept_ids = _read_kept()
    scans = _union(cfg_scans, kept_scans, fold=False)
    ids = _union(cfg_ids, kept_ids, fold=True)
    if (scans, ids) != (kept_scans, kept_ids) and (scans or ids):
        try:
            leak_guard._write_private(kept_path(), "# ci-policy public push checks; delete this "
                                      "file AND the git config to turn them off\n"
                                      + "".join(f"scanner {s}\n" for s in scans)
                                      + "".join(f"identity {i}\n" for i in ids))
        except OSError as e:
            raise ConfigError(f"cannot write {kept_path()}: {e}") from None
    return scans, ids


def scanners() -> List[str]:
    return configured()[0]


def identities() -> List[str]:
    return configured()[1]


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
    """(sha, field, value) for the four identity fields and the full message
    of every commit the rev-list words name (a Co-authored-by or
    Signed-off-by trailer is published too). Raw values (%an, not %aN):
    .mailmap does not change what GitHub shows. A git failure raises."""
    out = leak_guard._git_out(["-c", "log.showSignature=false", "log", "--no-color",
                               "--format=%x01%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
                               *words, "--"], cwd)
    found = []
    for rec in out.split("\x01"):
        if not rec.strip("\n"):
            continue
        parts = rec.split("\0", 5)
        if len(parts) != 6:
            raise RuntimeError(f"cannot read the identity of a commit: {rec[:80]!r}")
        sha, *values = parts
        found.extend((sha, f, v.rstrip("\n")) for f, v in zip(FIELDS + ("message",), values))
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
                           timeout=scanner_timeout())
    except subprocess.TimeoutExpired:
        return f"{scanner} did not finish in {scanner_timeout()} s"
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
        scans, forbidden = configured()
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
        if not local_sha or is_zero(local_sha):
            continue                               # a deletion adds nothing
        words = leak_guard.range_words(local_sha, remote_sha, remote, cwd, trust_tracking, dest)
        if forbidden:
            for sha, field, value in commit_identities(words, cwd):
                hit = _matches(value, forbidden)
                if hit and field == "message":
                    line = next(l for l in value.split("\n") if hit.lower() in l.lower())
                    found.append(f"commit {sha[:9]} message line {line[:120]!r} matches "
                                 f"{IDENTITY_KEY} {hit!r}")
                elif hit:
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
