# Changelog

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
