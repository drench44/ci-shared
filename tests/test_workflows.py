"""Static checks on this repo's workflow files (stdlib only, no YAML parser)."""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "examples", "*.yml")))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def uses_lines(path):
    for n, line in enumerate(read(path).splitlines(), 1):
        m = USES.match(line)
        if m:
            yield n, m.group(1), m.group(2).strip()


def permissions(text):
    """The top-level permissions block as {scope: level}."""
    m = re.search(r"^permissions:\n((?:  \S.*\n)+)", text, re.M)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.split("#")[0].strip()
    return out


class PinTests(unittest.TestCase):
    def test_every_third_party_action_is_pinned_by_sha_with_a_version_comment(self):
        self.assertTrue(WORKFLOWS)
        for path in WORKFLOWS:
            for n, ref, rest in uses_lines(path):
                where = f"{os.path.basename(path)}:{n}"
                if ref.startswith("./"):
                    continue   # this repo's own reusable workflows
                with self.subTest(where=where, ref=ref):
                    if ref.startswith("docker://"):
                        self.assertRegex(ref, r"@sha256:[0-9a-f]{64}$")
                    else:
                        self.assertRegex(ref, r"@[0-9a-f]{40}$")
                    self.assertRegex(rest, r"^# v?\d+(\.\d+)*$",
                                     "needs a trailing comment naming the version")

    def test_examples_pin_ci_policy_by_sha_placeholder(self):
        for path in EXAMPLES:
            for n, ref, _ in uses_lines(path):
                with self.subTest(path=os.path.basename(path)):
                    self.assertNotRegex(ref, r"@(main|master)$")


class MainWatchWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(ROOT, ".github", "workflows", "main-watch.yml"))

    def test_every_input_reaches_the_script(self):
        inputs = re.findall(r"^      ([a-z-]+):\n", self.text, re.M)
        self.assertIn("required-checks", inputs)
        for name in inputs:
            if name in ("runs-on", "ci-policy-ref"):
                continue
            env = "INPUT_" + name.upper().replace("-", "_")
            with self.subTest(input=name):
                self.assertIn(f"{env}: ${{{{ inputs.{name} }}}}", self.text)

    def test_callers_grant_what_the_reusable_workflow_asks_for(self):
        # A called workflow asking for more than its caller grants fails to start.
        wanted = permissions(self.text)
        self.assertEqual(wanted.get("statuses"), "write")
        self.assertEqual(wanted.get("actions"), "read")
        for caller in [os.path.join(ROOT, "examples", "main-watch.yml"),
                       os.path.join(ROOT, ".github", "workflows", "policy.yml")]:
            granted = permissions(read(caller))
            for scope, level in wanted.items():
                with self.subTest(caller=os.path.basename(caller), scope=scope):
                    self.assertIn(granted.get(scope), (level, "write"))

    def test_no_caller_allow_rules_input(self):
        # Rules a watched repo passed in would let a push allow itself.
        self.assertNotRegex(self.text, r"(?m)^      allow-rules:")
        self.assertNotIn("INPUT_ALLOW_RULES", self.text)
        for path in EXAMPLES + WORKFLOWS:
            with self.subTest(path=os.path.basename(path)):
                self.assertNotIn("allow-rules:", read(path))

    def test_own_main_watch_runs_the_code_from_before_the_push(self):
        text = read(os.path.join(ROOT, ".github", "workflows", "policy.yml"))
        job = text[text.index("\n  main-watch:"):]
        self.assertIn("ci-policy-ref: ${{ needs.pin.outputs.ref }}", job)
        self.assertIn("needs: pin", job)
        self.assertNotIn("github.sha }}\n", job)
        pin = text[text.index("\n  pin:"):text.index("\n  main-watch:")]
        self.assertIn("BEFORE: ${{ github.event.before }}", pin)
        self.assertIn(".parents[0].sha", pin)

    def run_pin(self, event, before, parents_json, gh_fails=False):
        """Run the pin step's script under bash with a fake gh on PATH."""
        text = read(os.path.join(ROOT, ".github", "workflows", "policy.yml"))
        pin = text[text.index("\n  pin:"):text.index("\n  main-watch:")]
        body = pin[pin.index("        run: |\n") + len("        run: |\n"):]
        script = "\n".join(l[10:] for l in body.splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            gh = os.path.join(tmp, "gh")
            with open(gh, "w") as f:
                if gh_fails:
                    f.write("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
                else:
                    # Applies the step's own --jq to the canned commit with jq.
                    f.write("#!/bin/sh\nwhile [ $# -gt 0 ]; do\n"
                            "  [ \"$1\" = --jq ] && { shift; q=\"$1\"; }; shift\ndone\n"
                            f"printf '%s' '{parents_json}' | jq -r \"$q\"\n")
            os.chmod(gh, 0o755)
            out = os.path.join(tmp, "out")
            open(out, "w").close()
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                       REPO="o/r", EVENT=event, BEFORE=before, SHA="c" * 40,
                       GITHUB_OUTPUT=out)
            p = subprocess.run(["bash", "-e", "-c", script], env=env,
                               capture_output=True, text=True)
            return p.returncode, read(out), p.stdout

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq and bash")
    def test_pin_picks_the_right_ref(self):
        a, b, c = "a" * 40, "b" * 40, "c" * 40
        one_parent = json.dumps({"parents": [{"sha": b}]})
        root = json.dumps({"parents": []})
        with self.subTest("push uses the old tip"):
            self.assertEqual(self.run_pin("push", a, root)[:2], (0, f"ref={a}\n"))
        with self.subTest("schedule uses the first parent"):
            self.assertEqual(self.run_pin("schedule", "", one_parent)[:2], (0, f"ref={b}\n"))
        with self.subTest("schedule on a first commit uses that commit"):
            code, out, stdout = self.run_pin("schedule", "", root)
            self.assertEqual((code, out), (0, f"ref={c}\n"))
            self.assertIn("::notice::", stdout)
        with self.subTest("a new branch push falls back to the parent"):
            self.assertEqual(self.run_pin("push", "0" * 40, one_parent)[:2], (0, f"ref={b}\n"))
        with self.subTest("a failed lookup fails the job, never pins itself"):
            code, out, _ = self.run_pin("schedule", "", root, gh_fails=True)
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")

    def test_callers_schedule_the_recheck(self):
        for caller in [os.path.join(ROOT, "examples", "main-watch.yml"),
                       os.path.join(ROOT, ".github", "workflows", "policy.yml")]:
            with self.subTest(caller=os.path.basename(caller)):
                self.assertRegex(read(caller), r"schedule:\n\s+- cron: ")


if __name__ == "__main__":
    unittest.main()
