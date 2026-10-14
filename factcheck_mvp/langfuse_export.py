"""Exporta os runs (`runs/<id>/trace.jsonl`) para o Langfuse e puxa de volta para auditoria.

O trace local continua sendo a fonte de verdade; o Langfuse é uma visão externa dele. A exportação
lê o trace já gravado (e já redigido por `telemetria.redigir`), então funciona para runs antigos e
não altera o pipeline. Mapeamento:

  run            -> trace   (id determinístico: reexportar o mesmo run sobrescreve, não duplica)
  etapa          -> span    (início = fim da etapa anterior; nível WARNING se não "ok")
  llm            -> generation (prompt, saída, modelo, latência; ERROR se `erro`)
  fonte/fallback/decisao/sinal/evidencias -> event

Não passa por `replay.*` de propósito: é observabilidade, não I/O do pipeline, e nunca deve ser
gravada em cassete. Credenciais: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY e LANGFUSE_BASE_URL no .env.

Uso:
  python -m factcheck_mvp.langfuse_export enviar <run_id…> [--tag x]
  python -m factcheck_mvp.langfuse_export enviar --simulacao reports/<pasta>/dados.json [--tag x]
  python -m factcheck_mvp.langfuse_export puxar --tag x [--saida arquivo.json]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import telemetria

_NS = uuid.UUID("6f1c2b8e-4c3a-4b8e-9a51-7d0e2f1a9c33")
LOTE_MAX_BYTES = 3_000_000  # a ingestão aceita ~3,5 MB por requisição
TIMEOUT_S = 30.0


def _id(*partes: Any) -> str:
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in partes)))


def configurado() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _base() -> str:
    return (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
            or "https://cloud.langfuse.com").rstrip("/")


def _auth() -> str:
    par = f"{os.environ.get('LANGFUSE_PUBLIC_KEY', '')}:{os.environ.get('LANGFUSE_SECRET_KEY', '')}"
    return "Basic " + base64.b64encode(par.encode()).decode()


def _req(metodo: str, caminho: str, corpo: Any = None) -> Any:
    dados = json.dumps(corpo, ensure_ascii=False, default=str).encode("utf-8") if corpo is not None else None
    r = urllib.request.Request(_base() + caminho, data=dados, method=metodo,
                               headers={"Authorization": _auth(), "Content-Type": "application/json"})
    for tentativa in range(4):
        try:
            with urllib.request.urlopen(r, timeout=TIMEOUT_S) as resp:
                txt = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:  # mensagem do servidor, sem credenciais
            if e.code == 429 and tentativa < 3:
                time.sleep(min(float(e.headers.get("Retry-After") or 10), 70))
                continue
            raise RuntimeError(f"Langfuse HTTP {e.code} em {caminho}: "
                               f"{e.read().decode('utf-8', 'replace')[:300]}") from None
    return json.loads(txt) if txt else None


# ---------------------------------------------------------------- trace local -> eventos de ingestão
def _ts(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def _env(tipo: str, corpo: Dict[str, Any], ts: Optional[str], chave: str) -> Dict[str, Any]:
    return {"id": _id("evt", chave), "type": tipo, "timestamp": ts or datetime.now().astimezone().isoformat(),
            "body": {k: v for k, v in corpo.items() if v is not None}}


def eventos_do_run(pasta: Path, tags: Iterable[str] = (), metadados: Optional[Dict[str, Any]] = None
                   ) -> List[Dict[str, Any]]:
    """Converte um run local em eventos da API de ingestão do Langfuse."""
    run_id = pasta.name
    trace = [json.loads(l) for l in (pasta / "trace.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    res_arq = pasta / "resultado.json"
    resultado = json.loads(res_arq.read_text(encoding="utf-8")) if res_arq.exists() else {}
    rel = resultado.get("relatorio") or {}
    tid = _id("trace", run_id)
    ini = next((e for e in trace if e["tipo"] == "run_inicio"), None)
    fim = next((e for e in trace if e["tipo"] == "run_fim"), None)
    meta_ini = (ini or {}).get("dados") or {}
    saida = {k: rel.get(k) for k in ("propensao", "justificativa", "limitacoes") if isinstance(rel, dict) and k in rel}
    if fim:
        saida.setdefault("propensao", fim["dados"].get("nivel"))
    out = [_env("trace-create", {
        "id": tid, "name": "checar", "timestamp": (ini or {}).get("ts"),
        "input": meta_ini.get("entrada"), "output": saida or None,
        "tags": sorted(set(tags)), "sessionId": (metadados or {}).get("sessao"),
        "metadata": {"run_id": run_id, "config": meta_ini.get("config"), "tipo": meta_ini.get("tipo"),
                     "dur_ms": (fim or {}).get("dados", {}).get("dur_ms"), **(metadados or {})},
    }, (ini or {}).get("ts"), f"{run_id}:trace")]

    anterior = (ini or {}).get("ts")
    for n, e in enumerate(trace):
        d, ts, tipo = e.get("dados") or {}, e.get("ts"), e["tipo"]
        chave = f"{run_id}:{n}"
        if tipo == "etapa":
            nivel = "DEFAULT" if d.get("status") in ("ok", "pulada") else "WARNING"
            out.append(_env("span-create", {
                "id": _id("span", chave), "traceId": tid, "name": f"etapa:{d.get('nome')}",
                "startTime": anterior or ts, "endTime": ts, "level": nivel,
                "statusMessage": d.get("status"), "output": d.get("detalhe"),
                "metadata": {"n_fontes": d.get("n_fontes")}}, ts, chave))
            anterior = ts
        elif tipo == "llm":
            fim_g = _ts(ts)
            ini_g = fim_g - timedelta(milliseconds=float(d.get("latencia_ms") or 0)) if fim_g else None
            out.append(_env("generation-create", {
                "id": _id("gen", chave), "traceId": tid, "name": f"llm:{d.get('finalidade')}",
                "startTime": _iso(ini_g), "endTime": ts, "model": d.get("modelo"),
                "input": d.get("prompt") or {"prompt_sha": d.get("prompt_sha"), "n_chars": d.get("n_chars_prompt")},
                "output": d.get("saida"), "level": "ERROR" if d.get("erro") else "DEFAULT",
                "statusMessage": d.get("erro"),
                "metadata": {"motor": d.get("motor"), "cache": d.get("cache"), "status": d.get("status"),
                             "prompt_sha": d.get("prompt_sha")}}, ts, chave))
        elif tipo in ("fonte", "fallback", "decisao", "sinal", "evidencias", "indice_descarte"):
            nome = tipo
            if tipo == "fonte":
                nome = f"fonte:{d.get('estagio')}"
            elif tipo == "fallback":
                nome = f"fallback:{d.get('onde')}"
            out.append(_env("event-create", {
                "id": _id("ev", chave), "traceId": tid, "name": nome, "startTime": ts, "input": d,
                "level": "WARNING" if tipo == "fallback" else "DEFAULT"}, ts, chave))
    return out


def _lotes(eventos: List[Dict[str, Any]]) -> Iterable[List[Dict[str, Any]]]:
    lote, tam = [], 0
    for ev in eventos:
        n = len(json.dumps(ev, ensure_ascii=False, default=str).encode("utf-8"))
        if lote and tam + n > LOTE_MAX_BYTES:
            yield lote
            lote, tam = [], 0
        lote.append(ev)
        tam += n
    if lote:
        yield lote


def enviar(eventos: List[Dict[str, Any]]) -> Dict[str, int]:
    ok = erros = 0
    for lote in _lotes(eventos):
        r = _req("POST", "/api/public/ingestion", {"batch": lote}) or {}
        ok += len(r.get("successes") or [])
        for err in r.get("errors") or []:
            erros += 1
            print(f"  erro de ingestão: {str(err)[:200]}", file=sys.stderr)
    return {"ok": ok, "erros": erros}


def exportar_runs(run_ids: Iterable[str], tags: Iterable[str] = (),
                  metadados_por_run: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, int]:
    eventos: List[Dict[str, Any]] = []
    n = 0
    for rid in run_ids:
        pasta = telemetria.diretorio() / rid
        if not (pasta / "trace.jsonl").exists():
            print(f"  sem trace: {rid}", file=sys.stderr)
            continue
        eventos += eventos_do_run(pasta, tags, (metadados_por_run or {}).get(rid))
        n += 1
    r = enviar(eventos)
    r["runs"] = n
    return r


def exportar_run_atual(run_id: str) -> None:
    """Gancho do fim do run (LANGFUSE_AUTO=1). Nunca derruba o pipeline."""
    try:
        exportar_runs([run_id], tags=["auto"])
    except Exception as e:  # observabilidade externa é opcional
        print(f"[langfuse] exportação falhou: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)


# ---------------------------------------------------------------- Langfuse -> auditoria
def puxar(tag: str, limite: int = 200) -> List[Dict[str, Any]]:
    """Baixa os traces com a tag, cada um com suas observações (spans, generations, events)."""
    traces, pagina = [], 1
    while len(traces) < limite:
        q = urllib.parse.urlencode({"tags": tag, "limit": 50, "page": pagina})
        r = _req("GET", f"/api/public/traces?{q}") or {}
        dados = r.get("data") or []
        traces += dados
        if len(dados) < 50:
            break
        pagina += 1
    traces = traces[:limite]
    if not traces:
        return []
    # GET /traces/{id} tem limite baixo (5/min); as observações vêm em páginas pela janela de tempo.
    por_trace: Dict[str, List[Dict[str, Any]]] = {t["id"]: [] for t in traces}
    ts = sorted(t["timestamp"] for t in traces)
    de = (_ts(ts[0].replace("Z", "+00:00")) - timedelta(minutes=1)).isoformat()
    ate = (_ts(ts[-1].replace("Z", "+00:00")) + timedelta(minutes=10)).isoformat()
    pagina = 1
    while True:
        q = urllib.parse.urlencode({"fromStartTime": de, "toStartTime": ate, "limit": 100, "page": pagina})
        r = _req("GET", f"/api/public/observations?{q}") or {}
        dados = r.get("data") or []
        for o in dados:
            if o.get("traceId") in por_trace:
                por_trace[o["traceId"]].append(o)
        meta = r.get("meta") or {}
        if not dados or pagina >= int(meta.get("totalPages") or pagina):
            break
        pagina += 1
    for t in traces:
        t["observations"] = sorted(por_trace[t["id"]], key=lambda o: o.get("startTime") or "")
    return traces


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m factcheck_mvp.langfuse_export")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enviar")
    e.add_argument("run_ids", nargs="*")
    e.add_argument("--simulacao", help="dados.json de scripts/simular_usuario.py")
    e.add_argument("--tag", action="append", default=[])
    p = sub.add_parser("puxar")
    p.add_argument("--tag", required=True)
    p.add_argument("--saida", default="")
    p.add_argument("--limite", type=int, default=200)
    a = ap.parse_args(argv)
    if not configurado():
        print("LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY ausentes no ambiente (.env).", file=sys.stderr)
        return 2
    if a.cmd == "enviar":
        ids, meta = list(a.run_ids), {}
        if a.simulacao:
            sim = Path(a.simulacao)
            dados = json.loads(sim.read_text(encoding="utf-8"))
            for c in dados.get("casos") or []:
                rid, fr = c.get("run_id"), c.get("frase") or {}
                if not rid:
                    continue
                ids.append(rid)
                meta[rid] = {"sessao": sim.parent.name, "caso": fr.get("id"), "categoria": fr.get("categoria"),
                             "rotulo": fr.get("rotulo"), "obtido": c.get("nivel"),
                             "resultado": c.get("resultado"), "onde": c.get("onde_encontrado"),
                             "buscas_serpapi": c.get("serpapi")}
            a.tag.append(sim.parent.name)
        r = exportar_runs(ids, a.tag, meta)
        print(f"runs={r['runs']} eventos_ok={r['ok']} erros={r['erros']} tags={a.tag}")
        return 0 if r["erros"] == 0 else 1
    traces = puxar(a.tag, a.limite)
    txt = json.dumps(traces, ensure_ascii=False, indent=1, default=str)
    if a.saida:
        Path(a.saida).write_text(txt, encoding="utf-8")
        print(f"{len(traces)} trace(s) -> {a.saida}")
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    from . import config  # noqa: F401  (carrega o .env)
    sys.exit(main())
