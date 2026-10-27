#!/usr/bin/env node
// AI Status: os comandos em segundo plano do Claude Code (com o progresso) e as sessões (agentes) e o que cada uma faz.
//
// Lê os históricos (~/.claude/projects/<projeto>/<sessão>.jsonl): o comando lançado com run_in_background, o ID que a
// ferramenta devolveu e a notificação de fim (<task-notification> com o status). A saída de cada um fica em
// %TEMP%/claude/<projeto>/<sessão>/tasks/<id>.output. Só a biblioteca padrão do Node; só lê, não mexe em nada.
// A extensão roda este arquivo com o Node do próprio VS Code (ELECTRON_RUN_AS_NODE), sem nada para instalar.
//
// Uso:  node server.js [--port 8765] [--days 3] [--usage-days 30]   e abra http://localhost:8765
//       node server.js --selftest
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

// Pasta de configuração do Claude Code (CLAUDE_CONFIG_DIR quando definida, senão ~/.claude).
const CONFIG = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const PROJECTS = path.join(CONFIG, "projects");
const TASKS = path.join(os.tmpdir(), "claude");
const USAGE = path.join(CONFIG, "ai-status-usage.json");   // gravado por media/statusline.js (statusLine do Claude Code)
const LAUNCH = /(?:running in background with ID: |agentId: )([A-Za-z0-9]{6,})/;
const HANDBACK = /<agent-message from="([^"]+)">\s*\[Subagent hand-back\]/g;
const NOTE = /<task-id>([^<]+)<\/task-id>.*?<status>([^<]+)<\/status>(?:.*?<summary>([^<]*)<\/summary>)?/s;
const cache = new Map();   // caminho -> {mtime, st: estado incremental de parseSession}
const now = () => Date.now() / 1000;

function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((c) => (c && typeof c === "object" ? c.text || "" : String(c))).join("\n");
  return "";
}

function firstLine(s) {
  return s.trim().split(/\r?\n/)[0] || "";
}

// Bytes novos do arquivo desde o offset, só até a última quebra de linha (linha pela metade fica para a próxima).
function readNew(file, offset) {
  const fd = fs.openSync(file, "r");
  try {
    const size = fs.fstatSync(fd).size;
    if (size <= offset) return { lines: [], offset };
    const buf = Buffer.alloc(size - offset);
    fs.readSync(fd, buf, 0, buf.length, offset);
    const end = buf.lastIndexOf(10) + 1;
    return { lines: buf.subarray(0, end).toString("utf8").split(/\r?\n/), offset: offset + end };
  } finally {
    fs.closeSync(fd);
  }
}

// Tarefas lançadas numa sessão: id -> dados (comando, descrição, início, status). Incremental: com o estado da
// chamada anterior, lê só o que foi escrito depois (o histórico da sessão passa de centenas de MB).
function parseSession(file, state) {
  const st = state || { offset: 0, calls: {}, tasks: {}, title: "", cwd: "" };
  const { calls, tasks } = st;
  const got = readNew(file, st.offset);
  st.offset = got.offset;
  for (const line of got.lines) {
    if (!line) continue;
    if (!line.includes("run_in_background") && !line.includes("task-id") && !line.includes("background with ID")
        && !line.includes("agentId") && !line.includes("agent-message") && !line.includes('"Agent"')   // Agent assíncrono: a chamada não traz nenhuma das outras marcas
        && st.title && st.cwd) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }
    st.cwd = st.cwd || d.cwd || "";
    const msg = d.message || {};
    const content = msg.content;
    if (d.type === "user" && !st.title && typeof content === "string" && !content.startsWith("<")) st.title = firstLine(content).slice(0, 90);
    if (Array.isArray(content)) {
      for (const c of content) {
        if (!c || typeof c !== "object") continue;
        if (c.type === "tool_use") {
          const inp = c.input || {};
          if (inp.run_in_background || c.name === "Agent") {
            calls[c.id] = { tool: c.name, description: inp.description || "", command: inp.command || inp.prompt || "" };
          }
        } else if (c.type === "tool_result" && calls[c.tool_use_id]) {
          const result = textOf(c.content);
          const m = LAUNCH.exec(result);
          if (m) {
            // Subagente sem segundo plano: o resultado já é o fim (o relatório).
            const done = calls[c.tool_use_id].tool === "Agent" && !result.toLowerCase().includes("launched");
            tasks[m[1]] = { ...calls[c.tool_use_id], started: d.timestamp || "", status: done ? "completed" : "rodando", summary: "",
              ended: done ? d.timestamp || "" : "" };
          }
        }
      }
    }
    const body = textOf(content) + "\n" + (typeof d.content === "string" ? d.content : "");
    // Um aviso pode trazer várias tarefas (as que morreram com a sessão vêm juntas): todos os <task-id> antes do <status>.
    for (const chunk of body.split("<task-notification>").slice(1)) {
      const m = NOTE.exec(chunk);
      if (!m) continue;
      const head = chunk.slice(0, m.index + m[0].indexOf("<status>"));
      for (const [, tid] of head.matchAll(/<task-id>([^<]+)<\/task-id>/g)) {
        if (tasks[tid]) {
          tasks[tid].status = m[2];
          tasks[tid].summary = (m[3] || "").trim();
          tasks[tid].ended = d.timestamp || tasks[tid].ended || "";   // api-request-blob repete o aviso sem hora
        }
      }
    }
    for (const [, tid] of body.matchAll(HANDBACK)) {
      if (tasks[tid]) {
        tasks[tid].status = "completed";
        tasks[tid].ended = d.timestamp || tasks[tid].ended || "";
      }
    }
  }
  return st;
}

// O que a sessão está fazendo, pelo fim do histórico: modelo, última ação (ferramenta ou resposta) e último pedido.
function agentState(file) {
  const { lines, age } = tailOf(file, 256000);
  let model = "", action = "", kind = "", prompt = "", when = "";
  const done = new Set(), current = [];   // current: ferramentas chamadas que ainda não devolveram resultado
  let settled = false;
  for (let i = lines.length - 1; i >= 0; i--) {
    let d;
    try { d = JSON.parse(lines[i]); } catch { continue; }
    if (d.isSidechain) continue;
    const msg = d.message || {};
    const content = msg.content;
    if (!when && d.timestamp) when = d.timestamp;
    for (const c of Array.isArray(content) ? content : []) {
      if (!c || typeof c !== "object") continue;
      if (c.type === "tool_result") done.add(c.tool_use_id);
      else if (c.type === "tool_use" && d.type === "assistant" && !settled) {
        const inp = c.input || {};
        if (done.has(c.id)) settled = true;   // daqui para trás o turno já terminou
        else if (!inp.run_in_background) {    // as de segundo plano já estão em tasks
          current.push({ tool: c.name, started: d.timestamp || "",
            description: inp.description || inp.file_path || inp.query || "",
            command: String(inp.command || inp.prompt || "").slice(0, 2000) });
        }
      }
    }
    if (d.type === "user" && typeof content === "string") settled = true;   // um pedido novo: o que ficou sem resultado antes dele foi interrompido
    if (d.type === "assistant" && !kind) {
      model = msg.model || "";
      const parts = Array.isArray(content) ? content : [];
      const tool = [...parts].reverse().find((c) => c && c.type === "tool_use");
      const text = ([...parts].reverse().find((c) => c && c.type === "text") || {}).text || "";
      if (tool) {
        const inp = tool.input || {};
        kind = "tool";
        action = `${tool.name}: ${inp.description || inp.command || inp.file_path || inp.query || ""}`;
      } else if (text.trim()) {
        kind = "text";
        action = firstLine(text);
      }
    } else if (d.type === "user" && !prompt && typeof content === "string" && !content.startsWith("<")) {
      prompt = firstLine(content);
    }
    if (kind && prompt && settled) break;
  }
  return { model, action: action.slice(0, 180), kind, prompt: prompt.slice(0, 140), age, last: when,
    current: (age != null && age > 6 * 3600) ? [] : current.reverse() };   // nada escrito há horas: a sessão morreu com a ferramenta aberta
}

// Limites do plano (5 h e 7 d), perguntados ao próprio Claude Code (o pedido get_usage, o mesmo do painel Usage da
// extensão oficial): nenhum prompt vai ao modelo (custo 0) e quem lê a credencial é o Claude Code, não este script.
const USAGE_EVERY = 120;   // s entre consultas ao Claude Code (custo 0; a previsão precisa de leituras frequentes)
let cliUsage = null, cliError = null;

function which(name) {
  const exts = process.platform === "win32" ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    for (const ext of exts) {
      const p = path.join(dir, name + ext);
      try { if (fs.statSync(p).isFile()) return p; } catch {}
    }
  }
  return null;
}

function cliUsageOnce() {
  const exe = which("claude") || path.join(os.homedir(), ".local", "bin", process.platform === "win32" ? "claude.exe" : "claude");
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;   // a marca do Node do VS Code não passa adiante
  return new Promise((resolve, reject) => {
    const p = spawn(exe, ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
      "--no-session-persistence", "--setting-sources", ""], { windowsHide: true, env });   // sem hooks, sem gravar sessão
    const other = [];   // o que não é JSON (erros do claude): vira o motivo da falha
    let buf = "", settledP = false;
    const finish = (err, value) => {
      if (settledP) return;
      settledP = true;
      clearTimeout(timer);
      p.kill();
      err ? reject(err) : resolve(value);
    };
    const timer = setTimeout(() => finish(new Error(`claude (${exe}) saiu sem responder (30 s)`)), 30000);
    const onData = (chunk) => {
      buf += chunk;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        let m;
        try { m = JSON.parse(line); } catch { if (line.trim()) other.push(line.trim()); continue; }
        if (m.type === "control_response") {
          const r = m.response || {};
          if (r.subtype === "error") return finish(new Error(`claude: ${r.error}`));
          return finish(null, r.response || {});
        }
      }
    };
    p.stdout.setEncoding("utf8").on("data", onData);
    p.stderr.setEncoding("utf8").on("data", onData);
    p.on("error", (e) => finish(e));
    p.on("close", () => finish(new Error(`claude (${exe}) saiu sem responder` + (other.length ? `: ${other.join(" ").slice(-300)}` : ""))));
    p.stdin.on("error", () => {});
    p.stdin.end(JSON.stringify({ type: "control_request", request_id: "ai-status", request: { subtype: "get_usage" } }) + "\n");
  });
}

function cliWindow(w) {
  if (!w || w.utilization == null) return null;
  let reset = w.resets_at;
  if (typeof reset === "string") reset = Date.parse(reset) / 1000;
  return { pct: Number(w.utilization), resets_at: reset };
}

async function cliUsageLoop() {
  for (;;) {
    let wait = USAGE_EVERY;
    try {
      const r = await cliUsageOnce();
      const rl = r.rate_limits || {};
      if (!(rl.five_hour || rl.seven_day)) throw new Error(`claude respondeu sem rate_limits (rate_limits_available=${r.rate_limits_available})`);
      cliUsage = { source: "claude", updated: now(), five_hour: cliWindow(rl.five_hour), seven_day: cliWindow(rl.seven_day) };
      cliError = null;
    } catch (e) {   // qualquer falha: fica o último dado (ou o da linha de status) e tenta logo de novo
      cliError = e.message || String(e);
      wait = 60;
    }
    await new Promise((r) => setTimeout(r, wait * 1000));
  }
}

// Do Claude Code (cliUsageLoop) ou, sem ele, do statusline.js (o que o Claude Code de terminal passa à linha de
// status); o mais recente vence.
function usage() {
  const windowOf = (w) => (!w || w.used_percentage == null ? null : { pct: Number(w.used_percentage), resets_at: w.resets_at });
  let best = cliUsage;
  try {
    const d = JSON.parse(fs.readFileSync(USAGE, "utf8"));
    const rl = d.rate_limits || {};
    if (!best || (d.updated || 0) > best.updated) {
      best = { source: "statusline", updated: d.updated, five_hour: windowOf(rl.five_hour), seven_day: windowOf(rl.seven_day) };
    }
  } catch {}
  if (!best) return best;
  return { ...best, five_hour: forecast("five_hour", best.five_hour, best.updated), seven_day: forecast("seven_day", best.seven_day, best.updated) };
}

// Previsão: no ritmo da última hora (5 h) ou das últimas 6 h (semana), quando o uso chega a 100%. Guarda as leituras
// de cada janela desde que ela reiniciou (muda o resets_at); null se ainda não há ritmo (menos de 10 min, ou parado).
// ponytail: histórico só em memória; reiniciar o servidor zera a previsão até juntar 10 min de leituras de novo.
const PACE = { five_hour: 3600, seven_day: 6 * 3600 };
const history = { five_hour: [], seven_day: [] };
function forecast(key, w, updated) {
  if (!w) return w;
  const h = history[key];
  if (h.length && Math.abs((h[h.length - 1].resets_at || 0) - (w.resets_at || 0)) > 120) h.length = 0;   // a janela reiniciou
  if (updated && (!h.length || updated > h[h.length - 1].t)) h.push({ t: updated, pct: w.pct, resets_at: w.resets_at });
  while (h.length > 1 && h[0].t < updated - PACE[key]) h.shift();
  return { ...w, forecast: pace(h, w) };
}

function pace(h, w) {
  if (h.length < 2) return null;
  const a = h[0], b = h[h.length - 1];
  if (b.t - a.t < 600) return null;
  const perHour = ((b.pct - a.pct) / (b.t - a.t)) * 3600;
  if (perHour <= 0) return { per_hour: 0, hits_at: null };
  const hits = b.t + ((100 - b.pct) / perHour) * 3600;
  return { per_hour: perHour, hits_at: w.resets_at && hits >= w.resets_at ? null : hits };   // null: não chega antes de reiniciar
}

// Avisos: cada janela do VS Code pergunta antes de mostrar; só a primeira a pedir uma chave mostra (sem repetir o
// mesmo aviso em todas as janelas abertas).
const claimed = new Set();
function claim(key) {
  if (claimed.size > 5000) claimed.clear();
  if (!key || claimed.has(key)) return false;
  claimed.add(key);
  return true;
}

// Uso em tokens e custo: todas as respostas do modelo nos históricos (sessões e subagentes), contadas uma vez só
// (o Claude Code grava a mesma mensagem numa linha por bloco, e a sessão retomada copia o histórico).
const PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json";
const KINDS = ["entrada", "saída", "gravação de cache", "leitura de cache"];
let prices = {};
const seen = new Set();
const offsets = new Map();
const buckets = new Map();   // "dia\0projeto\0modelo\0fast" -> [entrada, saída, cache 5 min, cache 1 h, leitura de cache]
const tools = new Map();     // "dia\0projeto\0ferramenta" -> chamadas
let report = {};            // o último usageReport, trocado inteiro

const localDay = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

// Soma o que foi escrito no histórico desde a última leitura.
function scanUsage(file) {
  let off = offsets.get(file) || 0;
  const fd = fs.openSync(file, "r");
  try {
    const size = fs.fstatSync(fd).size;
    const CHUNK = 8 << 20;   // em pedaços: o histórico passa de centenas de MB
    let rest = Buffer.alloc(0);
    for (let pos = off; pos < size;) {
      const buf = Buffer.alloc(Math.min(CHUNK, size - pos));
      const n = fs.readSync(fd, buf, 0, buf.length, pos);
      if (!n) break;
      pos += n;
      const data = rest.length ? Buffer.concat([rest, buf.subarray(0, n)]) : buf.subarray(0, n);
      let start = 0, i;
      while ((i = data.indexOf(10, start)) >= 0) {
        const raw = data.subarray(start, i);
        off += i + 1 - start;
        start = i + 1;
        if (raw.indexOf('"usage"') < 0) continue;
        let d;
        try { d = JSON.parse(raw.toString("utf8")); } catch { continue; }
        const msg = d.message || {};
        const u = msg.usage, model = msg.model || "", ts = d.timestamp;
        if (d.type !== "assistant" || !u || !ts || model.startsWith("<")) continue;   // <synthetic>: sem chamada
        const day = localDay(Date.parse(ts));
        const project = path.basename((d.cwd || "").replace(/[\\/]+$/, "")) || path.basename(path.dirname(file));
        for (const c of Array.isArray(msg.content) ? msg.content : []) {
          if (c && c.type === "tool_use" && !seen.has(c.id)) {
            seen.add(c.id);
            const k = `${day}\0${project}\0${c.name}`;
            tools.set(k, (tools.get(k) || 0) + 1);
          }
        }
        const key = `${msg.id}:${d.requestId}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const h1 = (u.cache_creation || {}).ephemeral_1h_input_tokens || 0;
        const row = [u.input_tokens || 0, u.output_tokens || 0, (u.cache_creation_input_tokens || 0) - h1, h1, u.cache_read_input_tokens || 0];
        const bk = `${day}\0${project}\0${model}\0${u.speed === "fast" ? 1 : 0}`;
        const b = buckets.get(bk) || [0, 0, 0, 0, 0];
        row.forEach((x, j) => (b[j] += x));
        buckets.set(bk, b);
      }
      rest = Buffer.from(data.subarray(start));   // linha pela metade fica para a próxima
    }
  } finally {
    fs.closeSync(fd);
  }
  offsets.set(file, off);
}

// US$ de cada tipo de token (KINDS) pela tabela do LiteLLM (preço de API); null se o modelo não está nela.
function costs(model, fast, t) {
  const k = [model, model.replace(/-\d{8}$/, ""), "anthropic/" + model].find((x) => prices[x]);
  if (!k) return null;
  const p = prices[k];
  const w = p.cache_creation_input_token_cost || 0;
  const c = [t[0] * (p.input_cost_per_token || 0), t[1] * (p.output_cost_per_token || 0),
    t[2] * w + t[3] * (p.cache_creation_input_token_cost_above_1hr || w), t[4] * (p.cache_read_input_token_cost || 0)];
  const f = fast ? ((p.provider_specific_entry || {}).fast ?? 1) : 1;
  return c.map((x) => x * f);
}

function usageReport(days) {
  const dayAgo = (n) => localDay(Date.now() - n * 86400000);
  const start = dayAgo(days - 1), today = dayAgo(0);
  const groups = { days: new Map(), projects: new Map(), models: new Map() };
  const kinds = KINDS.map(() => [0, 0]);
  const unpriced = new Set();
  for (const [bk, t] of buckets) {
    const [day, project, model, fast] = bk.split("\0");
    if (day < start) continue;
    let c = costs(model, fast === "1", t);
    if (!c) { unpriced.add(model); c = [0, 0, 0, 0]; }
    const tok = [t[0], t[1], t[2] + t[3], t[4]];
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    for (const [g, name] of [["days", day], ["projects", project], ["models", model]]) {
      const r = groups[g].get(name) || { name, cost: 0, tokens: 0 };
      r.cost += sum(c);
      r.tokens += sum(tok);
      groups[g].set(name, r);
    }
    for (let i = 0; i < 4; i++) { kinds[i][0] += tok[i]; kinds[i][1] += c[i]; }
  }
  const toolSum = new Map();
  for (const [k, n] of tools) {
    const [day, , name] = k.split("\0");
    if (day >= start) toolSum.set(name, (toolSum.get(name) || 0) + n);
  }
  const zero = { cost: 0, tokens: 0 };
  const top = (g) => [...groups[g].values()].sort((a, b) => b.cost - a.cost || b.tokens - a.tokens);
  const every = Array.from({ length: days }, (_, i) => dayAgo(days - 1 - i));
  return { period: days, priced: Object.keys(prices).length > 0, unpriced: [...unpriced].sort(),
    total: { cost: kinds.reduce((s, k) => s + k[1], 0), tokens: kinds.reduce((s, k) => s + k[0], 0) },
    today: groups.days.get(today) || zero,
    days: every.map((d) => groups.days.get(d) || { ...zero, name: d }),
    projects: top("projects"), models: top("models"),
    kinds: KINDS.map((n, i) => ({ name: n, tokens: kinds[i][0], cost: kinds[i][1] })),
    tools: [...toolSum].sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count })) };
}

function* walk(dir) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else if (e.name.endsWith(".jsonl")) yield p;
  }
}

async function usageLoop(days) {
  for (;;) {
    if (!Object.keys(prices).length) {
      try { prices = await (await fetch(PRICES_URL, { signal: AbortSignal.timeout(30000) })).json(); }
      catch { prices = {}; }   // offline: só tokens, tenta de novo na próxima volta
    }
    const cutoff = now() - days * 86400;
    for (const file of walk(PROJECTS)) {
      try { if (fs.statSync(file).mtimeMs / 1000 >= cutoff) scanUsage(file); } catch {}
      await new Promise((r) => setImmediate(r));   // entre um arquivo e outro, o servidor responde
    }
    report = { ...usageReport(days), updated: now() };
    await new Promise((r) => setTimeout(r, 60000));
  }
}

// Arquivos .jsonl de sessão (direto na pasta de cada projeto; os de subagentes ficam em subpastas).
function sessionFiles() {
  const out = [];
  let dirs;
  try { dirs = fs.readdirSync(PROJECTS, { withFileTypes: true }); } catch { return out; }
  for (const d of dirs) {
    if (!d.isDirectory()) continue;
    let files;
    try { files = fs.readdirSync(path.join(PROJECTS, d.name)); } catch { continue; }
    for (const f of files) if (f.endsWith(".jsonl")) out.push(path.join(PROJECTS, d.name, f));
  }
  return out;
}

const projectOf = (cwd, file) => path.basename((cwd || "").replace(/[\\/]+$/, "")) || path.basename(path.dirname(file));
const stem = (file) => path.basename(file, ".jsonl");

// Sessões do Claude Code com atividade nas últimas horas, com o estado e as tarefas rodando de cada uma.
function agents(rows, hours = 24) {
  const out = [], t = now();
  for (const file of sessionFiles()) {
    let mtime;
    try { mtime = fs.statSync(file).mtimeMs / 1000; } catch { continue; }
    if (t - mtime > hours * 3600 || !cache.has(file)) continue;
    const st = cache.get(file).st;
    const a = agentState(file);
    const age = a.age || 0;
    let status = age < 600 ? (a.kind === "tool" ? "trabalhando" : "aguardando você") : "parado";
    if (a.kind === "tool" && age >= 120) status = age < 600 ? "esperando ferramenta" : "parado";
    const running = rows.filter((r) => r.session === stem(file) && r.status === "rodando");
    out.push({ ...a, session: stem(file), project: projectOf(st.cwd, file), title: st.title, status, running: running.length,
      subagents: running.filter((r) => r.tool === "Agent").map((r) => r.description) });
  }
  return out.sort((x, y) => (x.age || 0) - (y.age || 0));
}

const utf8 = new TextDecoder("utf-8", { fatal: true });
const cp1252 = new TextDecoder("windows-1252");

// Linhas não vazias do fim do arquivo, tamanho e idade (s) da última escrita; {lines: [], size: 0, age: null} se não existe.
function tailOf(file, nbytes = 64000) {
  let st, raw;
  try {
    st = fs.statSync(file);
    const fd = fs.openSync(file, "r");
    try {
      const start = Math.max(0, st.size - nbytes);
      raw = Buffer.alloc(st.size - start);
      fs.readSync(fd, raw, 0, raw.length, start);
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return { lines: [], size: 0, age: null };
  }
  let text;
  try { text = utf8.decode(raw); } catch { text = cp1252.decode(raw); }   // saída do Python no console do Windows (cp1252)
  return { lines: text.replace(/\r/g, "\n").split("\n").filter((l) => l.trim()), size: st.size, age: now() - st.mtimeMs / 1000 };
}

const REDIRECT = /(?:\d?>>?|\btee(?:\s+-a)?)\s*("[^"]+"|'[^']+'|[^\s;&|<>]+)/g;

// Caminho do Git Bash (/c/...) vira do Windows (C:/...); relativo fica junto do cwd.
function winPath(p, cwd) {
  p = p.replace(/^["']|["']$/g, "").replace(/^\/([a-zA-Z])\//, (_, d) => d.toUpperCase() + ":/");
  return path.isAbsolute(p) || /^[A-Za-z]:[\\/]/.test(p) || !cwd ? path.normalize(p) : path.join(cwd, p);
}

// Arquivos para onde o comando redireciona a saída (sem /dev/null e &1).
function logPaths(command, cwd) {
  const out = [];
  for (const m of (command || "").matchAll(REDIRECT)) {
    const p = m[1].replace(/^["']|["']$/g, "");
    if (p === "/dev/null" || p.startsWith("&")) continue;
    out.push(winPath(p, cwd));
  }
  return out;
}

const PROGRESS = /(?<![\d/.:])(\d{1,7})\s*\/\s*(\d{1,7})(?![\d/.:])|(\d{1,3}(?:[.,]\d+)?)\s*%/g;

// Último progresso escrito: "N/M" (N <= M) ou "NN%"; {pct, text} ou null.
function progress(lines) {
  for (const line of lines.slice(-600).reverse()) {
    for (const m of [...line.matchAll(PROGRESS)].reverse()) {
      if (m[3]) {
        const pct = parseFloat(m[3].replace(",", "."));
        if (pct <= 100) return { pct, text: line.trim().slice(0, 160) };
      } else {
        const n = parseInt(m[1], 10), total = parseInt(m[2], 10);
        if (total > 0 && n <= total) return { pct: Math.round((1000 * n) / total) / 10, text: line.trim().slice(0, 160) };
      }
    }
  }
  return null;
}

// Palavras de um trecho de shell, com aspas simples e duplas; null se as aspas não fecham.
function shellWords(s) {
  const out = [];
  let cur = null, q = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (q) {
      if (ch === q) q = null;
      else if (ch === "\\" && q === '"' && i + 1 < s.length) cur += s[++i];
      else cur += ch;
    } else if (ch === "'" || ch === '"') { q = ch; cur = cur || ""; }
    else if (ch === "\\" && i + 1 < s.length) { cur = (cur || "") + s[++i]; }
    else if (/\s/.test(ch)) { if (cur !== null) out.push(cur); cur = null; }
    else cur = (cur || "") + ch;
  }
  if (q) return null;
  if (cur !== null) out.push(cur);
  return out;
}

// [arquivo, url] de cada curl -o / wget -O do comando, com o diretório dos cd anteriores.
function downloads(command, cwd) {
  const out = [];
  for (const seg of (command || "").split(/&&|\|\||;|\n/)) {
    const tok = shellWords(seg.trim());
    if (!tok || !tok.length) continue;
    if (tok[0] === "cd" && tok.length > 1) cwd = winPath(tok[1], cwd);
    else if (tok[0] === "curl" || tok[0] === "wget") {
      const flag = tok[0] === "curl" ? ["-o", "--output"] : ["-O", "--output-document"];
      const i = tok.slice(0, -1).findIndex((t) => flag.includes(t));
      const dest = i >= 0 ? tok[i + 1] : null;
      const url = tok.find((t) => /^https?:\/\//.test(t));
      if (dest && url && dest !== "/dev/null") out.push([winPath(dest, cwd), url]);
    }
  }
  return out;
}

// Tamanho anunciado pelo servidor (HEAD, uma vez por URL); null se não diz. Assíncrono: a primeira consulta só dispara
// o HEAD, a resposta vale nas próximas.
const sizes = new Map();
function remoteSize(url) {
  if (!sizes.has(url)) {
    sizes.set(url, null);
    fetch(url, { method: "HEAD", headers: { "User-Agent": "bg-dashboard" }, signal: AbortSignal.timeout(10000) })
      .then((r) => sizes.set(url, parseInt(r.headers.get("content-length") || "0", 10) || null)).catch(() => {});
  }
  return sizes.get(url);
}

// Progresso do download em andamento: tamanho do arquivo no disco sobre o tamanho no servidor.
function downloadProgress(command, cwd) {
  for (const [dest, url] of downloads(command, cwd)) {
    let have;
    try { have = fs.statSync(dest).size; } catch { continue; }
    const total = remoteSize(url);
    if (total) {
      return { pct: Math.round(Math.min(100, (100 * have) / total) * 10) / 10,
        text: `download ${path.basename(dest)}: ${(have / 1e6).toFixed(0)} de ${(total / 1e6).toFixed(0)} MB` };
    }
  }
  return null;
}

// Windows: true se algum processo mantém o arquivo aberto (a tarefa de fundo escrevendo nele), false se ninguém.
// null fora do Windows ou se não deu para saber. UV_FS_O_EXLOCK (0x10000000 na libuv do Windows; o Node não exporta a
// constante) abre sem compartilhar: dá EBUSY se alguém tem aberto.
// ponytail: só Windows; no Linux/macOS daria para varrer /proc/*/fd ou usar lsof, se fizer falta.
const EXLOCK = fs.constants.UV_FS_O_EXLOCK || 0x10000000;
function heldOpen(file) {
  if (process.platform !== "win32") return null;
  try {
    fs.closeSync(fs.openSync(file, fs.constants.O_RDONLY | EXLOCK));
    return false;
  } catch (e) {
    return e.code === "EBUSY" ? true : null;
  }
}

// Timestamp ISO do histórico ("2026-10-20T12:00:00.123Z") em segundos; null se vazio ou inválido.
function epoch(ts) {
  const t = ts ? Date.parse(ts) : NaN;
  return Number.isNaN(t) ? null : t / 1000;
}

function snapshot(days) {
  const rows = [], t = now();
  for (const file of sessionFiles()) {
    let mtime;
    try { mtime = fs.statSync(file).mtimeMs / 1000; } catch { continue; }
    if (t - mtime > days * 86400) continue;
    const c = cache.get(file);
    if (!c || c.mtime !== mtime) {
      try { cache.set(file, { mtime, st: parseSession(file, c && c.st) }); } catch { continue; }
    }
    const { title, cwd, tasks } = cache.get(file).st;
    const session = stem(file), proj = path.basename(path.dirname(file));
    for (const [tid, task] of Object.entries(tasks)) {
      let out = path.join(TASKS, proj, session, "tasks", `${tid}.output`);
      if (!fs.existsSync(out)) {   // sessão retomada: a saída fica na pasta do id do processo que lançou a tarefa
        let dirs = [];
        try { dirs = fs.readdirSync(path.join(TASKS, proj)); } catch {}
        const found = dirs.map((s) => path.join(TASKS, proj, s, "tasks", `${tid}.output`)).find((p) => fs.existsSync(p));
        if (found) out = found;
      }
      const tail = tailOf(out);
      let age = tail.age;
      const outAge = tail.age;
      const last = tail.lines.length ? tail.lines[tail.lines.length - 1].slice(0, 200) : "";
      // Log para onde o comando manda a saída (> arquivo, >> arquivo, tee arquivo): ali está o progresso.
      let log = "", logLines = [], logAge = null;
      for (const lp of logPaths(task.command, cwd)) {
        const l = tailOf(lp);
        if (l.lines.length && (logAge == null || l.age < logAge)) { log = lp; logLines = l.lines; logAge = l.age; }
      }
      if (logAge != null) age = age != null ? Math.min(age, logAge) : logAge;
      let status = task.status, ended = epoch(task.ended);
      const end = /^\[(killed|exited with code (\d+))\]/.exec(last);
      if (status === "rodando" && end) {
        status = end[1] === "killed" ? "killed" : end[2] === "0" ? "completed" : "failed";
        ended = t - (age || 0);   // a última escrita na saída foi o fim
      } else if (status === "rodando" && task.tool === "Bash" && outAge != null && outAge > 30 && heldOpen(out) === false) {
        // Ninguém escreve mais na saída: o processo morreu sem o marcador de fim (o Claude Code que o lançou fechou).
        status = "stopped";
        ended = t - outAge;
      } else if (status === "rodando" && (age != null ? age : t - mtime) > 6 * 3600) {
        status = "sem notícia";   // nada escrito há horas e nenhum aviso de fim
      }
      rows.push({ id: tid, tool: task.tool, description: task.description, command: task.command.slice(0, 2000),
        started: task.started, ended, status, summary: task.summary, last, size: tail.size,
        log, log_tail: logLines.slice(-4).map((l) => l.slice(0, 200)),
        progress: progress(logLines.length ? logLines : tail.lines) || (status === "rodando" ? downloadProgress(task.command, cwd) : null),
        age, session, title, project: projectOf(cwd, file) });
    }
  }
  const running = rows.filter((r) => r.status === "rodando").sort((a, b) => (a.started < b.started ? -1 : a.started > b.started ? 1 : 0));
  const rest = rows.filter((r) => r.status !== "rodando").sort((a, b) => (a.started < b.started ? 1 : a.started > b.started ? -1 : 0));
  return [...running, ...rest];
}

// Páginas: media/pages. head.html (idioma e "dentro da aba") entra no <head> das duas abas.
const PAGES = path.join(__dirname, "pages");
const page = (name) => fs.readFileSync(path.join(PAGES, name), "utf8");
const HEAD = page("head.html");
const STATUS_PAGE = page("status.html").replace("</head>", HEAD + "</head>");
const USAGE_PAGE = page("usage.html").replace("</head>", HEAD + "</head>");
const TABS_PAGE = page("tabs.html");

const BOOT = now();
let VERSION = "";   // a extensão compara com a sua: um servidor mais antigo (de outra janela) é trocado
try { VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8")).version; } catch {}

function serve(port, days, usageDays) {
  const server = http.createServer((req, res) => {
    let body, kind = "text/html; charset=utf-8";
    try {
      if (req.url.startsWith("/api/usage")) { body = JSON.stringify(report); kind = "application/json"; }
      else if (req.url.startsWith("/api/claim")) {
        body = JSON.stringify({ first: claim(new URL(req.url, "http://x").searchParams.get("key")) });
        kind = "application/json";
      }
      else if (req.url.startsWith("/api")) {
        const rows = snapshot(days);
        body = JSON.stringify({ tasks: rows, agents: agents(rows), usage: usage(), usage_error: cliError, now: now(), boot: BOOT,
          version: VERSION, pid: process.pid });
        kind = "application/json";
      } else if (req.url.startsWith("/usage")) body = USAGE_PAGE;
      else if (req.url.startsWith("/status")) body = STATUS_PAGE;
      else body = TABS_PAGE;
    } catch (e) {
      res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      return res.end(String(e && e.stack || e));
    }
    res.writeHead(200, { "Content-Type": kind, "Cache-Control": "no-store" });
    res.end(body);
  });
  server.listen(port, "127.0.0.1", () => console.log(`Painel em http://localhost:${port}  (Ctrl+C para parar)`));   // ponytail: só localhost, sem login
  cliUsageLoop();
  usageLoop(usageDays);
}

function selftest() {
  const assert = require("assert");
  const vm = require("vm");
  const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "ai-status-"));
  const jsonl = (rows) => rows.map((r) => JSON.stringify(r) + "\n").join("");
  const p = path.join(tmp(), "s.jsonl");
  fs.writeFileSync(p, jsonl([
    { type: "user", cwd: "C:\\x\\freedom", message: { role: "user", content: "faça as quadras" } },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Bash", input: { command: "python a.py", description: "Gera", run_in_background: true } }] } },
    { type: "user", timestamp: "2026-10-18T10:00:00Z", message: { content: [{ type: "tool_result", tool_use_id: "t1", content: "Command running in background with ID: b123abcde. Output" }] } },
    { type: "user", timestamp: "2026-10-18T10:05:00Z", message: { content: "<task-notification>\n<task-id>b123abcde</task-id>\n<status>completed</status>\n<summary>ok</summary>" } },
    { type: "api-request-blob", message: { content: "<task-notification>\n<task-id>b123abcde</task-id>\n<status>completed</status>" } },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "t2", name: "Bash", input: { command: "x", description: "B", run_in_background: true } }] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t2", content: "running in background with ID: c999zzzzz." }] } },
    { type: "user", message: { content: "<task-notification><task-id>a000yyyyy</task-id><task-id>c999zzzzz</task-id><status>stopped</status>" } },
    // Agent assíncrono (o padrão): a linha da chamada não tem run_in_background nem agentId
    { type: "assistant", message: { content: [{ type: "tool_use", id: "t3", name: "Agent", input: { description: "Revisa", prompt: "revise", subagent_type: "general-purpose" } }] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t3", content: [{ type: "text", text: "Async agent launched successfully.\nagentId: ab4d307c28cf1c0d3 (internal ID)" }] }] } },
  ]));
  const st = parseSession(p);
  const { title, cwd, tasks } = st;
  assert.ok(title === "faça as quadras" && cwd.endsWith("freedom"), JSON.stringify([title, cwd]));
  assert.ok(tasks.b123abcde.command === "python a.py" && tasks.b123abcde.status === "completed", JSON.stringify(tasks));
  assert.strictEqual(tasks.c999zzzzz.status, "stopped");   // várias tarefas num aviso só
  assert.ok(tasks.ab4d307c28cf1c0d3.status === "rodando" && tasks.ab4d307c28cf1c0d3.tool === "Agent");
  assert.ok(epoch(tasks.b123abcde.ended) === 1792317900 && epoch("") === null);   // a repetição sem hora não apaga
  if (process.platform === "win32") {   // saída de tarefa viva: aberta pelo processo; morta: ninguém segura
    const fd = fs.openSync(p, "a");
    assert.strictEqual(heldOpen(p), true);
    fs.closeSync(fd);
    assert.strictEqual(heldOpen(p), false);
  }
  assert.strictEqual(progress(["ladrilho 18/93 (40.73, -74.00): 44 quadras", "Solow: 3 partes"]).pct, 19.4);
  assert.strictEqual(progress(["Uploading LFS objects: 100% (24/24)"]).pct, 100);
  assert.strictEqual(progress(["data 2026/10/18 12:30"]), null);
  const slash = (x) => x.replace(/\\/g, "/");
  assert.deepStrictEqual(logPaths("python -u a.py > /c/Users/x/log.txt 2>&1; tail -5 /c/Users/x/log.txt", "C:/p").map(slash), ["C:/Users/x/log.txt"]);
  assert.deepStrictEqual(downloads("mkdir -p /e/B && cd /e/B && curl -sSfL -o b.zip https://x.org/b.zip && unzip -q b.zip", "C:/p").map(([d, u]) => [slash(d), u]),
    [["E:/B/b.zip", "https://x.org/b.zip"]]);
  // O JS das páginas quebrado deixa o painel em branco sem erro nenhum aqui.
  const scripts = (html) => [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  for (const html of [STATUS_PAGE, USAGE_PAGE, TABS_PAGE]) for (const s of scripts(html)) new vm.Script(s);
  // Processo corrente: a ferramenta sem resultado é a que roda; a anterior, já respondida, não.
  const a = path.join(tmp(), "a.jsonl");
  fs.writeFileSync(a, jsonl([
    { type: "user", message: { content: "rode os testes" } },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "u1", name: "Read", input: { file_path: "x" } }] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "u1", content: "ok" }] } },
    { type: "assistant", timestamp: "2026-10-20T10:00:00Z", message: { content: [{ type: "tool_use", id: "u2", name: "Bash", input: { command: "pytest", description: "Testes" } }] } },
  ]));
  assert.deepStrictEqual(agentState(a).current.map((c) => [c.tool, c.description]), [["Bash", "Testes"]]);
  // Uso: a mesma mensagem em duas linhas (um bloco por linha) conta uma vez; a ferramenta conta uma vez.
  const line = { type: "assistant", timestamp: "2026-10-20T12:00:00Z", requestId: "r1", cwd: "C:\\x\\freedom",
    message: { id: "m1", model: "claude-x", content: [{ type: "tool_use", id: "z1", name: "Bash" }],
      usage: { input_tokens: 10, output_tokens: 100, cache_creation_input_tokens: 1000, cache_read_input_tokens: 10000,
        cache_creation: { ephemeral_1h_input_tokens: 400 } } } };
  const other = { ...line, message: { ...line.message, content: [{ type: "text", text: "x" }] } };
  fs.writeFileSync(a, JSON.stringify(line) + "\n" + JSON.stringify(other) + "\n" + JSON.stringify(line));
  scanUsage(a);   // a terceira linha não tem \n: fica para a próxima leitura
  const key = [...buckets.keys()].find((k) => k.split("\0")[2] === "claude-x");
  assert.strictEqual(key.split("\0")[1], "freedom");
  assert.deepStrictEqual(buckets.get(key), [10, 100, 600, 400, 10000]);
  assert.strictEqual([...tools].filter(([k]) => k.endsWith("\0Bash")).reduce((s, [, n]) => s + n, 0), 1);
  prices = { "claude-x": { input_cost_per_token: 1, output_cost_per_token: 2, cache_creation_input_token_cost: 3,
    cache_creation_input_token_cost_above_1hr: 4, cache_read_input_token_cost: 0.5 } };
  assert.deepStrictEqual(costs("claude-x-20260101", false, buckets.get(key)), [10, 200, 600 * 3 + 400 * 4, 5000]);
  prices = {};
  // Previsão: 40% -> 50% em 30 min = 20%/h; de 50% faltam 50 pontos = 2,5 h, antes do reinício em 4 h.
  const T0 = 1_800_000_000, R = T0 + 4 * 3600;
  assert.strictEqual(forecast("five_hour", { pct: 40, resets_at: R }, T0).forecast, null);   // uma leitura só: sem ritmo
  const f = forecast("five_hour", { pct: 50, resets_at: R }, T0 + 1800).forecast;
  assert.ok(Math.abs(f.per_hour - 20) < 1e-9 && f.hits_at === T0 + 1800 + 2.5 * 3600, JSON.stringify(f));
  assert.strictEqual(forecast("five_hour", { pct: 51, resets_at: T0 + 1900 + 600 }, T0 + 1900).forecast, null);   // reiniciou: recomeça
  forecast("seven_day", { pct: 10, resets_at: T0 + 3600 }, T0);
  assert.strictEqual(forecast("seven_day", { pct: 11, resets_at: T0 + 3600 }, T0 + 1200).forecast.hits_at, null);   // não chega antes de reiniciar
  assert.ok(claim("a") && !claim("a") && claim("b"));   // o aviso sai em uma janela só
  const w = cliWindow({ utilization: 77, resets_at: "2026-10-19T13:20:00.052350+00:00" });
  assert.ok(w.pct === 77 && Math.abs(w.resets_at - 1792416000.05235) < 1, JSON.stringify(w));
  console.log("BG_DASHBOARD_OK");
}

if (require.main === module) {
  const arg = (name, def) => { const i = process.argv.indexOf(name); return i > 0 ? Number(process.argv[i + 1]) : def; };
  if (process.argv.includes("--selftest")) selftest();
  else serve(arg("--port", 8765), arg("--days", 3), arg("--usage-days", 30));
}
