"""Stop a push to a public repo that names a private repo.

The pre-push hook calls check_push() for every pushed ref. Unless the remote
is a path on this machine, or a GitHub repo the visibility cache knows is
private, the push is scanned for the names of the owner's private repos: the
new commits' messages, their added lines (read as text, whatever
.gitattributes says), the paths of the files they add or change, the pushed
ref name, and the message of a pushed annotated tag. A hit blocks the push.
A remote that is not recognized as GitHub (another host, an ssh host alias
that does not resolve to github.com) counts as public.

Nothing private is ever stored in a repo. The lists live on the machine only,
in ~/.config/ci-policy (mode 600), written by `ci-policy refresh-private`
(also run by the installer, and by the hook itself once a day when gh works):

  private-names.txt   one private repo name per line
  visibility.json     {"owner/name": "PUBLIC" | "PRIVATE"} for every repo of
                      the configured owners
  owners.txt          the configured owners, one per line

Owners are `git config --global --get-all ci-policy.privateOwner` plus
owners.txt, so unsetting the git config alone does not turn the check off
(delete owners.txt too, or uninstall).
A name that is also an ordinary word (say a repo called "shed" next to a
dashboard that shows the shed) can be marked loose with
`git config --global --add ci-policy.leakLoose <name>`: it then counts only as
owner/name, "<name> repo", "<name> PR", "<name> #12" or "<name>#12".

Off until an owner is configured. Then it fails closed: a scanned push is
refused while the name list is missing or empty, when a refresh finds no
private repo at all, or when the list is more than 7 days old and cannot be
refreshed, until `ci-policy refresh-private` succeeds. Humans can break glass
for one push with CI_POLICY_ALLOW_LEAK=1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ci_policy.allowlist import redact

ZERO = "0" * 40
REFRESH_AFTER = 24 * 3600
# Older than this and not refreshable: the lists are not trusted at all.
STALE_AFTER = 7 * 24 * 3600
GH_TIMEOUT = 15
MAX_HITS = 20
# gh repo list --limit: a result this long may be cut short, so it is refused.
LIST_LIMIT = 1000
# The "known private, skip the scan" pass trusts a visibility list at most
# this old; otherwise it asks GitHub live (a repo just made public).
PRIVATE_FRESH = 10 * 60
VISIBILITIES = {"PUBLIC", "PRIVATE", "INTERNAL"}
TAG_DEPTH = 10


class CacheError(Exception):
    """A machine-local list exists but cannot be read: refuse, never pass."""


def config_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "ci-policy")


def _names_path() -> str:
    return os.path.join(config_dir(), "private-names.txt")


def _vis_path() -> str:
    return os.path.join(config_dir(), "visibility.json")


def _owners_path() -> str:
    return os.path.join(config_dir(), "owners.txt")


def _git_lines(args: Sequence[str], cwd: Optional[str] = None) -> List[str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       errors="replace")
    return [l.strip() for l in p.stdout.splitlines() if l.strip()] if p.returncode == 0 else []


class OwnersError(Exception):
    """The configured owners could not be read: the check must not turn off."""


def _config_owners() -> List[str]:
    p = subprocess.run(["git", "config", "--global", "--get-all", "ci-policy.privateOwner"],
                       capture_output=True, text=True, errors="replace")
    if p.returncode == 1:
        return []                                   # simply not set
    if p.returncode != 0:
        raise OwnersError("git config --global --get-all ci-policy.privateOwner failed: "
                          + (p.stderr.strip()[:200] or f"exit {p.returncode}"))
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def _kept_owners() -> List[str]:
    path = _owners_path()
    if not os.path.lexists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except (OSError, UnicodeDecodeError) as e:
        raise OwnersError(f"cannot read {path}: {e}") from None


def owners() -> List[str]:
    """The git config owners plus the kept copy in owners.txt (deduplicated).

    Raises OwnersError when either cannot be read: once configured, the check
    is never silently off.
    """
    out: List[str] = []
    for o in _config_owners() + _kept_owners():
        if o.lower() not in {x.lower() for x in out}:
            out.append(o)
    return out


def loose_names() -> Set[str]:
    return {n.lower() for n in _git_lines(["config", "--global", "--get-all", "ci-policy.leakLoose"])}


def load_names() -> Optional[List[str]]:
    """The private name list, or None when there is none. Raises CacheError
    when it exists but cannot be read (permissions, bad encoding)."""
    path = _names_path()
    if not os.path.lexists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise CacheError(f"cannot read {path}: {e}") from None


def load_visibility() -> Dict[str, str]:
    """owner/name (lowercase) -> visibility, exactly as refresh wrote it.
    Anything unreadable or odd (a non-object, a value not spelled the way
    refresh writes it) counts for nothing, so the push is scanned."""
    try:
        with open(_vis_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k.lower(): v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v in VISIBILITIES}


def _vis_age() -> Optional[float]:
    try:
        return max(0.0, time.time() - os.path.getmtime(_vis_path()))
    except OSError:
        return None


def confirmed_private(repo: str, vis: Dict[str, str]) -> bool:
    """Is ``repo`` private right now? A visibility list under 10 minutes old
    may say so; otherwise GitHub is asked (`gh api repos/O/R`). Any error or
    doubt is "no", so the push is scanned."""
    age = _vis_age()
    if age is not None and age <= PRIVATE_FRESH and vis.get(repo.lower()) == "PRIVATE":
        return True
    if not shutil.which("gh"):
        return False
    try:
        p = subprocess.run(["gh", "api", f"repos/{repo}", "--jq", ".private"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and p.stdout.strip() == "true"


def _write_private(path: str, text: str) -> None:
    """Write via a new file created mode 600 (mkstemp, O_EXCL) in the same
    directory, then rename it into place: no moment where another user can
    read it, and no reuse of a leftover file with looser permissions."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh() -> Tuple[bool, str]:
    """Rewrite both caches from GitHub. Returns (ok, message)."""
    try:
        who = owners()
    except OwnersError as e:
        return False, str(e)
    if not who:
        return False, "no owners configured (git config --global --add ci-policy.privateOwner <owner>)"
    if not shutil.which("gh"):
        return False, "gh is not installed"
    names: Set[str] = set()
    vis: Dict[str, str] = {}
    old_vis = load_visibility()
    try:
        old_names = load_names() or []
    except CacheError:
        old_names = []
    for owner in who:
        try:
            p = subprocess.run(["gh", "repo", "list", owner, "--limit", str(LIST_LIMIT),
                                "--json", "name,visibility"],
                               capture_output=True, text=True, timeout=GH_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"gh repo list {owner} failed: {e}"
        if p.returncode != 0:
            return False, f"gh repo list {owner} failed: {p.stderr.strip()[:200]}"
        try:
            repos = json.loads(p.stdout or "[]")
        except ValueError:
            return False, f"gh repo list {owner}: unreadable output"
        if not isinstance(repos, list):
            return False, f"gh repo list {owner}: expected a list"
        if len(repos) >= LIST_LIMIT:
            return False, (f"gh repo list {owner} returned {len(repos)} repos, the limit; "
                           "the list may be cut short. The old list was kept")
        mine = 0
        for r in repos:
            name = r.get("name") if isinstance(r, dict) else None
            v = r.get("visibility") if isinstance(r, dict) else None
            if not isinstance(name, str) or not name.strip() or "/" in name \
                    or not isinstance(v, str) or v.upper() not in VISIBILITIES:
                return False, (f"gh repo list {owner}: an entry without a usable name and "
                               "visibility; the old list was kept")
            v = v.upper()
            vis[f"{owner}/{name}".lower()] = v
            if v != "PUBLIC":
                names.add(name)
                mine += 1
        had = [k for k, v in old_vis.items() if k.split("/")[0] == owner.lower() and v != "PUBLIC"]
        if had and not mine:
            return False, (f"gh repo list {owner} found no private repos, but {len(had)} were "
                           "private last time; is gh logged in as the right account? The old "
                           "list was kept")
    # Keep the owners even when nothing private turned up, so the check stays on.
    _write_private(_owners_path(), "".join(f"{o}\n" for o in who))
    if not names:
        # A gh that answers for the wrong account (or an empty list) must not
        # turn the check into a silent pass.
        return False, (f"found no private repos for {', '.join(who)}; is gh logged in as an "
                       "account that can see them? The old list was kept")
    # A name that was private and is gone now (deleted, renamed, or made
    # public) stays on the list: the list never shrinks quietly.
    gone = sorted({n for n in old_names if n.lower() not in {x.lower() for x in names}},
                  key=str.lower)
    if gone:
        print(f"ci-policy refresh-private: WARNING: {len(gone)} name(s) that were private are "
              "no longer listed as private; they stay on the list. Edit "
              f"{_names_path()} by hand to drop one.", file=sys.stderr)
    names |= set(gone)
    _write_private(_names_path(), "".join(f"{n}\n" for n in sorted(names, key=str.lower)))
    _write_private(_vis_path(), json.dumps(vis, indent=1, sort_keys=True) + "\n")
    return True, f"{len(names)} private names, {len(vis)} repos"


def cache_age() -> Optional[float]:
    """Seconds since the name list was written, or None when there is none."""
    try:
        return max(0.0, time.time() - os.path.getmtime(_names_path()))
    except OSError:
        return None


def maybe_refresh() -> Optional[str]:
    """Refresh a list older than a day. Returns why a refresh failed, or None."""
    age = cache_age()
    if age is not None and age <= REFRESH_AFTER:
        return None
    try:
        ok, msg = refresh()
    except OSError as e:
        return str(e)
    return None if ok else msg


def _pattern(name: str, loose: bool) -> re.Pattern:
    # A name counts when no letter or digit touches it, so it is caught inside
    # "port-NAME", "NAME-style" or "x_NAME" but not inside a longer word.
    n = re.escape(name)
    edge_l, edge_r = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
    if loose:
        return re.compile(r"(?i)(?:[A-Za-z0-9_.-]+/" + n + edge_r +
                          r"|" + edge_l + n + r"(?:\s+(?:repo|repository|PR|pull)\b|\s*#\d))")
    return re.compile(r"(?i)" + edge_l + n + edge_r)


def find(text: str, names: Iterable[str], loose: Set[str], skip: Set[str]) -> List[str]:
    hits = []
    for n in names:
        if n.lower() in skip:
            continue
        if _pattern(n, n.lower() in loose).search(text):
            hits.append(n)
    return hits


def _have_commit(sha: str, cwd: Optional[str]) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd,
                          capture_output=True).returncode == 0


def _dest_tips(dest: str, cwd: Optional[str]) -> List[str]:
    """Commits the push destination already has (its branch and tag tips that
    this clone also has), read live; [] when it cannot be read."""
    try:
        p = subprocess.run(["git", "ls-remote", "--heads", "--tags", dest], cwd=cwd,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    shas = {line.split()[0] for line in p.stdout.splitlines() if line.split()}
    return [s for s in shas if _have_commit(s, cwd)]


def _new_commits(local_sha: str, remote_sha: str, remote: str, cwd: Optional[str],
                 trust_tracking: bool = False, dest: str = "") -> List[str]:
    """Commits this ref update brings to the destination.

    A new ref (or a remote tip this clone lacks) leaves out commits already
    there. The remote-tracking refs say what is there only when the push goes
    where they were fetched from (``trust_tracking``); otherwise (a private
    fetch URL, a public push URL) the destination is asked live, and if it
    cannot be, the whole history is scanned.
    """
    if remote_sha and remote_sha != ZERO and _have_commit(remote_sha, cwd):
        return _git_out(["rev-list", f"{remote_sha}..{local_sha}"], cwd).split()
    if trust_tracking and remote:
        excl = ["--not", f"--remotes={remote}"]
    else:
        tips = _dest_tips(dest, cwd) if dest else []
        excl = ["--not", *tips] if tips else []
    return _git_out(["rev-list", local_sha, *excl], cwd).split()


def _git_out(args: Sequence[str], cwd: Optional[str]) -> str:
    """git output; a failure raises (an unreadable commit must not scan as clean)."""
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=cwd,
                       capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:200]}")
    return p.stdout


def _commit_text(sha: str, cwd: Optional[str]) -> str:
    """The commit message, its added lines, and the paths it adds or changes.

    The message and the diff are read separately, so a message line that
    looks like a diff header cannot hide the rest of the message. --text and
    --no-textconv: a `-diff` or `binary` attribute, or a textconv filter, must
    not hide what is really in a file. A merge is diffed against its first
    parent, so a line only the merge itself adds (an evil merge) is seen.
    """
    out = [_git_out(["log", "-1", "--no-color", "--format=%B", sha], cwd)]
    show = _git_out(["show", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
                     "--format=", "-p", "--unified=0", "--diff-merges=first-parent", sha], cwd)
    in_hunk = False
    for line in show.splitlines():
        if line.startswith("diff --git ") or line.startswith("diff --cc "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            out.append(line[1:])                   # added lines only
    paths = _git_out(["diff-tree", "-r", "--root", "-m", "--no-commit-id", "--name-only",
                      "-z", "--no-renames", "--diff-filter=d", sha], cwd)
    out.extend(p for p in dict.fromkeys(paths.split("\0")) if p)
    return "\n".join(out)


def _tag_texts(sha: str, cwd: Optional[str]) -> List[str]:
    """The text of an annotated tag (and any tag it points at), or []."""
    texts = []
    for _ in range(TAG_DEPTH):
        kind = _git_out(["cat-file", "-t", sha], cwd).strip()   # raises on failure
        if kind != "tag":
            return texts
        body = _git_out(["cat-file", "-p", sha], cwd)
        texts.append(body)
        m = re.match(r"object ([0-9a-f]{40,64})", body)
        if not m:
            raise RuntimeError(f"tag object {sha[:9]} has no object line")
        sha = m.group(1)
    raise RuntimeError(f"tag {sha[:9]} is nested more than {TAG_DEPTH} deep")


def _warn(text: str) -> None:
    print(f"ci-policy pre-push: WARNING: {text}", file=sys.stderr)


def check_push(targets: Sequence[Tuple[Optional[str], bool]], remote: str,
               updates: Sequence[Tuple[str, str, str, str]],
               cwd: Optional[str] = None, trust_tracking: bool = False,
               dest: str = "") -> Tuple[bool, List[str]]:
    """updates: (local_ref, local_sha, remote_ref, remote_sha). Returns (ok, lines).

    ``targets`` has one (owner/name or None, provably reaches GitHub) per
    push URL that is not a path on this machine; none means the push stays
    on this machine (never scanned). The push is scanned unless EVERY target
    provably reaches a GitHub repo confirmed private (a visibility list under
    10 minutes old from a refresh that worked, or GitHub asked live). Anything
    else counts as public. ``trust_tracking`` and ``dest`` say how to tell
    which commits the destination already has (see _new_commits).
    """
    if not targets:
        return True, []                            # only paths on this machine
    try:
        who = owners()
    except OwnersError as e:
        return False, [f"cannot tell whether the private repo check is on: {e}"]
    if not who:
        return True, []                            # not configured on this machine
    repos = [r for r, _ in targets if r]
    where = ", ".join(repos) or redact(remote) or "this remote"
    why_stale = maybe_refresh()
    age = cache_age()
    if age is not None and age > STALE_AFTER:
        return False, [f"cannot check this push to {where} for private repo names: the list at "
                       f"{_names_path()} is {int(age // 86400)} days old and could not be "
                       f"refreshed ({why_stale or 'unknown error'}). Run: ci-policy "
                       "refresh-private"]
    if why_stale:
        _warn(f"could not refresh the private repo list ({why_stale}); checking with the "
              f"list from {int((age or 0) // 3600)} h ago")
    vis = {} if why_stale else load_visibility()   # a failed refresh: trust no PRIVATE
    # Leave out destinations proven private; scan for the rest.
    open_targets = [(r, gh) for r, gh in targets
                    if not (gh and r and confirmed_private(r, vis))]
    if not open_targets:
        return True, []
    try:
        names = load_names()
    except CacheError as e:
        return False, [f"cannot check this push to {where} for private repo names: {e}. "
                       "Run: ci-policy refresh-private"]
    if not names:
        what = "no list at" if names is None else "an empty list at"
        return False, [f"cannot check this push to {where} for private repo names: {what} "
                       f"{_names_path()}" + (f" ({why_stale})" if why_stale else "")
                       + ". Run: ci-policy refresh-private"]
    # A repo may name itself, but only when EVERY destination still scanned
    # is that one GitHub repo of a configured owner (another host's
    # "acme/secret-proj" is not the private one, and a second public
    # destination must not receive the name).
    mine = {o.lower() for o in who}
    own = {r.lower() for r, gh in open_targets if gh and r}
    skip: Set[str] = set()
    if len(own) == 1 and all(gh and r for r, gh in open_targets):
        only = next(iter(own))
        if only.split("/")[0] in mine:
            skip = {only.split("/")[-1]}
    loose = loose_names()
    found: List[str] = []
    for local_ref, local_sha, remote_ref, remote_sha in updates:
        if not local_sha or local_sha == ZERO:
            continue                               # a deletion adds nothing
        if remote_ref.startswith("refs/heads/"):
            label, ref_name = "branch name", remote_ref[len("refs/heads/"):]
        else:
            label, ref_name = "ref name", remote_ref
        for n in find(ref_name, names, loose, skip):
            found.append(f"{label} {ref_name!r} names {n}")
        for text in _tag_texts(local_sha, cwd):
            for n in find(text, names, loose, skip):
                found.append(f"tag {ref_name!r} message names {n}")
        for sha in _new_commits(local_sha, remote_sha, remote, cwd, trust_tracking, dest):
            for n in find(_commit_text(sha, cwd), names, loose, skip):
                found.append(f"commit {sha[:9]} names {n}")
            if len(found) >= MAX_HITS:
                break
    if not found:
        return True, []
    return False, [f"{where} is public (or not known to be private), and this push names "
                   f"private repos:"] + [f"  {f}" for f in found[:MAX_HITS]]
