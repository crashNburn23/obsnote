# noteshell

A little CLI that turns your terminal into a notebook. `noteshell shell` drops
you into a subshell that's quietly recording; work as normal, then `since`
writes the whole stretch into your Obsidian vault as one clean markdown
block — commands, exit codes, notes, whatever you annotated along the way.

It started as "save my shell history to Obsidian" and grew into something
built for taking notes on a technical course: pages per topic, tags, and a
`shell` → do stuff → `since` workflow for turning a lab exercise into a
readable note without manually copy-pasting a terminal.

## ⚠️ Read this before you use it

**Personal project, not a security product.** Treat it like your shell
history: useful, convenient, not private.

- Recording only ever happens inside a subshell you explicitly started with
  `noteshell shell` — noteshell never touches your real `~/.bashrc`/`~/.zshrc`,
  and there's no lingering hook to forget about. Commands sit as a rolling
  plaintext shadow (plus the last command's full output) under
  `~/.local/state/noteshell/` until `since` writes them out; exiting the
  subshell clears whatever's left by default, including if it was killed
  or crashed (`noteshell config --no-forget-on-exit` to keep it around, and
  `noteshell forget` to clear it by hand anytime).
- Redaction only filters commands captured inside that subshell. `noteshell
  run`, and any command's *output* (an env dump, a curl response with a
  token in it), goes into your vault **unfiltered**.
- Notes are plain markdown on disk. If your vault syncs anywhere (Obsidian
  Sync, iCloud, Dropbox, a git repo), whatever noteshell writes goes with it.
- It's one small module (`noteshell/cli.py`). If you're unsure what it's doing
  with your data, it's a quick read.

Don't paste real credentials into a terminal that's feeding this tool.

## Install

```bash
git clone <this repo>
cd noteshell
pip install -e .
```

## Quick start

```bash
noteshell config --vault ~/path/to/your/ObsidianVault
noteshell shell                 # opens a recording subshell, marks a session
# ...do your work...
since                          # inside the subshell, writes it to your vault
exit                           # back to your normal shell
```

Run `noteshell doctor` any time something seems off — it checks your vault,
config, and whether you're currently inside a `noteshell shell`.

## Mental model

Everything lands on the **active page**, a markdown file in your vault
(`noteshell page new/use/list`, or `--page` for a one-off).

- `noteshell note "..."` — a timestamped freeform note (`--tag` to tag it).
- `noteshell shell` → do stuff → `since` — starts a recording subshell and a
  marker together; `since` writes everything typed as one bash block,
  flagging failures (`# exited 1`) and directory changes, then closes the
  marker. `--preview` shows it first without writing; `--ok-only` drops
  failed attempts. Exit the subshell without running `since` and you're
  asked whether to save first; decline and the marker is discarded. Use
  `noteshell shell --no-mark` for a plain recording subshell with no marker,
  or `--mark NAME`/`--page NAME` to name it.
- `noteshell annotate "..."` / `noteshell summary "..."` — drop a note into the
  pending timeline, or a summary that renders above it, before you `since`.
- `noteshell run -- <cmd>` — capture one command and its output instead of a
  whole shell stretch, without needing a subshell at all.

Inside `noteshell shell`, the common commands above work without the
`noteshell` prefix (`since`, `note`, `annotate`, `summary`, `run`, `page`,
`tail`, `show`, `pause`, `resume`, `undo`, `forget`, `doctor`, `config`).

Every entry starts with `From noteshell: <timestamp>` plus any tags, then the
note, command, output, or history.

## Commands

| Command | What it does |
|---|---|
| `noteshell config` | show/set vault, default page, redaction, output limits, and forget-on-exit |
| `noteshell note [text]` | append a freeform note (reads stdin if omitted) |
| `noteshell run -- <cmd>` | run a command, capture output, append it |
| `noteshell shell [bash\|zsh] [--mark NAME] [--page NAME] [--no-mark]` | open a recording subshell, marking a session by default |
| `noteshell since [--preview\|--ok-only]` | write (or preview) the current shell session |
| `noteshell annotate [text]` / `summary [text]` | insert a note / summary into the pending timeline |
| `noteshell undo [--page]` | remove the last noteshell entry from a page |
| `noteshell page new/use/list <name>` | create, switch, or list pages |
| `noteshell tail [--page] [-n]` | read-only peek at the last entries on a page |
| `noteshell show` | read-only status: capture state, active page, last command, markers |
| `noteshell doctor` | preflight check: vault, config, current shell state |
| `noteshell pause` / `resume` | pause/resume recording, with a confirmed status readout |
| `noteshell forget [--last N]` | clear captured commands/output from local state (vault untouched) |

Run `noteshell <command> --help` for the full flag list on any of these.

## Shell integration

`noteshell shell` starts a temporary bash or zsh session (`$SHELL` by default):
it sources your real rc file first, then layers on the recording hook and
the unprefixed command shortcuts. Nothing is written to your actual
`.bashrc`/`.zshrc` — exit the subshell and every trace of the integration
goes with it, aside from whatever you already saved via `since`.

Unless you pass `--no-mark`, `noteshell shell` opens a marker on entry, the
same one `since` writes and closes. Exit normally without saving and you're
asked to confirm; decline and the marker's deleted, capture pauses if
nothing else is pending.

On exit, `noteshell shell` also runs the equivalent of `noteshell forget` --
clearing any captured commands/output/markers left in local state, saved or
not -- so nothing lingers between sessions. Turn that off with `noteshell
config --no-forget-on-exit` (back on with `--forget-on-exit`), e.g. if you
want `noteshell show`/`noteshell since` to see across separate `shell`
invocations.

That cleanup also covers a subshell that's killed or crashes instead of
exiting normally: `noteshell shell` records its own pid before handing off
to the subshell, and the next noteshell command you run notices that pid is
gone and applies the same exit cleanup then (or just warns, if
`forget_on_exit` is off).

Guardrails on top of that:

- A **leading space** keeps a command out of both shell history and
  noteshell's capture (`ignorespace`, enabled automatically).
- A **redact pattern list** silently drops commands that look like a live
  credential (`TOKEN=...`, `mysql -pSECRET`, `Authorization: Bearer ...`)
  before they touch disk — extend it with `noteshell config --redact-pattern
  '<regex>'`. A redacted command leaves a placeholder in the timeline
  (`# noteshell skipped a redacted command`) with no text stored.

`noteshell run` bypasses redaction on purpose — if you explicitly ran it
through noteshell, that's an intentional capture, not passive history.

Doing something sensitive mid-session and don't trust the pattern list? Run
`noteshell pause`. If something already slipped in, `noteshell forget --last 5`
(or plain `forget`) scrubs it from local state.

While capture is active, the subshell prefixes your prompt with `[● rec]`
in red. If the hook fails to write state, it leaves a warning file that
`noteshell show` will surface.

One more guardrail: the active page is remembered per vault. If a
project-local `.noteshell.json` points a directory at a different vault, a
page activated elsewhere is ignored there instead of writing to the wrong
vault.

## Config

Settings resolve in order: environment variables (`NOTESHELL_VAULT`,
`NOTESHELL_NOTE`, ...) → a project-local `.noteshell.json` (found by walking up
from cwd, like `.git`) → the global config at `~/.config/noteshell/config.json`
→ built-in defaults. The project-local file is handy for pointing a course
repo at a specific default page without touching your global setup.

## Status

Early, actively-changing personal tool. There's a small standard-library
test suite for the core CLI/state behavior, but no compatibility matrix or
security guarantees. If something breaks, `noteshell doctor` and `noteshell show`
are the first stop.

## License

MIT — see [LICENSE](LICENSE).
