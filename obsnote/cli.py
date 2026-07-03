from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__


APP_NAME = "obsnote"
DEFAULT_NOTE = "Notebook/Linux.md"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1"
DEFAULT_MAX_OUTPUT_CHARS = 40000
DEFAULT_HISTORY_LIMIT = 2000
PROJECT_CONFIG_NAME = ".obsnote.json"

# Case-insensitive patterns checked against typed shell commands before they're
# passively recorded via the PROMPT_COMMAND hook. A match means the command is
# dropped silently (never written to state or the vault). Extend via
# `obsnote config --redact-pattern <regex>`.
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

# Matches the start of an entry on a page: the current `From obsnote: <timestamp>`
# footer line, or the pre-9d9d741 "`command` _timestamp_" header still present in
# older vault pages.
ENTRY_HEADER_RE = re.compile(
    r"(?m)^(?:From obsnote: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}.*|`[^`\n]+`\s+_[^_\n]+_.*)$"
)


@dataclass(frozen=True)
class Settings:
    vault: Path | None
    note: str
    ollama_url: str
    ollama_model: str
    max_output_chars: int
    redact_patterns: list[str]


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


def load_settings() -> Settings:
    cfg = load_json(config_path())
    project_config = find_project_config(safe_cwd())
    project_cfg = load_json(project_config) if project_config else {}

    vault_raw = os.environ.get("OBSNOTE_VAULT", project_cfg.get("vault", cfg.get("vault")))
    note = os.environ.get("OBSNOTE_NOTE", project_cfg.get("note", cfg.get("note", DEFAULT_NOTE)))
    ollama_url = os.environ.get(
        "OBSNOTE_OLLAMA_URL", project_cfg.get("ollama_url", cfg.get("ollama_url", DEFAULT_OLLAMA_URL))
    )
    ollama_model = os.environ.get(
        "OBSNOTE_OLLAMA_MODEL", project_cfg.get("ollama_model", cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL))
    )
    max_output_raw = os.environ.get(
        "OBSNOTE_MAX_OUTPUT_CHARS",
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

    return Settings(
        vault=Path(vault_raw).expanduser() if vault_raw else None,
        note=str(note),
        ollama_url=str(ollama_url).rstrip("/"),
        ollama_model=str(ollama_model),
        max_output_chars=max_output_chars,
        redact_patterns=redact_patterns,
    )


def resolve_note_path(settings: Settings, note: str) -> Path:
    if settings.vault is None:
        raise SystemExit("No Obsidian vault configured. Run: obsnote config --vault /path/to/vault")
    note_path = Path(note)
    if note_path.is_absolute() or ".." in note_path.parts:
        raise SystemExit("Notebook path must be relative to the vault and may not contain '..'")
    return settings.vault / note_path


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def entry_footer(tags: str = "") -> str:
    footer = f"From obsnote: {now_stamp()}"
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
        + f"\n\n[obsnote clipped {len(output) - limit} characters]\n\n"
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
        data["command"] = command
        if clear_output:
            data.pop("output", None)
            data.pop("return_code", None)
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


def require_last_command() -> dict[str, Any]:
    data = load_last()
    if not data.get("command"):
        raise SystemExit(
            "No last command remembered yet.\n"
            "For interactive shell history, run: eval \"$(obsnote shell-init bash)\"\n"
            "To make it permanent, run: obsnote shell-install bash"
        )
    return data


def require_last_output() -> dict[str, Any]:
    data = load_last()
    if not data.get("command") or data.get("output") is None:
        raise SystemExit(
            "No captured command output found yet.\n"
            "Run a command through obsnote first, for example: obsnote run -- ls -la\n"
            "Then use: obsnote save --output"
        )
    return data


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_json(config_path())
    changed = False
    for attr, key in [
        ("vault", "vault"),
        ("note", "note"),
        ("ollama_url", "ollama_url"),
        ("ollama_model", "ollama_model"),
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
    if changed:
        save_json(config_path(), cfg)
    settings = load_settings()
    project_config = find_project_config(safe_cwd())
    print(f"config: {config_path()}")
    if project_config:
        print(f"project config: {project_config}")
    print(f"vault: {settings.vault or '(unset)'}")
    print(f"note: {settings.note}")
    print(f"ollama_url: {settings.ollama_url}")
    print(f"ollama_model: {settings.ollama_model}")
    print(f"max_output_chars: {settings.max_output_chars}")
    print(f"redact_patterns: {', '.join(settings.redact_patterns)}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    settings = load_settings()
    text_argv = strip_separator(args.text)
    if any(tok in ("--page", "-p", "--tag", "-t") for tok in text_argv):
        print(
            "obsnote note: warning: --page/--tag found inside the note text -- flags are only "
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
        return 0
    append_command_history(command, status=args.status, cwd=args.cwd)
    return 0


def cmd_pause(_: argparse.Namespace) -> int:
    confirmed = set_capture_paused(True)
    print("Pausing passive shell-history capture...")
    print(f"Confirmed: capture_paused = {confirmed}")
    if confirmed:
        print("obsnote will not record anything typed at the shell until you run: obsnote resume")
        print("(or `obsnote mark`, which turns capture back on too). Explicit commands")
        print("(note/run/save/annotate) still work normally -- only the passive shell hook is paused.")
    else:
        print("WARNING: could not confirm the paused state was saved. Check permissions on:")
        print(f"  {state_path()}")
    return 0 if confirmed else 1


def cmd_forget(args: argparse.Namespace) -> int:
    data = load_last()
    history = command_history(data)
    if args.last is not None:
        if args.last <= 0:
            raise SystemExit("--last must be a positive number of commands.")
        forgotten = min(args.last, len(history))
        history = history[: len(history) - forgotten]
        data["command_history"] = history
        for marker in markers(data).values():
            if isinstance(marker, dict) and isinstance(marker.get("index"), int):
                marker["index"] = min(marker["index"], len(history))
        for key in ("command", "output", "return_code"):
            data.pop(key, None)
        print(f"Forgot the last {forgotten} captured command(s) and the last remembered command/output.")
    else:
        forgotten = len(history)
        marker_count = len(markers(data))
        for key in ("command_history", "command", "output", "return_code", "markers"):
            data.pop(key, None)
        print(
            f"Forgot {forgotten} captured command(s), the last remembered command/output, "
            f"and {marker_count} marker(s)."
        )
    data["updated_at"] = now_stamp()
    save_json(state_path(), data)
    print("Nothing already written to the vault was touched -- `obsnote undo` removes vault entries.")
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
        raise SystemExit(f"No obsnote entries found in {page}.")
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


def append_last_command(page: str, tags: str) -> Path:
    settings = load_settings()
    data = require_last_command()
    command = data.get("command")
    return append_markdown(settings, page, f"{entry_footer(tags)}\n\n{fence(command, 'bash')}")


def append_last_output(page: str, tags: str) -> Path:
    settings = load_settings()
    data = require_last_output()
    command = data.get("command")
    output = data.get("output")
    output, clipped = clip_output(str(output), settings.max_output_chars)
    clipped_note = "\n\n_Output clipped by obsnote._" if clipped else ""
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
    if args.synth and args.no_append:
        raise SystemExit("`obsnote run` cannot combine --synth and --no-append.")
    command_argv = strip_separator(args.command)
    if not command_argv:
        raise SystemExit("Usage: obsnote run [--synth|--no-append] -- <command> [args...]")
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
        raise SystemExit(f"obsnote run: {exc}") from exc
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
    if not args.no_append:
        settings = load_settings()
        page = resolve_target_page(args.page, settings)
        tags = format_tags(args.tag)
        if args.synth:
            print(append_last_summary(page, tags))
        else:
            print(append_last_output(page, tags))
    return return_code


def synthesize(settings: Settings, command: str, output: str) -> str:
    clipped_output, _ = clip_output(output, settings.max_output_chars)
    prompt = (
        "You are writing a concise engineering note for an Obsidian notebook.\n"
        "Summarize what the command did, the important result, errors or risks, "
        "and any sensible next step. Keep it factual and compact.\n\n"
        f"Command:\n{command}\n\nOutput:\n{clipped_output}\n"
    )
    payload = json.dumps(
        {
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"Ollama request failed: {exc}") from exc
    except TimeoutError as exc:
        raise SystemExit("Ollama request timed out.") from exc
    text = clean_llm_response(body.get("response"))
    if not text:
        raise SystemExit(f"Ollama returned no response: {body}")
    return text


def clean_llm_response(text: Any) -> str:
    if text is None:
        return ""
    cleaned = str(text).strip()
    cleaned = re.sub(r"(?is)<think>.*?</think>\s*", "", cleaned).strip()
    return cleaned


def synthesize_history(settings: Settings, name: str, entries: list[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        if entry.get("type") == "note":
            lines.append(f"- {entry['at']}: [note] {entry.get('text', '')}")
        elif entry.get("type") == "summary":
            lines.append(f"- {entry['at']}: [summary] {entry.get('text', '')}")
        else:
            lines.append(f"- {entry['at']}: {entry.get('command', '')}")
    command_list = "\n".join(lines)
    clipped_commands, _ = clip_output(command_list, settings.max_output_chars)
    prompt = (
        "You are writing a concise engineering note for an Obsidian notebook.\n"
        "Summarize the terminal work since the named marker. Identify the likely goal, "
        "important commands, results that can be inferred from the command sequence, "
        "and sensible next steps. Do not invent command output that is not present.\n\n"
        f"Marker: {name}\n\nCommands:\n{clipped_commands}\n"
    )
    payload = json.dumps(
        {
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"Ollama request failed: {exc}") from exc
    except TimeoutError as exc:
        raise SystemExit("Ollama request timed out.") from exc
    text = clean_llm_response(body.get("response"))
    if not text:
        raise SystemExit(f"Ollama returned no response: {body}")
    return text


def append_last_summary(page: str, tags: str) -> Path:
    settings = load_settings()
    data = require_last_output()
    command = data.get("command")
    output = data.get("output")
    summary = synthesize(settings, str(command), str(output))
    markdown = (
        f"{entry_footer(tags)}\n\n"
        f"{fence(str(command), 'bash')}\n\n"
        f"{summary}"
    )
    return append_markdown(settings, page, markdown)


def cmd_save(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = resolve_target_page(args.page, settings)
    tags = format_tags(args.tag)
    if args.output:
        path = append_last_output(page, tags)
    elif args.synth:
        path = append_last_summary(page, tags)
    else:
        path = append_last_command(page, tags)
    print(path)
    return 0


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
        print("capture: OFF -> ON (recording until `since`/`unmark`)", file=sys.stderr)
    set_marker(name, page)
    return 0


def cmd_mark_list(args: argparse.Namespace) -> int:
    name = getattr(args, "name", None)
    if name:
        return cmd_mark_show(name)
    data = load_last()
    history = command_history(data)
    all_markers = markers(data)
    if not all_markers:
        print("No markers set.")
        return 0
    for marker_key in sorted(all_markers):
        marker = all_markers[marker_key]
        if not isinstance(marker, dict):
            continue
        index = marker.get("index")
        at = marker.get("at", "unknown")
        page = marker.get("page") or "(default)"
        remaining = len(history) - index if isinstance(index, int) else "unknown"
        print(f"{marker_key}\tset={at}\tpage={page}\tcommands_since={remaining}")
    return 0


def cmd_mark_show(raw_name: str) -> int:
    name = marker_name(raw_name)
    marker, entries = history_since_marker(name)
    page = marker.get("page") or "(default)"
    if not entries:
        print(f"No commands recorded since marker `{name}` (set {marker.get('at', 'unknown')}, page: {page}).")
        return 0
    print(f"--- commands since `{name}` (page: {page}, set {marker.get('at', 'unknown')}) ---\n")
    print(format_session_markdown(entries))
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
        if entry.get("type") in ("note", "summary") or entry.get("status") in (None, 0)
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
    name = resolve_marker_arg(args.name)
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


def cmd_synth_since(args: argparse.Namespace) -> int:
    settings = load_settings()
    name = resolve_marker_arg(args.name)
    marker, entries = since_entries(args, name)
    summary = synthesize_history(settings, name, entries)
    page = resolve_since_page(args, settings, marker)
    tags = format_tags(args.tag)
    markdown = (
        f"{entry_footer(tags)}\n\n"
        f"{summary}\n\n"
        f"{format_session_markdown(entries)}"
    )
    path = append_markdown(settings, page, markdown)
    print(path)
    return 0


def maybe_auto_pause_after_since() -> None:
    if is_capture_paused():
        return
    if len(markers(load_last())) > 1:
        print(
            "capture: staying ON -- other markers are still pending (run `obsnote pause` if you're done)",
            file=sys.stderr,
        )
        return
    set_capture_paused(True)
    print("capture: ON -> OFF (auto-paused; run `obsnote resume` to keep recording)", file=sys.stderr)


def cmd_since(args: argparse.Namespace) -> int:
    result = cmd_synth_since(args) if args.synth else cmd_history_since(args)
    maybe_auto_pause_after_since()
    return result


def cmd_page_new(args: argparse.Namespace) -> int:
    settings = load_settings()
    page = normalize_page_name(args.name)
    path = resolve_note_path(settings, page)
    if path.exists():
        raise SystemExit(
            f"Page already exists: {page}\nUse `obsnote page use {page}` to switch to it."
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
        raise SystemExit(f"No such page: {page}\nUse `obsnote page new {page}` to create it.")
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
        raise SystemExit("No Obsidian vault configured. Run: obsnote config --vault /path/to/vault")
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
    print(f"capture: {'PAUSED (auto-resumes on `obsnote mark`, or run: obsnote resume)' if paused else 'active'}")
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
                print("[preview truncated; obsnote save --output writes the full capture]")
    else:
        print("no command captured yet")
    all_markers = markers(data)
    if all_markers:
        print(f"markers: {', '.join(sorted(all_markers))}")
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
# newlines, so obsnote captures the command the way it was typed.
shopt -s cmdhist lithist

# A leading space on a typed command keeps it out of bash history entirely
# (and therefore out of obsnote's capture too) -- the standard bash convention
# for "don't record this". obsnote turns it on if it isn't already.
case ";$HISTCONTROL;" in
  *";ignorespace;"*|*";ignoreboth;"*) ;;
  *) HISTCONTROL="${HISTCONTROL:+$HISTCONTROL:}ignorespace" ;;
esac
export HISTCONTROL

__obsnote_prompt_command() {
  local last_status=$?
  local cmd
  cmd="$(HISTTIMEFORMAT= fc -ln -1 2>/dev/null | sed '1s/^[[:space:]]*//')"
  case "$cmd" in
    ""|obsnote*) ;;
    *) obsnote remember-cmd --status "$last_status" --cwd "$PWD" -- "$cmd" >/dev/null 2>&1 || true ;;
  esac
  return $last_status
}

case ";$PROMPT_COMMAND;" in
  *";__obsnote_prompt_command;"*) ;;
  *) PROMPT_COMMAND="__obsnote_prompt_command${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
'''.strip()

# `status` is a read-only special parameter in zsh, hence `last_status`.
ZSH_SHELL_INIT = r'''
# A leading space on a typed command keeps it out of zsh history entirely
# (and therefore out of obsnote's capture too).
setopt hist_ignore_space

__obsnote_precmd() {
  local last_status=$?
  local cmd
  cmd="$(fc -ln -1 2>/dev/null | sed '1s/^[[:space:]]*//')"
  case "$cmd" in
    ""|obsnote*) ;;
    *) obsnote remember-cmd --status "$last_status" --cwd "$PWD" -- "$cmd" >/dev/null 2>&1 || true ;;
  esac
  return 0
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd __obsnote_precmd
'''.strip()

SHELL_INIT = {"bash": BASH_SHELL_INIT, "zsh": ZSH_SHELL_INIT}
SHELL_RC = {"bash": ".bashrc", "zsh": ".zshrc"}


def cmd_shell_init(args: argparse.Namespace) -> int:
    print(SHELL_INIT[args.shell])
    return 0


def shell_init_block(shell: str) -> str:
    return (
        "\n# obsnote shell integration\n"
        "if command -v obsnote >/dev/null 2>&1; then\n"
        f'  eval "$(obsnote shell-init {shell})"\n'
        "fi\n"
    )


# Matches whatever shell_init_block() appended, regardless of which shell it
# was installed for -- lets `shell-uninstall` clean it up even if it drifted
# slightly from the exact string shell-install would write today.
SHELL_INTEGRATION_BLOCK_RE = re.compile(
    r"\n?# obsnote shell integration\n"
    r"if command -v obsnote >/dev/null 2>&1; then\n"
    r"  eval \"\$\(obsnote shell-init \w+\)\"\n"
    r"fi\n"
)


def cmd_shell_install(args: argparse.Namespace) -> int:
    rc = Path.home() / SHELL_RC[args.shell]
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    marker = "obsnote shell integration"
    if marker in existing:
        print(f"obsnote shell integration is already present in {rc}")
        return 0
    with rc.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(shell_init_block(args.shell))
    set_capture_paused(True)
    print(f"Added obsnote shell integration to {rc}")
    print(f"Open a new shell, or run: source ~/{SHELL_RC[args.shell]}")
    print("Passive capture is OFF by default. It turns on automatically when you run")
    print("`obsnote mark`, and back off after `obsnote since`/`unmark` -- or run")
    print("`obsnote resume` yourself to leave it recording continuously.")
    return 0


def cmd_shell_uninstall(args: argparse.Namespace) -> int:
    rc = Path.home() / SHELL_RC[args.shell]
    if not rc.exists():
        print(f"No {rc} found -- nothing to remove.")
        return 0
    text = rc.read_text(encoding="utf-8")
    new_text, count = SHELL_INTEGRATION_BLOCK_RE.subn("", text)
    if count == 0:
        print(f"obsnote shell integration not found in {rc}")
        return 0
    rc.write_text(new_text, encoding="utf-8")
    print(f"Removed obsnote shell integration from {rc}")
    print(f"Open a new shell, or run: source ~/{SHELL_RC[args.shell]}")
    print("Passive capture won't run anymore -- explicit commands (note/run/save/mark/...)")
    print("still work normally.")
    return 0


def check_ollama(settings: Settings) -> bool:
    try:
        with urllib.request.urlopen(f"{settings.ollama_url}/api/tags", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def cmd_doctor(_: argparse.Namespace) -> int:
    def mark(ok: bool) -> str:
        return "[ok]" if ok else "[!!]"

    problems: list[str] = []
    settings = load_settings()
    print("obsnote preflight check\n")

    on_path = shutil.which("obsnote") is not None
    print(f"{mark(on_path)} obsnote on PATH")
    if not on_path:
        problems.append(
            "`obsnote` isn't on PATH. The shell hook checks `command -v obsnote` at shell "
            "startup and silently skips installing itself if it's missing."
        )

    if settings.vault is None:
        print("[!!] vault configured")
        problems.append("No vault configured. Run: obsnote config --vault /path/to/vault")
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

    installed_rcs = []
    for rc_name in SHELL_RC.values():
        rc = Path.home() / rc_name
        if rc.exists() and "obsnote shell integration" in rc.read_text(encoding="utf-8"):
            installed_rcs.append(f"~/{rc_name}")
    installed = bool(installed_rcs)
    where = f" ({', '.join(installed_rcs)})" if installed_rcs else ""
    print(f"{mark(installed)} shell hook installed{where}")
    if not installed:
        problems.append("Shell hook not installed. Run: obsnote shell-install bash (or zsh)")

    print("[?]  shell hook active in *this* terminal -- can't be checked from a subprocess")
    print('     Run this yourself: echo "$PROMPT_COMMAND"  (bash)  or  echo $precmd_functions  (zsh)')
    print("     It should mention __obsnote. If it doesn't: source your shell rc file,")
    print("     or open a new terminal (the hook only activates when a shell starts).")

    paused = is_capture_paused()
    if paused:
        print("[--] capture is PAUSED (default) -- turns on automatically with `obsnote mark`,")
        print("     or run: obsnote resume")
    else:
        print("[ok] capture is active (not paused)")

    ollama_ok = check_ollama(settings)
    print(f"{mark(ollama_ok)} ollama reachable at {settings.ollama_url} (only needed for --synth)")

    print()
    if problems:
        print(f"{len(problems)} issue(s) found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Config, vault, and shell-hook install all look good.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsnote")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command_name", required=True)

    config = sub.add_parser("config", help="show or update configuration")
    config.add_argument("--vault")
    config.add_argument("--note")
    config.add_argument("--ollama-url")
    config.add_argument("--ollama-model")
    config.add_argument("--max-output-chars", type=int)
    config.add_argument(
        "--redact-pattern",
        action="append",
        default=[],
        help="regex; matching typed commands are never captured by the shell hook (repeatable)",
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

    save = sub.add_parser("save", help="append remembered command, output, or summary")
    save_mode = save.add_mutually_exclusive_group()
    save_mode.add_argument("--output", action="store_true", help="append last captured command output")
    save_mode.add_argument("--synth", action="store_true", help="append local-LLM summary of last captured output")
    save.add_argument("--page", "-p", help="target page for this entry only")
    save.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    save.set_defaults(func=cmd_save)

    run = sub.add_parser("run", help="run a command, capture output, and append it")
    run.add_argument("--synth", action="store_true", help="append a local-LLM summary instead of raw output")
    run.add_argument("--no-append", action="store_true", help="capture state without writing to Obsidian")
    run.add_argument("--page", "-p", help="target page for this entry only")
    run.add_argument("--tag", "-t", action="append", default=[], help="inline #tag; repeatable")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    mark = sub.add_parser("mark", help="set a command-history marker")
    mark.add_argument("name", nargs="?", help="marker name (defaults to an auto-numbered '1', '2', ...)")
    mark.add_argument("--page", "-p", help="page this marker belongs to (defaults to the active page)")
    mark.set_defaults(func=cmd_mark)

    marks = sub.add_parser("marks", help="list markers, or preview commands recorded since one")
    marks.add_argument("name", nargs="?", help="if given, show the commands recorded since this marker (read-only)")
    marks.set_defaults(func=cmd_mark_list)

    unmark = sub.add_parser("unmark", help="delete a marker")
    unmark.add_argument(
        "name", nargs="?", help="marker name (defaults to 'default', or the only marker if just one exists)"
    )
    unmark.set_defaults(func=cmd_mark_del)

    since = sub.add_parser("since", help="append commands since a marker")
    since.add_argument(
        "name", nargs="?", help="marker name (defaults to 'default', or the only marker if just one exists)"
    )
    since.add_argument("--synth", action="store_true", help="append a local-LLM summary")
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
        help="insert a note into the command timeline, e.g. between commands in a mark session",
        description="Reads stdin when text is omitted. Shows up inline (as a comment) the next time "
        "`since`/`marks <name>` renders the commands recorded around it.",
    )
    annotate.add_argument("text", nargs=argparse.REMAINDER)
    annotate.set_defaults(func=cmd_annotate)

    summary = sub.add_parser(
        "summary",
        help="add a summary to the current mark session",
        description="Reads stdin when text is omitted. The summary is rendered before the command "
        "timeline the next time `since`/`marks <name>` renders a mark session.",
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

    page.set_defaults(func=cmd_page_show)

    pages = sub.add_parser("pages", help="list pages in the vault")
    pages.set_defaults(func=cmd_pages)

    show = sub.add_parser("show", help="show current capture state without writing to the vault")
    show.set_defaults(func=cmd_show)

    tail = sub.add_parser("tail", help="show the last entries from a page, read-only")
    tail.add_argument("--page", "-p", help="page to read (defaults to the active page)")
    tail.add_argument("-n", "--number", type=int, default=3, help="number of entries to show (default: 3)")
    tail.set_defaults(func=cmd_tail)

    undo = sub.add_parser("undo", help="remove the last obsnote entry from a page")
    undo.add_argument("--page", "-p", help="page to undo on (defaults to the active page)")
    undo.set_defaults(func=cmd_undo)

    forget = sub.add_parser(
        "forget",
        help="clear captured commands/output from obsnote's state file",
        description="Clears what the shell hook has captured into local state (a shadow shell "
        "history). Does not touch anything already written to the vault -- see `obsnote undo`.",
    )
    forget.add_argument("--last", type=int, metavar="N", help="forget only the N most recent captured commands")
    forget.set_defaults(func=cmd_forget)

    shell_init = sub.add_parser("shell-init", help="print shell integration")
    shell_init.add_argument("shell", choices=sorted(SHELL_INIT))
    shell_init.set_defaults(func=cmd_shell_init)

    shell_install = sub.add_parser("shell-install", help="install shell integration")
    shell_install.add_argument("shell", choices=sorted(SHELL_INIT))
    shell_install.set_defaults(func=cmd_shell_install)

    shell_uninstall = sub.add_parser("shell-uninstall", help="remove shell integration installed via shell-install")
    shell_uninstall.add_argument("shell", choices=sorted(SHELL_INIT))
    shell_uninstall.set_defaults(func=cmd_shell_uninstall)

    doctor = sub.add_parser("doctor", help="check that the vault, config, and shell hook are ready to go")
    doctor.set_defaults(func=cmd_doctor)

    pause = sub.add_parser("pause", help="pause passive shell-history capture and confirm it's off")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="resume passive shell-history capture")
    resume.set_defaults(func=cmd_resume)

    return parser


def rewrite_legacy_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    command = argv[0]
    rest = argv[1:]
    if command == "remember-cmd":
        return argv
    if command == "last-cmd":
        return ["save", *rest]
    if command == "last":
        return ["save", "--output", *rest]
    if command == "synth":
        return ["save", "--synth", *rest]
    if command == "mark-list":
        return ["marks", *rest]
    if command == "mark-del":
        return ["unmark", *rest]
    if command == "mark" and rest[:1] == ["list"]:
        return ["marks", *rest[1:]]
    if command == "mark" and rest[:1] == ["del"]:
        return ["unmark", *rest[1:]]
    if command == "history-since":
        return ["since", *rest]
    if command == "synth-since":
        return ["since", *rest, "--synth"]
    if command == "start":
        return ["doctor", *rest]
    if command == "stop":
        return ["pause", *rest]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = rewrite_legacy_args(argv)
    if argv and argv[0] == "remember-cmd":
        remember = argparse.ArgumentParser(prog="obsnote remember-cmd")
        remember.add_argument("--status", type=int, default=None, help="exit status of the command")
        remember.add_argument("--cwd", default=None, help="directory the command ran in")
        remember.add_argument("command", nargs=argparse.REMAINDER)
        args = remember.parse_args(argv[1:])
        return cmd_remember_cmd(args)
    args = parser.parse_args(argv)
    return int(args.func(args))
