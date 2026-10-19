"""allow-check and the push judge against real git repos.

allow-check is what the Claude Code push guard calls: exit 0 allowed,
1 refused, 2 could not decide.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import fakes  # noqa: F401  (puts lib/ on sys.path)
from ci_policy import local_hooks

ZERO = "0" * 40


def git(cwd, *args, input_text=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, input=input_text).stdout.strip()


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": self.tmp, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("CI_POLICY_ALLOWLIST", None)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "-q", "--bare", self.origin)
        git(self.tmp, "init", "-q", "-b", "main", self.repo)
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        git(self.repo, "remote", "add", "origin", self.origin)
        git(self.repo, "config", "ci-policy.repo", "drench44/family-hub")
        self.commit("a.txt", "one", "one")
        git(self.repo, "push", "-q", "origin", "main")
        self.allowlist = os.path.join(self.tmp, "allow.json")
        with open(self.allowlist, "w") as f:
            json.dump({"repos": {"drench44/family-hub": [
                {"name": "release", "branch": "main", "subject": "^release: v",
                 "paths": ["VERSION"]}]}}, f)
        os.environ["CI_POLICY_ALLOWLIST"] = self.allowlist

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, path, content, message):
        with open(os.path.join(self.repo, path), "w") as f:
            f.write(content + "\n")
        git(self.repo, "add", path)
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def check(self, branch="main", src="HEAD", remote="origin"):
        err = io.StringIO()
        with redirect_stderr(err):
            code = local_hooks.allow_check(remote, branch, src, self.repo)
        return code, err.getvalue()


class AllowCheckTests(Repo):
    def test_allowlisted_commit(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.assertEqual(self.check()[0], 0)

    def test_ordinary_commit_refused(self):
        self.commit("app.py", "x", "add app")
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("without a PR", err)

    def test_release_subject_touching_more_refused(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.commit("app.py", "x", "release: v1.0.2")
        self.assertEqual(self.check()[0], 1)

    def test_unresolvable_src_cannot_decide(self):
        self.assertEqual(self.check(src="nope")[0], 2)

    def test_unreachable_remote_cannot_decide(self):
        git(self.repo, "remote", "add", "gone", os.path.join(self.tmp, "missing.git"))
        self.assertEqual(self.check(remote="gone")[0], 2)

    def test_branch_on_remote_but_not_fetched_cannot_decide(self):
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.origin, other)
        git(other, "push", "-q", "origin", "refs/remotes/origin/main:refs/heads/master")
        code, err = self.check(branch="master")
        self.assertEqual(code, 2)
        self.assertIn("not fetched", err)

    def test_creating_branch_on_remote_with_branches_needs_allowlist(self):
        self.commit("app.py", "x", "add app")
        self.assertEqual(self.check(branch="master")[0], 1)

    def test_creating_branch_on_empty_remote_is_allowed(self):
        empty = os.path.join(self.tmp, "empty.git")
        git(self.tmp, "init", "-q", "--bare", empty)
        git(self.repo, "remote", "add", "empty", empty)
        self.assertEqual(self.check(remote="empty")[0], 0)

    def test_force_is_refused(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.commit("VERSION", "1.0.2", "release: v1.0.2")
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("force push", err)

    def test_broken_allowlist_cannot_decide(self):
        with open(self.allowlist, "w") as f:
            f.write("{nope")
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.assertEqual(self.check()[0], 2)

    def test_cli_turns_errors_into_2(self):
        with mock.patch.object(local_hooks, "allow_check", side_effect=RuntimeError("boom")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(local_hooks.main(["allow-check", "--remote", "origin",
                                               "--branch", "main"]), 2)


class JudgeTests(Repo):
    def test_author_rules_see_the_commit_author(self):
        with open(self.allowlist, "w") as f:
            json.dump({"repos": {"drench44/family-hub": [
                {"subject": "^bot:", "author": "bot@example.com"}]}}, f)
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "config", "user.email", "bot@example.com")
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "bot@example.com"}):
            new = self.commit("x", "x", "bot: update")
        rules = local_hooks.load_rules("drench44/family-hub")
        ok, _ = local_hooks.judge_update(new, base, "main", rules, self.repo, "origin")
        self.assertTrue(ok)
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "someone@example.com"}):
            newer = self.commit("y", "y", "bot: again")
        ok, _ = local_hooks.judge_update(newer, new, "main", rules, self.repo, "origin")
        self.assertFalse(ok)

    def test_delete_is_refused(self):
        ok, lines = local_hooks.judge_update(ZERO, git(self.repo, "rev-parse", "HEAD"), "main",
                                             [], self.repo, "origin")
        self.assertFalse(ok)
        self.assertIn("deleting", lines[0])

    def test_fork_pushes_only_to_its_push_url(self):
        # Fetch from them, push to me: the fetch URL never counts.
        git(self.repo, "remote", "set-url", "origin", "https://github.com/them/proj.git")
        git(self.repo, "remote", "set-url", "--push", "origin", "https://github.com/me/proj.git")
        t = local_hooks.push_target("origin", "", self.repo)
        self.assertEqual(t.github_repos, ["me/proj"])
        self.assertEqual(t.repo, "me/proj")

    def test_backup_push_url_on_another_host_is_ignored_for_the_allowlist(self):
        git(self.repo, "remote", "set-url", "origin", "https://github.com/drench44/family-hub.git")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "https://github.com/drench44/family-hub.git")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "ssh://git@backup.example.net/me/family-hub.git")
        t = local_hooks.push_target("origin", "", self.repo)
        self.assertEqual(len(t.away), 2)
        repo, rules = local_hooks.target_rules(t, self.allowlist)
        self.assertEqual((repo, [r.name for r in rules]), ("drench44/family-hub", ["release"]))

    def test_insteadof_shorthand_is_classified_after_rewriting(self):
        git(self.repo, "config", "url.https://github.com/acme/.insteadOf", "gh:")
        git(self.repo, "remote", "set-url", "origin", "gh:proj")
        t = local_hooks.push_target("origin", "", self.repo)
        self.assertEqual((t.repo, t.github, t.local), ("acme/proj", True, False))

    def test_github_push_urls_with_different_allowlists_refuse_main(self):
        git(self.repo, "remote", "set-url", "origin", "https://github.com/drench44/family-hub.git")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "https://github.com/drench44/family-hub.git")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "https://github.com/acme/other.git")
        t = local_hooks.push_target("origin", "", self.repo)
        with self.assertRaises(ValueError) as e:
            local_hooks.target_rules(t, self.allowlist)
        self.assertIn("allowlists differ", str(e.exception))
        # Two repos with the same (empty) allowlist are fine.
        git(self.repo, "config", "--unset-all", "remote.origin.pushurl")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "https://github.com/acme/a.git")
        git(self.repo, "remote", "set-url", "--add", "--push", "origin",
            "https://github.com/acme/b.git")
        repo, rules = local_hooks.target_rules(
            local_hooks.push_target("origin", "", self.repo), self.allowlist)
        self.assertEqual(rules, [])

    def test_push_insteadof_decides_the_push_url(self):
        git(self.repo, "remote", "set-url", "origin", "https://github.com/acme/pubapp.git")
        git(self.repo, "config", "url.https://github.com/acme/other.git.pushInsteadOf",
            "https://github.com/acme/pubapp.git")
        self.assertEqual(local_hooks.push_target("origin", "", self.repo).github_repos,
                         ["acme/other"])

    def fake_ssh(self, mapping, mode=""):
        """A fake `ssh` first on PATH whose `ssh -G host` answers from
        ``mapping`` (host -> extra -G lines; hostname defaults to the host)."""
        bin_dir = os.path.join(self.tmp, "sshbin")
        os.makedirs(bin_dir, exist_ok=True)
        lines = []
        for host, extra in mapping.items():
            lines.append(f"{host}) printf '%s\\n' {' '.join(repr(x) for x in extra)} ;;")
        script = ("#!/bin/sh\nfor last; do :; done\n"
                  "[ \"$1\" = -G ] || exit 1\n"
                  f"mode={mode!r}\n"
                  "[ \"$mode\" = fail ] && exit 255\n"
                  "[ \"$mode\" = hang ] && sleep 10\n"
                  "case \"$last\" in\n" + "\n".join(lines) +
                  "\n*) printf 'hostname %s\\n' \"$last\" ;;\nesac\n")
        path = os.path.join(bin_dir, "ssh")
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
        local_hooks._SSH_HOSTS.clear()
        return mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]})

    def test_ssh_alias_resolved_through_ssh_g(self):
        with self.fake_ssh({"gh-alias": ["hostname github.com"],
                            "evil-alias": ["hostname evil.example"]}):
            git(self.repo, "remote", "set-url", "origin", "git@gh-alias:acme/pubapp.git")
            t = local_hooks.push_target("origin", "", self.repo)
            self.assertEqual((t.repo, t.github, t.away[0].reaches), ("acme/pubapp", True, True))
            git(self.repo, "remote", "set-url", "origin", "git@evil-alias:acme/pubapp.git")
            t = local_hooks.push_target("origin", "", self.repo)
            self.assertEqual((t.repo, t.github), (None, False))
            self.assertEqual(t.away[0].repo, "acme/pubapp")

    def test_a_proxy_or_a_failing_ssh_never_proves_github(self):
        url = "git@github.com:acme/secret-proj.git"
        cases = {"proxyjump": ({"github.com": ["hostname github.com", "proxyjump jump.example"]},
                               ""),
                 "proxycommand": ({"github.com": ["hostname github.com",
                                                  "proxycommand nc evil.example 22"]}, ""),
                 "ssh -G fails": ({}, "fail")}
        for why, (mapping, mode) in cases.items():
            with self.subTest(why=why), self.fake_ssh(mapping, mode):
                repo, is_github, reaches = local_hooks.url_repo(url)
                self.assertEqual((repo, is_github, reaches), ("acme/secret-proj", True, False))
        with self.fake_ssh({}):
            self.assertEqual(local_hooks.url_repo(url), ("acme/secret-proj", True, True))
        # No ssh at all on PATH.
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        local_hooks._SSH_HOSTS.clear()
        with mock.patch.dict(os.environ, {"PATH": empty}):
            self.assertEqual(local_hooks.url_repo(url)[2], False)

    def test_a_hanging_ssh_never_proves_github(self):
        with self.fake_ssh({}, "hang"):
            self.assertFalse(local_hooks.url_repo("git@github.com:acme/secret-proj.git")[2])

    def test_another_ssh_command_never_proves_github(self):
        git(self.repo, "remote", "set-url", "origin", "git@github.com:acme/secret-proj.git")
        with self.fake_ssh({}):
            self.assertTrue(local_hooks.push_target("origin", "", self.repo).away[0].reaches)
            for env in ({"GIT_SSH_COMMAND": "ssh -o HostName=evil.example"},
                        {"GIT_SSH": "/usr/bin/ssh"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    t = local_hooks.push_target("origin", "", self.repo)
                    self.assertEqual((t.away[0].github, t.away[0].reaches), (True, False))
            git(self.repo, "config", "core.sshCommand", "ssh -i /dev/null")
            self.assertFalse(local_hooks.push_target("origin", "", self.repo).away[0].reaches)

    def test_release_subject_touching_github_is_refused(self):
        with open(self.allowlist, "w") as f:
            json.dump({"repos": {"drench44/family-hub": [
                {"name": "release", "subject": "^release: v"}]}}, f)
        os.makedirs(os.path.join(self.repo, ".github"))
        self.commit(".github/x.yml", "a: 1", "release: v2")
        code, err = self.check()
        self.assertEqual(code, 1, err)
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.commit("VERSION", "2", "release: v2")
        code, err = self.check()
        self.assertEqual(code, 0, err)

    def test_creating_main_with_no_known_remote_is_refused(self):
        ok, lines = local_hooks.judge_update(git(self.repo, "rev-parse", "HEAD"), ZERO, "main",
                                             [], self.repo, "")
        self.assertFalse(ok)
        self.assertIn("cannot tell", lines[0])

    def test_pre_push_refuses_a_ref_line_it_cannot_read(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = local_hooks.pre_push("origin", self.origin, ["", "only three fields"],
                                        self.repo)
        self.assertEqual(code, 1)
        self.assertIn("cannot read the ref line", err.getvalue())

    def test_override_only_for_local_remotes_and_never_hashed(self):
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/family-hub")
        git(self.repo, "remote", "set-url", "origin", "file://" + self.origin)
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/family-hub")
        git(self.repo, "remote", "set-url", "origin", "git@no-such-host.invalid:acme/x.git")
        self.assertIsNone(local_hooks.repo_name("origin", "", self.repo))
        git(self.repo, "remote", "set-url", "origin", self.origin)
        from ci_policy import allowlist
        git(self.repo, "config", "ci-policy.repo", allowlist.repo_key("drench44/family-hub"))
        self.commit("VERSION", "2", "release: v2")
        code, err = self.check()
        self.assertEqual(code, 1, err)
        self.assertIn("plain owner/name", err)

    def test_remote_name_with_a_slash_is_still_a_remote(self):
        # "team/origin" is a valid remote name, not a local path: its GitHub
        # URL decides, and ci-policy.repo must not apply.
        git(self.repo, "remote", "add", "team/origin", "https://github.com/acme/pubapp.git")
        t = local_hooks.push_target("team/origin", "", self.repo)
        self.assertEqual((t.repo, t.local), ("acme/pubapp", False))
        self.assertEqual(local_hooks.repo_name("team/origin", "", self.repo), "acme/pubapp")

    def test_direct_url_push_counts_its_insteadof_rewrite(self):
        git(self.repo, "config", "url.https://github.com/acme/pubapp.git.insteadOf",
            self.origin)
        t = local_hooks.push_target(self.origin, "", self.repo)
        self.assertEqual((t.repo, t.local), ("acme/pubapp", False))

    def test_merge_commit_files_are_listed(self):
        git(self.repo, "switch", "-qc", "side")
        self.commit("s.txt", "s", "side")
        git(self.repo, "switch", "-q", "main")
        self.commit("m.txt", "m", "main")
        git(self.repo, "merge", "-q", "--no-commit", "side")
        os.makedirs(os.path.join(self.repo, ".github", "workflows"))
        with open(os.path.join(self.repo, ".github", "workflows", "x.yml"), "w") as f:
            f.write("on: push\n")
        git(self.repo, "add", ".github")
        git(self.repo, "commit", "-qm", "merge")
        self.assertIn(".github/workflows/x.yml",
                      local_hooks._commit_files(git(self.repo, "rev-parse", "HEAD"), self.repo))

    def test_rename_out_of_github_lists_both_paths(self):
        os.makedirs(os.path.join(self.repo, ".github"))
        self.commit(".github/x.yml", "a: 1", "add")
        git(self.repo, "config", "diff.renames", "copies")
        git(self.repo, "mv", ".github/x.yml", "x.yml")
        git(self.repo, "commit", "-qm", "backup: move")
        files = local_hooks._commit_files(git(self.repo, "rev-parse", "HEAD"), self.repo)
        self.assertEqual(sorted(files), [".github/x.yml", "x.yml"])

    def test_odd_file_names_are_listed_unquoted(self):
        name = "caf\u00e9 \"q\".txt"
        self.commit(name, "x", "odd")
        self.assertEqual(local_hooks._commit_files(git(self.repo, "rev-parse", "HEAD"),
                                                   self.repo), [name])

    def test_unreadable_owner_config_refuses_the_leak_check(self):
        from ci_policy import leak_guard
        bad = os.path.join(self.tmp, "broken-gitconfig")
        with open(bad, "w") as f:
            f.write("[ci-policy\n\tprivateOwner = acme\n")   # not valid git config
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": bad}):
            with self.assertRaises(leak_guard.OwnersError):
                leak_guard.owners()
            ok, why = leak_guard.check_push([("acme/pubapp", True)], "origin",
                                            [("refs/heads/x", "1" * 40, "refs/heads/x", ZERO)],
                                            self.repo)
        self.assertFalse(ok)
        self.assertIn("cannot tell whether the private repo check is on", why[0])

    def test_a_push_with_no_push_url_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(local_hooks, "push_urls", return_value=[]):
            code = local_hooks.pre_push("nowhere", "", [f"refs/heads/x {'1' * 40} "
                                                         f"refs/heads/x {ZERO}"], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("cannot tell where this push goes", err.getvalue())

    def test_tag_errors_refuse_instead_of_stopping(self):
        from ci_policy import leak_guard
        with self.assertRaises(RuntimeError):
            leak_guard._tag_texts("f" * 40, self.repo)      # no such object
        head = git(self.repo, "rev-parse", "HEAD")
        sha = head
        for i in range(leak_guard.TAG_DEPTH + 1):           # a tag of a tag of a tag...
            sha = git(self.repo, "mktag", input_text=(
                f"object {sha}\ntype {'commit' if i == 0 else 'tag'}\ntag t{i}\n"
                "tagger t <t@example.com> 0 +0000\n\nx\n"))
        with self.assertRaises(RuntimeError):
            leak_guard._tag_texts(sha, self.repo)

    def test_private_rules_come_from_a_local_file_you_own(self):
        cfg = os.path.join(self.tmp, ".config", "ci-policy")
        os.makedirs(cfg, exist_ok=True)
        path = os.path.join(cfg, "private-allowlist.json")
        with open(path, "w") as f:
            json.dump({"repos": {"acme/secret-proj": [{"name": "backup",
                                                       "subject": "^backup: "}]}}, f)
        os.chmod(path, 0o600)
        self.assertEqual([r.name for r in local_hooks.load_rules("acme/secret-proj")],
                         ["backup"])
        self.assertEqual(local_hooks.load_rules("acme/other"), [])
        os.chmod(path, 0o666)
        with self.assertRaises(OSError):
            local_hooks.load_rules("acme/secret-proj")

    def test_credentials_in_a_remote_url_never_reach_the_output(self):
        token = "ghp_" + "T" * 36
        url = f"https://me:{token}@127.0.0.1:9/acme/x.git"
        git(self.repo, "remote", "add", "creds", url)
        self.commit("VERSION", "2", "release: v2")
        for argv, stdin in ((["allow-check", "--remote", "creds", "--branch", "main",
                              "-C", self.repo], ""),
                            (["pre-push", "creds", url],
                             f"refs/heads/main {git(self.repo, 'rev-parse', 'HEAD')} "
                             f"refs/heads/main {ZERO}\n")):
            with self.subTest(cmd=argv[0]):
                err = io.StringIO()
                cwd = os.getcwd()
                os.chdir(self.repo)
                try:
                    with redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
                        code = local_hooks.main(argv)
                finally:
                    os.chdir(cwd)
                self.assertNotEqual(code, 0, err.getvalue())
                self.assertNotIn(token, err.getvalue())

    def test_targets(self):
        cases = [("ssh://git@ssh.github.com:443/acme/pubapp.git", ("acme/pubapp", False, True)),
                 ("git@github.com:acme/pubapp.git", ("acme/pubapp", False, True)),
                 ("git@no-such-host.invalid:acme/pubapp.git", (None, False, False)),
                 ("https://git.example.net/acme/pubapp.git", (None, False, False)),
                 ("file://" + self.origin, ("drench44/family-hub", True, False))]
        for url, want in cases:
            with self.subTest(url=url):
                git(self.repo, "remote", "set-url", "origin", url)
                t = local_hooks.push_target("origin", "", self.repo)
                self.assertEqual((t.repo, t.local, t.github), want)

    def test_repo_name_prefers_a_github_url_over_the_override(self):
        git(self.repo, "remote", "set-url", "origin", "git@github.com:drench44/demo.git")
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/demo")
        git(self.repo, "remote", "set-url", "origin", self.origin)
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/family-hub")


if __name__ == "__main__":
    unittest.main()
