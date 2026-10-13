"""B4 (decisão da usuária, 13/10): agência de checagem curada é confiável por natureza.

R1: a postura REFUTA de agência curada pesa como um selo (1,5), não 1,0.
R11: havendo REFUTA de agência (lida, dentro da janela), a SUSTENTA de quem não é agência não vota.
"""
from factcheck_mvp import confiabilidade
from factcheck_mvp.decisao import AfirmacaoDecisao, Evidencias, ItemEvidencia, decidir

JUIZ = "llm-juiz:openrouter"


def _it(url, classe, agencia, curada=True, cluster=None, af=0, data=None, corpo=True):
    return ItemEvidencia(url=url, afirmacao=af, cluster=cluster or url, classe=classe, motor=JUIZ,
                         citacao_verificada=True, curada=curada, corpo_lido=corpo, agencia=agencia, data_pub=data)


def _ev(itens, **kw):
    return Evidencias(afirmacoes=[AfirmacaoDecisao(texto="x", nucleo="x", polaridade="afirma",
                                                   **kw.pop("af_kw", {}))],
                      itens=itens, n_lidas=len(itens), n_consultadas=len(itens), **kw)


def test_r1_refuta_de_agencia_sozinha_chega_a_alta():
    d = decidir(_ev([_it("https://ag.test/a", "REFUTA", agencia=True)]))
    assert d.log_odds == 1.5 and d.nivel == "alta"
    assert d.votos[0].motivo == "postura REFUTA (agência)"


def test_r1_veiculo_curado_que_nao_e_agencia_segue_em_1_0():
    d = decidir(_ev([_it("https://jornal.test/a", "REFUTA", agencia=False)]))
    assert d.log_odds == 1.0 and d.nivel == "media"


def test_r1_nao_vale_para_agencia_nao_curada():
    # candidato descoberto automaticamente: curada=False, mesmo que o item diga agencia=True
    d = decidir(_ev([_it("https://cand.test/a", "REFUTA", agencia=True, curada=False)]))
    assert d.log_odds < 1.0  # peso de site pouco acessado


def test_sustenta_de_agencia_nao_ganha_peso_extra():
    d = decidir(_ev([_it("https://ag.test/a", "SUSTENTA", agencia=True)]))
    assert d.log_odds == -1.0


def test_r11_sustenta_de_nao_agencia_nao_vota_quando_agencia_refuta():
    itens = [_it("https://ag.test/a", "REFUTA", agencia=True),
             _it("https://jornal.test/a", "SUSTENTA", agencia=False),
             _it("https://outro.test/a", "SUSTENTA", agencia=False)]
    d = decidir(_ev(itens))
    assert d.log_odds == 1.5 and d.nivel == "alta"
    assert [x["url"] for x in d.posturas_suprimidas] == ["https://jornal.test/a", "https://outro.test/a"]
    assert "agência de checagem da base contesta" in d.justificativa()
    assert "posturas_suprimidas" in d.resumo_trace()


def test_r11_sem_agencia_refutando_a_sustenta_continua_votando():
    itens = [_it("https://jornal.test/a", "REFUTA", agencia=False),
             _it("https://outro.test/a", "SUSTENTA", agencia=False)]
    d = decidir(_ev(itens))
    assert not d.posturas_suprimidas and d.log_odds == 0.0


def test_r11_sustenta_de_outra_agencia_continua_votando():
    itens = [_it("https://ag1.test/a", "REFUTA", agencia=True),
             _it("https://ag2.test/a", "SUSTENTA", agencia=True)]
    d = decidir(_ev(itens))
    assert not d.posturas_suprimidas and d.log_odds == 0.5  # +1,5 -1,0


def test_r11_vale_por_afirmacao():
    itens = [_it("https://ag.test/a", "REFUTA", agencia=True, af=0),
             _it("https://jornal.test/b", "SUSTENTA", agencia=False, af=1)]
    ev = Evidencias(afirmacoes=[AfirmacaoDecisao(texto="x", nucleo="x", polaridade="afirma"),
                                AfirmacaoDecisao(texto="y", nucleo="y", polaridade="afirma")],
                    itens=itens, n_lidas=2, n_consultadas=2)
    d = decidir(ev)
    assert not d.posturas_suprimidas  # a agência refuta a afirmação 0, a SUSTENTA é da afirmação 1


def test_r11_agencia_de_outro_periodo_nao_anula_fonte_atual():
    """Agência que refuta um episódio antigo ("hoje" no texto, checagem de anos atrás) não apaga a
    SUSTENTA de uma fonte do período."""
    af_kw = {"janela": 2, "calculada": True}
    itens = [_it("https://ag.test/a", "REFUTA", agencia=True, data="2019-01-01"),
             _it("https://jornal.test/a", "SUSTENTA", agencia=False, data="2026-10-12")]
    d = decidir(_ev(itens, af_kw=af_kw, data_referencia="2026-10-13"))
    assert not d.posturas_suprimidas


def test_r11_agencia_sem_corpo_lido_nao_suprime():
    itens = [_it("https://ag.test/a", "REFUTA", agencia=True, corpo=False),
             _it("https://jornal.test/a", "SUSTENTA", agencia=False)]
    d = decidir(_ev(itens))
    assert not d.posturas_suprimidas and d.log_odds == -1.0


def test_item_sem_marca_confere_pela_url_no_catalogo():
    # snapshot antigo: sem `agencia`; a Lupa é agência curada do catálogo, um jornal geral não
    assert ItemEvidencia(url="https://lupa.uol.com.br/x", curada=True).eh_agencia() is True
    assert ItemEvidencia(url="https://www1.folha.uol.com.br/x", curada=True).eh_agencia() is False
    assert ItemEvidencia(url="https://lupa.uol.com.br/x", curada=False).eh_agencia() is False
    assert confiabilidade.eh_agencia_checagem("https://dominio-qualquer.test/x") is False
