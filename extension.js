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

// The server's page goes straight into the webview (no iframe). The webview never talks to 127.0.0.1 itself (VS Code
// webviews may block local network access): the page asks the extension for /api, and the extension fetches it.
const BRIDGE = `<script>(() => {
  const vs = acquireVsCodeApi(), waiting = {};
  let next = 0;
  addEventListener("message", (e) => { const m = e.data || {}; if (waiting[m.id]) { waiting[m.id](m); delete waiting[m.id]; } });
  window.AI_STATUS_FETCH = () => new Promise((ok, fail) => {
    const id = ++next;
    waiting[id] = (m) => (m.error ? fail(new Error(m.error)) : ok(m.data));
    vs.postMessage({ id, type: "api" });
  });
})()</script>`;

async function page(port, error) {
  if (error) {
    return `<!doctype html><body style="font:13px system-ui;color:var(--vscode-foreground);padding:12px">
      AI Status unavailable: ${String(error).replace(/[<>&]/g, "")}</body>`;
  }
  const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">`;
  const html = await get(port, "/");
  return html.replace("<head>", `<head>${csp}${BRIDGE}`);
}

class Provider {
  constructor(context) {
    this.context = context;
  }

  async resolveWebviewView(view) {
    this.view = view;
    this.lastAsk = Date.now();
    view.onDidDispose(() => { if (this.view === view) this.view = null; });
    view.onDidChangeVisibility(() => { if (view.visible && this.view === view) this.render(); });
    view.webview.onDidReceiveMessage(async (m) => {
      if (!m || m.type !== "api") return;
      this.lastAsk = Date.now();
      try {
        const port = vscode.workspace.getConfiguration("aiStatus").get("port");
        view.webview.postMessage({ id: m.id, data: JSON.parse(await get(port, "/api")) });
      } catch (e) {
        view.webview.postMessage({ id: m.id, error: String(e.message || e) });
      }
    });
    await this.render();
  }

  // The page asks for data every 3 s. A visible panel that went quiet was recreated blank (moved to another side):
  // put the page back.
  watch() {
    if (this.view && this.view.visible && !this.rendering && Date.now() - (this.lastAsk || 0) > 10000) {
      this.lastAsk = Date.now();
      this.render();
    }
  }

  async render() {
    if (!this.view) return;
    this.rendering = true;
    try {
      const port = await ensureServer(this.context);
      this.view.webview.options = { enableScripts: true };
      const html = await page(port);
      if (this.view) this.view.webview.html = html;
    } catch (e) {
      if (this.view) this.view.webview.html = await page(0, e.message);
    } finally {
      this.rendering = false;
    }
  }
}

function activate(context) {
  const provider = new Provider(context);
  context.subscriptions.push(
    // No retainContextWhenHidden: a retained webview is an overlay that stays behind when the panel is moved.
    vscode.window.registerWebviewViewProvider("aiStatus.view", provider),
    vscode.commands.registerCommand("aiStatus.reload", () => provider.render()),
    { dispose: clearInterval.bind(null, setInterval(() => provider.watch(), 5000)) }
  );
}

function deactivate() {
  if (server && server.exitCode === null) server.kill();
}

module.exports = { activate, deactivate };
