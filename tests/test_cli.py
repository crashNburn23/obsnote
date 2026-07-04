from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from noteshell import cli


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def write_config(self, **values: object) -> None:
        path = cli.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values), encoding="utf-8")

    def read_state(self) -> dict[str, object]:
        return json.loads(cli.state_path().read_text(encoding="utf-8"))

    def test_empty_config_uses_defaults(self) -> None:
        code, stdout, _ = self.run_cli("show")

        self.assertEqual(code, 0)
        self.assertIn("capture: active", stdout)
        self.assertIn("default page: Notebook/Linux.md", stdout)

    def test_config_command_writes_vault_and_note(self) -> None:
        code, stdout, _ = self.run_cli("config", "--vault", str(self.vault), "--note", "notes.md")

        self.assertEqual(code, 0)
        self.assertIn(f"vault: {self.vault}", stdout)
        self.assertIn("note: notes.md", stdout)
        self.assertEqual(json.loads(cli.config_path().read_text(encoding="utf-8"))["note"], "notes.md")

    def test_pause_resume_and_remember_cmd_respects_pause(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        code, stdout, _ = self.run_cli("pause")
        self.assertEqual(code, 0)
        self.assertIn("capture_paused = True", stdout)

        self.run_cli("remember-cmd", "--", "echo paused")
        self.assertNotIn("command", self.read_state())

        code, stdout, _ = self.run_cli("resume")
        self.assertEqual(code, 0)
        self.assertIn("capture_paused = False", stdout)

        self.run_cli("remember-cmd", "--", "echo active")
        self.assertEqual(self.read_state()["command"], "echo active")

    def test_note_writes_markdown_to_configured_vault(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        code, stdout, _ = self.run_cli("note", "--tag", "lab", "hello", "world")

        self.assertEqual(code, 0)
        note = self.vault / "notes.md"
        self.assertEqual(stdout.strip(), str(note))
        text = note.read_text(encoding="utf-8")
        self.assertIn("hello world", text)
        self.assertIn("From noteshell:", text)
        self.assertIn("#lab", text)
        self.assertLess(text.index("From noteshell:"), text.index("hello world"))

    def test_mark_since_writes_command_history(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        self.run_cli("annotate", "checkpoint")
        self.run_cli("remember-cmd", "--", "echo two")
        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        note = Path(stdout.strip())
        self.assertEqual(note, self.vault / "notes.md")
        text = note.read_text(encoding="utf-8")
        expected = "```bash\necho one\n```\n\n> [!note] checkpoint\n\n```bash\necho two\n```"
        self.assertIn(expected, text)
        self.assertIn("From noteshell:", text)
        self.assertLess(text.index("From noteshell:"), text.index("```bash"))

    def test_mark_summary_posts_before_command_history(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        self.run_cli("summary", "installed dependencies and reached failing tests")
        self.run_cli("remember-cmd", "--", "pytest")
        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        summary = "> [!summary] installed dependencies and reached failing tests"
        self.assertIn(summary, text)
        self.assertLess(text.index(summary), text.index("```bash\necho one"))
        self.assertLess(text.index(summary), text.index("pytest"))

    def test_remember_cmd_preserves_multiline_command(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        command = "cat <<'EOF'\nalpha\nbeta\nEOF"

        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", command)
        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["command"], command)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertIn(f"```bash\n{command}\n```", text)

    def test_shell_init_uses_multiline_history_capture(self) -> None:
        code, stdout, _ = self.run_cli("shell-init", "bash")

        self.assertEqual(code, 0)
        self.assertIn("shopt -s cmdhist lithist", stdout)
        self.assertIn("fc -ln -1", stdout)
        self.assertNotIn("history 1", stdout)

    def test_tail_splits_entries_in_current_format(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        for i in range(3):
            self.run_cli("note", f"entry number {i}")

        code, stdout, _ = self.run_cli("tail", "-n", "2")

        self.assertEqual(code, 0)
        self.assertIn("last 2 of 3 entries", stdout)
        self.assertNotIn("entry number 0", stdout)
        self.assertIn("entry number 1", stdout)
        self.assertIn("entry number 2", stdout)

    def test_tail_splits_legacy_entry_headers(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        (self.vault / "notes.md").write_text(
            "`echo one` _2025-01-01 10:00:00 +0000_\n\nold entry\n\n"
            "`echo two` _2025-01-01 11:00:00 +0000_\n\nnewer entry\n",
            encoding="utf-8",
        )

        code, stdout, _ = self.run_cli("tail", "-n", "1")

        self.assertEqual(code, 0)
        self.assertIn("last 1 of 2 entries", stdout)
        self.assertNotIn("old entry", stdout)
        self.assertIn("newer entry", stdout)

    def test_pause_confirms_capture_off(self) -> None:
        code, stdout, _ = self.run_cli("pause")
        self.assertEqual(code, 0)
        self.assertIn("capture_paused = True", stdout)

        self.run_cli("resume")
        code, stdout, _ = self.run_cli("pause")
        self.assertEqual(code, 0)
        self.assertIn("capture_paused = True", stdout)

    def test_shell_starts_marked_temporary_bash_session_and_discards_on_exit(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        home = self.root / "home"
        home.mkdir()
        (home / ".bashrc").write_text("alias ll='ls -l'\n", encoding="utf-8")
        launched: dict[str, object] = {}

        def fake_call(argv: list[str], env: dict[str, str]) -> int:
            rc = Path(argv[2])
            launched["argv"] = argv
            launched["env"] = env
            launched["rc_text"] = rc.read_text(encoding="utf-8")
            self.assertTrue(rc.exists())
            return 17

        with (
            mock.patch.object(cli.Path, "home", return_value=home),
            mock.patch.object(cli.shutil, "which", return_value="/bin/bash"),
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call),
            mock.patch("builtins.input", return_value="n"),
        ):
            code, stdout, _ = self.run_cli("shell", "--mark", "lab", "bash")

        self.assertEqual(code, 17)
        self.assertIn("Marked `lab`", stdout)
        self.assertIn("Starting temporary noteshell bash shell", stdout)
        self.assertIn("Deleted marker `lab`", stdout)
        self.assertEqual(launched["argv"], ["/bin/bash", "--rcfile", launched["argv"][2], "-i"])
        self.assertEqual(launched["env"]["NOTESHELL_TEMP_SHELL"], "1")
        self.assertIn("source", str(launched["rc_text"]))
        self.assertIn("noteshell shell-init bash", str(launched["rc_text"]))
        self.assertIn('since() { command noteshell since "$@"; }', str(launched["rc_text"]))
        self.assertIn('page() { command noteshell page "$@"; }', str(launched["rc_text"]))
        state = self.read_state()
        self.assertTrue(state["capture_paused"])
        self.assertNotIn("markers", state)
        self.assertNotIn("active_shell", state)

    def test_shell_exit_can_save_pending_session_with_since(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        home = self.root / "home"
        home.mkdir()

        def fake_call(argv: list[str], env: dict[str, str]) -> int:
            self.run_cli("remember-cmd", "--", "echo from shell")
            return 0

        with (
            mock.patch.object(cli.Path, "home", return_value=home),
            mock.patch.object(cli.shutil, "which", return_value="/bin/bash"),
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call),
            mock.patch("builtins.input", return_value="y"),
        ):
            code, stdout, _ = self.run_cli("shell", "--mark", "lab", "bash")

        self.assertEqual(code, 0)
        self.assertIn(str(self.vault / "notes.md"), stdout)
        text = (self.vault / "notes.md").read_text(encoding="utf-8")
        self.assertIn("echo from shell", text)
        state = self.read_state()
        self.assertNotIn("markers", state)
        self.assertNotIn("command_history", state)
        self.assertTrue(state["capture_paused"])

    def test_shell_exit_clears_captured_state_by_default(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        home = self.root / "home"
        home.mkdir()

        def fake_call(argv: list[str], env: dict[str, str]) -> int:
            self.run_cli("remember-cmd", "--", "echo from shell")
            return 0

        with (
            mock.patch.object(cli.Path, "home", return_value=home),
            mock.patch.object(cli.shutil, "which", return_value="/bin/bash"),
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call),
            mock.patch("builtins.input", return_value="n"),
        ):
            code, stdout, _ = self.run_cli("shell", "--mark", "lab", "bash")

        self.assertEqual(code, 0)
        self.assertIn("Cleared", stdout)
        state = self.read_state()
        self.assertNotIn("markers", state)
        self.assertNotIn("command_history", state)
        self.assertNotIn("command", state)

    def test_shell_no_forget_on_exit_keeps_captured_state(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md", forget_on_exit=False)
        home = self.root / "home"
        home.mkdir()

        def fake_call(argv: list[str], env: dict[str, str]) -> int:
            self.run_cli("remember-cmd", "--", "echo from shell")
            return 0

        with (
            mock.patch.object(cli.Path, "home", return_value=home),
            mock.patch.object(cli.shutil, "which", return_value="/bin/bash"),
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call),
            mock.patch("builtins.input", return_value="n"),
        ):
            code, stdout, _ = self.run_cli("shell", "--mark", "lab", "bash")

        self.assertEqual(code, 0)
        self.assertNotIn("Cleared", stdout)
        state = self.read_state()
        self.assertEqual(
            [e["command"] for e in state["command_history"]],
            ["echo from shell"],
        )

    def test_config_reports_and_toggles_forget_on_exit(self) -> None:
        code, stdout, _ = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("forget_on_exit: True", stdout)

        code, stdout, _ = self.run_cli("config", "--no-forget-on-exit")
        self.assertEqual(code, 0)
        self.assertIn("forget_on_exit: False", stdout)
        self.assertFalse(
            json.loads(cli.config_path().read_text(encoding="utf-8"))["forget_on_exit"]
        )

        code, stdout, _ = self.run_cli("config", "--forget-on-exit")
        self.assertEqual(code, 0)
        self.assertIn("forget_on_exit: True", stdout)

    def _inject_dead_active_shell(self, marker: str) -> None:
        proc = subprocess.Popen(["true"])
        dead_pid = proc.pid
        proc.wait()
        data = self.read_state()
        data["active_shell"] = {"pid": dead_pid, "marker": marker}
        cli.save_json(cli.state_path(), data)

    def test_stale_shell_session_is_detected_and_cleaned_up(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        self._inject_dead_active_shell("lab")

        code, stdout, stderr = self.run_cli("show")

        self.assertEqual(code, 0)
        self.assertIn("never exited cleanly", stderr)
        state = self.read_state()
        self.assertNotIn("active_shell", state)
        self.assertNotIn("markers", state)
        self.assertNotIn("command_history", state)

    def test_stale_shell_session_with_forget_on_exit_disabled_warns_but_keeps_state(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md", forget_on_exit=False)
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        self._inject_dead_active_shell("lab")

        code, stdout, stderr = self.run_cli("show")

        self.assertEqual(code, 0)
        self.assertIn("never exited cleanly", stderr)
        state = self.read_state()
        self.assertNotIn("active_shell", state)
        self.assertEqual([e["command"] for e in state["command_history"]], ["echo one"])
        self.assertIn("lab", state["markers"])

    def test_active_shell_with_live_pid_is_left_alone(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        data = self.read_state()
        data["active_shell"] = {"pid": os.getpid(), "marker": "lab"}
        cli.save_json(cli.state_path(), data)

        code, stdout, stderr = self.run_cli("show")

        self.assertEqual(code, 0)
        self.assertNotIn("never exited cleanly", stderr)
        state = self.read_state()
        self.assertIn("active_shell", state)
        self.assertIn("lab", state["markers"])

    def test_shell_no_mark_starts_temporary_zsh_hook_paused(self) -> None:
        home = self.root / "home"
        home.mkdir()
        launched: dict[str, object] = {}

        def fake_call(argv: list[str], env: dict[str, str]) -> int:
            rc = Path(env["ZDOTDIR"]) / ".zshrc"
            launched["argv"] = argv
            launched["env"] = env
            launched["rc_text"] = rc.read_text(encoding="utf-8")
            self.assertTrue(rc.exists())
            return 0

        with (
            mock.patch.object(cli.Path, "home", return_value=home),
            mock.patch.object(cli.shutil, "which", return_value="/bin/zsh"),
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call),
        ):
            code, stdout, _ = self.run_cli("shell", "--no-mark", "zsh")

        self.assertEqual(code, 0)
        self.assertIn("Starting temporary noteshell zsh shell", stdout)
        self.assertEqual(launched["argv"], ["/bin/zsh", "-i"])
        self.assertEqual(launched["env"]["NOTESHELL_TEMP_SHELL"], "1")
        self.assertIn("noteshell shell-init zsh", str(launched["rc_text"]))
        self.assertIn('pause() { command noteshell pause "$@"; }', str(launched["rc_text"]))
        state = self.read_state()
        self.assertTrue(state["capture_paused"])
        self.assertNotIn("markers", state)

    def test_shell_install_and_uninstall_are_removed(self) -> None:
        for name in ("shell-install", "shell-uninstall"):
            with self.assertRaises(SystemExit):
                self.run_cli(name, "bash")

    def test_public_help_omits_internal_and_removed_commands(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        text = stdout.getvalue()
        self.assertNotIn("shell-install", text)
        self.assertNotIn("shell-uninstall", text)
        self.assertNotIn("shell-init", text)
        self.assertNotIn(" mark ", text)
        self.assertNotIn("unmark", text)
        self.assertNotIn("synth", text)

    def test_mark_auto_resumes_and_since_auto_pauses_capture(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("pause")
        self.assertTrue(self.read_state()["capture_paused"])

        code, _, stderr = self.run_cli("mark", "lab")
        self.assertEqual(code, 0)
        self.assertFalse(self.read_state()["capture_paused"])
        self.assertIn("OFF -> ON", stderr)

        self.run_cli("remember-cmd", "--", "echo one")
        self.assertEqual([e["command"] for e in self.read_state()["command_history"]], ["echo one"])

        code, stdout, stderr = self.run_cli("since", "lab")
        self.assertEqual(code, 0)
        self.assertEqual(Path(stdout.strip()), self.vault / "notes.md")
        self.assertIn("ON -> OFF", stderr)
        self.assertTrue(self.read_state()["capture_paused"])

    def test_since_leaves_capture_on_when_other_markers_pending(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab1")
        self.run_cli("mark", "lab2")
        self.run_cli("remember-cmd", "--", "echo one")

        code, _, stderr = self.run_cli("since", "lab1")
        self.assertEqual(code, 0)
        self.assertIn("staying ON", stderr)
        self.assertFalse(self.read_state().get("capture_paused"))
        self.assertEqual(set(self.read_state()["markers"]), {"lab2"})

    def test_since_with_no_other_markers_deletes_marker_and_auto_pauses(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")

        code, _, stderr = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        self.assertIn("ON -> OFF", stderr)
        state = self.read_state()
        self.assertEqual(state.get("markers"), {})
        self.assertTrue(state["capture_paused"])

    def test_unmark_auto_pauses_when_last_marker_removed(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.assertFalse(self.read_state().get("capture_paused"))

        code, _, stderr = self.run_cli("unmark", "lab")
        self.assertEqual(code, 0)
        self.assertIn("ON -> OFF", stderr)
        self.assertTrue(self.read_state()["capture_paused"])

    def test_unmark_with_no_name_deletes_the_sole_marker(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab1")

        code, stdout, _ = self.run_cli("unmark")

        self.assertEqual(code, 0)
        self.assertIn("Deleted marker `lab1`", stdout)
        self.assertEqual(self.read_state().get("markers", {}), {})

    def test_unmark_with_no_name_and_multiple_markers_requires_a_name(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab1")
        self.run_cli("mark", "lab2")

        with self.assertRaises(SystemExit):
            self.run_cli("unmark")

        state = self.read_state()
        self.assertEqual(set(state["markers"]), {"lab1", "lab2"})

    def test_mark_with_no_name_auto_numbers(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        code, stdout, _ = self.run_cli("mark")
        self.assertEqual(code, 0)
        self.assertIn("Marked `1`", stdout)

        code, stdout, _ = self.run_cli("mark")
        self.assertEqual(code, 0)
        self.assertIn("Marked `2`", stdout)

        self.assertEqual(set(self.read_state()["markers"]), {"1", "2"})

    def test_mark_then_since_with_no_names_round_trips(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        self.run_cli("mark")
        self.run_cli("remember-cmd", "--", "echo one")
        code, stdout, _ = self.run_cli("since")

        self.assertEqual(code, 0)
        text = (self.vault / "notes.md").read_text(encoding="utf-8")
        self.assertIn("echo one", text)

    def test_since_preview_does_not_write_or_consume_marker(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")

        code, stdout, _ = self.run_cli("since", "lab", "--preview")

        self.assertEqual(code, 0)
        self.assertIn("commands since `lab`", stdout)
        self.assertIn("echo one", stdout)
        self.assertFalse((self.vault / "notes.md").exists())
        self.assertIn("lab", self.read_state()["markers"])

    def test_doctor(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        code, stdout, _ = self.run_cli("doctor")
        self.assertIn("noteshell preflight check", stdout)

    def test_doctor_accepts_temporary_shell(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        with (
            mock.patch.dict(os.environ, {"NOTESHELL_TEMP_SHELL": "1"}, clear=False),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/noteshell"),
        ):
            code, stdout, _ = self.run_cli("doctor")

        self.assertEqual(code, 0)
        self.assertIn("temporary noteshell shell active", stdout)

    def test_legacy_aliases_are_removed(self) -> None:
        for name in ("start", "stop", "last", "synth", "history-since", "mark-list", "save"):
            with self.assertRaises(SystemExit):
                self.run_cli(name)

    def test_run_no_append_option_is_removed(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        with self.assertRaises(SystemExit):
            self.run_cli("run", "--no-append", "--", "echo", "hi")

    def test_run_synth_option_is_removed(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        with self.assertRaises(SystemExit):
            self.run_cli("run", "--synth", "--", "echo", "hi")

    def test_undo_removes_only_last_entry(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("note", "keep me")
        self.run_cli("note", "remove me")

        code, stdout, _ = self.run_cli("undo")

        self.assertEqual(code, 0)
        self.assertIn("Removed the last entry", stdout)
        text = (self.vault / "notes.md").read_text(encoding="utf-8")
        self.assertIn("keep me", text)
        self.assertNotIn("remove me", text)

        self.run_cli("undo")
        with self.assertRaises(SystemExit):
            self.run_cli("undo")

    def test_forget_clears_captured_state(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "echo one")
        self.run_cli("remember-cmd", "--", "echo two")

        code, stdout, _ = self.run_cli("forget", "--last", "1")
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual([e["command"] for e in state["command_history"]], ["echo one"])
        self.assertNotIn("command", state)

        code, _, _ = self.run_cli("forget")
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertNotIn("command_history", state)
        self.assertNotIn("markers", state)

    def test_since_renders_exit_codes_and_cwd_changes(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--status", "0", "--cwd", "/srv/one", "--", "echo ok")
        self.run_cli("remember-cmd", "--status", "1", "--cwd", "/srv/two", "--", "false")

        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertIn("# in /srv/one", text)
        self.assertIn("# in /srv/two", text)
        self.assertIn("false\n# exited 1", text)
        self.assertNotIn("echo ok\n# exited", text)

    def test_since_ok_only_drops_failed_commands(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--status", "0", "--", "echo ok")
        self.run_cli("remember-cmd", "--status", "1", "--", "false")

        code, stdout, _ = self.run_cli("since", "lab", "--ok-only")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertIn("echo ok", text)
        self.assertNotIn("false", text)

    def test_duplicate_command_updates_status_and_cwd(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--status", "1", "--cwd", "/srv/old", "--", "pytest")
        self.run_cli("remember-cmd", "--status", "0", "--cwd", "/srv/new", "--", "pytest")

        code, stdout, _ = self.run_cli("since", "lab", "--ok-only")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertIn("pytest", text)
        self.assertNotIn("# exited 1", text)
        history = self.read_state()["command_history"]
        self.assertEqual(history[-1]["status"], 0)
        self.assertEqual(history[-1]["cwd"], "/srv/new")

    def test_redacted_command_leaves_placeholder_without_command_text(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "TOKEN=supersecret curl example.test")

        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertIn("# noteshell skipped a redacted command", text)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("skipped_redacted_commands", self.read_state())

    def test_since_uniform_cwd_is_not_annotated(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--cwd", "/srv/one", "--", "echo a")
        self.run_cli("remember-cmd", "--cwd", "/srv/one", "--", "echo b")

        code, stdout, _ = self.run_cli("since", "lab")

        self.assertEqual(code, 0)
        text = Path(stdout.strip()).read_text(encoding="utf-8")
        self.assertNotIn("# in /srv/one", text)

    def test_active_page_ignored_when_vault_changes(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("page", "new", "Course/Lab1")

        other_vault = self.root / "other-vault"
        other_vault.mkdir()
        self.write_config(vault=str(other_vault), note="notes.md")

        code, stdout, _ = self.run_cli("note", "where does this land")

        self.assertEqual(code, 0)
        self.assertEqual(Path(stdout.strip()), other_vault / "notes.md")
        self.assertFalse((other_vault / "Course" / "Lab1.md").exists())

        _, stdout, _ = self.run_cli("page")
        self.assertIn("it was set for a different vault", stdout)

    def test_page_list_lists_pages(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("page", "new", "Course/Lab1")
        self.run_cli("note", "--page", "scratch", "hello")

        code, stdout, _ = self.run_cli("page", "list")

        self.assertEqual(code, 0)
        self.assertIn("* Course/Lab1.md", stdout)
        self.assertIn("  scratch.md", stdout)

    def test_top_level_pages_command_is_removed(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli("pages")

    def test_shell_init_zsh_prints_precmd_hook(self) -> None:
        code, stdout, _ = self.run_cli("shell-init", "zsh")

        self.assertEqual(code, 0)
        self.assertIn("add-zsh-hook precmd __noteshell_precmd", stdout)
        self.assertIn('remember-cmd --status "$last_status" --cwd "$PWD"', stdout)
        self.assertIn("●%f rec", stdout)

    def test_recording_status_reflects_capture_state(self) -> None:
        code, _, _ = self.run_cli("recording-status")
        self.assertEqual(code, 0)

        self.run_cli("pause")
        code, _, _ = self.run_cli("recording-status")
        self.assertEqual(code, 1)

    def test_show_includes_marker_details_skipped_count_and_hook_warning(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")
        self.run_cli("mark", "lab")
        self.run_cli("remember-cmd", "--", "TOKEN=supersecret curl example.test")
        cli.hook_error_path().parent.mkdir(parents=True, exist_ok=True)
        cli.hook_error_path().write_text("remember-cmd failed with exit 1", encoding="utf-8")

        code, stdout, _ = self.run_cli("show")

        self.assertEqual(code, 0)
        self.assertIn("markers:", stdout)
        self.assertIn("lab: 1 pending, page: notes.md", stdout)
        self.assertIn("skipped commands pending: 1 redacted", stdout)
        self.assertIn("shell hook warning:", stdout)

    def test_invalid_page_path_is_rejected(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        with self.assertRaises(SystemExit) as raised:
            self.run_cli("note", "--page", "../escape", "nope")

        self.assertIn("may not contain '..'", str(raised.exception))

    def test_write_errors_are_clean_system_exits(self) -> None:
        bad_config_home = self.root / "not-a-dir"
        bad_config_home.write_text("occupied", encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(bad_config_home)}, clear=False):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli("config", "--note", "notes.md")

        self.assertIn("Could not write", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
