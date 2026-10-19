#!/usr/bin/env bash
# End-to-end tests for the global git hooks: the installer, the dispatcher,
# the pre-commit policy, the pre-push guard, and chaining to repo hooks.
# Real git against throwaway repos and bare remotes; HOME is a temp dir, so
# the machine's own ~/.gitconfig is never touched. Plain bash, no bats needed.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
PASS=0 FAIL=0 CURRENT=""
EMDASH=$(printf '\342\200\224')

fail() { printf '  FAIL [%s]: %s\n' "$CURRENT" "$*"; FAIL=$((FAIL + 1)); }
ok()   { PASS=$((PASS + 1)); }
assert_eq()       { [[ "$1" == "$2" ]] && ok || fail "expected '$2', got '$1' ${3:-}"; }
assert_contains() { [[ "$1" == *"$2"* ]] && ok || fail "expected to find '$2' in: ${1:0:800}"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] && ok || fail "did not expect '$2' in: ${1:0:800}"; }

# A fresh machine: temp HOME, ci-policy installed globally, one repo "r" with a
# bare remote "origin" that already has main.
setup() {
  CURRENT="$1"
  T=$(mktemp -d)
  export HOME="$T/home" GIT_CONFIG_NOSYSTEM=1
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE CI_POLICY_ALLOW_MAIN_PUSH CI_POLICY_ALLOWLIST \
    CI_POLICY_DISPATCH_DEPTH CI_POLICY_HOME CI_POLICY_HOOKS_DIR CI_POLICY_TIDY_NO_GH CI_POLICY_PYTHON \
    GIT_SSH_COMMAND GIT_SSH FAKE_SSH_REPO FAKE_SSH_ROOT FAKE_NOW_PUBLIC
  mkdir -p "$HOME/Documents"
  git config --global user.email t@example.com
  git config --global user.name t
  git config --global init.defaultBranch main
  git config --global protocol.file.allow always
  R="$HOME/Documents/r"
  git init -q --bare "$T/origin.git"
  git init -q "$R"
  git -C "$R" remote add origin "$T/origin.git"
  echo one >"$R/a.txt"
  git -C "$R" add a.txt
  git -C "$R" commit -q -m one
  git -C "$R" push -q origin main 2>/dev/null
  git -C "$R" config ci-policy.repo drench44/test-repo
}

install() { OUT=$(bash "$SRC/install/install-git-hooks.sh" "$@" 2>&1); RC=$?; }

# sshremote URL: point origin at an ssh URL (git@github.com:..., ssh://...)
# that a fake `ssh` first on PATH serves from the local bare repo, the way a
# real machine has ssh on PATH (GIT_SSH_COMMAND would rightly make the leak
# check distrust the destination). The hooks see a real GitHub (or other
# host) URL, with no insteadOf rewriting to a local path. `ssh -G host`
# answers "hostname host" with no proxy.
sshremote() {
  mkdir -p "$T/bin"
  cat >"$T/bin/ssh" <<'EOF'
#!/usr/bin/env bash
# Stands in for ssh: every repo on every host is the bare repo $FAKE_SSH_REPO.
for last; do :; done
if [ "${1:-}" = "-G" ]; then
  printf 'hostname %s\nproxycommand none\n' "$last"
  exit 0
fi
repo="$FAKE_SSH_REPO"
path=$(printf '%s' "$last" | sed -n "s/^git-[a-z-]* '\/*\(.*\)'$/\1/p")
[ -n "${FAKE_SSH_ROOT:-}" ] && [ -n "$path" ] && [ -d "$FAKE_SSH_ROOT/$path" ] && repo="$FAKE_SSH_ROOT/$path"
case "$last" in
  git-receive-pack*) exec git-receive-pack "$repo" ;;
  git-upload-pack*) exec git-upload-pack "$repo" ;;
esac
echo "fake ssh: unexpected command: $*" >&2
exit 1
EOF
  chmod 755 "$T/bin/ssh"
  case ":$PATH:" in *":$T/bin:"*) ;; *) export PATH="$T/bin:$PATH" ;; esac
  export FAKE_SSH_REPO="$T/origin.git"
  git -C "$R" remote set-url origin "$1"
  git -C "$R" fetch -q origin 2>/dev/null
}

# commit FILE CONTENT [message]: sets RC and OUT.
commit() {
  printf '%s\n' "$2" >"$R/$1"
  git -C "$R" add -- "$1"
  OUT=$(git -C "$R" commit -m "${3:-change $1}" 2>&1); RC=$?
}

push() { OUT=$(git -C "$R" push "$@" 2>&1); RC=$?; }

teardown() { rm -rf "$T"; }

# ---------------------------------------------------------------- install

setup "install sets core.hooksPath, copies code, writes wrappers"
install
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" "$HOME/.config/git/hooks"
[[ -x "$HOME/.config/git/hooks/pre-commit" && -x "$HOME/.config/git/hooks/pre-push" ]] && ok || fail "wrappers missing"
[[ -f "$HOME/.config/ci-policy/lib/ci_policy/local_hooks.py" ]] && ok || fail "lib missing"
[[ -f "$HOME/.config/ci-policy/policy/main-allowlist.json" ]] && ok || fail "allowlist missing"
OUT=$(bash "$SRC/install/install-git-hooks.sh" --check 2>&1); assert_eq "$?" 0 "$OUT"
teardown

setup "install refuses to replace a foreign core.hooksPath without --force"
git config --global core.hooksPath /somewhere/else
install
assert_eq "$RC" 1
assert_contains "$OUT" "already /somewhere/else"
install --force
assert_eq "$RC" 0 "$OUT"
teardown

setup "install refuses to clobber foreign files in the hooks dir"
mkdir -p "$HOME/.config/git/hooks"; echo mine >"$HOME/.config/git/hooks/pre-commit"
install
assert_eq "$RC" 1
assert_contains "$OUT" "did not write"
install --force
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$HOME/.config/git/hooks.bak/pre-commit")" "mine"
teardown

setup "reinstall is idempotent"
install; install
assert_eq "$RC" 0 "$OUT"
teardown

setup "dry run changes nothing"
install --dry-run
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
[[ ! -e "$HOME/.config/ci-policy" ]] && ok || fail "dry run wrote files"
teardown

# ---------------------------------------------------------------- pre-commit

setup "em dash in an added line is blocked"
install
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 1
assert_contains "$OUT" "em dash"
assert_contains "$OUT" "b.txt:1"
teardown

setup "clean commit passes; removing an em dash line passes"
printf 'x %s y\n' "$EMDASH" >"$R/old.txt"; git -C "$R" add old.txt; git -C "$R" commit -q -m old --no-verify
install
commit b.txt "plain text"
assert_eq "$RC" 0 "$OUT"
git -C "$R" rm -q old.txt
OUT=$(git -C "$R" commit -m "drop old" 2>&1); assert_eq "$?" 0 "$OUT"
teardown

setup "em dash allowed by ci-policy.emdashAllow and by linguist-vendored"
install
git -C "$R" config --add ci-policy.emdashAllow 'vendor/**'
mkdir -p "$R/vendor"
commit vendor/up.html "a ${EMDASH} b"
assert_eq "$RC" 0 "$OUT"
printf 'board.html linguist-vendored\n' >"$R/.gitattributes"; git -C "$R" add .gitattributes
git -C "$R" commit -q -m attrs
commit board.html "a ${EMDASH} b"
assert_eq "$RC" 0 "$OUT"
teardown

setup "secret in an added line is blocked, redacted, and the marker allows a fake"
install
tok="ghp_$(printf 'A%.0s' {1..36})"
commit c.py "TOKEN = '$tok'"
assert_eq "$RC" 1
assert_contains "$OUT" "GitHub token"
assert_not_contains "$OUT" "$tok"
git -C "$R" reset -q
commit c.py "TOKEN = '$tok'  # ci-policy: allow-secret"
assert_eq "$RC" 0 "$OUT"
teardown

setup "secret-named files are blocked, examples are fine"
install
commit .env "HA_TOKEN=x"
assert_eq "$RC" 1
assert_contains "$OUT" "secret file"
git -C "$R" reset -q; rm "$R/.env"
commit .env.example "HA_TOKEN=put-yours-here"
assert_eq "$RC" 0 "$OUT"
teardown

setup "renaming a file to .env with no content change is still caught"
install
commit settings.txt "HA_TOKEN=x"
git -C "$R" mv settings.txt .env
OUT=$(git -C "$R" commit -m "rename" 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "secret file"
teardown

setup "--no-verify still bypasses pre-commit (human escape hatch)"
install
printf 'a %s b\n' "$EMDASH" >"$R/b.txt"; git -C "$R" add b.txt
OUT=$(git -C "$R" commit -q -m x --no-verify 2>&1); assert_eq "$?" 0 "$OUT"
teardown

# ---------------------------------------------------------------- chaining

setup "repo .git/hooks/pre-commit still runs, after the policy"
cat >"$R/.git/hooks/pre-commit" <<'EOF'
#!/bin/sh
echo local-pre-commit-ran >"$(git rev-parse --git-dir)/marker"
exit 0
EOF
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
[[ -f "$R/.git/marker" ]] && ok || fail "local hook did not run"
teardown

setup "a failing repo hook still blocks the commit"
printf '#!/bin/sh\necho repo-says-no >&2\nexit 1\n' >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 1
assert_contains "$OUT" "repo-says-no"
teardown

setup "the repo hook does not run when the policy already blocked"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/marker"\n' >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 1
[[ ! -f "$R/.git/marker" ]] && ok || fail "local hook ran after a policy block"
teardown

setup "hooks without policy (commit-msg, post-commit) chain with args"
printf '#!/bin/sh\ncp "$1" "$(git rev-parse --git-dir)/msg-copy"\n' >"$R/.git/hooks/commit-msg"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/post"\n' >"$R/.git/hooks/post-commit"
chmod +x "$R/.git/hooks/commit-msg" "$R/.git/hooks/post-commit"
install
commit b.txt "fine" "hello message"
assert_eq "$RC" 0 "$OUT"
assert_contains "$(cat "$R/.git/msg-copy" 2>/dev/null)" "hello message"
[[ -f "$R/.git/post" ]] && ok || fail "post-commit did not chain"
teardown

setup "a non-executable or sample repo hook is ignored"
printf '#!/bin/sh\nexit 1\n' >"$R/.git/hooks/pre-commit"; chmod -x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
teardown

setup "repo with its own core.hooksPath is migrated and its hooks still run"
mkdir -p "$R/.githooks"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/githooks-ran"\n' >"$R/.githooks/pre-commit"
chmod +x "$R/.githooks/pre-commit"
git -C "$R" config core.hooksPath .githooks
install
assert_contains "$OUT" "migrating $R"
assert_eq "$(git -C "$R" config --local --get core.hooksPath)" ""
assert_eq "$(git -C "$R" config --get ci-policy.chainHooksPath)" ".githooks"
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
[[ -f "$R/.git/githooks-ran" ]] && ok || fail ".githooks/pre-commit did not run"
commit c.txt "a ${EMDASH} b"
assert_eq "$RC" 1 "(policy runs in a migrated repo)"
teardown

setup "check flags a repo whose own core.hooksPath shadows the policy"
install
git -C "$R" config core.hooksPath .githooks
OUT=$(bash "$SRC/install/install-git-hooks.sh" --check 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "sets its own core.hooksPath"
teardown

setup "chaining works from a linked worktree"
printf '#!/bin/sh\ntouch "%s/wt-marker"\n' "$T" >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
git -C "$R" worktree add -q "$HOME/Documents/r-wt" -b wt 2>/dev/null
echo x >"$HOME/Documents/r-wt/w.txt"; git -C "$HOME/Documents/r-wt" add w.txt
OUT=$(git -C "$HOME/Documents/r-wt" commit -m wt 2>&1); assert_eq "$?" 0 "$OUT"
[[ -f "$T/wt-marker" ]] && ok || fail "worktree commit did not chain"
teardown

setup "chainHooksPath pointing back at the global hooks does not loop"
install
git -C "$R" config ci-policy.chainHooksPath "$HOME/.config/git/hooks"
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
teardown

# ---------------------------------------------------------------- pre-push

setup "push to main is refused, a feature branch is fine"
install
commit b.txt "fine"
push origin main
assert_eq "$RC" 1
assert_contains "$OUT" "BLOCKED push to main"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 0 "$OUT"
teardown

setup "HEAD:main, refs/heads/main, master and --force are refused"
git -C "$R" push -q origin main:master 2>/dev/null
install
commit b.txt "fine"
push origin HEAD:main;               assert_eq "$RC" 1 "(HEAD:main)"
push origin HEAD:refs/heads/main;    assert_eq "$RC" 1 "(refs/heads/main)"
push origin HEAD:master;             assert_eq "$RC" 1 "(master)"
push --force origin HEAD:main;       assert_eq "$RC" 1 "(--force)"
git -C "$R" reset -q --hard HEAD~1
echo other >"$R/o.txt"; git -C "$R" add o.txt; git -C "$R" commit -q -m other --no-verify
git -C "$R" checkout -q -b tmp; git -C "$R" checkout -q main
push --force origin main
assert_eq "$RC" 1
assert_contains "$OUT" "BLOCKED"
teardown

setup "deleting main on the remote is refused"
install
push origin :main
assert_eq "$RC" 1
assert_contains "$OUT" "deleting main"
teardown

setup "creating main on an empty remote is allowed (new repo)"
install
git init -q --bare "$T/empty.git"
git -C "$R" remote add empty "$T/empty.git"
push empty main
assert_eq "$RC" 0 "$OUT"
teardown

setup "allowlisted release commit may go to main; the same subject touching more may not"
install
git -C "$R" config ci-policy.repo drench44/family-hub
mkdir -p "$R/src/family_hub/web/static"
echo 1.4.1 >"$R/VERSION"; echo log >"$R/CHANGELOG.md"; echo html >"$R/src/family_hub/web/static/index.html"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "allowed: release script"
echo 1.4.2 >"$R/VERSION"; echo code >"$R/app.py"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.2"
push origin main
assert_eq "$RC" 1
assert_contains "$OUT" "release: v1.4.2"
teardown

setup "allowlist is per repo: the same release commit in another repo is refused"
install
echo 1.4.1 >"$R/VERSION"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 1 "$OUT"
teardown

setup "repo name comes from a GitHub remote URL"
install
git -C "$R" config --unset ci-policy.repo
sshremote "git@github.com:drench44/family-hub.git"
echo 1.4.1 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin HEAD:main
assert_eq "$RC" 0 "$OUT"
echo x >"$R/n.md"; git -C "$R" add n.md; git -C "$R" commit -q --no-verify -m "tweak settings"
push origin HEAD:main
assert_eq "$RC" 1 "$OUT"
teardown

setup "break glass: CI_POLICY_ALLOW_MAIN_PUSH=1"
install
commit b.txt "fine"
OUT=$(CI_POLICY_ALLOW_MAIN_PUSH=1 git -C "$R" push origin main 2>&1); assert_eq "$?" 0 "$OUT"
assert_contains "$OUT" "WARNING"
teardown

setup "the repo pre-push hook still runs with the ref lines on stdin"
cat >"$R/.git/hooks/pre-push" <<EOF
#!/bin/sh
cat >"$T/prepush-stdin"
echo "\$1 \$2" >"$T/prepush-args"
EOF
chmod +x "$R/.git/hooks/pre-push"
install
commit b.txt "fine"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 0 "$OUT"
assert_contains "$(cat "$T/prepush-stdin" 2>/dev/null)" "refs/heads/feature"
assert_eq "$(cat "$T/prepush-args" 2>/dev/null)" "origin $T/origin.git"
teardown

setup "a broken install fails closed"
install
rm -rf "$HOME/.config/ci-policy/lib"
commit b.txt "fine"
assert_eq "$RC" 1
assert_contains "$OUT" "fail closed"
teardown

setup "a real force push of an allowlisted commit is still refused"
install
git -C "$R" config ci-policy.repo drench44/family-hub
echo 1.4.1 >"$R/VERSION"; git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 0 "$OUT"
git -C "$R" reset -q --hard HEAD~1
echo 1.4.2 >"$R/VERSION"; git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.2"
push --force origin main
assert_eq "$RC" 1
assert_contains "$OUT" "force push"
teardown

setup "an allowlisted push onto remote history this clone lacks is refused"
install
git -C "$R" config ci-policy.repo drench44/family-hub
git clone -q "$T/origin.git" "$T/other"
echo x >"$T/other/x.txt"; git -C "$T/other" add x.txt
git -C "$T/other" -c core.hooksPath=/dev/null commit -q -m other
git -C "$T/other" -c core.hooksPath=/dev/null push -q origin main
echo 1.4.1 >"$R/VERSION"; git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push --force origin main
assert_eq "$RC" 1
assert_contains "$OUT" "fetch first"
teardown

setup "creating master on a remote that already has main needs the allowlist"
install
commit b.txt "fine"
push origin HEAD:master
assert_eq "$RC" 1
assert_contains "$OUT" "creating master would land"
git -C "$R" reset -q --hard HEAD~1
push origin HEAD:master
assert_eq "$RC" 0 "(a commit the remote already has) $OUT"
teardown

setup "the allowlist is per branch: a main release rule does not open master"
install
git -C "$R" config ci-policy.repo drench44/family-hub
git -C "$R" push -q origin main:master 2>/dev/null || CI_POLICY_ALLOW_MAIN_PUSH=1 git -C "$R" push -q origin main:master 2>/dev/null
echo 1.4.1 >"$R/VERSION"; git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin HEAD:master
assert_eq "$RC" 1 "$OUT"
teardown

setup "ci-policy.repo cannot borrow another repo's allowlist for a GitHub remote"
install
git -C "$R" config ci-policy.repo drench44/family-hub
sshremote "git@github.com:drench44/demo.git"
echo 1.4.1 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin HEAD:main
assert_eq "$RC" 1 "$OUT"
teardown

setup "a broken or missing allowlist refuses pushes to main"
install
git -C "$R" config ci-policy.repo drench44/family-hub
echo 1.4.1 >"$R/VERSION"; git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
echo '{not json' >"$T/bad.json"
OUT=$(CI_POLICY_ALLOWLIST="$T/bad.json" git -C "$R" push origin main 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "cannot read the allowlist"
OUT=$(CI_POLICY_ALLOWLIST="$T/missing.json" git -C "$R" push origin main 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "missing"
teardown

setup "a failing repo pre-push hook blocks the push"
printf '#!/bin/sh\necho repo-pre-push-says-no >&2\nexit 1\n' >"$R/.git/hooks/pre-push"
chmod +x "$R/.git/hooks/pre-push"
install
commit b.txt "fine"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 1
assert_contains "$OUT" "repo-pre-push-says-no"
teardown

setup "a chain that loops back through the dispatcher stops at the depth guard"
install
mkdir -p "$T/loop"
printf '#!/bin/sh\nexec "%s" pre-commit "$@"\n' "$HOME/.config/ci-policy/git-hooks/dispatch" >"$T/loop/pre-commit"
chmod +x "$T/loop/pre-commit"
git -C "$R" config ci-policy.chainHooksPath "$T/loop"
commit b.txt "fine"
assert_eq "$RC" 1
assert_contains "$OUT" "nested"
teardown

setup "no python means fail closed; a missing chain dir is reported"
install
OUT=$(cd "$R" && echo z >z.txt && git add z.txt && CI_POLICY_PYTHON=/nonexistent/python3 git commit -m z 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "not found"
git -C "$R" reset -q
git -C "$R" config ci-policy.chainHooksPath "$T/gone"
commit b.txt "fine"
assert_eq "$RC" 0
assert_contains "$OUT" "does not exist"
teardown

setup "a non-executable repo hook is ignored with a note, like git does"
printf '#!/bin/sh\nexit 1\n' >"$R/.git/hooks/commit-msg"
install
commit b.txt "fine"
assert_eq "$RC" 0
assert_contains "$OUT" "not set as executable"
teardown

setup "type change from symlink to file and a -diff attribute cannot hide a secret"
install
tok="gh""p_$(printf 'B%.0s' {1..36})"
ln -s a.txt "$R/link"; git -C "$R" add link; git -C "$R" commit -q -m link --no-verify
rm "$R/link"; printf "TOKEN='%s'\n" "$tok" >"$R/link"; git -C "$R" add link
OUT=$(git -C "$R" commit -m typechange 2>&1); assert_eq "$?" 1 "(type change)"
git -C "$R" reset -q --hard
printf 'conf.yml -diff\n' >"$R/.gitattributes"; git -C "$R" add .gitattributes
git -C "$R" commit -q -m attrs --no-verify
printf "TOKEN='%s'\n" "$tok" >"$R/conf.yml"; git -C "$R" add conf.yml
OUT=$(git -C "$R" commit -m conf 2>&1); assert_eq "$?" 1 "(-diff attribute)"
assert_contains "$OUT" "GitHub token"
teardown

setup "a commit cannot exempt itself by adding linguist-generated in the same commit"
install
printf '* linguist-generated\n' >"$R/.gitattributes"; git -C "$R" add .gitattributes
commit c.md "a ${EMDASH} b"
assert_eq "$RC" 1
teardown

setup "server-side hooks of a bare repo still run through the chain"
install
printf '#!/bin/sh\ncat >/dev/null\ntouch "%s/post-receive-ran"\n' "$T" >"$T/origin.git/hooks/post-receive"
chmod +x "$T/origin.git/hooks/post-receive"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 0 "$OUT"
[[ -f "$T/post-receive-ran" ]] && ok || fail "bare repo post-receive did not run"
[[ ! -e "$HOME/.config/git/hooks/push-to-checkout" ]] && ok || fail "push-to-checkout wrapper must not exist"
teardown

# ---------------------------------------------------------------- scope + uninstall

setup "scoped install only covers repos under the scope"
mkdir -p "$HOME/workspace"
install --scope "$HOME/workspace"
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 0 "(outside the scope nothing runs)"
W="$HOME/workspace/w"; git init -q "$W"
printf 'a %s b\n' "$EMDASH" >"$W/x.txt"; git -C "$W" add x.txt
OUT=$(git -C "$W" commit -m x 2>&1); assert_eq "$?" 1 "(inside the scope the policy runs)"
OUT=$(bash "$SRC/install/install-git-hooks.sh" --scope "$HOME/workspace" --check 2>&1)
assert_eq "$?" 0 "$OUT"
teardown

setup "uninstall restores repo hooks and removes everything"
mkdir -p "$R/.githooks"; git -C "$R" config core.hooksPath .githooks
install
install --uninstall
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
assert_eq "$(git -C "$R" config --local core.hooksPath)" ".githooks"
assert_eq "$(git -C "$R" config --local ci-policy.chainHooksPath)" ""
[[ ! -e "$HOME/.config/ci-policy" && ! -e "$HOME/.config/git/hooks" ]] && ok || fail "files left behind"
teardown

setup "uninstall of a scoped install removes the includeIf"
mkdir -p "$HOME/workspace"
install --scope "$HOME/workspace"
install --scope "$HOME/workspace" --uninstall
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global --get-regexp '^includeif' || true)" ""
teardown

setup "a repo cannot shadow the policy code with its own ci_policy package"
install
mkdir -p "$R/ci_policy"; : >"$R/ci_policy/__init__.py"
printf 'import sys\nsys.exit(0)\n' >"$R/ci_policy/local_hooks.py"
printf 'raise SystemExit(0)\n' >"$R/json.py"
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 1 "(the em dash must still be refused: $OUT)"
teardown


# ---- tidy ----------------------------------------------------------------
# A "GitHub" clone G merges and deletes branches on the shared remote. R's
# origin URL looks like GitHub (insteadOf maps it to the bare repo) so the gh
# lookup has an owner/name to ask about; a fake gh on PATH answers it.
GHURL="https://github.com/drench44/test-repo.git"
tidy_setup() {
  setup "$1"
  install
  export CI_POLICY_TIDY_NO_GH=1 CI_POLICY_ALLOW_MAIN_PUSH=1
  git config --global "url.$T/origin.git.insteadOf" "$GHURL"
  git -C "$R" remote set-url origin "$GHURL"
  git -C "$R" fetch -q
  git -C "$R" branch -q -u origin/main main
  G="$T/g"; git clone -q "$T/origin.git" "$G" 2>/dev/null
  mkdir -p "$T/bin"
  cat >"$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_GH_LOG"
[[ -n "${FAKE_GH_SLEEP:-}" ]] && sleep "$FAKE_GH_SLEEP"
printf '%s' "${FAKE_GH_OUT:-[]}"
exit "${FAKE_GH_RC:-0}"
EOF
  chmod 755 "$T/bin/gh"
  export FAKE_GH_LOG="$T/gh.log"
}
# branch NAME: two commits on a new branch in R, pushed with an upstream.
branch() {
  git -C "$R" switch -q -c "$1" main
  printf '%s\n' "$1" >"$R/$1.txt"; git -C "$R" add "$1.txt"; git -C "$R" commit -q -m "$1 one"
  printf '%s two\n' "$1" >>"$R/$1.txt"; git -C "$R" commit -q -am "$1 two"
  git -C "$R" push -q -u origin "$1" 2>/dev/null
  git -C "$R" switch -q main
}
has_branch() { git -C "$R" rev-parse -q --verify "refs/heads/$1" >/dev/null; }
tip() { git -C "$R" rev-parse "refs/heads/$1"; }
g_squash() { git -C "$G" fetch -q; git -C "$G" merge -q --squash "origin/$1" && git -C "$G" commit -q -m "$1 (#1)"; }
g_merge() { git -C "$G" fetch -q; git -C "$G" merge -q --no-ff -m "Merge $1" "origin/$1"; }
g_push() { git -C "$G" push -q origin main 2>/dev/null; }
g_delete() { git -C "$G" push -q origin --delete "$@" 2>/dev/null; }
# advance: a new commit on main, so the next pull really merges (post-merge
# does not run on "Already up to date").
advance() { git -C "$G" pull -q 2>/dev/null; echo "$RANDOM" >>"$G/main.txt"; git -C "$G" add main.txt; git -C "$G" commit -q -m advance; g_push; }
pull() { OUT=$(git -C "$R" pull -q 2>&1); RC=$?; }
# g_moved: one unrelated commit on G's main, so cherry-picks get a new parent
# (a pick onto the same parent in the same second is the identical commit).
g_moved() { git -C "$G" fetch -q; echo "$RANDOM" >>"$G/main-moved.txt"; git -C "$G" add main-moved.txt; git -C "$G" commit -q -m "main moved"; }

tidy_setup "tidy: a pull deletes merged and squash-merged branches, keeps the rest"
assert_eq "$(git config --global fetch.prune)" "true"
branch merged; branch squashed; branch abandoned; branch open; branch inworktree
git -C "$R" switch -q -c localonly; git -C "$R" switch -q main
git -C "$R" branch -q --track tracksmain main
git -C "$R" worktree add -q "$T/wt" inworktree 2>/dev/null
g_merge merged; g_squash squashed; g_squash inworktree; g_push
g_delete merged squashed abandoned inworktree
ABANDONED=$(tip abandoned)
pull; assert_eq "$RC" 0 "$OUT"
! has_branch merged && ok || fail "merged branch kept"
! has_branch squashed && ok || fail "squash-merged branch kept"
assert_eq "$(tip abandoned)" "$ABANDONED" "(abandoned branch must be untouched)"
has_branch open && ok || fail "branch with a live remote deleted"
has_branch localonly && ok || fail "never-pushed branch deleted"
has_branch tracksmain && ok || fail "branch tracking a local branch deleted"
has_branch inworktree && ok || fail "branch checked out in a worktree deleted"
assert_contains "$OUT" "deleted merged (merged; restore with: git branch merged "
assert_contains "$OUT" "deleted squashed (squash-merged;"
assert_contains "$OUT" "kept abandoned: its remote branch is gone but its work is not in origin/main"
assert_not_contains "$OUT" "inworktree"
assert_not_contains "$OUT" "localonly"
# The next pull stays quiet about the abandoned branch (same tip)...
advance; pull
assert_not_contains "$OUT" "abandoned"
# ...but git tidy always reports, and frees the worktree branch once released.
OUT=$(git -C "$R" tidy 2>&1)
assert_contains "$OUT" "kept abandoned"
assert_contains "$OUT" "kept inworktree: it is checked out"
git -C "$R" worktree remove "$T/wt"
advance; pull
! has_branch inworktree && ok || fail "released worktree branch kept: $OUT"
teardown

tidy_setup "tidy: never deletes work that did not land"
branch extra; branch extramerge; branch partial; branch edited
g_squash extra; g_merge extramerge
g_moved
git -C "$G" cherry-pick "$(git -C "$R" rev-parse partial~1)" >/dev/null || fail "setup: cherry-pick"
git -C "$G" fetch -q; git -C "$G" merge -q --squash origin/edited
echo tweak >>"$G/edited.txt"; git -C "$G" add edited.txt; git -C "$G" commit -q -m "edited (#4)"
g_push; g_delete extra extramerge partial edited
git -C "$R" switch -q extra; echo more >>"$R/extra.txt"; git -C "$R" commit -q -am "extra three"
git -C "$R" switch -q extramerge; echo more >>"$R/extramerge.txt"; git -C "$R" commit -q -am "extramerge three"
git -C "$R" switch -q main
E1=$(tip extra) E2=$(tip extramerge) E3=$(tip partial) E4=$(tip edited)
pull; assert_eq "$RC" 0 "$OUT"
assert_eq "$(tip extra)" "$E1" "(a local commit after a squash merge must keep the branch)"
assert_eq "$(tip extramerge)" "$E2" "(a local commit after a merge must keep the branch)"
assert_eq "$(tip partial)" "$E3" "(half a branch landing must keep it)"
assert_eq "$(tip edited)" "$E4" "(a squash edited on merge needs GitHub's word; none here)"
teardown

tidy_setup "tidy: squash merge that main built on top of still counts"
branch feat
g_squash feat; printf 'later\n' >>"$G/feat.txt"; git -C "$G" commit -q -am later; g_push; g_delete feat
pull
! has_branch feat && ok || fail "squash-merged branch kept after later main commits: $OUT"
teardown

tidy_setup "tidy: GitHub's word covers rebase merges, only at the exact tip and base"
unset CI_POLICY_TIDY_NO_GH; export PATH="$T/bin:$PATH"
branch rebased; branch moved; branch otherbase; branch flaky
g_moved
for b in rebased moved otherbase flaky; do
  git -C "$G" fetch -q; git -C "$G" cherry-pick "origin/$b~1" "origin/$b" >/dev/null || fail "setup: cherry-pick $b"
done
g_push; g_delete rebased moved otherbase flaky
git -C "$R" switch -q moved; echo more >>"$R/moved.txt"; git -C "$R" commit -q -am "moved three"
git -C "$R" switch -q main
OLD_MOVED=$(git -C "$R" rev-parse moved~1)
cat >"$T/bin/gh" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"\$FAKE_GH_LOG"
case "\$*" in
  *"--head rebased "*) echo '[{"headRefOid":"$(tip rebased)","baseRefName":"main"}]' ;;
  *"--head moved "*) echo '[{"headRefOid":"$OLD_MOVED","baseRefName":"main"}]' ;;
  *"--head otherbase "*) echo '[{"headRefOid":"$(tip otherbase)","baseRefName":"stack-base"}]' ;;
  *"--head flaky "*) [[ -e "$T/gh-ok" ]] && echo '[{"headRefOid":"$(tip flaky)","baseRefName":"main"}]' && exit 0; exit 1 ;;
esac
EOF
pull; assert_eq "$RC" 0 "$OUT"
! has_branch rebased && ok || fail "rebase-merged branch kept despite GitHub: $OUT"
assert_contains "$OUT" "deleted rebased (PR merged;"
has_branch moved && ok || fail "branch with a commit after the merged PR tip deleted"
has_branch otherbase && ok || fail "branch merged into a non-default base deleted"
has_branch flaky && ok || fail "branch deleted although gh failed"
assert_contains "$(cat "$T/gh.log")" "--repo drench44/test-repo"
assert_not_contains "$OUT" "flaky"
# gh failed for flaky, so it was not remembered: asked again, and now it works.
touch "$T/gh-ok"; advance; pull
! has_branch flaky && ok || fail "branch not rechecked after gh recovered: $OUT"
# moved and otherbase were judged at this tip already: not asked again.
: >"$T/gh.log"; advance; pull
assert_not_contains "$(cat "$T/gh.log")" "--head moved "
OUT=$(git -C "$R" tidy --no-gh --dry-run 2>&1); assert_contains "$OUT" "kept moved"
teardown

tidy_setup "tidy: a slow GitHub cannot stall a pull"
unset CI_POLICY_TIDY_NO_GH; export PATH="$T/bin:$PATH" FAKE_GH_SLEEP=30
branch slow1; branch slow2; branch slow3; branch slow4
g_delete slow1 slow2 slow3 slow4
advance
START=$(date +%s); pull; ELAPSED=$(( $(date +%s) - START ))
assert_eq "$RC" 0 "$OUT"
(( ELAPSED < 30 )) && ok || fail "pull took ${ELAPSED}s with a hanging gh"
for b in slow1 slow2 slow3 slow4; do has_branch $b && ok || fail "$b deleted on a gh timeout"; done
unset FAKE_GH_SLEEP
teardown

# ubranch NAME: like branch, but pushed the way our tooling does, with no
# upstream (an explicit HEAD:refs/heads/NAME refspec).
ubranch() {
  git -C "$R" switch -q -c "$1" main
  printf '%s\n' "$1" >"$R/$1.txt"; git -C "$R" add "$1.txt"; git -C "$R" commit -q -m "$1 one"
  printf '%s two\n' "$1" >>"$R/$1.txt"; git -C "$R" commit -q -am "$1 two"
  git -C "$R" push -q origin "HEAD:refs/heads/$1" 2>/dev/null
  git -C "$R" switch -q main
}

tidy_setup "tidy: unlinked branches need a squash landing or commits of their own"
ubranch usq; ubranch umg; ubranch ulive; ubranch uextra; ubranch urebased
git -C "$R" branch -q ufresh main
git -C "$R" switch -q -c uwip; echo wip >"$R/uwip.txt"; git -C "$R" add uwip.txt
git -C "$R" commit -q -m wip; git -C "$R" switch -q main
g_squash usq; g_merge umg; g_squash ulive; g_squash uextra
g_moved
git -C "$G" fetch -q; git -C "$G" cherry-pick origin/urebased~1 origin/urebased >/dev/null || fail "setup: cherry-pick"
g_push; g_delete usq umg uextra urebased
git -C "$R" switch -q uextra; echo more >>"$R/uextra.txt"; git -C "$R" commit -q -am "uextra three"
git -C "$R" switch -q main
F1=$(tip ufresh) F2=$(tip uwip) F3=$(tip ulive) F5=$(tip uextra) F6=$(tip urebased)
pull; assert_eq "$RC" 0 "$OUT"
! has_branch usq && ok || fail "squash-merged unlinked branch kept: $OUT"
assert_contains "$OUT" "deleted usq (squash-merged;"
! has_branch umg && ok || fail "merged unlinked branch with its own commits kept: $OUT"
assert_contains "$OUT" "deleted umg (merged;"
assert_eq "$(tip ufresh)" "$F1" "(a branch fresh from main must stay)"
assert_eq "$(tip uwip)" "$F2" "(a never-pushed branch must stay)"
assert_eq "$(tip ulive)" "$F3" "(a branch still on origin must stay)"
assert_eq "$(tip uextra)" "$F5" "(a commit after the squash landed must keep it)"
assert_eq "$(tip urebased)" "$F6" "(the hook does not ask GitHub about unlinked branches)"
assert_not_contains "$OUT" "kept"
OUT=$(git -C "$R" tidy --no-gh 2>&1)
assert_contains "$OUT" "left 4 local branches alone: no proof their work is in the default branch"
# git tidy asks GitHub about unlinked branches, at the exact tip only.
unset CI_POLICY_TIDY_NO_GH; export PATH="$T/bin:$PATH"
export FAKE_GH_OUT="[{\"headRefOid\":\"$F6\",\"baseRefName\":\"main\"}]"
OUT=$(git -C "$R" tidy 2>&1)
! has_branch urebased && ok || fail "rebase-merged unlinked branch kept despite GitHub: $OUT"
assert_contains "$OUT" "deleted urebased (PR merged;"
for b in ufresh uwip uextra; do has_branch $b && ok || fail "$b deleted when gh answered for another tip"; done
unset FAKE_GH_OUT
teardown

tidy_setup "tidy: squash matches are exact (whitespace, empty changes)"
printf 'a = 1\n' >"$R/w.txt"; git -C "$R" add w.txt; git -C "$R" commit -q -m w
git -C "$R" push -q origin main 2>/dev/null; git -C "$G" pull -q 2>/dev/null
git -C "$R" switch -q -c uspace
printf 'a=1\n' >"$R/w.txt"; git -C "$R" commit -q -am "tighten"
git -C "$R" switch -q -c unet main
echo tmp >"$R/tmp.txt"; git -C "$R" add tmp.txt; git -C "$R" commit -q -m tmp
git -C "$R" rm -q tmp.txt; git -C "$R" commit -q -m "drop tmp"
git -C "$R" switch -q main
printf 'a  =  1\n' >"$G/w.txt"; git -C "$G" commit -q -am "respace"
git -C "$G" commit -q --allow-empty -m "retrigger CI"
g_push
S1=$(tip uspace) S2=$(tip unet)
pull; assert_eq "$RC" 0 "$OUT"
assert_eq "$(tip uspace)" "$S1" "(a whitespace-different change did not land)"
assert_eq "$(tip unet)" "$S2" "(a branch with no net change matched an empty commit)"
teardown

tidy_setup "tidy: a branch judged without gh is judged again once gh works"
branch lrebased
g_moved
git -C "$G" fetch -q; git -C "$G" cherry-pick origin/lrebased~1 origin/lrebased >/dev/null || fail "setup: cherry-pick"
g_push; g_delete lrebased
pull
has_branch lrebased && ok || fail "rebase-merged branch deleted with no gh"
assert_contains "$OUT" "kept lrebased"
unset CI_POLICY_TIDY_NO_GH; export PATH="$T/bin:$PATH"
LT=$(tip lrebased)
export FAKE_GH_OUT="[{\"headRefOid\":\"$LT\",\"baseRefName\":\"main\"}]"
advance; pull
! has_branch lrebased && ok || fail "branch remembered as unmerged after gh became available: $OUT"
unset FAKE_GH_OUT
teardown

tidy_setup "tidy: main is never deleted, even when its upstream is gone and merged"
git init -q --bare "$T/fork.git"
git -C "$R" remote add fork "$T/fork.git"
git -C "$R" push -q fork main:trunk main:main 2>/dev/null
git -C "$T/fork.git" symbolic-ref HEAD refs/heads/trunk
git -C "$R" fetch -q fork; git -C "$R" remote set-head fork trunk >/dev/null
git -C "$R" branch -q -u fork/main main
git -C "$R" switch -q -c elsewhere
git -C "$R" push -q fork --delete main 2>/dev/null
git -C "$R" fetch -q fork
OUT=$(git -C "$R" tidy 2>&1)
has_branch main && ok || fail "main deleted: $OUT"
teardown

tidy_setup "tidy: the current branch is kept, opt out, dry run, git tidy, uninstall"
branch done1; branch here
g_merge done1; g_merge here; g_push; g_delete done1 here
git -C "$R" config ci-policy.tidy false
pull
has_branch done1 && ok || fail "opted-out repo was tidied"
OUT=$(git -C "$R" tidy --dry-run 2>&1); assert_eq "$?" 0 "$OUT"
assert_contains "$OUT" "would delete done1"
assert_contains "$OUT" "would delete here"
has_branch done1 && ok || fail "dry run deleted"
git -C "$R" switch -q here
OUT=$(git -C "$R" tidy 2>&1)
assert_contains "$OUT" "kept here: it is checked out"
assert_contains "$OUT" "deleted done1"
has_branch here && ok || fail "current branch deleted"
! has_branch done1 && ok || fail "git tidy kept a merged branch"
git -C "$R" switch -q main
OUT=$(git -C "$R" tidy 2>&1); assert_contains "$OUT" "deleted here"
OUT=$(git -C "$R" tidy 2>&1); assert_contains "$OUT" "nothing to clean up"
install --uninstall
assert_eq "$(git config --global --get alias.tidy || true)" ""
teardown

tidy_setup "tidy: a pull succeeds and keeps branches when the tidy cannot run"
branch x; g_merge x; g_push; g_delete x
OUT=$(CI_POLICY_PYTHON=/nonexistent/python3 git -C "$R" pull -q 2>&1); assert_eq "$?" 0 "$OUT"
has_branch x && ok || fail "branch deleted with no python"
printf '#!/bin/sh\nexit 3\n' >"$T/bin/crashpy"; chmod 755 "$T/bin/crashpy"
advance
OUT=$(CI_POLICY_PYTHON="$T/bin/crashpy" git -C "$R" pull -q 2>&1); assert_eq "$?" 0 "$OUT"
has_branch x && ok || fail "branch deleted by a crashing tidy"
teardown

tidy_setup "tidy: the repo's own post-merge hook still runs, and tidy runs once"
branch y; g_merge y; g_push; g_delete y
mkdir -p "$R/.git/hooks"
printf '#!/bin/sh\necho ran >>"%s/repo-hook"\n' "$T" >"$R/.git/hooks/post-merge"
chmod 755 "$R/.git/hooks/post-merge"
pull
assert_eq "$(cat "$T/repo-hook" 2>/dev/null)" "ran"
! has_branch y && ok || fail "merged branch kept with a chained hook"
assert_eq "$(printf '%s\n' "$OUT" | grep -c 'deleted y')" "1"
teardown

tidy_setup "tidy: a repo cannot shadow the tidy code"
branch z; g_merge z; g_push; g_delete z
mkdir -p "$R/ci_policy"; : >"$R/ci_policy/__init__.py"
printf 'open("%s/pwned", "w").write("x")\n' "$T" >"$R/ci_policy/local_hooks.py"
printf 'open("%s/pwned", "w").write("x")\n' "$T" >"$R/json.py"
export PYTHONPATH="$R"
pull
[[ ! -e "$T/pwned" ]] && ok || fail "repo code ran from the post-merge hook"
! has_branch z && ok || fail "real tidy did not run: $OUT"
OUT=$(git -C "$R" tidy 2>&1)
[[ ! -e "$T/pwned" ]] && ok || fail "repo code ran from git tidy"
unset PYTHONPATH
teardown

setup "install keeps a chosen fetch.prune and a foreign alias.tidy"
git config --global fetch.prune false
git config --global alias.tidy "!echo mine"
install; assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "alias.tidy is already"
assert_eq "$(git config --global fetch.prune)" "false"
install; assert_eq "$RC" 0 "(reinstall: $OUT)"
install --uninstall
assert_eq "$(git config --global alias.tidy)" "!echo mine"
teardown

# ---- leak check: a push to a public repo may not name a private repo -------
# A fake gh lists acme's repos: "pubapp" public, "secret-proj" and "shed"
# private. R's origin is git@github.com:acme/pubapp (a fake ssh serves it from
# the bare repo).
leak_setup() {
  setup "$1"
  install
  mkdir -p "$T/bin"
  cat >"$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
[[ -n "${FAKE_GH_FAIL:-}" ]] && exit 1
case "$*" in
  "repo list acme"*)
    def='[{"name":"pubapp","visibility":"PUBLIC"},{"name":"secret-proj","visibility":"PRIVATE"},{"name":"shed","visibility":"PRIVATE"}]'
    printf '%s' "${FAKE_REPOS:-$def}" ;;
  "api repos/acme/"*" --jq .private")
    # Live visibility: private unless named in FAKE_NOW_PUBLIC.
    name=${2#repos/acme/}
    case " ${FAKE_NOW_PUBLIC:-pubapp} " in *" $name "*) echo false ;; *) echo true ;; esac ;;
  *) exit 1 ;;
esac
EOF
  chmod 755 "$T/bin/gh"
  export PATH="$T/bin:$PATH"
  sshremote "git@github.com:acme/pubapp.git"
  git -C "$R" config --unset ci-policy.repo
  git config --global --add ci-policy.privateOwner acme
  git config --global --add ci-policy.leakLoose shed
  OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
}
# lcommit FILE CONTENT MESSAGE: a commit on a new branch $B (w-<file>).
lcommit() {
  B="w-${1%%.*}"
  git -C "$R" switch -q -C "$B" main
  printf '%s\n' "$2" >"$R/$1"; git -C "$R" add -- "$1"
  git -C "$R" commit -q --no-verify -m "$3"
}

leak_setup "leak: refresh writes the private list, mode 600, outside any repo"
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$HOME/.config/ci-policy/private-names.txt")" "$(printf 'secret-proj\nshed')"
assert_eq "$(stat -c %a "$HOME/.config/ci-policy/private-names.txt" 2>/dev/null || stat -f %Lp "$HOME/.config/ci-policy/private-names.txt")" "600"
teardown

leak_setup "leak: a commit message, an added line or a branch name naming a private repo is refused"
lcommit a.txt "plain" "port the fix from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "names secret-proj"
lcommit b.txt "see Secret-Proj for details" "docs"
push origin "$B"
assert_eq "$RC" 1 "(case-insensitive added line: $OUT)"
lcommit c.txt "plain" "plain"
push origin HEAD:refs/heads/secret-proj-port
assert_eq "$RC" 1 "(branch name: $OUT)"
assert_contains "$OUT" "branch name"
lcommit d.txt "plain" "plain"
push origin HEAD:refs/heads/port_secret-proj
assert_eq "$RC" 1 "(underscore-joined branch name: $OUT)"
lcommit e.txt "plain" "the secret-proj-style layout"
push origin "$B"
assert_eq "$RC" 1 "(dash-joined word: $OUT)"
lcommit f.txt "plain" "secretproj and secret-projector are other words"
push origin "$B"
assert_eq "$RC" 0 "(a longer word is not the name: $OUT)"
teardown

leak_setup "leak: clean pushes, the repo's own name and removed lines pass"
lcommit a.txt "the pubapp wall" "pubapp: tidy"
push origin "$B"
assert_eq "$RC" 0 "$OUT"
printf 'mentions secret-proj\n' >"$R/old.txt"; git -C "$R" add old.txt
CI_POLICY_ALLOW_LEAK=1 git -C "$R" commit -q --no-verify -m "x"
CI_POLICY_ALLOW_LEAK=1 git -C "$R" push -q origin "$B" 2>/dev/null
git -C "$R" rm -q old.txt; git -C "$R" commit -q --no-verify -m "drop old notes"
push origin "$B"
assert_eq "$RC" 0 "(a removed line is not a leak: $OUT)"
teardown

leak_setup "leak: a loose name counts only as a repo reference"
lcommit a.txt "the shed door is open" "shed temperature card"
push origin "$B"
assert_eq "$RC" 0 "(the plain word: $OUT)"
lcommit b.txt "plain" "ported from acme/shed"
push origin "$B"
assert_eq "$RC" 1 "(owner/name: $OUT)"
lcommit c.txt "plain" "see shed PR #58"
push origin "$B"
assert_eq "$RC" 1 "(name PR: $OUT)"
lcommit d.txt "plain" "shed#12 follow-up"
push origin "$B"
assert_eq "$RC" 1 "(name#N: $OUT)"
teardown

leak_setup "leak: pushes to a private repo are not scanned"
export FAKE_REPOS='[{"name":"pubapp","visibility":"PRIVATE"},{"name":"secret-proj","visibility":"PRIVATE"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1)
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 0 "$OUT"
unset FAKE_REPOS
teardown

leak_setup "leak: fails closed without a list, breaks glass for humans"
rm -f "$HOME/.config/ci-policy/private-names.txt"
export FAKE_GH_FAIL=1
lcommit a.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "ci-policy refresh-private"
lcommit b.txt "plain" "plain"
OUT=$(CI_POLICY_ALLOW_LEAK=1 git -C "$R" push origin "$B" 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "WARNING"
unset FAKE_GH_FAIL
teardown

leak_setup "leak: an unknown GitHub repo counts as public; a stale list is refreshed"
sshremote "git@github.com:other/thing.git"
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "(not in the cache: $OUT)"
touch -t 202001010000 "$HOME/.config/ci-policy/private-names.txt"
# A repo made private since: a day-old list is refreshed before judging.
export FAKE_REPOS='[{"name":"secret-proj","visibility":"PRIVATE"},{"name":"shed","visibility":"PRIVATE"},{"name":"newproj","visibility":"PRIVATE"}]'
lcommit b.txt "plain" "port from newproj"
push origin "$B"
assert_eq "$RC" 1 "(a day-old list is refreshed before judging: $OUT)"
assert_contains "$OUT" "names newproj"
# A name that stops being private stays on the list, with a warning.
export FAKE_REPOS='[{"name":"shed","visibility":"PRIVATE"},{"name":"newproj","visibility":"PRIVATE"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "no longer listed as private"
assert_contains "$(cat "$HOME/.config/ci-policy/private-names.txt")" "secret-proj"
unset FAKE_REPOS
teardown

leak_setup "leak: any remote that is not a local path is scanned (ssh aliases, ports, other hosts)"
# An ssh host alias that does not resolve to github.com: visibility unknown,
# so it counts as public.
sshremote "git@gh-alias:acme/secret-proj.git"
lcommit a.txt "plain" "notes from shed PR #3"
push origin "$B"
assert_eq "$RC" 1 "(ssh alias: $OUT)"
assert_contains "$OUT" "names shed"
# GitHub over ssh on port 443 is still GitHub: a private repo is not scanned...
sshremote "ssh://git@ssh.github.com:443/acme/secret-proj.git"
push origin "$B"
assert_eq "$RC" 0 "(ssh.github.com:443 to a private repo: $OUT)"
# ...and a public one is.
sshremote "ssh://git@ssh.github.com:443/acme/pubapp.git"
lcommit b.txt "plain" "notes from shed PR #4"
push origin "$B"
assert_eq "$RC" 1 "(ssh.github.com:443 to a public repo: $OUT)"
# Another host altogether.
sshremote "ssh://git@git.example.net/acme/pubapp.git"
push origin "$B"
assert_eq "$RC" 1 "(another host: $OUT)"
# A path on this machine is never scanned.
git -C "$R" remote set-url origin "file://$T/origin.git"
push origin "$B"
assert_eq "$RC" 0 "(file:// remote: $OUT)"
teardown

leak_setup "leak: file paths, -diff files and annotated tag messages are scanned"
lcommit a.txt "plain" "plain"
mkdir -p "$R/notes"; printf 'plain\n' >"$R/notes/secret-proj.md"
git -C "$R" add notes; git -C "$R" commit -q --no-verify -m "add notes"
push origin "$B"
assert_eq "$RC" 1 "(a file path: $OUT)"
assert_contains "$OUT" "names secret-proj"
lcommit b.txt "plain" "plain"
printf '*.dat -diff\n' >"$R/.gitattributes"; printf 'see secret-proj\n' >"$R/blob.dat"
git -C "$R" add .gitattributes blob.dat; git -C "$R" commit -q --no-verify -m "data"
push origin "$B"
assert_eq "$RC" 1 "(content of a -diff file: $OUT)"
lcommit c.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 0 "(clean branch: $OUT)"
git -C "$R" tag -a v1 -m "ported from secret-proj"
push origin v1
assert_eq "$RC" 1 "(annotated tag message: $OUT)"
assert_contains "$OUT" "tag"
git -C "$R" tag -a v2 -m "clean release"
push origin v2
assert_eq "$RC" 0 "(clean tag: $OUT)"
teardown

leak_setup "leak: an empty list, a refresh finding nothing private, a stale list all fail closed"
: >"$HOME/.config/ci-policy/private-names.txt"
lcommit a.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 1 "(empty list: $OUT)"
assert_contains "$OUT" "empty list"
export FAKE_REPOS='[{"name":"pubapp","visibility":"PUBLIC"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 1 "(refresh with no private repos: $OUT)"
assert_contains "$OUT" "found no private repos"
unset FAKE_REPOS
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1)
touch -t 202001010000 "$HOME/.config/ci-policy/private-names.txt"
export FAKE_GH_FAIL=1
push origin "$B"
assert_eq "$RC" 1 "(a list older than 7 days that cannot be refreshed: $OUT)"
assert_contains "$OUT" "days old"
touch "$HOME/.config/ci-policy/private-names.txt"
push origin "$B"
assert_eq "$RC" 0 "(a fresh list needs no refresh: $OUT)"
unset FAKE_GH_FAIL
teardown

leak_setup "leak: unsetting the git config alone does not turn the check off"
assert_eq "$(cat "$HOME/.config/ci-policy/owners.txt")" "acme"
git config --global --unset-all ci-policy.privateOwner
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "(owners.txt still names acme: $OUT)"
install
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$HOME/.config/ci-policy/owners.txt" 2>/dev/null)" "acme" "(a reinstall keeps it)"
push origin "$B"
assert_eq "$RC" 1 "(after a reinstall: $OUT)"
teardown

setup "push URLs: a fork, a backup host and differing allowlists"
install
sshremote "git@github.com:them/proj.git"
git -C "$R" config --unset ci-policy.repo
# A fork: fetch from them, push to me. Only the push URL counts.
git -C "$R" remote set-url --push origin "git@github.com:me/proj.git"
git -C "$R" switch -q -c work
commit b.txt "fine"
push origin work
assert_eq "$RC" 0 "(fork: $OUT)"
# A second push URL on another host (a backup) changes nothing.
git -C "$R" remote set-url --add --push origin "ssh://git@backup.example.net/me/proj.git"
commit c.txt "fine"
push origin work
assert_eq "$RC" 0 "(backup push URL: $OUT)"
# Two GitHub push URLs whose allowlists differ: a push to main is refused.
git -C "$R" config --unset-all remote.origin.pushurl
git -C "$R" remote set-url --push origin "git@github.com:drench44/family-hub.git"
git -C "$R" remote set-url --add --push origin "git@github.com:acme/other.git"
git -C "$R" switch -q main
echo 1.4.1 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "allowlists differ"
teardown

setup "ci-policy.repo is ignored for a non-local remote and refused as a sha256 key"
install
git -C "$R" config ci-policy.repo "sha256:$(printf '%064d' 0)"
echo 1.4.1 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "plain owner/name"
git -C "$R" config ci-policy.repo drench44/family-hub
sshremote "git@gh-alias:drench44/family-hub.git"
push origin HEAD:main
assert_eq "$RC" 1 "(an unknown host gets no allowlist: $OUT)"
teardown

leak_setup "leak: a message line that looks like a diff header hides nothing"
lcommit a.txt "plain" "$(printf 'notes\n\ndiff --git a/x b/x\nported from secret-proj')"
push origin "$B"
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "names secret-proj"
teardown

leak_setup "leak: an evil merge, a rename-only new name and a non-ASCII path are scanned"
lcommit a.txt "plain" "side"
git -C "$R" switch -q -c base main
printf 'base\n' >"$R/base.txt"; git -C "$R" add base.txt; git -C "$R" commit -q --no-verify -m base
git -C "$R" merge -q --no-ff --no-commit w-a 2>/dev/null
printf 'only the merge says secret-proj\n' >>"$R/a.txt"; git -C "$R" add a.txt
git -C "$R" commit -q --no-verify -m "merge side"
push origin HEAD:refs/heads/merged
assert_eq "$RC" 1 "(a line only the merge adds: $OUT)"
lcommit b.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 0 "$OUT"
git -C "$R" mv b.txt secret-proj.txt; git -C "$R" commit -q --no-verify -m "rename"
push origin "$B"
assert_eq "$RC" 1 "(a rename to a private name: $OUT)"
lcommit c.txt "plain" "plain"
mkdir -p "$R/notes"; printf 'plain\n' >"$R/notes/caf$(printf '\303\251')-secret-proj.md"
git -C "$R" add notes; git -C "$R" commit -q --no-verify -m "notes"
push origin "$B"
assert_eq "$RC" 1 "(a non-ASCII path: $OUT)"
teardown

leak_setup "leak: lightweight tags are scanned by name and by the commits they bring"
lcommit a.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 0 "$OUT"
git -C "$R" tag secret-proj
push origin secret-proj
assert_eq "$RC" 1 "(a tag named after a private repo: $OUT)"
assert_contains "$OUT" "refs/tags/secret-proj"
git -C "$R" switch -q -C w-b main
printf 'x\n' >"$R/b.txt"; git -C "$R" add b.txt; git -C "$R" commit -q --no-verify -m "port of secret-proj"
git -C "$R" tag v9
push origin v9
assert_eq "$RC" 1 "(a lightweight tag on a new commit: $OUT)"
teardown

leak_setup "leak: a failed refresh within 7 days warns, scans the old list and trusts no PRIVATE"
python3 -c 'import os, sys, time; t = time.time() - 3 * 86400; os.utime(sys.argv[1], (t, t))' \
  "$HOME/.config/ci-policy/private-names.txt"
export FAKE_GH_FAIL=1
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "(scanned with the old list: $OUT)"
assert_contains "$OUT" "WARNING"
lcommit b.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 0 "(a clean push still goes, with a warning: $OUT)"
assert_contains "$OUT" "could not refresh"
# secret-proj is PRIVATE in the cache, but the last refresh failed: scan.
sshremote "git@github.com:acme/secret-proj.git"
lcommit c.txt "plain" "notes from shed PR #2"
push origin "$B"
assert_eq "$RC" 1 "(a private verdict from a failed refresh is not trusted: $OUT)"
unset FAKE_GH_FAIL
teardown

leak_setup "leak: an overridden ssh command is never trusted to reach a private repo"
sshremote "git@github.com:acme/secret-proj.git"
lcommit a.txt "plain" "notes from shed PR #5"
push origin "$B"
assert_eq "$RC" 0 "(plain ssh to a private repo: $OUT)"
git -C "$R" config core.sshCommand "$T/bin/ssh"
lcommit b.txt "plain" "notes from shed PR #6"
push origin "$B"
assert_eq "$RC" 1 "(core.sshCommand may connect anywhere: $OUT)"
git -C "$R" config --unset core.sshCommand
teardown

leak_setup "leak: only a configured owner's GitHub repo may name itself"
sshremote "git@github.com:other/secret-proj.git"
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "(another owner's repo of the same name: $OUT)"
teardown

leak_setup "leak: an unreadable owners.txt refuses instead of turning the check off"
chmod 000 "$HOME/.config/ci-policy/owners.txt"
git config --global --unset-all ci-policy.privateOwner
lcommit a.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "cannot tell whether the private repo check is on"
chmod 600 "$HOME/.config/ci-policy/owners.txt"
teardown

setup "pre-push refuses a ref line it cannot read"
install
OUT=$(printf 'garbage line\n' | (cd "$R" && "$HOME/.config/ci-policy/bin/ci-policy" pre-push origin "$T/origin.git") 2>&1); RC=$?
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "cannot read the ref line"
teardown

setup "no allow rule passes a commit that touches .github"
install
printf '{"repos": {"drench44/test-repo": [{"name": "rel", "subject": "^release: "}]}}\n' >"$T/allow.json"
export CI_POLICY_ALLOWLIST="$T/allow.json"
mkdir -p "$R/.github"; echo x >"$R/.github/x.yml"
git -C "$R" add .github; git -C "$R" commit -q --no-verify -m "release: v2"
push origin main
assert_eq "$RC" 1 "(.github: $OUT)"
git -C "$R" reset -q --hard HEAD~1
echo 2 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v2"
push origin main
assert_eq "$RC" 0 "(the same rule without .github: $OUT)"
unset CI_POLICY_ALLOWLIST
teardown

leak_setup "leak: a mixed push is scanned for every destination that is not proven private"
sshremote "git@github.com:acme/secret-proj.git"
git -C "$R" remote set-url --add --push origin "git@github.com:acme/secret-proj.git"
git -C "$R" remote set-url --add --push origin "git@github.com:acme/pubapp.git"
lcommit a.txt "plain" "port from secret-proj"
push origin "$B"
assert_eq "$RC" 1 "(private plus public: the public one gets the name: $OUT)"
git -C "$R" config --unset-all remote.origin.pushurl
git -C "$R" remote set-url --add --push origin "git@github.com:acme/secret-proj.git"
git -C "$R" remote set-url --add --push origin "ssh://git@backup.example.net/acme/secret-proj.git"
push origin "$B"
assert_eq "$RC" 1 "(private plus another host: $OUT)"
git -C "$R" config --unset-all remote.origin.pushurl
git -C "$R" remote set-url --add --push origin "git@github.com:acme/secret-proj.git"
git -C "$R" remote set-url --add --push origin "$T/origin.git"
push origin "$B"
assert_eq "$RC" 0 "(private plus a local path: $OUT)"
teardown

leak_setup "leak: a private fetch URL does not vouch for what a public push URL has"
mkdir -p "$T/hosts/acme"
git init -q --bare "$T/hosts/acme/pubapp.git"
git -C "$R" push -q "$T/hosts/acme/pubapp.git" main 2>/dev/null
export FAKE_SSH_ROOT="$T/hosts"
# origin's fetch URL is the private repo, which has a commit naming shed.
sshremote "git@github.com:acme/secret-proj.git"
lcommit a.txt "plain" "notes from shed PR #9"
git -C "$R" push -q --no-verify origin "$B" 2>/dev/null
git -C "$R" fetch -q origin
# Pushing the same commit to the public repo must still scan it.
git -C "$R" remote set-url --push origin "git@github.com:acme/pubapp.git"
push origin "$B:refs/heads/copy"
assert_eq "$RC" 1 "(a commit only the private repo has: $OUT)"
assert_contains "$OUT" "names shed"
unset FAKE_SSH_ROOT
teardown

leak_setup "leak: a repo made public since the last refresh is asked about live"
sshremote "git@github.com:acme/secret-proj.git"
python3 -c 'import os, sys, time; t = time.time() - 1800; os.utime(sys.argv[1], (t, t))' \
  "$HOME/.config/ci-policy/visibility.json"
lcommit a.txt "plain" "notes from shed PR #7"
push origin "$B"
assert_eq "$RC" 0 "(still private, asked live: $OUT)"
export FAKE_NOW_PUBLIC="pubapp secret-proj"
lcommit b.txt "plain" "notes from shed PR #8"
push origin "$B"
assert_eq "$RC" 1 "(made public half an hour ago: $OUT)"
unset FAKE_NOW_PUBLIC
teardown

leak_setup "leak: a corrupt or odd visibility list proves nothing"
sshremote "git@github.com:acme/secret-proj.git"
export FAKE_GH_FAIL=1
for bad in '{not json' '["acme/secret-proj"]' '{"acme/secret-proj": "Private"}'; do
  printf '%s\n' "$bad" >"$HOME/.config/ci-policy/visibility.json"
  lcommit a.txt "plain" "notes from shed PR #1"
  push origin "$B"
  assert_eq "$RC" 1 "(visibility $bad: $OUT)"
done
unset FAKE_GH_FAIL
teardown

leak_setup "leak: GIT_SSH_COMMAND or GIT_SSH means the destination is not proven"
sshremote "git@github.com:acme/secret-proj.git"
lcommit a.txt "plain" "notes from shed PR #3"
OUT=$(GIT_SSH_COMMAND="$T/bin/ssh" git -C "$R" push origin "$B" 2>&1); RC=$?
assert_eq "$RC" 1 "(GIT_SSH_COMMAND: $OUT)"
OUT=$(GIT_SSH="$T/bin/ssh" git -C "$R" push origin "$B" 2>&1); RC=$?
assert_eq "$RC" 1 "(GIT_SSH: $OUT)"
teardown

leak_setup "leak: refresh refuses a list that may be cut short, odd or suddenly empty"
export FAKE_REPOS="$(python3 -c 'import json; print(json.dumps([{"name": "r%d" % i, "visibility": "PRIVATE"} for i in range(1000)]))')"
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 1 "(1000 repos, the limit: $OUT)"
assert_contains "$OUT" "cut short"
export FAKE_REPOS='[{"name":"shed","visibility":"PRIVATE"},{"visibility":"PRIVATE"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 1 "(an entry without a name: $OUT)"
export FAKE_REPOS='[{"name":"shed","visibility":"SECRET"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 1 "(an unknown visibility: $OUT)"
export FAKE_REPOS='[{"name":"pubapp","visibility":"PUBLIC"}]'
OUT=$("$HOME/.config/ci-policy/bin/ci-policy" refresh-private 2>&1); RC=$?
assert_eq "$RC" 1 "(an owner that had private repos now has none: $OUT)"
assert_contains "$OUT" "private last time"
unset FAKE_REPOS
teardown

leak_setup "leak: an unreadable name list or a broken tag refuses"
chmod 000 "$HOME/.config/ci-policy/private-names.txt"
lcommit a.txt "plain" "plain"
push origin "$B"
assert_eq "$RC" 1 "(unreadable list: $OUT)"
assert_contains "$OUT" "cannot read"
chmod 600 "$HOME/.config/ci-policy/private-names.txt"
printf '\377\376bad\n' >"$HOME/.config/ci-policy/private-names.txt"
push origin "$B"
assert_eq "$RC" 1 "(a list that is not UTF-8: $OUT)"
teardown

setup "main: a bad commit in the middle of a push is refused even when the tip is allowed"
install
printf '{"repos": {"drench44/test-repo": [{"name": "rel", "subject": "^release: ", "paths": ["VERSION"]}]}}\n' >"$T/allow.json"
export CI_POLICY_ALLOWLIST="$T/allow.json"
echo 1 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v1"
echo x >"$R/hack.txt"; git -C "$R" add hack.txt; git -C "$R" commit -q --no-verify -m "sneak in"
echo 2 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v2"
push origin main
assert_eq "$RC" 1 "(middle commit: $OUT)"
assert_contains "$OUT" "sneak in"
assert_not_contains "$OUT" "release: v2  (allowed"
git -C "$R" reset -q --hard origin/main
# A merge: the side branch carries a commit no rule allows.
git -C "$R" switch -q -c side
echo y >"$R/side.txt"; git -C "$R" add side.txt; git -C "$R" commit -q --no-verify -m "side change"
git -C "$R" switch -q main
echo 3 >"$R/VERSION"; git -C "$R" add VERSION; git -C "$R" commit -q --no-verify -m "release: v3"
git -C "$R" merge -q --no-ff --no-verify -m "release: v4" side 2>/dev/null
push origin main
assert_eq "$RC" 1 "(a bad side-parent commit: $OUT)"
assert_contains "$OUT" "side change"
unset CI_POLICY_ALLOWLIST
teardown

setup "leak: off on a machine with no owner configured"
install
sshremote "git@github.com:acme/pubapp.git"
git -C "$R" switch -q -c work
echo x >"$R/a.txt"; git -C "$R" add a.txt; git -C "$R" commit -q --no-verify -m "port from secret-proj"
push origin work
assert_eq "$RC" 0 "$OUT"
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
