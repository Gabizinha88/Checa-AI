"""Quebra do input em afirmações verificáveis (JSON), preservando idioma e polaridade.

Saída do LLM (validada com Pydantic, 1 retry com correção):
    [{"afirmacao": str, "nucleo": str, "polaridade": "afirma"|"nega", "consulta": str,
      "marcadores_tempo": [str]}]

- `afirmacao`: o que o USUÁRIO afirma, em português, com a polaridade dele
  ("É falso que a vacina altera o DNA" → "A vacina não altera o DNA").
- `nucleo`: a alegação na forma afirmativa ("A vacina altera o DNA"): é o que o
  juiz e os selos avaliam; `polaridade="nega"` faz a decisão inverter a direção.
- `consulta`: 5-8 palavras-chave em PT para a busca ("vacina covid altera DNA").
- `marcadores_tempo` (Missão F / item 7): "hoje", "acabou de sair"... fora do núcleo e da consulta; a
  `afirmacao` os preserva e a regra de data E4 os lê (`Afirmacao.texto_temporal`).

Nunca devolve vazio para entrada válida: CAIXA ALTA e pontuação excessiva são
normalizadas antes; LLM vazio/inválido → fallback determinístico, marcado como
`fallback-regex` (+ `telemetria.fallback`). Números iniciais são preservados
("30 mil" continua "30 mil").
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Literal, Optional

from pydantic import BaseModel, TypeAdapter, field_validator

from . import aplicabilidade, config, llm, telemetria
from .schemas import Afirmacao

# ----------------------------------------------------------------------------- normalização
_APELOS = re.compile(
    r"\b(compartilhe(m)?( já| agora| com todos)?|repasse(m)?( já)?|encaminhe(m)?|urgente|"
    r"não deixe de compartilhar|divulgue(m)?|espalhe(m)?)\b", re.I)
_SAUDACAO = re.compile(r"\b(bom dia|boa tarde|boa noite|olá|oi pessoal|inscreva-se)\b", re.I)
_MOLDURA_NEGA = re.compile(
    r"^\s*(?:(?:é|e)\s+(?:falso|falsa|mentira|fake|boato)|n[ãa]o\s+(?:é|e)\s+(?:verdade|fato|real)|"
    r"(?:é|e)\s+enganoso|desmentido\s*:|falso\s*:|fake\s*:)\s*(?:que\s+)?(.+)$", re.I | re.S)
_PERGUNTA = re.compile(r"^\s*(?:é\s+verdade\s+que|e\s+verdade\s+que|será\s+que|procede\s+que)\s+(.+?)\??\s*$",
                       re.I | re.S)

_STOP_PT = frozenset("""a o os as um uma uns umas de do da dos das em no na nos nas por pelo pela pelos pelas
para pra com sem que se e ou mas como mais menos foi eram será são é está estão esta este isto isso esse
essa aquele aquela muito tão já ainda também pode podem tem têm há ser ter seu sua seus suas ao aos à às
não sim sobre entre quando onde qual quais quem todo toda todos todas vai vão diz dizem afirma afirmam
""".split())
_STOP_EN = frozenset("the was were is are of and to in that for with has have been by on it this".split())


def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def normalizar_entrada(texto: str) -> str:
    """CAIXA ALTA → caixa de frase; '!!!'/'???' → um sinal; apelos removidos; espaços colapsados."""
    t = re.sub(r"\s+", " ", (texto or "")).strip()
    t = re.sub(r"([!?.,;:])[!?.,;:]+", r"\1", t)
    letras = [c for c in t if c.isalpha()]
    if len(letras) >= 8 and sum(c.isupper() for c in letras) / len(letras) >= 0.7:
        t = t.lower()
        t = re.sub(r"(^|[.!?]\s+)(\w)", lambda m: m.group(1) + m.group(2).upper(), t)
    t = _APELOS.sub(" ", t)
    t = _SAUDACAO.sub(" ", t)
    t = re.sub(r"\s+([!?.,;:])", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;:-–—")
    t = re.sub(r"^[!?.,;:]+\s*", "", t)
    return t.strip()


def _cap(s: str) -> str:
    s = (s or "").strip()
    return (s[0].upper() + s[1:]) if s else s


def palavras_chave(texto: str, n: int = 8) -> str:
    """Consulta determinística: tokens de conteúdo (números preservados), em ordem."""
    toks = re.findall(r"[\wÀ-ÿ]+", texto or "")
    out: List[str] = []
    for t in toks:
        tl = t.lower()
        if tl in _STOP_PT or (len(tl) < 3 and not tl.isdigit()):
            continue
        if tl in (x.lower() for x in out):
            continue
        out.append(t)
        if len(out) >= n:
            break
    return " ".join(out)


def _parece_ingles(s: str) -> bool:
    toks = re.findall(r"[a-zà-ÿ]+", (s or "").lower())
    en = sum(1 for t in toks if t in _STOP_EN)
    pt = sum(1 for t in toks if t in _STOP_PT)
    return en >= 2 and en > pt


_NEGACAO = re.compile(r"\b(n[ãa]o|nunca|jamais|nem|falso|falsa|mentira|fake|boato|desmentid\w*|engan\w*)\b", re.I)


def _tirar_nao(frase: str) -> Optional[str]:
    """'Café não cura câncer' -> 'Café cura câncer' (só com exatamente um 'não')."""
    if len(re.findall(r"\bn[ãa]o\b", frase, re.I)) != 1:
        return None
    return _cap(re.sub(r"\s*\bn[ãa]o\b\s*", " ", frase, flags=re.I).strip())


def _termos_inventados(consulta: str, texto: str) -> List[str]:
    """Termos da consulta (>= 4 letras) que não aparecem no texto, se forem > 1/3 dos termos."""
    base = _sem_acento(texto).lower()
    toks = [t for t in re.findall(r"[\wÀ-ÿ]+", consulta or "") if len(t) >= 4]
    fora = [t for t in toks if _sem_acento(t).lower()[:5] not in base]
    return fora if toks and len(fora) * 3 > len(toks) else []


def _moldura_negacao(texto: str) -> Optional[str]:
    """'É falso que X' / 'Não é verdade que X' → X (núcleo afirmativo)."""
    m = _MOLDURA_NEGA.match(texto or "")
    if not m:
        return None
    x = re.sub(r"[.!?]+$", "", m.group(1)).strip()
    return _cap(x) if len(x) >= 5 else None


# ----------------------------------------------------------------------------- limpeza (Missão F / item 7)
# Marcadores relativos de tempo: dizem QUANDO o fato ocorreu para quem escreve (regra de data E4), não
# QUAL é o fato. Dentro do núcleo vão ao juiz e ao índice; dentro da consulta zeram o AND do Google News.
_TEMPO = (r"acab(?:ou|a)(?:\s+de)?\s+sair|acab(?:ou|a)\s+de|h[áa]\s+(?:pouco|instantes|poucas\s+horas)|"
          r"(?:nesta|esta)\s+(?:manh[ãa]|tarde|noite|madrugada|semana)|neste\s+m[êe]s|semana\s+passada|"
          r"anteontem|ontem|hoje|agora")
# Fora de "até hoje", "a partir de agora", "nos dias de hoje", "hoje em dia", "há pouco mais de" (quantidade).
_RE_TEMPO = re.compile(
    r"(?<![\wÀ-ÿ])(?<!até )(?<!ate )(?<!partir de )(?<!de )(" + _TEMPO + r")(?![\wÀ-ÿ])"
    r"(?!\s+em\s+(?:dia|diante)\b)(?!\s+(?:mais|menos)\s+(?:de|que)\b)", re.I)
_TEMPO_ENTRE_VIRGULAS = re.compile(r"\s*,\s*(?:" + _TEMPO + r")\s*,\s*", re.I)  # "O ministro, hoje, foi preso"
_TEMPO_INICIAL = re.compile(r"^\s*(?:" + _TEMPO + r")\s*[:,;–—-]\s*", re.I)
_ACABOU_SAIR_QUE = re.compile(
    r"^\s*acab(?:ou|a)(?:\s+de)?\s+sair\s+(?:a\s+)?(?:not[ií]cia|informa[çc][ãa]o|confirma[çc][ãa]o)\s*"
    r"(?:de\s+)?(?:que\s+|:\s*)", re.I)
# Moldura de entrega ("Me mandaram isso:", "Vi um post dizendo que"): como a mensagem chegou, não o fato.
_MOLDURA_ENTREGA = re.compile(
    r"^\s*(?:me\s+(?:mandaram|enviaram|passaram|encaminharam)|recebi|acabei\s+de\s+(?:receber|ver|ler)|vi|li|ouvi)"
    r"(?:\s+(?:isso|aquilo|(?:essa|esta|esse|este|uma|um|o|a)\s+(?:mensagem|post|postagem|v[ií]deo|[áa]udio|"
    r"texto|foto|print|imagem|not[ií]cia|corrente|story|reel)))?"
    r"(?:\s+aqui)?(?:\s+(?:no|na|do|da|pelo|pela)\s+[\wÀ-ÿ]+)?"
    r"(?:\s*:\s*|\s*,\s*|\s+(?:dizendo|falando|afirmando|contando|avisando|informando|mostrando)(?:\s+que)?\s+|"
    r"\s+que\s+)", re.I)
# Ruído da consulta: veredito/pergunta, negação (a consulta é o tema, afirmativa), verbo de relato e "vídeo mostra".
_RE_RUIDO_CONSULTA = re.compile(
    r"(?<![\wÀ-ÿ])(?:é|n[ãa]o|nunca|jamais|nem|verdade|verdadeiro|falso|falsa|fake|boato|mentira|checagem|"
    r"disse|disseram|diz|dizem|dizendo|afirma|afirmou|afirmam|afirmando|mostra|mostram|mostrando|segundo|"
    r"circula|circulando|not[ií]cia|mensagem|mandaram|post)(?![\wÀ-ÿ])", re.I)
_RE_VIDEO_MOSTRA = re.compile(
    r"(?<![\wÀ-ÿ])(?:v[ií]deos?|[áa]udios?|print|post|foto|imagem|texto)\s+"
    r"(?:mostra|mostram|mostrando|diz|dizem|dizendo|afirma|afirmando|circula|viraliza)(?![\wÀ-ÿ])", re.I)


def marcadores_do_texto(texto: str) -> List[str]:
    """Marcadores relativos de tempo achados no texto, na ordem, minúsculos e sem repetir."""
    achados: List[str] = []
    for m in _RE_TEMPO.finditer(texto or ""):
        t = re.sub(r"\s+", " ", m.group(1).lower())
        if t not in achados:
            achados.append(t)
    return achados


def _arrumar(t: str) -> str:
    t = re.sub(r"\s+([,;:.!?])", r"\1", t)
    t = re.sub(r",(\s*,)+", ",", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;:–—-")
    return _cap(t)


def tirar_tempo_e_moldura(frase: str) -> tuple[str, List[str]]:
    """(frase sem moldura de entrega nem marcador de tempo, marcadores achados). Determinístico.
    Se a limpeza deixaria pouco (< 3 palavras), devolve a frase original: melhor ruído que perder o fato."""
    original = (frase or "").strip()
    marcadores = marcadores_do_texto(original)
    t = _MOLDURA_ENTREGA.sub("", original, count=1)
    t = _TEMPO_INICIAL.sub("", t, count=1)
    t = _TEMPO_ENTRE_VIRGULAS.sub(" ", t)
    t = _ACABOU_SAIR_QUE.sub("", t, count=1)
    t = _RE_TEMPO.sub(" ", t)
    t = _arrumar(t)
    return (t, marcadores) if len(t.split()) >= 3 else (original, marcadores)


def limpar_consulta(consulta: str) -> str:
    """Consulta sem marcadores de tempo, moldura, veredito, negação e verbos de relato (o Google News
    exige TODOS os termos: uma palavra de ruído zera a busca; auditoria E, 4.2)."""
    c = _RE_VIDEO_MOSTRA.sub(" ", consulta or "")
    c = _RE_TEMPO.sub(" ", c)
    c = _RE_RUIDO_CONSULTA.sub(" ", c)
    return re.sub(r"\s+", " ", c).strip()


def _marcadores_validos(*fontes) -> List[str]:
    """Une marcadores (do LLM e da regra), mantendo só os que a regra de data E4 reconhece como
    marcador (`aplicabilidade.marcador_temporal`); sem repetir, no máximo 5."""
    saida: List[str] = []
    for lista in fontes:
        for m in lista or []:
            m = re.sub(r"\s+", " ", str(m or "")).strip(" :,;.-").lower()
            if 0 < len(m) <= 40 and m not in saida and aplicabilidade.marcador_temporal(m) is not None:
                saida.append(m)
    return saida[:5]


# ----------------------------------------------------------------------------- LLM
class _ItemLLM(BaseModel):
    afirmacao: str
    nucleo: str = ""
    polaridade: Literal["afirma", "nega"] = "afirma"
    consulta: str = ""
    marcadores_tempo: List[str] = []

    @field_validator("marcadores_tempo", mode="before")
    @classmethod
    def _marc(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

    @field_validator("polaridade", mode="before")
    @classmethod
    def _pol(cls, v):
        v = _sem_acento(str(v or "afirma")).strip().lower()
        return "nega" if v.startswith("neg") else "afirma"


_INSTRUCAO = """Extraia as afirmações factuais verificáveis do TEXTO (português do Brasil).

Responda SOMENTE com um array JSON, sem texto antes ou depois. Cada item:
{{"afirmacao": "...", "nucleo": "...", "polaridade": "afirma" ou "nega", "consulta": "...", "marcadores_tempo": []}}

Regras:
1. "afirmacao": o que o TEXTO afirma, em português, com as palavras do texto, frase autocontida (sem pronomes soltos). Não acrescente fatos. NUNCA traduza para outro idioma. Preserve números, datas e marcadores de tempo exatamente como estão (ex.: "30 mil", "2022", "hoje", "agora", "acabou de sair", "ontem").
2. Negação: se o TEXTO nega uma alegação ("É falso que X", "Não é verdade que X", "X não faz Y"), "afirmacao" mantém a negação, "nucleo" é a alegação X na forma afirmativa (sem a negação) e "polaridade" é "nega". Se não há negação, "nucleo" repete "afirmacao" (sem os marcadores de tempo, regra 5) e "polaridade" é "afirma". A negação conta mesmo dentro de aspas ou depois de uma moldura ("Recebi isso: “É falso que X”" → polaridade "nega"). A pergunta final ("é verdade?") não é negação. Mas se a negação está DENTRO da oração relatada de uma atribuição ("a PF concluiu que não teve corrupção", "Fulano disse que não vai disputar"), ela faz parte do que foi atribuído: "afirmacao" e "nucleo" mantêm a frase inteira, com a negação, e a polaridade é "afirma". "nega" é só para quando o TEXTO rejeita a alegação.
3. Moldura x atribuição. (a) Descarte a MOLDURA de como a mensagem chegou ("me mandaram", "vi um post", "recebi no grupo", "estão compartilhando") e as perguntas ("é verdade?", "dá pra confiar?"): extraia só o conteúdo que ela traz. (b) PRESERVE a atribuição quando o conteúdo diz que uma pessoa, órgão ou estudo disse, escreveu, chamou, decidiu, concluiu ou mostrou algo ("Fulano disse que X", "a PF concluiu X", "estudo aponta X"): a afirmação e o núcleo são a frase inteira, com o autor, porque o que se checa é se ele de fato o fez. Só remova o relator quando for mera circulação sem autor identificado ("circula um áudio afirmando X", "dizem que X" → X).
4. "consulta": 5 a 8 palavras-chave em português, tiradas do núcleo, para buscar o tema no Google Notícias, que exige TODOS os termos (se o núcleo for curto, use todas as palavras importantes dele). Mantenha os acentos. Não invente termos que não estão no texto. Sem aspas, sem operadores e sem palavras-ruído: marcadores de tempo, moldura de entrega, "é verdade", "fake", "falso", "boato", "não" e verbos de relato sem autor ("mostra", "circula").
5. "nucleo" NÃO leva marcadores de tempo ("agora", "hoje", "ontem", "acabou de sair", "Agora:") nem moldura de entrega: eles dizem quando ou como a mensagem chegou, não qual é o fato. "marcadores_tempo" é a lista dos marcadores relativos de tempo do TEXTO que se referem a esta afirmação, como aparecem (ex.: ["hoje"]); [] se não há. Não coloque aí moldura de entrega nem datas absolutas.
6. Ignore saudações, apelos ("compartilhe", "urgente") e opiniões. No máximo {n} itens. Se não houver nenhuma afirmação factual, responda [].
7. Uma afirmação por alegação. Uma frase com várias orações (separadas por vírgula, "e", "enquanto", "só") que descreve um único acontecimento ou uma comparação é UMA afirmação: mantenha a frase inteira (causa e efeito, detalhes do mesmo fato, um boato contado em várias frases, o relato mais o seu conteúdo). Se a mesma frase traz duas ou mais alegações INDEPENDENTES (cada uma pode ser verdadeira ou falsa sozinha, sobre fatos ou sujeitos diferentes), devolva uma afirmação para cada, cada uma autocontida. NÃO divida quando as partes formam uma alegação só.

Exemplos:
TEXTO: "É falso que beber água gelada causa gripe"
[{{"afirmacao": "Beber água gelada não causa gripe", "nucleo": "Beber água gelada causa gripe", "polaridade": "nega", "consulta": "beber água gelada causa gripe", "marcadores_tempo": []}}]
TEXTO: "Estão compartilhando: “O ministro disse que não haverá aumento do imposto.” Isso procede?"
[{{"afirmacao": "O ministro disse que não haverá aumento do imposto", "nucleo": "O ministro disse que não haverá aumento do imposto", "polaridade": "afirma", "consulta": "ministro aumento imposto", "marcadores_tempo": []}}]
TEXTO: "Recebi no grupo: “Fulano disse que os idosos vão decidir a eleição.” Alguém sabe se é verdade?"
[{{"afirmacao": "Fulano disse que os idosos vão decidir a eleição", "nucleo": "Fulano disse que os idosos vão decidir a eleição", "polaridade": "afirma", "consulta": "Fulano idosos decidir eleição", "marcadores_tempo": []}}]
TEXTO: "Prefeitura do Recife abriu 30 mil vagas de emprego em 2025"
[{{"afirmacao": "A Prefeitura do Recife abriu 30 mil vagas de emprego em 2025", "nucleo": "A Prefeitura do Recife abriu 30 mil vagas de emprego em 2025", "polaridade": "afirma", "consulta": "Prefeitura Recife 30 mil vagas emprego 2025", "marcadores_tempo": []}}]
TEXTO: "Me mandaram um áudio: agora a empresa Alfa anunciou que vai demitir mil funcionários"
[{{"afirmacao": "Agora a empresa Alfa anunciou que vai demitir mil funcionários", "nucleo": "A empresa Alfa anunciou que vai demitir mil funcionários", "polaridade": "afirma", "consulta": "empresa Alfa demitir mil funcionários", "marcadores_tempo": ["agora"]}}]
TEXTO: "A ponte nova de Beta caiu e o prefeito de Gama foi preso"
[{{"afirmacao": "A ponte nova de Beta caiu", "nucleo": "A ponte nova de Beta caiu", "polaridade": "afirma", "consulta": "ponte nova Beta caiu", "marcadores_tempo": []}}, {{"afirmacao": "O prefeito de Gama foi preso", "nucleo": "O prefeito de Gama foi preso", "polaridade": "afirma", "consulta": "prefeito Gama preso", "marcadores_tempo": []}}]
TEXTO: "Circula um vídeo dizendo que o chá de erva Delta cura a gripe em dois dias, e que isso foi provado por uma universidade"
[{{"afirmacao": "O chá de erva Delta cura a gripe em dois dias, segundo estudo de uma universidade", "nucleo": "O chá de erva Delta cura a gripe em dois dias, segundo estudo de uma universidade", "polaridade": "afirma", "consulta": "chá erva Delta cura gripe dois dias", "marcadores_tempo": []}}]

TEXTO: \"\"\"
{texto}
\"\"\""""


_LISTA = TypeAdapter(List[_ItemLLM])


def _validar_itens(dados):
    """Aceita lista, {"afirmacoes": [...]} ou um item solto; exige português."""
    if isinstance(dados, dict):
        listas = [v for v in dados.values() if isinstance(v, list)]
        dados = listas[0] if ("afirmacao" not in dados and listas) else [dados]
    itens = _LISTA.validate_python(dados)
    ruins = [it.afirmacao for it in itens if _parece_ingles(it.afirmacao) or _parece_ingles(it.nucleo)]
    if ruins:
        raise ValueError(f"afirmações fora do português: {ruins[:2]}. Escreva em português, como no texto")
    return itens


def _via_llm(texto: str, max_n: int) -> tuple[List[Afirmacao], str]:
    res = llm.chat_json(
        [{"role": "user", "content": _INSTRUCAO.format(n=max_n, texto=texto[:4000])}],
        None, finalidade="afirmacoes", max_tokens=2500, validar=_validar_itens)
    if not res.ok:
        raise RuntimeError(res.erro or "LLM indisponível")
    saida: List[Afirmacao] = []
    moldura = _moldura_negacao(texto)
    for i, it in enumerate(res.dados[:max_n]):
        af = re.sub(r"\s+", " ", it.afirmacao).strip()
        if len(af) < 5:
            continue
        nucleo = re.sub(r"\s+", " ", it.nucleo or af).strip()
        pol = it.polaridade
        # Moldura explícita de negação no texto do usuário é determinística: vence o LLM
        # (o LLM local já removeu "É falso que" e passou a checar o próprio boato).
        if moldura and i == 0:
            pol, nucleo = "nega", moldura
            if not _NEGACAO.search(af):  # o LLM tirou a negação: a afirmação é a do usuário
                af = re.sub(r"[.!?]+$", "", texto.strip())
        elif pol == "nega" and not _NEGACAO.search(texto):
            # Iteração 1: "Vacina da covid altera o DNA humano" veio com polaridade "nega" -> sem
            # nenhuma negação no texto do usuário, a polaridade é "afirma" (senão a direção inverte).
            telemetria.fallback("afirmacoes.polaridade", "LLM marcou 'nega' sem negação no texto: afirma")
            pol, nucleo = "afirma", af
        elif pol == "nega" and _sem_acento(nucleo).lower().strip(" .") == _sem_acento(af).lower().strip(" ."):
            # núcleo não veio na forma afirmativa: deriva; se não der, julga o texto do usuário como está
            derivado = _moldura_negacao(af) or _tirar_nao(af)
            telemetria.fallback("afirmacoes.polaridade", "núcleo igual à afirmação negada: "
                                + ("derivado por regra" if derivado else "julgada como está (afirma)"))
            pol, nucleo = ("nega", derivado) if derivado else ("afirma", af)
        if pol == "afirma":
            nucleo = nucleo if nucleo and not _parece_ingles(nucleo) else af
        marcadores = _marcadores_validos(it.marcadores_tempo, marcadores_do_texto(af))
        if config.EXTRATOR_LIMPEZA:  # Missão F / item 7: núcleo sem tempo nem moldura (a afirmação os mantém)
            nucleo = tirar_tempo_e_moldura(nucleo)[0]
        consulta = (it.consulta or "").strip() or palavras_chave(nucleo)
        if len(consulta.split()) > 10:
            consulta = palavras_chave(consulta, 8)
        # A consulta é o TEMA: sem palavras de veredito (senão a busca vira "falso vacina…")
        consulta = re.sub(r"(?i)\b(falso|falsa|verdade|verdadeiro|boato|fake|mentira|checagem)\b", " ", consulta)
        consulta = re.sub(r"\s+", " ", consulta).strip() or palavras_chave(nucleo)
        if config.EXTRATOR_LIMPEZA:
            consulta = limpar_consulta(consulta)
            if len(consulta.split()) < 2:  # a limpeza esvaziou a consulta: palavras do núcleo limpo
                consulta = limpar_consulta(palavras_chave(nucleo)) or palavras_chave(nucleo)
        inventadas = _termos_inventados(consulta, f"{texto} {nucleo}")
        if inventadas:
            # Iteração 1: "Café cura câncer" -> "cafe cura cancer onco terapia" levou a busca a Google Books.
            telemetria.fallback("afirmacoes.consulta", f"consulta do LLM com termos fora do texto {inventadas}: "
                                "palavras-chave do núcleo", consulta_llm=consulta)
            consulta = limpar_consulta(palavras_chave(nucleo)) if config.EXTRATOR_LIMPEZA else palavras_chave(nucleo)
        saida.append(Afirmacao(texto=af[:500], indice=len(saida), nucleo=nucleo[:500],
                               polaridade=pol, consulta=consulta[:200], marcadores_tempo=marcadores))
    return saida, f"{res.motor}:{res.modelo}"


# ----------------------------------------------------------------------------- fallback
def _fallback(texto: str, max_n: int) -> List[Afirmacao]:
    """Determinístico: moldura de negação/pergunta + sentenças; consulta por palavras-chave."""
    t = normalizar_entrada(texto)
    if len(re.sub(r"[^\wÀ-ÿ]", "", t)) < 8 or len(palavras_chave(t).split()) < 2:
        return []  # só saudação/apelo: sem afirmação factual
    m = _PERGUNTA.match(t)
    if m:
        t = _cap(m.group(1))
    partes = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", t) if p and len(p.strip()) > 25]
    partes = [re.sub(r"[.!?]+$", "", p).strip() for p in partes] or [re.sub(r"[.!?]+$", "", t).strip()]
    saida: List[Afirmacao] = []
    for p in partes[:max_n]:
        nucleo, pol = p, "afirma"
        x = _moldura_negacao(p)
        if x:
            nucleo, pol = x, "nega"
        elif _tirar_nao(p):
            nucleo, pol = _tirar_nao(p), "nega"
        marcadores = _marcadores_validos(marcadores_do_texto(p))
        if config.EXTRATOR_LIMPEZA:
            nucleo = tirar_tempo_e_moldura(nucleo)[0]
        consulta = palavras_chave(nucleo)
        if config.EXTRATOR_LIMPEZA:
            consulta = limpar_consulta(consulta) or consulta
        saida.append(Afirmacao(texto=p[:500], indice=len(saida), nucleo=nucleo[:500], polaridade=pol,
                               consulta=consulta, marcadores_tempo=marcadores))
    return saida


def extrair_afirmacoes(texto: str, max_n: int = 0, usar_llm: bool = True) -> tuple[List[Afirmacao], str]:
    """Retorna (afirmacoes, motor). Motor honesto: `<provedor>:<modelo>` ou `fallback-regex`."""
    limite = max_n or config.MAX_AFIRMACOES
    limpo = normalizar_entrada(texto) or (texto or "").strip()
    if usar_llm:
        try:
            got, motor = _via_llm(limpo, limite)
            if got:
                return got, motor
            fb = _fallback(texto, limite)
            if fb:
                telemetria.fallback("afirmacoes", f"LLM ({motor}) devolveu vazio para entrada válida: regex")
            return fb, "fallback-regex"
        except Exception as e:
            telemetria.fallback("afirmacoes", f"{type(e).__name__}: {str(e)[:200]}")
    return _fallback(texto, limite), "fallback-regex"
