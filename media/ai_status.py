#!/usr/bin/env python3
"""AI Status: os comandos em segundo plano do Claude Code (com o progresso) e as sessões (agentes) e o que cada uma faz.

Lê os históricos (~/.claude/projects/<projeto>/<sessão>.jsonl): o comando lançado com run_in_background, o ID que a
ferramenta devolveu e a notificação de fim (<task-notification> com o status). A saída de cada um fica em
%TEMP%/claude/<projeto>/<sessão>/tasks/<id>.output. Só stdlib; só lê, não mexe em nada.

Uso:  python ai_status.py [--port 8765] [--days 3]   e abra http://localhost:8765
      python ai_status.py --selftest
"""

import argparse
import html
import http.server
import json
import os
import pathlib
import re
import shlex
import tempfile
import time
import urllib.request

# Pasta de configuração do Claude Code (CLAUDE_CONFIG_DIR quando definida, senão ~/.claude).
PROJECTS = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude") / "projects"
TASKS = pathlib.Path(tempfile.gettempdir()) / "claude"
CONFIG = PROJECTS.parent
USAGE = CONFIG / "ai-status-usage.json"   # gravado por media/statusline.py (statusLine do Claude Code)
LAUNCH = re.compile(r"(?:running in background with ID: |agentId: )([A-Za-z0-9]{6,})")
HANDBACK = re.compile(r'<agent-message from="([^"]+)">\s*\[Subagent hand-back\]')
NOTE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([^<]+)</status>(?:.*?<summary>([^<]*)</summary>)?", re.S)
_cache = {}  # caminho -> (mtime, estado incremental de parse_session)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return ""


def parse_session(path, state=None):
    """Tarefas lançadas numa sessão: id -> dados (comando, descrição, início, status). Incremental: com o state da
    chamada anterior, lê só o que foi escrito depois (o histórico da sessão passa de centenas de MB)."""
    st = state or {"offset": 0, "calls": {}, "tasks": {}, "title": "", "cwd": ""}
    calls, tasks, title, cwd = st["calls"], st["tasks"], st["title"], st["cwd"]
    with open(path, "rb") as f:
        f.seek(st["offset"])
        data = f.read()
    end = data.rfind(b"\n") + 1   # linha pela metade fica para a próxima
    st["offset"] += end
    for raw_line in data[:end].splitlines():
        line = raw_line.decode("utf-8", "replace")
        if "run_in_background" not in line and "task-id" not in line and "background with ID" not in line \
                and "agentId" not in line and "agent-message" not in line and (title and cwd):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        cwd = cwd or d.get("cwd", "")
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "user" and not title and isinstance(content, str) and not content.startswith("<"):
            title = content.strip().splitlines()[0][:90]
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    if inp.get("run_in_background") or c.get("name") == "Agent":
                        calls[c["id"]] = {"tool": c.get("name"), "description": inp.get("description", ""),
                                          "command": inp.get("command") or inp.get("prompt", "")}
                elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    result = text_of(c.get("content"))
                    m = LAUNCH.search(result)
                    if m:
                        # Subagente sem segundo plano: o resultado já é o fim (o relatório).
                        done = calls[c["tool_use_id"]]["tool"] == "Agent" and "launched" not in result.lower()
                        tasks[m.group(1)] = dict(calls[c["tool_use_id"]], started=d.get("timestamp", ""),
                                                 status="completed" if done else "rodando", summary="")
        raw = d.get("content") if isinstance(d.get("content"), str) else ""
        body = text_of(content) + "\n" + raw
        # Um aviso pode trazer várias tarefas (as que morreram com a sessão vêm juntas): todos os <task-id> antes do <status>.
        for chunk in body.split("<task-notification>")[1:]:
            m = NOTE.search(chunk)
            if not m:
                continue
            ids = re.findall(r"<task-id>([^<]+)</task-id>", chunk[:m.end(2)])
            for tid in ids:
                if tid in tasks:
                    tasks[tid]["status"], tasks[tid]["summary"] = m.group(2), (m.group(3) or "").strip()
        for tid in HANDBACK.findall(body):
            if tid in tasks:
                tasks[tid]["status"] = "completed"
    st["title"], st["cwd"] = title, cwd
    return st


def agent_state(path):
    """O que a sessão está fazendo, pelo fim do histórico: modelo, última ação (ferramenta ou resposta) e último pedido."""
    lines, _, age = tail_of(path, 256000)
    model, action, kind, prompt, when = "", "", "", "", ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if not when and d.get("timestamp"):
            when = d["timestamp"]
        if d.get("type") == "assistant" and not kind:
            model = msg.get("model", "")
            parts = content if isinstance(content, list) else []
            tool = next((c for c in reversed(parts) if isinstance(c, dict) and c.get("type") == "tool_use"), None)
            text = next((c.get("text", "") for c in reversed(parts) if isinstance(c, dict) and c.get("type") == "text"), "")
            if tool:
                inp = tool.get("input") or {}
                kind, action = "tool", f'{tool.get("name")}: {inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("query") or ""}'
            elif text.strip():
                kind, action = "text", text.strip().splitlines()[0]
        elif d.get("type") == "user" and not prompt and isinstance(content, str) and not content.startswith("<"):
            prompt = content.strip().splitlines()[0] if content.strip() else ""
        if kind and prompt:
            break
    return {"model": model, "action": action[:180], "kind": kind, "prompt": prompt[:140], "age": age, "last": when}


def usage():
    """Limites do plano (5 h e 7 d): percentual usado e quando reinicia. Fonte: o statusline.py (o que o Claude Code
    passa à linha de status); sem ele, o cache de outra extensão (vscode-claude-status), avisando a idade."""
    def window(w, pct_key, reset_key, scale=1.0):
        if not w or w.get(pct_key) is None:
            return None
        return {"pct": float(w[pct_key]) * scale, "resets_at": w.get(reset_key)}
    try:
        d = json.loads(USAGE.read_text(encoding="utf-8"))
        rl = d.get("rate_limits") or {}
        return {"source": "statusline", "updated": d.get("updated"),
                "five_hour": window(rl.get("five_hour"), "used_percentage", "resets_at"),
                "seven_day": window(rl.get("seven_day"), "used_percentage", "resets_at")}
    except (OSError, ValueError):
        pass
    try:
        d = json.loads((CONFIG / "vscode-claude-status-cache.json").read_text(encoding="utf-8"))
        u = d.get("usageData") or {}
        updated = time.mktime(time.strptime(d["updatedAt"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if d.get("updatedAt") else None
        return {"source": "vscode-claude-status", "updated": updated,
                "five_hour": window({"p": u.get("utilization5h"), "r": u.get("reset5hAt")}, "p", "r", 100.0),
                "seven_day": window({"p": u.get("utilization7d"), "r": u.get("reset7dAt")}, "p", "r", 100.0)}
    except (OSError, ValueError, KeyError):
        return None


def agents(rows, days_hours=24):
    """Sessões do Claude Code com atividade nas últimas horas, com o estado e as tarefas rodando de cada uma."""
    out, now = [], time.time()
    for path in PROJECTS.glob("*/*.jsonl"):
        mtime = path.stat().st_mtime
        if now - mtime > days_hours * 3600 or path not in _cache:
            continue
        st = _cache[path][1]
        a = agent_state(path)
        age = a["age"] or 0
        status = ("trabalhando" if a["kind"] == "tool" else "aguardando você") if age < 600 else "parado"
        if a["kind"] == "tool" and age >= 120:
            status = "esperando ferramenta" if age < 600 else "parado"
        running = [r for r in rows if r["session"] == path.stem and r["status"] == "rodando"]
        out.append(dict(a, session=path.stem, project=os.path.basename(st["cwd"].rstrip("\\/")) or path.parent.name,
                        title=st["title"], status=status, running=len(running),
                        subagents=[r["description"] for r in running if r["tool"] == "Agent"]))
    out.sort(key=lambda r: r["age"] or 0)
    return out


def tail_of(path, nbytes=64000):
    """Linhas não vazias do fim do arquivo, tamanho e idade (s) da última escrita; ([], 0, None) se não existe."""
    try:
        st = path.stat()
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - nbytes))
            raw = f.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:   # saída do Python no console do Windows (cp1252)
            text = raw.decode("cp1252", "replace")
    except OSError:
        return [], 0, None
    return [l for l in text.replace("\r", "\n").splitlines() if l.strip()], st.st_size, time.time() - st.st_mtime


REDIRECT = re.compile(r"(?:\d?>>?|\btee(?:\s+-a)?)\s*(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")


def log_paths(command, cwd):
    """Arquivos para onde o comando redireciona a saída (sem /dev/null e &1); caminho do Git Bash vira do Windows."""
    paths = []
    for m in REDIRECT.finditer(command or ""):
        p = m.group(1).strip("\"'")
        if p in ("/dev/null", "&1", "&2") or p.startswith("&"):
            continue
        p = re.sub(r"^/([a-zA-Z])/", lambda m: m.group(1).upper() + ":/", p)
        q = pathlib.Path(p)
        paths.append(q if q.is_absolute() or not cwd else pathlib.Path(cwd) / q)
    return paths


PROGRESS = re.compile(r"(?<![\d/.:])(\d{1,7})\s*/\s*(\d{1,7})(?![\d/.:])|(\d{1,3}(?:[.,]\d+)?)\s*%")


def progress(lines):
    """Último progresso escrito: "N/M" (N <= M) ou "NN%"; {pct, text} ou None."""
    for line in reversed(lines[-600:]):
        for m in reversed(list(PROGRESS.finditer(line))):
            if m.group(3):
                pct = float(m.group(3).replace(",", "."))
                if pct <= 100:
                    return {"pct": pct, "text": line.strip()[:160]}
            else:
                n, total = int(m.group(1)), int(m.group(2))
                if 0 < total and n <= total:
                    return {"pct": round(100.0 * n / total, 1), "text": line.strip()[:160]}
    return None


def win_path(p, cwd):
    p = re.sub(r"^/([a-zA-Z])/", lambda m: m.group(1).upper() + ":/", p.strip("\"'"))
    q = pathlib.Path(p)
    return q if q.is_absolute() or not cwd else pathlib.Path(cwd) / q


def downloads(command, cwd):
    """(arquivo, url) de cada curl -o / wget -O do comando, com o diretório dos cd anteriores."""
    out = []
    for seg in re.split(r"&&|\|\||;|\n", command or ""):
        try:
            tok = shlex.split(seg.strip())
        except ValueError:
            continue
        if not tok:
            continue
        if tok[0] == "cd" and len(tok) > 1:
            cwd = str(win_path(tok[1], cwd))
        elif tok[0] in ("curl", "wget"):
            flag = ("-o", "--output") if tok[0] == "curl" else ("-O", "--output-document")
            dest = next((tok[i + 1] for i, t in enumerate(tok[:-1]) if t in flag), None)
            url = next((t for t in tok if t.startswith(("http://", "https://"))), None)
            if dest and url and dest != "/dev/null":
                out.append((win_path(dest, cwd), url))
    return out


_sizes = {}


def remote_size(url):
    """Tamanho anunciado pelo servidor (HEAD, uma vez por URL); None se não diz."""
    if url not in _sizes:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "bg-dashboard"})
            _sizes[url] = int(urllib.request.urlopen(req, timeout=10).headers.get("Content-Length") or 0) or None
        except Exception:
            _sizes[url] = None
    return _sizes[url]


def download_progress(command, cwd):
    """Progresso do download em andamento: tamanho do arquivo no disco sobre o tamanho no servidor."""
    for dest, url in downloads(command, cwd):
        try:
            have = dest.stat().st_size
        except OSError:
            continue
        total = remote_size(url)
        if total:
            return {"pct": round(min(100.0, 100.0 * have / total), 1),
                    "text": f"download {dest.name}: {have / 1e6:.0f} de {total / 1e6:.0f} MB"}
    return None


def snapshot(days):
    rows, now = [], time.time()
    for path in PROJECTS.glob("*/*.jsonl"):
        mtime = path.stat().st_mtime
        if now - mtime > days * 86400:
            continue
        if _cache.get(path, (None,))[0] != mtime:
            _cache[path] = (mtime, parse_session(path, _cache[path][1] if path in _cache else None))
        st = _cache[path][1]
        title, cwd, tasks = st["title"], st["cwd"], st["tasks"]
        session = path.stem
        for tid, t in tasks.items():
            out = TASKS / path.parent.name / session / "tasks" / f"{tid}.output"
            tail, size, age = tail_of(out)
            last = tail[-1][:200] if tail else ""
            # Log para onde o comando manda a saída (> arquivo, >> arquivo, tee arquivo): ali está o progresso.
            log, log_lines, log_age = "", [], None
            for lp in log_paths(t["command"], cwd):
                lines, _, a = tail_of(lp)
                if lines and (log_age is None or a < log_age):
                    log, log_lines, log_age = str(lp), lines, a
            if log_age is not None:
                age = min(age, log_age) if age is not None else log_age
            status = t["status"]
            end = re.match(r"\[(killed|exited with code (\d+))\]", last)
            if status == "rodando" and end:
                status = "killed" if end.group(1) == "killed" else ("completed" if end.group(2) == "0" else "failed")
            elif status == "rodando" and (age if age is not None else now - mtime) > 6 * 3600:
                status = "sem notícia"   # nada escrito há horas e nenhum aviso de fim
            rows.append({"id": tid, "tool": t["tool"], "description": t["description"], "command": t["command"][:2000],
                         "started": t["started"], "status": status, "summary": t["summary"], "last": last, "size": size,
                         "log": log, "log_tail": [l[:200] for l in log_lines[-4:]], "progress": progress(log_lines or tail) or (download_progress(t["command"], cwd) if status == "rodando" else None),
                         "age": age, "session": session, "title": title,
                         "project": os.path.basename(cwd.rstrip("\\/")) or path.parent.name})
    order = {"rodando": 0, "sem notícia": 1}
    rows.sort(key=lambda r: (order.get(r["status"], 2), r["started"]), reverse=False)
    running = [r for r in rows if r["status"] == "rodando"]
    rest = sorted((r for r in rows if r["status"] != "rodando"), key=lambda r: r["started"], reverse=True)
    return running + rest


PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Status</title><style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e3e2dd;--run:#1f7a4d;--done:#5b5b57;--fail:#b3261e;--warn:#9a6a00;--wait:#2f5fb3}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--mut:#9a9993;--line:#33322f;--run:#4fbf87;--done:#a3a29c;--fail:#ff8a80;--warn:#e0b44a;--wait:#8ab4ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:14px 20px;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}h1{font-size:18px;margin:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px;font-weight:600}
.mut{color:var(--mut)}label{margin-left:auto}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0 20px 24px;align-items:start}
@media (max-width:760px){main{grid-template-columns:1fr}}
section{min-width:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 10px;min-width:0}
.top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.st{font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.rodando,.trabalhando{color:var(--run)}.completed{color:var(--done)}.failed,.killed{color:var(--fail)}.stopped,.sem,.parado,.esperando{color:var(--warn)}.aguardando{color:var(--wait)}
.desc{font-weight:600;overflow-wrap:anywhere}.agent{font-size:12px}code,pre{font:12px/1.4 ui-monospace,Consolas,monospace}
pre{white-space:pre-wrap;word-break:break-all;margin:6px 0 0;color:var(--mut)}details summary{cursor:pointer;color:var(--mut);font-size:12px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--run)}
.pct{font-weight:600;font-variant-numeric:tabular-nums}.log{font-size:12px;margin-top:6px;overflow-wrap:anywhere}
.last{margin-top:6px;padding:6px 8px;background:var(--bg);border-radius:6px;overflow-x:auto;white-space:pre}
.now{margin-top:6px;padding:6px 8px;background:var(--bg);border-radius:6px;font-size:13px;overflow-wrap:anywhere}
.usage{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0 20px 14px}@media (max-width:760px){.usage{grid-template-columns:1fr}}
.meter{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.meter .row{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.meter b{font-size:20px;font-variant-numeric:tabular-nums}.meter .bar{height:10px}
.hi .bar i{background:var(--warn)}.max .bar i{background:var(--fail)}.stale{font-size:12px;color:var(--warn);padding:0 20px 10px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
</style></head><body><header><h1>AI Status</h1><span class="mut" id="sum"></span>
<label class="mut"><input type="checkbox" id="all"> mostrar terminadas</label></header>
<div class="usage" id="usage"></div><div class="stale" id="stale"></div>
<main><section><h2>Tarefas</h2><div id="list"></div></section><section><h2>Agentes</h2><div id="agents"></div></section></main>
<script>
const esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ago=s=>s==null?'':s<90?Math.round(s)+' s':s<5400?Math.round(s/60)+' min':(s/3600).toFixed(1)+' h';
const NL=String.fromCharCode(10);
function task(r,open){return `
  <div class="card"><div class="top"><span class="st ${esc(r.status.split(' ')[0])}">${esc(r.status)}</span>
  <span class="desc">${esc(r.description||r.tool)}</span><code class="mut">${esc(r.id)}</code></div>
  <div class="agent mut">${esc(r.project)} · sessão <code>${esc(r.session.slice(0,8))}</code> · início ${esc((r.started||'').replace('T',' ').slice(0,19))} UTC${r.age!=null?' · escrito há '+ago(r.age):''}</div>
  ${r.summary?`<div class="mut">${esc(r.summary)}</div>`:''}
  ${r.progress?`<div class="bar"><i style="width:${Math.min(100,r.progress.pct)}%"></i></div>
  <div class="log"><span class="pct">${r.progress.pct.toFixed(1)}%</span> <span class="mut">${esc(r.progress.text)}</span></div>`:''}
  ${r.log_tail.length?`<div class="last"><code>${r.log_tail.map(esc).join(NL)}</code></div><div class="log mut">log: ${esc(r.log)}</div>`
    :(r.last?`<div class="last"><code>${esc(r.last)}</code></div>`:'')}
  <details data-id="${esc(r.id)}" ${open.has(r.id)?'open':''}><summary>comando (${esc(r.tool)})</summary><pre>${esc(r.command)}</pre></details></div>`}
function agent(a){return `
  <div class="card"><div class="top"><span class="st ${esc(a.status.split(' ')[0])}">${esc(a.status)}</span>
  <span class="desc">${esc(a.project)}</span><code class="mut">${esc(a.session.slice(0,8))}</code>
  <span class="mut">${esc(a.model)} · há ${ago(a.age)}</span></div>
  <div class="agent mut">${esc(a.title)}</div>
  ${a.action?`<div class="now">${a.kind==='tool'?'▶ ':'💬 '}${esc(a.action)}</div>`:''}
  ${a.prompt?`<div class="log mut">último pedido: ${esc(a.prompt)}</div>`:''}
  <div class="chips"><span class="chip">${a.running} tarefa(s) rodando</span>${a.subagents.map(s=>`<span class="chip">subagente: ${esc(s)}</span>`).join('')}</div></div>`}
function until(t,now){if(!t)return '';const s=t-now;if(s<=0)return 'reiniciando';
  const d=new Date(t*1000), hm=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  return 'reinicia '+(s<86400?'em '+ago(s)+' ('+hm+')':d.toLocaleDateString([], {weekday:'short',day:'2-digit'})+' '+hm)}
function meter(title,w,now){if(!w)return `<div class="meter"><div class="row"><span>${title}</span><span class="mut">sem dado</span></div></div>`;
  const cls=w.pct>=90?'max':w.pct>=70?'hi':'';
  return `<div class="meter ${cls}"><div class="row"><span>${title}</span><b>${w.pct.toFixed(0)}%</b></div>
  <div class="bar"><i style="width:${Math.min(100,w.pct)}%"></i></div><div class="log mut">${until(w.resets_at,now)}</div></div>`}
function usage(u,now){
  document.getElementById('usage').innerHTML=u?meter('Sessão (5 h)',u.five_hour,now)+meter('Semana (7 d)',u.seven_day,now)
    :'<div class="meter mut">Uso do plano indisponível: configure o statusline.py como linha de status do Claude Code.</div>';
  const age=u&&u.updated?now-u.updated:null;
  document.getElementById('stale').textContent=!u?'':u.source!=='statusline'?'Dado antigo de outra extensão ('+ago(age)+' atrás); configure o statusline.py para ficar em dia.'
    :age>900?'Atualizado há '+ago(age)+' (o Claude Code atualiza a cada resposta).':'';
}
async function tick(){
  let data;try{data=await (await fetch('api')).json()}catch(e){document.getElementById('sum').textContent='servidor parado';return}
  usage(data.usage,data.now);
  const rows=data.tasks, all=document.getElementById('all').checked, run=rows.filter(r=>r.status==='rodando');
  const busy=data.agents.filter(a=>a.status==='trabalhando').length;
  document.getElementById('sum').textContent=run.length+' tarefa(s) rodando · '+data.agents.length+' agente(s), '+busy+' trabalhando · '+new Date().toLocaleTimeString();
  const open=new Set([...document.querySelectorAll('details[open]')].map(d=>d.dataset.id));
  document.getElementById('list').innerHTML=(all?rows:rows.filter(r=>r.status==='rodando'||r.status==='sem notícia')).map(r=>task(r,open)).join('')
    ||'<p class="mut">Nada rodando agora.</p>';
  document.getElementById('agents').innerHTML=data.agents.map(agent).join('')||'<p class="mut">Nenhuma sessão ativa nas últimas 24 h.</p>';
}
document.getElementById('all').onchange=tick;tick();setInterval(tick,3000);
</script></body></html>"""


def serve(port, days):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api"):
                rows = snapshot(days)
                body, kind = json.dumps({"tasks": rows, "agents": agents(rows), "usage": usage(), "now": time.time()}).encode(), "application/json"
            else:
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"Painel em http://localhost:{port}  (Ctrl+C para parar)")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()  # ponytail: só localhost, sem login


def selftest():
    p = pathlib.Path(tempfile.mkdtemp()) / "s.jsonl"
    lines = [
        {"type": "user", "cwd": "C:\\x\\freedom", "message": {"role": "user", "content": "faça as quadras"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "python a.py", "description": "Gera", "run_in_background": True}}]}},
        {"type": "user", "timestamp": "2026-10-18T10:00:00Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                                   "content": "Command running in background with ID: b123abcde. Output"}]}},
        {"type": "user", "message": {"content": "<task-notification>\n<task-id>b123abcde</task-id>\n<status>completed</status>\n<summary>ok</summary>"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                       "input": {"command": "x", "description": "B", "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "running in background with ID: c999zzzzz."}]}},
        {"type": "user", "message": {"content": "<task-notification><task-id>a000yyyyy</task-id><task-id>c999zzzzz</task-id><status>stopped</status>"}},
    ]
    p.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    st = parse_session(p)
    title, cwd, tasks = st["title"], st["cwd"], st["tasks"]
    assert title == "faça as quadras" and cwd.endswith("freedom"), (title, cwd)
    assert tasks["b123abcde"]["command"] == "python a.py" and tasks["b123abcde"]["status"] == "completed", tasks
    assert tasks["c999zzzzz"]["status"] == "stopped", tasks   # várias tarefas num aviso só
    assert progress(["ladrilho 18/93 (40.73, -74.00): 44 quadras", "Solow: 3 partes"])["pct"] == 19.4
    assert progress(["Uploading LFS objects: 100% (24/24)"])["pct"] == 100.0
    assert progress(["data 2026/10/18 12:30"]) is None
    logs = log_paths('python -u a.py > /c/Users/x/log.txt 2>&1; tail -5 /c/Users/x/log.txt', "C:/p")
    assert [str(q).replace("\\", "/") for q in logs] == ["C:/Users/x/log.txt"], logs
    dl = downloads("mkdir -p /e/B && cd /e/B && curl -sSfL -o b.zip https://x.org/b.zip && unzip -q b.zip", "C:/p")
    assert [(str(d).replace("\\", "/"), u) for d, u in dl] == [("E:/B/b.zip", "https://x.org/b.zip")], dl
    import shutil, subprocess
    if shutil.which("node"):   # o JS da página quebrado deixa o painel em branco sem erro nenhum aqui
        js = pathlib.Path(tempfile.mkdtemp()) / "page.js"
        js.write_text(PAGE[PAGE.index("<script>") + 8:PAGE.index("</script>")], encoding="utf-8")
        assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0, "JS da página com erro de sintaxe"
    print("BG_DASHBOARD_OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else serve(a.port, a.days)
