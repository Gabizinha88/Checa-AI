"""Autoconsistência do juiz quando UM julgamento decide o nível (Missão F / A3).

Auditoria E (seção 2.3, risco R1): em 24 dos 39 níveis não indeterminados, um único cluster com um
único julgamento SUSTENTA/REFUTA decidia o caso, e o juiz muda de classe em ~9% dos itens ao ser
repetido. Aqui, nesses casos (e só neles), o item decisivo é rejulgado `K-1` vezes SEM cache e a
decisão passa pelo voto das `K` classes:

- as `K` concordam                      -> mantém (`unanime`);
- maioria estrita (> K/2) sem SUSTENTA×REFUTA na mesma amostra -> vale a classe da maioria (`maioria`);
  uma repetição que falhou (LLM, miss de cassete) é abstenção, não voto;
- SUSTENTA e REFUTA na mesma amostra, ou sem maioria -> `RELATA_SEM_ENDOSSO` (`instavel`): falha
  segura. Nunca inverte a direção (a amostra nunca vira o oposto da original; só perde a posição).
  Com a P2 do agregador, RELATA_SEM_ENDOSSO também não deixa o selo do índice votar, então o nível cai.

"Um julgamento decide" usa a MESMA noção de cluster do agregador: roda-se `decisao.decidir(ev)` e
olham-se os `votos` (um por afirmação × cluster independente, com valor ≠ 0). Exatamente 1 cluster
votante e nível ≠ indeterminada. 2+ clusters (corroboração independente) ou 0 votos: não rejulga.

Sem cache: cada repetição roda sob `replay.com_sal("autocN")` (o mesmo mecanismo do A1: o sal entra na chave do
cassete do juiz, então em `record` a repetição grava o próprio cassete e em `replay` relê o mesmo, de forma
determinística) e `replay.temperatura(t)` (payload com temperature > 0, que é o que gera variação em `live`).

Liga com `JUIZ_AUTOCONSISTENCIA=1` (padrão 0). `JUIZ_AUTOCONSISTENCIA_K` (3) = julgamentos no total e
`JUIZ_AUTOCONSISTENCIA_TEMP` (0,5) = temperatura das repetições. Telemetria: evento `autoconsistencia`.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from . import avaliador, config, decisao, juiz_llm, replay, telemetria

CLASSE_INSTAVEL = "RELATA_SEM_ENDOSSO"
# Texto fixo (não vem do LLM): o raciocínio original defendia uma classe que acabou de ser descartada.
RACIOCINIO_INSTAVEL = ("Os julgamentos automáticos repetidos desta página divergiram; ela é tratada "
                       "como relato, sem tomar posição.")
_CAMPOS_REP = ("citacao", "citacao_score", "citacao_verificada", "pagina_diz", "raciocinio")


def k_total() -> int:
    try:
        return int(getattr(config, "JUIZ_AUTOCONSISTENCIA_K", 3))
    except (TypeError, ValueError):
        return 3


def habilitada() -> bool:
    """Flag ligada e com pelo menos 1 repetição (K >= 2)."""
    return bool(getattr(config, "JUIZ_AUTOCONSISTENCIA", False)) and k_total() >= 2


# ----------------------------------------------------------------------------- voto
def votar(classes: List[Optional[str]], k: int) -> Tuple[str, str]:
    """-> (classe_final, desfecho) para as classes das `k` amostras (None = amostra que falhou).

    Direção divergente (SUSTENTA e REFUTA juntas) ou sem maioria estrita (> k/2 votos válidos iguais)
    -> RELATA_SEM_ENDOSSO, `instavel`. Senão, a maioria; `unanime` quando as `k` são iguais e válidas."""
    validas = [c for c in classes if c]
    if "SUSTENTA" in validas and "REFUTA" in validas:
        return CLASSE_INSTAVEL, "instavel"
    if validas:
        classe, n = Counter(validas).most_common(1)[0]
        if n * 2 > k:
            return classe, ("unanime" if n == len(classes) else "maioria")
    return CLASSE_INSTAVEL, "instavel"


# ----------------------------------------------------------------------------- detecção
def itens_decisivos(ev: "decisao.Evidencias", dec: "decisao.Decisao") -> Tuple[List[int], str]:
    """Índices de `ev.itens` cujo julgamento SUSTENTA/REFUTA decide o nível, ou ([], motivo).

    Decide = o único cluster votante (mesma noção do agregador: `dec.votos`) e o item tem postura na
    direção do voto, com corpo lido e juiz de verdade (não `fallback-*`)."""
    if dec.nivel == "indeterminada":
        return [], "nivel_indeterminada"
    votos = [v for v in dec.votos if v.valor != 0 and not dec._afirmacao_fora(v.afirmacao)]
    clusters = {(v.afirmacao, v.cluster) for v in votos}
    if len(clusters) != 1:
        return [], f"clusters={len(clusters)}"
    voto = votos[0]
    s = -1.0 if ev.afirmacoes[voto.afirmacao].polaridade == "nega" else 1.0
    achados = []
    for j, it in enumerate(ev.itens):
        if it.afirmacao != voto.afirmacao or (it.cluster or it.url) != voto.cluster:
            continue
        if it.classe not in decisao.CLASSES_VOTO or (it.motor or "").startswith("fallback") or not it.corpo_lido:
            continue
        d = 1.0 if it.classe == "REFUTA" else -1.0
        if s * d == voto.direcao:
            achados.append(j)
    if not achados:
        return [], "sem_julgamento_sustenta_refuta"
    return achados, ""


# ----------------------------------------------------------------------------- repetição
def _rejulgar(alvo: str, peca: Dict[str, Any], rep: int) -> Optional[Dict[str, Any]]:
    """Uma repetição do avaliador, sem cache (sal e temperatura próprios). None = falhou (já registrado)."""
    url = peca.get("url")
    try:
        with replay.com_sal(f"autoc{rep}"), replay.temperatura(getattr(config, "JUIZ_AUTOCONSISTENCIA_TEMP", 0.5)):
            a = avaliador.avaliar(alvo, peca)
    except Exception as e:  # uma repetição que falha não derruba a consulta: vira abstenção
        telemetria.fallback("autoconsistencia", f"repeticao {rep}: {type(e).__name__}: {e}", url=url)
        return None
    if a.get("posicao") not in juiz_llm.CLASSES:
        telemetria.fallback("autoconsistencia", f"repeticao {rep} sem julgamento: {a.get('erro')}", url=url,
                            motor=a.get("motor"))
        return None
    return a


def _peca_do_item(it: "decisao.ItemEvidencia", pecas: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return next((p for p in pecas if p.get("url") == it.url), None)


# ----------------------------------------------------------------------------- aplicação
def aplicar(ev: "decisao.Evidencias", pecas: List[Dict[str, Any]], afs: List[Any],
            julg: Dict[Tuple[int, int], Dict[str, Any]]) -> Dict[str, Any]:
    """Rejulga o item decisivo (se houver um só cluster decidindo) e corrige `ev.itens` e `julg` no
    lugar, ANTES da decisão final. Nunca levanta. -> resumo (`aplicada`, `itens`, `nivel_antes/depois`)."""
    if not habilitada():
        return {"aplicada": False, "motivo": "desligada"}
    try:
        return _aplicar(ev, pecas, afs, julg)
    except Exception as e:
        telemetria.fallback("autoconsistencia", f"{type(e).__name__}: {e}")
        return {"aplicada": False, "motivo": "erro"}


def _aplicar(ev, pecas, afs, julg) -> Dict[str, Any]:
    dec = decisao.decidir(ev)
    idx, motivo = itens_decisivos(ev, dec)
    if not idx:
        telemetria.evento("autoconsistencia", desfecho="nao_aplica", motivo=motivo, nivel=dec.nivel)
        return {"aplicada": False, "motivo": motivo}
    k = k_total()
    temp = getattr(config, "JUIZ_AUTOCONSISTENCIA_TEMP", 0.5)
    resumo: List[Dict[str, Any]] = []
    for j in idx:
        it = ev.itens[j]
        peca = _peca_do_item(it, pecas)
        if peca is None:
            telemetria.fallback("autoconsistencia", "peça do item decisivo não encontrada", url=it.url)
            continue
        alvo = afs[it.afirmacao].alvo()
        reps = [_rejulgar(alvo, peca, r) for r in range(1, k)]
        classes = [it.classe] + [(r or {}).get("posicao") for r in reps]
        final, desfecho = votar(classes, k)
        resumo.append({"j": j, "classes": classes, "reps": reps, "final": final, "desfecho": desfecho,
                       "original": it.classe})
    if not resumo:
        return {"aplicada": False, "motivo": "peca_nao_encontrada"}

    por_url_af = {(pecas[pi].get("url"), ai): (pi, ai) for (pi, ai) in julg}
    for r in resumo:
        it = ev.itens[r["j"]]
        chave = por_url_af.get((it.url, it.afirmacao))
        entrada = julg.get(chave) if chave else None
        if r["desfecho"] == "instavel":
            it.classe, it.citacao_verificada = CLASSE_INSTAVEL, None
            if entrada is not None:
                entrada.update(classe=CLASSE_INSTAVEL, citacao="", citacao_score=None, citacao_verificada=None,
                               raciocinio=RACIOCINIO_INSTAVEL, pagina_diz="")
        elif r["final"] != r["original"]:
            rep = next(x for x in r["reps"] if x and x.get("posicao") == r["final"])
            it.classe, it.citacao_verificada = r["final"], rep.get("citacao_verificada")
            if entrada is not None:
                entrada.update(classe=r["final"], **{c: rep.get(c) for c in _CAMPOS_REP if c in rep})
        if entrada is not None:
            entrada["autoconsistencia"] = {"classes": r["classes"], "desfecho": r["desfecho"]}
    depois = decisao.decidir(ev)
    for r in resumo:
        it = ev.itens[r["j"]]
        telemetria.evento(
            "autoconsistencia", url=it.url, afirmacao=it.afirmacao, cluster=it.cluster or it.url, k=k,
            temperatura=temp, classes=r["classes"], validas=sum(1 for c in r["classes"] if c),
            desfecho=r["desfecho"], classe_original=r["original"], classe_final=it.classe,
            mudou=it.classe != r["original"],
            nivel_antes=dec.nivel, nivel_depois=depois.nivel)
        r.pop("reps", None)
    return {"aplicada": True, "itens": resumo, "nivel_antes": dec.nivel, "nivel_depois": depois.nivel}
