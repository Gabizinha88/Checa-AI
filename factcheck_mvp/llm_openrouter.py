"""Camada OpenRouter (deepseek-v4-flash via StreamLake + gemma fallback) p/ geração.

Uso: afirmações + padrões. Nunca levanta sem fallback: caller cai p/ regex.
Chave só via env local (.env, nunca logada, nunca no recibo).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from . import config, replay, telemetria

log = logging.getLogger("factcheck.llm_openrouter")
_URL = "https://openrouter.ai/api/v1/chat/completions"
# Jev/TypeSafe: endpoint Decisions (NÃO chat/completions — 400 se usar /v1/...).
_URL_DECISIONS = "https://openrouter.ai/api/alpha/decisions"
_URL_CHAVE = "https://openrouter.ai/api/v1/key"  # consulta a chave; não gasta crédito
_CACHE_STATUS_S = 600
_AVISO_DIAS = 5
_cache_status: Dict[str, Any] = {}


def _provider() -> Dict[str, Any]:
    """Bloco provider p/ fixar inference (ex StreamLake fp8). Vazio = padrão OR."""
    ordem = [p.strip() for p in (config.OPENROUTER_PROVIDER_ORDER or "").split(",") if p.strip()]
    return {"order": ordem} if ordem else {}


def _post(modelo: str, messages: List[dict], max_tokens: int, timeout_s: int) -> str:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY ausente")
    payload: Dict[str, Any] = {"model": modelo, "messages": messages,
                               "temperature": 0.0, "max_tokens": max_tokens}
    prov = _provider()
    if prov:
        payload["provider"] = prov  # fallbacks p/ outros providers do MESMO modelo seguem ativos
    esforco = os.getenv("OPENROUTER_REASONING_EFFORT", "").strip()
    if esforco:  # low|medium|high: limita o raciocínio (que conta nos max_tokens)
        payload["reasoning"] = {"effort": esforco}
    r = replay.llm_post(
        _URL, payload,
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://crosscheckbr.local",
            "X-Title": "CrossCheckBR-MVP",
        },
        timeout=timeout_s, motor="openrouter",
    )
    r.raise_for_status()
    data = r.json()
    choices = (data or {}).get("choices", [])
    texto = ((choices[0] or {}).get("message", {}) or {}).get("content", "") if choices else ""
    texto = (texto or "").strip()
    if not texto:
        raise RuntimeError(f"resposta vazia do modelo {modelo}")
    uso = (data or {}).get("usage", {})
    # log seguro: modelo + uso, nunca chave/headers
    log.info("openrouter ok model=%s prompt_tokens=%s completion_tokens=%s",
             modelo, uso.get("prompt_tokens"), uso.get("completion_tokens"))
    return texto


def status_chave() -> Dict[str, Any]:
    """Estado da chave para o /saude: {ok, expira_em, dias_restantes, saldo, aviso?} ou {ok, erro}.

    ok=False só quando o OpenRouter recusa a chave (ou ela falta); falha de rede -> ok=None
    (não se sabe). Cache de 10 min. Nunca devolve nem registra a chave (nem o `label` mascarado).
    """
    agora = time.time()
    if _cache_status and agora - _cache_status["t"] < _CACHE_STATUS_S:
        return dict(_cache_status["st"])
    if not config.OPENROUTER_API_KEY:
        st: Dict[str, Any] = {"ok": False, "erro": "OPENROUTER_API_KEY ausente"}
    else:
        try:
            r = replay.http_get(_URL_CHAVE, headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
                                timeout=10)
            if r.status_code in (401, 403):
                st = {"ok": False, "erro": "chave expirada ou inválida"}
            elif r.status_code >= 400:
                st = {"ok": None, "erro": f"HTTP {r.status_code}"}
            else:
                d = (r.json() or {}).get("data") or {}
                st = {"ok": True, "expira_em": None, "dias_restantes": None, "saldo": d.get("limit_remaining")}
                if d.get("expires_at"):
                    exp = datetime.fromisoformat(str(d["expires_at"]).replace("Z", "+00:00"))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    dias = int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)
                    st.update(expira_em=exp.date().isoformat(), dias_restantes=dias)
                    if dias < 0:
                        st = {"ok": False, "erro": "chave expirada ou inválida"}
                    elif dias < _AVISO_DIAS:
                        st["aviso"] = f"a chave do OpenRouter expira em {dias} dia(s): gere outra"
        except Exception as e:
            st = {"ok": None, "erro": f"{type(e).__name__}: não foi possível consultar a chave"}
            telemetria.fallback("openrouter.status_chave", type(e).__name__)
    _cache_status.update(t=agora, st=st)
    return dict(st)


def chat(messages: List[dict], max_tokens: int = 0, temperature: float = 0.0,
         timeout_s: int = 0, modelos: List[str] | None = None) -> Tuple[str, str]:
    """Retorna (texto, modelo_usado). Tenta primary -> fallback. Sem limite diário.

    Resposta vazia (200 com conteúdo vazio, comum no StreamLake) ganha 1 retry
    no MESMO modelo antes de cair p/ o próximo — evita queimar o fallback à toa.
    """
    mt = max_tokens or config.OPENROUTER_MAX_TOKENS
    ts = timeout_s or config.OPENROUTER_TIMEOUT_S
    if not modelos:  # `modelos` = ordem própria da chamada (ex.: modelo do juiz); vazio = padrão global
        modelos = [m for m in (config.OPENROUTER_MODEL, config.OPENROUTER_FALLBACK_MODEL) if m]
    ultimo_erro: Exception | None = None
    for modelo in modelos:
        for tentativa in range(2):
            try:
                t0 = time.time()
                texto = _post(modelo, messages, mt, ts)
                log.info("openrouter model=%s lat=%.1fs", modelo, time.time() - t0)
                return texto, modelo
            except Exception as e:  # tenta de novo 1x se vazio, senão próximo
                ultimo_erro = e
                if "resposta vazia" in str(e) and tentativa == 0:
                    log.info("openrouter resposta vazia model=%s: repetindo 1x", modelo)
                    continue
                break
        log.warning("openrouter falhou model=%s: %s", modelo, str(ultimo_erro)[:150])
        telemetria.fallback("openrouter.chat", ultimo_erro, modelo=modelo)
    raise RuntimeError(f"openrouter primary+fallback falharam: {ultimo_erro}")


def decisions(model: str, state: Any, questions: Dict[str, Any],
              timeout_s: int = 0) -> Dict[str, Any]:
    """POST /api/alpha/decisions (Jev). Retorna JSON {model, answers, usage}.

    Sem limite diário. Sem provider.order StreamLake (Jev é TypeSafe).
    Nunca envia a chave no log. Levanta em erro/HTTP não-2xx (caller tem fallback).
    """
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY ausente")
    payload: Dict[str, Any] = {"model": model, "state": state, "questions": questions}
    r = replay.llm_post(
        _URL_DECISIONS, payload,
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://crosscheckbr.local",
            "X-Title": "CrossCheckBR-MVP",
            "Content-Type": "application/json",
        },
        timeout=timeout_s or config.JEV_TIMEOUT_S, motor="openrouter-decisions",
        extrair=lambda d: str((d or {}).get("answers")),
    )
    if r.status_code >= 400:
        # corpo pode embutir validação; trunca p/ não vazar payload gigante
        raise RuntimeError(f"decisions HTTP {r.status_code}: {r.text[:300]}")
    data = r.json() or {}
    answers = data.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise RuntimeError("decisions sem answers")
    uso = data.get("usage") or {}
    log.info("decisions ok model=%s resolved=%s in=%s out=%s",
             model, data.get("model"), uso.get("input_tokens"), uso.get("output_tokens"))
    return data
