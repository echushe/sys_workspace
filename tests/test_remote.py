"""Replay safety, foreground detection, grouping and real interactive bash startup."""
import ast
import copy
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from sessionctl_lib import collect, layout, remote, restore, snapshot

ROOT = Path(__file__).resolve().parents[1]


def resume(name="daily_0", host="user@example.test"):
    args = ["mosh", host, "--", "tmux", "new", "-A", "-s", name]
    return {"kind": "mosh", "args": args, "tmux_session": name}


class ParserTest(unittest.TestCase):
    def parse(self, command):
        return remote.parse_mosh(["mosh-client", "-# " + command + " |", "example.test", "60001"])

    def test_supported_forms_normalize(self):
        for command in ("new -A -s daily_0", "new-session -s daily_0", "attach -t daily_0",
                        "attach-session -d -t daily_0", "a -t daily_0", "new -D -A -s daily_0"):
            with self.subTest(command=command):
                r = self.parse("user@example.test -- tmux " + command)
                self.assertEqual(remote.replay_args(r),
                                 ["mosh", "user@example.test", "--", "tmux", "new-session", "-A", "-s", "daily_0"])

    def test_unsupported_text_never_retained(self):
        for text in ("example.test -- bash", "example.test -- tmux new -d -s x",
                     "example.test -- tmux new -A -s x ; touch /tmp/oops",
                     "--server=evil example.test -- tmux new -A -s x",
                     "example.test -- tmux attach -t x:0", "example.test -- tmux new -A -s 'x'",
                     "example.test -- tmux new -A -s $(id)", "-option -- tmux new -A -s x",
                     "example.test -- tmux new -A -s x\ny", "example.test  -- tmux new -A -s x",
                     "example.test -- tmux new -A -s x command"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), {"kind": "mosh", "replay": False})

    def test_snapshot_is_revalidated(self):
        good = resume()
        self.assertIsNotNone(remote.replay_args(good))
        for bad in (None, [], {}, {**good, "replay": False}, {**good, "kind": "ssh"},
                    {**good, "tmux_session": "other"}, {**good, "args": "mosh example.test"},
                    {**good, "args": ["sh", "-c", "id"]}, {**good, "args": [None] * 8}):
            self.assertIsNone(remote.replay_args(bad))
        detached = remote.replay_args(good, detach=True)
        self.assertIn("-D", detached)
        self.assertNotIn("-D", good["args"])

    def test_bad_process_title(self):
        for argv in ([], ["mosh-client"], ["other", "-# x |", "x", "1"],
                     ["mosh-client", "-# x", "x", "1"]):
            self.assertEqual(remote.parse_mosh(argv), remote.UNSUPPORTED)


class ForegroundTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        self.process(10, 1, 10, 20, "bash")
        self.process(20, 10, 20, 20, "mosh-client")
        children = self.proc / "10/task/10"
        children.mkdir(parents=True)
        (children / "children").write_text("20 ")

    def process(self, pid, parent, group, foreground, comm):
        p = self.proc / str(pid)
        p.mkdir(exist_ok=True)
        (p / "stat").write_text(f"{pid} (a name ) with spaces) S {parent} {group} 10 1 {foreground} 0")
        (p / "comm").write_text(comm + "\n")
        argv = ["mosh-client", "-# user@example.test -- tmux new -A -s daily_0 |", "example.test", "60001"]
        (p / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())

    def test_direct_foreground_child_without_environment(self):
        self.assertEqual(remote.foreground_mosh(10, self.proc)["tmux_session"], "daily_0")
        # There is deliberately no environ file in the fixture.

    def test_background_wrong_parent_or_wrong_command_ignored(self):
        for parent, group, comm in ((10, 99, "mosh-client"), (99, 20, "mosh-client"), (10, 20, "ssh")):
            self.process(20, parent, group, 20, comm)
            self.assertIsNone(remote.foreground_mosh(10, self.proc))

    def test_disappearing_and_idle_processes(self):
        (self.proc / "20/stat").unlink()
        self.assertIsNone(remote.foreground_mosh(10, self.proc))
        self.process(10, 1, 10, 10, "bash")
        self.assertIsNone(remote.foreground_mosh(10, self.proc))
        self.assertIsNone(remote.foreground_mosh(9999, self.proc))


class IntegrationTest(unittest.TestCase):
    def test_collect_adds_resume_and_preserves_pending(self):
        records = [{"pid": 10, "cwd": "/tmp", "conda": "base", "resume_pending":
                    "mosh example.test -- tmux new-session -A -s daily_0"}]
        with mock.patch.object(collect, "read_shell_records", return_value=records), \
             mock.patch.object(collect, "dbus_tree", return_value=(set(), set())), \
             mock.patch.object(collect, "atspi_window_tabs", return_value=None), \
             mock.patch.object(remote, "foreground_mosh", return_value=None):
            tab = collect.collect_terminals(prune=False)["windows"][0]["tabs"][0]
        self.assertEqual(tab["resume"]["tmux_session"], "daily_0")

    def test_group_remote_names_before_local_prompt(self):
        a = {"pid": 1, "cwd": "/tmp", "resume": resume("a")}
        b = {"pid": 2, "cwd": "/tmp", "resume": resume("b")}
        local = {"pid": 3, "cwd": "/tmp"}
        slots = [[{"tmux": "b", "cwd": "/remote"}], [{"tmux": "a"}, {"cwd": "/tmp"}]]
        groups = collect._group([a, b, local], 2, slots)
        self.assertEqual([w["tabs"] for w in groups], [[b], [a, local]])

    def test_digest_tracks_start_stop_and_session_change(self):
        base = {"terminals": {"windows": [{"tabs": [{"cwd": "/tmp", "conda": None}]}]}}
        a = copy.deepcopy(base)
        a["terminals"]["windows"][0]["tabs"][0]["resume"] = resume()
        b = copy.deepcopy(a)
        b["terminals"]["windows"][0]["tabs"][0]["resume"] = resume("other")
        self.assertEqual(len({snapshot.digest(s) for s in (base, a, b)}), 3)

    def test_remote_signature_waits_and_never_places_on_wrong_window(self):
        a = layout._terminal_signature([{}], ["~"], ["a"])
        b = layout._terminal_signature([{"tmux": "b"}])
        live_a = layout._terminal_signature([{"tmux": "a"}])
        self.assertEqual(layout._signature_score(a, live_a), 3)
        self.assertEqual(layout._signature_score(a, b), 0)
        record = {"sig": a, "wm_class": "Gnome-terminal"}
        live = {"sig": b, "wm_class": "Gnome-terminal"}
        matches, pending, _ = layout.match_windows([record], [live], strict=False)
        self.assertEqual(matches, [])
        self.assertEqual(pending, [record])

    def test_restore_quotes_revalidates_and_staggers_across_windows(self):
        state = {"windows": [{"tabs": [{"resume": resume()}]},
                             {"tabs": [{"cwd": "/tmp"}, {"resume": resume("second")}]}]}
        commands = restore.terminal_commands(state)
        scripts = [shlex.split(a[len("--command="):])[2] for cmd in commands for a in cmd if a.startswith("--command=")]
        self.assertIn("SESSIONCTL_RESUME_DELAY=0", scripts[0])
        self.assertEqual(scripts[1], "exec bash -i")
        self.assertIn("SESSIONCTL_RESUME_DELAY=1", scripts[2])
        self.assertNotIn("-D", scripts[0])
        self.assertIn("-D", restore._tab_script({"resume": resume()}, detach_mosh=True))
        self.assertEqual(restore._tab_script({"resume": {"kind": "mosh", "args": ["sh", "-c", "id"]}}), "exec bash -i")

    def test_accessibility_status_bar_suppresses_remote_prompt(self):
        with mock.patch.object(collect.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="null")) as run:
            collect.atspi_window_tabs()
        probe = run.call_args.args[0][2]
        tree = ast.parse(probe)
        nodes = [node for node in tree.body if
                 (isinstance(node, ast.FunctionDef) and node.name == "prompt_of") or
                 (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                  t.id in ("WITH_ENV", "NO_ENV") for t in node.targets))]
        api = mock.Mock()
        ns = {"re": __import__("re"), "Atspi": api, "terminal_under": lambda tab: tab}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "probe", "exec"), ns)
        for raw, expected in [
            ("(remote-env) user@server:~/remote$\n[daily_0] 0:bash*", (None, None, "daily_0")),
            ("(base) user@laptop:~/local$", ("base", "~/local", None)),
            ("no prompt", (None, None, None)),
        ]:
            api.Text.get_character_count.return_value = len(raw)
            api.Text.get_text.return_value = raw
            self.assertEqual(ns["prompt_of"](object()), expected)

    def test_real_bash_first_prompt_record_history_environment_and_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            (temp / "history").write_text("old_history_entry\n")
            (temp / "mosh").write_text('''#!/usr/bin/python3
import glob, json, os
from pathlib import Path
p = Path(os.environ["PROBE_DIR"])
records = glob.glob(str(p / "sessionctl/shells/*.json"))
with open(records[0]) as f: record = json.load(f)
(p / "result").write_text(json.dumps({"record": record, "args": __import__("sys").argv[1:],
 "env": [k for k in os.environ if k.startswith("SESSIONCTL_RESUME")]}))
with open(p / "calls", "a") as f: f.write("call\\n")
raise SystemExit(7)
''')
            (temp / "mosh").chmod(0o755)
            rc = temp / "rc"
            rc.write_text(f'HISTFILE={shlex.quote(str(temp / "history"))}\nPS1="READY> "\n'
                          f'source {shlex.quote(str(ROOT / "bash-hook.sh"))}\n')
            env = {**os.environ, "PATH": str(temp) + ":/usr/bin:/bin", "XDG_STATE_HOME": tmp,
                   "PROBE_DIR": tmp, "GNOME_TERMINAL_SCREEN": "/test", "HISTFILE": str(temp / "history"),
                   "SESSIONCTL_RESUME": "mosh user@example.test -- tmux new-session -A -s daily_0",
                   "SESSIONCTL_RESUME_DELAY": "0"}
            env.pop("_SESSIONCTL_HOOKED", None)
            master, slave = pty.openpty()
            proc = subprocess.Popen(["/bin/bash", "--noprofile", "--rcfile", str(rc), "-i"],
                                    stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            output = b""
            def read_until(marker):
                nonlocal output
                end = time.monotonic() + 5
                while marker not in output and time.monotonic() < end:
                    if select.select([master], [], [], 0.1)[0]:
                        try: output += os.read(master, 65536)
                        except OSError: break
                self.assertIn(marker, output)
            try:
                read_until(b"READY> ")
                result = json.loads((temp / "result").read_text())
                self.assertEqual(result["env"], [])
                self.assertEqual(result["record"]["resume_pending"], env["SESSIONCTL_RESUME"])
                self.assertEqual(result["args"], ["user@example.test", "--", "tmux", "new-session", "-A", "-s", "daily_0"])
                os.write(master, b"history > \"$PROBE_DIR/seen_history\"\n")
                end = time.monotonic() + 5
                while not (temp / "seen_history").exists() and time.monotonic() < end:
                    time.sleep(0.02)
                history = (temp / "seen_history").read_text()
                self.assertIn("old_history_entry", history)
                self.assertIn(env["SESSIONCTL_RESUME"], history)
                self.assertLess(history.index("old_history_entry"), history.index(env["SESSIONCTL_RESUME"]))
                self.assertEqual((temp / "calls").read_text(), "call\n")
                rec = json.loads(next((temp / "sessionctl/shells").glob("*.json")).read_text())
                self.assertEqual(rec["resume_pending"], "")
                os.write(master, b"exit\n")
                proc.wait(timeout=5)
                self.assertEqual(list((temp / "sessionctl/shells").glob("*.json")), [])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
