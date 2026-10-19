"""A private repo's allowlist rules are keyed by the sha256 of its name, so the
public allowlist never names it; lookups by the real name still find them."""

import hashlib
import json
import os
import unittest

import fakes  # noqa: F401  (puts lib/ on sys.path)
from ci_policy import allowlist

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HashedKeyTests(unittest.TestCase):
    def doc(self, key):
        return allowlist.parse({"repos": {key: [{"name": "bot", "subject": "^bot: "}]}})

    def test_hashed_key_found_by_real_name(self):
        key = "sha256:" + hashlib.sha256(b"someone/secret-thing").hexdigest()
        allow = self.doc(key)
        self.assertEqual(len(allowlist.rules_for(allow, "someone/secret-thing")), 1)
        self.assertEqual(len(allowlist.rules_for(allow, "Someone/Secret-Thing")), 1)
        self.assertEqual(allowlist.rules_for(allow, "someone/other"), [])
        self.assertEqual(allowlist.rules_for(allow, None), [])

    def test_repo_key_matches_the_documented_form(self):
        self.assertEqual(allowlist.repo_key("A/B"),
                         "sha256:" + hashlib.sha256(b"a/b").hexdigest())

    def test_hashed_key_with_its_plain_twin_is_refused(self):
        key = allowlist.repo_key("Acme/Secret-Proj")
        with self.assertRaises(ValueError) as e:
            allowlist.parse({"repos": {"acme/secret-proj": [{"subject": "^a"}],
                                       key: [{"subject": "^b"}]}})
        self.assertIn("twice", str(e.exception))
        with self.assertRaises(ValueError):
            allowlist.parse({"repos": {"Acme/Secret-Proj": [{"subject": "^a"}],
                                       key: [{"subject": "^b"}]}})

    def test_keys_differing_only_in_case_are_refused(self):
        with self.assertRaises(ValueError) as e:
            allowlist.parse({"repos": {"Acme/X": [{"subject": "^a"}],
                                       "acme/x": [{"subject": "^b"}]}})
        self.assertIn("twice", str(e.exception))

    def test_bad_keys_are_refused(self):
        for key in ("acme", "sha256:abc", "sha256:" + "g" * 64, "a/b/c", " a/b"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                allowlist.parse({"repos": {key: [{"subject": "^a"}]}})

    def test_rules_for_refuses_anything_but_owner_name(self):
        key = allowlist.repo_key("acme/secret-proj")
        allow = self.doc(key)
        for repo in (key, key.upper(), "sha256:x", "acme", "a/b/c"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                allowlist.rules_for(allow, repo)

    def test_repo_from_url(self):
        for url, want in [("https://github.com/acme/pubapp.git", "acme/pubapp"),
                          ("git@github.com:acme/pubapp.git", "acme/pubapp"),
                          ("ssh://git@github.com/acme/pubapp", "acme/pubapp"),
                          ("ssh://git@ssh.github.com:443/acme/pubapp.git", "acme/pubapp"),
                          ("https://GitHub.com/acme/pubapp/", "acme/pubapp"),
                          ("https://notgithub.com/acme/pubapp.git", None),
                          ("https://github.com.example.net/acme/pubapp.git", None),
                          ("git@gh-alias:acme/pubapp.git", None),
                          ("/srv/git/acme/pubapp.git", None),
                          # "github.com" anywhere but the host is not GitHub.
                          ("https://evil.example/x/github.com/acme/pubapp", None),
                          ("https://github.com@evil.example/acme/pubapp", None),
                          ("git@evil.example:github.com/acme/pubapp", None),
                          ("https://github.com/acme/pubapp/extra", None),
                          ("https://github.com/acme/pubapp.git?x=1", None),
                          ("https://user:pw@github.com/acme/pubapp", "acme/pubapp")]:
            with self.subTest(url=url):
                self.assertEqual(allowlist.repo_from_url(url), want)

    def test_non_ascii_names_do_not_fold_into_ascii(self):
        kelvin = "acme/\u212aey"          # lowercases to "acme/key"
        allow = allowlist.parse({"repos": {"acme/key": [{"subject": "^a"}]}})
        with self.assertRaises(ValueError):
            allowlist.rules_for(allow, kelvin)
        with self.assertRaises(ValueError):
            allowlist.parse({"repos": {kelvin: [{"subject": "^a"}]}})

    def test_github_path_variants_are_protected(self):
        for p in (".github", "./.github/x", ".GitHub/Workflows/a.yml", "a/../.github/x"):
            with self.subTest(p=p):
                self.assertTrue(allowlist.touches_protected(p))
        for p in ("src/.github/x", "github/x", "docs/.githubx"):
            with self.subTest(p=p):
                self.assertFalse(allowlist.touches_protected(p))

    def test_shipped_file_names_no_private_repo_at_all(self):
        with open(os.path.join(ROOT, "policy", "main-allowlist.json")) as f:
            data = json.load(f)
        for key in data["repos"]:
            self.assertFalse(key.startswith("sha256:"), key)

    def test_redact_handles_an_at_sign_in_the_password(self):
        self.assertEqual(allowlist.redact("https://user:p@ss@host/a/b"), "https://host/a/b")

    def test_redact_removes_credentials_from_urls(self):
        self.assertEqual(allowlist.redact("push to https://me:ghp_secret@github.com/a/b.git"),
                         "push to https://github.com/a/b.git")
        self.assertEqual(allowlist.redact("ssh://git@host/a/b and git@github.com:a/b"),
                         "ssh://host/a/b and git@github.com:a/b")

    def test_is_local_url(self):
        for url in ("/srv/x.git", "../x.git", "x.git", "file:///srv/x.git", "./a:b"):
            with self.subTest(url=url):
                self.assertTrue(allowlist.is_local_url(url))
        for url in ("git@gh-alias:acme/x.git", "gh-alias:x", "https://github.com/a/b",
                    "ssh://git@ssh.github.com:443/a/b", ""):
            with self.subTest(url=url):
                self.assertFalse(allowlist.is_local_url(url))

    def test_github_paths_are_never_allowed(self):
        rules = allowlist.parse({"repos": {"o/r": [
            {"subject": "^backup: "},
            {"subject": "^release", "paths": ["**"]},
            {"subject": "^x", "not_paths": ["secrets/**"]}]}})["o/r"]
        for subject in ("backup: a", "release", "x"):
            for files in ([".github/workflows/ci.yml"], ["a.md", ".github/CODEOWNERS"]):
                with self.subTest(subject=subject, files=files):
                    self.assertIsNone(allowlist.match(rules, subject, "main", lambda f=files: f))
            self.assertIsNotNone(allowlist.match(rules, subject, "main", lambda: ["a.md"]))
            self.assertIsNone(allowlist.match(rules, subject, "main", lambda: None))
        self.assertIsNone(allowlist.match(rules, "x", "main", lambda: ["secrets/k"]))
        # An empty commit touches nothing: fine for a rule without paths.
        self.assertIsNotNone(allowlist.match(rules, "backup: a", "main", lambda: []))
        self.assertIsNone(allowlist.match(rules[1:2], "release", "main", lambda: []))

    def test_shipped_rules_keep_github_out(self):
        allow = allowlist.load(os.path.join(ROOT, "policy", "main-allowlist.json"))
        for key, rules in allow.items():
            for r in rules:
                with self.subTest(rule=r.name):
                    self.assertTrue(r.paths or r.not_paths, "every rule limits its paths")
                    if r.paths:
                        for p in r.paths.patterns:
                            self.assertNotIn("*", p, "release rules list exact files")

    def test_shipped_file_names_only_public_style_keys(self):
        with open(os.path.join(ROOT, "policy", "main-allowlist.json")) as f:
            data = json.load(f)
        allowlist.parse(data)
        for key in data["repos"]:
            self.assertTrue(key.startswith("sha256:") or key.count("/") == 1, key)


if __name__ == "__main__":
    unittest.main()
