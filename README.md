# ci-shared

Shared CI policy (the `ci-policy` tool) for drench44 repos. GitHub Free cannot enforce branch
protection on private repos, so these pieces do the job instead: checks that
run on every PR, a watchdog that alerts when something reaches main without a
green PR, local guards that stop pushes to main before they happen, and one
deploy script with a real health gate for the homelab services.

| Piece | Where | What it does |
| --- | --- | --- |
| PR policy | `.github/workflows/pr-policy.yml` (reusable) | Band stated, regression fix has a test, size limit |
| Main watch | `.github/workflows/main-watch.yml` (reusable) | Issue (emailed) when a commit reaches main without a merged, all-green PR |
| Main watch audit | `lib/ci_policy/main_watch_audit.py`, run hourly from a private repo | Issue (emailed) when main-watch never concluded on a push, for example while a self-hosted runner is down |
| Allowlist | `policy/main-allowlist.json` | The only commits allowed on main without a PR. Read by main watch, the git pre-push hook, and the Claude Code push guard |
| Git hooks | `git-hooks/dispatch`, `install/install-git-hooks.sh`, `lib/ci_policy/branch_tidy.py` | pre-commit: no em dashes, no secrets. pre-push: no pushes to main/master. post-merge (`git pull`): delete local branches whose PR merged (also `git tidy`; opt out with `git config ci-policy.tidy false`). Chains to each repo's own hooks |
| Deploy | `deploy/homelab-deploy`, `deploy/deploy-lib.sh` | Tag, deploy, real health check, automatic rollback, rollback point in git |

Everything is Python standard library and bash, so it runs on GitHub's
runners, self-hosted runners, macOS (stock bash 3.2) and Linux.

## PR policy

Add `examples/pr-policy.yml` to a repo as `.github/workflows/pr-policy.yml`.
Private repos pass their self-hosted runner labels, for example
`runs-on: '["self-hosted"]'`; public repos pass `'"ubuntu-latest"'`.

Rules, for PRs opened by `drench44` (other people and bots pass with a notice):

1. **Review band.** The body says which band ran, in any wording the PR hook
   already produces: `Review band: one agent (code-reviewer), ordinary code`,
   `All three review agents ran`, `Review-band: 0 (docs only)`.
2. **Regression fixes need a test.** If the title says "regression"/"regressed",
   or the body says it fixes one ("fixes a regression from #88", "broken in
   #1089"), some test file must be added or changed. "No regressions",
   "regression risk" and "regression suite" do not count as a claim. Rare real
   exception: a `Regression-test: <why none>` line or the `no-regression-test`
   label (turns the failure into a warning).
3. **Size.** Changed lines, not counting lockfiles, generated, vendored,
   snapshot and fixture files, or anything `.gitattributes` marks
   `linguist-generated` or `linguist-vendored`. Over 800: warning. Over 2,500:
   fails unless the `size-override` label is on the PR or the body has a
   `Size-override: <reason>` line.

The job summary explains every result. The check re-runs when the body or
labels change.

## Main watch

Add `examples/main-watch.yml` as `.github/workflows/main-watch.yml` (branch
`master` for repos that use it). Pin it to a ci-policy commit by full SHA and
pass the same SHA as `ci-policy-ref`. On every push to the default branch,
each new commit must match `policy/main-allowlist.json` (the only allow list:
there is no per-caller `allow-rules` input, since rules a watched repo passed
in would let a push allow itself) or have reached the branch through a merged
PR whose head commit was all green:

- **Every** check run and commit status on the PR head concluded `success`,
  `neutral` or `skipped` (GitHub's own rule for required checks). One failure
  anywhere, or a workflow that could not start (a check suite that failed
  before creating any check run), is not green. Runs from pushes to the
  default branch itself (a fast-forward merge shares the sha) are not the
  PR's gate and are left out, as is main-watch's own status. Apps that
  register a check suite and never report (the Claude app does this on every
  PR) are ignored.
- Only the newest run of each check counts, per workflow file (like GitHub's
  required checks): a pr-policy run that failed and was re-run green after
  the body was fixed is green. Without `actions: read` main-watch cannot tell
  workflows apart, warns, and judges every run on its own (strict).
- At least one of the PR's own checks passed (skipped-only proves nothing;
  the shared `pr-policy` and `main-watch` jobs must be green but do not
  count, since they run on every PR), when the repo has any active workflow
  besides main-watch (`require-checks: auto`, the default; `true` or `false`
  to force it). The newest run of a workflow that was cancelled before any
  job ran is not green.
- Every name in the optional `required-checks` input (one exact check run
  name or status context per line, for example `Vitest (full suite)`) is
  present and green. A missing required check is not green.
- `ignore-checks` leaves checks out by name (globs). A glob that ignores
  every check (`*`) is refused unless `required-checks` names the checks that
  must still pass, and however the globs are written, a PR head where they
  left no check to judge is not green.
- **A new branch.** The push event lists at most 20 commits, so a branch
  created with 20 or more, or with none listed, is an alert.
- **Stacked PRs.** A commit of a child PR that was merged into its parent's
  branch (base: another branch, not main) reaches main inside the parent PR.
  main-watch finds that carrier (a PR merged into main in the same push whose
  head contains the commit, or by walking up the child's base branches) and
  judges the carrier's head, the code that actually landed.

Anything else, and any force push or branch deletion, opens one issue labeled
`main-watch` (later alerts comment on it). GitHub emails the owner. It never
reverts anything.

**Checks still running at merge time.** Automation that merges without
waiting (scheduled bots) can merge a PR while its
Vitest job is still queued. That is not a failure yet, so main-watch waits:
the commit gets a pending status, and the caller's hourly scheduled run
reads the branch's 50 newest commits and re-reads the checks of every one
still pending. It keeps paging until a whole page is older than the
pending timeout plus a day and holds no pending commit (a quiet page alone
is not enough), up to 500 commits; stopping there early is an alert. Green turns the status
green; a failure, or checks still not done `pending-timeout-minutes` (default
180) after the merge, flags it like any other. The `PR #N` in a pending
status is only a hint: the re-check uses PR N only when it was merged into
the watched branch and the commit is its merge commit, its head, or one of
its own commits; otherwise it judges the commit from scratch. It does not wait inside the
job: on a private repo the job may hold the repo's only self-hosted runner, the very
runner the queued check needs.

**Commit status.** Every commit main-watch judges gets a
`ci-policy/main-watch` status: success, failure or pending, linking to the
run. A flagged commit whose alert issue could not be opened stays pending, so
the re-check judges it again and retries the issue (and the run is red). The audit below uses it to tell "judged" from "never ran". Callers must
grant `statuses: write` and `actions: read` (the reusable workflow asks for
them, and a called workflow cannot get more than its caller grants).

**This repo's own main watch** runs with `ci-policy-ref` pinned to the commit
before the push (`github.event.before`; the scheduled re-check uses the first
parent of main's tip), so a push cannot change the code or allowlist it is
judged by. The limit: `uses: ./.github/workflows/main-watch.yml` is always
read from the pushed commit (GitHub allows no expression there), so a push
that rewrites that workflow file still runs its own version. Nothing inside
the repo can close that; the audit catches it instead, since any push that
changes a file under `.github/workflows/` is reported.

## Main watch audit

main-watch runs on each repo's own runner. If a self-hosted runner is down the
job sits queued (GitHub drops it after a day) and nobody hears about it.
`lib/ci_policy/main_watch_audit.py` covers that from outside: run hourly on a
GitHub-hosted runner, it reads a list of watched repos (a JSON file with each
repo's branch, caller workflow and `since`; see `load_repos`) and lists the
pushes to the watched branch from the last 72 hours (the repository activity
API). Each push tip older than 6 hours must carry a final
`ci-policy/main-watch` status set by GitHub Actions (creator
`github-actions[bot]`, read from the commit's full status list) whose link
points at a run of the watched workflow in that repo, on the watched branch,
started by the push (with that push's tip as its head) or by the schedule
(with a head that contains the commit). A final status needs a finished run
(success, or failure for a flag), and the run's commit must not have changed
that workflow file. Every workflow posts as `github-actions[bot]`, so the run
is what proves it. A manual `workflow_dispatch` re-check does not count for
the audit; let the scheduled run conclude instead. A status anyone or anything else set does not count, and neither does a main-watch run that left
no status (callers pinned to a main-watch too old to set one must update). A
commit inside a push (not its tip) still marked pending is reported too, and
so is any push that changes a file under `.github/workflows/` (it can change
or switch off main-watch itself), with one exception: a pure ci-shared pin
bump, landed as the merge (or squash) commit of a PR merged into the watched
branch with green checks. Green checks prove nothing about a rigged
workflow, so "pure" is checked on the whole file before and after: the only
lines that differ are job-level `uses: drench44/ci-shared/...@<sha>` lines
and that job's `with: ci-policy-ref:`, one new sha for the file, that sha is
on ci-shared's main, and the file uses no YAML anchors, aliases, merge keys,
extra documents or tabs. Anything else, a renamed, added or removed
workflow, a file list that may be cut short (100 or more files), or a file
that cannot be read, is reported. Only GitHub's own facts count for the PR:
it is merged, its merge commit is the push tip, its base is the watched
branch, every other commit in the push is one of the PR's own commits, and
every check run on the merged head passed (at least one from GitHub
Actions). Such a merge's own main-watch status is trusted too. A commit
inside a push with no final main-watch status (missing, forged or pending)
is reported. A rebase merge (new commit ids) or a
branch's first push is never vetted this way and is reported for a look. A
push that cannot be compared with the branch before it (a new branch, a lost
old tip, too many files) counts as a possible workflow change, a push whose
commits cannot all be read is reported, and so is a push with no readable
time. The push list must be read in full, and a push that cannot be read
turns the run red like a repo the token cannot read. The repos file is
checked too: a `since` in the future, a window the grace period empties, a
watched branch that does not exist, or a branch tip that moved inside the
window with no push listed is a problem; the lookback may be at most a year.

**Heartbeat.** The audit cannot report its own silence (a disabled schedule,
a broken token). In the repo that runs it, pass `audit-workflow:
main-watch-audit.yml` to that repo's main-watch: its hourly scheduled run
then alerts when the audit has not succeeded for `audit-max-age-hours`
(default 48). A problem that stays after its issue is closed is not
reported again; that is a known limit. Problems open one issue labeled
`main-watch-audit` in the repo running it (GitHub emails it), or comment on
the open one. Text copied into the issue cannot open or close an HTML
comment, and the "already reported" keys are read only from issues and
comments the audit itself wrote (`github-actions[bot]`, so run it with the
workflow's own token). A standing condition (a repo the token cannot read, a
missing or expiring token) is reported again whenever no audit issue is open.

The audit and its repo list live in a PRIVATE repo, so the list of watched
repos is never public. It reads the other repos with a fine-grained,
read-only personal access token (Metadata, Contents, Commit statuses,
Actions) and warns 14 days before the token expires.

### The allowlist

This file is public, so it holds public repos only. A private repo is never
in it, not even as a hash (a guess at a name could confirm it). Its rules
live in two private places instead:

- `.github/main-allowlist.json` in the repo itself (`{"rules": [...]}`, same
  rule shape), read by main-watch as it was at the push's old tip, and used
  only when main-watch passed that old tip. A change to it touches
  `.github/`, so it can only land through a PR.
- `~/.config/ci-policy/private-allowlist.json` on each machine that pushes
  to it (same shape as this file, with the plain repo name; your own file,
  mode 600), read by the git hooks. A reinstall keeps it.

Lookups take only a plain owner/name.

A rule can limit files two ways: `paths` (every changed file must match) and
`not_paths` (no changed file may match). Whatever a rule says, no commit that
touches `.github/**` is ever allowed without a PR, and a commit whose file
list GitHub truncated (300 files or more) never matches.

| Repo | Automation | Rule |
| --- | --- | --- |
| family-hub | `scripts/release.py`, pushed by hand with `git push --follow-tags` | subject `release: vX.Y.Z`, only `VERSION`, `CHANGELOG.md`, `src/family_hub/web/static/index.html` |
| house-climate | `scripts/release.py`, same flow | subject `release: vX.Y.Z`, only `VERSION`, `CHANGELOG.md` and the three pages it stamps: `src/house_climate/web/static/index.html`, `moisture.html`, `square.html` (a new page needs a line here) |

Every other automation pushes feature branches and lands through PRs
(scheduled bots, Dependabot). Nothing else
commits to main. Change the list through a PR here; all three guards pick it
up (reinstall the git hooks to refresh the local copy).

## Git hooks (every machine)

```sh
install/install-git-hooks.sh                    # Mac: every repo
install/install-git-hooks.sh --scope ~/workspace  # a shared box: only the dev clones
install/install-git-hooks.sh --check            # report problems, exit 1 if any
install/install-git-hooks.sh --uninstall        # undo, restoring repo hooks
```

The installer copies the code to `~/.config/ci-policy`, writes a wrapper for
every git hook name into `~/.config/git/hooks`, and points `core.hooksPath` at
it. On a shared box the scope keeps automation clones (timers that commit as
the same user outside `~/workspace`) untouched.

- **pre-commit** reads a file git calls binary as text for the secret scan
  (UTF-16 included) and refuses a text file over 5 MB it cannot scan. It
  blocks an em dash in any added line, a secret-shaped string
  (GitHub, AWS, Anthropic, OpenAI, Slack, Stripe, Google, Supabase and npm
  keys, private keys, JWTs such as Home Assistant tokens), and staged files
  named like secrets (`.env`, `id_rsa`, `*.p12`, ...; `.env.example` is fine).
  Escape hatches: `ci-policy: allow-secret` on a line with a fake key;
  `git config --add ci-policy.emdashAllow '<glob>'` or `linguist-vendored` for
  vendored upstream files; `git config --add ci-policy.secretAllow '<glob>'`.
- **pre-push** refuses any push that updates or deletes `main`/`master`,
  including `HEAD:main`, `refs/heads/main` and force pushes, unless every
  pushed commit is on the allowlist. Creating `main` on an empty remote (a new
  repo) is allowed. Humans can break glass with `CI_POLICY_ALLOW_MAIN_PUSH=1`.
  The repo comes from where the push really goes: `git remote get-url --push
  --all` (the pushurls, or the url when there are none, after `insteadOf` and
  `pushInsteadOf` rewriting) plus the URL git hands the hook. A fetch-only url
  never counts, so a fork (fetch from them, push to you) works, and so does a
  second push URL on a backup host. Only GitHub push URLs pick the allowlist;
  a push to main whose GitHub push URLs name repos with different allowlists
  is refused. `git config ci-policy.repo owner/name` only applies when every
  push URL is a path on this machine (test remotes); a `sha256:` value is
  refused. A host that is not GitHub (and an ssh alias that `ssh -G` does not
  resolve to github.com) gets no allowlist. Creating `main` is allowed only
  on a remote that is really empty (checked at the push URL). A ref line from
  git that cannot be read refuses the push.
- **Leak check** (on with `--private-owner <owner>`): unless every push URL
  is a path on this machine or a GitHub repo known to be private, the push
  may not name one of the owner's private repos in commit messages, added
  lines (read as text even when `.gitattributes` says `-diff`; a merge is
  diffed against its first parent), the paths of added, changed or renamed
  files, the ref name, or an annotated tag's message. A host whose
  visibility cannot be looked up counts as public, and the "known private"
  pass needs a URL that provably reaches GitHub: an ssh URL qualifies only
  when `ssh -G` shows GitHub with no proxy and git is not told to run another
  ssh command (`GIT_SSH_COMMAND`, `GIT_SSH`, `core.sshCommand`); a failing,
  missing or slow `ssh -G` proves nothing. "Private" is confirmed live with
  GitHub unless the visibility list is under 10 minutes old, so a repo made
  public since the last refresh is scanned. With several push URLs, each one
  not proven private is scanned for. A repo may name itself only when every
  such destination is that one GitHub repo of a configured owner. Which
  commits a new branch brings is read from the remote-tracking refs only
  when the push goes where the fetch came from; otherwise the destination
  is asked (or the whole history is scanned). The pre-push hook
  runs it; the Claude Code guard (`allow-check`) does not. The lists live only on the machine,
  in `~/.config/ci-policy` (mode 600): `private-names.txt`, `visibility.json`
  and `owners.txt`, refreshed daily by the hook. It fails closed: a missing,
  empty or unreadable name list, a refresh that finds no private repo, or a
  list older than 7 days that cannot be refreshed refuses the push until
  `ci-policy refresh-private` works. A refresh refuses a list that may be cut
  short (as many repos as its limit), an entry without a usable name and
  visibility, and an owner whose private repos all vanished; a name that
  stops being private stays on the list, with a warning. A push with no push
  URL, or a tag that cannot be read, is refused. A refresh that fails on a younger list warns, scans
  with the old list, and trusts none of its "private" verdicts. Owners are
  `ci-policy.privateOwner` in the global git config plus `owners.txt`, so
  unsetting the git config alone does not turn it off (a reinstall keeps the
  file; `--uninstall` removes it); when either cannot be read, the push is
  refused. Humans
  can break glass for one push with `CI_POLICY_ALLOW_LEAK=1`.
- **Chaining.** `core.hooksPath` makes git ignore `.git/hooks`, which would
  silently turn off each repo's own hooks. Every wrapper runs the repo's hook
  of the same name after the policy passes, with the same arguments and stdin.
  Repos that set their own `core.hooksPath` (family-hub and house-climate use
  `.githooks`) would otherwise
  shadow the global setting, so the installer moves that value to
  `ci-policy.chainHooksPath` and the dispatcher chains there. `--check` finds
  any repo that sets `core.hooksPath` again later (for example by rerunning
  its own `scripts/install-hooks.sh`, or `npm install` re-running husky);
  rerun the installer to fix it.
  `CI_POLICY_DEBUG=1 git hook run pre-commit` shows which repo hook a repo
  chains to.

A vendored upstream file can be exempt from the em dash check per machine
(`ci-policy.emdashAllow`); marking it `linguist-vendored` in the repo's
`.gitattributes` makes the exception travel with the repo.

## Deploy

`deploy/homelab-deploy <config>` runs one deploy from a per-repo config file
(`deploy/example.deploy.conf`; every setting is documented at the top of
`deploy/deploy-lib.sh`):

1. Refuses a shallow health gate. The config must name fields `/health` has
   to carry (`DL_HEALTH_REQUIRE`) and/or a data timestamp that must be recent
   (`DL_HEALTH_FRESH`). An endpoint that only says ok is how a deploy deleted
   a box's `.env` and a card vanished for a day.
2. Records the rollback point: tags each running image
   `<image>:pre-<time>-<sha>`, where `<image>` is that service's own
   `image:` from `docker compose config --format json` (or
   `<project>-<service>` for a build-only service), never an image of a
   service it depends on. After a healthy deploy the newest
   `DL_SNAPSHOT_KEEP` (default 5) such tags per image are kept; only the
   exact `pre-<YYYYMMDDTHHMMSSZ>-<12 hex>` shape is pruned, so hand-made
   `pre-*` tags stay. It also snapshots the compose directory on the box, and
   pushes git tag `rollback-point/<service>/<time>` on the commit the box was
   running.
3. Runs the config's `dl_sync`, then checks required files and env values on
   the box (`DL_REQUIRE_FILES=".env"`, `DL_REQUIRE_ENV=".env:HA_TOKEN"`)
   before anything restarts.
4. `docker compose up -d --build`, then polls health, and checks once more
   after `DL_HEALTH_SETTLE` seconds. If the app can report its commit,
   `DL_HEALTH_COMMIT` proves the new version is the one answering. A config
   can add a check the JSON cannot make by defining `dl_health_extra` (for
   example a headless page load over the LAN IP, or the ports a container
   publishes); it runs only once the JSON passes, and it never replaces it.
5. Healthy: records the commit on the box, pushes `deploy/<service>/<time>-<sha>`.
   Unhealthy: restores the files and images, recreates without building,
   judges the old version again, pushes `failed-deploy/<service>/<time>-<sha>`.

A rollback is judged by what the old version proved before the deploy, never
by more. It is probed with the full gate first: if it passed, the rollback
must pass the full gate again; if not (a version from before the gate, no
deploy record, a source already down), the rollback must answer liveness
(`DL_LIVENESS_URL`, for example `/health`), plus `dl_health_extra` if that
passed before. The log names the gate used. "SERVICE IS DOWN" is printed only
when liveness does not answer.

Git tags are pushed only to a remote known to be private (a GitHub remote is
asked with `gh api`; a local path counts as private; another host needs
`DL_TAG_REMOTE_PRIVATE=1`), or the deploy stops before changing anything. A
public repo sets `DL_TAG_PUSH=local`: the record goes into the deploying clone
as refs under `refs/deploy-history/` (not tags, which a release's
`--follow-tags` push would publish; list them with
`git for-each-ref refs/deploy-history`).
Every outcome is also appended to `deploys.log` in the service's state dir on
the box, so the record exists either way.

Exit codes: 0 ok; 1 rolled back and the old version passes what it passed
before; 2 down, liveness fails (manual command printed); 3 refused before
restarting; 4 deployed but a tag could not be made or pushed; 5 up but not as
intended (the rollback is incomplete, or the old version answers liveness but
fails a check it passed before); 6 nothing restarted, but the files a failed
sync put on the box could not be taken back off (the next restart would use
them).
`homelab-deploy <config> --health` runs the gate once against what runs now.

## Pinning

Every third-party action in this repo's workflows is pinned to a full commit
SHA (a docker action to its image digest) with a comment naming the version,
and `tests/test_workflows.py` fails on anything else. Dependabot
(`.github/dependabot.yml`) proposes updates, which keep the comment in step.
Callers pin ci-policy itself the same way.

## Tests

```sh
cd tests && python3 -m unittest discover -s .   # policy, watch, audit, allowlist, workflows, hook checks
bash tests/deploy/run-tests.sh                  # deploy library (stubbed docker/ssh/curl)
bash tests/git-hooks/run-tests.sh               # installer, dispatcher, hooks, real git
```

CI runs all three on Ubuntu and macOS (stock bash 3.2), plus shellcheck,
actionlint and an em dash check. This repo also runs its own PR policy from
the commit under test, and its own main watch from the commit before the push.
