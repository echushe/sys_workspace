"""Tests for githooks/credential_check.py, the hook that keeps credentials out of the repo.

Every sample credential below is joined from fragments at run time, so this file
holds no literal the hook would flag -- the repository-wide test at the bottom
checks exactly that, along with every other tracked file.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "githooks"))

import credential_check as cc  # noqa: E402


def j(*parts):
    return "".join(parts)


SAMPLES = {
    "private-key": j("-----BEGIN ", "OPENSSH PRIVATE KEY-----"),
    "ssh-public-key": j("ssh-", "ed25519 AAAA", "C3NzaC1lZDI1NTE5AAAAIQ", "xyz"),
    "ssh-key-fingerprint": j("SHA256", ":", "Ab1" * 14, "C"),
    "aws-access-key": j("AKIA", "ABCDEFGHIJ234567"),
    "github-token": j("gh", "p_", "a1B2" * 9),
    "gitlab-token": j("gl", "pat-", "a1B2c3D4e5F6g7H8i9J0"),
    "slack-token": j("xo", "xb-", "1234567890-abcdef"),
    "ai-api-key": j("sk-", "ant-", "a1B2c3D4" * 3),
    "huggingface-token": j("hf", "_", "a1B2c3" * 6),
    "stripe-key": j("sk", "_live_", "a1B2c3D4" * 3),
    "npm-token": j("npm", "_", "a1B2c3" * 6),
    "google-api-key": j("AIza", "a1B2c3D4e5" * 3, "f6G7H"),
    "jwt": j("eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJ", "zdWIiOiIxMjM0In0", ".", "sig123"),
    "url-with-password": j("https://", "deploy", ":", "hunter22", "@", "example.com/r.git"),
    "authorization-header": j("Authorization", ": Basic ", "dXNlcjpodW50ZXIy"),
    "bearer-token": j("Bear", "er ", "a1b2c3d4e5f6g7h8i9j0k"),
    "quoted-secret": j("pass", "word = ", '"', "hunter22", '"'),
    "env-secret": j("GITHUB_", "TOKEN=", "abc123def456"),
    "sshpass": j("sshpass", " -p ", "hunter22 ssh host"),
    "password-piped-to-sudo": j("echo ", "hunter22", " | sudo -S apt update"),
    "curl-basic-auth": j("curl ", "-u ", "me:", "hunter22", " https://example.com"),
    "high-entropy-string": j("Zq8Xw2Lk9Vb4Nm7", "Pr1Ts6Yh3Jd5Fg0Hc8Ae2Ru"),
}

# Lines of the kind this repository really contains, none of them a credential.
CLEAN = [
    "/media/shengcn/d8d577a6-bf6a-42cc-a513-c02318d27956/projects/sys_workspace",
    "583e540b3163c18ab809c83dcc9fc86c80810e8e",
    "(`HintSystem: true`), so PolicyKit may ask for a password when mounting them on",
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
    "url = git@github.com:echushe/sys_workspace.git",
    '_TOKEN = re.compile(r"[A-Za-z0-9+=_-]{32,}")',
    "X_TOKEN = OTHER_NAME",
    j("pass", 'word = "${DB_PASSWORD}"'),
    j("https://user", ":${TOKEN}@example.com"),
    j("Authorization", ": Bearer <token>"),
    "def test_restore_target_prefers_previous_boot_snapshot_over_latest(self):",
    "sk-ssh-ed25519@openssh.com keys are supported",
    "ExecStart=%h/.local/bin/sessionctl save",
    "a login more than two minutes after boot left it with no anchor at all",
]


class ScanLineTest(unittest.TestCase):
    def test_each_rule_catches_its_sample(self):
        for rule, sample in SAMPLES.items():
            with self.subTest(rule=rule):
                rules = [r for r, _ in cc.scan_line(f"x = 1  {sample}  # trailing")]
                self.assertIn(rule, rules)

    def test_lines_like_this_repository_are_clean(self):
        for line in CLEAN:
            with self.subTest(line=line):
                self.assertEqual(cc.scan_line(line), [])

    def test_allow_marker_skips_a_reviewed_line(self):
        line = f"{SAMPLES['github-token']}  # {cc.ALLOW_MARKER}"
        self.assertEqual(cc.scan_line(line), [])

    def test_scan_text_reports_line_numbers(self):
        text = f"clean\n{SAMPLES['aws-access-key']}\nclean\n"
        self.assertEqual(cc.scan_text("f.txt", text),
                         [("f.txt:2", "aws-access-key", SAMPLES["aws-access-key"])])

    def test_mask_never_prints_the_whole_secret(self):
        secret = SAMPLES["github-token"]
        masked = cc.mask(secret)
        self.assertNotIn(secret, masked)
        self.assertIn(f"{len(secret)} chars", masked)
        self.assertNotIn("hunter22", cc.mask("hunter22"))


class ScanNameTest(unittest.TestCase):
    def test_key_and_secret_file_names(self):
        for path in ["id_ed25519", "keys/id_rsa", ".env", ".env.local", "deploy/server.pem",
                     "a.key", "store.p12", "config/secrets.yaml", "credentials.json", ".netrc"]:
            with self.subTest(path=path):
                self.assertEqual(cc.scan_name(path)[0][1], "credential-file-name")

    def test_ordinary_file_names(self):
        for path in ["README.md", "keyboard.py", "monkey.py", "keys.md", "docs/ai_history.md",
                     "systemd/sessionctl-save.timer", "tests/test_setup.py"]:
            with self.subTest(path=path):
                self.assertEqual(cc.scan_name(path), [])


class ScanMessageTest(unittest.TestCase):
    def test_body_is_scanned(self):
        msg = f"Add deploy script\n\nuses {SAMPLES['github-token']}\n"
        self.assertEqual([r for _, r, _ in cc.scan_message(msg)], ["github-token"])

    def test_comments_and_verbose_diff_are_ignored(self):
        msg = (f"Subject\n\n# {SAMPLES['github-token']}\n{cc.SCISSORS}\n"
               f"diff --git a/x b/x\n+{SAMPLES['aws-access-key']}\n")
        self.assertEqual(cc.scan_message(msg), [])


@unittest.skipUnless(shutil.which("git"), "git not installed")
class ScanStagedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "core.hooksPath", "/dev/null")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True)

    def write(self, name, data, mode="w"):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(data)

    def test_added_line_is_reported_with_its_location(self):
        self.write("clean.txt", "nothing here\n")
        self.write("sub dir/notes.md", f"line one\n{SAMPLES['github-token']}\n")
        self.git("add", ".")
        self.assertEqual(cc.scan_staged(self.dir),
                         [("sub dir/notes.md:2", "github-token", SAMPLES["github-token"])])

    def test_only_added_lines_count(self):
        # A credential already in history is not this commit's doing; the
        # repository-wide test below is what guards existing content.
        self.write("notes.md", f"{SAMPLES['github-token']}\nold\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "seed")
        self.write("notes.md", f"{SAMPLES['github-token']}\nnew\n")
        self.git("add", ".")
        self.assertEqual(cc.scan_staged(self.dir), [])

    def test_removing_a_credential_is_allowed(self):
        self.write("notes.md", f"{SAMPLES['github-token']}\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "seed")
        self.git("rm", "-q", "notes.md")
        self.assertEqual(cc.scan_staged(self.dir), [])

    def test_key_file_name_and_binary_content(self):
        self.write("id_ed25519", "\x00\x01binary\x00" + SAMPLES["private-key"] + "\n")
        self.git("add", ".")
        rules = {r for _, r, _ in cc.scan_staged(self.dir)}
        self.assertEqual(rules, {"credential-file-name", "private-key"})

    def test_nothing_staged(self):
        self.assertEqual(cc.scan_staged(self.dir), [])


@unittest.skipUnless(shutil.which("git") and os.path.isdir(os.path.join(REPO, ".git")),
                     "not running from a git checkout")
class RepositoryIsCleanTest(unittest.TestCase):
    def test_no_tracked_file_holds_a_credential(self):
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True,
                             capture_output=True).stdout.decode()
        paths = [os.path.join(REPO, p) for p in out.split("\0") if p]
        findings = cc.scan_files([p for p in paths if os.path.isfile(p)])
        self.assertEqual(findings, [], "\n".join(
            f"{where}  {rule}  {cc.mask(hit)}" for where, rule, hit in findings))


if __name__ == "__main__":
    unittest.main()
