from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__


APP_NAME = "noteshell"
DEFAULT_NOTE = "Notebook/Linux.md"
DEFAULT_MAX_OUTPUT_CHARS = 40000
DEFAULT_HISTORY_LIMIT = 2000
DEFAULT_FORGET_ON_EXIT = True
PROJECT_CONFIG_NAME = ".noteshell.json"

# Case-insensitive patterns checked against typed shell commands before they're
# passively recorded via the PROMPT_COMMAND hook. A match means the command is
# dropped silently (never written to state or the vault). Extend via
# `noteshell config --redact-pattern <regex>`.
#
# These require an actual value attached (KEY=value, -pVALUE, "Bearer xyz", ...)
# rather than bare keywords -- matching on the word "token" or "secret" alone is
# too broad and silently eats ordinary commands that just mention the word (e.g.
# `grep -E 'token|secret' access.log` while working through course material).
DEFAULT_REDACT_PATTERNS = [
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\s*[:=]\s*\S",
    r"-p\S{3,}",
    r"--password[= ]\S+",
    r"authorization:\s*bearer\s+\S+",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]

# Matches the start of an entry on a page: the current `From noteshell: <timestamp>`
# footer line, the `From obsnote: <timestamp>` footer this tool wrote before the
# noteshell rename, or the pre-9d9d741 "`command` _timestamp_" header still present
# in older vault pages.
ENTRY_HEADER_RE = re.compile(
    r"(?m)^(?:From (?:noteshell|obsnote): \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}.*|`[^`\n]+`\s+_[^_\n]+_.*)$"
)


@dataclass(frozen=True)
class Settings:
    vault: Path | None
    note: str
    max_output_chars: int
    redact_patterns: list[str]
    forget_on_exit: bool


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def config_path() -> Path:
    return xdg_config_home() / APP_NAME / "config.json"


def state_dir() -> Path:
    return xdg_state_home() / APP_NAME


def state_path() -> Path:
    return state_dir() / "last.json"


def hook_error_path() -> Path:
    return state_dir() / "hook-error.txt"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Expected an object in {path}")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        tmp.replace(path)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise SystemExit(f"Could not write {path}: {exc}") from exc


def safe_cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None


def find_project_config(start: Path | None) -> Path | None:
    if start is None:
        return None
    try:
        current = start.resolve()
    except OSError:
        return None
    for parent in (current, *current.parents):
        candidate = parent / PROJECT_CONFIG_NAME
        if candidate.exists():
            return candidate
    return None


def parse_bool_setting(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise SystemExit(f"{field} must be a boolean (true/false)")


def load_settings() -> Settings:
    cfg = load_json(config_path())
    project_config = find_project_config(safe_cwd())
    project_cfg = load_json(project_config) if project_config else {}

    vault_raw = os.environ.get("NOTESHELL_VAULT", project_cfg.get("vault", cfg.get("vault")))
    note = os.environ.get("NOTESHELL_NOTE", project_cfg.get("note", cfg.get("note", DEFAULT_NOTE)))
    max_output_raw = os.environ.get(
        "NOTESHELL_MAX_OUTPUT_CHARS",
        project_cfg.get("max_output_chars", cfg.get("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)),
    )

    try:
        max_output_chars = int(max_output_raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("max_output_chars must be an integer") from exc

    extra_patterns: list[str] = []
    for source in (cfg, project_cfg):
        raw_patterns = source.get("redact_patterns", [])
        if isinstance(raw_patterns, list):
            extra_patterns.extend(str(p) for p in raw_patterns)
    redact_patterns = DEFAULT_REDACT_PATTERNS + extra_patterns

    forget_on_exit_raw = os.environ.get(
        "NOTESHELL_FORGET_ON_EXIT",
        project_cfg.get("forget_on_exit", cfg.get("forget_on_exit", DEFAULT_FORGET_ON_EXIT)),
    )
    forget_on_exit = parse_bool_setting(forget_on_exit_raw, field="forget_on_exit")

    return Settings(
        vault=Path(vault_raw).expanduser() if vault_raw else None,
        note=str(note),
        max_output_chars=max_output_chars,
        redact_patterns=redact_patterns,
        forget_on_exit=forget_on_exit,
    )


def resolve_note_path(settings: Settings, note: str) -> Path:
    if settings.vault is None:
        raise SystemExit("No Obsidian vault configured. Run: noteshell config --vault /path/to/vault")
    note_path = Path(note)
    if note_path.is_absolute() or ".." in note_path.parts:
        raise SystemExit("Notebook path must be relative to the vault and may not contain '..'")
    return settings.vault / note_path


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def entry_footer(tags: str = "") -> str:
    footer = f"From noteshell: {now_stamp()}"
    if tags:
        footer = f"{footer} {tags}"
    return footer


def normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", "-", tag.strip().lstrip("#"))


def format_tags(tags: list[str] | None) -> str:
    if not tags:
        return ""
    seen: list[str] = []
    for tag in tags:
        normalized = normalize_tag(tag)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return " ".join(f"#{tag}" for tag in seen)


def normalize_page_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise SystemExit("Page name must not be empty.")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("Page name must be relative to the vault and may not contain '..'")
    if path.suffix.lower() not in (".md", ".markdown"):
        name = f"{name}.md"
    return name


def active_page_for(settings: Settings, data: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """Return (usable_active_page, mismatched_page).

    The active page only counts when it was set for the vault currently in
    effect. Project-local configs can swap vaults between directories, and a
    page set in one vault must not silently target a same-named file in
    another. When the vaults differ, the second element carries the ignored
    page name so status commands can explain why it isn't being used.
    """
    if data is None:
        data = load_last()
    page = data.get("active_page")
    if not (isinstance(page, str) and page.strip()):
        return None, None
    recorded = data.get("active_page_vault")
    if (
        isinstance(recorded, str)
        and settings.vault is not None
        and Path(recorded).expanduser() != settings.vault
    ):
        return None, page
    return page, None


def resolve_target_page(explicit: str | None, settings: Settings) -> str:
    if explicit:
        return normalize_page_name(explicit)
    active, _ = active_page_for(settings)
    return active if active else settings.note


def set_active_page(page: str, settings: Settings) -> None:
    save_last({"active_page": page, "active_page_vault": str(settings.vault) if settings.vault else None})


def append_markdown(settings: Settings, note: str, markdown: str) -> Path:
    path = resolve_note_path(settings, note)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if path.exists() and path.stat().st_size:
            fh.write("\n\n")
        fh.write(markdown.rstrip())
        fh.write("\n")
    return path


def fence(text: str, language: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def shell_join(argv: list[str]) -> str:
    return shlex.join(argv)


def strip_separator(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def clip_output(output: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(output) <= limit:
        return output, False
    head_len = limit // 2
    tail_len = limit - head_len
    clipped = (
        output[:head_len]
        + f"\n\n[noteshell clipped {len(output) - limit} characters]\n\n"
        + output[-tail_len:]
    )
    return clipped, True


def save_last(data: dict[str, Any]) -> None:
    current = load_json(state_path())
    current.update(data)
    current["updated_at"] = now_stamp()
    save_json(state_path(), current)


def load_last() -> dict[str, Any]:
    return load_json(state_path())


def command_history(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("command_history", [])
    if not isinstance(raw, list):
        return []
    history: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("note", "summary") and isinstance(item.get("text"), str):
            history.append({"at": str(item.get("at", "")), "type": str(item["type"]), "text": item["text"]})
        elif item.get("type") == "skipped" and isinstance(item.get("reason"), str):
            history.append({"at": str(item.get("at", "")), "type": "skipped", "reason": item["reason"]})
        elif isinstance(item.get("command"), str):
            entry: dict[str, Any] = {"at": str(item.get("at", "")), "type": "command", "command": item["command"]}
            if isinstance(item.get("status"), int):
                entry["status"] = item["status"]
            if isinstance(item.get("cwd"), str):
                entry["cwd"] = item["cwd"]
            history.append(entry)
    return history


def append_history_entry(data: dict[str, Any], history: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    history = [*history, entry]
    if len(history) > DEFAULT_HISTORY_LIMIT:
        trim = len(history) - DEFAULT_HISTORY_LIMIT
        history = history[trim:]
        all_markers = data.get("markers", {})
        if isinstance(all_markers, dict):
            for marker in all_markers.values():
                if isinstance(marker, dict) and isinstance(marker.get("index"), int):
                    marker["index"] = max(0, marker["index"] - trim)
    return history


def append_command_history(
    command: str,
    *,
    clear_output: bool = True,
    status: int | None = None,
    cwd: str | None = None,
) -> None:
    data = load_last()
    history = command_history(data)
    if history and history[-1].get("type") == "command" and history[-1]["command"] == command:
        if status is not None:
            history[-1]["status"] = status
        if cwd is not None:
            history[-1]["cwd"] = cwd
        data["command"] = command
        if clear_output:
            data.pop("output", None)
            data.pop("return_code", None)
        data["command_history"] = history
        data["updated_at"] = now_stamp()
        save_json(state_path(), data)
        return
    entry: dict[str, Any] = {"at": now_stamp(), "type": "command", "command": command}
    if status is not None:
        entry["status"] = status
    if cwd is not None:
        entry["cwd"] = cwd
    history = append_history_entry(data, history, entry)
    data["command"] = command
    if clear_output:
        data.pop("output", None)
        data.pop("return_code", None)
    data["command_history"] = history
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)


def append_annotation(text: str) -> None:
    data = load_last()
    history = command_history(data)
    history = append_history_entry(data, history, {"at": now_stamp(), "type": "note", "text": text})
    data["command_history"] = history
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)


def append_session_summary(text: str) -> None:
    data = load_last()
    history = command_history(data)
    history = append_history_entry(data, history, {"at": now_stamp(), "type": "summary", "text": text})
    data["command_history"] = history
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)


def note_skipped_command(reason: str) -> None:
    data = load_last()
    history = command_history(data)
    history = append_history_entry(data, history, {"at": now_stamp(), "type": "skipped", "reason": reason})
    count_key = f"skipped_{reason}_commands"
    current = data.get(count_key, 0)
    data[count_key] = (current if isinstance(current, int) else 0) + 1
    data["command_history"] = history
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)


def pop_skipped_counts(data: dict[str, Any]) -> dict[str, int]:
    skipped: dict[str, int] = {}
    for key in list(data):
        if key.startswith("skipped_") and key.endswith("_commands"):
            value = data.get(key)
            if isinstance(value, int) and value > 0:
                skipped[key.removeprefix("skipped_").removesuffix("_commands")] = value
            data.pop(key, None)
    return skipped


def hook_error() -> str | None:
    path = hook_error_path()
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def markers(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("markers", {})
    return raw if isinstance(raw, dict) else {}


def marker_name(value: str | None) -> str:
    return value.strip() if value and value.strip() else "default"


def next_auto_marker_name(all_markers: dict[str, Any]) -> str:
    n = len(all_markers) + 1
    while str(n) in all_markers:
        n += 1
    return str(n)


def resolve_marker_arg(raw_name: str | None) -> str:
    all_markers = markers(load_last())
    if raw_name is None and len(all_markers) == 1:
        return next(iter(all_markers))
    return marker_name(raw_name)


def history_since_marker(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = load_last()
    history = command_history(data)
    marker = markers(data).get(name)
    if not isinstance(marker, dict) or not isinstance(marker.get("index"), int):
        known = ", ".join(sorted(markers(data))) or "none"
        raise SystemExit(f"No marker named `{name}`. Known markers: {known}")
    return marker, history[marker["index"] :]


def require_last_output() -> dict[str, Any]:
    data = load_last()
    if not data.get("command") or data.get("output") is None:
        raise SystemExit(
            "No captured command output found yet.\n"
            "Run a command through noteshell first, for example: noteshell run -- ls -la"
        )
    return data


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_json(config_path())
    changed = False
    for attr, key in [
        ("vault", "vault"),
        ("note", "note"),
        ("max_output_chars", "max_output_chars"),
    ]:
        value = getattr(args, attr)
        if value is not None:
            cfg[key] = str(Path(value).expanduser()) if key == "vault" else value
            changed = True
    if args.redact_pattern:
        existing = cfg.get("redact_patterns", [])
        if not isinstance(existing, list):
            existing = []
        for pattern in args.redact_pattern:
            if pattern not in existing:
                existing.append(pattern)
        cfg["redact_patterns"] = existing
        changed = True
    if args.forget_on_exit is not None:
        cfg["forget_on_exit"] = args.forget_on_exit
        changed = True
    if changed:
        save_json(config_path(), cfg)
    settings = load_settings()
    project_config = find_project_config(safe_cwd())
    print(f"config: {config_path()}")
    if project_config:
        print(f"project config: {project_config}")
    print(f"vault: {settings.vault or '(unset)'}")
    print(f"note: {settings.note}")
    print(f"max_output_chars: {settings.max_output_chars}")
    print(f"redact_patterns: {', '.join(settings.redact_patterns)}")
    print(f"forget_on_exit: {settings.forget_on_exit}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    settings = load_settings()
    text_argv = strip_separator(args.text)
    if any(tok in ("--page", "-p", "--tag", "-t") for tok in text_argv):
        print(
            "noteshell note: warning: --page/--tag found inside the note text -- flags are only "
            "recognized before the text, so this was written as literal words.",
            file=sys.stderr,
        )
    text = " ".join(text_argv).strip() if text_argv else sys.stdin.read().strip()
    if not text:
        raise SystemExit("No note text provided.")
    page = resolve_target_page(args.page, settings)
    tags = format_tags(args.tag)
    path = append_markdown(settings, page, f"{entry_footer(tags)}\n\n{text}")
    print(path)
    return 0


def is_redacted(command: str, settings: Settings) -> bool:
    for pattern in settings.redact_patterns:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def is_capture_paused() -> bool:
    return bool(load_last().get("capture_paused"))


def set_capture_paused(paused: bool) -> bool:
    save_last({"capture_paused": paused})
    return is_capture_paused() == paused


def cmd_remember_cmd(args: argparse.Namespace) -> int:
    command = " ".join(strip_separator(args.command)).strip()
    if not command:
        return 0
    if is_capture_paused():
        return 0
    settings = load_settings()
    if is_redacted(command, settings):
        note_skipped_command("redacted")
        return 0
    append_command_history(command, status=args.status, cwd=args.cwd)
    return 0


def cmd_recording_status(_: argparse.Namespace) -> int:
    return 1 if is_capture_paused() else 0


def cmd_pause(_: argparse.Namespace) -> int:
    confirmed = set_capture_paused(True)
    print("Pausing passive shell-history capture...")
    print(f"Confirmed: capture_paused = {confirmed}")
    if confirmed:
        print("noteshell will not record anything typed at the shell until you run: noteshell resume")
        print("Explicit commands (note/run/annotate) still work normally -- only")
        print("the passive shell hook is paused.")
    else:
        print("WARNING: could not confirm the paused state was saved. Check permissions on:")
        print(f"  {state_path()}")
    return 0 if confirmed else 1


def forget_all() -> tuple[int, int]:
    """Clear all captured commands/output/markers from local state.

    Returns (commands forgotten, markers forgotten).
    """
    data = load_last()
    history = command_history(data)
    forgotten = len(history)
    marker_count = len(markers(data))
    for key in ("command_history", "command", "output", "return_code", "markers"):
        data.pop(key, None)
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    return forgotten, marker_count


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Any other errno (e.g. EPERM) means the pid exists but we can't signal it.
        return True
    return True


def clear_active_shell() -> None:
    data = load_last()
    if "active_shell" in data:
        data.pop("active_shell", None)
        data["updated_at"] = now_stamp()
        save_json(state_path(), data)


def check_stale_shell_session() -> None:
    """Detect a `noteshell shell` process that never reached its own exit cleanup.

    `kill -9` and crashes can't be intercepted by the process being killed, so
    there's no way for that process to clean up after itself. Instead, every
    later noteshell invocation checks whether the pid it recorded is still
    alive; if not, it applies the same forget_on_exit cleanup right now.
    """
    data = load_last()
    info = data.get("active_shell")
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return
    if pid_alive(info["pid"]):
        return
    marker = info.get("marker")
    data.pop("active_shell", None)
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    settings = load_settings()
    if settings.forget_on_exit:
        forgotten, marker_count = forget_all()
        print(
            f"noteshell: a `noteshell shell` session (marker `{marker}`, pid {info['pid']}) never "
            f"exited cleanly (crash or kill -9); cleared {forgotten} captured command(s) and "
            f"{marker_count} marker(s). Disable with `noteshell config --no-forget-on-exit`.",
            file=sys.stderr,
        )
    elif marker:
        print(
            f"noteshell: a `noteshell shell` session (marker `{marker}`, pid {info['pid']}) never "
            "exited cleanly (crash or kill -9); its captured commands are still in state -- run "
            "`noteshell since` to save them or `noteshell forget` to clear them.",
            file=sys.stderr,
        )


def cmd_forget(args: argparse.Namespace) -> int:
    if args.last is not None:
        if args.last <= 0:
            raise SystemExit("--last must be a positive number of commands.")
        data = load_last()
        history = command_history(data)
        forgotten = min(args.last, len(history))
        history = history[: len(history) - forgotten]
        data["command_history"] = history
        for marker in markers(data).values():
            if isinstance(marker, dict) and isinstance(marker.get("index"), int):
                marker["index"] = min(marker["index"], len(history))
        for key in ("command", "output", "return_code"):
            data.pop(key, None)
        data["updated_at"] = now_stamp()
        save_json(state_path(), data)
        print(f"Forgot the last {forgotten} captured command(s) and the last remembered command/output.")
    else:
        forgotten, marker_count = forget_all()
        print(
            f"Forgot {forgotten} captured command(s), the last remembered command/output, "
            f"and {marker_count} marker(s)."
        )
    print("Nothing already written to the vault was touched -- `noteshell undo` removes vault entries.")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = resolve_target_page(args.page, settings)
    path = resolve_note_path(settings, page)
    if not path.exists():
        raise SystemExit(f"No such page: {page}")
    text = path.read_text(encoding="utf-8")
    matches = list(ENTRY_HEADER_RE.finditer(text))
    if not matches:
        raise SystemExit(f"No noteshell entries found in {page}.")
    start = matches[-1].start()
    removed = text[start:].rstrip()
    remaining = text[:start].rstrip()
    path.write_text(f"{remaining}\n" if remaining else "", encoding="utf-8")
    lines = removed.splitlines()
    preview = next(
        (line for line in lines[1:] if line.strip() and not line.lstrip().startswith("```")),
        "",
    )
    print(f"Removed the last entry from {page}:")
    print(f"  {lines[0]}")
    if preview:
        print(f"  {preview}")
    left = len(matches) - 1
    print(f"{left} {'entry remains' if left == 1 else 'entries remain'}.")
    return 0


def cmd_resume(_: argparse.Namespace) -> int:
    confirmed = set_capture_paused(False)
    print("Resuming passive shell-history capture...")
    print(f"Confirmed: capture_paused = {is_capture_paused()}")
    return 0 if confirmed else 1


def cmd_annotate(args: argparse.Namespace) -> int:
    text_argv = strip_separator(args.text)
    text = " ".join(text_argv).strip() if text_argv else sys.stdin.read().strip()
    if not text:
        raise SystemExit("No annotation text provided.")
    append_annotation(text)
    print(f"Noted: {text}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    text_argv = strip_separator(args.text)
    text = " ".join(text_argv).strip() if text_argv else sys.stdin.read().strip()
    if not text:
        raise SystemExit("No summary text provided.")
    append_session_summary(text)
    print(f"Summarized: {text}")
    return 0


def append_last_output(page: str, tags: str) -> Path:
    settings = load_settings()
    data = require_last_output()
    command = data.get("command")
    output = data.get("output")
    output, clipped = clip_output(str(output), settings.max_output_chars)
    clipped_note = "\n\n_Output clipped by noteshell._" if clipped else ""
    return_code = data.get("return_code")
    exit_note = f"\n\n_Exited with code {return_code}._" if isinstance(return_code, int) and return_code != 0 else ""
    markdown = (
        f"{entry_footer(tags)}\n\n"
        f"{fence(str(command), 'bash')}\n\n"
        f"{fence(output, 'text')}"
        f"{exit_note}"
        f"{clipped_note}"
    )
    return append_markdown(settings, page, markdown)


def cmd_run(args: argparse.Namespace) -> int:
    command_argv = strip_separator(args.command)
    if not command_argv:
        raise SystemExit("Usage: noteshell run -- <command> [args...]")
    command = shell_join(command_argv)
    try:
        proc = subprocess.Popen(
            command_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
    except OSError as exc:
        raise SystemExit(f"noteshell run: {exc}") from exc
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    return_code = proc.wait()
    output = "".join(chunks)
    save_last({"command": command, "output": output, "return_code": return_code})
    append_command_history(command, clear_output=False)
    settings = load_settings()
    page = resolve_target_page(args.page, settings)
    tags = format_tags(args.tag)
    print(append_last_output(page, tags))
    return return_code


def set_marker(name: str, page: str) -> None:
    data = load_last()
    history = command_history(data)
    all_markers = markers(data)
    all_markers[name] = {"at": now_stamp(), "index": len(history), "page": page}
    data["markers"] = all_markers
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    print(f"Marked `{name}` at command #{len(history)} (page: {page})")


def cmd_mark(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = resolve_target_page(args.page, settings)
    raw = args.name.strip() if args.name else ""
    name = raw or next_auto_marker_name(markers(load_last()))
    if is_capture_paused():
        set_capture_paused(False)
        print("capture: OFF -> ON (recording until `since` or shell exit)", file=sys.stderr)
    set_marker(name, page)
    return 0


def cmd_mark_del(args: argparse.Namespace) -> int:
    name = resolve_marker_arg(args.name)
    data = load_last()
    all_markers = markers(data)
    if name not in all_markers:
        known = ", ".join(sorted(all_markers)) or "none"
        raise SystemExit(f"No marker named `{name}`. Known markers: {known}")
    del all_markers[name]
    data["markers"] = all_markers
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    print(f"Deleted marker `{name}`")
    if not all_markers and not is_capture_paused():
        set_capture_paused(True)
        print("capture: ON -> OFF (auto-paused; no markers left)", file=sys.stderr)
    return 0


def delete_marker(name: str) -> int:
    data = load_last()
    all_markers = markers(data)
    if name not in all_markers:
        return len(all_markers)
    del all_markers[name]
    data["markers"] = all_markers
    if not all_markers:
        pop_skipped_counts(data)
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    return len(all_markers)


def note_callout(text: str) -> str:
    lines = str(text).splitlines() or [""]
    if len(lines) == 1:
        return f"> [!note] {lines[0]}"
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"> [!note]\n{quoted}"


def summary_callout(text: str) -> str:
    lines = str(text).splitlines() or [""]
    if len(lines) == 1:
        return f"> [!summary] {lines[0]}"
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"> [!summary]\n{quoted}"


def abbrev_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def format_history_markdown(entries: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    command_lines: list[str] = []
    # Directory comments only appear when the stretch actually spans more than
    # one directory -- a uniform stretch stays clean.
    dirs = {e["cwd"] for e in entries if e.get("type") == "command" and isinstance(e.get("cwd"), str)}
    annotate_cwd = len(dirs) > 1
    current_cwd: str | None = None

    def flush_commands() -> None:
        nonlocal command_lines
        if command_lines:
            blocks.append(fence("\n".join(command_lines), "bash"))
            command_lines = []

    for entry in entries:
        if entry.get("type") == "note":
            flush_commands()
            blocks.append(note_callout(str(entry.get("text", ""))))
        elif entry.get("type") == "summary":
            continue
        elif entry.get("type") == "skipped":
            command_lines.append(f"# noteshell skipped a {entry.get('reason', 'unknown')} command")
        else:
            cwd = entry.get("cwd")
            if annotate_cwd and isinstance(cwd, str) and cwd != current_cwd:
                command_lines.append(f"# in {abbrev_home(cwd)}")
                current_cwd = cwd
            command_lines.append(str(entry.get("command", "")))
            status = entry.get("status")
            if isinstance(status, int) and status != 0:
                command_lines.append(f"# exited {status}")
    flush_commands()
    return "\n\n".join(blocks)


def format_session_summary_markdown(entries: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        summary_callout(str(entry.get("text", "")))
        for entry in entries
        if entry.get("type") == "summary"
    )


def format_session_markdown(entries: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        block
        for block in (format_session_summary_markdown(entries), format_history_markdown(entries))
        if block
    )


def filter_ok_only(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if entry.get("type") in ("note", "summary", "skipped") or entry.get("status") in (None, 0)
    ]


def resolve_since_page(args: argparse.Namespace, settings: Settings, marker: dict[str, Any]) -> str:
    if args.page:
        return normalize_page_name(args.page)
    marker_page = marker.get("page")
    if isinstance(marker_page, str) and marker_page.strip():
        return marker_page
    return resolve_target_page(None, settings)


def since_entries(args: argparse.Namespace, name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    marker, entries = history_since_marker(name)
    if not entries:
        raise SystemExit(f"No commands recorded since marker `{name}`.")
    if getattr(args, "ok_only", False):
        entries = filter_ok_only(entries)
        if not entries:
            raise SystemExit(f"No successful commands recorded since marker `{name}` (--ok-only).")
    return marker, entries


def cmd_history_since(args: argparse.Namespace) -> int:
    settings = load_settings()
    name = getattr(args, "_resolved_marker_name", None) or resolve_marker_arg(args.name)
    marker, entries = since_entries(args, name)
    page = resolve_since_page(args, settings, marker)
    tags = format_tags(args.tag)
    markdown = (
        f"{entry_footer(tags)}\n\n"
        f"{format_session_markdown(entries)}"
    )
    path = append_markdown(settings, page, markdown)
    print(path)
    return 0


def maybe_auto_pause_after_since(remaining_markers: int) -> None:
    if is_capture_paused():
        return
    if remaining_markers > 0:
        print(
            "capture: staying ON -- other markers are still pending (run `noteshell pause` if you're done)",
            file=sys.stderr,
        )
        return
    set_capture_paused(True)
    print("capture: ON -> OFF (auto-paused; run `noteshell shell` to start another session)", file=sys.stderr)


def cmd_since(args: argparse.Namespace) -> int:
    name = resolve_marker_arg(args.name)
    setattr(args, "_resolved_marker_name", name)
    if args.preview:
        marker, entries = since_entries(args, name)
        page = marker.get("page") or "(default)"
        print(f"--- commands since `{name}` (page: {page}, set {marker.get('at', 'unknown')}) ---\n")
        print(format_session_markdown(entries))
        return 0
    result = cmd_history_since(args)
    remaining_markers = delete_marker(name)
    maybe_auto_pause_after_since(remaining_markers)
    return result


def cmd_page_new(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = normalize_page_name(args.name)
    path = resolve_note_path(settings, page)
    if path.exists():
        raise SystemExit(
            f"Page already exists: {page}\nUse `noteshell page use {page}` to switch to it."
        )
    title = args.title or Path(page).stem.replace("_", " ").replace("-", " ")
    tags = format_tags(args.tag)
    content = f"# {title}\n\n{entry_footer(tags)}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    set_active_page(page, settings)
    print(f"Created page: {page}")
    print(f"Active page: {page}")
    return 0


def cmd_page_use(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = normalize_page_name(args.name)
    path = resolve_note_path(settings, page)
    if not path.exists():
        raise SystemExit(f"No such page: {page}\nUse `noteshell page new {page}` to create it.")
    set_active_page(page, settings)
    print(f"Active page: {page}")
    return 0


def cmd_page_show(_: argparse.Namespace) -> int:
    settings = load_settings()
    active, mismatched = active_page_for(settings)
    print(f"active: {active or '(none, using default)'}")
    if mismatched:
        print(f"  (ignoring `{mismatched}` -- it was set for a different vault)")
    print(f"default: {settings.note}")
    return 0


def cmd_pages(_: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.vault is None:
        raise SystemExit("No Obsidian vault configured. Run: noteshell config --vault /path/to/vault")
    active, _ = active_page_for(settings)
    files = sorted(p.relative_to(settings.vault).as_posix() for p in settings.vault.rglob("*.md"))
    if not files:
        print("No pages found in vault.")
        return 0
    for f in files:
        marker = "* " if f == active else "  "
        print(f"{marker}{f}")
    return 0


def cmd_show(_: argparse.Namespace) -> int:
    settings = load_settings()
    data = load_last()
    paused = bool(data.get("capture_paused"))
    print(f"capture: {'PAUSED (run `noteshell shell` to start a capture session)' if paused else 'active'}")
    active, mismatched = active_page_for(settings, data)
    print(f"active page: {active or '(none, using default)'}")
    if mismatched:
        print(f"  (ignoring `{mismatched}` -- it was set for a different vault)")
    print(f"default page: {settings.note}")
    command = data.get("command")
    if command:
        print(f"last command: {command}")
        if "return_code" in data:
            print(f"last exit code: {data.get('return_code')}")
        output = data.get("output")
        if output is not None:
            preview, clipped = clip_output(str(output), 2000)
            print("last output preview:")
            print(preview)
            if clipped:
                print("[preview truncated; noteshell run writes the full capture]")
    else:
        print("no command captured yet")
    all_markers = markers(data)
    if all_markers:
        history = command_history(data)
        print("markers:")
        for marker_key in sorted(all_markers):
            marker = all_markers[marker_key]
            if not isinstance(marker, dict):
                continue
            index = marker.get("index")
            page = marker.get("page") or "(default)"
            pending = len(history) - index if isinstance(index, int) else "unknown"
            print(f"  {marker_key}: {pending} pending, page: {page}, set: {marker.get('at', 'unknown')}")
    skipped = {
        key.removeprefix("skipped_").removesuffix("_commands"): value
        for key, value in data.items()
        if key.startswith("skipped_") and key.endswith("_commands") and isinstance(value, int) and value > 0
    }
    if skipped:
        parts = [f"{count} {reason}" for reason, count in sorted(skipped.items())]
        print(f"skipped commands pending: {', '.join(parts)}")
    error = hook_error()
    if error:
        print("shell hook warning:")
        print(f"  {error}")
    return 0


def split_entries(text: str) -> list[str]:
    matches = list(ENTRY_HEADER_RE.finditer(text))
    if not matches:
        return [text.strip()] if text.strip() else []
    entries = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append(text[start:end].rstrip())
    return entries


def cmd_tail(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = resolve_target_page(args.page, settings)
    path = resolve_note_path(settings, page)
    if not path.exists():
        raise SystemExit(f"No such page: {page}")
    entries = split_entries(path.read_text(encoding="utf-8"))
    if not entries:
        print(f"(no entries in {page})")
        return 0
    count = max(1, args.number)
    print(f"--- {page} (last {min(count, len(entries))} of {len(entries)} entries) ---\n")
    for entry in entries[-count:]:
        print(entry)
        print()
    return 0


BASH_SHELL_INIT = r'''
# Keep compound/pasted multiline commands as one history entry with literal
# newlines, so noteshell captures the command the way it was typed.
shopt -s cmdhist lithist

# A leading space on a typed command keeps it out of bash history entirely
# (and therefore out of noteshell's capture too) -- the standard bash convention
# for "don't record this". noteshell turns it on if it isn't already.
case ";$HISTCONTROL;" in
  *";ignorespace;"*|*";ignoreboth;"*) ;;
  *) HISTCONTROL="${HISTCONTROL:+$HISTCONTROL:}ignorespace" ;;
esac
export HISTCONTROL

__noteshell_prompt_command() {
  local last_status=$?
  local cmd
  cmd="$(HISTTIMEFORMAT= fc -ln -1 2>/dev/null | sed '1s/^[[:space:]]*//')"
  case "$cmd" in
    ""|noteshell*) ;;
    *)
      if noteshell remember-cmd --status "$last_status" --cwd "$PWD" -- "$cmd" >/dev/null 2>&1; then
        rm -f "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell/hook-error.txt" 2>/dev/null || true
      else
        mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell" 2>/dev/null || true
        printf "remember-cmd failed with exit %s; run noteshell doctor or noteshell show\n" "$?" > "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell/hook-error.txt" 2>/dev/null || true
      fi
      ;;
  esac
  if [ -z "${__NOTESHELL_ORIGINAL_PS1+x}" ]; then
    __NOTESHELL_ORIGINAL_PS1="$PS1"
  fi
  if noteshell recording-status >/dev/null 2>&1; then
    PS1="[\[\e[31m\]●\[\e[0m\] rec] $__NOTESHELL_ORIGINAL_PS1"
  else
    PS1="$__NOTESHELL_ORIGINAL_PS1"
  fi
  return $last_status
}

case ";$PROMPT_COMMAND;" in
  *";__noteshell_prompt_command;"*) ;;
  *) PROMPT_COMMAND="__noteshell_prompt_command${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
'''.strip()

# `status` is a read-only special parameter in zsh, hence `last_status`.
ZSH_SHELL_INIT = r'''
# A leading space on a typed command keeps it out of zsh history entirely
# (and therefore out of noteshell's capture too).
setopt hist_ignore_space

__noteshell_precmd() {
  local last_status=$?
  local cmd
  cmd="$(fc -ln -1 2>/dev/null | sed '1s/^[[:space:]]*//')"
  case "$cmd" in
    ""|noteshell*) ;;
    *)
      if noteshell remember-cmd --status "$last_status" --cwd "$PWD" -- "$cmd" >/dev/null 2>&1; then
        rm -f "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell/hook-error.txt" 2>/dev/null || true
      else
        mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell" 2>/dev/null || true
        printf "remember-cmd failed with exit %s; run noteshell doctor or noteshell show\n" "$?" > "${XDG_STATE_HOME:-$HOME/.local/state}/noteshell/hook-error.txt" 2>/dev/null || true
      fi
      ;;
  esac
  if [[ -z ${__NOTESHELL_ORIGINAL_PROMPT+x} ]]; then
    __NOTESHELL_ORIGINAL_PROMPT="$PROMPT"
  fi
  if noteshell recording-status >/dev/null 2>&1; then
    PROMPT="[%F{red}●%f rec] $__NOTESHELL_ORIGINAL_PROMPT"
  else
    PROMPT="$__NOTESHELL_ORIGINAL_PROMPT"
  fi
  return 0
}

autoload -Uz add-zsh-hook
if (( ${precmd_functions[(Ie)__noteshell_precmd]} == 0 )); then
  add-zsh-hook precmd __noteshell_precmd
fi
'''.strip()

SHELL_INIT = {"bash": BASH_SHELL_INIT, "zsh": ZSH_SHELL_INIT}
SHELL_RC = {"bash": ".bashrc", "zsh": ".zshrc"}
SHELL_BIN = {"bash": "bash", "zsh": "zsh"}
TEMP_SHELL_SHORT_COMMANDS = [
    "annotate",
    "config",
    "doctor",
    "forget",
    "note",
    "page",
    "pause",
    "resume",
    "run",
    "shell",
    "show",
    "since",
    "summary",
    "tail",
    "undo",
]


def cmd_shell_init(args: argparse.Namespace) -> int:
    print(SHELL_INIT[args.shell])
    return 0


def detect_shell() -> str:
    shell = Path(os.environ.get("SHELL", "")).name
    return shell if shell in SHELL_INIT else "bash"


def temp_shell_message(auto_mark: bool) -> str:
    if auto_mark:
        return "noteshell shell active. A marker is already open; run `noteshell since` to write it."
    return "noteshell shell active. Run `noteshell mark` to start capture; exit returns to your normal shell."


def temp_shell_shortcuts() -> str:
    return "\n".join(f'{command}() {{ command noteshell {command} "$@"; }}' for command in TEMP_SHELL_SHORT_COMMANDS)


def temp_bashrc(original_rc: Path, *, auto_mark: bool) -> str:
    rc = shlex.quote(str(original_rc))
    message = temp_shell_message(auto_mark)
    lines = [
        "# temporary noteshell shell",
        f"if [ -r {rc} ]; then",
        f"  source {rc}",
        "fi",
        'if command -v noteshell >/dev/null 2>&1; then',
        '  eval "$(noteshell shell-init bash)"',
        temp_shell_shortcuts(),
        "fi",
        f'printf "\\n{message}\\n"',
    ]
    return "\n".join(lines) + "\n"


def temp_zshrc(original_rc: Path, *, auto_mark: bool) -> str:
    rc = shlex.quote(str(original_rc))
    message = temp_shell_message(auto_mark)
    lines = [
        "# temporary noteshell shell",
        f"if [[ -r {rc} ]]; then",
        f"  source {rc}",
        "fi",
        "if command -v noteshell >/dev/null 2>&1; then",
        '  eval "$(noteshell shell-init zsh)"',
        temp_shell_shortcuts(),
        "fi",
        f'printf "\\n{message}\\n"',
    ]
    return "\n".join(lines) + "\n"


def confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def finish_shell_marker(name: str) -> None:
    if name not in markers(load_last()):
        return
    try:
        _, entries = since_entries(argparse.Namespace(ok_only=False), name)
    except SystemExit:
        entries = []
    if entries and confirm(f"Save commands from `{name}` with noteshell since before exiting?"):
        args = argparse.Namespace(name=name, preview=False, ok_only=False, page=None, tag=[])
        try:
            cmd_since(args)
            return
        except SystemExit as exc:
            print(exc, file=sys.stderr)
    remaining_markers = delete_marker(name)
    print(f"Deleted marker `{name}` (temporary shell exited without saving).")
    if remaining_markers == 0 and not is_capture_paused():
        set_capture_paused(True)
        print("capture: ON -> OFF (auto-paused; no markers left)", file=sys.stderr)


def cmd_shell(args: argparse.Namespace) -> int:
    shell = args.shell or detect_shell()
    binary = shutil.which(SHELL_BIN[shell])
    if binary is None:
        raise SystemExit(f"{shell} was not found on PATH.")
    settings = load_settings()
    marker_name_started = ""
    if args.no_mark:
        if not markers(load_last()):
            set_capture_paused(True)
    else:
        page = resolve_target_page(args.page, settings)
        marker_name_started = args.mark_name or next_auto_marker_name(markers(load_last()))
        if is_capture_paused():
            set_capture_paused(False)
            print("capture: OFF -> ON (recording until `since` or shell exit)", file=sys.stderr)
        set_marker(marker_name_started, page)
        save_last({"active_shell": {"pid": os.getpid(), "marker": marker_name_started}})
    with tempfile.TemporaryDirectory(prefix="noteshell-shell-") as tmp:
        tmpdir = Path(tmp)
        env = os.environ.copy()
        env["NOTESHELL_TEMP_SHELL"] = "1"
        if shell == "bash":
            rc = tmpdir / "bashrc"
            rc.write_text(temp_bashrc(Path.home() / SHELL_RC["bash"], auto_mark=not args.no_mark), encoding="utf-8")
            argv = [binary, "--rcfile", str(rc), "-i"]
        else:
            rc = tmpdir / ".zshrc"
            rc.write_text(temp_zshrc(Path.home() / SHELL_RC["zsh"], auto_mark=not args.no_mark), encoding="utf-8")
            env["ZDOTDIR"] = str(tmpdir)
            argv = [binary, "-i"]
        print(f"Starting temporary noteshell {shell} shell. Exit to return.")
        code = subprocess.call(argv, env=env)
    if marker_name_started:
        clear_active_shell()
        finish_shell_marker(marker_name_started)
    if settings.forget_on_exit:
        forgotten, marker_count = forget_all()
        if forgotten or marker_count:
            print(
                f"Cleared {forgotten} captured command(s) and {marker_count} marker(s) on exit "
                "(disable with `noteshell config --no-forget-on-exit`)."
            )
    return code


def cmd_doctor(_: argparse.Namespace) -> int:
    def mark(ok: bool) -> str:
        return "[ok]" if ok else "[!!]"

    problems: list[str] = []
    settings = load_settings()
    print("noteshell preflight check\n")

    if settings.vault is None:
        print("[!!] vault configured")
        problems.append("No vault configured. Run: noteshell config --vault /path/to/vault")
    else:
        exists = settings.vault.is_dir()
        print(f"{mark(exists)} vault exists: {settings.vault}")
        if not exists:
            problems.append(f"Vault directory does not exist: {settings.vault}")
        else:
            writable = os.access(settings.vault, os.W_OK)
            print(f"{mark(writable)} vault is writable")
            if not writable:
                problems.append(f"Vault directory is not writable: {settings.vault}")

    print(f"[ok] config file: {config_path()}")
    project_config = find_project_config(safe_cwd())
    if project_config:
        print(f"[ok] project config: {project_config}")

    if settings.vault is not None:
        page = resolve_target_page(None, settings)
        active, mismatched = active_page_for(settings)
        print(f"[ok] target page: {page} ({'active' if active else 'default'})")
        if mismatched:
            print(f"     (active page `{mismatched}` was set for a different vault and is ignored)")

    temp_shell = os.environ.get("NOTESHELL_TEMP_SHELL") == "1"
    if temp_shell:
        print("[ok] temporary noteshell shell active")
    else:
        print("[--] not currently inside `noteshell shell`")

    paused = is_capture_paused()
    if paused:
        print("[--] capture is paused")
    else:
        print("[ok] capture is active (not paused)")

    print()
    if problems:
        print(f"{len(problems)} issue(s) found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Config and vault look good.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noteshell")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command_name", required=True)

    config = sub.add_parser("config", help="show or update configuration")
    config.add_argument("--vault")
    config.add_argument("--note")
    config.add_argument("--max-output-chars", type=int)
    config.add_argument(
        "--redact-pattern",
        action="append",
        default=[],
        help="regex; matching typed commands are never captured by the shell hook (repeatable)",
    )
    config.add_argument(
        "--forget-on-exit",
        dest="forget_on_exit",
        action="store_true",
        default=None,
        help="clear captured commands/output when `noteshell shell` exits (default: on)",
    )
    config.add_argument(
        "--no-forget-on-exit",
        dest="forget_on_exit",
        action="store_false",
        help="keep captured commands/output around after `noteshell shell` exits",
    )
    config.set_defaults(func=cmd_config)

    note = sub.add_parser(
        "note",
        help="append a note; reads stdin when text is omitted",
        description="Flags (--page/--tag) must come before the text, since text swallows everything after it.",
    )
    note.add_argument("--page", "-p", help="target page for this entry only (does not change the active page)")
    note.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    note.add_argument("text", nargs=argparse.REMAINDER)
    note.set_defaults(func=cmd_note)

    run = sub.add_parser("run", help="run a command, capture output, and append it")
    run.add_argument("--page", "-p", help="target page for this entry only")
    run.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    since = sub.add_parser("since", help="append commands since a marker")
    since.add_argument(
        "name", nargs="?", help="marker name (defaults to 'default', or the only marker if just one exists)"
    )
    since.add_argument("--preview", action="store_true", help="render pending commands without writing or closing marker")
    since.add_argument(
        "--ok-only",
        dest="ok_only",
        action="store_true",
        help="skip commands that exited nonzero (keeps annotations)",
    )
    since.add_argument("--page", "-p", help="target page (defaults to the marker's page)")
    since.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    since.set_defaults(func=cmd_since)

    annotate = sub.add_parser(
        "annotate",
        help="insert a note into the current shell-session timeline",
        description="Reads stdin when text is omitted. Shows up inline (as a comment) the next time "
        "`since`/`since --preview` renders the commands recorded around it.",
    )
    annotate.add_argument("text", nargs=argparse.REMAINDER)
    annotate.set_defaults(func=cmd_annotate)

    summary = sub.add_parser(
        "summary",
        help="add a summary to the current shell session",
        description="Reads stdin when text is omitted. The summary is rendered before the command "
        "timeline the next time `since`/`since --preview` renders a shell session.",
    )
    summary.add_argument("text", nargs=argparse.REMAINDER)
    summary.set_defaults(func=cmd_summary)

    page = sub.add_parser("page", help="show, create, or switch the active page")
    page_sub = page.add_subparsers(dest="page_command")

    page_new = page_sub.add_parser("new", help="create a new page and make it active")
    page_new.add_argument("name")
    page_new.add_argument("--title", help="page title heading (defaults to the filename)")
    page_new.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    page_new.set_defaults(func=cmd_page_new)

    page_use = page_sub.add_parser("use", help="switch the active page")
    page_use.add_argument("name")
    page_use.set_defaults(func=cmd_page_use)

    page_list = page_sub.add_parser("list", help="list pages in the vault")
    page_list.set_defaults(func=cmd_pages)

    page.set_defaults(func=cmd_page_show)

    show = sub.add_parser("show", help="show current capture state without writing to the vault")
    show.set_defaults(func=cmd_show)

    tail = sub.add_parser("tail", help="show the last entries from a page, read-only")
    tail.add_argument("--page", "-p", help="page to read (defaults to the active page)")
    tail.add_argument("-n", "--number", type=int, default=3, help="number of entries to show (default: 3)")
    tail.set_defaults(func=cmd_tail)

    undo = sub.add_parser("undo", help="remove the last noteshell entry from a page")
    undo.add_argument("--page", "-p", help="page to undo on (defaults to the active page)")
    undo.set_defaults(func=cmd_undo)

    forget = sub.add_parser(
        "forget",
        help="clear captured commands/output from noteshell's state file",
        description="Clears what the shell hook has captured into local state (a shadow shell "
        "history). Does not touch anything already written to the vault -- see `noteshell undo`.",
    )
    forget.add_argument("--last", type=int, metavar="N", help="forget only the N most recent captured commands")
    forget.set_defaults(func=cmd_forget)

    shell = sub.add_parser("shell", help="start a temporary noteshell-enabled shell")
    shell.add_argument("--mark", dest="mark_name", help="marker name to start automatically (default: auto-numbered)")
    shell.add_argument("--page", "-p", help="page the automatic marker belongs to")
    shell.add_argument("--no-mark", action="store_true", help="start the temporary hook without creating a marker")
    shell.add_argument("shell", nargs="?", choices=sorted(SHELL_INIT), help="shell to start (default: $SHELL, or bash)")
    shell.set_defaults(func=cmd_shell)

    doctor = sub.add_parser("doctor", help="check vault, config, and current noteshell shell state")
    doctor.set_defaults(func=cmd_doctor)

    pause = sub.add_parser("pause", help="pause passive shell-history capture and confirm it's off")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="resume passive shell-history capture")
    resume.set_defaults(func=cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    # remember-cmd/recording-status run on every single typed command via the
    # shell hook -- skip the stale-session check there and let it run once per
    # actual noteshell invocation instead.
    if not argv or argv[0] not in ("remember-cmd", "recording-status", "shell-init"):
        check_stale_shell_session()
    if argv and argv[0] == "remember-cmd":
        remember = argparse.ArgumentParser(prog="noteshell remember-cmd")
        remember.add_argument("--status", type=int, default=None, help="exit status of the command")
        remember.add_argument("--cwd", default=None, help="directory the command ran in")
        remember.add_argument("command", nargs=argparse.REMAINDER)
        args = remember.parse_args(argv[1:])
        return cmd_remember_cmd(args)
    if argv and argv[0] == "recording-status":
        return cmd_recording_status(argparse.Namespace())
    if argv and argv[0] == "shell-init":
        shell_init = argparse.ArgumentParser(prog="noteshell shell-init")
        shell_init.add_argument("shell", choices=sorted(SHELL_INIT))
        args = shell_init.parse_args(argv[1:])
        return cmd_shell_init(args)
    if argv and argv[0] == "mark":
        mark = argparse.ArgumentParser(prog="noteshell mark")
        mark.add_argument("name", nargs="?")
        mark.add_argument("--page", "-p")
        args = mark.parse_args(argv[1:])
        return cmd_mark(args)
    if argv and argv[0] == "unmark":
        unmark = argparse.ArgumentParser(prog="noteshell unmark")
        unmark.add_argument("name", nargs="?")
        args = unmark.parse_args(argv[1:])
        return cmd_mark_del(args)
    args = parser.parse_args(argv)
    return int(args.func(args))
