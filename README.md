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
obsnote shell-install bash   # wires up automatic command capture
source ~/.bashrc             # or just open a new terminal
obsnote start                # sanity-check that all of the above actually worked
```

`obsnote start` is worth running any time something seems off — it checks
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
  writing anything.
- **Annotate** drops a note *into* that command timeline mid-session —
  `obsnote annotate "switching to venv setup here"` shows up as a `# comment`
  at the right spot the next time you `since` it.
- **Save / run** capture a single command and its output (or an LLM summary
  of it, if you've got Ollama running locally) instead of a whole marked
  stretch: `obsnote run -- pytest -k foo`.

Every entry gets a timestamp and, best-effort, the directory and git branch
it was written from — handy context once you're scrolling back through
weeks of notes.

## Commands

| Command | What it does |
|---|---|
| `obsnote config` | show/set vault, default page, Ollama settings, redact patterns |
| `obsnote note [text]` | append a freeform note (reads stdin if omitted) |
| `obsnote run -- <cmd>` | run a command, capture output, append it |
| `obsnote save [--output\|--synth]` | append the last captured command / its output / an LLM summary |
| `obsnote mark [name]` | set a checkpoint in your shell history |
| `obsnote marks [name]` | list markers, or preview commands since one |
| `obsnote unmark [name]` | delete a marker |
| `obsnote since [name] [--synth]` | write everything since a marker (optionally summarized) |
| `obsnote annotate [text]` | insert a note into the pending command timeline |
| `obsnote page new/use <name>` | create or switch the active page |
| `obsnote pages` | list pages in the vault |
| `obsnote tail [--page] [-n]` | read-only peek at the last entries on a page |
| `obsnote show` | read-only status: capture state, active page, last command, markers |
| `obsnote start` | preflight check: vault, config, shell hook |
| `obsnote stop` / `obsnote resume` | pause/resume passive shell-history capture, with a confirmed status readout |

Run `obsnote <command> --help` for the full flag list on any of these.

## Shell integration

`obsnote shell-install bash` adds a `PROMPT_COMMAND` hook to `~/.bashrc` that
passively records every command you type (skipping obsnote's own commands).
Two guardrails on top of that:

- A **leading space** before a command keeps it out of both bash's history
  and obsnote's capture — the standard `HISTCONTROL=ignorespace` convention,
  which the hook turns on automatically if it isn't already.
- A **redact pattern list** silently drops commands that look like they
  contain a live credential (`TOKEN=...`, `mysql -pSECRET`, `Authorization:
  Bearer ...`, etc.) before they're ever written to disk. Extend it with
  `obsnote config --redact-pattern '<regex>'`.

`obsnote run` bypasses redaction on purpose — if you explicitly ran a command
through obsnote, that's an intentional capture, not passive history.

Doing something sensitive and don't trust the pattern list to catch it? Run
`obsnote stop` to pause passive capture entirely (it reads the state back and
confirms it actually took), and `obsnote resume` when you're done.

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
or security guarantees. If something breaks, `obsnote start` and `obsnote show`
are your first stop for diagnosing it.

## License

MIT — see [LICENSE](LICENSE).
