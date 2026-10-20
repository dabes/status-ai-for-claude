# Status AI for Claude

> **Unofficial.** Not made by, affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.

A VS Code panel that shows your **plan usage** at the top (current 5-hour session and current week, with the time each resets), and below it, in two columns:

- **Tasks.** What each Claude Code session is running: the tool call it is waiting on right now (a foreground command, a subagent, a file read…), shown as *primeiro plano* with how long it has been running, and the commands it launched in the background, including subagents. Each background task shows:
  - its status: running, completed, failed, killed or stopped;
  - the session that launched it;
  - a progress bar;
  - the last lines of its log.
- **Agents.** Every Claude Code session active in the last 24 hours. Each one shows:
  - its model;
  - what it is doing right now (the tool it is running, or the start of its last reply);
  - the last request you made;
  - how many tasks it has running.

## AI Usage

A second tab, **AI Usage**, sums the tokens of every model response in your Claude Code histories (sessions and subagents) over the last 30 days (`aiStatus.usageDays`) and shows them per day, project, model, token type (input, output, cache write, cache read) and tool (number of calls). **AI Usage: Open Charts** opens the same data as charts inside VS Code.

The cost is the tokens times the API prices in [LiteLLM's price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), the same source ccusage uses, including 1-hour cache writes and fast mode. It is what the usage would cost on the API, not what a Pro/Max plan charges. Offline, the tab shows tokens only. A response the history repeats (one line per content block, or a resumed session) counts once.

## Where the progress comes from

- **Logs.** When a command redirects its output (`> file`, `>> file`, `tee file`), the panel reads that file. The progress is the last `N/M` or `NN%` written there, for example `tile 18/93`.
- **Downloads.** When a command downloads with `curl -o` or `wget -O`, the panel compares the size of the file on disk with the size the server reports.

## Plan usage (5 h / 7 d)

Every 5 minutes the panel asks Claude Code for your plan's limits, using the same `get_usage` request that feeds Claude Code's own **Usage** view: it starts `claude` with no prompt, sends that one request and closes it. Nothing goes to the model (cost 0, no tokens), and Claude Code handles the login itself; the panel never reads your credentials. It needs the `claude` command installed and logged in.

As a fallback (or for updates after every response in the terminal), `media/statusline.py` can be Claude Code's status line. It saves the numbers it receives and prints `5h 6% · 7d 27%`:

```json
"statusLine": { "type": "command", "command": "python \"<extension folder>/media/statusline.py\"" }
```

The newest of the two wins. A window that has already reset shows no number until fresh data arrives.

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

It serves the page on `127.0.0.1` only. It makes two kinds of network request: a `HEAD` request to the URL of a download that is in progress, to learn the file's total size, and one download of LiteLLM's public price table (from GitHub) for the AI Usage cost. Nothing about your usage is sent.

## Settings

| Setting | Default | |
|---|---|---|
| `aiStatus.python` | `python` (Windows) / `python3` | Python 3 executable |
| `aiStatus.port` | `8765` | Local port of the panel server |
| `aiStatus.days` | `3` | Days of sessions to scan for tasks |
| `aiStatus.usageDays` | `30` | Days of history the AI Usage tab sums |

The interface is currently in Portuguese (pt-BR).
