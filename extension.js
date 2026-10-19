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

function get(port, urlPath) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port, path: urlPath, timeout: 5000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve(body));
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
  });
}

// The server's page goes straight into the webview (no iframe: an iframe to localhost stayed blank after the panel
// was moved to another side). Its script fetches /api from the server; the server allows only vscode-webview origins.
async function page(port, error) {
  if (error) {
    return `<!doctype html><body style="font:13px system-ui;color:var(--vscode-foreground);padding:12px">
      AI Status unavailable: ${String(error).replace(/[<>&]/g, "")}</body>`;
  }
  const api = `http://127.0.0.1:${port}/api`;
  const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src ${api}; style-src 'unsafe-inline'; script-src 'unsafe-inline';">`;
  const html = await get(port, "/");
  return html.replace("<head>", `<head>${csp}<script>window.AI_STATUS_API=${JSON.stringify(api)}</script>`);
}

class Provider {
  constructor(context) {
    this.context = context;
  }

  async resolveWebviewView(view) {
    this.view = view;
    view.onDidDispose(() => { if (this.view === view) this.view = null; });
    view.onDidChangeVisibility(() => { if (view.visible && this.view === view) this.render(); });
    await this.render();
  }

  async render() {
    if (!this.view) return;
    try {
      const port = await ensureServer(this.context);
      this.view.webview.options = { enableScripts: true };
      this.view.webview.html = await page(port);
    } catch (e) {
      this.view.webview.html = await page(0, e.message);
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
