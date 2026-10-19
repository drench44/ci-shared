"""main-watch-audit: notice when main-watch did not run at all.

main-watch runs on each repo's own runner. Private repos use a self-hosted runner, so when
it is down the job just sits queued (GitHub drops it after a day) and
nobody hears about it. This audit runs somewhere else: a scheduled workflow in
a private repo (which keeps the list of watched repos private), on
GitHub-hosted runners, reading every watched repo with a read-only token.

For every push to a watched branch between ``lookback-hours`` and
``grace-hours`` ago, the pushed tip must carry a final ``ci-policy/main-watch``
status (success, failure or error) set by GitHub Actions (creator
``github-actions[bot]``); a status anyone else set does not count, and neither
does a workflow run without a status. A pending status that old means the
scheduled re-check is not running either. A push that changes any workflow
file (``.github/workflows/``) is a problem too: it can change or switch off
main-watch itself. Problems open one issue in this repo labeled
``main-watch-audit`` (or comment on the open one). Each problem is reported
once: the audit's own issues and comments carry the keys it reported, and
closing the issue acknowledges them.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple

from ci_policy import gh
from ci_policy.main_watch import STATUS_CONTEXT, ZERO_SHA, parse_time, utcnow

FINAL_STATES = {"success", "failure", "error"}
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
WORKFLOW_RE = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and bool(SHA_RE.fullmatch(value))


def valid_repo(value: object) -> bool:
    return isinstance(value, str) and bool(REPO_RE.fullmatch(value)) and ".." not in value \
        and not any(part in (".", "..") for part in value.split("/"))


def valid_branch(value: object) -> bool:
    return isinstance(value, str) and bool(BRANCH_RE.fullmatch(value)) \
        and ".." not in value and "//" not in value and not value.endswith(("/", ".lock", "."))


def valid_workflow(value: object) -> bool:
    return isinstance(value, str) and bool(WORKFLOW_RE.fullmatch(value)) and ".." not in value
# Who writes the main-watch status (a workflow's GITHUB_TOKEN) and the audit's
# own issues and comments.
ACTIONS_BOT = "github-actions[bot]"
WORKFLOWS_DIR = ".github/workflows/"
# The compare API may stop listing files well before 300; 100 or more is
# treated as possibly cut short.
COMPARE_FILE_LIMIT = 100
MAX_COMPARE_PAGES = 30
KEYS_RE = re.compile(r"<!-- main-watch-audit keys: ([^>]*) -->")
DEFAULT_WORKFLOW = ".github/workflows/main-watch.yml"
EXPIRY_WARN_DAYS = 14


@dataclass
class Watched:
    repo: str
    branch: str
    # When the repo's main-watch caller merged: earlier pushes had nothing to
    # conclude on them.
    since: dt.datetime
    workflow: str = DEFAULT_WORKFLOW


@dataclass
class Problem:
    key: str
    repo: str
    text: str
    # True for a fact about one push (reported once; closing the issue
    # acknowledges it). False for a standing condition (a repo the token cannot
    # read, a missing token): reported again whenever no audit issue is open.
    once: bool = True


@dataclass
class RepoReport:
    repo: str
    checked: int = 0
    problems: List[Problem] = field(default_factory=list)


def load_repos(path: str) -> List[Watched]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise ValueError(f"{path}: expected an object with a non-empty `repos` map")
    out = []
    for name, cfg in repos.items():
        if not valid_repo(name):
            raise ValueError(f"{path}: `{name}` is not owner/repo")
        if not isinstance(cfg, dict) or not valid_branch(cfg.get("branch")):
            raise ValueError(f"{path}: {name} needs a `branch` (a plain branch name)")
        workflow = cfg.get("workflow", DEFAULT_WORKFLOW)
        if not valid_workflow(workflow):
            raise ValueError(f"{path}: {name} `workflow` must be a .github/workflows/*.yml file")
        since = parse_time(cfg.get("since")) if isinstance(cfg.get("since"), str) else None
        if since is None or since.tzinfo is None:
            raise ValueError(f"{path}: {name} needs `since`, the UTC time its main-watch caller "
                             "merged (for example 2026-09-23T14:43:00Z)")
        out.append(Watched(name, cfg["branch"], since, workflow))
    return out


def _hours(delta: dt.timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f} h"


def inert(text: str) -> str:
    """Text copied into an issue or comment, unable to open or close an HTML
    comment (so it can never forge or hide the audit's key marker)."""
    out = str(text or "")
    while "<!--" in out or "-->" in out:
        out = out.replace("<!--", "").replace("-->", "")
    return out


# A manual dispatch can run on chosen inputs, so only push and schedule runs
# (the push check and the hourly re-check) vouch for a commit.
WATCH_EVENTS = {"push", "schedule"}
_RUN_URL = r"https://github\.com/{repo}/actions/runs/(\d+)(?:/attempts/\d+)?/?"


def _blob(api: gh.GitHub, repo: str, path: str, ref: str) -> Optional[str]:
    try:
        data = api.get(f"/repos/{repo}/contents/{path}", {"ref": ref}) or {}
    except gh.GitHubError as e:
        if e.status != 404:
            raise
        return None
    return data.get("sha") if isinstance(data, dict) else None


def _watch_run(api: gh.GitHub, w: Watched, status: dict) -> Optional[dict]:
    """The Actions run a status links to, when it is a run of the watched
    workflow in this repo; else None. Every workflow posts as
    github-actions[bot], so the creator alone proves nothing."""
    m = re.fullmatch(_RUN_URL.format(repo=re.escape(w.repo)), status.get("target_url") or "",
                     re.I)
    if not m:
        return None
    try:
        run = api.get(f"/repos/{w.repo}/actions/runs/{m.group(1)}") or {}
    except gh.GitHubError as e:
        if e.status != 404:
            raise
        return None
    if not run or (run.get("repository") or {}).get("full_name", "").lower() != w.repo.lower():
        return None
    if run.get("path") != w.workflow or run.get("event") not in WATCH_EVENTS \
            or run.get("head_branch") != w.branch:
        return None
    if status.get("state") != "pending" and (
            run.get("status") != "completed" or run.get("conclusion") not in ("success",
                                                                               "failure")):
        return None   # a final mark needs a run that finished (green, or red for a flag)
    return run


def _workflow_unchanged(api: gh.GitHub, w: Watched, head: str) -> bool:
    """The watched workflow file at the run's commit is the one its parent had,
    so the run was not executing a workflow its own commit rewrote."""
    parents = (api.get(f"/repos/{w.repo}/commits/{head}") or {}).get("parents") or []
    if not parents or not isinstance(parents[0], dict) or not valid_sha(parents[0].get("sha")):
        return False
    now = _blob(api, w.repo, w.workflow, head)
    return now is not None and now == _blob(api, w.repo, w.workflow, parents[0].get("sha"))


OK_CONCLUSIONS = {"success", "neutral", "skipped"}
CI_SHARED = "drench44/ci-shared"
# The only two kinds of line a pin bump may change, compared whole-file: a
# job-level `uses:` of a ci-shared reusable workflow and that job's
# `with: ci-policy-ref:`. The sha is group 2; everything else on the line
# must stay byte for byte (a trailing comment may change).
_PIN_USES = re.compile(r"^( +uses: " + re.escape(CI_SHARED) +
                       r"/\.github/workflows/[A-Za-z0-9_-]+\.ya?ml@)([0-9a-f]{40})( +#.*)?$")
_PIN_REF = re.compile(r"^( +ci-policy-ref: ['\"]?)([0-9a-f]{40})(['\"]?)( +#.*)?$")
# YAML features that can make a line mean something other than it looks.
_RISKY_YAML = [re.compile(r"(^|[\s:\[,{-])&[A-Za-z0-9_-]"),     # anchor
               re.compile(r"(^|[\s:\[,{-])\*[A-Za-z0-9_-]"),    # alias
               re.compile(r"<<\s*:"),                            # merge key
               re.compile(r"^(---|\.\.\.)(\s|$)"),               # document marker
               re.compile(r"\t"),                                # tab indentation
               re.compile(r"^\s*[?%!]")]                         # complex key, directive, tag
_BLOCK_SCALAR = re.compile(r"[:-]\s*[|>][-+0-9]*\s*(#.*)?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parent(lines: Sequence[str], i: int) -> int:
    """Index of the nearest line above ``i`` that is less indented (its YAML
    parent), skipping blanks and comments; -1 at the top."""
    want = _indent(lines[i])
    for j in range(i - 1, -1, -1):
        text = lines[j].strip()
        if not text or text.startswith("#"):
            continue
        if _indent(lines[j]) < want:
            return j
    return -1


def _key(lines: Sequence[str], i: int) -> str:
    m = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(#.*)?$", lines[i]) if i >= 0 else None
    return m.group(1) if m else ""


def _pin_line_ok(lines: Sequence[str], i: int, m: "re.Match[str]") -> bool:
    """The pin line really is what it looks like: `jobs.<id>.uses` or
    `jobs.<id>.with.ci-policy-ref`, not text inside a block scalar or another
    key's value."""
    if _BLOCK_SCALAR.search(lines[_parent(lines, i)] if _parent(lines, i) >= 0 else ""):
        return False
    up = _parent(lines, i)
    if m.re is _PIN_USES:
        job = up
    else:
        if _key(lines, up) != "with":
            return False
        job = _parent(lines, up)
    jobs = _parent(lines, job)
    return bool(_key(lines, job)) and _key(lines, jobs) == "jobs" and \
        jobs >= 0 and _indent(lines[jobs]) == 0


def pin_bump_shas(before: str, after: str) -> Optional[Set[str]]:
    moves = pin_bump_moves(before, after)
    return {new for _, new in moves} if moves else None


def pin_bump_moves(before: str, after: str) -> Optional[Set[Tuple[str, str]]]:
    """The new ci-shared sha when ``after`` is ``before`` with only pin lines
    changed (same lines, same order, only the sha and a trailing comment
    differ, one new sha for the whole file), else None. Compares the whole
    file, so an added step, job, input or trigger anywhere fails it.
    Returns the (old sha, new sha) pairs the pin lines moved between."""
    old, new = before.split("\n"), after.split("\n")
    if len(old) != len(new):
        return None
    if any(r.search(line) for line in new for r in _RISKY_YAML):
        return None
    shas: Set[str] = set()
    moves: Set[Tuple[str, str]] = set()
    changed = 0
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        ma = _PIN_USES.match(a) or _PIN_REF.match(a)
        mb = _PIN_USES.match(b) or _PIN_REF.match(b)
        if not ma or not mb or ma.re is not mb.re or ma.group(1) != mb.group(1):
            return None
        if mb.re is _PIN_REF and ma.group(3) != mb.group(3):
            return None
        if not _pin_line_ok(new, i, mb) or not _pin_line_ok(old, i, ma):
            return None
        shas.add(mb.group(2))
        moves.add((ma.group(2), mb.group(2)))
        changed += 1
    # The job's ci-policy-ref must match its uses: one sha on every pin line.
    every = {m.group(2) for m in (_PIN_USES.match(l) or _PIN_REF.match(l) for l in new) if m}
    return moves if changed and len(shas) == 1 and every == shas else None


def _file_text(api: gh.GitHub, repo: str, path: str, ref: str) -> Optional[str]:
    """A file's text at ``ref``, or None (missing, too big for the API, not
    base64, not UTF-8): never a guess."""
    try:
        data = api.get(f"/repos/{repo}/contents/{path}", {"ref": ref})
    except gh.GitHubError as e:
        if e.status != 404:
            raise
        return None
    if not isinstance(data, dict) or data.get("type") != "file" \
            or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _moves_forward(api: gh.GitHub, old: str, new: str) -> bool:
    """Does ci-shared's ``new`` come strictly after ``old`` (``old`` is an
    ancestor of ``new``)? A downgrade to an older commit, or a sideways move
    to a commit that does not contain ``old``, is not a pin bump."""
    if not valid_sha(old) or not valid_sha(new) or old == new:
        return False
    try:
        cmp = api.get(f"/repos/{CI_SHARED}/compare/{old}...{new}") or {}
    except gh.GitHubError as e:
        if e.status not in (404, 422):
            raise
        return False
    return isinstance(cmp, dict) and cmp.get("status") == "ahead"


def _on_ci_shared_main(api: gh.GitHub, sha: str) -> bool:
    """Is ``sha`` a commit on ci-shared's main (not a fork's commit that
    GitHub would also serve under the same name)?"""
    if not valid_sha(sha):
        return False
    try:
        cmp = api.get(f"/repos/{CI_SHARED}/compare/{sha}...main") or {}
    except gh.GitHubError as e:
        if e.status not in (404, 422):
            raise
        return False
    return isinstance(cmp, dict) and cmp.get("status") in ("ahead", "identical")


def pure_pin_bump(api: gh.GitHub, w: Watched, base: str, head: str,
                  files: Sequence[dict]) -> bool:
    """Every workflow file changed between ``base`` and ``head`` is a pure
    pin bump to a commit on ci-shared's main. ``files`` (the changed files)
    must be a complete list with a usable name on every entry; a list that
    may be cut short, a renamed, added or removed workflow, or any file that
    cannot be read means no."""
    if not valid_sha(base) or not valid_sha(head) or not isinstance(files, list) or not files \
            or len(files) >= COMPARE_FILE_LIMIT:
        return False
    paths: List[str] = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str):
            return False
        names = [f["filename"], f.get("previous_filename") or ""]
        if any(n.startswith(WORKFLOWS_DIR) for n in names if n):
            if f.get("status") != "modified" or f.get("previous_filename") \
                    or not valid_workflow(f["filename"]):
                return False
            paths.append(f["filename"])
    if not paths:
        return False
    for path in paths:
        old = _file_text(api, w.repo, path, base)
        new = _file_text(api, w.repo, path, head)
        moves = pin_bump_moves(old, new) if old is not None and new is not None else None
        if not moves or not all(_moves_forward(api, a, b) and _on_ci_shared_main(api, b)
                                for a, b in moves):
            return False
    return True


def pr_vetted(api: gh.GitHub, w: Watched, sha: str, push_commits: Optional[List[str]],
              files: Sequence[dict], base: str) -> bool:
    """Was the workflow change in ``files`` a pure ci-shared pin bump, landed
    by ``sha`` as the merge (or squash) commit of a PR merged into the
    watched branch whose head checks were all green? Green checks alone
    prove nothing about a rigged workflow, so only a pin bump to a commit on
    ci-shared's main qualifies; any other workflow edit is reported.

    ``push_commits``: every commit the push added. Each must be ``sha`` itself
    or one of that PR's own commits, so a direct commit pushed along with a
    PR merge is never covered by the PR.
    """
    if not pure_pin_bump(api, w, base, sha, files):
        return False
    try:
        pulls = api.get(f"/repos/{w.repo}/commits/{sha}/pulls") or []
    except gh.GitHubError as e:
        if e.status not in (404, 422):
            raise
        return False
    for pr in pulls:
        if not isinstance(pr, dict) or not pr.get("merged_at") \
                or (pr.get("base") or {}).get("ref") != w.branch \
                or pr.get("merge_commit_sha") != sha \
                or not isinstance(pr.get("number"), int) or isinstance(pr.get("number"), bool):
            continue
        # The PR as it was merged (the commits list can be stale).
        pr = api.get(f"/repos/{w.repo}/pulls/{pr['number']}") or {}
        if not pr.get("merged") or pr.get("merge_commit_sha") != sha \
                or (pr.get("base") or {}).get("ref") != w.branch:
            continue
        head = (pr.get("head") or {}).get("sha")
        if not valid_sha(head):
            continue
        if push_commits is not None:
            own = {c.get("sha") for c in api.paginate(
                f"/repos/{w.repo}/pulls/{pr['number']}/commits", complete=True)}
            if any(c != sha and c not in own for c in push_commits):
                continue
        runs = api.paginate(f"/repos/{w.repo}/commits/{head}/check-runs", {"filter": "latest"},
                            item_key="check_runs", complete=True)
        combined = (api.get(f"/repos/{w.repo}/commits/{head}/status") or {}).get("state")
        if runs and all(r.get("status") == "completed" and r.get("conclusion") in OK_CONCLUSIONS
                        for r in runs) \
                and any(r.get("conclusion") == "success"
                        and (r.get("app") or {}).get("slug") == "github-actions"
                        for r in runs) \
                and combined not in ("failure", "error"):
            return True
    return False


def _reaches(api: gh.GitHub, repo: str, sha: str, head: str) -> bool:
    """Is ``sha`` the commit ``head`` or one of its ancestors?"""
    if sha == head:
        return True
    try:
        cmp = api.get(f"/repos/{repo}/compare/{sha}...{head}") or {}
    except gh.GitHubError as e:
        if e.status not in (404, 422):
            raise
        return False
    return cmp.get("status") == "ahead"


def audit_status(api: gh.GitHub, w: Watched, sha: str, tip: str) -> Optional[dict]:
    """The newest ``ci-policy/main-watch`` status on ``sha`` that the watched
    main-watch workflow itself wrote, or None.

    It must be created by github-actions[bot] (read from the full status list,
    which names the creator) and link to a run of ``w.workflow`` in ``w.repo``
    on the watched branch: a push run whose head is ``tip`` (the push that
    brought ``sha``), or a scheduled re-check whose head contains ``sha``. A
    final status needs a finished run (success, or failure for a flag), and
    the run's commit must not have changed the workflow file it ran. Anything
    else, even from another workflow in the same repo, is ignored.
    """
    for s in api.paginate(f"/repos/{w.repo}/commits/{sha}/statuses", {"per_page": 100}):
        if s.get("context") != STATUS_CONTEXT or \
                (s.get("creator") or {}).get("login") != ACTIONS_BOT:
            continue
        run = _watch_run(api, w, s)
        if not run:
            continue
        head = run.get("head_sha") or ""
        if not valid_sha(head):
            continue
        if run.get("event") == "push" and head != tip:
            continue
        if run.get("event") == "schedule" and not _reaches(api, w.repo, sha, head):
            continue
        if _workflow_unchanged(api, w, head):
            return s
        commit = api.get(f"/repos/{w.repo}/commits/{head}") or {}
        parents = commit.get("parents") if isinstance(commit, dict) else None
        parent = (parents[0] or {}).get("sha") if isinstance(parents, list) and parents \
            and isinstance(parents[0], dict) else None
        if valid_sha(parent) and pr_vetted(api, w, head, None, commit.get("files"), parent):
            return s
    return None


def run_state(api: gh.GitHub, w: Watched, sha: str) -> str:
    """For a push tip with no main-watch status: why not? A hint for the
    report only; a run without a status never counts as concluded."""
    data = api.get(f"/repos/{w.repo}/actions/runs",
                   {"head_sha": sha, "event": "push", "per_page": 100}) or {}
    runs = [r for r in data.get("workflow_runs") or [] if r.get("path") == w.workflow]
    if not runs:
        return "no main-watch run exists for it"
    latest = max(runs, key=lambda r: (r.get("id") or 0, r.get("run_attempt") or 0))
    if latest.get("status") != "completed":
        return f"its main-watch run is still {latest.get('status')}"
    hint = (" (does the caller grant every permission the pinned main-watch asks for?)"
            if latest.get("conclusion") == "startup_failure" else
            " (a main-watch too old to set one? update the caller's pin)"
            if latest.get("conclusion") == "success" else "")
    return (f"its main-watch run ended {latest.get('conclusion') or 'without a conclusion'}"
            f" and left no status{hint}")


def workflow_changes(w: Watched, cmp: dict) -> Optional[List[str]]:
    """Workflow files a push changed, or None when that cannot be told: the
    list is missing, not a list, may be cut short, is empty although the push
    has commits, or has an entry without a file name."""
    files = cmp.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILE_LIMIT:
        return None
    if not files and cmp.get("commits"):
        return None
    hit: List[str] = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str) \
                or not f["filename"]:
            return None
        for name in (f.get("filename"), f.get("previous_filename")):
            if name and (name == w.workflow or name.startswith(WORKFLOWS_DIR)) \
                    and name not in hit:
                hit.append(name)
    return hit


def check_push(api: gh.GitHub, w: Watched, a: dict, age: dt.timedelta) -> List[Problem]:
    after = a.get("after") or ZERO_SHA
    kind = a.get("activity_type")
    link = f"[{after[:7]}](https://github.com/{w.repo}/commit/{after})"
    problems: List[Problem] = []
    mark = audit_status(api, w, after, after)
    state = (mark or {}).get("state")
    if state == "pending":
        problems.append(Problem(
            f"{w.repo}@{after}", w.repo,
            f"{link} ({kind}) is still marked pending {_hours(age)} after the push, so the "
            f"scheduled re-check is not running: {(mark or {}).get('description') or ''}"))
    elif state not in FINAL_STATES:
        problems.append(Problem(
            f"{w.repo}@{after}", w.repo,
            f"{link} ({kind}) has no main-watch conclusion {_hours(age)} after the push: "
            f"{run_state(api, w, after)}"))
    # A commit inside the push (not its tip) left pending is invisible above.
    before = a.get("before") or ZERO_SHA
    cmp: dict = {}
    if before != ZERO_SHA:
        try:
            cmp = api.get(f"/repos/{w.repo}/compare/{before}...{after}", {"per_page": 100}) or {}
        except gh.GitHubError as e:
            if e.status not in (404, 422):
                raise
            cmp = {}   # the old tip is gone (force push); main-watch flagged that itself
        commits = list(cmp.get("commits") or [])
        total = int(cmp.get("total_commits") or len(commits))
        page = 2
        while cmp and len(commits) < total and page <= MAX_COMPARE_PAGES:
            more = (api.get(f"/repos/{w.repo}/compare/{before}...{after}",
                            {"per_page": 100, "page": page}) or {}).get("commits") or []
            if not more:
                break
            commits.extend(more)
            page += 1
        if cmp and len(commits) < total:
            problems.append(Problem(
                f"{w.repo}@{after}:partial", w.repo,
                f"push {link} has {total} commits and only {len(commits)} could be read; "
                "the rest were not checked"))
        if cmp:
            cmp["commits"] = commits
    # Workflow changes: a PR merged green may change them (a pin bump); a
    # direct push may not. Unknown (new branch, old tip gone) counts as changed.
    if cmp:
        changed = workflow_changes(w, cmp)
    else:
        changed = None
    if changed is None or changed:
        listed = [c.get("sha") for c in cmp.get("commits") or []
                  if isinstance(c, dict)] if cmp else None
        if listed is not None and not all(valid_sha(x) for x in listed):
            listed = None
        if listed is None or not pr_vetted(api, w, after, listed, cmp.get("files"), before):
            what = (f"changed workflow files, which can change or turn off main-watch: "
                    f"{', '.join(changed[:10])}" if changed else
                    "could not be compared with the branch before it (a new branch, a force "
                    "push, or too many files), so it may have changed a workflow (main-watch "
                    "included); review it by hand")
            problems.append(Problem(f"{w.repo}@{after}:workflows", w.repo,
                                    f"push {link} ({kind}) {what}"))
    if cmp:
        for c in cmp.get("commits") or []:
            sha = c.get("sha") if isinstance(c, dict) else None
            if not valid_sha(sha):
                problems.append(Problem(f"{w.repo}@{after}:bad", w.repo,
                                        f"push {link} lists a commit id that is not a sha"))
                continue
            if sha == after:
                continue
            inner = audit_status(api, w, sha, after)
            state = (inner or {}).get("state")
            inner_link = f"[{sha[:7]}](https://github.com/{w.repo}/commit/{sha})"
            if state == "pending":
                problems.append(Problem(
                    f"{w.repo}@{sha}", w.repo,
                    f"{inner_link}, part of push {link}, is still marked pending "
                    f"{_hours(age)} after the push, so the scheduled re-check is not "
                    f"running: {inner.get('description') or ''}"))
            elif state not in FINAL_STATES:
                problems.append(Problem(
                    f"{w.repo}@{sha}", w.repo,
                    f"{inner_link}, part of push {link}, has no main-watch conclusion "
                    f"{_hours(age)} after the push"))
    return problems


MAX_LOOKBACK_HOURS = 24 * 365


def time_period(lookback_hours: float) -> str:
    """The activity API's time_period that covers the lookback."""
    for hours, name in ((24, "day"), (24 * 7, "week"), (24 * 30, "month"),
                        (24 * 90, "quarter"), (MAX_LOOKBACK_HOURS, "year")):
        if lookback_hours <= hours:
            return name
    raise ValueError(f"lookback-hours {lookback_hours:g} is more than a year")


def audit_repo(api: gh.GitHub, w: Watched, now: dt.datetime, grace_hours: float,
               lookback_hours: float) -> RepoReport:
    report = RepoReport(w.repo)
    if w.since > now:
        report.problems.append(Problem(
            f"{w.repo}:since", w.repo,
            f"`since` ({w.since:%Y-%m-%dT%H:%MZ}) is in the future, so nothing would be "
            "audited; fix the repos file", once=False))
        return report
    oldest = max(now - dt.timedelta(hours=lookback_hours), w.since)
    newest = now - dt.timedelta(hours=grace_hours)
    if oldest >= newest:
        report.problems.append(Problem(
            f"{w.repo}:window@{w.since:%Y%m%dT%H%M}", w.repo,
            f"the audit window is empty (`since` {w.since:%Y-%m-%dT%H:%MZ} is within the "
            f"{grace_hours:g} h grace), so no push was checked yet"))
        return report
    read_problem = ("could not read the repo with the audit token ({}). The token needs read "
                    "access to Metadata, Contents, Commit statuses and Actions on this repo.")
    try:
        branch = api.get(f"/repos/{w.repo}/branches/{w.branch}") or {}
        activity = api.paginate(f"/repos/{w.repo}/activity",
                                {"ref": f"refs/heads/{w.branch}",
                                 "time_period": time_period(lookback_hours)},
                                limit=1000, complete=True)
    except gh.GitHubError as e:
        report.problems.append(Problem(f"{w.repo}:read", w.repo, read_problem.format(e),
                                       once=False))
        return report
    tip = ((branch.get("commit") or {}).get("sha")) or ""
    tip_time = parse_time((((branch.get("commit") or {}).get("commit") or {})
                           .get("committer") or {}).get("date"))
    if not tip:
        report.problems.append(Problem(f"{w.repo}:read", w.repo, read_problem.format(
            f"branch {w.branch} has no tip"), once=False))
        return report
    if tip_time is not None and tip_time >= oldest \
            and tip not in {a.get("after") for a in activity}:
        # The branch moved, but the push list does not show it.
        report.problems.append(Problem(
            f"{w.repo}@{tip}:unlisted", w.repo,
            f"`{w.branch}` is at {tip[:7]} (committed {_hours(now - tip_time)} ago), but no "
            "push to it is listed; it could not be audited"))
    seen: Set[str] = set()
    for a in activity:
        ts = parse_time(a.get("timestamp"))
        if ts is None:
            # Cannot tell whether it is in the window: never skip it quietly.
            report.problems.append(Problem(
                f"{w.repo}@{a.get('after') or a.get('id')}:time", w.repo,
                f"a push to `{w.branch}` ({str(a.get('after') or '?')[:7]}) has no readable "
                f"time ({a.get('timestamp')!r}), so it could not be audited"))
            continue
        if ts < oldest or ts > newest:
            continue
        age = now - ts
        after = a.get("after") or ZERO_SHA
        if not valid_sha(after) or not valid_sha(a.get("before") or ZERO_SHA):
            report.problems.append(Problem(
                f"{w.repo}@{a.get('id')}:bad", w.repo,
                f"a push to `{w.branch}` lists a commit id that is not a sha "
                f"({str(after)[:20]!r}); it could not be audited"))
            continue
        if a.get("activity_type") == "branch_deletion" or after == ZERO_SHA:
            report.problems.append(Problem(
                f"{w.repo}@deleted@{a.get('id') or a.get('timestamp')}", w.repo,
                f"`{w.branch}` was deleted {_hours(age)} ago"))
            continue
        if after in seen:
            continue
        seen.add(after)
        report.checked += 1
        try:
            report.problems.extend(check_push(api, w, a, age))
        except gh.GitHubError as e:
            # One push that cannot be read must not hide the others.
            report.problems.append(Problem(
                f"{w.repo}@{after}:error", w.repo,
                f"could not check push {after[:7]} ({e})", once=False))
    return report


def token_expiry_problem(api: gh.GitHub, now: dt.datetime) -> Optional[Problem]:
    """Fine-grained tokens report their expiry in a response header."""
    _, _, headers = api.request("GET", "/rate_limit")
    raw = next((v for k, v in headers.items()
                if k.lower() == "github-authentication-token-expiration"), "")
    if not raw:
        return None
    try:
        expires = dt.datetime.strptime(raw.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return Problem("token:expiry-unreadable", "",
                       f"could not read the audit token's expiry `{raw}`", once=False)
    if expires - now > dt.timedelta(days=EXPIRY_WARN_DAYS):
        return None
    return Problem(f"token:expires:{expires:%Y-%m-%d}", "",
                   f"the audit token (secret MAIN_WATCH_AUDIT_TOKEN) expires {expires:%Y-%m-%d}; "
                   "make a new one and update the secret", once=False)


def reported_keys(api: gh.GitHub, home: str,
                  label: str) -> Tuple[Set[str], Optional[int], Set[str]]:
    """Keys the recent audit issues reported (open or closed), the open issue's
    number, and the keys that open issue carries."""
    issues = api.get(f"/repos/{home}/issues", {"labels": label, "state": "all",
                                               "sort": "created", "direction": "desc",
                                               "per_page": 10}) or []
    issues = [i for i in issues if "pull_request" not in i]
    every: Set[str] = set()
    open_number: Optional[int] = None
    open_keys: Set[str] = set()
    for issue in issues:
        # Only the audit's own words count: a key marker anyone else wrote (a
        # comment, an issue opened by hand with the label) would silence it.
        if (issue.get("user") or {}).get("login") != ACTIONS_BOT:
            continue
        texts = [issue.get("body") or ""]
        texts += [c.get("body") or "" for c in
                  api.paginate(f"/repos/{home}/issues/{issue['number']}/comments")
                  if (c.get("user") or {}).get("login") == ACTIONS_BOT]
        keys = {k for t in texts for m in KEYS_RE.finditer(t) for k in m.group(1).split() if k}
        every |= keys
        if issue.get("state") == "open" and open_number is None:
            open_number, open_keys = issue["number"], keys
    return every, open_number, open_keys


def new_keys(problems: List[Problem], every: Set[str], open_keys: Set[str]) -> Set[str]:
    """Push facts are new until any issue listed them; standing conditions are
    new until an OPEN issue lists them (closing the issue does not silence a
    token that still cannot read a repo)."""
    return {p.key for p in problems
            if (p.key not in every if p.once else p.key not in open_keys)}


def render_alert(problems: List[Problem], new: Set[str], run_url: str) -> str:
    lines = ["**main-watch-audit** found pushes that main-watch never concluded on. Usually "
             "the runner is down (a self-hosted runner, for private repos) or a main-watch job was "
             "cancelled. Once the runner is back, re-run the main-watch run for that push "
             "from the repo's Actions tab.", "", f"[Audit run]({run_url})", "",
             "| | Repo | Problem |", "| --- | --- | --- |"]
    for p in problems:
        lines.append(f"| {'NEW' if p.key in new else 'still'} | {inert(p.repo) or '-'} | "
                     f"{gh.md_escape(inert(p.text))} |")
    keys = " ".join(sorted(inert(p.key).replace(">", "") for p in problems))
    lines += ["", "Each problem is reported once. Close this issue to acknowledge them; a "
              "later problem opens a new one.",
              f"<!-- main-watch-audit keys: {keys} -->"]
    return "\n".join(lines)


def raise_audit_alert(api: gh.GitHub, home: str, label: str, body: str,
                      open_issue: Optional[int]) -> str:
    if open_issue:
        api.post(f"/repos/{home}/issues/{open_issue}/comments", {"body": body})
        return f"commented on #{open_issue}"
    try:
        api.post(f"/repos/{home}/labels", {"name": label, "color": "D93F0B",
                                           "description": "main-watch did not run on a push"})
    except gh.GitHubError as e:
        if e.status != 422:
            raise
    issue = api.post(f"/repos/{home}/issues", {"title": "main-watch-audit: main-watch did not "
                                                        "conclude on some pushes",
                                               "body": body, "labels": [label]})
    return f"opened #{issue['number']}"


def main() -> int:
    home = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{home}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    label = gh.env_input("issue-label", "main-watch-audit")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    now = utcnow()
    problems: List[Problem] = []
    reports: List[RepoReport] = []
    blind = False
    try:
        home_api = gh.GitHub(gh.env_input("token"), api_url)
    except gh.GitHubError as e:
        gh.annotate("error", f"main-watch-audit has no token for this repo: {e}", "audit")
        return 1
    try:
        watched = load_repos(gh.env_input("repos-file"))
        grace = float(gh.env_input("grace-hours", "6"))
        lookback = float(gh.env_input("lookback-hours", "72"))
        if not 0 < grace < lookback:
            raise ValueError("need 0 < grace-hours < lookback-hours")
        time_period(lookback)   # refuses a lookback the activity API cannot cover
        token = gh.env_input("audit-token")
        if not token:
            blind = True
            problems.append(Problem("token:missing", "", "the MAIN_WATCH_AUDIT_TOKEN secret is "
                                    "not set, so no repo was audited (README, main-watch "
                                    "audit, has the token recipe)", once=False))
        else:
            api = gh.GitHub(token, api_url)
            try:
                expiry = token_expiry_problem(api, now)
            except gh.GitHubError as e:
                blind = True
                expiry = Problem("token:rejected", "", f"the audit token was rejected ({e})",
                                 once=False)
            if expiry:
                problems.append(expiry)
            if not blind:
                for w in watched:
                    r = audit_repo(api, w, now, grace, lookback)
                    reports.append(r)
                    problems.extend(r.problems)
                # Any repo the token cannot read is a repo nobody audits: the
                # run goes red, not just the one issue line.
                if any(p.key.endswith((":read", ":error")) for r in reports
                       for p in r.problems):
                    blind = True
    except Exception as e:  # noqa: BLE001  the audit failing must be loud
        blind = True
        problems.append(Problem(f"audit:crash:{type(e).__name__}", "",
                                f"the audit itself failed: {type(e).__name__}: {e}",
                                once=False))

    lines = [f"## main-watch-audit: {len(problems)} problem(s)", "",
             f"Window: pushes {gh.env_input('lookback-hours', '72')} h to "
             f"{gh.env_input('grace-hours', '6')} h old.", ""]
    if reports:
        lines += ["| Repo | Pushes checked | Problems |", "| --- | --- | --- |"]
        lines += [f"| {r.repo} | {r.checked} | {len(r.problems)} |" for r in reports]
        lines.append("")
    for p in problems:
        lines.append(f"- {p.repo + ': ' if p.repo else ''}{gh.md_escape(inert(p.text))}")

    alert = None
    failed = blind
    if problems:
        try:
            every, open_issue, open_keys = reported_keys(home_api, home, label)
            new = new_keys(problems, every, open_keys)
            if new:
                alert = raise_audit_alert(home_api, home, label,
                                          render_alert(problems, new, run_url), open_issue)
            else:
                alert = "nothing new; already reported"
        except gh.GitHubError as e:
            alert = f"FAILED to open or update the issue ({e})"
            failed = True
        for p in problems:
            gh.annotate("error" if blind else "warning",
                        f"{p.repo + ': ' if p.repo else ''}{p.text}", "main-watch-audit")
        lines += ["", f"Alert issue: {alert}."]
    gh.write_summary("\n".join(lines))
    gh.set_output("problems", str(len(problems)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
