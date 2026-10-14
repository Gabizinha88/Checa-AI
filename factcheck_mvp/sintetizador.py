"""Sintetizador por afirmação (Missão F / A2; auditoria E, seção 2.2 e 2.4).

O juiz julga uma página por vez e o agregador soma os votos; ninguém relê o CONJUNTO antes do nível.
Aqui há uma chamada LLM por afirmação que recebe todos os itens já julgados (classe, citação, raciocínio,
veículo, agência sim/não, data) e diz se o conjunto é coerente: páginas equivalentes com classes
diferentes, REFUTA/SUSTENTA por inferência ou silêncio, alegação checada diferente da afirmação,
decisão apoiada numa só fonte fraca.

O LLM só RECOMENDA (`manter` | `rebaixar` | `buscar_mais`). Quem manda é a trava determinística:

  * o sintetizador NUNCA sobe o nível nem muda a direção: o resultado é sempre o nível original,
    `media` ou `indeterminada` (`travar_nivel`, ponto único de segurança, testado em todos os níveis);
  * `rebaixar` leva alta/baixa a `media` (sobra evidência coerente a favor) ou `indeterminada`
    (as fontes se contradizem), e `media` a `indeterminada` (`destino_rebaixar`);
  * `buscar_mais` só SINALIZA (telemetria, `travas.sintetizador_buscar_mais` e limitação); a onda extra
    depende de reabrir o laço do agente depois da decisão e ficou fora deste item (docs/TELEMETRIA.md);
  * saída inválida ou LLM indisponível = `manter` + `telemetria.fallback("sintetizador", ...)`.

O agregador continua dono do número (`log_odds`/`prob` não mudam). Atrás de `SINTETIZADOR=1` (padrão 0).
Nunca levanta.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, field_validator

from . import config, llm, telemetria

log = logging.getLogger("factcheck.sintetizador")

ACOES = ("manter", "rebaixar", "buscar_mais")
# Convicção do nível: alta e baixa são extremos (2); media é 1; indeterminada é 0. Só se desce.
CONVICCAO = {"alta": 2, "baixa": 2, "media": 1, "indeterminada": 0}
_DESTINOS = ("media", "indeterminada")
_NOME = {"alta": "alta", "media": "média", "baixa": "baixa", "indeterminada": "indeterminada"}


# ----------------------------------------------------------------------------- trava (o essencial)
def travar_nivel(original: str, proposto: str) -> str:
    """Único ponto onde o sintetizador mexe no nível. Devolve `proposto` só se for `media` ou
    `indeterminada` e de convicção menor que a do original; senão, o original. Logo: nunca sobe,
    nunca vira o lado (alta<->baixa) e nunca inventa nível."""
    if proposto in _DESTINOS and CONVICCAO.get(proposto, 0) < CONVICCAO.get(original, 0):
        return proposto
    return original


def destino_rebaixar(nivel: str, n_a_favor: int, n_contra: int) -> str:
    """Para onde `rebaixar` leva o nível. alta/baixa: `media` se ainda há fontes independentes na
    direção do nível e elas superam as contrárias (evidência coerente que sobra, mas sem força para
    cravar); `indeterminada` se as fontes se contradizem (contrárias >= a favor) ou não sobra nenhuma.
    media -> indeterminada. indeterminada fica."""
    if nivel in ("alta", "baixa"):
        return "media" if n_a_favor > n_contra else "indeterminada"
    if nivel == "media":
        return "indeterminada"
    return nivel


# ----------------------------------------------------------------------------- schema da resposta
def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


class RespostaSintese(BaseModel):
    consistente: bool = True
    conflitos: List[str] = []
    acao: Literal["manter", "rebaixar", "buscar_mais"] = "manter"
    motivo: str = ""

    @field_validator("acao", mode="before")
    @classmethod
    def _acao(cls, v):
        k = re.sub(r"[\s\-]+", "_", _sem_acento(str(v if v is not None else "")))
        if k.startswith("rebaix"):
            return "rebaixar"
        if k.startswith("buscar"):
            return "buscar_mais"
        if k.startswith("mant"):
            return "manter"
        raise ValueError(f"acao deve ser manter, rebaixar ou buscar_mais (veio {v!r})")

    @field_validator("conflitos", mode="before")
    @classmethod
    def _conflitos(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, (str, dict)):
            v = [v]
        return [json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else str(x) for x in v]

    @field_validator("motivo", mode="before")
    @classmethod
    def _motivo(cls, v):
        return "" if v is None else str(v)


INSTRUCAO = """Você revisa o CONJUNTO de julgamentos feitos sobre uma afirmação. Cada item abaixo é uma página que um juiz já classificou, isoladamente, frente à AFIRMAÇÃO. Sua tarefa é dizer se o conjunto é coerente o bastante para sustentar a DECISÃO ATUAL.

AFIRMAÇÃO: "{afirmacao}"
DECISÃO ATUAL: {decisao}

Classes do juiz: SUSTENTA (a página afirma o mesmo que a afirmação), REFUTA (a página diz que a afirmação é falsa, enganosa ou sem comprovação), RELATA_SEM_ENDOSSO (trata do assunto ou de algo parecido sem tomar posição), NAO_TRATA (outro assunto).

Procure, de forma geral:
1. Páginas EQUIVALENTES (mesmo fato, mesma informação, mesma conclusão) que receberam classes diferentes. Exemplo genérico: os itens 1 e 3 dizem a mesma coisa sobre o mesmo fato, mas um é SUSTENTA e o outro RELATA_SEM_ENDOSSO.
2. REFUTA ou SUSTENTA por inferência ou por silêncio: a página não contesta (ou não confirma) a afirmação, apenas diz algo relacionado, ou não a menciona, e o juiz deduziu a posição. Exemplo genérico: "o remédio X tem efeitos colaterais" não é prova de que "X não cura Y".
3. Alegação checada DIFERENTE da afirmação: o selo, o título ou o que a página diz ("pagina_diz") trata de uma variante (outro sujeito, número, data, lugar ou efeito).
4. Decisão apoiada numa única fonte, sobretudo se ela não for agência de checagem, tiver citação genérica ou raciocínio hipotético, ou se as demais páginas não a confirmam.
5. Raciocínio que contradiz a classe, ou citação que não sustenta a classe.

Regras:
- Use só os itens abaixo; não use conhecimento próprio nem invente fatos novos.
- Você NÃO muda a direção da decisão: só indica se ela pode ser mantida ou se a confiança deve ser reduzida.
- "manter": o conjunto é coerente para a decisão atual. Divergência entre páginas que tratam de aspectos diferentes não é conflito. Não invente conflitos.
- "rebaixar": há conflito real (itens 1 a 5) que tira a base da decisão atual; a confiança deve cair.
- "buscar_mais": o conjunto é insuficiente ou ambíguo e uma nova busca provavelmente esclareceria.
- "conflitos": lista curta (até 5) de frases, cada uma citando os índices "i" dos itens envolvidos. Vazia se não houver.
- "consistente": true se não há conflito real; false caso contrário.
- "motivo": uma frase curta, em português, explicando a ação.
- Responda SOMENTE com JSON, neste formato:
{{"consistente": true, "conflitos": [], "acao": "manter", "motivo": "<frase curta>"}}

ITENS:
{itens}"""


# ----------------------------------------------------------------------------- entrada
def _curto(s: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def _eh_agencia(p: Dict[str, Any]) -> bool:
    return p.get("tipo_portal") == "checagem" or bool(p.get("veredito"))


def montar_itens(ai: int, pecas: List[Dict[str, Any]], julg: Dict[Tuple[int, int], Dict[str, Any]],
                 limite: int = 0) -> List[Dict[str, Any]]:
    """Itens julgados da afirmação `ai` (classe não None), na ordem: os que tomam posição ou tratam do
    assunto primeiro, depois NAO_TRATA (que entram para o sintetizador achar equivalentes), até `limite`.
    O índice `i` é o do item na lista enviada ao LLM."""
    brutos = []
    for (pi, a), r in julg.items():
        if a != ai or r.get("classe") is None or pi >= len(pecas):
            continue
        p = pecas[pi]
        brutos.append({
            "classe": r["classe"], "veiculo": p.get("veiculo") or p.get("dominio") or "",
            "dominio": p.get("dominio") or "", "agencia_de_checagem": "sim" if _eh_agencia(p) else "nao",
            "data": p.get("data_pub") or "", "selo": p.get("veredito") or "",
            "pagina_diz": _curto(r.get("pagina_diz"), 200),
            "citacao": _curto(r.get("citacao"), 240), "raciocinio": _curto(r.get("raciocinio"), 300),
            "url": p.get("url") or ""})
    ordem = {"REFUTA": 0, "SUSTENTA": 0, "RELATA_SEM_ENDOSSO": 1, "NAO_TRATA": 2}
    brutos.sort(key=lambda d: (ordem.get(d["classe"], 3), d["url"]))
    limite = limite or config.SINTETIZADOR_MAX_ITENS or 12
    itens = brutos[:limite]
    for i, d in enumerate(itens):
        d["i"] = i
    return itens


def _decisao_texto(dec: Any, ai: int) -> str:
    """Descreve a decisão atual SEM números do agregador: nível e as classes que a apoiam."""
    sinal = 1 if dec.nivel == "alta" else -1 if dec.nivel == "baixa" else 0
    votos = [v for v in dec.votos if v.afirmacao == ai and (sinal == 0 or v.valor * sinal > 0)]
    classes = sorted({c for v in votos for c in v.classes if c in ("SUSTENTA", "REFUTA")})
    nome = {"alta": "ALTA propensão de ser desinformação (as fontes contestam a afirmação)",
            "baixa": "BAIXA propensão de ser desinformação (as fontes confirmam a afirmação)",
            "media": "propensão MÉDIA (evidência fraca ou dividida)"}.get(dec.nivel, dec.nivel)
    apoio = f"; apoiada por {len(votos)} fonte(s) independente(s) com as classes {', '.join(classes) or 'nenhuma'}"
    return nome + (apoio if votos else "")


def _contagem_direcao(dec: Any, ai: int) -> Tuple[int, int]:
    """(clusters a favor da direção do nível, clusters contra) na afirmação `ai`."""
    sinal = 1 if dec.nivel == "alta" else -1
    afs = [v for v in dec.votos if v.afirmacao == ai and v.valor != 0]
    return (sum(1 for v in afs if v.valor * sinal > 0), sum(1 for v in afs if v.valor * sinal < 0))


# ----------------------------------------------------------------------------- LLM
@dataclass
class Sintese:
    afirmacao: int
    acao: str = "manter"             # a ação EFETIVA (depois das guardas); a do LLM fica em `acao_llm`
    acao_llm: str = ""
    consistente: Optional[bool] = None
    conflitos: List[str] = field(default_factory=list)
    motivo: str = ""
    modelo: str = ""
    erro: Optional[str] = None       # motivo do fallback (manter)


def sintetizar(ai: int, afirmacao: str, decisao_txt: str, itens: List[Dict[str, Any]]) -> Sintese:
    """Uma chamada LLM. Nunca levanta: qualquer falha vira `manter` + fallback."""
    if not itens:
        return Sintese(afirmacao=ai, erro="sem itens julgados")
    linhas = "\n".join(json.dumps({k: v for k, v in d.items() if k != "url" and v != ""},
                                  ensure_ascii=False) for d in itens)
    prompt = INSTRUCAO.format(afirmacao=_curto(afirmacao, 500), decisao=decisao_txt, itens=linhas)
    try:
        res = llm.chat_json([{"role": "user", "content": prompt}], RespostaSintese, finalidade="sintetizador",
                            max_tokens=config.SINTETIZADOR_MAX_TOKENS or 1200,
                            timeout_s=config.SINTETIZADOR_TIMEOUT_S or 0)
    except Exception as e:  # chat_json não levanta; defesa para o contrato "nunca levanta"
        telemetria.fallback("sintetizador", f"{type(e).__name__}: {str(e)[:200]}", afirmacao=ai)
        return Sintese(afirmacao=ai, erro=f"{type(e).__name__}: {str(e)[:200]}")
    if not res.ok:
        telemetria.fallback("sintetizador", f"LLM indisponível ou JSON inválido: {(res.erro or '')[:200]}",
                            afirmacao=ai)
        return Sintese(afirmacao=ai, erro=(res.erro or "LLM indisponível")[:300])
    r: RespostaSintese = res.dados
    s = Sintese(afirmacao=ai, acao=r.acao, acao_llm=r.acao, consistente=r.consistente,
                conflitos=[_curto(c, 300) for c in r.conflitos[:5]], motivo=_curto(r.motivo, 300),
                modelo=res.modelo)
    if r.acao == "rebaixar" and r.consistente and not s.conflitos:
        # Veto sem conflito declarado não é base para rebaixar: não derruba a decisão do agregador.
        s.acao, s.erro = "manter", "rebaixar sem conflito declarado (consistente=true e conflitos vazios)"
        telemetria.fallback("sintetizador", s.erro, afirmacao=ai)
    return s


# ----------------------------------------------------------------------------- aplicação
def _frase_rebaixou(antes: str, depois: str) -> str:
    return (f"Ao comparar as fontes entre si, foram apontadas inconsistências entre elas; por isso a "
            f"propensão foi reduzida de {_NOME[antes]} para {_NOME[depois]}.")


def aplicar(dec: Any, afs: List[Any], pecas: List[Dict[str, Any]],
            julg: Dict[Tuple[int, int], Dict[str, Any]], limitacoes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Roda o sintetizador sobre `dec` (saída do agregador) e aplica a trava. Muta `dec` só para
    REBAIXAR (nível, motivo, travas, parametros) e devolve o resumo gravado no evento `sintetizador`.
    Só chama o LLM se o nível não é `indeterminada` e para as afirmações que votaram e entram na
    conjunção (nada a rebaixar nas outras). Nunca levanta; em qualquer falha `dec` fica como estava."""
    nivel_antes = dec.nivel
    resumo: Dict[str, Any] = {"nivel_antes": nivel_antes, "nivel_depois": nivel_antes, "efeito": "nenhum",
                              "afirmacoes": []}
    try:
        if nivel_antes == "indeterminada":
            resumo["efeito"] = "pulado: nível já indeterminado"
            return resumo
        propostas: List[str] = []
        buscar_mais: List[int] = []
        for ai, af in enumerate(afs):
            info = dec.por_afirmacao[ai] if ai < len(dec.por_afirmacao) else {}
            if not info.get("n_votos") or "fora_da_conjuncao" in info:
                continue
            itens = montar_itens(ai, pecas, julg)
            s = sintetizar(ai, af.alvo(), _decisao_texto(dec, ai), itens)
            entrada = {"afirmacao": ai, "n_itens": len(itens),
                       "classes": {c: sum(1 for d in itens if d["classe"] == c)
                                   for c in ("SUSTENTA", "REFUTA", "RELATA_SEM_ENDOSSO", "NAO_TRATA")},
                       "agencias": sum(1 for d in itens if d["agencia_de_checagem"] == "sim")}
            if s.acao == "rebaixar":
                a_favor, contra = _contagem_direcao(dec, ai)
                propostas.append(destino_rebaixar(nivel_antes, a_favor, contra))
            elif s.acao == "buscar_mais":
                buscar_mais.append(ai)
            resumo["afirmacoes"].append({**entrada, "acao": s.acao, "acao_llm": s.acao_llm,
                                         "consistente": s.consistente, "conflitos": s.conflitos,
                                         "motivo": s.motivo, "erro": s.erro, "modelo": s.modelo})
        novo = nivel_antes
        for p in propostas:  # a proposta mais baixa vence; `travar_nivel` garante que nunca sobe
            novo = travar_nivel(novo, p)
        resumo["buscar_mais"] = buscar_mais
        if novo != nivel_antes:
            conflitos = [c for a in resumo["afirmacoes"] if a["acao"] == "rebaixar" for c in a["conflitos"]]
            dec.nivel = novo
            dec.travas["sintetizador_rebaixou"] = True
            dec.parametros["sintetizador"] = {"nivel_antes": nivel_antes, "conflitos": conflitos[:5],
                                              "frase": _frase_rebaixou(nivel_antes, novo)}
            if novo == "indeterminada":
                dec.motivo = ("a comparação entre as fontes apontou inconsistências que tiram a base da "
                              "decisão: não sobrou evidência coerente suficiente")
            else:
                dec.motivo = "evidência fraca ou dividida (a comparação entre as fontes apontou inconsistências)"
            if limitacoes is not None:
                limitacoes.append(_frase_rebaixou(nivel_antes, novo))
            resumo["nivel_depois"], resumo["efeito"] = novo, f"rebaixado: {nivel_antes} -> {novo}"
        elif propostas:
            resumo["efeito"] = "rebaixar pedido, sem efeito (já no piso)"
        if buscar_mais:
            dec.travas["sintetizador_buscar_mais"] = True
            if limitacoes is not None:
                limitacoes.append("A comparação entre as fontes indicou que uma busca adicional poderia "
                                  "esclarecer o resultado; ela não foi feita nesta consulta.")
            if resumo["efeito"] == "nenhum":
                resumo["efeito"] = "buscar_mais sinalizado (nível mantido)"
        return resumo
    except Exception as e:  # o sintetizador nunca derruba o pipeline: o nível do agregador fica
        telemetria.fallback("sintetizador", f"aplicar: {type(e).__name__}: {str(e)[:200]}")
        resumo["erro"] = f"{type(e).__name__}: {str(e)[:200]}"
        return resumo


def rodar(dec: Any, afs: List[Any], pecas: List[Dict[str, Any]], julg: Dict[Tuple[int, int], Dict[str, Any]],
          limitacoes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ponto de entrada do pipeline: `aplicar` + evento de telemetria `sintetizador`."""
    resumo = aplicar(dec, afs, pecas, julg, limitacoes)
    telemetria.evento("sintetizador", **resumo)
    return resumo
