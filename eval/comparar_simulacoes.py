"""Compara simulações de usuário (`reports/simulacao_*/dados.json`) com intervalo e teste pareado.

    python3 -m eval.comparar_simulacoes reports/simulacao_A [reports/simulacao_B] [--nomes base,final]
                                        [--runs runs] [--saida arquivo.md]

Auditoria E, A1: ninguém declara melhora sem intervalo. Para cada simulação:
  - acerto por categoria e geral, com IC95 de Wilson (células caso×repetição e casos por maioria das repetições);
  - `antigas_como_atuais` fora do geral (o desenho atual manda "indeterminada + aviso de data"): reportada à parte;
  - erro grave (falso/enganoso -> baixa, verdadeiro -> alta);
  - mudança de nível entre repetições do mesmo caso;
  - caminho por caso (base x web) e acerto em cada caminho;
  - com os traces em `runs/` (--runs): % de itens do juiz com frase de veredito no trecho, consultas vazias por
    caso da web e casos que acharam alguma agência.
Com duas simulações: McNemar pareado exato por (caso, repetição) e por caso (maioria), e as diferenças.
Sem dependências além da biblioteca padrão (não importa `factcheck_mvp`, que carrega o .env).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RAIZ = Path(__file__).resolve().parent.parent
FALSOS = ("falso", "enganoso")
# Frase de veredito no trecho que o juiz recebeu: o que um leitor usaria para saber a conclusão da página.
VEREDITO_RE = re.compile(
    r"\b(é|e|são|foi|foram)\s+(falso|falsa|falsos|falsas|fake|mentira|enganos[oa]s?|verdade|verdadeir[oa]s?|montagem)\b|"
    r"\bnão\s+(é|há|existe|procede|foi)\b[^.]{0,40}\b(verdade|evidência|prova|comprovação|real|fato)\b|"
    r"#fake|#fato|\bcomprova explica\b|\bconclus[ãa]o\s*[:\-–—]|\bsem\s+(evidência|comprovação)\b|"
    r"\b(falso|enganoso|distorcido|verdadeiro)\s*[:\-–—]", re.I)


# --------------------------------------------------------------------------- estatística
def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    meia = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - meia), min(1.0, centro + meia)


def mcnemar_exato(b: int, c: int) -> float:
    """p bicaudal exato (binomial, p=0,5) sobre os discordantes b e c."""
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    cauda = sum(math.comb(n, i) for i in range(m + 1)) / 2 ** n
    return min(1.0, 2 * cauda)


def pct(k: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% [IC95 {100 * lo:.0f}–{100 * hi:.0f}]"


# --------------------------------------------------------------------------- dados
def carregar(caminho: str) -> Dict[str, Any]:
    p = Path(caminho)
    arq = p / "dados.json" if p.is_dir() else p
    return json.loads(arq.read_text(encoding="utf-8"))


def celulas(dados: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Uma célula por execução (caso × repetição) concluída, sem erro de execução."""
    out = []
    for r in dados["casos"]:
        if r.get("erro") or r.get("resultado") == "nao_executado":
            continue
        f = r["frase"]
        out.append({"id": f["id"], "cat": f["categoria"], "rotulo": f["rotulo"], "rep": r.get("repeticao") or 0,
                    "nivel": r.get("nivel"), "ok": r.get("resultado") == "acerto",
                    "resultado": r.get("resultado"), "origem": r.get("origem_resposta") or "?",
                    "alerta_data": r.get("alerta_data") is True, "run_id": r.get("run_id"),
                    "serpapi": r.get("serpapi") or 0, "fonte_excluida": bool(r.get("fonte_excluida")),
                    "grave": erro_grave(f["rotulo"], r.get("nivel"))})
    return out


def erro_grave(rotulo: str, nivel: Optional[str]) -> bool:
    return (rotulo in FALSOS and nivel == "baixa") or (rotulo == "verdadeiro" and nivel == "alta")


def por_caso(cels: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    d: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in cels:
        d[c["id"]].append(c)
    return d


def caso_ok(reps: List[Dict[str, Any]]) -> bool:
    """Caso certo = a maioria estrita das repetições acertou (empate conta como não)."""
    return sum(1 for x in reps if x["ok"]) * 2 > len(reps)


def instabilidade(cels: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mudança de nível entre repetições do mesmo caso (só casos com 2+ repetições)."""
    casos = [v for v in por_caso(cels).values() if len(v) >= 2]
    if not casos:
        return {"casos": 0}
    mudou = sum(1 for v in casos if len({x["nivel"] for x in v}) > 1)
    pares = discord = 0
    for v in casos:
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                pares += 1
                discord += v[i]["nivel"] != v[j]["nivel"]
    return {"casos": len(casos), "casos_que_mudaram": mudou, "pares": pares, "pares_discordantes": discord,
            "taxa_casos": mudou / len(casos), "taxa_pares": discord / pares if pares else 0.0}


def resumo(dados: Dict[str, Any]) -> Dict[str, Any]:
    cels = celulas(dados)
    sem_antigas = [c for c in cels if c["cat"] != "antigas_como_atuais"]
    antigas = [c for c in cels if c["cat"] == "antigas_como_atuais"]
    cats = {}
    for cat in sorted({c["cat"] for c in cels}):
        cs = [c for c in cels if c["cat"] == cat]
        casos = por_caso(cs)
        cats[cat] = {"celulas": len(cs), "ok": sum(c["ok"] for c in cs), "casos": len(casos),
                     "casos_ok": sum(caso_ok(v) for v in casos.values()), "graves": sum(c["grave"] for c in cs)}

    def bloco(cs):
        casos = por_caso(cs)
        return {"celulas": len(cs), "ok": sum(c["ok"] for c in cs), "casos": len(casos),
                "casos_ok": sum(caso_ok(v) for v in casos.values()), "graves": sum(c["grave"] for c in cs)}
    caminhos = {}
    for o in sorted({c["origem"] for c in sem_antigas}):
        caminhos[o] = bloco([c for c in sem_antigas if c["origem"] == o])
    erros = sum(1 for r in dados["casos"] if r.get("erro"))
    return {"meta": dados.get("meta", {}), "cels": cels, "geral_sem_antigas": bloco(sem_antigas),
            "antigas": {**bloco(antigas), "com_aviso_de_data": sum(c["alerta_data"] for c in antigas),
                        "niveis": dict(Counter(c["nivel"] for c in antigas))},
            "categorias": cats, "caminhos": caminhos, "instabilidade": instabilidade(sem_antigas),
            "instabilidade_com_antigas": instabilidade(cels),
            "niveis": dict(Counter(c["nivel"] for c in sem_antigas)), "erros_execucao": erros,
            "graves": [(c["id"], c["rep"], c["rotulo"], c["nivel"]) for c in cels if c["grave"]],
            "serpapi_total": sum(r.get("serpapi") or 0 for r in dados["casos"])}


def pareado(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """McNemar entre duas simulações, sem as `antigas`. Pareia por (id, repetição) e por caso (maioria)."""
    ca = {(c["id"], c["rep"]): c for c in a["cels"] if c["cat"] != "antigas_como_atuais"}
    cb = {(c["id"], c["rep"]): c for c in b["cels"] if c["cat"] != "antigas_como_atuais"}
    comuns = sorted(set(ca) & set(cb))
    b_ = sum(1 for k in comuns if ca[k]["ok"] and not cb[k]["ok"])      # A acertou, B não
    c_ = sum(1 for k in comuns if not ca[k]["ok"] and cb[k]["ok"])      # B acertou, A não
    casos_a, casos_b = por_caso(list(ca.values())), por_caso(list(cb.values()))
    ids = sorted(set(casos_a) & set(casos_b))
    bc = sum(1 for i in ids if caso_ok(casos_a[i]) and not caso_ok(casos_b[i]))
    cc = sum(1 for i in ids if not caso_ok(casos_a[i]) and caso_ok(casos_b[i]))
    mudou = [i for i in ids if caso_ok(casos_a[i]) != caso_ok(casos_b[i])]
    return {"celulas": {"n": len(comuns), "so_A": b_, "so_B": c_, "p": mcnemar_exato(b_, c_)},
            "casos": {"n": len(ids), "so_A": bc, "so_B": cc, "p": mcnemar_exato(bc, cc), "mudaram": mudou}}


# --------------------------------------------------------------------------- traces
def _linhas(run_dir: Path) -> List[Dict[str, Any]]:
    try:
        return [json.loads(l) for l in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def itens_do_juiz(eventos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Itens JSON que o juiz recebeu (precisa de TELEMETRIA_PROMPTS=1 no run)."""
    itens = []
    for e in eventos:
        d = e.get("dados") or {}
        if e.get("tipo") != "llm" or d.get("finalidade") != "juiz" or not d.get("prompt"):
            continue
        texto = " ".join(str(m.get("content", "")) for m in d["prompt"] if isinstance(m, dict) and m.get("role") == "user")
        if "ITENS:" not in texto:
            continue
        for linha in texto.split("ITENS:", 1)[1].splitlines():
            try:
                itens.append(json.loads(linha))
            except ValueError:
                continue
    return itens


def metricas_trace(run_dir: Path) -> Dict[str, Any]:
    ev = _linhas(run_dir)
    itens = itens_do_juiz(ev)
    com_verd = sum(1 for it in itens if VEREDITO_RE.search(f"{it.get('titulo', '')}\n{it.get('trecho', '')}"))
    ondas = [(e.get("dados") or {}) for e in ev
             if e.get("tipo") == "etapa" and (e.get("dados") or {}).get("nome") == "agente-onda"]
    buscadas = [d for d in ondas if d.get("status") != "pulada" and not d.get("reuso")]
    vazias = sum(1 for d in buscadas if (d.get("n_resultados") or 0) == 0)
    achou_ag = False
    for e in ev:
        if e.get("tipo") == "evidencias":
            achou_ag = any(it.get("agencia") and it.get("classe") in ("SUSTENTA", "REFUTA", "RELATA_SEM_ENDOSSO")
                           for it in (e.get("dados") or {}).get("itens", []))
    return {"itens_juiz": len(itens), "itens_com_veredito": com_verd, "buscas": len(buscadas), "vazias": vazias,
            "achou_agencia": achou_ag, "teve_busca": bool(buscadas)}


def agregar_traces(dados: Dict[str, Any], runs: Path) -> Dict[str, Any]:
    tot = Counter()
    n_web = n_ag = 0
    for r in dados["casos"]:
        if r.get("erro") or not r.get("run_id") or r["frase"]["categoria"] == "antigas_como_atuais":
            continue
        m = metricas_trace(runs / r["run_id"])
        if not (runs / r["run_id"]).exists():
            continue
        tot["itens_juiz"] += m["itens_juiz"]
        tot["itens_com_veredito"] += m["itens_com_veredito"]
        if m["teve_busca"]:
            n_web += 1
            tot["buscas"] += m["buscas"]
            tot["vazias"] += m["vazias"]
            n_ag += m["achou_agencia"]
    return {**tot, "casos_com_busca": n_web, "casos_com_busca_que_acharam_agencia": n_ag}


# --------------------------------------------------------------------------- relatório
def _tabela(res: Dict[str, Any]) -> List[str]:
    g = res["geral_sem_antigas"]
    L = [f"- Geral (sem `antigas`): células {pct(g['ok'], g['celulas'])}; casos (maioria) {pct(g['casos_ok'], g['casos'])}",
         f"- Erro grave: {g['graves']} célula(s) {res['graves'] or ''}",
         f"- Erros de execução: {res['erros_execucao']} · SerpAPI live: {res['serpapi_total']}"]
    a = res["antigas"]
    L.append(f"- `antigas_como_atuais` (à parte): {a['celulas']} células, aviso de data em {a['com_aviso_de_data']}, "
             f"níveis {a['niveis']}")
    L += ["", "| categoria | células | casos (maioria) | graves |", "|---|---|---|---|"]
    for cat, v in res["categorias"].items():
        L.append(f"| {cat} | {pct(v['ok'], v['celulas'])} | {pct(v['casos_ok'], v['casos'])} | {v['graves']} |")
    L += ["", "| caminho | células | casos (maioria) | graves |", "|---|---|---|---|"]
    for o, v in res["caminhos"].items():
        L.append(f"| {o} | {pct(v['ok'], v['celulas'])} | {pct(v['casos_ok'], v['casos'])} | {v['graves']} |")
    i = res["instabilidade"]
    if i.get("casos"):
        L += ["", f"- Mudança de nível entre repetições (sem `antigas`): {i['casos_que_mudaram']}/{i['casos']} casos "
                  f"({100 * i['taxa_casos']:.0f}%); pares discordantes {i['pares_discordantes']}/{i['pares']} "
                  f"({100 * i['taxa_pares']:.1f}%)"]
    L.append(f"- Níveis (sem `antigas`): {res['niveis']}")
    return L


def relatorio(nomes: List[str], resumos: List[Dict[str, Any]], traces: List[Optional[Dict[str, Any]]]) -> str:
    L = ["# Comparação de simulações", ""]
    for nome, res, tr in zip(nomes, resumos, traces):
        m = res["meta"]
        L += [f"## {nome}", f"- início {m.get('inicio')} · seed {m.get('seed')} · n={m.get('n')} · "
              f"repetições {m.get('repeticoes', 1)} · fonte deixada de fora {m.get('excluir_fonte', False)} · "
              f"commit {m.get('commit')}"]
        L += _tabela(res)
        if tr:
            it = tr["itens_juiz"] if "itens_juiz" in tr else 0
            L += ["", f"- Itens do juiz com frase de veredito no trecho: {pct(tr.get('itens_com_veredito', 0), it)}",
                  f"- Consultas vazias: {tr.get('vazias', 0)} de {tr.get('buscas', 0)} buscadas em "
                  f"{tr.get('casos_com_busca', 0)} casos com busca "
                  f"({(tr.get('vazias', 0) / max(1, tr.get('casos_com_busca', 0))):.2f} por caso)",
                  f"- Casos com busca que acharam alguma agência: {tr.get('casos_com_busca_que_acharam_agencia', 0)}"
                  f"/{tr.get('casos_com_busca', 0)}"]
        L.append("")
    if len(resumos) == 2:
        p = pareado(resumos[0], resumos[1])
        a, b = nomes
        L += [f"## {a} x {b} (sem `antigas`)",
              f"- McNemar por célula (caso×repetição): n={p['celulas']['n']}, só {a} acertou {p['celulas']['so_A']}, "
              f"só {b} acertou {p['celulas']['so_B']}, p={p['celulas']['p']:.3f}",
              f"- McNemar por caso (maioria): n={p['casos']['n']}, só {a} {p['casos']['so_A']}, só {b} {p['casos']['so_B']}, "
              f"p={p['casos']['p']:.3f}; casos que mudaram: {p['casos']['mudaram']}",
              "- Leitura: p >= 0,05 ou menos de 3 discordantes líquidos = **sem diferença detectável**. As células do "
              "mesmo caso não são independentes; o teste por caso é o mais conservador.", ""]
        ga, gb = resumos[0]["geral_sem_antigas"], resumos[1]["geral_sem_antigas"]
        L += [f"- Graves: {a} {ga['graves']} · {b} {gb['graves']}"]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("simulacoes", nargs="+", help="1 ou 2 pastas/dados.json")
    ap.add_argument("--nomes", default="", help="nomes separados por vírgula (padrão: A,B)")
    ap.add_argument("--runs", type=Path, default=None, help="pasta runs/ para as métricas de trace")
    ap.add_argument("--saida", type=Path, default=None)
    a = ap.parse_args(argv)
    if len(a.simulacoes) > 2:
        ap.error("no máximo 2 simulações")
    dados = [carregar(s) for s in a.simulacoes]
    nomes = [n for n in a.nomes.split(",") if n] or ["A", "B"][:len(dados)]
    resumos = [resumo(d) for d in dados]
    traces = [agregar_traces(d, a.runs) if a.runs else None for d in dados]
    texto = relatorio(nomes, resumos, traces)
    if a.saida:
        a.saida.write_text(texto, encoding="utf-8")
    sys.stdout.buffer.write((texto + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
