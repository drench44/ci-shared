"""Delete local branches whose pull request has merged.

GitHub deletes a merged PR's branch (repo setting "Automatically delete head
branches") and `git fetch --prune` drops the remote-tracking copy, but the
local branch stays forever. This removes it, and only when its work is
provably in the default branch:

  1. its tip is an ancestor of <remote>/<default> (a merge commit, or a
     fast-forward), or
  2. its whole diff since the merge base already landed on the default branch
     as one commit (a squash merge, checked by patch id with `git cherry`), or
  3. GitHub says a PR from this branch merged into the default branch with
     this exact tip (`gh`, when installed). This covers rebase merges, which
     rewrite the commits, and squash merges whose diff was edited on merge.

A branch linked to a remote branch (an upstream, from `push -u`) is only
considered once that upstream is gone, so a branch whose PR is still open is
never touched. A branch with no upstream (pushed to an explicit refs/heads/x refspec, or never
pushed) is considered only when origin has no branch of that name, and needs
stronger proof: its diff landed as one commit (squash), or it is an ancestor of
main AND its reflog shows commits made on it (a branch just created from main
is an ancestor too), or, from `git tidy` only, GitHub confirms a merged PR at
its exact tip. The hook never reports unlinked branches it keeps (they are
ordinary local branches); `git tidy` counts them.

Squash matches are exact: `git cherry` finds candidates, then whitespace-exact
patch ids (`git patch-id --verbatim`) must agree, and a branch with no net
change never matches. The current branch,
any branch checked out in a worktree, main/master and the remote's default
branch are always kept, and nothing runs while a rebase is in progress. When a
check fails or errors, the branch is kept: tidying fails safe.

Each deletion is reported as it happens. A gone branch that is kept is named
with the reason, once per tip in hook mode (remembered in
$GIT_COMMON_DIR/ci-policy-tidy-kept), so a pull does not repeat itself or ask
GitHub again about the same abandoned branch.

Runs from the post-merge hook (so `git pull` tidies) and as `git tidy`
(`ci-policy tidy`). Opt out per repo or globally: git config ci-policy.tidy
false. The installer also sets fetch.prune, so a pull notices deleted branches.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import Callable, List, Optional, Sequence, Set, Tuple

from ci_policy import allowlist

GH_TIMEOUT = 10
HOOK_GH_BUDGET = 20  # seconds of gh calls one post-merge run may spend in total
KEPT_FILE = "ci-policy-tidy-kept"
ALWAYS_KEEP = ("main", "master")

Report = Callable[[str], None]


def _git(args: Sequence[str], cwd: Optional[str]) -> Tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       errors="replace")
    return p.returncode, p.stdout.strip()


def enabled(cwd: Optional[str] = None) -> bool:
    rc, out = _git(["config", "--type=bool", "--get", "ci-policy.tidy"], cwd)
    return not (rc == 0 and out == "false")


def _exists(ref: str, cwd: Optional[str]) -> bool:
    return _git(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd)[0] == 0


def default_ref(remote: str, cwd: Optional[str]) -> Optional[str]:
    """refs/remotes/<remote>/<default>, from <remote>/HEAD, else main, else master."""
    rc, out = _git(["symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD"], cwd)
    if rc == 0 and out and _exists(out, cwd):
        return out
    for name in ALWAYS_KEEP:
        ref = f"refs/remotes/{remote}/{name}"
        if _exists(ref, cwd):
            return ref
    return None


def candidates(cwd: Optional[str]) -> List[Tuple[str, str, str, bool]]:
    """(branch, tip sha, remote, linked) for each local branch that may be
    merged: linked ones whose upstream is gone, and unlinked ones (no upstream)
    when origin exists and has no branch of the same name."""
    fmt = ("%(refname)%00%(objectname)%00%(upstream)%00%(upstream:track)"
           "%00%(upstream:remotename)")
    rc, out = _git(["for-each-ref", f"--format={fmt}", "refs/heads"], cwd)
    if rc != 0:
        return []
    rc, remotes = _git(["remote"], cwd)
    has_origin = rc == 0 and "origin" in remotes.split()
    found = []
    for line in out.splitlines():
        ref, sha, upstream, track, remote = (line.split("\0") + [""] * 5)[:5]
        if not ref.startswith("refs/heads/"):
            continue
        name = ref[len("refs/heads/"):]
        if upstream:
            # remote "." is a branch tracking another local branch: not ours to judge.
            if track == "[gone]" and remote and remote != ".":
                found.append((name, sha, remote, True))
        elif has_origin and not _exists(f"refs/remotes/origin/{name}", cwd):
            found.append((name, sha, "origin", False))
    return found


def checked_out(cwd: Optional[str]) -> Optional[Set[str]]:
    """Branches checked out in any worktree, or None when that cannot be known."""
    rc, out = _git(["worktree", "list", "--porcelain"], cwd)
    if rc != 0:
        return None
    names = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            names.add(line[len("branch refs/heads/"):])
    rc, cur = _git(["symbolic-ref", "-q", "HEAD"], cwd)
    if rc == 0 and cur.startswith("refs/heads/"):
        names.add(cur[len("refs/heads/"):])
    return names


def rebase_in_progress(cwd: Optional[str]) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        rc, path = _git(["rev-parse", "--path-format=absolute", "--git-path", name], cwd)
        if rc != 0:
            return True  # cannot tell: be safe
        if os.path.exists(path):
            return True
    return False


def _verbatim_ids(log_args: Sequence[str], cwd: Optional[str]) -> Optional[List[str]]:
    """Whitespace-exact patch ids (git patch-id --verbatim) of `git log -p`."""
    try:
        log = subprocess.run(["git", "log", "-p", "--no-color", "--no-ext-diff",
                              "--format=commit %H", *log_args],
                             cwd=cwd, capture_output=True)
        if log.returncode != 0:
            return None
        pid = subprocess.run(["git", "patch-id", "--verbatim"], cwd=cwd,
                             input=log.stdout, capture_output=True)
    except OSError:
        return None
    if pid.returncode != 0:
        return None
    return [line.split()[0] for line in pid.stdout.decode("ascii", "replace").splitlines()
            if line.strip()]


def squash_landed(sha: str, base_ref: str, cwd: Optional[str]) -> bool:
    """True when the branch's full diff since the merge base landed on base as
    one commit, byte for byte (whitespace included)."""
    rc, mb = _git(["merge-base", base_ref, sha], cwd)
    if rc != 0 or not mb:
        return False
    rc, tree = _git(["rev-parse", f"{sha}^{{tree}}"], cwd)
    rc2, mb_tree = _git(["rev-parse", f"{mb}^{{tree}}"], cwd)
    if rc != 0 or rc2 != 0 or tree == mb_tree:
        return False  # no net change: nothing to have landed (and empty commits match)
    rc, synthetic = _git(["commit-tree", tree, "-p", mb, "-m", "ci-policy tidy probe"], cwd)
    if rc != 0:
        return False
    # git cherry is the fast filter, but its patch ids ignore whitespace.
    rc, out = _git(["cherry", base_ref, synthetic], cwd)
    if rc != 0 or not out.startswith("-"):
        return False
    mine = _verbatim_ids(["-1", synthetic], cwd)
    landed = _verbatim_ids([f"{mb}..{base_ref}"], cwd)
    return bool(mine) and landed is not None and mine[0] in landed


def made_commits(branch: str, cwd: Optional[str]) -> bool:
    """The branch's reflog shows commits made on it (not just "Created from")."""
    rc, out = _git(["reflog", "show", "--format=%gs", f"refs/heads/{branch}", "--"], cwd)
    return rc == 0 and any(line.startswith("commit") for line in out.splitlines())


def gh_available() -> bool:
    return not os.environ.get("CI_POLICY_TIDY_NO_GH") and bool(shutil.which("gh"))


def remote_repo(remote: str, cwd: Optional[str]) -> Optional[str]:
    rc, out = _git(["config", "--get-all", f"remote.{remote}.url"], cwd)
    for url in out.splitlines() if rc == 0 else []:
        found = allowlist.repo_from_url(url)
        if found:
            return found
    return None


def gh_merged_at_tip(branch: str, sha: str, remote: str, base: Optional[str],
                     cwd: Optional[str], timeout: float) -> Optional[bool]:
    """Whether GitHub has a PR from `branch` merged into `base` whose head was
    exactly `sha`. False when GitHub cannot be asked at all (no gh, not a
    GitHub remote); None when asking failed this time (offline, timeout)."""
    if not gh_available():
        return False
    repo = remote_repo(remote, cwd)
    if not repo or not base:
        return False
    if timeout <= 0:
        return None
    # Own process group, killed whole on timeout: a plain run(timeout=) waits
    # for the pipes to close, which a child of gh can hold open long after.
    try:
        p = subprocess.Popen(
            ["gh", "pr", "list", "--repo", repo, "--state", "merged", "--head", branch,
             "--json", "headRefOid,baseRefName", "--limit", "100"],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = p.communicate(timeout=min(timeout, GH_TIMEOUT))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        return None
    if p.returncode != 0:
        return None
    try:
        prs = json.loads(out or "[]")
    except ValueError:
        return None
    if not isinstance(prs, list):
        return None
    return any(isinstance(pr, dict) and pr.get("headRefOid") == sha
               and pr.get("baseRefName") == base for pr in prs)


def _kept_path(cwd: Optional[str]) -> Optional[str]:
    rc, path = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    return os.path.join(path, KEPT_FILE) if rc == 0 and path else None


def _read_kept(path: Optional[str]) -> Set[str]:
    try:
        with open(path or "", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def _write_kept(path: Optional[str], entries: Set[str]) -> None:
    if not path:
        return
    try:
        if entries:
            with open(path, "w", encoding="utf-8") as f:
                f.write("".join(f"{e}\n" for e in sorted(entries)))
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def tidy(cwd: Optional[str] = None, dry_run: bool = False, use_gh: bool = True,
         hook: bool = False, report: Report = lambda line: None) -> int:
    """Delete merged local branches, calling report() for each line as it
    happens. Returns how many were deleted (or would be, on a dry run)."""
    gone = candidates(cwd)
    kept_path = _kept_path(cwd) if hook else None
    if not gone:
        _write_kept(kept_path, set())
        return 0
    if rebase_in_progress(cwd):
        if not hook:
            report("skipped: a rebase is in progress")
        return 0
    busy = checked_out(cwd)
    if busy is None:
        report("skipped: could not list worktrees")
        return 0
    seen = _read_kept(kept_path)
    still_kept: Set[str] = set()
    deadline = time.monotonic() + HOOK_GH_BUDGET if hook else None
    # Part of every memo key: once gh can be asked, branches judged without it
    # are judged again.
    gh_state = "gh" if use_gh and gh_available() else "nogh"
    quiet_unlinked = 0
    done = 0
    try:
        for branch, sha, remote, linked in gone:
            base = default_ref(remote, cwd)
            base_name = base[len(f"refs/remotes/{remote}/"):] if base else None
            if base is None or branch in ALWAYS_KEEP or branch == base_name:
                continue
            # Linked: judged once per tip (its PR already merged or not). Unlinked:
            # also per base tip, since its PR may merge later.
            base_sha = "" if linked else _git(["rev-parse", base], cwd)[1]
            key = f"{branch} {sha} {base_sha} {gh_state}"
            if hook and key in seen:
                still_kept.add(key)  # judged at this tip before: stay quiet
                continue
            if branch in busy:
                # Not remembered: it is judged again once it is switched away from.
                if not hook and linked:
                    report(f"kept {branch}: it is checked out (its remote branch is gone)")
                continue
            how = None
            asked: Optional[bool] = False
            ancestor = _git(["merge-base", "--is-ancestor", sha, base], cwd)[0] == 0
            if ancestor and (linked or made_commits(branch, cwd)):
                # Unlinked: an ancestor is only merged if commits were made on
                # it; a branch just created from main is an ancestor too.
                how = "merged"
            elif not ancestor and squash_landed(sha, base, cwd):
                how = "squash-merged"
            elif use_gh and (linked or not hook):
                # The hook asks GitHub only about linked branches; asking about
                # every local branch whenever main moves would slow pulls.
                budget = (deadline - time.monotonic()) if deadline else GH_TIMEOUT
                asked = gh_merged_at_tip(branch, sha, remote, base_name, cwd, budget)
                if asked:
                    how = "PR merged"
            if how is None:
                if asked is None:
                    # GitHub could not be asked this time: try again next pull.
                    if not hook:
                        report(f"kept {branch}: could not ask GitHub whether its PR merged")
                    continue
                still_kept.add(key)
                if not linked:
                    quiet_unlinked += 1  # an ordinary local branch
                    continue
                report(f"kept {branch}: its remote branch is gone but its work is not in "
                       f"{remote}/{base_name} (delete it yourself if it is abandoned)")
                continue
            if not dry_run and _git(["branch", "-D", "--", branch], cwd)[0] != 0:
                report(f"kept {branch}: git branch -D failed")
                continue
            done += 1
            report(f"{'would delete' if dry_run else 'deleted'} {branch} ({how}; "
                   f"restore with: git branch {branch} {sha[:9]})")
    finally:
        _write_kept(kept_path, still_kept)
    if quiet_unlinked and not hook:
        report(f"left {quiet_unlinked} local branch{'es' if quiet_unlinked != 1 else ''} "
               f"alone: no proof their work is in the default branch"
               f"{'' if gh_state == 'gh' else ' (gh unavailable, so merged PRs were not checked)'}")
    return done
