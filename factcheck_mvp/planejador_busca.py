"""Planejador de busca (Missão F / A5; auditoria E, seção 5). Atrás de `BUSCA_PLANEJADOR=1` (padrão 0).

Quatro decisões, todas dentro do orçamento por caso que o agente já tem (`EstadoBusca.reservar`) e dos tetos
globais de SerpAPI (`replay._reservar_serpapi`: `--max-buscas`, `SERPAPI_ORCAMENTO_RESTANTE`):

1. **Relaxar** a consulta que voltou vazia (sem `site:`/aspas/operadores; sem palavras-ruído e ano; só os
   termos mais longos) antes de desistir. No Google News o AND de muitos termos zera o resultado (13/48
   consultas vazias em r5).
2. **Motor por domínio**: consulta com `site:` (agências, boatos.org, e-farsas) vai ao Google orgânico
   (`engine=google`, com `num`), porque o News não indexa essas fontes; a consulta pura segue no motor
   configurado. O resultado do orgânico é normalizado pelo mesmo `serpapi_layer.normalizar_item`.
3. **Ler mais** que os `ORGANICOS_POR_BUSCA` resultados já lidos quando os primeiros foram, na maioria, fora
   do tema: reaproveita o payload que já foi pago (custo zero de busca), até `BUSCA_LER_MAX` por busca.
4. **Usar o orçamento que sobra**: até `BUSCA_EXTRAS_MAX` ondas extras por afirmação (em vez de parar na
   primeira), enquanto houver teto e a afirmação seguir sem evidência (o crítico decide).

Este módulo só tem as regras puras e a telemetria; a execução das buscas continua em `agente.py`.
Evento `planejador` (docs/TELEMETRIA.md): `decisao` = relaxou | trocou_motor | leu_mais | parou_orcamento.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import config, replay, telemetria

# Termos que o Google News exige no AND e quase nunca estão no texto da notícia (auditoria D, seção 4).
_RUIDO = frozenset("""disse diz dizem afirma afirmou video videos mostra mostrando checagem checar boato boatos
falso falsa fake verdade verdadeiro noticia noticias urgente agora hoje ontem circula circulando viraliza
viral compartilhe acabou sair""".split())
_ANO = re.compile(r"(19|20)\d{2}")


def ativo() -> bool:
    return bool(getattr(config, "BUSCA_PLANEJADOR", False))


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


# ----------------------------------------------------------------------------- 1. relaxar
def sem_operadores(q: str) -> str:
    """Tira `site:x`, aspas, parênteses, `OR`, `|` e `-termo`; mantém as palavras da consulta."""
    q = re.sub(r"\bsite:\S+", " ", q or "", flags=re.I)
    q = re.sub(r"[\"“”()]", " ", q)
    q = re.sub(r"\bOR\b|\|", " ", q)
    q = re.sub(r"(^|\s)-\S+", " ", q)
    return " ".join(q.split())


def _mais_longos(termos: List[str]) -> List[str]:
    """Metade dos termos (mínimo 2), os mais longos (= mais específicos), na ordem original."""
    k = max(2, math.ceil(len(termos) / 2))
    if len(termos) <= k:
        return termos
    escolhidos = sorted(range(len(termos)), key=lambda i: (-len(termos[i]), i))[:k]
    return [termos[i] for i in sorted(escolhidos)]


def passos_relaxamento(consulta: str) -> List[str]:
    """Consultas cada vez mais frouxas derivadas de `consulta`, na ordem em que valem a pena:
    (1) sem operadores; (2) sem palavras-ruído e sem ano; (3) só os termos mais longos.
    Sem repetições, sem a própria consulta e com ao menos 2 palavras (1 palavra só traz tudo)."""
    orig = " ".join((consulta or "").split()).lower()
    out: List[str] = []

    def _add(c: str) -> None:
        c = " ".join(c.split())
        if len(c.split()) >= 2 and c.lower() != orig and c.lower() not in (o.lower() for o in out):
            out.append(c)

    base = sem_operadores(consulta)
    _add(base)
    util = [t for t in base.split() if _norm(t).strip(".,;:!?") not in _RUIDO and not _ANO.fullmatch(t)]
    _add(" ".join(util))
    _add(" ".join(_mais_longos(util or base.split())))
    return out


def proxima_relaxada(consulta: str, feitas: Iterable[str]) -> Optional[str]:
    """Primeiro passo de relaxamento de `consulta` que `feitas` ainda não contém (None = esgotou)."""
    ja = {" ".join(f.split()).lower() for f in feitas}
    for c in passos_relaxamento(consulta):
        if c.lower() not in ja:
            return c
    return None


def max_rodadas_relaxar() -> int:
    return max(0, int(getattr(config, "BUSCA_RELAXAR_RODADAS", 2) or 0))


# ----------------------------------------------------------------------------- 2. motor por domínio
def tem_site(q: str) -> bool:
    return bool(re.search(r"\bsite:", q or "", flags=re.I))


def motor_por_dominio(q: Dict[str, Any], motor_padrao: str) -> Optional[Dict[str, Any]]:
    """Consulta com `site:` -> Google orgânico (`engine=google`, `num` mantido). None = não se aplica
    (planejador desligado ou consulta pura: segue o motor configurado). Emite `trocou_motor` quando o
    motor padrão era outro."""
    if not ativo() or not tem_site(q.get("q", "")):
        return None
    novo = dict(q)
    novo["engine"] = "google"
    novo.setdefault("num", 10)
    if motor_padrao != "google":
        evento("trocou_motor", de=motor_padrao, para="google", consulta=q.get("q"),
               motivo="site: só funciona no orgânico; o News não indexa agências/boatos.org")
    return novo


# ----------------------------------------------------------------------------- 3. ler mais
def ler_max() -> int:
    from . import agente
    return max(agente.ORGANICOS_POR_BUSCA, int(getattr(config, "BUSCA_LER_MAX", 16) or 0))


def fora_do_tema(resumo: Any) -> bool:
    """Os resultados lidos não trouxeram postura e a maioria foi julgada NAO_TRATA."""
    n = getattr(resumo, "n", {}) or {}
    julgadas = sum(v for k, v in n.items() if k != "sem_juiz")
    if julgadas <= 0 or getattr(resumo, "clusters_postura", 0) > 0:
        return False
    frac = float(getattr(config, "BUSCA_FORA_TEMA_FRACAO", 0.5) or 0.5)
    return n.get("NAO_TRATA", 0) / julgadas >= frac


def ler_mais(estado: Any, pedidos: Sequence[Tuple[int, Any]], onda: int) -> Set[int]:
    """Para cada afirmação que o crítico mandou buscar de novo e cujos resultados lidos foram fora do tema,
    absorve os resultados seguintes dos payloads JÁ obtidos (sem gastar busca). -> afirmações atendidas."""
    if not getattr(estado, "planejador", False):
        return set()
    atendidas: Set[int] = set()
    teto = ler_max()
    for ai, resumo in pedidos:
        if not fora_do_tema(resumo):
            continue
        novos = estado.ler_mais(ai, onda, teto)
        if novos:
            atendidas.add(ai)
            evento("leu_mais", afirmacao=ai, onda=onda, novos=novos, teto_por_busca=teto,
                   motivo=f"{resumo.n.get('NAO_TRATA', 0)} de {sum(v for k, v in resumo.n.items() if k != 'sem_juiz')} "
                          "julgadas fora do tema")
    return atendidas


# ----------------------------------------------------------------------------- 4. orçamento
def orcamento_esgotado() -> bool:
    """Teto de buscas live do processo/arquivo (`replay.uso_serpapi`) já atingido? Só serve para parar de
    insistir depois de uma busca que falhou; a contagem e a recusa em si são de `replay._reservar_serpapi`."""
    try:
        u = replay.uso_serpapi()
    except Exception as e:  # contador ilegível: não decide nada
        telemetria.fallback("planejador", f"uso_serpapi ilegível: {type(e).__name__}: {e}")
        return False
    teto_p = u.get("teto_processo")
    if teto_p is not None and int(u.get("usadas_processo") or 0) >= int(teto_p):
        return True
    rest = u.get("restantes")
    return rest is not None and int(rest) <= 0


def limite_extras(max_extras: int) -> int:
    """Ondas extras por afirmação com o planejador ligado (quem chama já checou o flag do estado):
    `BUSCA_EXTRAS_MAX`, mas nunca abaixo do configurado, e 0 continua 0 (quem desligou as ondas extras
    não as ganha de volta)."""
    if max_extras <= 0:
        return 0
    return max(max_extras, int(getattr(config, "BUSCA_EXTRAS_MAX", 3) or 0))


# ----------------------------------------------------------------------------- telemetria
def evento(decisao: str, **dados: Any) -> None:
    telemetria.evento("planejador", decisao=decisao, **dados)
