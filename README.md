# obsnote

A little CLI that turns your terminal into a notebook. Instead of alt-tabbing
into Obsidian every five minutes to write down what you just did, you tell
`obsnote` and it writes clean markdown straight into your vault — freeform
notes, captured commands and their output, an optional local-LLM summary, and
whole marked stretches of shell history bundled into one tidy block.

It started as "save my shell history to Obsidian" and grew into something
built for taking notes on a technical course: pages per topic, tags, and a
`mark` → do stuff → `since` workflow for turning a lab exercise into a
readable note without manually copy-pasting a terminal.

## ⚠️ Read this before you use it

**This is a personal project, not a security product.** Treat it the way you
treat your shell history — useful, convenient, and not something you should
assume is private.

- The redaction feature only filters commands *passively* captured by the
  shell hook. Anything you run through `obsnote run`, or any command's
  *output* (an env dump, `cat`-ing a config file, a curl response with a
  token in it), goes into your vault **unfiltered**.
- Notes are plain markdown files on disk. If your vault syncs anywhere
  (Obsidian Sync, iCloud, Dropbox, a git repo), whatever obsnote writes goes
  wherever that syncs.
- The passive capture keeps a rolling shadow of your shell history (plus the
  last captured command's full output) in plaintext under
  `~/.local/state/obsnote/`. `obsnote forget` clears it.
- Passive capture is off by default and only runs while you're inside a
  `mark` → `since` bracket (or after you explicitly `obsnote resume`) — see
  [Shell integration](#shell-integration).
- Most behavior still lives in one small CLI module (`obsnote/cli.py`) — if
  you're not sure what it's doing with your data, it's a quick read.

Don't paste real credentials into a terminal that's feeding this tool.

## Install

```bash
git clone <this repo>
cd obsnote
pip install -e .
```

## Quick start

```bash
obsnote config --vault ~/path/to/your/ObsidianVault
obsnote shell-install bash   # wires up the hook, capture starts OFF (or: zsh)
source ~/.bashrc             # or just open a new terminal
obsnote doctor               # sanity-check that all of the above actually worked
obsnote mark                 # turns capture on and starts a session
```

`obsnote doctor` is worth running any time something seems off — it checks
your vault, config, and shell hook, and tells you exactly what's missing.

## The mental model

- **Pages** are markdown files in your vault. `obsnote page new "Week3/Networking"`
  creates one and makes it *active* — everything goes there until you switch
  (`obsnote page use ...`) or override it for a single call with `--page`.
- **Notes** (`obsnote note "..."`) are timestamped freeform text, optionally
  tagged (`--tag regex --tag week3`).
- **Marks** are checkpoints in your shell history. `obsnote mark lab1`, do
  some work, `obsnote since lab1` writes everything you typed in between as
  one clean bash block. `obsnote marks lab1` previews it first without
  writing anything. The hook records each command's exit status and working
  directory, so the rendered block flags failures (`# exited 1`), notes
  directory changes (`# in ~/proj`) when the stretch spans more than one, and
  `obsnote since lab1 --ok-only` drops the failed attempts and keeps the path
  that worked. Passive capture is off until you `mark` — see
  [Shell integration](#shell-integration) for the on/off details. `obsnote mark`
  with no name auto-numbers it (`1`, `2`, ...) instead of colliding on a
  shared `default` marker; `obsnote since`/`obsnote unmark` with no name
  resolve to that marker automatically as long as it's the only one around.
- **Annotate** drops a note *into* that command timeline mid-session —
  `obsnote annotate "switching to venv setup here"` shows up as an Obsidian
  callout between command blocks the next time you `since` it.
- **Summary** adds a top-of-session summary while you are still working —
  `obsnote summary "tests fail on auth setup"` is rendered before the command
  timeline when you later post the mark with `obsnote since ...`.
- **Save / run** capture a single command and its output (or an LLM summary
  of it, if you've got Ollama running locally) instead of a whole marked
  stretch: `obsnote run -- pytest -k foo`.

Every entry keeps the Markdown quiet: first `From obsnote: <timestamp>` plus
any tags you explicitly passed, then the captured note, command, output, or
history.

## Commands

| Command | What it does |
|---|---|
| `obsnote config` | show/set vault, default page, Ollama settings, redact patterns |
| `obsnote note [text]` | append a freeform note (reads stdin if omitted) |
| `obsnote run -- <cmd>` | run a command, capture output, append it |
| `obsnote save [--output\|--synth]` | append the last captured command / its output / an LLM summary |
| `obsnote mark [name]` | set a checkpoint in your shell history (name defaults to auto-numbered `1`, `2`, ...); turns passive capture on if it was off |
| `obsnote marks [name]` | list markers, or preview commands since one |
| `obsnote unmark [name]` | delete a marker (name defaults to `default`, or the only marker if just one exists); turns passive capture back off if no markers remain |
| `obsnote since [name] [--synth] [--ok-only]` | write everything since a marker (name defaults to `default`, or the only marker if just one exists); turns passive capture back off unless another marker is still pending |
| `obsnote annotate [text]` | insert a note into the pending command timeline |
| `obsnote summary [text]` | add a summary that posts before the pending command timeline |
| `obsnote undo [--page]` | remove the last obsnote entry from a page |
| `obsnote page new/use <name>` | create or switch the active page |
| `obsnote pages` | list pages in the vault |
| `obsnote tail [--page] [-n]` | read-only peek at the last entries on a page |
| `obsnote show` | read-only status: capture state, active page, last command, markers |
| `obsnote doctor` | preflight check: vault, config, shell hook |
| `obsnote pause` / `obsnote resume` | pause/resume passive shell-history capture, with a confirmed status readout |
| `obsnote forget [--last N]` | clear captured commands/output from obsnote's local state (the vault is untouched) |
| `obsnote shell-install <bash\|zsh>` | wire up the passive capture hook in your shell rc file (capture starts off) |
| `obsnote shell-uninstall <bash\|zsh>` | remove the hook from your shell rc file; passive capture stops entirely |

Run `obsnote <command> --help` for the full flag list on any of these.

## Shell integration

`obsnote shell-install bash` adds a `PROMPT_COMMAND` hook to `~/.bashrc`
(`obsnote shell-install zsh` adds a `precmd` hook to `~/.zshrc`) that, once
active, records every command you type — along with its exit status and
working directory — while skipping obsnote's own commands.

**Capture is opt-in, bracketed by mark/since.** A fresh `shell-install`
leaves passive capture *off*. It turns itself on when you `obsnote mark`, and
back off automatically once you `obsnote since` (or `obsnote unmark`) that
marker — so a `mark` → do stuff → `since` session is fully recorded without
you touching pause/resume, and nothing gets captured in between sessions or
before you've ever run `mark`. If you `mark` more than one thing at once,
`since` on one leaves capture on until the others are also closed out. Want
it recording continuously instead, the old always-on way? `obsnote resume`
does that — it stays on until you `obsnote pause` yourself.

Two guardrails on top of that:

- A **leading space** before a command keeps it out of both the shell's
  history and obsnote's capture — the standard `ignorespace` convention,
  which the hook turns on automatically if it isn't already.
- A **redact pattern list** silently drops commands that look like they
  contain a live credential (`TOKEN=...`, `mysql -pSECRET`, `Authorization:
  Bearer ...`, etc.) before they're ever written to disk. Extend it with
  `obsnote config --redact-pattern '<regex>'`.

`obsnote run` bypasses redaction on purpose — if you explicitly ran a command
through obsnote, that's an intentional capture, not passive history.

Doing something sensitive mid-mark and don't trust the pattern list to catch
it? Run `obsnote pause` to pause passive capture entirely (it reads the state
back and confirms it actually took), and `obsnote resume` or `obsnote mark`
when you're done. If something already slipped into the capture buffer,
`obsnote forget --last 5` (or plain `obsnote forget` for everything) scrubs
it from local state.

Want obsnote out of your shell startup entirely? `obsnote shell-uninstall
bash` (or `zsh`) removes the block `shell-install` added — no more passive
capture, no more `obsnote remember-cmd` calls on every prompt. Explicit
commands (`note`, `run`, `save`, ...) keep working; only the automatic hook
goes away.

One more guardrail: the *active page* is remembered per vault. If a
project-local `.obsnote.json` points a directory at a different vault, a page
you activated elsewhere is ignored there instead of silently creating a
same-named file in the wrong vault.

## Config

Settings resolve in this order: environment variables (`OBSNOTE_VAULT`,
`OBSNOTE_NOTE`, ...) → a project-local `.obsnote.json` (found by walking up
from your current directory, like `.git`) → the global config at
`~/.config/obsnote/config.json` → built-in defaults. The project-local file
is handy for pointing a specific course repo at a specific default page
without touching your global setup.

## Status

Early, actively-changing personal tool. There is now a small standard-library
test suite for the core CLI/state behavior, but no broad compatibility matrix
or security guarantees. If something breaks, `obsnote doctor` and `obsnote show`
are your first stop for diagnosing it.

Older command names (`start`, `stop`, `last`, `synth`, `history-since`, ...)
keep working as aliases for their renamed equivalents.

## License

MIT — see [LICENSE](LICENSE).
