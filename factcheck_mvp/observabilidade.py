"""Observabilidade do trace (Missão F, item 8; auditoria E, seção 5). Só descreve o que aconteceu:
nada aqui muda decisão, nível ou o que o pipeline faz.

- `usage_da_resposta` / `finish_reason_da_resposta`: o que o provedor de LLM devolveu além do texto
  (tokens, custo, motivo de parada). Vão no evento `llm` e viram `usageDetails`/`costDetails` no Langfuse.
- `estado_juiz` / `etapa_juiz`: separam "juiz indisponível" (havia o que julgar e o LLM falhou, não
  tinha chave ou estava desligado) de "nada para julgar" (zero páginas candidatas; o juiz nem foi chamado).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from . import telemetria

# estados do juiz numa execução (travas.juiz_estado e evento `juiz_estado`)
JUIZ_DISPONIVEL = "disponivel"
JUIZ_INDISPONIVEL = "indisponivel"
JUIZ_NADA_PARA_JULGAR = "nada_para_julgar"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def usage_da_resposta(d: Any) -> Optional[Dict[str, Any]]:
    """Normaliza o `usage` de uma resposta de LLM. Chat OpenAI-compatível/OpenRouter:
    prompt_tokens, completion_tokens, total_tokens, cost, completion_tokens_details.reasoning_tokens,
    prompt_tokens_details.cached_tokens. Endpoint Decisions (Jev): input_tokens/output_tokens.
    Devolve só as chaves presentes: prompt_tokens, completion_tokens, total_tokens, custo (USD),
    reasoning_tokens, cached_tokens. Sem `usage` utilizável -> None. Nunca levanta."""
    try:
        u = d.get("usage") if isinstance(d, dict) else None
        if not isinstance(u, dict):
            return None
        ent = _num(u.get("prompt_tokens", u.get("input_tokens")))
        sai = _num(u.get("completion_tokens", u.get("output_tokens")))
        total = _num(u.get("total_tokens"))
        if total is None and ent is not None and sai is not None:
            total = ent + sai
        det_c = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
        det_p = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
        out = {"prompt_tokens": ent, "completion_tokens": sai, "total_tokens": total,
               "custo": _num(u.get("cost")), "reasoning_tokens": _num(det_c.get("reasoning_tokens")),
               "cached_tokens": _num(det_p.get("cached_tokens"))}
        out = {k: v for k, v in out.items() if v is not None}
        return out or None
    except Exception:
        return None


def finish_reason_da_resposta(d: Any) -> Optional[str]:
    """`choices[0].finish_reason` (ex.: stop, length). `length` com raciocínio consumindo o orçamento é a
    causa de saída cortada no meio (auditoria E, seção 5: `{"consulta": "Sérgio Sacani e`)."""
    try:
        fr = ((d.get("choices") or [{}])[0] or {}).get("finish_reason")
        return str(fr) if fr else None
    except Exception:
        return None


def estado_juiz(n_candidatas: int, juiz_ok: bool) -> str:
    """`nada_para_julgar`: zero candidatas (o juiz não foi chamado). `indisponivel`: havia candidatas e
    nenhuma foi classificada (LLM falhou, sem chave, desligado ou teto). `disponivel`: classificou algo."""
    if n_candidatas <= 0:
        return JUIZ_NADA_PARA_JULGAR
    return JUIZ_DISPONIVEL if juiz_ok else JUIZ_INDISPONIVEL


def etapa_juiz(n_candidatas: int, julg: Dict[Any, Dict[str, Any]], juiz_ok: bool, contagem: Dict[str, int],
               n_sem: int, n_reb: int, motor: str) -> Tuple[str, str]:
    """-> (status, detalhe) da etapa "juiz" e emite o evento `juiz_estado`. O texto do caminho normal é o
    que o pipeline já tinha; só os dois casos sem julgamento ganham rótulo próprio."""
    estado = estado_juiz(n_candidatas, juiz_ok)
    motivo = ""
    if estado == JUIZ_NADA_PARA_JULGAR:
        status = "pulada"
        detalhe = "Nada para julgar: nenhuma fonte candidata (o juiz não foi chamado)."
    elif estado == JUIZ_INDISPONIVEL:
        motivo = next((str(r.get("erro")) for r in julg.values() if r.get("erro")), "")[:200]
        status = "falha"
        detalhe = (f"Juiz indisponível: {n_sem}/{len(julg)} julgamento(s) sem resposta do LLM"
                   + (f" ({motivo})" if motivo else "") + ".")
    else:
        status = "ok" if not n_sem else "parcial"
        detalhe = (f"{len(julg)} julgamento(s) via {motor}: {contagem['SUSTENTA']} sustentam, "
                   f"{contagem['REFUTA']} refutam, {contagem['RELATA_SEM_ENDOSSO']} só relatam, "
                   f"{contagem['NAO_TRATA']} fora do tema ({n_reb} rebaixada(s) por citação não verificada); "
                   f"{n_sem} sem julgamento.")
    telemetria.evento("juiz_estado", estado=estado, n_candidatas=n_candidatas, n_julgadas=len(julg) - n_sem,
                      n_sem_julgamento=n_sem, motivo=motivo or None)
    return status, detalhe
