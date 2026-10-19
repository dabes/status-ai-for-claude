// AI Status for Claude Code: shows the page served by media/ai_status.py (a local, read-only Python server) in a
// webview. Reuses a server already listening on the port; otherwise starts one and stops it when VS Code closes.
const vscode = require("vscode");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");

let server = null;

function alive(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api", timeout: 3000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

async function ensureServer(context) {
  const cfg = vscode.workspace.getConfiguration("aiStatus");
  const port = cfg.get("port");
  if (await alive(port)) return port;
  if (!server || server.exitCode !== null) {
    const python = cfg.get("python") || (process.platform === "win32" ? "python" : "python3");
    const script = path.join(context.extensionPath, "media", "ai_status.py");
    server = spawn(python, [script, "--port", String(port), "--days", String(cfg.get("days"))], { windowsHide: true, stdio: "ignore" });
    server.on("error", () => {});
  }
  for (let i = 0; i < 20; i++) {
    if (await alive(port)) return port;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`the panel server did not answer on port ${port}. Check that Python 3 is installed (setting aiStatus.python) and the port is free (aiStatus.port).`);
}

function page(port, error) {
  if (error) {
    return `<!doctype html><body style="font:13px system-ui;color:var(--vscode-foreground);padding:12px">
      AI Status unavailable: ${String(error).replace(/[<>&]/g, "")}</body>`;
  }
  return `<!doctype html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src http://localhost:${port}; style-src 'unsafe-inline';">
    <style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100%;overflow:hidden}</style></head>
    <body><iframe src="http://localhost:${port}/"></iframe></body></html>`;
}

class Provider {
  constructor(context) {
    this.context = context;
  }

  async resolveWebviewView(view) {
    this.view = view;
    await this.render();
  }

  async render() {
    if (!this.view) return;
    try {
      const port = await ensureServer(this.context);
      this.view.webview.options = { enableScripts: true, portMapping: [{ webviewPort: port, extensionHostPort: port }] };
      this.view.webview.html = page(port);
    } catch (e) {
      this.view.webview.html = page(0, e.message);
    }
  }
}

function activate(context) {
  const provider = new Provider(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("aiStatus.view", provider, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.commands.registerCommand("aiStatus.reload", () => provider.render())
  );
}

function deactivate() {
  if (server && server.exitCode === null) server.kill();
}

module.exports = { activate, deactivate };
