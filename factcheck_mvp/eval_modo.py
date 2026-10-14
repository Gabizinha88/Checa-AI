"""Recursos só de avaliação (auditoria E, A1): "fonte deixada de fora" e repetições sem cache do juiz.

Nada aqui age fora de `config.EVAL_MODO` (a API de uma simulação sobe com `EVAL_MODO=1`). Em produção os
campos `eval_*` da EntradaConsulta são ignorados e o descarte vira `fallback onde=eval`, para que ninguém consiga
esconder uma fonte do pipeline por engano ou de propósito.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from . import config, telemetria


def ativo() -> bool:
    return bool(getattr(config, "EVAL_MODO", False))


def normalizar_url(url: str) -> str:
    """Chave de comparação: sem esquema, sem `www.`, sem query/fragmento e sem barra final, em minúsculas.
    `https://www.Site.com/a/b/?x=1` e `http://site.com/a/b` são a mesma página."""
    try:
        p = urlsplit((url or "").strip())
        host = (p.hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        caminho = (p.path or "").rstrip("/").lower()
        return f"{host}{caminho}"
    except ValueError:
        return (url or "").strip().lower().rstrip("/")


def urls_excluidas(entrada: Any) -> frozenset:
    """Conjunto normalizado de URLs a tratar como inexistentes neste caso. Vazio fora do modo de avaliação."""
    pedidas = [u for u in (getattr(entrada, "eval_excluir_urls", None) or []) if u]
    if not pedidas:
        return frozenset()
    if not ativo():
        telemetria.fallback("eval", "eval_excluir_urls ignorado: EVAL_MODO desligado", n=len(pedidas))
        return frozenset()
    return frozenset(n for n in (normalizar_url(u) for u in pedidas) if n)


def sal_do_juiz(entrada: Any) -> str:
    """Sal do cassete do juiz para esta execução: repetição >= 1 não reaproveita o julgamento gravado das
    outras repetições (a repetição 0 usa os cassetes de sempre)."""
    rep = int(getattr(entrada, "eval_repeticao", 0) or 0)
    if rep <= 0:
        return ""
    if not ativo():
        telemetria.fallback("eval", "eval_repeticao ignorada: EVAL_MODO desligado", repeticao=rep)
        return ""
    return f"rep{rep}"


def foi_excluida(url: str, excluidas: frozenset) -> bool:
    return bool(excluidas) and normalizar_url(url) in excluidas


def filtrar_pecas(pecas: Iterable[Dict[str, Any]], excluidas: frozenset, onde: str) -> List[Dict[str, Any]]:
    """Tira as peças cuja URL está em `excluidas`, com um evento `eval_exclusao` para cada uma."""
    if not excluidas:
        return list(pecas)
    saida = []
    for p in pecas:
        if foi_excluida(p.get("url") or "", excluidas):
            telemetria.evento("eval_exclusao", url=p.get("url"), onde=onde)
            continue
        saida.append(p)
    return saida


def meta_para_trace(entrada: Any) -> Optional[Dict[str, Any]]:
    """Resumo para o `run_inicio` (None quando o caso não usa nenhum recurso de avaliação)."""
    n_ex = len(getattr(entrada, "eval_excluir_urls", None) or [])
    rep = int(getattr(entrada, "eval_repeticao", 0) or 0)
    if not n_ex and not rep:
        return None
    return {"modo": ativo(), "excluir_urls": n_ex, "repeticao": rep}
