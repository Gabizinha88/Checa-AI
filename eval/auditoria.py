"""Auditoria de traces: resume um run (`runs/<id>/trace.jsonl`) num texto curto e legível.

Serve para revisar, caso a caso, o que cada etapa LLM recebeu e respondeu (afirmações, avaliador/juiz)
e o que a decisão fez com isso. Os prompts e as respostas completas só estão no trace quando o run foi
gravado com `TELEMETRIA_PROMPTS=1` (ver docs/TELEMETRIA.md); sem isso o resumo avisa e mostra o que há.

    python3 -m eval.auditoria <run_id>... [--simulacao reports/<pasta>/dados.json] [--saida pasta]

Não faz rede nem chama LLM. Só lê `runs/` (ou `TELEMETRIA_DIR`) e escreve markdown.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

RAIZ = Path(__file__).resolve().parent.parent
TRECHO = 350   # chars do trecho de cada página mostrados no resumo
SAIDA = 500    # chars de uma saída de LLM fora do formato esperado


def _eventos(pasta: Path) -> List[Dict[str, Any]]:
    arq = pasta / "trace.jsonl"
    evs: List[Dict[str, Any]] = []
    for linha in arq.read_text(encoding="utf-8").splitlines():
        try:
            evs.append(json.loads(linha))
        except ValueError:
            continue
    return evs


def _json_da_saida(saida: Any) -> Optional[Any]:
    """Interpreta a saída de texto do LLM como JSON (aceita cerca ```json). None se não for JSON."""
    if not isinstance(saida, str):
        return None
    t = saida.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        return None


def _ultimo_conteudo(prompt: Any) -> str:
    if isinstance(prompt, list) and prompt:
        ult = prompt[-1]
        return str(ult.get("content", "")) if isinstance(ult, dict) else str(ult)
    return ""


def _linhas_afirmacoes(d: Dict[str, Any]) -> List[str]:
    out = ["### Extração de afirmações (LLM)"]
    cont = _ultimo_conteudo(d.get("prompt"))
    m = re.search(r'TEXTO:\s*"""\s*(.*?)\s*"""\s*$', cont, re.S)
    if m:
        out.append(f"- Texto enviado: {m.group(1)[:600]!r}")
    elif not d.get("prompt"):
        out.append("- (prompt não gravado: rode com TELEMETRIA_PROMPTS=1)")
    dados = _json_da_saida(d.get("saida"))
    if isinstance(dados, list):
        for it in dados:
            if isinstance(it, dict):
                out.append(f"- [{it.get('polaridade')}] afirmacao={it.get('afirmacao')!r} | nucleo={it.get('nucleo')!r}"
                           f" | consulta={it.get('consulta')!r}")
    else:
        out.append(f"- Saída não-JSON: {str(d.get('saida'))[:SAIDA]!r}")
    if d.get("erro"):
        out.append(f"- ERRO: {d['erro']}")
    return out


def _linhas_juiz(d: Dict[str, Any]) -> List[str]:
    out = ["### Juiz/avaliador (LLM)"]
    cont = _ultimo_conteudo(d.get("prompt"))
    m = re.search(r'AFIRMAÇÃO A VERIFICAR:\s*"(.*?)"\n', cont, re.S)
    if m:
        out.append(f"- Afirmação julgada: {m.group(1)!r}")
    entradas: Dict[int, Dict[str, Any]] = {}
    if "ITENS:" in cont:
        for linha in cont.split("ITENS:", 1)[1].strip().splitlines():
            try:
                it = json.loads(linha)
                entradas[int(it.get("i", len(entradas)))] = it
            except (ValueError, TypeError):
                continue
    elif not d.get("prompt"):
        out.append("- (prompt não gravado: rode com TELEMETRIA_PROMPTS=1)")
    dados = _json_da_saida(d.get("saida"))
    itens = dados.get("itens") if isinstance(dados, dict) else dados if isinstance(dados, list) else None
    if not isinstance(itens, list):
        out.append(f"- Saída não-JSON: {str(d.get('saida'))[:SAIDA]!r}")
    else:
        for r in itens:
            if not isinstance(r, dict):
                continue
            e = entradas.get(r.get("i"), {})
            trecho = re.sub(r"\s+", " ", str(e.get("trecho", "")))[:TRECHO]
            out.append(f"- item {r.get('i')}: {e.get('veiculo', '')} | {e.get('titulo', '')!r} | data={e.get('data', '')}"
                       + (f" | veredito_pagina={e.get('veredito_pagina')}" if e.get("veredito_pagina") else ""))
            if trecho:
                out.append(f"    trecho: {trecho!r}")
            out.append(f"    -> classe={r.get('classe')} | pagina_diz={r.get('pagina_diz')!r}")
            out.append(f"       citacao={r.get('citacao')!r}")
            out.append(f"       raciocinio={r.get('raciocinio')!r}")
    if d.get("erro"):
        out.append(f"- ERRO: {d['erro']}")
    return out


def resumir_run(pasta: Path, esperado: Optional[str] = None, frase: Optional[str] = None) -> str:
    """Resumo markdown de um run. `esperado` e `frase` (do gabarito) são opcionais e só aparecem no cabeçalho."""
    pasta = Path(pasta)
    evs = _eventos(pasta)
    out: List[str] = [f"# Run {pasta.name}"]
    ini = next((e["dados"] for e in evs if e["tipo"] == "run_inicio"), {})
    out.append(f"- Entrada: {(frase or ini.get('entrada') or '')[:600]!r}")
    if esperado:
        out.append(f"- Esperado (gabarito): {esperado}")
    llms = [e["dados"] for e in evs if e["tipo"] == "llm"]
    if llms and not any(d.get("prompt") for d in llms):
        out.append("- AVISO: este run não tem os prompts completos (TELEMETRIA_PROMPTS não estava ligada).")
    for e in evs:
        d = e["dados"]
        if e["tipo"] == "llm":
            fin = d.get("finalidade")
            if fin == "afirmacoes":
                out += _linhas_afirmacoes(d)
            elif fin == "juiz":
                out += _linhas_juiz(d)
            else:
                out.append(f"### LLM `{fin}`: saída {str(d.get('saida'))[:200]!r}" + (f" ERRO {d['erro']}" if d.get("erro") else ""))
        elif e["tipo"] == "etapa" and d.get("nome") in ("base-checagem", "descoberta", "deep-crawl", "agente-onda"):
            out.append(f"- etapa {d['nome']} [{d['status']}]: {str(d.get('detalhe'))[:300]}")
        elif e["tipo"] == "fonte" and d.get("estagio") == "aplicabilidade":
            out.append(f"- aplicabilidade {d.get('url')}: {d.get('decisao')} ({d.get('motivo')})")
        elif e["tipo"] == "fonte" and d.get("decisao") == "descartada":
            out.append(f"- fonte descartada ({d.get('estagio')}): {d.get('url')} - {d.get('motivo')}")
        elif e["tipo"] == "fallback":
            out.append(f"- FALLBACK {d.get('onde')}: {str(d.get('motivo'))[:200]}")
        elif e["tipo"] == "erro":
            out.append(f"- ERRO {json.dumps(d, ensure_ascii=False)[:300]}")
    ev_ev = next((e["dados"] for e in evs if e["tipo"] == "evidencias"), None)
    if ev_ev:
        out.append("### Evidências enviadas à decisão")
        for it in ev_ev.get("itens", []):
            out.append(f"- af{it.get('afirmacao')} {it.get('classe')} cit_ok={it.get('citacao_verificada')} "
                       f"{it.get('confiabilidade')} curada={it.get('curada')} selo={it.get('veredito')}"
                       f"({it.get('origem_veredito')}) data={it.get('data_pub')} {it.get('url')}")
    dec = next((e["dados"] for e in reversed(evs) if e["tipo"] == "decisao"), None)
    if dec:
        out.append("### Decisão")
        out.append(f"- nivel={dec.get('nivel')} score={dec.get('score')} | {dec.get('why')}")
        t = dec.get("travas") or {}
        out.append(f"- L={t.get('L')} motivo={t.get('motivo')!r} votos={t.get('votos')}")
        trv = [k for k, v in t.items() if v is True]
        if trv:
            out.append(f"- travas ativas: {trv}")
    return "\n".join(out) + "\n"


def _texto(frase: Any) -> Optional[str]:
    return frase.get("texto") if isinstance(frase, dict) else frase


def _gabarito(caso: Dict[str, Any]) -> str:
    """Linha de gabarito de um caso de scripts/simular_usuario.py (`frase` é o dict sorteado)."""
    f = caso.get("frase") if isinstance(caso.get("frase"), dict) else {}
    return (f"{f.get('id', '?')} | categoria={f.get('categoria')} | rótulo={f.get('rotulo')} | "
            f"nota={f.get('nota')!r} | checagem de origem={f.get('url_checagem')} (data {f.get('data')}) | "
            f"o sistema respondeu: {caso.get('nivel')} (p={caso.get('prob')}) = {caso.get('resultado')}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_ids", nargs="*", help="ids em runs/ (ou caminhos de pasta)")
    ap.add_argument("--simulacao", type=Path, help="dados.json de scripts/simular_usuario.py: resume todos os casos")
    ap.add_argument("--runs", type=Path, default=RAIZ / "runs")
    ap.add_argument("--saida", type=Path, default=None, help="pasta onde gravar um .md por run (padrão: stdout)")
    a = ap.parse_args(argv)
    alvos: List[tuple] = []
    if a.simulacao:
        for c in json.loads(a.simulacao.read_text(encoding="utf-8"))["casos"]:
            if c.get("run_id"):
                alvos.append((c["run_id"], _gabarito(c), _texto(c.get("frase"))))
    alvos += [(r, None, None) for r in a.run_ids]
    if not alvos:
        ap.error("informe run_ids ou --simulacao")
    for rid, esperado, frase in alvos:
        pasta = Path(rid) if Path(rid).is_dir() else a.runs / rid
        try:
            txt = resumir_run(pasta, esperado, frase)
        except OSError as e:
            print(f"[{rid}] sem trace: {e}", file=sys.stderr)
            continue
        if a.saida:
            a.saida.mkdir(parents=True, exist_ok=True)
            (a.saida / f"{pasta.name}.md").write_text(txt, encoding="utf-8")
        else:
            print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
