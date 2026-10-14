"""Trava de similaridade no atalho do índice de checagens (Missão F / A6, auditoria E, risco R2).

Problema: com `INDICE_CHECAGENS=1`, o selo de UMA checagem do índice (peso 1,5 > τ) cravava alta/baixa
sozinho. O índice acha checagens de alegação VIZINHA ("#CaiuNaRede", mesma pessoa/assunto com outro
predicado) e o juiz confirma o selo pelo título; a variante vizinha herdava o nível da checagem.

Regra (geral, sem exceção por caso), aplicada em `decisao._agregar` — o ÚNICO ponto em que o selo do
índice vira voto agregado:
  quando o |L_a| ≥ τ de uma afirmação depende de UM só cluster, e o que decide nesse cluster é o selo do
  índice (origem_veredito "indice"), exige-se similaridade mínima entre a alegação que a checagem checou e
  o núcleo da afirmação do usuário. Abaixo do limiar o selo não crava nada sozinho: o |L_a| é cortado para
  0,99τ (teto `media`), como as outras travas de `_agregar`.
  Com 2+ clusters independentes na mesma direção, ou quando o extremo se sustenta sem o selo, não age.

Medida: coeficiente de Dice (2·|A∩B| / (|A|+|B|)) entre os termos de conteúdo de `indice.tokenizar_pt`
(minúsculas, sem acento, sem stopword nem "moldura" de boato, radical RSLP) — a mesma tokenização que o
índice usa para achar a checagem, então "falso", "fake", "circula" etc. não contam. O índice já mede a
cobertura da CONSULTA na checagem (`score_rel` ≥ 0,6); falta o sentido inverso: a checagem pode checar
algo mais específico ou diferente (outro predicado, outro objeto). O Dice, média harmônica das duas
coberturas, penaliza os dois lados.

Limiar padrão = 0,6, o MESMO `cobertura_min` do índice (indice._LIMIAR, validado no mini-benchmark de
28/09/2026 e na recarga de 07/10/2026). Razão geral: pelo menos 3 a cada 5 termos de conteúdo (na média dos
dois sentidos) em comum. Paráfrases da mesma alegação mantêm o núcleo (sujeito, verbo, objeto, número); a
alegação vizinha troca um deles e cai para perto de 0,5 ou menos. O valor não foi ajustado a nenhum caso
do conjunto; é configurável (`INDICE_TRAVA_SIM_MIN`) para a medição final.

Alegação da checagem = `afirmacao_checada`, o título e a 1ª frase do trecho; vale a MELHOR com pelo menos
`MIN_TERMOS` termos de conteúdo (uma hashtag como "#CaiuNaRede" tem 1 e não identifica alegação alguma).
Sem nenhuma candidata utilizável, a similaridade é 0 (a checagem não diz o que checou: não decide sozinha).

Módulo puro: sem I/O nem telemetria (quem emite é `Pipeline._emitir_decisao`). Atrás de
`INDICE_TRAVA_SIMILARIDADE=1` (padrão 0).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config, indice

MIN_TERMOS = 3          # termos de conteúdo mínimos para uma candidata identificar uma alegação
_MAX_FRASE = 300        # a 1ª frase do trecho é cortada aqui (trecho sem pontuação não vira "uma frase" gigante)
# Prefixos da descrição (`decisao._contribuicoes`) dos votos que o índice sozinho sustenta: o selo e, desde B4/R1,
# a postura REFUTA de agência curada (pesa como selo, 1,5): a mesma página do índice, com o mesmo poder de cravar.
_DESCRICOES_DO_INDICE = ("selo", "postura REFUTA (agência)")


def habilitada() -> bool:
    """Flag lida a cada chamada (monkeypatch/env em runtime)."""
    return bool(config.INDICE_TRAVA_SIMILARIDADE)


def limiar() -> float:
    return float(config.INDICE_TRAVA_SIM_MIN)


def primeira_frase(trecho: Optional[str]) -> str:
    texto = " ".join((trecho or "").split())
    if not texto:
        return ""
    return re.split(r"(?<=[.!?])\s+", texto, maxsplit=1)[0][:_MAX_FRASE]


def candidatas(afirmacao_checada: Optional[str], titulo: Optional[str], trecho: Optional[str]) -> List[str]:
    """Textos que podem dizer o que a checagem checou, sem repetição, em ordem de confiança."""
    saida: List[str] = []
    for t in (afirmacao_checada, titulo, primeira_frase(trecho)):
        t = " ".join((t or "").split())
        if t and t not in saida:
            saida.append(t)
    return saida


def _termos(texto: str) -> set:
    return set(indice.tokenizar_pt(texto))


def similaridade(nucleo: str, alegacoes: Sequence[str]) -> Tuple[float, Optional[str]]:
    """(melhor Dice, candidata que o deu) entre o núcleo e as candidatas utilizáveis; (0.0, None) se nenhuma."""
    q = _termos(nucleo)
    melhor, quem = 0.0, None
    if not q:
        return melhor, quem
    for cand in alegacoes or ():
        t = _termos(cand)
        if len(t) < MIN_TERMOS:
            continue
        d = 2.0 * len(q & t) / (len(q) + len(t))
        if d > melhor:
            melhor, quem = d, cand
    return round(melhor, 4), quem


def _eh_selo_do_indice(item: Any, descricao: str) -> bool:
    return getattr(item, "origem_veredito", None) == "indice" and descricao.startswith(_DESCRICOES_DO_INDICE)


def avaliar_teto(clusters: Dict[str, List[tuple]], L_a: float, tau: float,
                 nucleo: str) -> Optional[Dict[str, Any]]:
    """Decide se a trava corta o |L_a| de UMA afirmação. `clusters` = {cluster: [(valor, descrição, item)]}
    (o mesmo formato que `decisao._agregar` agrega). Devolve o registro do corte ou None (não agiu).

    Só age se: flag ligada; |L_a| ≥ τ; exatamente UM cluster vota na direção de L_a; o voto desse
    cluster é decidido por uma peça do índice (selo ou REFUTA de agência, B4/R1) cujo peso é necessário
    (sem ela o cluster ficaria abaixo de τ); e a melhor similaridade entre as alegações checadas e o núcleo fica abaixo do limiar.
    Itens do índice sem `alegacoes_checadas` (None: snapshot antigo / peça sem esse dado) não são
    avaliados: sem dado não se corta."""
    if not habilitada() or abs(L_a) < tau:
        return None
    sinal = 1.0 if L_a > 0 else -1.0
    a_favor = [cid for cid, cs in clusters.items() if sum(v for v, _, _ in cs) * sinal > 0]
    if len(a_favor) != 1:
        return None  # 2+ clusters independentes na direção (ou nenhum): a decisão não é de uma fonte só
    cid = a_favor[0]
    pos = [(abs(v), d, it) for v, d, it in clusters[cid] if v * sinal > 0]
    peso = max(p for p, _, _ in pos)
    selos = [it for p, d, it in pos if _eh_selo_do_indice(it, d)]
    if not any(p == peso and _eh_selo_do_indice(it, d) for p, d, it in pos):
        return None  # quem decide o cluster não é o selo do índice
    resto = max((p for p, d, it in pos if not _eh_selo_do_indice(it, d)), default=0.0)
    if resto >= tau:
        return None  # o extremo se sustenta sem o selo (postura/outra fonte)
    avaliaveis = [it for it in selos if getattr(it, "alegacoes_checadas", None) is not None]
    if not avaliaveis:
        return None
    sim, cand = max((similaridade(nucleo, it.alegacoes_checadas) for it in avaliaveis),
                    key=lambda x: x[0])
    if sim >= limiar():
        return None
    return {"cluster": cid, "urls": sorted({it.url for it in avaliaveis}), "similaridade": sim,
            "limiar": limiar(), "alegacao": cand, "nucleo": nucleo, "L_antes": round(L_a, 4),
            "direcao": int(sinal)}
