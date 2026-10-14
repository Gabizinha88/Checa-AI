"""Sintetizador por afirmação (Missão F / A2): trava determinística (nunca sobe), ações, fallback, flag.

Sem rede: o LLM é falso (`llm._local`), a SerpAPI e o deep crawl dos testes de pipeline também.
"""
import asyncio
import itertools
import json
import re

import pytest

from factcheck_mvp import config, decisao, llm, sintetizador, telemetria
from factcheck_mvp import pipeline as pl
from factcheck_mvp.aprofundar import CorpoLido
from factcheck_mvp.catalogo import Catalogo
from factcheck_mvp.decisao import Voto
from factcheck_mvp.indice import Indice
from factcheck_mvp.modelo_fake import MockDetector
from factcheck_mvp.pipeline import Pipeline
from factcheck_mvp.schemas import Afirmacao, EntradaConsulta

NIVEIS = ("alta", "media", "baixa", "indeterminada")
CONV = {"alta": 2, "baixa": 2, "media": 1, "indeterminada": 0}


# ----------------------------------------------------------------------------- fakes
def _llm(respostas, vistos=None):
    """Substitui llm._local: devolve as respostas em sequência (a última se repete)."""
    fila = list(respostas)

    def fake(messages, max_tokens, timeout_s, finalidade):
        if vistos is not None:
            vistos.append((finalidade, messages))
        r = fila.pop(0) if len(fila) > 1 else fila[0]
        if isinstance(r, Exception):
            raise r
        return (r if isinstance(r, str) else json.dumps(r)), "fake"
    return fake


def _resp(acao, consistente=None, conflitos=None, motivo="m"):
    conflitos = ["itens 0 e 1: mesma informação com classes diferentes"] if conflitos is None else conflitos
    return {"consistente": (acao == "manter") if consistente is None else consistente,
            "conflitos": conflitos, "acao": acao, "motivo": motivo}


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    fb, ev = [], []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo="", /, **kw: fb.append((onde, str(motivo))))
    monkeypatch.setattr(telemetria, "evento", lambda tipo, /, **d: ev.append((tipo, d)))
    return {"fallbacks": fb, "eventos": ev}


def _voto(af, cluster, valor, classe):
    return Voto(afirmacao=af, cluster=cluster, direcao=1.0 if valor > 0 else -1.0, peso=abs(valor), valor=valor,
                urls=[f"https://{cluster}.com/a"], classes=[classe], vereditos=[], motivo=f"postura {classe}")


def _dec(nivel, n_favor=2, n_contra=0, n_afirmacoes=1):
    """Decisao montada à mão: `n_favor` clusters na direção do nível, `n_contra` na oposta (afirmação 0)."""
    sinal = -1.0 if nivel == "baixa" else 1.0
    votos = [_voto(0, f"f{k}", sinal * 1.0, "SUSTENTA" if sinal < 0 else "REFUTA") for k in range(n_favor)]
    votos += [_voto(0, f"c{k}", -sinal * 1.0, "REFUTA" if sinal < 0 else "SUSTENTA") for k in range(n_contra)]
    por_af = [{"afirmacao": f"a{i}", "nucleo": f"a{i}", "polaridade": "afirma", "L": 1.0,
               "n_votos": len(votos) if i == 0 else 1} for i in range(n_afirmacoes)]
    return decisao.Decisao(nivel=nivel, log_odds=1.5, prob=0.8, motivo="original", votos=votos,
                           por_afirmacao=por_af, travas={})


def _pecas_julg(n=3, classes=("REFUTA", "REFUTA", "RELATA_SEM_ENDOSSO")):
    pecas = [{"url": f"https://site{k}.com/a", "veiculo": f"Veiculo{k}", "dominio": f"site{k}.com",
              "tipo_portal": "checagem" if k == 0 else "", "data_pub": "2026-09-0%d" % (k + 1),
              "veredito": "FALSO" if k == 0 else None} for k in range(n)]
    julg = {(k, 0): {"classe": classes[k], "citacao": f"citação {k}", "raciocinio": f"raciocínio {k}",
                     "pagina_diz": f"diz {k}"} for k in range(n)}
    return pecas, julg


def _aplicar(nivel, resposta, n_favor=2, n_contra=0):
    dec = _dec(nivel, n_favor, n_contra)
    pecas, julg = _pecas_julg()
    lims = []
    resumo = sintetizador.aplicar(dec, [Afirmacao(texto="a0")], pecas, julg, lims)
    return dec, resumo, lims


# ----------------------------------------------------------------------------- trava pura
def test_travar_nivel_nunca_sobe_nem_inverte_o_lado():
    for original, proposto in itertools.product(NIVEIS, NIVEIS + ("", "absurdo", None)):
        out = sintetizador.travar_nivel(original, proposto)
        assert CONV[out] <= CONV[original], (original, proposto, out)
        assert out in (original, "media", "indeterminada")
        if original == "alta":
            assert out != "baixa"
        if original == "baixa":
            assert out != "alta"
        if original in ("media", "indeterminada"):
            assert out in (original, "indeterminada")


def test_travar_nivel_so_aceita_destino_mais_baixo():
    assert sintetizador.travar_nivel("alta", "media") == "media"
    assert sintetizador.travar_nivel("baixa", "indeterminada") == "indeterminada"
    assert sintetizador.travar_nivel("media", "alta") == "media"      # tentativa de subir: ignorada
    assert sintetizador.travar_nivel("media", "media") == "media"
    assert sintetizador.travar_nivel("indeterminada", "baixa") == "indeterminada"


def test_destino_rebaixar():
    d = sintetizador.destino_rebaixar
    assert d("alta", 1, 0) == "media" and d("baixa", 3, 1) == "media"
    assert d("alta", 1, 1) == "indeterminada" and d("alta", 0, 0) == "indeterminada"
    assert d("alta", 1, 2) == "indeterminada"
    assert d("media", 5, 0) == "indeterminada"
    assert d("indeterminada", 2, 0) == "indeterminada"


# ----------------------------------------------------------------------------- resposta do LLM
def test_resposta_normaliza_acao_e_conflitos():
    R = sintetizador.RespostaSintese
    assert R(acao="Rebaixar", conflitos="um conflito").conflitos == ["um conflito"]
    assert R(acao="buscar mais").acao == "buscar_mais" and R(acao="Mantém").acao == "manter"
    assert R(acao="manter", conflitos=[{"i": 1}]).conflitos == ['{"i": 1}']
    assert R(acao="manter", conflitos=None).conflitos == []
    with pytest.raises(ValueError):
        R(acao="subir")
    with pytest.raises(ValueError):
        R(acao=None)


# ----------------------------------------------------------------------------- aplicar: nível × ação
@pytest.mark.parametrize("nivel", NIVEIS)
@pytest.mark.parametrize("acao", ("manter", "rebaixar", "buscar_mais"))
def test_matriz_nivel_acao_nunca_sobe(ambiente, monkeypatch, nivel, acao):
    monkeypatch.setattr(llm, "_local", _llm([_resp(acao)]))
    dec, resumo, lims = _aplicar(nivel, acao)
    assert CONV[dec.nivel] <= CONV[nivel]
    assert dec.nivel in (nivel, "media", "indeterminada")
    if acao != "rebaixar" or nivel == "indeterminada":
        assert dec.nivel == nivel
    esperado = {"alta": "media", "baixa": "media", "media": "indeterminada", "indeterminada": "indeterminada"}
    if acao == "rebaixar":
        assert dec.nivel == esperado[nivel]
    assert dec.log_odds == 1.5 and dec.prob == 0.8   # o número é do agregador
    assert resumo["nivel_antes"] == nivel and resumo["nivel_depois"] == dec.nivel


def test_resposta_tentando_subir_o_nivel_e_ignorada(ambiente, monkeypatch):
    """Campos extras (nivel/propensao) no JSON do LLM não têm efeito: só `acao` conta."""
    r = {**_resp("manter"), "nivel": "alta", "propensao": "alta", "acao_nivel": "subir"}
    monkeypatch.setattr(llm, "_local", _llm([r]))
    dec, resumo, _ = _aplicar("media", r)
    assert dec.nivel == "media" and resumo["efeito"] == "nenhum"


def test_rebaixar_alta_para_media_com_evidencia_coerente(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar", motivo="uma fonte fraca")]))
    dec, resumo, lims = _aplicar("alta", None, n_favor=1, n_contra=0)
    assert dec.nivel == "media" and dec.travas["sintetizador_rebaixou"] is True
    assert resumo["efeito"] == "rebaixado: alta -> media"
    assert any("reduzida de alta para média" in l for l in lims)
    assert "sintetizador" in dec.parametros and dec.parametros["sintetizador"]["nivel_antes"] == "alta"
    # justificativa e texto: aparece a redução; o agregador segue dono do número
    assert "reduzida de alta para média" in dec.justificativa()


def test_rebaixar_alta_para_indeterminada_quando_as_fontes_se_contradizem(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar")]))
    dec, resumo, lims = _aplicar("alta", None, n_favor=1, n_contra=1)
    assert dec.nivel == "indeterminada"
    assert "não sobrou evidência coerente" in dec.motivo
    assert "não sobrou evidência coerente" in dec.justificativa()


def test_rebaixar_baixa_vai_a_media_ou_indeterminada_nunca_a_alta(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar")]))
    dec, _, _ = _aplicar("baixa", None, n_favor=2, n_contra=0)
    assert dec.nivel == "media"
    dec, _, _ = _aplicar("baixa", None, n_favor=1, n_contra=1)
    assert dec.nivel == "indeterminada"


def test_buscar_mais_so_sinaliza(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("buscar_mais", conflitos=[], consistente=False)]))
    dec, resumo, lims = _aplicar("alta", None)
    assert dec.nivel == "alta" and dec.travas["sintetizador_buscar_mais"] is True
    assert "sintetizador_rebaixou" not in dec.travas
    assert resumo["buscar_mais"] == [0] and "buscar_mais sinalizado" in resumo["efeito"]
    assert any("busca adicional" in l for l in lims)


def test_json_invalido_vira_manter_com_fallback(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm(["isto não é json", "continua sem json"]))
    dec, resumo, lims = _aplicar("alta", None)
    assert dec.nivel == "alta" and resumo["efeito"] == "nenhum" and lims == []
    assert resumo["afirmacoes"][0]["acao"] == "manter" and resumo["afirmacoes"][0]["erro"]
    assert any(onde == "sintetizador" for onde, _ in ambiente["fallbacks"])


def test_acao_desconhecida_vira_manter_com_fallback(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([{"consistente": True, "conflitos": [], "acao": "subir", "motivo": ""}]))
    dec, _, _ = _aplicar("media", None)
    assert dec.nivel == "media"
    assert any(onde == "sintetizador" for onde, _ in ambiente["fallbacks"])


def test_llm_indisponivel_vira_manter_com_fallback(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([llm.ErroLLM("sem modelo")]))
    dec, resumo, _ = _aplicar("alta", None)
    assert dec.nivel == "alta" and resumo["afirmacoes"][0]["acao"] == "manter"
    assert any(onde == "sintetizador" for onde, _ in ambiente["fallbacks"])


def test_rebaixar_sem_conflito_declarado_nao_rebaixa(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar", consistente=True, conflitos=[])]))
    dec, resumo, _ = _aplicar("alta", None)
    assert dec.nivel == "alta"
    a = resumo["afirmacoes"][0]
    assert a["acao_llm"] == "rebaixar" and a["acao"] == "manter"
    assert any(onde == "sintetizador" and "sem conflito" in m for onde, m in ambiente["fallbacks"])


def test_excecao_ao_aplicar_deixa_o_nivel_e_registra_fallback(ambiente, monkeypatch):
    monkeypatch.setattr(sintetizador, "montar_itens", lambda *a, **k: 1 / 0)
    dec, resumo, _ = _aplicar("alta", None)
    assert dec.nivel == "alta" and "ZeroDivisionError" in resumo["erro"]
    assert any(onde == "sintetizador" for onde, _ in ambiente["fallbacks"])


def test_indeterminada_nao_chama_o_llm(ambiente, monkeypatch):
    vistos = []
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar")], vistos))
    dec, resumo, _ = _aplicar("indeterminada", None)
    assert vistos == [] and dec.nivel == "indeterminada" and "pulado" in resumo["efeito"]


def test_varias_afirmacoes_a_mais_baixa_vence_e_ignora_fora_da_conjuncao(ambiente, monkeypatch):
    vistos = []
    # af0 pede manter; af1 pede rebaixar; af2 está fora da conjunção (nem chama o LLM)
    monkeypatch.setattr(llm, "_local", _llm([_resp("manter"), _resp("rebaixar")], vistos))
    dec = _dec("alta", 2, 0, n_afirmacoes=3)
    dec.votos.append(_voto(1, "g0", 1.0, "REFUTA"))
    dec.por_afirmacao[1]["n_votos"] = 1
    dec.por_afirmacao[2]["fora_da_conjuncao"] = "só evidência de outro período"
    pecas, julg = _pecas_julg()
    julg.update({(k, 1): dict(julg[(k, 0)]) for k in range(3)})
    resumo = sintetizador.aplicar(dec, [Afirmacao(texto=f"a{i}") for i in range(3)], pecas, julg, [])
    assert len(vistos) == 2 and [a["afirmacao"] for a in resumo["afirmacoes"]] == [0, 1]
    assert dec.nivel == "media"   # af1: 1 cluster a favor, 0 contra


def test_prompt_leva_todos_os_campos_e_as_regras(ambiente, monkeypatch):
    vistos = []
    monkeypatch.setattr(llm, "_local", _llm([_resp("manter")], vistos))
    _aplicar("alta", None)
    fin, msgs = vistos[0]
    prompt = msgs[-1]["content"]
    assert fin == "sintetizador" and len(vistos) == 1
    linhas = [json.loads(l) for l in prompt.split("ITENS:\n", 1)[1].splitlines() if l.strip()]
    assert len(linhas) == 3
    primeiro = linhas[0]
    assert primeiro["classe"] == "REFUTA" and primeiro["agencia_de_checagem"] == "sim"
    assert primeiro["citacao"] and primeiro["raciocinio"] and primeiro["veiculo"] and primeiro["data"]
    assert linhas[2]["agencia_de_checagem"] == "nao"
    for trecho in ("EQUIVALENTES", "inferência ou por silêncio", "DIFERENTE da afirmação", "única fonte",
                   "NÃO muda a direção", '"acao": "manter"'):
        assert trecho in prompt
    assert "ALTA propensão" in prompt


def test_montar_itens_ordem_e_limite():
    pecas = [{"url": f"https://s{k}.com/a", "veiculo": f"V{k}"} for k in range(6)]
    classes = ["NAO_TRATA", "RELATA_SEM_ENDOSSO", "SUSTENTA", "NAO_TRATA", "REFUTA", None]
    julg = {(k, 0): {"classe": c} for k, c in enumerate(classes)}
    julg[(3, 1)] = {"classe": "REFUTA"}   # outra afirmação: fora
    itens = sintetizador.montar_itens(0, pecas, julg)
    assert [d["classe"] for d in itens] == ["SUSTENTA", "REFUTA", "RELATA_SEM_ENDOSSO", "NAO_TRATA", "NAO_TRATA"]  # por classe, depois URL
    assert [d["i"] for d in itens] == [0, 1, 2, 3, 4]
    assert len(sintetizador.montar_itens(0, pecas, julg, limite=2)) == 2


def test_evento_sintetizador_e_gravado_por_rodar(ambiente, monkeypatch):
    monkeypatch.setattr(llm, "_local", _llm([_resp("rebaixar")]))
    dec = _dec("alta", 1, 0)
    pecas, julg = _pecas_julg()
    sintetizador.rodar(dec, [Afirmacao(texto="a0")], pecas, julg, [])
    tipo, d = next(e for e in ambiente["eventos"] if e[0] == "sintetizador")
    assert d["nivel_antes"] == "alta" and d["nivel_depois"] == "media"
    assert d["afirmacoes"][0]["acao"] == "rebaixar" and d["afirmacoes"][0]["classes"]["REFUTA"] == 2


# ----------------------------------------------------------------------------- pipeline: flag ligada/desligada
def _org(url, titulo, snippet="snip"):
    return {"link": url, "title": titulo, "snippet": snippet, "displayed_link": url, "position": 1}


class FakeSerp:
    ativo = True
    ultimo_motivo = "ok"
    uso_hoje = 0

    def __init__(self, rota):
        self.rota = rota
        self.chamadas = []

    def buscar(self, params):
        self.chamadas.append(params["q"])
        r = self.rota(params["q"])
        return None if r is None else {"organic_results": r}

    def engine_de(self, q):
        return "google"

    def result_key(self, q):
        return "organic_results"


def _juiz_falso(chamadas, sintese):
    def fake(messages, max_tokens, timeout_s, finalidade):
        chamadas.append(finalidade)
        conteudo = messages[-1]["content"]
        if finalidade == "afirmacoes":
            texto = conteudo.split('"""')[1].strip()
            return json.dumps([{"afirmacao": texto, "nucleo": texto, "polaridade": "afirma",
                                "consulta": texto.lower()}]), "fake"
        if finalidade == "padroes":
            return "NENHUM", "fake"
        if finalidade == "sintetizador":
            r = sintese.pop(0) if len(sintese) > 1 else sintese[0]
            return (r if isinstance(r, str) else json.dumps(r)), "fake"
        itens = [json.loads(l) for l in conteudo.split("ITENS:\n", 1)[1].splitlines() if l.strip()]
        out = []
        for it in itens:
            m = re.search(r"[^.]*(é falso|não cura)[^.]*", f"{it['titulo']}\n{it['trecho']}", re.I)
            out.append({"i": it["i"], "classe": "REFUTA" if m else "NAO_TRATA",
                        "citacao": m.group(0).strip() if m else "", "raciocinio": "a página contesta"})
        return json.dumps({"itens": out}), "fake"
    return fake


@pytest.fixture
def pipe_amb(monkeypatch):
    monkeypatch.setenv("INDICE_CHECAGENS", "0")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "SERP_ESTRATEGIA", "agente")
    monkeypatch.setattr(config, "SERP_ENGINE", "google")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SITES", 0)
    corpos = {}

    async def fake_aprofundar(cands, catalogo, **kw):
        out = {}
        for d in cands:
            c = corpos.get(d["url"])
            out[d["url"]] = (CorpoLido(url=d["url"], erro="HTTP 403") if c is None else
                             CorpoLido(url=d["url"], final_url=d["url"], trecho_corpo=c[:3000], corpo_lido=True,
                                       texto_completo=c, metodo="fake"))
        return out
    monkeypatch.setattr(pl, "aprofundar", fake_aprofundar)
    eventos = []
    orig = telemetria.evento
    monkeypatch.setattr(telemetria, "evento", lambda tipo, /, **d: (eventos.append((tipo, d)), orig(tipo, **d))[1])
    return {"corpos": corpos, "eventos": eventos}


def _rodar_ibu(pipe_amb, monkeypatch, sintese, flag):
    monkeypatch.setattr(config, "SINTETIZADOR", flag)
    chamadas = []
    monkeypatch.setattr(llm, "_local", _juiz_falso(chamadas, sintese))
    pecas = []
    for url, tit, frase in (
            ("https://www.aosfatos.org/noticias/ibuprofeno-dengue/", "Ibuprofeno e dengue",
             "É falso que o ibuprofeno cura dengue, segundo a Anvisa."),
            ("https://www.bbc.com/portuguese/articles/ibu123", "O que se sabe sobre dengue",
             "Médicos reforçam que o ibuprofeno não cura dengue.")):
        dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        ctx = " ".join(f"Apuração própria de {dom}, parágrafo {k}, com detalhes do caso." for k in range(12))
        pipe_amb["corpos"][url] = f"{tit}. {ctx} {frase} Mais contexto de {dom} sem relação direta."
        pecas.append(_org(url, tit, frase[:120]))
    a, b = pecas
    serp = FakeSerp(lambda q: [b] if "site:" in q else [a])
    pipe = Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(), serpapi=serp, detector=MockDetector())
    rel = asyncio.run(pipe.executar(EntradaConsulta(tipo="titulo", conteudo="Ibuprofeno cura dengue")))
    return rel, chamadas


def test_pipeline_flag_desligada_e_identico_ao_agregador(pipe_amb, monkeypatch):
    rel, chamadas = _rodar_ibu(pipe_amb, monkeypatch, [_resp("rebaixar")], flag=False)
    assert rel.propensao == "alta"
    assert "sintetizador" not in chamadas
    assert not [e for e in pipe_amb["eventos"] if e[0] == "sintetizador"]
    dec_ev = next(d for t, d in pipe_amb["eventos"] if t == "decisao")
    assert dec_ev["nivel"] == dec_ev["nivel_agregador"] == "alta"
    assert "sintetizador_rebaixou" not in rel.decisao["travas"]


def test_pipeline_flag_ligada_manter_nao_muda_nada(pipe_amb, monkeypatch):
    rel, chamadas = _rodar_ibu(pipe_amb, monkeypatch, [_resp("manter")], flag=True)
    assert rel.propensao == "alta" and chamadas.count("sintetizador") == 1
    ev = next(d for t, d in pipe_amb["eventos"] if t == "sintetizador")
    assert ev["efeito"] == "nenhum" and ev["nivel_antes"] == ev["nivel_depois"] == "alta"


def test_pipeline_flag_ligada_rebaixa_alta_para_media(pipe_amb, monkeypatch):
    rel, chamadas = _rodar_ibu(pipe_amb, monkeypatch, [_resp("rebaixar")], flag=True)
    assert rel.propensao == "media" and rel.header.endswith("Propensão média de ser fake news")
    dec_ev = next(d for t, d in pipe_amb["eventos"] if t == "decisao")
    assert dec_ev["nivel"] == "media" and dec_ev["nivel_agregador"] == "alta"
    assert any("reduzida de alta para média" in l for l in rel.limitacoes)
    assert "reduzida de alta para média" in rel.justificativa
    assert rel.decisao["travas"]["sintetizador_rebaixou"] is True


def test_pipeline_flag_ligada_json_invalido_mantem_o_nivel(pipe_amb, monkeypatch):
    rel, _ = _rodar_ibu(pipe_amb, monkeypatch, ["lixo sem json"], flag=True)
    assert rel.propensao == "alta"
    assert any(t == "fallback" and d.get("onde") == "sintetizador" for t, d in pipe_amb["eventos"])


def test_pipeline_sem_llm_nao_chama_o_sintetizador(pipe_amb, monkeypatch):
    monkeypatch.setattr(config, "SINTETIZADOR", True)
    chamadas = []
    monkeypatch.setattr(llm, "_local", _juiz_falso(chamadas, [_resp("rebaixar")]))
    pipe = Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(), serpapi=FakeSerp(lambda q: []),
                    detector=MockDetector())
    rel = asyncio.run(pipe.executar(EntradaConsulta(tipo="titulo", conteudo="Ibuprofeno cura dengue"),
                                    usar_llm=False))
    assert "sintetizador" not in chamadas and rel.propensao == "indeterminada"
