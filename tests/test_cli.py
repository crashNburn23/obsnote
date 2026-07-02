from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obsnote import cli


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

    def test_stop_resume_and_remember_cmd_respects_pause(self) -> None:
        self.write_config(vault=str(self.vault), note="notes.md")

        code, stdout, _ = self.run_cli("stop")
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
        self.assertIn("_note_ #lab", text)
        self.assertIn("hello world", text)

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
        self.assertIn("_commands since lab_", text)
        self.assertIn("echo one", text)
        self.assertIn("# checkpoint", text)
        self.assertIn("echo two", text)

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
