// Status AI for Claude: native trees (plan usage, sessions with their current and background tasks; AI Usage with
// tokens and cost) and a status-bar item, fed by /api and /api/usage of
// media/server.js (a local, read-only Node server). Reuses a server already listening on the port; otherwise
// starts one and stops it when VS Code closes. A tree, not a webview: a webview view in the panel stayed blank after
// the panel was moved to another side (VS Code leaves its overlay behind and sends no further event).
const vscode = require("vscode");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");

let server = null;
let log = { appendLine() {} };
// UI strings are English in the code; vscode.l10n swaps them for l10n/bundle.l10n.<language>.json (pt-br).
const L = (m, ...a) => vscode.l10n.t(m, ...a);
// The server reports statuses and token kinds in Portuguese (also CSS classes on its page): shown translated here.
const shownStatus = (s) => ({ rodando: L("running"), "sem notícia": L("no news"), completed: L("completed"), failed: L("failed"),
  killed: L("killed"), stopped: L("stopped"), trabalhando: L("working"), "aguardando você": L("waiting for you"),
  "esperando ferramenta": L("waiting on a tool"), parado: L("idle") })[s] || s;
const kindName = (k) => ({ entrada: L("input"), "saída": L("output"), "gravação de cache": L("cache write"), "leitura de cache": L("cache read") })[k] || k;
const say = (m) => log.appendLine(`${new Date().toLocaleTimeString()} ${m}`);

// The server's {version, pid} when it answers, else false. A pre-0.5.1 server answers without them.
function alive(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api", timeout: 3000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        if (res.statusCode !== 200) return resolve(false);
        try { const d = JSON.parse(body); resolve({ version: d.version || "0", pid: d.pid }); } catch { resolve(false); }
      });
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function older(a, b) {
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0);
  return false;
}

async function ensureServer(context) {
  const cfg = vscode.workspace.getConfiguration("aiStatus");
  const port = cfg.get("port");
  const mine = context.extension.packageJSON.version;
  const running = await alive(port);
  // A server left by another window on an older version lacks newer routes (/api/usage): replace it. A newer one
  // is kept, so two windows on different versions don't keep killing each other's server.
  if (running && older(running.version, mine)) {
    if (!running.pid) throw new Error(L("an older AI Status server (before 0.5.1) is on port {0}. Reload the other VS Code windows (Developer: Reload Window) to stop it.", port));
    say(`replacing server ${running.version} (pid ${running.pid}) with ${mine}`);
    try { process.kill(running.pid); } catch {}
    for (let i = 0; i < 20 && (await alive(port)); i++) await new Promise((r) => setTimeout(r, 250));
  } else if (running) return port;
  if (!server || server.exitCode !== null) {
    // VS Code's own Node (its Electron binary with ELECTRON_RUN_AS_NODE): nothing for the user to install.
    const script = path.join(context.extensionPath, "media", "server.js");
    server = spawn(process.execPath, [script, "--port", String(port), "--days", String(cfg.get("days")), "--usage-days", String(cfg.get("usageDays"))],
      { windowsHide: true, stdio: "ignore", env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" } });
    server.on("error", (e) => say(`server failed to start: ${e.message}`));
  }
  for (let i = 0; i < 20; i++) {
    if (await alive(port)) return port;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(L("the panel server did not answer on port {0}. Check that the port is free (setting aiStatus.port).", port));
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

function ago(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.round((s % 86400) / 3600)} h`;
}

function bar(pct) {
  const n = Math.round(Math.min(100, Math.max(0, pct)) / 10);
  return "█".repeat(n) + "░".repeat(10 - n);
}

// A window that has already reset: its number is from before, show nothing.
function windowOf(w, now) {
  return w && !(w.resets_at && w.resets_at < now) ? w : null;
}

function item(label, opts = {}) {
  const state = !opts.children || !opts.children.length ? vscode.TreeItemCollapsibleState.None
    : opts.collapsed ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.Expanded;
  const it = new vscode.TreeItem(label, state);
  if (opts.id) it.id = opts.id;   // keeps what the user expanded or collapsed across refreshes
  it.description = opts.description;
  it.tooltip = opts.tooltip;
  if (opts.icon) it.iconPath = new vscode.ThemeIcon(opts.icon, opts.color && new vscode.ThemeColor(opts.color));
  if (opts.log) it.command = { command: "vscode.open", title: L("Open log"), arguments: [vscode.Uri.file(opts.log)] };
  it.children = opts.children;
  return it;
}

// Every item gets a stable id: without one VS Code keys it by its label, which changes on each refresh ("43%"), and it
// sometimes leaves the old row painted under the new one.
function meter(id, title, w, now, err) {
  if (!w) return item(title, { id, description: err ? L("no data: {0}", err) : L("no data"), tooltip: err, icon: "dashboard" });
  const reset = w.resets_at ? L("resets in {0}", ago(w.resets_at - now)) : "";
  const color = w.pct >= 90 ? "errorForeground" : w.pct >= 70 ? "editorWarning.foreground" : undefined;
  return item(`${title}  ${w.pct.toFixed(0)}%`, { id, description: `${bar(w.pct)}  ${reset}`, icon: "dashboard", color });
}

const KEEP_DONE = 300;   // s that a finished task stays listed

function tree(d) {
  const now = d.now, u = d.usage || {};
  const usage = item(L("Plan usage"), { id: "usage", icon: "pulse", children: [
    meter("usage:5h", L("Session (5 h)"), windowOf(u.five_hour, now), now, d.usage_error),
    meter("usage:7d", L("Week (7 d)"), windowOf(u.seven_day, now), now, d.usage_error),
  ] });
  // Background tasks still running, plus the finished ones for 5 min after they end (then they leave the list).
  const shown = (t) => t.status === "rodando" || t.status === "sem notícia" || (t.ended != null && now - t.ended < KEEP_DONE);
  function task(t) {
    const pct = t.status === "rodando" && t.progress && t.progress.pct != null ? `${t.progress.pct.toFixed(0)}%  ` : "";
    const icon = t.status === "rodando" ? "sync~spin" : t.status === "completed" ? "check" : "error";
    const when = t.ended != null ? ` · ${L("{0} ago", ago(now - t.ended))}` : "";
    // Running: what it last wrote (the progress line, else the log's or the output's last line), refreshed every poll.
    const line = t.status === "rodando" && ((t.progress && t.progress.text) || (t.log_tail || []).slice(-1)[0] || t.last);
    return item(t.description || t.command, { id: `t:${t.session}:${t.id}`, description: line ? `${pct}${line.trim().slice(0, 120)}` : `${pct}${shownStatus(t.status)}${when}`, icon, log: t.log,
      tooltip: [t.command, ...(t.log_tail || [t.last || ""])].join("\n") });
  }
  // The tool call the session is waiting on right now (foreground: Bash without run_in_background, Agent, Read…).
  const fg = (c, id) => item(c.description || c.command || c.tool, { id, icon: "play-circle", color: "charts.green", tooltip: c.command,
    description: `${c.tool} · ${L("foreground")}${c.started ? ` · ${ago(now - Date.parse(c.started) / 1000)}` : ""}` });
  // A session's calls split in two groups: commands (Bash and other tools) and agents (Agent: subagents).
  const groups = (id, current, tasks) => {
    const agent = (x) => x.tool === "Agent";
    const group = (key, name, icon, list) => list.length && item(`${name}  (${list.length})`, { id: `${id}:${key}`, icon, children: list });
    return [
      group("cmd", L("Commands"), "terminal", [...current.filter((c) => !agent(c)).map((c, i) => fg(c, `${id}:cmd:f${i}`)), ...tasks.filter((t) => !agent(t)).map(task)]),
      group("agents", L("Agents"), "hubot", [...current.filter(agent).map((c, i) => fg(c, `${id}:agents:f${i}`)), ...tasks.filter(agent).map(task)]),
    ].filter(Boolean);
  };
  const live = new Set(d.agents.map((a) => a.session));
  const sessions = d.agents.map((a) => {
    const mine = d.tasks.filter((t) => t.session === a.session && shown(t));
    const current = a.current || [];
    const running = mine.filter((t) => t.status === "rodando").length + current.length;
    return item(`${a.project}: ${shownStatus(a.status)}${running ? `  (${L("{0} running", running)})` : ""}`, {
      id: a.session, description: a.action, tooltip: `${a.title}\n${a.model}\n${a.action}`, children: groups(a.session, current, mine),
      collapsed: !running, icon: a.status === "trabalhando" ? "loading~spin" : a.status === "parado" ? "debug-pause" : "comment" });
  });
  const orphans = d.tasks.filter((t) => !live.has(t.session) && shown(t));
  if (orphans.length) {
    sessions.push(item(L("Inactive sessions"), { id: "inactive", icon: "history", children: groups("inactive", [], orphans),
      collapsed: !orphans.some((t) => t.status === "rodando") }));
  }
  return [usage, item(`${L("Sessions")}  (${d.agents.length})`, { id: "sessions", icon: "hubot", children: sessions })];
}

const money = (c) => `$${c.toFixed(2)}`;
const tokens = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : `${Math.round(n)}`;

// AI Usage tab: tokens and cost (API prices) from /api/usage, as bars relative to the largest row of each group.
function usageTree(u) {
  if (!u.days) return [item(L("calculating usage…"), { id: "u-loading", icon: "loading~spin", description: L("reading the Claude Code histories") })];
  const val = (r) => (u.priced ? r.cost : r.tokens);
  const label = (r) => `${u.priced ? `${money(r.cost)} · ` : ""}${tokens(r.tokens)} tokens`;
  const bars = (id, rows, name, val, label) => {
    const max = Math.max(...rows.map(val), 1e-9);
    return rows.map((r) => item(name(r), { id: `${id}:${r.name}`, description: `${bar((100 * val(r)) / max)}  ${label(r)}` }));
  };
  const group = (id, title, icon, children, collapsed) => item(title, { id, icon, children, collapsed });
  const days = u.days.slice(-14).reverse();
  const weekday = (s) => new Date(`${s}T12:00:00`).toLocaleDateString(vscode.env.language, { weekday: "short", day: "2-digit", month: "2-digit" });
  return [
    item(`${L("Last {0} days", u.period)}  ${u.priced ? money(u.total.cost) : tokens(u.total.tokens)}`, { id: "u-total", icon: "graph",
      description: `${tokens(u.total.tokens)} tokens · ${L("today {0}", label(u.today))}`,
      tooltip: u.priced ? L("Cost at API prices (LiteLLM table), not what the plan charges.") + (u.unpriced.length ? `\n${L("No price: {0}", u.unpriced.join(", "))}` : "")
        : L("No price table (offline): tokens only.") }),
    group("u-days", L("Per day (14 d)"), "calendar", bars("u-days", days, (r) => weekday(r.name), val, label)),
    group("u-projects", L("Projects"), "folder", bars("u-projects", u.projects, (r) => r.name, val, label)),
    group("u-models", L("Models"), "hubot", bars("u-models", u.models, (r) => r.name, val, label), true),
    group("u-kinds", L("Token types"), "symbol-number", bars("u-kinds", u.kinds, (r) => kindName(r.name), val, label), true),
    group("u-tools", L("Tools"), "tools", bars("u-tools", u.tools, (r) => r.name, (r) => r.count, (r) => L("{0} calls", r.count)), true),
  ];
}

function usageText(d) {
  const u = (d && d.usage) || {};
  const part = (label, w) => (windowOf(w, d.now) ? `${label} ${w.pct.toFixed(0)}%` : null);
  return [part("5h", u.five_hour), part("7d", u.seven_day)].filter(Boolean).join(" · ");
}

class Provider {
  constructor(context, urlPath, build) {
    this.context = context;
    this.urlPath = urlPath;
    this.build = build;
    this.changed = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.changed.event;
    this.roots = [item(L("loading…"), { id: "loading", icon: "loading~spin" })];
  }

  getTreeItem(it) {
    return it;
  }

  getChildren(it) {
    return it ? it.children || [] : this.roots;
  }

  async refresh() {
    try {
      const port = await ensureServer(this.context);
      this.data = JSON.parse(await get(port, this.urlPath));
      this.roots = this.build(this.data);
    } catch (e) {
      say(`refresh failed: ${e.message}`);
      this.roots = [item(L("AI Status unavailable"), { id: "error", description: e.message, tooltip: e.message, icon: "error" })];
    }
    this.changed.fire();
  }
}

function activate(context) {
  log = vscode.window.createOutputChannel("AI Status");
  const provider = new Provider(context, "/api", tree);
  const view = vscode.window.createTreeView("aiStatus.view", { treeDataProvider: provider });
  const usageProvider = new Provider(context, "/api/usage", usageTree);
  const usageView = vscode.window.createTreeView("aiStatus.usage", { treeDataProvider: usageProvider });
  // ?lang=: the page follows VS Code's display language unless the viewer picks one on the page.
  const page = (p) => `http://localhost:${vscode.workspace.getConfiguration("aiStatus").get("port")}/?lang=${encodeURIComponent(vscode.env.language)}${p}`;
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "aiStatus.view.focus";
  status.tooltip = L("AI Status: Claude Code plan usage (5 h · 7 d)");
  let at = 0;
  const tick = async (force) => {
    // ponytail: polls the server every 3 s while the tree is visible, every 30 s (status bar only) otherwise.
    if (!force && !view.visible && Date.now() - at < 30000) return;
    at = Date.now();
    await provider.refresh();
    status.text = `$(pulse) ${usageText(provider.data) || "AI Status"}`;
    status.show();
  };
  let usageAt = 0;
  const usageTick = async (force) => {
    // ponytail: the server recomputes usage once a minute, so polling it every 30 s while visible is plenty.
    if (!force && (!usageView.visible || Date.now() - usageAt < 30000)) return;
    usageAt = Date.now();
    await usageProvider.refresh();
  };
  tick(true);
  const timer = setInterval(() => { tick(); usageTick(); }, 3000);
  context.subscriptions.push(log, view, usageView, status, { dispose: () => clearInterval(timer) },
    view.onDidChangeVisibility(() => view.visible && tick(true)),
    usageView.onDidChangeVisibility(() => usageView.visible && usageTick(true)),
    vscode.commands.registerCommand("aiStatus.reload", () => tick(true)),
    vscode.commands.registerCommand("aiStatus.reloadUsage", () => usageTick(true)),
    vscode.commands.registerCommand("aiStatus.openBrowser", () => vscode.env.openExternal(vscode.Uri.parse(page("")))),
    vscode.commands.registerCommand("aiStatus.openUsage", () => vscode.commands.executeCommand("simpleBrowser.show", page("#usage"))));
}

function deactivate() {
  if (server && server.exitCode === null) server.kill();
}

module.exports = { activate, deactivate, tree, usageTree, usageText };
