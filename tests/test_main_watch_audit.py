import base64
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import ActionsEnv, FakeGitHub
from ci_policy import gh, main_watch_audit as audit
from ci_policy.main_watch_audit import Watched, audit_repo

NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
R = "/repos/drench44/demo"
S1, S2, S3 = "1" * 40, "2" * 40, "3" * 40
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def push(sha, hours_ago, kind="pr_merge", id_=None):
    ts = (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": id_ or int(hours_ago * 10), "after": sha, "before": "0" * 39 + "9",
            "ref": "refs/heads/main", "timestamp": ts, "activity_type": kind}


BOT = {"login": "github-actions[bot]"}


RUN_URL = "https://github.com/drench44/demo/actions/runs/{}"


def mark(state, description="", creator=BOT, run=500):
    """The commit's status list, newest first, as /commits/{sha}/statuses returns it.
    Run 500 is a scheduled main-watch run (see watch_run)."""
    return [{"context": "other", "state": "success", "creator": BOT},
            {"context": "ci-policy/main-watch", "state": state, "description": description,
             "creator": creator, "target_url": RUN_URL.format(run)}]


TIP = "f" * 40          # main's tip when the scheduled re-check ran
WF = ".github/workflows/main-watch.yml"


def watch_run(event="schedule", head_sha=TIP, path=WF, branch="main", repo="drench44/demo",
              status="completed", conclusion="success"):
    return {"event": event, "head_sha": head_sha, "path": path, "head_branch": branch,
            "repository": {"full_name": repo}, "status": status, "conclusion": conclusion}


def with_runs(routes):
    """Every status links to run 500 (a scheduled main-watch run on TIP, which
    contains every commit and did not change the workflow) unless a test
    stubs its own."""
    routes = dict(routes)
    routes.setdefault(f"GET {R}/actions/runs/500", watch_run())
    routes.setdefault(f"GET {R}/compare/*", {"status": "ahead", "commits": [], "files": []})
    for sha in (TIP, S1, S2, S3):
        routes.setdefault(f"GET {R}/commits/{sha}", {"parents": [{"sha": "e" * 40}]})
    routes.setdefault(f"GET {R}/contents/{WF}", {"sha": "blob1"})
    # The branch tip predates the window, so an empty push list is fine.
    routes.setdefault(f"GET {R}/branches/main", {"commit": {
        "sha": "0" * 39 + "9", "commit": {"committer": {"date": "2026-01-01T00:00:00Z"}}}})
    return routes


def wf_runs(*runs):
    return {"workflow_runs": [dict({"path": ".github/workflows/main-watch.yml", "id": i,
                                    "run_attempt": 1}, **r) for i, r in enumerate(runs, 1)]}


W = Watched("drench44/demo", "main", NOW - dt.timedelta(days=30))


class AuditRepoTests(unittest.TestCase):
    def run_audit(self, routes, grace=6, lookback=72):
        return audit_repo(FakeGitHub(with_runs(routes)), W, NOW, grace, lookback)

    def test_concluded_pushes_are_fine(self):
        routes = {f"GET {R}/activity": [push(S1, 7), push(S2, 30)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/commits/{S2}/statuses": mark("failure")}
        r = self.run_audit(routes)
        self.assertEqual(r.problems, [])
        self.assertEqual(r.checked, 2)

    def test_push_with_no_conclusion_after_grace_is_a_problem(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": [],
                  f"GET {R}/actions/runs": wf_runs({"status": "queued"})}
        r = self.run_audit(routes)
        self.assertEqual([p.key for p in r.problems], [f"drench44/demo@{S1}"])
        self.assertIn("still queued", r.problems[0].text)
        self.assertIn("7.0 h after the push", r.problems[0].text)

    def test_no_run_at_all_and_cancelled_run_are_problems(self):
        for runs, text in [(wf_runs(), "no main-watch run exists"),
                           (wf_runs({"status": "completed", "conclusion": "cancelled"}),
                            "ended cancelled"),
                           (wf_runs({"status": "completed", "conclusion": "startup_failure"}),
                            "caller grant every permission"),
                           (wf_runs({"status": "completed", "conclusion": "action_required"}),
                            "ended action_required")]:
            with self.subTest(text=text):
                routes = {f"GET {R}/activity": [push(S1, 7)],
                          f"GET {R}/commits/{S1}/statuses": [],
                          f"GET {R}/actions/runs": runs}
                self.assertIn(text, self.run_audit(routes).problems[0].text)

    def test_runs_of_other_workflows_do_not_count(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": [],
                  f"GET {R}/actions/runs": wf_runs({"status": "completed", "conclusion":
                                                    "success", "path": ".github/workflows/ci.yml"})}
        self.assertEqual(len(self.run_audit(routes).problems), 1)

    def test_successful_run_without_status_is_not_proof(self):
        # Only the status counts; a green run with none proves nothing.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": [],
                  f"GET {R}/actions/runs": wf_runs(
                      {"status": "completed", "conclusion": "cancelled"},
                      {"status": "completed", "conclusion": "success", "run_attempt": 2})}
        (p,) = self.run_audit(routes).problems
        self.assertIn("ended success and left no status", p.text)

    def test_status_set_by_anyone_but_actions_does_not_count(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success", creator={"login": "mallory"}),
                  f"GET {R}/actions/runs": wf_runs()}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}")
        self.assertIn("no main-watch conclusion", p.text)

    def test_status_must_come_from_a_run_of_the_watched_workflow(self):
        # Every workflow posts as github-actions[bot]: the linked run decides.
        bad_runs = {"another workflow": watch_run(path=".github/workflows/ci.yml"),
                    "a pull_request run": watch_run(event="pull_request"),
                    "another branch": watch_run(branch="feature"),
                    "another repo": watch_run(repo="drench44/other"),
                    "a push run for another tip": watch_run(event="push", head_sha=S2),
                    "a manual dispatch": watch_run(event="workflow_dispatch"),
                    "a cancelled run": watch_run(conclusion="cancelled"),
                    "a run still going": watch_run(status="in_progress")}
        for why, bad in bad_runs.items():
            with self.subTest(why=why):
                routes = {f"GET {R}/activity": [push(S1, 7)],
                          f"GET {R}/commits/{S1}/statuses": mark("success", run=9),
                          f"GET {R}/actions/runs/9": bad,
                          f"GET {R}/actions/runs": wf_runs()}
                (p,) = self.run_audit(routes).problems
                self.assertIn("no main-watch conclusion", p.text)
        for why, st in {"no link": dict(mark("success")[1], target_url=""),
                        "a link to another repo": dict(
                            mark("success")[1],
                            target_url="https://github.com/drench44/other/actions/runs/500"),
                        "a run that does not exist": mark("success", run=404)[1]}.items():
            with self.subTest(why=why):
                routes = {f"GET {R}/activity": [push(S1, 7)],
                          f"GET {R}/commits/{S1}/statuses": [st],
                          f"GET {R}/actions/runs": wf_runs()}
                self.assertEqual(len(self.run_audit(routes).problems), 1)
        # A re-check whose head does not contain the commit.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{S1}...{TIP}": {"status": "diverged"},
                  f"GET {R}/actions/runs": wf_runs()}
        self.assertEqual(len(self.run_audit(routes).problems), 1)
        # A run whose own commit rewrote the workflow file it ran.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/contents/{WF}": lambda params: {
                      "sha": "new" if params["ref"] == TIP else "old"},
                  f"GET {R}/actions/runs": wf_runs()}
        self.assertEqual(len(self.run_audit(routes).problems), 1)
        # The push run for this very push counts.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success", run=9),
                  f"GET {R}/actions/runs/9": watch_run(event="push", head_sha=S1)}
        self.assertEqual(self.run_audit(routes).problems, [])

    def test_pending_status_from_a_running_watch_counts_and_a_forged_one_does_not(self):
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/statuses": mark("pending", "PR #5", run=9),
                  f"GET {R}/actions/runs/9": watch_run(status="in_progress", conclusion=None)}
        (p,) = self.run_audit(routes).problems
        self.assertIn("re-check is not running", p.text)
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/statuses": mark("pending", "PR #5",
                                                         creator={"login": "mallory"}),
                  f"GET {R}/actions/runs": wf_runs()}
        (p,) = self.run_audit(routes).problems
        self.assertIn("no main-watch conclusion", p.text)

    def test_new_branch_or_lost_old_tip_counts_as_a_possible_workflow_change(self):
        a = push(S1, 7)
        a["before"] = "0" * 40
        routes = {f"GET {R}/activity": [a], f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/commits/{S1}/pulls": []}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:workflows")
        self.assertIn("could not be compared", p.text)
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{'0' * 39 + '9'}...{S1}": gh.GitHubError("gone", 404),
                  f"GET {R}/commits/{S1}/pulls": []}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:workflows")

    def test_compare_is_paged_past_100_commits(self):
        before = "0" * 39 + "9"
        many = [{"sha": "%040x" % i} for i in range(1, 101)]

        def cmp(params):
            if params.get("page") == 2:
                return {"commits": [{"sha": S2}, {"sha": S1}]}
            return {"total_commits": 102, "commits": many, "files": [{"filename": "a.txt"}]}
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{before}...{S1}": cmp,
                  f"GET {R}/commits/{S2}/statuses": mark("pending", "PR #4")}
        for c in many:
            routes[f"GET {R}/commits/{c['sha']}/statuses"] = mark("success")
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S2}")

    def test_compare_that_stays_short_is_a_problem(self):
        before = "0" * 39 + "9"
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{before}...{S1}": lambda params: (
                      {"commits": []} if params.get("page") else
                      {"total_commits": 5, "commits": [{"sha": S1}],
                       "files": [{"filename": "a.txt"}]})}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:partial")

    def test_push_without_a_readable_time_is_a_problem(self):
        a = push(S1, 7)
        a["timestamp"] = "sometime"
        (p,) = self.run_audit({f"GET {R}/activity": [a]}).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:time")

    def test_activity_list_cut_short_is_a_read_problem(self):
        class Short(FakeGitHub):
            def paginate(self, path, params=None, item_key=None, limit=5000, complete=False):
                if path.endswith("/activity"):
                    self.calls.append(("GET", path, complete))
                    if complete:
                        raise gh.GitHubError("more than 1000 items", None)
                return super().paginate(path, params, item_key, limit)
        api = Short(with_runs({}))
        r = audit_repo(api, W, NOW, 6, 72)
        self.assertEqual([p.key for p in r.problems], ["drench44/demo:read"])

    def test_newest_actions_status_wins_over_a_later_forged_one(self):
        forged = {"context": "ci-policy/main-watch", "state": "success",
                  "creator": {"login": "mallory"}}
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/statuses": [forged] + mark("pending", "PR #5")}
        (p,) = self.run_audit(routes).problems
        self.assertIn("re-check is not running", p.text)

    def test_push_changing_a_workflow_is_a_problem(self):
        before = "0" * 39 + "9"
        for files, want in [([{"filename": ".github/workflows/main-watch.yml"}], "main-watch.yml"),
                            ([{"filename": "x.yml", "previous_filename":
                               ".github/workflows/ci.yml"}], "ci.yml"),
                            ([{"filename": "README.md"}] * 300, "too many files")]:
            with self.subTest(want=want):
                routes = {f"GET {R}/activity": [push(S1, 7)],
                          f"GET {R}/commits/{S1}/statuses": mark("success"),
                          f"GET {R}/compare/{before}...{S1}": {"commits": [{"sha": S1}],
                                                                "files": files}}
                (p,) = self.run_audit(routes).problems
                self.assertEqual(p.key, f"drench44/demo@{S1}:workflows")
                self.assertIn(want, p.text)
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{before}...{S1}": {"commits": [{"sha": S1}],
                                                        "files": [{"filename": "src/a.py"},
                                                                  {"filename": ".github/x.md"}]}}
        self.assertEqual(self.run_audit(routes).problems, [])

    def test_failed_run_without_status_is_not_proof(self):
        # A job killed by its timeout leaves a failed run and no status.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": [],
                  f"GET {R}/actions/runs": wf_runs({"status": "completed",
                                                    "conclusion": "failure"})}
        (p,) = self.run_audit(routes).problems
        self.assertIn("ended failure and left no status", p.text)

    def test_pending_commit_inside_a_push_is_found(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{'0' * 39 + '9'}...{S1}": {"commits": [
                      {"sha": S3}, {"sha": S2}, {"sha": S1}], "files": [{"filename": "a.txt"}]},
                  f"GET {R}/commits/{S3}/statuses": mark("success"),
                  f"GET {R}/commits/{S2}/statuses": mark("pending", "PR #4: waiting")}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S2}")
        self.assertIn("part of push", p.text)

    def test_one_unreadable_push_does_not_hide_the_others(self):
        routes = {f"GET {R}/activity": [push(S1, 7), push(S2, 8)],
                  f"GET {R}/commits/{S1}/statuses": gh.GitHubError("No commit found", 422),
                  f"GET {R}/commits/{S2}/statuses": [],
                  f"GET {R}/actions/runs": wf_runs()}
        keys = sorted(p.key for p in self.run_audit(routes).problems)
        self.assertEqual(keys, [f"drench44/demo@{S1}:error", f"drench44/demo@{S2}"])

    def test_deletion_without_an_id_keys_on_its_time(self):
        a = push("0" * 40, 7, kind="branch_deletion")
        a.pop("id")
        (p,) = self.run_audit({f"GET {R}/activity": [a]}).problems
        self.assertEqual(p.key, f"drench44/demo@deleted@{a['timestamp']}")

    def test_pending_past_grace_means_the_recheck_is_not_running(self):
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/statuses": mark("pending", "PR #5: waiting")}
        p = self.run_audit(routes).problems[0]
        self.assertIn("re-check is not running", p.text)
        self.assertIn("PR #5: waiting", p.text)

    def test_pushes_before_main_watch_existed_are_skipped(self):
        # Seen on the first dry run: every push from before the rollout had no
        # main-watch run, and would have flooded the first issue.
        w = Watched("drench44/demo", "main", NOW - dt.timedelta(hours=10))
        r = audit_repo(FakeGitHub(with_runs({f"GET {R}/activity": [push(S1, 11),
                                                                   push(S2, 30)]})),
                       w, NOW, 6, 72)
        self.assertEqual((r.checked, r.problems), (0, []))

    def test_window_skips_fresh_and_old_pushes(self):
        routes = {f"GET {R}/activity": [push(S1, 1), push(S2, 100)]}
        r = self.run_audit(routes)
        self.assertEqual((r.checked, r.problems), (0, []))

    def test_same_tip_pushed_twice_is_checked_once(self):
        routes = {f"GET {R}/activity": [push(S1, 7, id_=1), push(S1, 9, id_=2)],
                  f"GET {R}/commits/{S1}/statuses": mark("success")}
        self.assertEqual(self.run_audit(routes).checked, 1)

    def test_branch_deletion_is_a_problem(self):
        routes = {f"GET {R}/activity": [push("0" * 40, 7, kind="branch_deletion", id_=77)]}
        p = self.run_audit(routes).problems[0]
        self.assertEqual(p.key, "drench44/demo@deleted@77")

    def test_unreadable_repo_is_a_problem_naming_the_permissions(self):
        routes = {f"GET {R}/activity": gh.GitHubError("Not Found", 404)}
        p = self.run_audit(routes).problems[0]
        self.assertEqual(p.key, "drench44/demo:read")
        self.assertIn("Commit statuses", p.text)

    def test_compare_server_error_is_a_problem_not_a_pass(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/statuses": mark("success"),
                  f"GET {R}/compare/{'0' * 39 + '9'}...{S1}": gh.GitHubError("down", 502)}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:error")

    def test_long_lookback_asks_for_a_month(self):
        api = FakeGitHub(with_runs({f"GET {R}/activity": []}))
        audit_repo(api, W, NOW, 6, 24 * 10)
        (params,) = [c[2] for c in api.calls if c[1].endswith("/activity")]
        self.assertEqual(params["time_period"], "month")

    def test_activity_query(self):
        api = FakeGitHub(with_runs({f"GET {R}/activity": []}))
        audit_repo(api, W, NOW, 6, 72)
        (params,) = [c[2] for c in api.calls if c[1].endswith("/activity")]
        self.assertEqual(params, {"ref": "refs/heads/main", "time_period": "week"})


class LoadReposTests(unittest.TestCase):
    def load(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        try:
            return audit.load_repos(f.name)
        finally:
            os.unlink(f.name)

    def test_bad_files(self):
        ok = {"branch": "main", "since": "2026-09-23T14:43:00Z"}
        for data in [{}, {"repos": {}}, {"repos": {"nope": ok}},
                     {"repos": {"a/b": {}}},
                     {"repos": {"a/b": dict(ok, workflow="x.yml")}},
                     {"repos": {"a/b": {"branch": "main"}}},                       # no since
                     {"repos": {"a/b": dict(ok, since="yesterday")}},
                     {"repos": {"a/b": dict(ok, since="2026-09-23T14:43:00")}}]:  # no zone
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)


class ValidationTests(unittest.TestCase):
    def test_a_sha_that_is_not_a_sha_is_a_problem_not_a_request(self):
        bad = "1" * 20 + "/../../x"
        a = push(S1, 7)
        a["after"] = bad
        routes = with_runs({f"GET {R}/activity": [a]})
        (p,) = audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems
        self.assertTrue(p.key.endswith(":bad"), p.key)
        routes = with_runs({f"GET {R}/activity": [push(S1, 7)],
                            f"GET {R}/commits/{S1}/statuses": mark("success"),
                            f"GET {R}/compare/{BEFORE}...{S1}": {
                                "commits": [{"sha": bad}, {"sha": S1}],
                                "files": [{"filename": "a.txt"}]}})
        keys = [p.key for p in audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems]
        self.assertIn(f"drench44/demo@{S1}:bad", keys)

    def test_repos_file_refuses_odd_names(self):
        ok = {"branch": "main", "since": "2026-09-23T14:43:00Z"}
        for data in ({"repos": {"acme/..": ok}}, {"repos": {"../pubapp": ok}},
                     {"repos": {"acme/pubapp": dict(ok, branch="main?x=1")}},
                     {"repos": {"acme/pubapp": dict(ok, branch="a/../b")}},
                     {"repos": {"acme/pubapp": dict(ok, branch="m%2e")}},
                     {"repos": {"acme/pubapp": dict(ok, workflow=".github/workflows/../x.yml")}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                LoadReposTests.load(self, data)


class TokenExpiryTests(unittest.TestCase):
    def check(self, header):
        api = FakeGitHub({"GET /rate_limit": {}})
        if header is not None:
            api.headers["/rate_limit"] = {"GitHub-Authentication-Token-Expiration": header}
        return audit.token_expiry_problem(api, NOW)

    def test_expiry(self):
        self.assertIsNone(self.check(None))
        self.assertIsNone(self.check("2026-12-01 00:00:00 UTC"))
        p = self.check("2026-09-30 00:00:00 UTC")
        self.assertEqual(p.key, "token:expires:2026-09-30")
        self.assertEqual(self.check("soon").key, "token:expiry-unreadable")


HOME = "/repos/drench44/ci-shared"


class AuditMainTests(unittest.TestCase):
    def run_main(self, audit_routes, home_routes, inputs=None, token="pat", expiry=None):
        repos = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"repos": {"drench44/demo": {"branch": "main",
                                               "since": "2026-08-01T00:00:00Z"}}}, repos)
        repos.close()
        home = FakeGitHub(home_routes)
        pat = FakeGitHub(with_runs({"GET /rate_limit": {}, **audit_routes}))
        if expiry:
            pat.headers["/rate_limit"] = {"github-authentication-token-expiration": expiry}
        ins = {"repos-file": repos.name, **(inputs or {})}
        if token:
            ins["audit-token"] = token

        def client(tok, url):
            if not tok:
                raise gh.GitHubError("no GitHub token")
            return pat if tok == "pat" else home
        try:
            with ActionsEnv({}, ins, {"GITHUB_REPOSITORY": "drench44/ci-shared"}) as env, \
                    mock.patch.object(gh, "GitHub", side_effect=client), \
                    mock.patch.object(audit, "utcnow", return_value=NOW), \
                    mock.patch("builtins.print"):
                code = audit.main()
                return code, env.summary(), home
        finally:
            os.unlink(repos.name)

    STUCK = {f"GET {R}/activity": [push(S1, 7)],
             f"GET {R}/commits/{S1}/statuses": [],
             f"GET {R}/actions/runs": wf_runs()}

    def test_clean_run_posts_nothing(self):
        code, summary, home = self.run_main(
            {f"GET {R}/activity": [push(S1, 7)],
             f"GET {R}/commits/{S1}/statuses": mark("success")}, {})
        self.assertEqual(code, 0, summary)
        self.assertEqual(home.posts, [])
        self.assertIn("| drench44/demo | 1 | 0 |", summary)

    def test_new_problem_opens_an_issue_with_keys(self):
        code, summary, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 9}})
        self.assertEqual(code, 0, summary)
        body = home.posts[-1][1]["body"]
        self.assertIn(f"<!-- main-watch-audit keys: drench44/demo@{S1} -->", body)
        self.assertIn("| NEW | drench44/demo |", body)
        self.assertIn("opened #9", summary)

    def test_already_reported_problem_stays_quiet_even_after_close(self):
        issue = {"number": 9, "state": "closed", "user": BOT,
                 "body": f"x <!-- main-watch-audit keys: drench44/demo@{S1} -->"}
        code, summary, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts, [])
        self.assertIn("already reported", summary)

    def test_standing_condition_alerts_again_once_its_issue_is_closed(self):
        # The token still cannot read the repo: closing the issue must not
        # silence that forever.
        issue = {"number": 9, "state": "closed", "user": BOT,
                 "body": "x <!-- main-watch-audit keys: drench44/demo:read -->"}
        code, _, home = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)}, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": [],
            f"POST {HOME}/labels": {}, f"POST {HOME}/issues": {"number": 10}})
        self.assertEqual(code, 1)
        self.assertIn("drench44/demo:read", home.posts[-1][1]["body"])

    def test_standing_condition_in_the_open_issue_stays_quiet(self):
        issue = {"number": 9, "state": "open", "user": BOT,
                 "body": "x <!-- main-watch-audit keys: drench44/demo:read -->"}
        code, summary, home = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)},
                                            {f"GET {HOME}/issues": [issue],
                                             f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 1)
        self.assertEqual(home.posts, [])

    def test_push_reported_in_an_older_closed_issue_stays_quiet(self):
        newer = {"number": 12, "state": "open", "user": BOT, "body": "<!-- main-watch-audit keys: other -->"}
        older = {"number": 9, "state": "closed", "user": BOT,
                 "body": f"<!-- main-watch-audit keys: drench44/demo@{S1} -->"}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [newer, older], f"GET {HOME}/issues/12/comments": [],
            f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts, [])

    def test_keys_from_other_authors_are_ignored(self):
        # Anyone can comment a key marker; only the audit's own count.
        issue = {"number": 9, "state": "open", "user": BOT, "body": "x"}
        forged = {"number": 8, "state": "closed", "user": {"login": "mallory"},
                  "body": f"<!-- main-watch-audit keys: drench44/demo@{S1} -->"}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue, forged], f"GET {HOME}/issues/9/comments": [
                {"body": f"<!-- main-watch-audit keys: drench44/demo@{S1} -->",
                 "user": {"login": "mallory"}}],
            f"POST {HOME}/issues/9/comments": {}})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts[0][0], f"{HOME}/issues/9/comments")
        self.assertIn(f"keys: drench44/demo@{S1} -->", home.posts[0][1]["body"])

    def test_copied_text_cannot_open_or_close_a_comment(self):
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/statuses": mark(
                      "pending", "PR #5 --> <!-- main-watch-audit keys: x@y --><!<!---->--")}
        code, _, home = self.run_main(routes, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 9}})
        body = home.posts[-1][1]["body"]
        self.assertEqual(body.count("<!--"), 1, body)
        self.assertEqual(body.count("-->"), 1, body)
        self.assertEqual(audit.KEYS_RE.findall(body), [f"drench44/demo@{S1}"])

    def test_key_reported_in_a_comment_stays_quiet(self):
        # After the first issue every alert is a comment; keys live there too.
        issue = {"number": 9, "state": "open", "user": BOT, "body": "<!-- main-watch-audit keys: other -->"}
        code, summary, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": [
                {"body": f"<!-- main-watch-audit keys: drench44/demo@{S1} -->", "user": BOT}]})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts, [])
        self.assertIn("already reported", summary)

    def test_newest_open_issue_gets_the_comment(self):
        newer = {"number": 12, "state": "open", "user": BOT, "body": ""}
        older = {"number": 9, "state": "open", "user": BOT, "body": ""}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [newer, older], f"GET {HOME}/issues/12/comments": [],
            f"GET {HOME}/issues/9/comments": [], f"POST {HOME}/issues/12/comments": {}})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts[0][0], f"{HOME}/issues/12/comments")

    def test_expiring_token_still_audits_and_warns(self):
        routes = dict(self.STUCK)
        code, summary, home = self.run_main(routes, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 1}},
            expiry="2026-09-30 00:00:00 UTC")
        self.assertEqual(code, 0, summary)
        body = home.posts[-1][1]["body"]
        self.assertIn("token:expires:2026-09-30", body)
        self.assertIn(f"drench44/demo@{S1}", body)

    def test_new_problem_comments_on_the_open_issue(self):
        issue = {"number": 9, "state": "open", "user": BOT, "body": "<!-- main-watch-audit keys: old -->"}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": [
                {"body": "<!-- main-watch-audit keys: older -->", "user": BOT}],
            f"POST {HOME}/issues/9/comments": {}})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts[0][0], f"{HOME}/issues/9/comments")

    def test_missing_token_is_loud(self):
        code, summary, home = self.run_main({}, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 1}}, token="")
        self.assertEqual(code, 1)
        self.assertIn("MAIN_WATCH_AUDIT_TOKEN secret is not set", home.posts[-1][1]["body"])

    def test_rejected_token_is_loud(self):
        code, _, home = self.run_main({"GET /rate_limit": gh.GitHubError("Bad credentials", 401)},
                                      {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                       f"POST {HOME}/issues": {"number": 1}})
        self.assertEqual(code, 1)
        self.assertIn("token:rejected", home.posts[-1][1]["body"])

    def test_an_unreadable_push_turns_the_run_red(self):
        code, _, _ = self.run_main(
            {f"GET {R}/activity": [push(S1, 7)],
             f"GET {R}/commits/{S1}/statuses": gh.GitHubError("down", 502)},
            {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
             f"POST {HOME}/issues": {"number": 1}})
        self.assertEqual(code, 1)

    def test_every_repo_unreadable_is_blind_and_fails(self):
        code, _, _ = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)},
                                   {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                    f"POST {HOME}/issues": {"number": 1}})
        self.assertEqual(code, 1)

    def test_issue_write_failure_fails(self):
        code, summary, _ = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": gh.GitHubError("forbidden", 403)})
        self.assertEqual(code, 1)
        self.assertIn("FAILED", summary)

    def test_bad_window_is_loud(self):
        code, _, _ = self.run_main({}, {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                        f"POST {HOME}/issues": {"number": 1}},
                                   {"grace-hours": "80"})
        self.assertEqual(code, 1)




# ------------------------------------------------------------ pin bumps
OLD, NEW = "a" * 40, "b" * 40
BEFORE = "0" * 39 + "9"
CALLER = f"""name: main-watch
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  watch:
    uses: drench44/ci-shared/.github/workflows/main-watch.yml@{OLD} # main @ aaaaaaa
    with:
      ci-policy-ref: {OLD}
      runs-on: '["self-hosted"]'
"""
BUMPED = CALLER.replace(OLD, NEW).replace("main @ aaaaaaa", "main @ bbbbbbb")


def content(text):
    return {"type": "file", "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(), "sha": str(hash(text))}


def pin_routes(after_text=BUMPED, before_text=CALLER, files=None):
    """A push (BEFORE..S1) that is the squash commit of green PR #7 and
    changes the caller workflow from ``before_text`` to ``after_text``."""
    pr = {"number": 7, "merged_at": "2026-09-23T00:00:00Z", "base": {"ref": "main"},
          "merge_commit_sha": S1, "head": {"sha": S3}}
    return {
        f"GET {R}/activity": [push(S1, 7)],
        f"GET {R}/commits/{S1}/statuses": mark("success", run=9),
        f"GET {R}/actions/runs/9": watch_run(event="push", head_sha=S1),
        f"GET {R}/actions/runs": wf_runs(),
        f"GET {R}/contents/{WF}": lambda params: content(
            after_text if params["ref"] == S1 else before_text),
        f"GET {R}/compare/{BEFORE}...{S1}": {
            "commits": [{"sha": S1}],
            "files": files if files is not None else [{"filename": WF, "status": "modified"}]},
        f"GET {R}/commits/{S1}": {"parents": [{"sha": BEFORE}],
                                  "files": [{"filename": WF, "status": "modified"}]},
        f"GET {R}/commits/{S1}/pulls": [pr],
        f"GET {R}/pulls/7": dict(pr, merged=True),
        f"GET {R}/pulls/7/commits": [{"sha": S3}],
        f"GET {R}/commits/{S3}/check-runs": {"check_runs": [
            {"name": "test", "status": "completed", "conclusion": "success",
             "app": {"slug": "github-actions"}}]},
        f"GET {R}/commits/{S3}/status": {"state": "success"},
        f"GET /repos/drench44/ci-shared/compare/{NEW}...main": {"status": "ahead"},
        f"GET /repos/drench44/ci-shared/compare/{OLD}...{NEW}": {"status": "ahead"},
    }


class PinBumpTests(unittest.TestCase):
    def problems(self, routes):
        return sorted(p.key for p in audit_repo(FakeGitHub(with_runs(routes)), W, NOW, 6,
                                                72).problems)

    def assert_reported(self, routes):
        self.assertIn(f"drench44/demo@{S1}:workflows", self.problems(routes))

    def test_a_pure_pin_bump_merged_through_a_green_pr_is_fine(self):
        # Its own main-watch status (from the bumped workflow) is trusted too.
        self.assertEqual(self.problems(pin_routes()), [])

    def test_any_other_workflow_edit_is_reported(self):
        edits = {
            "another input": BUMPED.replace('"self-hosted"', '"ubuntu-latest"'),
            "a new step": BUMPED + "  more:\n    runs-on: x\n    steps:\n      - run: id\n",
            "a new trigger": BUMPED.replace("on:\n", "on:\n  workflow_dispatch:\n"),
            "a permission": BUMPED.replace("contents: read", "contents: write"),
            "another owner": BUMPED.replace("drench44/ci-shared", "evil/ci-shared"),
            "a lookalike repo": BUMPED.replace("drench44/ci-shared", "drench44/ci-shared-x"),
            "a mismatched ref": CALLER.replace(f"ci-policy-ref: {OLD}", f"ci-policy-ref: {NEW}"),
            "only the ref": CALLER.replace(f"ci-policy-ref: {OLD}", f"ci-policy-ref: {NEW}")
            .replace(f"@{OLD}", f"@{OLD}"),
            "an anchor": BUMPED + "x: &a\n  y: 1\n",
            "a second document": BUMPED + "---\nz: 1\n",
        }
        for why, after in edits.items():
            with self.subTest(why=why):
                self.assert_reported(pin_routes(after_text=after))

    def test_a_pin_line_inside_a_run_block_is_not_a_pin(self):
        before = (f"jobs:\n  x:\n    runs-on: y\n    steps:\n      - run: |\n"
                  f"          uses: drench44/ci-shared/.github/workflows/main-watch.yml@{OLD}\n")
        self.assert_reported(pin_routes(before_text=before, after_text=before.replace(OLD, NEW)))

    def test_a_downgrade_or_sideways_pin_is_reported(self):
        # NEW is on main, but it is older than OLD (behind) or not after it.
        for status in ("behind", "diverged", "identical"):
            with self.subTest(status=status):
                routes = pin_routes()
                routes[f"GET /repos/drench44/ci-shared/compare/{OLD}...{NEW}"] = {
                    "status": status}
                self.assert_reported(routes)
        routes = pin_routes()
        routes[f"GET /repos/drench44/ci-shared/compare/{OLD}...{NEW}"] = gh.GitHubError(
            "Not Found", 404)
        self.assert_reported(routes)

    def test_a_pin_to_a_commit_not_on_ci_shared_main_is_reported(self):
        for answer in ({"status": "diverged"}, gh.GitHubError("Not Found", 404)):
            with self.subTest(answer=answer):
                routes = pin_routes()
                routes[f"GET /repos/drench44/ci-shared/compare/{NEW}...main"] = answer
                self.assert_reported(routes)

    def test_missing_or_partial_inputs_are_never_a_pass(self):
        cases = {
            "file list may be cut short": dict(files=[{"filename": WF, "status": "modified"}] +
                                               [{"filename": f"f{i}"} for i in range(120)]),
            "no file list": dict(files=[]),
            "a file entry with no name": dict(files=[{"status": "modified"}]),
            "a renamed workflow": dict(files=[{"filename": WF, "status": "renamed",
                                               "previous_filename": ".github/workflows/x.yml"}]),
            "an added workflow": dict(files=[{"filename": WF, "status": "added"}]),
        }
        for why, kw in cases.items():
            with self.subTest(why=why):
                self.assert_reported(pin_routes(**kw))
        for why, answer in {"file missing": gh.GitHubError("Not Found", 404),
                            "too big to inline": {"type": "file", "encoding": "none",
                                                  "content": ""},
                            "not a file": {"type": "dir"},
                            "not text": {"type": "file", "encoding": "base64",
                                         "content": base64.b64encode(b"\xff\xfe").decode()}
                            }.items():
            with self.subTest(why=why):
                routes = pin_routes()
                routes[f"GET {R}/contents/{WF}"] = answer
                self.assert_reported(routes)

    def test_pr_vetting_table(self):
        pr = {"number": 7, "merged_at": "2026-09-23T00:00:00Z", "base": {"ref": "main"},
              "merge_commit_sha": S1, "head": {"sha": S3}}
        green = {"name": "test", "status": "completed", "conclusion": "success",
                 "app": {"slug": "github-actions"}}
        cases = {
            "base not watched": {f"GET {R}/commits/{S1}/pulls": [dict(pr, base={"ref": "dev"})]},
            "merge sha differs": {f"GET {R}/commits/{S1}/pulls": [dict(pr, merge_commit_sha=S2)]},
            "merged but another sha": {f"GET {R}/pulls/7": dict(pr, merged=True,
                                                               merge_commit_sha=S2)},
            "closed, not merged": {f"GET {R}/pulls/7": dict(pr, merged=False)},
            "no runs": {f"GET {R}/commits/{S3}/check-runs": {"check_runs": []}},
            "only skipped and neutral": {f"GET {R}/commits/{S3}/check-runs": {"check_runs": [
                dict(green, conclusion="skipped"), dict(green, conclusion="neutral")]}},
            "in progress": {f"GET {R}/commits/{S3}/check-runs": {"check_runs": [
                green, dict(green, name="slow", status="in_progress", conclusion=None)]}},
            "queued": {f"GET {R}/commits/{S3}/check-runs": {"check_runs": [
                green, dict(green, name="slow", status="queued", conclusion=None)]}},
            "combined status failure": {f"GET {R}/commits/{S3}/status": {"state": "failure"}},
            "green only from another app": {f"GET {R}/commits/{S3}/check-runs": {
                "check_runs": [dict(green, app={"slug": "someone-else"})]}},
            "check runs cut short": {f"GET {R}/commits/{S3}/check-runs":
                                     gh.GitHubError("more than 5000 items", None)},
            "PR commits cut short": {f"GET {R}/pulls/7/commits":
                                     gh.GitHubError("more than 5000 items", None)},
            "a direct commit along": {f"GET {R}/compare/{BEFORE}...{S1}": {
                "commits": [{"sha": S2}, {"sha": S1}],
                "files": [{"filename": WF, "status": "modified"}]},
                f"GET {R}/commits/{S2}/statuses": mark("success")},
        }
        for why, change in cases.items():
            with self.subTest(why=why):
                routes = pin_routes()
                routes.update(change)
                keys = self.problems(routes)
                self.assertTrue(any(k.startswith(f"drench44/demo@{S1}") for k in keys), keys)


class WindowTests(unittest.TestCase):
    def test_future_since_and_empty_window_are_problems(self):
        w = Watched("drench44/demo", "main", NOW + dt.timedelta(days=1))
        (p,) = audit_repo(FakeGitHub(with_runs({})), w, NOW, 6, 72).problems
        self.assertEqual(p.key, "drench44/demo:since")
        w = Watched("drench44/demo", "main", NOW - dt.timedelta(hours=2))
        (p,) = audit_repo(FakeGitHub(with_runs({})), w, NOW, 6, 72).problems
        self.assertTrue(p.key.startswith("drench44/demo:window"))

    def test_missing_branch_is_a_read_problem(self):
        routes = with_runs({f"GET {R}/activity": []})
        routes[f"GET {R}/branches/main"] = gh.GitHubError("Branch not found", 404)
        (p,) = audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems
        self.assertEqual(p.key, "drench44/demo:read")

    def test_branch_that_moved_without_a_listed_push_is_a_problem(self):
        routes = with_runs({f"GET {R}/activity": []})
        routes[f"GET {R}/branches/main"] = {"commit": {
            "sha": S2, "commit": {"committer": {"date": "2026-09-23T01:00:00Z"}}}}
        (p,) = audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems
        self.assertEqual(p.key, f"drench44/demo@{S2}:unlisted")

    def test_time_period_covers_the_lookback(self):
        self.assertEqual([audit.time_period(h) for h in (24, 72, 400, 1500, 5000)],
                         ["day", "week", "month", "quarter", "year"])
        with self.assertRaises(ValueError):
            audit.time_period(24 * 400)


class InnerCommitTests(unittest.TestCase):
    def test_inner_commit_without_a_final_status_is_reported(self):
        for why, st in {"no status": [], "forged": mark("success",
                                                        creator={"login": "mallory"})}.items():
            with self.subTest(why=why):
                routes = with_runs({
                    f"GET {R}/activity": [push(S1, 7)],
                    f"GET {R}/commits/{S1}/statuses": mark("success"),
                    f"GET {R}/compare/{BEFORE}...{S1}": {"commits": [{"sha": S2}, {"sha": S1}],
                                                         "files": [{"filename": "a.txt"}]},
                    f"GET {R}/commits/{S2}/statuses": st})
                (p,) = audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems
                self.assertEqual(p.key, f"drench44/demo@{S2}")
                self.assertIn("no main-watch conclusion", p.text)

    def test_100_files_may_be_cut_short(self):
        routes = with_runs({
            f"GET {R}/activity": [push(S1, 7)],
            f"GET {R}/commits/{S1}/statuses": mark("success"),
            f"GET {R}/commits/{S1}/pulls": [],
            f"GET {R}/compare/{BEFORE}...{S1}": {"commits": [{"sha": S1}],
                                                 "files": [{"filename": f"f{i}"}
                                                           for i in range(100)]}})
        (p,) = audit_repo(FakeGitHub(routes), W, NOW, 6, 72).problems
        self.assertEqual(p.key, f"drench44/demo@{S1}:workflows")


if __name__ == "__main__":
    unittest.main()
