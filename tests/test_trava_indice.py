"""Missão F / A6: trava de similaridade no atalho do índice (`trava_indice`, ligada em `decisao._agregar`).

Selo do índice de UMA só checagem cuja alegação não é a do texto não crava alta/baixa sozinho: teto `media`.
Tudo sem rede; a flag `INDICE_TRAVA_SIMILARIDADE` (padrão 0) é testada ligada e desligada.
"""
import asyncio

import pytest

from factcheck_mvp import config, decisao, indice, telemetria, trava_indice
from factcheck_mvp.agregador import verificar_neutralidade
from factcheck_mvp.catalogo import Catalogo
from factcheck_mvp.decisao import AfirmacaoDecisao, Evidencias, ItemEvidencia, decidir
from factcheck_mvp.indice import Indice
from factcheck_mvp.modelo_fake import MockDetector
from factcheck_mvp.pipeline import Pipeline
from factcheck_mvp.schemas import EntradaConsulta

JUIZ = "llm-juiz:llm-local"
NUCLEO = "Café cura câncer de fígado em idosos"
PARAFRASE = ["Café cura o câncer de fígado em pessoas idosas"]
VIZINHA = ["Café cura câncer de próstata, diz estudo feito com ratos em laboratório no interior"]


@pytest.fixture
def ligada(monkeypatch):
    monkeypatch.setattr(config, "INDICE_TRAVA_SIMILARIDADE", True)
    monkeypatch.setattr(config, "INDICE_TRAVA_SIM_MIN", 0.6)


def _selo(url, alegacoes, cluster=None, classe="REFUTA", veredito="FALSO", origem="indice", curada=True):
    return ItemEvidencia(url=url, afirmacao=0, cluster=cluster or url, classe=classe, motor=JUIZ,
                         citacao_verificada=True, curada=curada, corpo_lido=True, veredito=veredito,
                         origem_veredito=origem, veiculo="Lupa", alegacoes_checadas=alegacoes)


def _postura(url, classe="REFUTA", cluster=None):
    return ItemEvidencia(url=url, afirmacao=0, cluster=cluster or url, classe=classe, motor=JUIZ,
                         citacao_verificada=True, curada=True, corpo_lido=True)


def _ev(itens, nucleo=NUCLEO, pol="afirma"):
    return Evidencias(afirmacoes=[AfirmacaoDecisao(texto=nucleo, nucleo=nucleo, polaridade=pol)], itens=itens,
                      n_lidas=len(itens), n_consultadas=len(itens))


# ------------------------------------------------------------------ medida
def test_parafrase_passa_e_vizinha_cai_abaixo_do_limiar():
    assert trava_indice.similaridade(NUCLEO, PARAFRASE)[0] >= 0.6
    assert trava_indice.similaridade(NUCLEO, VIZINHA)[0] < 0.6


def test_hashtag_nao_identifica_alegacao_e_vale_a_melhor_candidata_utilizavel():
    # "#CaiuNaRede" tem 1 termo de conteúdo: não conta; o título (≥3 termos) é quem vale
    cands = ["#CaiuNaRede", "Café cura câncer de fígado em idosos? Checagem"]
    sim, quem = trava_indice.similaridade(NUCLEO, cands)
    assert quem == cands[1] and sim >= 0.6
    assert trava_indice.similaridade(NUCLEO, ["#CaiuNaRede"]) == (0.0, None)
    assert trava_indice.similaridade(NUCLEO, []) == (0.0, None)


def test_moldura_de_boato_nao_conta_na_similaridade():
    """'É falso que ... circula no zap' não aproxima nem afasta: a tokenização é a do índice."""
    a = trava_indice.similaridade(NUCLEO, ["Café cura câncer de fígado em idosos"])[0]
    b = trava_indice.similaridade(NUCLEO, ["É falso que circula no zap café cura câncer de fígado em idosos"])[0]
    assert a == b == 1.0


def test_candidatas_ordem_sem_repeticao_e_primeira_frase():
    c = trava_indice.candidatas("Café cura câncer", "Café cura câncer", "Frase um. Frase dois sem peso.")
    assert c == ["Café cura câncer", "Frase um."]
    assert trava_indice.candidatas(None, "", None) == []
    assert trava_indice.primeira_frase("Sem ponto final e nada mais") == "Sem ponto final e nada mais"


# ------------------------------------------------------------------ decisão
def test_flag_desligada_mantem_o_comportamento_atual(monkeypatch):
    monkeypatch.setattr(config, "INDICE_TRAVA_SIMILARIDADE", False)
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)]))
    assert d.nivel == "alta" and d.log_odds >= decisao.TAU
    assert not d.tetos_similaridade and d.travas["indice_similaridade_baixa"] is False


def test_variante_vizinha_com_uma_checagem_tem_teto_media(ligada):
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)]))
    assert d.nivel == "media"
    assert d.log_odds == pytest.approx(decisao.TAU * 0.99, abs=1e-3)
    assert d.travas["indice_similaridade_baixa"] is True
    (teto,) = d.tetos_similaridade
    assert teto["afirmacao"] == 0 and teto["similaridade"] < teto["limiar"] == 0.6
    assert teto["urls"] == ["https://lupa.uol.com.br/a"] and teto["direcao"] == 1
    assert "parecida, mas não igual" in d.motivo
    # o texto ao usuário explica o teto e segue neutro (RF12)
    assert "não passa de propensão média" in d.justificativa()
    assert not verificar_neutralidade(d.justificativa())


def test_mesma_alegacao_parafraseada_decide_normalmente(ligada):
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", PARAFRASE)]))
    assert d.nivel == "alta" and not d.tetos_similaridade
    assert d.travas["indice_similaridade_baixa"] is False


def test_hashtag_vaga_com_titulo_proximo_decide_e_sem_nada_utilizavel_tem_teto(ligada):
    ok = decidir(_ev([_selo("https://lupa.uol.com.br/a", ["#CaiuNaRede", "Café cura câncer de fígado em idosos"])]))
    assert ok.nivel == "alta"
    vaga = decidir(_ev([_selo("https://lupa.uol.com.br/a", ["#CaiuNaRede"])]))
    assert vaga.nivel == "media" and vaga.tetos_similaridade[0]["alegacao"] is None
    sem = decidir(_ev([_selo("https://lupa.uol.com.br/a", [])]))
    assert sem.nivel == "media"


def test_dois_clusters_independentes_nao_agem(ligada):
    itens = [_selo("https://lupa.uol.com.br/a", VIZINHA), _selo("https://aosfatos.org/b", VIZINHA)]
    d = decidir(_ev(itens))
    assert d.nivel == "alta" and not d.tetos_similaridade
    # outro cluster na mesma direção, mesmo sem selo, também tira a decisão de uma fonte só
    d2 = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA), _postura("https://g1.globo.com/c")]))
    assert d2.nivel == "alta" and not d2.tetos_similaridade


def test_duas_checagens_no_mesmo_cluster_continuam_sendo_uma_fonte(ligada):
    itens = [_selo("https://lupa.uol.com.br/a", VIZINHA, cluster="lupa"),
             _selo("https://lupa.uol.com.br/b", VIZINHA, cluster="lupa")]
    d = decidir(_ev(itens))
    assert d.nivel == "media" and d.tetos_similaridade[0]["urls"] == ["https://lupa.uol.com.br/a",
                                                                        "https://lupa.uol.com.br/b"]


def test_a_melhor_checagem_do_cluster_vale(ligada):
    itens = [_selo("https://lupa.uol.com.br/a", VIZINHA, cluster="lupa"),
             _selo("https://lupa.uol.com.br/b", PARAFRASE, cluster="lupa")]
    assert decidir(_ev(itens)).nivel == "alta"


def test_direcao_baixa_tambem_tem_teto(ligada):
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA, classe="SUSTENTA", veredito="VERDADEIRO")]))
    assert d.nivel == "media" and d.log_odds == pytest.approx(-decisao.TAU * 0.99, abs=1e-3)
    assert d.tetos_similaridade[0]["direcao"] == -1


def test_polaridade_nega_usa_o_mesmo_nucleo(ligada):
    # 'É falso que <núcleo>': a similaridade é com o núcleo, não com a moldura
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", PARAFRASE, classe="REFUTA", veredito="FALSO")], pol="nega"))
    assert d.nivel == "baixa" and not d.tetos_similaridade


def test_so_o_selo_do_indice_e_alvo_da_trava(ligada):
    # origem None: o selo vota como antes, mas não veio do índice -> a trava não é dele
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA, origem=None)]))
    assert d.nivel == "alta" and not d.tetos_similaridade
    # item sem a lista (snapshot antigo / não informado): sem dado não se corta
    d2 = decidir(_ev([_selo("https://lupa.uol.com.br/a", None)]))
    assert d2.nivel == "alta" and not d2.tetos_similaridade


def test_postura_sozinha_nao_e_alvo_e_nunca_chega_a_tau():
    d = decidir(_ev([_postura("https://g1.globo.com/a")]))
    assert d.nivel == "media" and not d.tetos_similaridade


def test_extremo_que_se_sustenta_sem_o_selo_nao_e_cortado(ligada, monkeypatch):
    # se a postura (fonte de outro peso) bastasse sozinha para passar de τ, o selo não seria necessário
    monkeypatch.setattr(decisao, "W_POSTURA", 2.0)
    itens = [_selo("https://lupa.uol.com.br/a", VIZINHA, cluster="mesmo"),
             _postura("https://g1.globo.com/c", cluster="mesmo")]
    d = decidir(_ev(itens))
    assert d.nivel == "alta" and not d.tetos_similaridade


def test_limiar_configuravel(ligada, monkeypatch):
    sim = trava_indice.similaridade(NUCLEO, VIZINHA)[0]
    monkeypatch.setattr(config, "INDICE_TRAVA_SIM_MIN", sim - 0.01)
    assert decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)])).nivel == "alta"
    monkeypatch.setattr(config, "INDICE_TRAVA_SIM_MIN", sim + 0.01)
    assert decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)])).nivel == "media"


def test_contrafactual_sem_desconto_segue_o_mesmo_teto(ligada):
    d = decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)]))
    assert d.nivel_sem_desconto == "media"


# ------------------------------------------------------------------ telemetria e pipeline
def test_emitir_decisao_grava_evento_trava(ligada, monkeypatch):
    eventos = []
    monkeypatch.setattr(telemetria, "evento", lambda tipo, /, **d: eventos.append((tipo, d)))
    Pipeline._emitir_decisao(decidir(_ev([_selo("https://lupa.uol.com.br/a", VIZINHA)])))
    (dados,) = [d for t, d in eventos if t == "trava"]
    assert dados["nome"] == "indice_similaridade" and dados["afirmacao"] == 0
    assert dados["similaridade"] < dados["limiar"] and dados["L_antes"] >= decisao.TAU
    assert "decisao" in [t for t, _ in eventos]


def _indice_com(afirmacao_checada, titulo, trecho):
    reg = {"url": "https://www.aosfatos.org/noticias/cafe-cura-cancer-figado/", "titulo": titulo,
           "afirmacao_checada": afirmacao_checada, "selo_original": "Falso", "veredito": "FALSO",
           "agencia": "aos-fatos", "trecho": trecho}
    distratores = [{"url": f"https://lupa.uol.com.br/x{i}", "titulo": t, "afirmacao_checada": t,
                    "selo_original": "Falso", "veredito": "FALSO", "agencia": "lupa", "trecho": t}
                   for i, t in enumerate(["Vacina altera o DNA humano", "Urnas foram fraudadas em 2022",
                                          "Limão em jejum cura diabetes", "Governo vai confiscar poupança",
                                          "Água gelada causa gripe"])]
    return Indice.de_checagens([reg] + distratores)


def _rodar_base(monkeypatch, idx, flag):
    from factcheck_mvp import llm, pipeline as pl
    from factcheck_mvp.aprofundar import CorpoLido
    from tests.test_pipeline import FakeSerp, _juiz_falso
    monkeypatch.setenv("INDICE_CHECAGENS", "1")
    monkeypatch.setattr(config, "INDICE_TRAVA_SIMILARIDADE", flag)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "SERP_ESTRATEGIA", "simples")
    monkeypatch.setattr(config, "SERP_ENGINE", "google")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SITES", 0)
    monkeypatch.setattr(llm, "_local", _juiz_falso())
    corpo = "É falso que café cura câncer, segundo especialistas. " * 40

    async def fake_aprofundar(cands, catalogo, **kw):
        return {d["url"]: CorpoLido(url=d["url"], final_url=d["url"], trecho_corpo=corpo[:3000], corpo_lido=True,
                                    texto_completo=corpo, metodo="fake") for d in cands}
    monkeypatch.setattr(pl, "aprofundar", fake_aprofundar)
    pipe = Pipeline(Catalogo.carregar(), idx, Indice(), serpapi=FakeSerp([]), detector=MockDetector())
    return asyncio.run(pipe.executar(EntradaConsulta(tipo="titulo", conteudo="Café cura câncer")))


def test_pipeline_checagem_vizinha_com_flag_ligada_fica_em_media(monkeypatch):
    longa = ("Café cura câncer de próstata, diz estudo feito com ratos em laboratório no interior do estado "
             "durante vários anos")
    idx = _indice_com(longa, longa, "É falso que café cure câncer de próstata em ratos, conclui estudo de laboratório.")
    assert idx.buscar_checagens("Café cura câncer")  # o atalho a acha
    rel = _rodar_base(monkeypatch, idx, flag=True)
    assert rel.propensao == "media", (rel.decisao, rel.justificativa)
    assert rel.decisao["travas"]["indice_similaridade_baixa"] is True
    assert rel.decisao["tetos_similaridade"][0]["similaridade"] < 0.6


def test_pipeline_mesma_checagem_com_flag_desligada_ou_alegacao_igual_decide(monkeypatch):
    longa = ("Café cura câncer de próstata, diz estudo feito com ratos em laboratório no interior do estado "
             "durante vários anos")
    idx = _indice_com(longa, longa, "É falso que café cure câncer de próstata em ratos, conclui estudo de laboratório.")
    assert _rodar_base(monkeypatch, idx, flag=False).propensao == "alta"
    igual = _indice_com("Café cura câncer", "É falso que café cura câncer",
                        "É falso que café cura câncer, segundo especialistas.")
    rel = _rodar_base(monkeypatch, igual, flag=True)
    assert rel.propensao == "alta" and not rel.decisao["tetos_similaridade"]


# ------------------------------------------------------------------ B4/R1: REFUTA de agência pesa como selo
def test_refuta_de_agencia_do_indice_sem_selo_tambem_tem_teto(ligada):
    """Desde B4/R1 a postura REFUTA de agência curada pesa 1,5 e crava sozinha; vinda do índice, é a mesma
    peça que o selo: a trava vale para ela também."""
    item = _selo("https://lupa.uol.com.br/a", VIZINHA, veredito=None)
    d = decidir(_ev([item]))
    assert d.nivel == "media" and d.tetos_similaridade, d.votos
    assert decidir(_ev([_selo("https://lupa.uol.com.br/a", PARAFRASE, veredito=None)])).nivel == "alta"


def test_refuta_de_agencia_achada_na_web_nao_e_alvo(ligada):
    item = _selo("https://lupa.uol.com.br/a", None, veredito=None, origem=None)
    d = decidir(_ev([item]))
    assert d.nivel == "alta" and not d.tetos_similaridade
