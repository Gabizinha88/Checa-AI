"""Base de checagens via Google Fact Check Tools API (ClaimReview de várias agências).

Reúne checagens publicadas por Lupa, Aos Fatos, Estadão Verifica, Comprova, G1 Fato ou Fake, AFP
etc., já com o selo da agência (`textualRating`). Devolve hits no MESMO formato de
`Indice.buscar_checagens`, então seguem o caminho da base no pipeline (dedupe, catálogo, juiz,
gate E1, regra P2: o selo só vota com SUSTENTA/REFUTA do juiz).

Porte do backup de 05/10 (`backup/sessao-2026-10-05`), adaptado: I/O por `replay.http_get`
(cassete sem a chave), falha vira `telemetria.fallback`, checagens excluídas do holdout do eval
não entram. Sem `GOOGLE_FACTCHECK_API_KEY`: não chama nada (lista vazia).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import config, replay, selos, telemetria

log = logging.getLogger("factcheck.factcheck_api")

URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
_cache: Dict[str, tuple] = {}  # consulta -> (expira_em, hits)
_CACHE_MAX = 200


def ativo() -> bool:
    return bool(config.FACTCHECK_API_KEY)


def _excluidas() -> set:
    """URLs reservadas para o eval (fora_do_indice): a API não pode trazê-las de volta."""
    try:
        from .ingestor import carregar_excluidas
        return carregar_excluidas()
    except Exception:
        return set()


def _agencia(url: str, site: str) -> Optional[str]:
    """Id da agência no catálogo pelo endereço da checagem (domínio + caminho, ex. g1.globo.com/fato-ou-fake)."""
    try:
        from .serpapi_layer import agencias_checagem
        u = urlsplit(url)
        alvo = f"{(u.hostname or '').lower().removeprefix('www.')}{u.path}"
        for a in agencias_checagem():
            s = (a.get("site") or "").lower().removeprefix("www.")
            if s and alvo.startswith(s) and not str(a.get("id", "")).startswith("extra:"):
                return a.get("id")
    except Exception:
        pass
    return None


def _para_hits(payload: Dict[str, Any], excluidas: set) -> List[Dict[str, Any]]:
    """claims[].claimReview[] -> hits do índice (1 por URL de checagem)."""
    from .ingestor import chave_url
    hits: List[Dict[str, Any]] = []
    vistos = set()
    for claim in (payload or {}).get("claims", []) or []:
        alegacao = (claim.get("text") or "").strip()
        autor = (claim.get("claimant") or "").strip()
        for rev in claim.get("claimReview", []) or []:
            url = (rev.get("url") or "").strip()
            k = chave_url(url) if url else ""
            if not url or k in vistos or k in excluidas:
                continue
            vistos.add(k)
            pub = rev.get("publisher") or {}
            nome = (pub.get("name") or pub.get("site") or "agência de checagem").strip()
            selo = (rev.get("textualRating") or "").strip() or None
            agencia = _agencia(url, pub.get("site") or "")
            hits.append({
                "url": url, "titulo": (rev.get("title") or alegacao or url).strip(),
                "agencia": agencia or nome, "agencia_nome": nome,
                "afirmacao_checada": alegacao or None, "selo_original": selo,
                # selo fora das tabelas de selos.json -> None: aparece, mas não vota
                "veredito": selos.normalizar(selo, agencia) if selo else None,
                "trecho": (f"Alegação checada: {alegacao}" + (f" (autor: {autor})" if autor else "")
                           + f". Classificação de {nome}: {selo or 'sem classificação'}."),
                "data_pub": rev.get("reviewDate") or claim.get("claimDate") or None,
            })
    return hits


def buscar(afirmacao: str, max_itens: int = 0) -> List[Dict[str, Any]]:
    """Checagens publicadas sobre a afirmação. Nunca levanta; nunca registra a chave."""
    if not ativo():
        return []
    q = (afirmacao or "").strip()[:200]
    if len(q) < 10:
        return []
    agora = time.time()
    hit = _cache.get(q)
    if hit and hit[0] > agora:
        return hit[1]
    try:
        r = replay.http_get(URL, params={"query": q, "languageCode": "pt-BR",
                                         "pageSize": max_itens or config.FACTCHECK_MAX_ITENS,
                                         "key": config.FACTCHECK_API_KEY},
                            timeout=config.FACTCHECK_TIMEOUT_S)
        if r.status_code >= 400:
            # 400/403 = chave inválida ou API não habilitada no projeto do Google Cloud
            telemetria.fallback("factcheck_api", f"HTTP {r.status_code}: sem checagens do Google Fact Check")
            return []
        hits = _para_hits(r.json() or {}, _excluidas())
    except Exception as e:  # só o tipo: a mensagem pode trazer a URL com a chave
        telemetria.fallback("factcheck_api", f"{type(e).__name__}: sem checagens do Google Fact Check")
        return []
    log.info("fact check: %d checagem(ns) (%s)", len(hits),
             ", ".join(f"{h['agencia_nome']}={h['veredito']}" for h in hits[:5]))
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(min(_cache, key=lambda c: _cache[c][0]), None)
    _cache[q] = (agora + (config.CACHE_SERPAPI_TTL or 3600), hits)
    return hits
