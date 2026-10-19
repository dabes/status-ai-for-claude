# AI Status for Claude Code

> **Unofficial.** Not made by, affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.

A VS Code panel that shows, in two columns:

- **Tasks.** The commands Claude Code launched in the background, including subagents. Each one shows:
  - its status: running, completed, failed, killed or stopped;
  - the session that launched it;
  - a progress bar;
  - the last lines of its log.
- **Agents.** Every Claude Code session active in the last 24 hours. Each one shows:
  - its model;
  - what it is doing right now (the tool it is running, or the start of its last reply);
  - the last request you made;
  - how many tasks it has running.

## Where the progress comes from

- **Logs.** When a command redirects its output (`> file`, `>> file`, `tee file`), the panel reads that file. The progress is the last `N/M` or `NN%` written there, for example `tile 18/93`.
- **Downloads.** When a command downloads with `curl -o` or `wget -O`, the panel compares the size of the file on disk with the size the server reports.

## Requirements

- Python 3 on `PATH`. If it isn't there, set **AI Status: Python** to the executable. The panel server uses only the standard library.
- Claude Code, which writes the session histories the panel reads from `~/.claude/projects`, or from `CLAUDE_CONFIG_DIR` when that is set.

## Usage

Open the **AI Status** tab in the bottom panel, next to Terminal and Output. The panel refreshes every 3 seconds. If it gets stuck, run **AI Status: Reload Panel**.

## Privacy

Everything stays on your machine. The panel only **reads**:

- the Claude Code session histories;
- the output files of background tasks;
- the log files those commands write.

It serves the page on `127.0.0.1` only. The single network request it makes is a `HEAD` request to the URL of a download that is in progress, to learn the file's total size.

## Settings

| Setting | Default | |
|---|---|---|
| `aiStatus.python` | `python` (Windows) / `python3` | Python 3 executable |
| `aiStatus.port` | `8765` | Local port of the panel server |
| `aiStatus.days` | `3` | Days of sessions to scan for tasks |

The interface is currently in Portuguese (pt-BR).
