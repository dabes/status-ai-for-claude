# Changelog

## 0.8.0

- **Plan usage forecast.** At the pace of the last hour (6 hours for the week), the panel and the page show when you will reach 100% if that happens before the reset: *100% at 15:40 at this pace*. The panel asks Claude Code for the limits every 2 minutes instead of 5 (no cost) to have the readings for it.
- **Notifications.** VS Code notifies when a session stops and waits for you, when a background command or subagent ends, and when plan usage passes 80% / 95% or is on pace to reach 100% within the hour. With several windows open, each notification shows once. Settings `aiStatus.notify.waiting`, `aiStatus.notify.tasks` and `aiStatus.notify.limit` turn them off.

## 0.7.0

- **No Python needed.** The panel server is now written in JavaScript and runs on the Node.js that comes with VS Code, so there is nothing to install. The **AI Status: Python** setting is gone. The optional status line script is now `media/statusline.js`, run with `node`.
- The panel no longer paints text over text (for example a stale *no data* under *Session (5 h) 43%*). Every row now has a stable id, so VS Code updates rows in place instead of redrawing them.
- Dates and times on the browser page follow the page's language instead of the browser's.
- A command that ended with `[exited …]` or `[killed]` now gets its end time, so it leaves the list 5 minutes later like the others.
- Marketplace page: animated demo, clearer description and more search keywords.

## 0.6.3

- The browser page opens in VS Code's display language and has a language selector (Auto, English, Português) next to the tabs. The choice is remembered for both tabs.
- The README shows screenshots of the panel and the page.

## 0.6.2

- Subagents show up again under **Agents**. Claude Code now launches them asynchronously by default, and the panel was skipping those calls, so almost none were listed.

## 0.6.1

- The browser page's AI Status tab now has the same structure as the panel: plan usage, then each session (collapsible) with its **Commands** and **Agents**, foreground and background together, and **Inactive sessions**. Finished tasks leave it 5 minutes after they end unless **show finished** is on. Sections you open or close stay that way across refreshes. When plan usage has no data, the page shows why.
- A background command whose process died without its end marker (for example when the Claude Code that launched it closed) no longer shows as running forever. On Windows the panel checks whether anything still holds the command's output file open. Elsewhere it is still marked after 6 hours without output.
- Tasks of a resumed session find their output, which stays in the folder of the session that launched them.
- Plan usage: when Claude Code doesn't answer, the panel shows why next to *no data* and retries after 1 minute instead of 5. Before, it stayed empty with no reason given.

## 0.6.0

- Internationalization: English and Portuguese (Brazil). The panel, commands and settings follow VS Code's display language; the browser page follows the browser's language. Other languages fall back to English.

## 0.5.3

- A running background task shows, next to its name, its progress and the last line it wrote, updated at every refresh, instead of just *rodando*.

## 0.5.2

- Each session in the panel groups what it is running under **Comandos** and **Agentes**, foreground and background together.
- A finished task leaves the list 5 minutes after it ends (it shows how long ago it finished), instead of the last 5 finished staying forever.

## 0.5.1

- AI Usage no longer hangs on "calculando o uso…" when another VS Code window was still running an older panel server on the same port: the server now reports its version, and an older one is replaced. A server from before 0.5.1 cannot be replaced automatically, so the panel asks you to reload the other windows.
- The browser page now has two tabs, **AI Status** and **AI Usage**. **Open Charts** opens it on the AI Usage tab.

## 0.5.0

- Each session also shows what it is running right now in the foreground (the tool call still waiting for its result), not only the background tasks.
- New **AI Usage** tab: tokens and cost (at API prices, from LiteLLM's table) for the last 30 days, per day, project, model, token type and tool, plus **Open Charts** for the same data as charts.

## 0.4.1

- Background tasks now sit under the session that launched them (running ones first, then the last 5 finished); tasks of sessions no longer active go under "Sessões inativas". What you expand or collapse is kept across refreshes.

## 0.4.0

- The panel is now a native tree (plan usage, background tasks, sessions) instead of a webview, so moving the panel to another side no longer leaves it blank. Click a task to open its log.
- Plan usage in the status bar (`5h 79% · 7d 51%`).
- "Open in Browser" button for the full page.

## 0.3.6

- No more automatic re-show (it pulled focus away from the other panel tabs). After moving the panel, click the refresh button in its title if it stays blank.

## 0.3.5

- Moving the panel to another side hid the view for good (VS Code sends no further event): the extension now asks for it back. A refresh button in the panel title rebuilds it by hand. An "AI Status" output channel logs what the panel does.

## 0.3.3

- Fix for the blank panel after moving it: the webview is no longer retained while hidden (VS Code draws a retained webview as an overlay that does not follow the panel to its new side); it is rebuilt where the panel is.

## 0.3.2

- The panel gets its data through the extension (webviews may block local network access), and a visible panel that stops asking for data for 10 s is rebuilt, which covers being moved to another side.

## 0.3.1

- The panel page now lives in the webview itself (no iframe), so moving the panel to another side no longer leaves it blank; it redraws whenever it becomes visible.

## 0.3.0

- Plan usage straight from Claude Code (the same `get_usage` request as its own Usage panel), every 5 minutes: no prompt is sent (cost 0) and Claude Code itself handles the login. Works with Claude Code in VS Code too; the status-line script stays as a fallback.
- The panel no longer stays blank after being moved (top/bottom/left/right): the iframe reloads itself when the page stops answering.

## 0.2.1

- Plan usage only from the status line; no more stale numbers from other caches, and no number from a window that has already reset.

## 0.2.0

- Plan usage at the top: 5-hour session and weekly limits with reset times, captured by the bundled status-line script (no token).

## 0.1.0

- First release: background tasks with progress (logs and downloads) and active Claude Code sessions, side by side.
