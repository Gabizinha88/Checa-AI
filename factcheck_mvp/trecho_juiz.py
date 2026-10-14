"""Trecho que o juiz lê (Missão F / A4; auditoria E, seção 3). ÚNICA fonte da verdade.

O juiz recebe `título + trecho`. Antes havia duas montagens: `aprofundar.montar_trecho` (com marcador
de conclusão, mas o resultado só alimentava a telemetria) e `juiz_llm.montar_trecho` (a que o prompt
usa, sem conclusão e sem limpeza de rodapé). Aqui fica uma só, com regras GERAIS de jornalismo de
checagem em PT-BR (nada específico de caso):

1. tira o título repetido no corpo e o boilerplate (links "Leia também", compartilhar, publicidade,
   chamadas de rede social/newsletter, rodapé legal, barras de menu);
2. se o que sobra cabe no limite, vai inteiro;
3. senão: título + lide (primeiras frases) + frases de CONCLUSÃO/VEREDITO ("Conclusão", "é falso",
   "#FAKE", "não há evidências", selo...) + frases com termos da afirmação, em ordem original,
   separadas por "…", sempre dentro do limite.

Veredito sem nenhum termo da afirmação só entra se estiver no fim da matéria (lá é onde a checagem
conclui) ou sob um cabeçalho de conclusão, e em número pequeno: páginas de debate/FAQ têm vereditos de
OUTRAS alegações (achado E: "um marcador simples também puxaria esse ruído").

`JUIZ_TRECHO_MODO=antigo` devolve a seleção anterior (lide de 2 frases + frases com termos) para a
medição antes/depois.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

from . import config

_STOP = frozenset("""a o os as um uma de do da dos das em no na nos nas por pelo pela para com sem que se
e ou mas como mais foi sao ser ter tem esta este isso essa ao aos nao sim sobre entre quando todo toda
""".split())


# ----------------------------------------------------------------------------- texto
def _fold(s: str) -> str:
    """minúsculas sem acento; mantém pontuação (os marcadores usam ':' e '#')."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", _fold(s))).strip()


def termos(texto: str) -> set:
    return {t for t in _norm(texto).split() if len(t) >= 4 and t not in _STOP}


def _colapsar(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ----------------------------------------------------------------------------- boilerplate
# Só vale para blocos CURTOS (chamadas de rodapé/menu são curtas); parágrafo de matéria nunca é descartado
# por estas regras. Texto já em `_fold`.
_BP_MAX_CHARS = 300
_BP_INICIO = re.compile(
    r"^(?:"
    r"(?:leia|veja|confira|assista|ouca|acesse)\s*[:\-–]"
    r"|(?:leia|veja|confira|assista|ouca|acesse)\s+(?:tambem|mais|ainda|a seguir|abaixo|aqui|a materia"
    r"|a reportagem|o video|a integra|outras)\b"
    r"|saiba mais\b|mais sobre\s*[:\-–]|continue lendo\b|clique (?:aqui|no link|para)\b"
    r"|(?:continua apos a )?publicidade\s*$|anuncio\s*$|advertisement\s*$|conteudo patrocinado\b|patrocinado\s*$"
    r"|(?:materias?|noticias?|conteudos?|posts?|textos?) relacionad[oa]s?\b|relacionad[oa]s?\s*[:\-–]?$"
    r"|mais lidas?\b|voce (?:tambem )?(?:pode|vai) (?:gostar|se interessar)\b|talvez voce (?:goste|queira)\b"
    r"|compartilh(?:e|ar)\s*(?:$|:|(?:no|na|nas|nos|via|pelo|pela|em)\b|por (?:e-?mail|whatsapp|link)"
    r"|(?:esta|essa|este|esse) (?:materia|noticia|reportagem|texto|conteudo|artigo)\b)|compartilhamento\s*$"
    r"|(?:siga|curta|acompanhe)\s+(?:nos|a gente|nossa|nossas|nosso|nossos|o canal|a pagina|o perfil)\b"
    r"|receba (?:as |nossas |nossa |o |a )?(?:noticias|newsletter|alertas|atualizacoes|conteudo|boletim)\b"
    r"|assine\b|inscreva-?se\b|cadastre-?se\b|baixe (?:o|nosso) (?:app|aplicativo)\b"
    r"|como falar com\b|fale (?:com|conosco)\b|chame (?:a gente|no)\b"
    r"|(?:envie|mande|encaminhe) (?:sua|seu|suas|seus) (?:denuncia|duvida|sugestao|pergunta|pauta|foto|video)\b"
    r"|(?:entre|participe) (?:no|em|do|de) (?:nosso |nossa )?(?:grupo|canal|comunidade)\b"
    r"|(?:foto|imagem|credito|creditos|reproducao|fonte da imagem|ilustracao)\s*[:/]"
    r"|comentarios?\s*$|deixe seu comentario\b|escreva um comentario\b"
    r"|ja (?:e|sou) assinante\b|faca login\b|todos os direitos\b|copyright\b|politica de privacidade\b"
    r"|termos de uso\b|este site usa cookies\b|aceitar (?:todos )?(?:os )?cookies\b"
    r"|(?:tags?|topicos?|etiquetas?|palavras-chave)\s*:"
    r"|(?:reportagem|texto|edicao|revisao|producao|colaborou|colaboracao|apuracao)\s*[:/]"
    r")", re.I)
# Barra de compartilhamento/menu: bloco curto só de nomes de rede/UI.
_UI = frozenset("""menu buscar pesquisar home inicio proxima anterior voltar topo imprimir ouvir compartilhar
comentar facebook twitter x whatsapp telegram linkedin instagram tiktok youtube email e-mail copiar link
pinterest reddit threads kwai bluesky sms ler depois salvar favoritar seguir""".split())
# Marcadores de FIM de matéria: do primeiro deles em diante, só rodapé/listas de links.
_FIM_MATERIA = re.compile(
    r"^(?:todos os direitos\b|copyright\b|politica de privacidade\b|mais lidas?\b|(?:materias?|noticias?|conteudos?) "
    r"relacionad[oa]s?\b|voce tambem pode gostar\b|talvez voce goste\b|comentarios?\s*$|deixe seu comentario\b"
    r"|como falar com\b|receba nossa newsletter\b)", re.I)
_LISTA_LINKS = re.compile(r"^(?:leia|veja|confira)\s+(?:tambem|mais|ainda)\b|^(?:materias?|noticias?|conteudos?) "
                          r"relacionad[oa]s?\b|^mais lidas?\b", re.I)
_SO_URL = re.compile(r"^(?:https?://\S+|www\.\S+)$", re.I)


def _eh_ui(f: str) -> bool:
    toks = re.findall(r"[a-z0-9\-]+", f)
    return 0 < len(toks) <= 8 and all(t in _UI for t in toks)


def _eh_boilerplate(texto: str) -> bool:
    if len(texto) > _BP_MAX_CHARS:
        return False
    if "©" in texto:
        return True
    f = _fold(texto).strip(" \t•·▪►»›|-–—*")
    if not f or _SO_URL.match(f) or _eh_ui(f):
        return True
    if f[0] in "\"“«'‘":  # citação do conteúdo checado (boato transcrito): nunca é rodapé
        return False
    return bool(_BP_INICIO.search(f))


def limpar_boilerplate(paragrafos: List[str]) -> List[str]:
    """Remove rodapé/menu/links de outras matérias. Conservador: só blocos curtos."""
    n = len(paragrafos)
    fim = n
    for i, p in enumerate(paragrafos):  # rodapé de fim de matéria: só na segunda metade
        if i >= n * 0.5 and len(p) <= 200 and _FIM_MATERIA.search(_fold(p).strip(" \t•·▪►»›|-–—*")):
            fim = i
            break
    saida: List[str] = []
    em_lista = False  # logo após "Leia também": manchetes soltas (sem ponto final) são links, não texto
    for p in paragrafos[:fim]:
        if _eh_boilerplate(p):
            em_lista = bool(_LISTA_LINKS.search(_fold(p).strip(" \t•·▪►»›|-–—*")))
            continue
        if em_lista and len(p) <= 160 and not p.rstrip().endswith((".", "!", "?", '"', "”")):
            continue
        em_lista = False
        saida.append(p)
    return saida


# ----------------------------------------------------------------------------- conclusão / veredito
# Frases que dão o veredito (regras gerais de checagem em PT-BR). Texto em `_fold`.
_VEREDITO_FORTE = re.compile(
    r"\b(?:e|sao|era|foi|eram|continua|segue)\s+(?:totalmente |completamente |inteiramente |comprovadamente "
    r"|absolutamente |parcialmente )?(?:falso|falsa|falsos|falsas|verdadeiro|verdadeira|verdadeiros|verdadeiras"
    r"|enganoso|enganosa|enganosos|enganosas|fake|boato|mentira|montagem|fato)\b"
    r"|\bnao (?:e|eh) (?:verdade|verdadeir[oa]s?|fato|real|procedente)\b"
    r"|\be (?:verdade|mentira|falso|enganoso|fato) que\b"
    r"|#(?:fake|fato|verdade|falso|verdadeiro|enganoso|boato|montagem|semcontexto|distorcido|exagerado|golpe)\b"
    r"|\bnao ha (?:nenhum[ao]? |qualquer )?(?:evidencia|prova|comprovacao|registro|indicio|estudo|base|fundamento"
    r"|respaldo)"
    r"|\bsem (?:nenhuma |qualquer )?(?:evidencia|prova|comprovacao|fundamento|respaldo|base cientifica)"
    r"|\b(?:nao|nunca) (?:foi|ha|houve|existe|existiu|existem|existiram) (?:comprovad|confirmad|registrad|evidencia"
    r"|prova|estudo)"
    r"|\bnao (?:procede|se confirma|foi confirmad|se sustenta|tem (?:base|fundamento|comprovacao|respaldo))"
    r"|\b(?:desmente|desmentiu|desmentid[oa]s?)\b"
    r"|\b(?:conclusao|veredito|classificacao|selo)\s*[:\-–]"
    r"|\b(?:classificamos|classificad[oa]|rotulad[oa]|marcad[oa]) (?:como|de)\b"
    r"|\b(?:concluimos|concluiu|conclui-se|constatamos|verificamos que|apuramos que|averiguamos)\b", re.I)
# Palavra solta de veredito: só conta junto de termo da afirmação (ou em cabeçalho).
_VEREDITO_FRACO = re.compile(
    r"\b(?:falso|falsa|enganos[oa]|distorcid[oa]|descontextualizad[oa]|fora de contexto|sem contexto|manipulad[oa]"
    r"|adulterad[oa]|montagem|boato|mentira|desinformacao|verdadeir[oa]|comprovad[oa]|confirmad[oa])\b", re.I)
# Parágrafo que ABRE com cabeçalho/selo de conclusão: suas frases entram como conclusão.
_CABECALHO = re.compile(
    r"^(?:a |o )?(?:conclusao|veredito|resumo|em resumo|em sintese|classificacao|selo|comprova explica)\b"
    r"|^o que (?:a |o )?(?:diz|mostra|conclui|concluiu|apurou)\b"
    r"|^(?:falso|verdadeiro|enganoso|fake|fato|impreciso|exagerado|distorcido|sem contexto|nao e bem assim)"
    r"\s*(?:[:.!\-–]|$)", re.I)


# ----------------------------------------------------------------------------- unidades
def _paragrafos(corpo: str) -> List[str]:
    return [p for p in (_colapsar(x) for x in re.split(r"\n+", corpo or "")) if p]


def _sem_titulo(paras: List[str], titulo: str) -> List[str]:
    """Tira o título repetido (no início do corpo ou como parágrafo/linha sozinho)."""
    if not titulo:
        return paras
    nt = _norm(titulo)
    out: List[str] = []
    for i, p in enumerate(paras):
        if (i == 0 and p.lower().startswith(titulo.lower())
                and not (len(p) > len(titulo) and p[len(titulo)].isalnum())):
            resto = p[len(titulo):].lstrip(" :-–—|.")
            if not resto:
                continue
            p = resto
        elif len(nt) >= 12 and _norm(p) == nt:
            continue
        out.append(p)
    return out


_FIM_FRASE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÝÇ0-9\"“«'‘#(\[])")


def _quebrar_longa(s: str, teto: int = 500) -> List[str]:
    """Sentença sem pontuação (lista, legenda) é cortada em pedaços para não consumir o orçamento."""
    saida = []
    while len(s) > teto:
        corte = s.rfind(" ", int(teto * 0.6), teto)
        corte = corte if corte > 0 else teto
        saida.append(s[:corte].strip())
        s = s[corte:].strip()
    if s:
        saida.append(s)
    return saida


def _unidades(paras: List[str], titulo: str) -> List[Tuple[str, int]]:
    """Frases (texto, índice do parágrafo), sem repetição e sem a linha do título."""
    nt = _norm(titulo)
    vistas = set()
    out: List[Tuple[str, int]] = []
    for pi, p in enumerate(paras):
        for fr in _FIM_FRASE.split(p):
            for u in _quebrar_longa(fr.strip()):
                chave = _norm(u)
                if not chave or chave in vistas or (len(nt) >= 12 and chave == nt):
                    continue
                if _eh_boilerplate(u):  # "Leia também: ..." colado no meio de um parágrafo
                    continue
                vistas.add(chave)
                out.append((u, pi))
    return out


# ----------------------------------------------------------------------------- seleção
def _montar_novo(titulo: str, corpo: str, afirmacao: str, limite: int) -> str:
    cabeca = (titulo + "\n") if titulo else ""
    resto = max(0, limite - len(cabeca))
    paras = limpar_boilerplate(_sem_titulo(_paragrafos(corpo), titulo))
    unid = _unidades(paras, titulo)
    if not unid:
        return cabeca.strip()[:limite]
    if sum(len(t) + 1 for t, _ in unid) - 1 <= resto:
        return (cabeca + " ".join(t for t, _ in unid)).strip()[:limite]

    alvo = termos(afirmacao)
    n = len(unid)
    # parágrafo que abre com cabeçalho/selo, ou que vem logo depois de um cabeçalho solto ("Conclusão" sozinho)
    eh_cab = [bool(_CABECALHO.match(_fold(p).strip(" \t*#>-–—"))) for p in paras]
    cab_par = {pi: eh_cab[pi] or (pi > 0 and eh_cab[pi - 1] and len(paras[pi - 1]) <= 40)
               for pi in {pi for _, pi in unid}}
    cands: List[Tuple[float, int, bool]] = []  # (score, k, sem_termo)
    sobre: List[int] = []
    for k, (t, pi) in enumerate(unid):
        f = _fold(t)
        ov = len(alvo & termos(t)) if alvo else 0
        sobre.append(ov)
        forte = bool(_VEREDITO_FORTE.search(f))
        fraco = bool(_VEREDITO_FRACO.search(f))
        cab = cab_par.get(pi, False)
        no_fim = k >= n * 0.6
        base = (3 if cab else 0) + (3 if forte else 0) + (1 if fraco and ov else 0)
        if not base or not (ov or cab or (forte and no_fim)):
            continue
        score = base + 2 * min(1.0, ov / max(1, min(len(alvo), 5))) + (1 if no_fim else 0)
        cands.append((score, k, ov == 0))

    escolhidas = set()
    usado = 0

    def _cabe(k: int, teto: int) -> bool:
        return usado + len(unid[k][0]) + 3 <= teto

    # 1) lide: 1ª frase sempre; 2ª se couber em ~35% do orçamento
    escolhidas.add(0)
    usado = len(unid[0][0]) + 3
    if n > 1 and _cabe(1, int(resto * 0.35)):
        escolhidas.add(1)
        usado += len(unid[1][0]) + 3
    # 2) conclusão/veredito, até ~55% do orçamento (no máximo 2 sem termo da afirmação)
    sem_termo = 0
    for score, k, st in sorted(cands, key=lambda c: (-c[0], -c[1])):
        if k in escolhidas or (st and sem_termo >= 2) or not _cabe(k, int(resto * 0.55)):
            continue
        escolhidas.add(k)
        usado += len(unid[k][0]) + 3
        sem_termo += 1 if st else 0
    # 3) frases com termos da afirmação, mais termos primeiro
    for ov, k in sorted(((o, k) for k, o in enumerate(sobre) if o), key=lambda x: (-x[0], x[1])):
        if k in escolhidas or not _cabe(k, resto):
            continue
        escolhidas.add(k)
        usado += len(unid[k][0]) + 3
    # 4) sobra de orçamento: conclusões que não coubram no teto de 55%
    for score, k, st in sorted(cands, key=lambda c: (-c[0], -c[1])):
        if k in escolhidas or (st and sem_termo >= 2) or not _cabe(k, resto):
            continue
        escolhidas.add(k)
        usado += len(unid[k][0]) + 3
        sem_termo += 1 if st else 0

    partes: List[str] = []
    ant = -1
    for k in sorted(escolhidas):
        if ant >= 0 and k != ant + 1:
            partes.append("…")
        partes.append(unid[k][0])
        ant = k
    return (cabeca + " ".join(partes)).strip()[:limite]


# ----------------------------------------------------------------------------- modo antigo (medição)
def _montar_antigo(titulo: str, corpo: str, afirmacao: str, limite: int) -> str:
    """Seleção anterior à A4, preservada para `JUIZ_TRECHO_MODO=antigo`: título + corpo; corpo longo =
    lide (2 frases) + frases com mais termos da afirmação, na ordem original, separadas por ' … '."""
    corpo = _colapsar(corpo)
    if corpo.startswith(titulo) and titulo:
        corpo = corpo[len(titulo):].strip()
    cabeca = (titulo + "\n") if titulo else ""
    resto = max(0, limite - len(cabeca))
    if len(corpo) <= resto:
        return (cabeca + corpo).strip()
    frases = [f for f in re.split(r"(?<=[.!?])\s+", corpo) if f]
    alvo = termos(afirmacao)
    pontos = [(len(alvo & termos(f)), -i, i) for i, f in enumerate(frases)]
    escolhidas = {0, 1} & set(range(len(frases)))
    usado = sum(len(frases[i]) + 3 for i in escolhidas)
    for p, _, i in sorted(pontos, reverse=True):
        if i in escolhidas or p == 0:
            continue
        if usado + len(frases[i]) + 3 > resto:
            continue
        escolhidas.add(i)
        usado += len(frases[i]) + 3
    partes, ant = [], -1
    for i in sorted(escolhidas):
        if ant >= 0 and i != ant + 1:
            partes.append("…")
        partes.append(frases[i])
        ant = i
    return (cabeca + " ".join(partes))[:limite].strip()


def montar_trecho(titulo: str, corpo: str, afirmacao: str, limite: int = 0) -> str:
    """Título + trecho do corpo (≤ `limite`, padrão JUIZ_TRECHO_MAX) que o juiz vai ler. Ver o docstring do módulo."""
    limite = limite or config.JUIZ_TRECHO_MAX or 3000
    titulo = _colapsar(titulo)
    if (getattr(config, "JUIZ_TRECHO_MODO", "novo") or "novo").strip().lower() == "antigo":
        return _montar_antigo(titulo, corpo, afirmacao, limite)
    return _montar_novo(titulo, corpo, afirmacao, limite)
