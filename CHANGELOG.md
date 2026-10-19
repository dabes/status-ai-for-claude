# Changelog

## 0.3.0

- Plan usage straight from Claude Code (the same `get_usage` request as its own Usage panel), every 5 minutes: no prompt is sent (cost 0) and Claude Code itself handles the login. Works with Claude Code in VS Code too; the status-line script stays as a fallback.
- The panel no longer stays blank after being moved (top/bottom/left/right): the iframe reloads itself when the page stops answering.

## 0.2.1

- Plan usage only from the status line; no more stale numbers from other caches, and no number from a window that has already reset.

## 0.2.0

- Plan usage at the top: 5-hour session and weekly limits with reset times, captured by the bundled status-line script (no token).

## 0.1.0

- First release: background tasks with progress (logs and downloads) and active Claude Code sessions, side by side.
