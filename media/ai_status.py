#!/usr/bin/env python3
"""AI Status: os comandos em segundo plano do Claude Code (com o progresso) e as sessões (agentes) e o que cada uma faz.

Lê os históricos (~/.claude/projects/<projeto>/<sessão>.jsonl): o comando lançado com run_in_background, o ID que a
ferramenta devolveu e a notificação de fim (<task-notification> com o status). A saída de cada um fica em
%TEMP%/claude/<projeto>/<sessão>/tasks/<id>.output. Só stdlib; só lê, não mexe em nada.

Uso:  python ai_status.py [--port 8765] [--days 3]   e abra http://localhost:8765
      python ai_status.py --selftest
"""

import argparse
from datetime import datetime, timedelta
import html
import http.server
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import threading
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
                                                 status="completed" if done else "rodando", summary="",
                                                 ended=d.get("timestamp", "") if done else "")
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
                    tasks[tid]["ended"] = d.get("timestamp") or tasks[tid].get("ended", "")   # api-request-blob repete o aviso sem hora
        for tid in HANDBACK.findall(body):
            if tid in tasks:
                tasks[tid]["status"], tasks[tid]["ended"] = "completed", d.get("timestamp") or tasks[tid].get("ended", "")
    st["title"], st["cwd"] = title, cwd
    return st


def agent_state(path):
    """O que a sessão está fazendo, pelo fim do histórico: modelo, última ação (ferramenta ou resposta) e último pedido."""
    lines, _, age = tail_of(path, 256000)
    model, action, kind, prompt, when = "", "", "", "", ""
    done, current, settled = set(), [], False   # current: ferramentas chamadas que ainda não devolveram resultado
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
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_result":
                done.add(c.get("tool_use_id"))
            elif c.get("type") == "tool_use" and d.get("type") == "assistant" and not settled:
                inp = c.get("input") or {}
                if c.get("id") in done:
                    settled = True   # daqui para trás o turno já terminou
                elif not inp.get("run_in_background"):   # as de segundo plano já estão em tasks
                    current.append({"tool": c.get("name"), "started": d.get("timestamp", ""),
                                    "description": inp.get("description") or inp.get("file_path") or inp.get("query") or "",
                                    "command": (inp.get("command") or inp.get("prompt") or "")[:2000]})
        if d.get("type") == "user" and isinstance(content, str):
            settled = True   # um pedido novo: o que ficou sem resultado antes dele foi interrompido
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
        if kind and prompt and settled:
            break
    if age is not None and age > 6 * 3600:
        current = []   # nada escrito há horas: a sessão morreu com a ferramenta aberta
    return {"model": model, "action": action[:180], "kind": kind, "prompt": prompt[:140], "age": age, "last": when,
            "current": current[::-1]}


USAGE_EVERY = 300   # s entre consultas ao Claude Code
_cli_usage = {}


def cli_usage_once():
    """Pergunta os limites ao próprio Claude Code (o pedido get_usage, o mesmo do painel Usage da extensão oficial):
    nenhum prompt vai ao modelo (custo 0) e quem lê a credencial é o Claude Code, não este script."""
    exe = shutil.which("claude") or str(pathlib.Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
    args = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", ""]   # sem hooks, sem gravar sessão
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timer = threading.Timer(30, p.kill)
    timer.start()
    try:
        p.stdin.write(json.dumps({"type": "control_request", "request_id": "ai-status",
                                  "request": {"subtype": "get_usage"}}) + "\n")
        p.stdin.flush()
        for line in p.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") == "control_response":
                return (m.get("response") or {}).get("response") or {}
    finally:
        timer.cancel()
        p.kill()
    return {}


def cli_window(w):
    if not w or w.get("utilization") is None:
        return None
    reset = w.get("resets_at")
    if isinstance(reset, str):
        reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
    return {"pct": float(w["utilization"]), "resets_at": reset}


def cli_usage_loop():
    while True:
        try:
            rl = cli_usage_once().get("rate_limits") or {}
            if rl.get("five_hour") or rl.get("seven_day"):
                _cli_usage.update(source="claude", updated=time.time(),
                                  five_hour=cli_window(rl.get("five_hour")), seven_day=cli_window(rl.get("seven_day")))
        except (OSError, ValueError):
            pass   # Claude Code ausente ou deslogado: fica o último dado (ou o da linha de status)
        time.sleep(USAGE_EVERY)


def usage():
    """Limites do plano (5 h e 7 d): percentual usado e quando reinicia. Do Claude Code (cli_usage_loop) ou, sem ele,
    do statusline.py (o que o Claude Code de terminal passa à linha de status); o mais recente vence."""
    def window(w, pct_key, reset_key, scale=1.0):
        if not w or w.get(pct_key) is None:
            return None
        return {"pct": float(w[pct_key]) * scale, "resets_at": w.get(reset_key)}
    best = dict(_cli_usage) or None
    try:
        d = json.loads(USAGE.read_text(encoding="utf-8"))
        rl = d.get("rate_limits") or {}
        if not best or (d.get("updated") or 0) > best["updated"]:
            best = {"source": "statusline", "updated": d.get("updated"),
                    "five_hour": window(rl.get("five_hour"), "used_percentage", "resets_at"),
                    "seven_day": window(rl.get("seven_day"), "used_percentage", "resets_at")}
    except (OSError, ValueError):
        pass
    return best


# Uso em tokens e custo: todas as respostas do modelo nos históricos (sessões e subagentes), contadas uma vez só
# (o Claude Code grava a mesma mensagem numa linha por bloco, e a sessão retomada copia o histórico).
PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
KINDS = ("entrada", "saída", "gravação de cache", "leitura de cache")
_prices = {}
_seen = set()
_offsets = {}
_buckets = {}   # (dia, projeto, modelo, fast) -> [entrada, saída, cache 5 min, cache 1 h, leitura de cache]
_tools = {}     # (dia, projeto, ferramenta) -> chamadas
_report = {}   # "u": o último usage_report, trocado inteiro


def scan_usage(path):
    """Soma o que foi escrito no histórico desde a última leitura."""
    off = _offsets.get(path, 0)
    with open(path, "rb") as f:
        f.seek(off)
        for raw in f:
            if not raw.endswith(b"\n"):
                break   # linha pela metade fica para a próxima
            off += len(raw)
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") or {}
            u, model, ts = msg.get("usage"), msg.get("model") or "", d.get("timestamp")
            if d.get("type") != "assistant" or not u or not ts or model.startswith("<"):   # <synthetic>: sem chamada
                continue
            day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
            project = os.path.basename((d.get("cwd") or "").rstrip("\\/")) or path.parent.name
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id") not in _seen:
                    _seen.add(c.get("id"))
                    k = (day, project, c.get("name"))
                    _tools[k] = _tools.get(k, 0) + 1
            key = f'{msg.get("id")}:{d.get("requestId")}'
            if key in _seen:
                continue
            _seen.add(key)
            h1 = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
            row = (u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                   (u.get("cache_creation_input_tokens") or 0) - h1, h1, u.get("cache_read_input_tokens") or 0)
            b = _buckets.setdefault((day, project, model, u.get("speed") == "fast"), [0] * 5)
            for i, n in enumerate(row):
                b[i] += n
    _offsets[path] = off


def costs(model, fast, t):
    """US$ de cada tipo de token (KINDS) pela tabela do LiteLLM (preço de API); None se o modelo não está nela."""
    p = next((_prices[k] for k in (model, re.sub(r"-\d{8}$", "", model), "anthropic/" + model) if k in _prices), None)
    if not p:
        return None
    w = p.get("cache_creation_input_token_cost") or 0
    c = [t[0] * (p.get("input_cost_per_token") or 0), t[1] * (p.get("output_cost_per_token") or 0),
         t[2] * w + t[3] * (p.get("cache_creation_input_token_cost_above_1hr") or w),
         t[4] * (p.get("cache_read_input_token_cost") or 0)]
    f = (p.get("provider_specific_entry") or {}).get("fast", 1) if fast else 1
    return [x * f for x in c]


def usage_report(days):
    start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    groups = {"days": {}, "projects": {}, "models": {}}
    kinds = [[0, 0.0] for _ in KINDS]
    unpriced = set()
    for (day, project, model, fast), t in _buckets.items():
        if day < start:
            continue
        c = costs(model, fast, t)
        if c is None:
            unpriced.add(model)
            c = [0.0] * 4
        tok = (t[0], t[1], t[2] + t[3], t[4])
        for g, name in (("days", day), ("projects", project), ("models", model)):
            r = groups[g].setdefault(name, {"name": name, "cost": 0.0, "tokens": 0})
            r["cost"] += sum(c)
            r["tokens"] += sum(tok)
        for i in range(4):
            kinds[i][0] += tok[i]
            kinds[i][1] += c[i]
    tools = {}
    for (day, _, name), n in _tools.items():
        if day >= start:
            tools[name] = tools.get(name, 0) + n
    zero = {"cost": 0.0, "tokens": 0}
    every = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    top = lambda g: sorted(groups[g].values(), key=lambda r: (r["cost"], r["tokens"]), reverse=True)
    return {"period": days, "priced": bool(_prices), "unpriced": sorted(unpriced),
            "total": {"cost": sum(k[1] for k in kinds), "tokens": sum(k[0] for k in kinds)},
            "today": groups["days"].get(today, zero),
            "days": [groups["days"].get(d, dict(zero, name=d)) for d in every],
            "projects": top("projects"), "models": top("models"),
            "kinds": [{"name": n, "tokens": k[0], "cost": k[1]} for n, k in zip(KINDS, kinds)],
            "tools": [{"name": n, "count": c} for n, c in sorted(tools.items(), key=lambda x: -x[1])]}


def usage_loop(days):
    while True:
        if not _prices:
            try:
                _prices.update(json.load(urllib.request.urlopen(PRICES_URL, timeout=30)))
            except Exception:
                pass   # offline: só tokens, tenta de novo na próxima volta
        cutoff = time.time() - days * 86400
        for path in PROJECTS.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    scan_usage(path)
            except OSError:
                pass
        _report["u"] = dict(usage_report(days), updated=time.time())
        time.sleep(60)


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


def epoch(ts):
    """Timestamp ISO do histórico ("2026-10-20T12:00:00.123Z") em segundos; None se vazio ou inválido."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
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
            status, ended = t["status"], epoch(t.get("ended"))
            end = re.match(r"\[(killed|exited with code (\d+))\]", last)
            if status == "rodando" and end:
                status = "killed" if end.group(1) == "killed" else ("completed" if end.group(2) == "0" else "failed")
                ended = now - (age or 0)   # a última escrita na saída foi o fim
            elif status == "rodando" and (age if age is not None else now - mtime) > 6 * 3600:
                status = "sem notícia"   # nada escrito há horas e nenhum aviso de fim
            rows.append({"id": tid, "tool": t["tool"], "description": t["description"], "command": t["command"][:2000],
                         "started": t["started"], "ended": ended, "status": status, "summary": t["summary"], "last": last, "size": size,
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
function meter(title,w,now){if(w&&w.resets_at&&w.resets_at<now)w=null;   // janela já reiniciou: o número é de antes
  if(!w)return `<div class="meter"><div class="row"><span>${title}</span><span class="mut">sem dado</span></div></div>`;
  const cls=w.pct>=90?'max':w.pct>=70?'hi':'';
  return `<div class="meter ${cls}"><div class="row"><span>${title}</span><b>${w.pct.toFixed(0)}%</b></div>
  <div class="bar"><i style="width:${Math.min(100,w.pct)}%"></i></div><div class="log mut">${until(w.resets_at,now)}</div></div>`}
function usage(u,now){
  document.getElementById('usage').innerHTML=u?meter('Sessão (5 h)',u.five_hour,now)+meter('Semana (7 d)',u.seven_day,now)
    :'<div class="meter mut">Uso do plano: consultando o Claude Code (precisa estar instalado e logado).</div>';
  const age=u&&u.updated?now-u.updated:null;
  document.getElementById('stale').textContent=u&&age>900?'Atualizado há '+ago(age)+'.':'';
}
async function tick(){
  let data;try{data=window.AI_STATUS_FETCH?await AI_STATUS_FETCH():await (await fetch('api')).json()}catch(e){document.getElementById('sum').textContent='servidor parado';return}
  if(!window.AI_STATUS_FETCH&&window.boot&&data.boot!==window.boot)return location.reload();   // servidor reiniciado (talvez código novo): recarrega a página
  window.boot=data.boot;
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


USAGE_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Usage</title><style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e3e2dd;--acc:#2f5fb3}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--mut:#9a9993;--line:#33322f;--acc:#8ab4ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif;padding:14px 20px 24px}
h1{font-size:18px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px;font-weight:600}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:12px 0}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}@media (max-width:760px){.grid{grid-template-columns:1fr}}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 8px;font-size:13px;margin:0 0 8px}.row span{overflow-wrap:anywhere}
.row .bar{grid-column:1/-1;height:6px;background:var(--line);border-radius:3px;overflow:hidden}.row .bar i{display:block;height:100%;background:var(--acc)}
svg text{fill:var(--mut);font-size:10px}svg rect{fill:var(--acc)}svg rect:hover{opacity:.7}
</style></head><body><h1>AI Usage</h1><div class="mut" id="sub">carregando…</div>
<div class="kpis" id="kpis"></div><div class="card"><h2>Por dia</h2><div id="chart"></div></div>
<div class="grid"><div class="card"><h2>Projetos</h2><div id="projects"></div></div><div class="card"><h2>Modelos</h2><div id="models"></div></div>
<div class="card"><h2>Tipos de token</h2><div id="kinds"></div></div><div class="card"><h2>Ferramentas (chamadas)</h2><div id="tools"></div></div></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=c=>'$'+c.toFixed(2);
const tok=n=>n>=1e9?(n/1e9).toFixed(2)+' B':n>=1e6?(n/1e6).toFixed(1)+' M':n>=1e3?(n/1e3).toFixed(0)+' k':String(Math.round(n));
function bars(rows,val,label){const max=Math.max(...rows.map(val),1e-9);
  return rows.map(r=>`<div class="row"><span>${esc(r.name)}</span><span class="mut">${label(r)}</span><div class="bar"><i style="width:${100*val(r)/max}%"></i></div></div>`).join('')||'<p class="mut">Nada no período.</p>'}
function chart(days,val,label){const H=140,bw=18,W=days.length*bw,max=Math.max(...days.map(val),1e-9),step=Math.ceil(days.length/12);
  return `<svg viewBox="0 0 ${W} ${H+16}" width="100%" role="img" aria-label="Uso por dia">`+days.map((d,i)=>{const h=Math.round(H*val(d)/max);
    return `<rect x="${i*bw+2}" y="${H-h}" width="${bw-4}" height="${h}" rx="2"><title>${d.name}: ${label(d)}</title></rect>`
      +(i%step===0?`<text x="${i*bw+bw/2}" y="${H+12}" text-anchor="middle">${d.name.slice(8)}/${d.name.slice(5,7)}</text>`:'')}).join('')+'</svg>'}
async function tick(){
  let u;try{u=await (await fetch('api/usage')).json()}catch(e){$('sub').textContent='servidor parado';return}
  if(!u.days){$('sub').textContent='calculando (lendo os históricos do Claude Code)…';return}
  const p=u.priced,val=p?r=>r.cost:r=>r.tokens,label=r=>(p?money(r.cost)+' · ':'')+tok(r.tokens)+' tokens';
  $('sub').textContent=`Últimos ${u.period} dias · `+(p?'custo a preço de API (tabela LiteLLM), não o que o plano cobra':'sem tabela de preços (offline): só tokens')
    +(u.unpriced.length?' · sem preço: '+u.unpriced.join(', '):'')+' · atualizado '+new Date(u.updated*1000).toLocaleTimeString();
  $('kpis').innerHTML=[['Total',u.total],['Hoje',u.today],['Média por dia',{cost:u.total.cost/u.period,tokens:u.total.tokens/u.period}]]
    .map(([t,r])=>`<div class="card kpi"><span class="mut">${t}</span><b>${p?money(r.cost):tok(r.tokens)}</b><span class="mut">${tok(r.tokens)} tokens</span></div>`).join('');
  $('chart').innerHTML=chart(u.days,val,label);
  $('projects').innerHTML=bars(u.projects,val,label);
  $('models').innerHTML=bars(u.models,val,label);
  $('kinds').innerHTML=bars(u.kinds,val,label);
  $('tools').innerHTML=bars(u.tools,r=>r.count,r=>r.count+'');
}
tick();setInterval(tick,30000);
</script></body></html>"""


# "/": as duas páginas acima em abas. Iframes porque cada uma tem o próprio CSS e JS (nomes repetidos: .row, tick, $...);
# dentro da aba o título some (FRAMED), a aba já diz o que é. #usage na URL abre direto na aba de uso.
FRAMED = "<script>if(window!==top)document.documentElement.classList.add('framed')</script><style>.framed h1{display:none}</style>"
PAGE = PAGE.replace("</head>", FRAMED + "</head>", 1)
USAGE_PAGE = USAGE_PAGE.replace("</head>", FRAMED + "</head>", 1)
TABS_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Status</title><style>
:root{--bg:#f6f6f4;--fg:#1c1c1a;--mut:#6b6b66;--line:#e3e2dd;--acc:#2f5fb3}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#9a9993;--line:#33322f;--acc:#8ab4ff}}
html,body{height:100%;margin:0}body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
nav{display:flex;gap:4px;padding:10px 20px 0;border-bottom:1px solid var(--line)}
nav a{padding:6px 14px;color:var(--mut);text-decoration:none;font-weight:600;border-bottom:2px solid transparent;margin-bottom:-1px}
nav a[aria-selected=true]{color:var(--fg);border-color:var(--acc)}
iframe{flex:1;border:0;width:100%}iframe[hidden]{display:none}
</style></head><body><nav role="tablist"><a role="tab" href="#status">AI Status</a><a role="tab" href="#usage">AI Usage</a></nav>
<iframe id="status" src="status" title="AI Status"></iframe><iframe id="usage" src="usage" title="AI Usage" hidden></iframe>
<script>
function show(){const t=location.hash==='#usage'?'usage':'status';
  for(const f of document.querySelectorAll('iframe'))f.hidden=f.id!==t;
  for(const a of document.querySelectorAll('nav a'))a.setAttribute('aria-selected',a.hash==='#'+t)}
addEventListener('hashchange',show);show();
</script></body></html>"""


BOOT = time.time()
try:   # a extensão compara com a sua: um servidor mais antigo (de outra janela) é trocado
    VERSION = json.loads((pathlib.Path(__file__).resolve().parent.parent / "package.json").read_text(encoding="utf-8"))["version"]
except (OSError, ValueError, KeyError):
    VERSION = ""


def serve(port, days, usage_days):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/usage"):
                body, kind = json.dumps(_report.get("u", {})).encode(), "application/json"
            elif self.path.startswith("/api"):
                rows = snapshot(days)
                body, kind = json.dumps({"tasks": rows, "agents": agents(rows), "usage": usage(), "now": time.time(), "boot": BOOT,
                                   "version": VERSION, "pid": os.getpid()}).encode(), "application/json"
            elif self.path.startswith("/usage"):
                body, kind = USAGE_PAGE.encode(), "text/html; charset=utf-8"
            elif self.path.startswith("/status"):
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            else:
                body, kind = TABS_PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    threading.Thread(target=cli_usage_loop, daemon=True).start()
    threading.Thread(target=usage_loop, args=(usage_days,), daemon=True).start()
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
        {"type": "user", "timestamp": "2026-10-18T10:05:00Z", "message": {"content": "<task-notification>\n<task-id>b123abcde</task-id>\n<status>completed</status>\n<summary>ok</summary>"}},
        {"type": "api-request-blob", "message": {"content": "<task-notification>\n<task-id>b123abcde</task-id>\n<status>completed</status>"}},
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
    assert epoch(tasks["b123abcde"]["ended"]) == 1792317900.0 and epoch("") is None   # a repetição sem hora não apaga
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
        for page in (PAGE, USAGE_PAGE, TABS_PAGE):
            body = page[page.index("<body>"):]
            js.write_text(body[body.index("<script>") + 8:body.index("</script>")], encoding="utf-8")
            assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0, "JS da página com erro de sintaxe"
    # Processo corrente: a ferramenta sem resultado é a que roda; a anterior, já respondida, não.
    a = pathlib.Path(tempfile.mkdtemp()) / "a.jsonl"
    a.write_text("".join(json.dumps(l) + "\n" for l in [
        {"type": "user", "message": {"content": "rode os testes"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-10-20T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": "pytest", "description": "Testes"}}]}},
    ]), encoding="utf-8")
    cur = agent_state(a)["current"]
    assert [(c["tool"], c["description"]) for c in cur] == [("Bash", "Testes")], cur
    # Uso: a mesma mensagem em duas linhas (um bloco por linha) conta uma vez; a ferramenta conta uma vez.
    usage_line = {"type": "assistant", "timestamp": "2026-10-20T12:00:00Z", "requestId": "r1", "cwd": "C:\\x\\freedom",
                  "message": {"id": "m1", "model": "claude-x", "content": [{"type": "tool_use", "id": "z1", "name": "Bash"}],
                              "usage": {"input_tokens": 10, "output_tokens": 100, "cache_creation_input_tokens": 1000,
                                        "cache_read_input_tokens": 10000, "cache_creation": {"ephemeral_1h_input_tokens": 400}}}}
    other = dict(usage_line, message=dict(usage_line["message"], content=[{"type": "text", "text": "x"}]))
    a.write_text(json.dumps(usage_line) + "\n" + json.dumps(other) + "\n" + json.dumps(usage_line), encoding="utf-8")
    scan_usage(a)   # a terceira linha não tem \\n: fica para a próxima leitura
    key = next(k for k in _buckets if k[2] == "claude-x")
    assert key[1] == "freedom" and _buckets[key] == [10, 100, 600, 400, 10000], _buckets
    assert sum(n for k, n in _tools.items() if k[2] == "Bash") == 1, _tools
    _prices["claude-x"] = {"input_cost_per_token": 1, "output_cost_per_token": 2, "cache_creation_input_token_cost": 3,
                           "cache_creation_input_token_cost_above_1hr": 4, "cache_read_input_token_cost": 0.5}
    assert costs("claude-x-20260101", False, _buckets[key]) == [10, 200, 600 * 3 + 400 * 4, 5000], costs("claude-x", False, _buckets[key])
    _prices.clear()
    w = cli_window({"utilization": 77, "resets_at": "2026-10-19T13:20:00.052350+00:00"})
    assert w["pct"] == 77.0 and abs(w["resets_at"] - 1792416000.05235) < 1, w
    print("BG_DASHBOARD_OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--usage-days", type=int, default=30)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else serve(a.port, a.days, a.usage_days)
