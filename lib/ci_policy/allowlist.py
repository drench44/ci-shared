"""The shared allowlist of commits that may reach a protected branch without a PR.

One JSON file (policy/main-allowlist.json) feeds three guards: main-watch in
CI, the global git pre-push hook, and the Claude Code push guard. Keeping one
file means an automation is allowed everywhere or nowhere.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence

from ci_policy.globs import GlobSet

PROTECTED_BRANCHES = ("main", "master")
# No allow rule can pass a commit that touches these, whatever the rule says:
# a change to a workflow (or to what runs the checks) always needs a PR.
PROTECTED_PATHS = GlobSet([".github", ".github/**"])


def touches_protected(path: str) -> bool:
    """Case-insensitive, so ``.GitHub/workflows`` (one directory on a
    case-insensitive disk) counts too."""
    norm = posixpath.normpath("/" + (path or "").replace("\\", "/")).lstrip("/").lower()
    return PROTECTED_PATHS.matches(norm)
# ASCII only, and checked BEFORE lowercasing: str.lower() folds some non-ASCII
# letters into ASCII ones (the Kelvin sign becomes "k").
_PLAIN_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
_HASHED_KEY = re.compile(r"sha256:[0-9a-fA-F]{64}", re.ASCII)


@dataclass
class Rule:
    name: str
    subject: Pattern[str]
    branch: Optional[str] = None
    author: Optional[str] = None
    paths: Optional[GlobSet] = None
    # Globs no touched file may match (for automation whose folders are not
    # known in advance).
    not_paths: Optional[GlobSet] = None

    def describe(self) -> str:
        bits = [f"subject /{self.subject.pattern}/"]
        if self.branch:
            bits.append(f"branch {self.branch}")
        if self.author:
            bits.append(f"author {self.author}")
        if self.paths:
            bits.append("paths " + ", ".join(self.paths.patterns))
        if self.not_paths:
            bits.append("never " + ", ".join(self.not_paths.patterns))
        return f"{self.name} ({'; '.join(bits)})"


def make_rule(item: Dict[str, Any], where: str) -> Rule:
    if not isinstance(item, dict) or not isinstance(item.get("subject"), str):
        raise ValueError(f"{where} needs a string \"subject\"")
    try:
        subject = re.compile(item["subject"])
    except re.error as e:
        raise ValueError(f"{where}: bad subject regex {item['subject']!r}: {e}") from None
    globs = {}
    for key in ("paths", "not_paths"):
        value = item.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise ValueError(f"{where}: \"{key}\" must be a list of globs")
        globs[key] = GlobSet(value) if value else None
    for key in ("branch", "author", "name"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ValueError(f"{where}: \"{key}\" must be a string")
    return Rule(name=item.get("name") or where, subject=subject,
                branch=item.get("branch") or None, author=item.get("author") or None,
                paths=globs["paths"], not_paths=globs["not_paths"])


def parse(data: Any) -> Dict[str, List[Rule]]:
    """Validate a loaded allowlist document; return repo (lowercase) -> rules."""
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        raise ValueError("allowlist must be an object with a \"repos\" object")
    out: Dict[str, List[Rule]] = {}
    for repo, items in data["repos"].items():
        if not isinstance(items, list):
            raise ValueError(f"allowlist repo {repo} must map to a list of rules")
        if not isinstance(repo, str) or not (_PLAIN_REPO.fullmatch(repo)
                                             or _HASHED_KEY.fullmatch(repo)):
            raise ValueError(f"allowlist key {repo!r} is neither owner/name nor "
                             "sha256:<64 hex digits>")
        key = repo.lower()
        if key in out:
            raise ValueError(f"allowlist repo {repo} is listed twice")
        out[key] = [make_rule(item, f"{repo}[{i}]") for i, item in enumerate(items)]
    # One repo, one entry: a hashed key whose plain twin is also listed would
    # let a second, harder to review set of rules ride along.
    plain = {repo_key(k): k for k in out if not k.startswith("sha256:")}
    for k in out:
        if k in plain:
            raise ValueError(f"allowlist lists {plain[k]} twice: plainly and as its "
                             "sha256 key; keep one")
    return out


def parse_repo_rules(data: Any, where: str) -> List[Rule]:
    """A repo's own rules file (``.github/main-allowlist.json``):
    {"rules": [rule, ...]}, for that repo only."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError(f"{where} must be an object with a \"rules\" list")
    return [make_rule(item, f"{where}[{i}]") for i, item in enumerate(data["rules"])]


def load(path: str) -> Dict[str, List[Rule]]:
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from None
    return parse(data)


def repo_key(repo: str) -> str:
    """The key a private repo uses in this public allowlist: the sha256 of its
    lowercase owner/name, so the file never names it."""
    return "sha256:" + hashlib.sha256(repo.lower().encode("utf-8")).hexdigest()


def is_plain_repo(repo: Optional[str]) -> bool:
    """True for a plain owner/name (never a sha256: key or anything else)."""
    return bool(repo) and bool(_PLAIN_REPO.fullmatch(repo or ""))


def rules_for(allow: Dict[str, List[Rule]], repo: Optional[str]) -> List[Rule]:
    """Rules for one repo, asked for by its real owner/name.

    Raises ValueError for anything else: a ``sha256:...`` key passed in as the
    repo (say through ``git config ci-policy.repo``) would otherwise reach a
    hashed repo's rules without knowing its name.
    """
    if not repo:
        return []
    if not _PLAIN_REPO.fullmatch(repo):
        raise ValueError(f"{repo!r} is not a plain owner/name")
    name = repo.lower()
    return list(allow.get(name, [])) + list(allow.get(repo_key(name), []))


# A whole remote URL, parsed from the start the way git reads it: the host is
# the part after the scheme (and any user@), never a "github.com" somewhere in
# the path. It must be github.com or a subdomain (ssh.github.com:443).
_OWNER_NAME = r"/?([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?"
_URL_REMOTE = re.compile(r"(?:https?|ssh|git|git\+ssh|ssh\+git)://(?:[^@/?#\s]+@)?"
                         r"([A-Za-z0-9.-]+)(?::\d+)?/" + _OWNER_NAME, re.I | re.ASCII)
_SCP_REMOTE = re.compile(r"(?:[^@/:\s]+@)?([A-Za-z0-9.-]+):" + _OWNER_NAME, re.I | re.ASCII)


def is_github_host(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return host == "github.com" or host.endswith(".github.com")


def repo_from_url(url: str) -> Optional[str]:
    """owner/name from a GitHub remote URL (https, ssh, or scp-like), else None.

    The whole URL must parse: an extra path segment, a query, or "github.com"
    anywhere but the host means it is not a GitHub repo URL.
    """
    url = (url or "").strip()
    m = _URL_REMOTE.fullmatch(url) if "://" in url else _SCP_REMOTE.fullmatch(url)
    if not m or not is_github_host(m.group(1)) or m.group(3) in (".", ".."):
        return None
    return f"{m.group(2)}/{m.group(3)}"


# Up to the LAST "@" before the path: a password may itself hold an "@".
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]*@")


def redact(text: str) -> str:
    """Text with the user:password@ part of every URL in it removed, so a
    token in a remote URL never reaches a message or a log."""
    return _USERINFO.sub(r"\1", str(text or ""))


def is_local_url(url: str) -> bool:
    """A remote that is a path on this machine (or file://), the way git tells:
    no ``scheme://`` and no ``host:`` before the first slash."""
    url = (url or "").strip()
    if not url:
        return False
    if url.lower().startswith("file://"):
        return True
    if "://" in url:
        return False
    colon, slash = url.find(":"), url.find("/")
    return colon == -1 or (slash != -1 and slash < colon)


def match(rules: Sequence[Rule], subject: str, branch: Optional[str],
          files: Callable[[], Optional[List[str]]],
          authors: Sequence[str] = ()) -> Optional[Rule]:
    """First rule this commit satisfies, or None.

    ``files`` is called lazily (once a rule's subject, branch and author fit)
    and must return every path the commit touches, or None when that is not
    known (a truncated list). Unknown files never pass; an empty list never
    satisfies a ``paths`` rule; and no rule passes a commit that touches
    PROTECTED_PATHS (.github/**), whatever it says.
    """
    touched: List[Optional[List[str]]] = []
    for rule in rules:
        if not rule.subject.search(subject or ""):
            continue
        if rule.branch and branch and rule.branch != branch:
            continue
        if rule.author and rule.author.lower() not in {a.lower() for a in authors if a}:
            continue
        if not touched:
            touched.append(files())
        paths = touched[0]
        if paths is None or any(touches_protected(p) for p in paths):
            return None
        if rule.paths and (not paths or not all(rule.paths.matches(p) for p in paths)):
            continue
        if rule.not_paths and any(rule.not_paths.matches(p) for p in paths):
            continue
        return rule
    return None
