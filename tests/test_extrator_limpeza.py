"""Missão F / item 7: marcadores de tempo e moldura fora do núcleo e da consulta, E4 ainda os vê,
alegações independentes em afirmações distintas. Sem rede (LLM falso)."""
import json

import pytest

from factcheck_mvp import afirmacoes, aplicabilidade, llm

REF = "2026-10-14"


def _setup(monkeypatch, respostas, vistos=None):
    fila = list(respostas)

    def fake(messages, max_tokens, timeout_s, finalidade):
        if vistos is not None:
            vistos.append(messages)
        return fila.pop(0), "m"
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(llm, "_local", fake)


def _item(af, nucleo=None, pol="afirma", consulta="", marcadores=None):
    d = {"afirmacao": af, "nucleo": af if nucleo is None else nucleo, "polaridade": pol, "consulta": consulta}
    if marcadores is not None:
        d["marcadores_tempo"] = marcadores
    return d


def _extrair(monkeypatch, texto, itens, limpeza):
    monkeypatch.setattr(afirmacoes.config, "EXTRATOR_LIMPEZA", limpeza)
    _setup(monkeypatch, [json.dumps(itens)])
    return afirmacoes.extrair_afirmacoes(texto)[0]


# ----------------------------------------------------------------------------- campo novo
def test_campo_marcadores_vem_do_llm_e_da_regra(monkeypatch):
    af = _extrair(monkeypatch, "Agora a empresa Alfa vai demitir mil funcionários",
                  [_item("Agora a empresa Alfa vai demitir mil funcionários",
                         "A empresa Alfa vai demitir mil funcionários",
                         consulta="empresa Alfa demitir mil funcionários", marcadores=["agora"])], False)[0]
    assert af.marcadores_tempo == ["agora"]
    assert af.nucleo == "A empresa Alfa vai demitir mil funcionários"
    assert af.consulta == "empresa Alfa demitir mil funcionários"


def test_campo_preenchido_pela_regra_quando_o_llm_nao_manda(monkeypatch):
    af = _extrair(monkeypatch, "O ministro foi preso ontem em Brasília",
                  [_item("O ministro foi preso ontem em Brasília", consulta="ministro preso Brasília")], False)[0]
    assert af.marcadores_tempo == ["ontem"]


def test_campo_descarta_o_que_nao_e_marcador_do_e4(monkeypatch):
    af = _extrair(monkeypatch, "Me mandaram que o ministro foi preso hoje em dia",
                  [_item("O ministro foi preso hoje em dia", consulta="ministro preso",
                         marcadores=["Me mandaram", "hoje em dia", "recentemente", "Hoje:"])], False)[0]
    assert af.marcadores_tempo == ["hoje"]  # "hoje em dia" não é marcador (exclusão do E4); moldura não é tempo


def test_marcadores_como_string_ou_nulo_nao_quebram(monkeypatch):
    af = _extrair(monkeypatch, "Ontem o ministro foi preso em Brasília",
                  [_item("Ontem o ministro foi preso em Brasília", consulta="ministro preso Brasília",
                         marcadores="ontem")], False)[0]
    assert af.marcadores_tempo == ["ontem"]
    af = _extrair(monkeypatch, "Ontem o ministro foi preso em Brasília",
                  [dict(_item("Ontem o ministro foi preso em Brasília", consulta="ministro preso Brasília"),
                        marcadores_tempo=None)], False)[0]
    assert af.marcadores_tempo == ["ontem"]


# ----------------------------------------------------------------------------- limpeza (flag)
CASOS_NUCLEO = [
    ("Agora: o governo anunciou o fim do imposto sobre bicicletas", "O governo anunciou o fim do imposto sobre bicicletas"),
    ("Hoje, o ministro foi preso em Brasília", "O ministro foi preso em Brasília"),
    ("O ministro, hoje, foi preso em Brasília", "O ministro foi preso em Brasília"),
    ("Me mandaram um áudio: a empresa Alfa vai demitir mil funcionários hoje",
     "A empresa Alfa vai demitir mil funcionários"),
    ("Vi um post dizendo que o chá de erva Delta cura a gripe", "O chá de erva Delta cura a gripe"),
    ("Acabou de sair a notícia de que o ministro foi preso", "O ministro foi preso"),
    ("Ontem Fulano disse que a vacina não funciona", "Fulano disse que a vacina não funciona"),
]


@pytest.mark.parametrize("entrada,esperado", CASOS_NUCLEO)
def test_tirar_tempo_e_moldura(entrada, esperado):
    assert afirmacoes.tirar_tempo_e_moldura(entrada)[0] == esperado


@pytest.mark.parametrize("entrada", [
    "Até hoje o governo não pagou os 300 reais prometidos",
    "Hoje em dia ninguém usa mais o imposto sobre bicicletas",
    "O prazo vale a partir de agora para todos os contribuintes",
    "Vi uma pessoa que cura câncer com chá de boldo",  # "Vi" aqui não é moldura de entrega
])
def test_nao_apaga_o_que_nao_e_marcador_nem_moldura(entrada):
    assert afirmacoes.tirar_tempo_e_moldura(entrada)[0] == entrada


def test_limpeza_nunca_esvazia_o_fato():
    assert afirmacoes.tirar_tempo_e_moldura("Hoje: ele morreu")[0] == "Hoje: ele morreu"


def test_limpar_consulta_tira_ruido():
    assert afirmacoes.limpar_consulta("acabou sair notícia ministro preso") == "ministro preso"
    assert afirmacoes.limpar_consulta("é verdade fake vacina altera DNA") == "vacina altera DNA"
    assert afirmacoes.limpar_consulta("Vídeo mostra prefeito Gama preso") == "prefeito Gama preso"
    assert afirmacoes.limpar_consulta("Café não cura câncer") == "Café cura câncer"
    assert afirmacoes.limpar_consulta("ministro disse hoje ponte caiu") == "ministro ponte caiu"


def test_flag_ligada_limpa_nucleo_e_consulta_e_mantem_afirmacao(monkeypatch):
    texto = "Me mandaram: agora o governo anunciou o fim do imposto sobre bicicletas. É verdade?"
    itens = [_item("Agora o governo anunciou o fim do imposto sobre bicicletas",
                   "Agora o governo anunciou o fim do imposto sobre bicicletas",
                   consulta="agora governo anunciou fim imposto bicicletas é verdade", marcadores=["agora"])]
    af = _extrair(monkeypatch, texto, itens, True)[0]
    assert af.texto == "Agora o governo anunciou o fim do imposto sobre bicicletas"  # E4 lê a afirmação
    assert af.nucleo == "O governo anunciou o fim do imposto sobre bicicletas"
    assert af.consulta == "governo anunciou fim imposto bicicletas"
    assert af.marcadores_tempo == ["agora"]


def test_flag_desligada_nao_mexe_em_nucleo_nem_consulta(monkeypatch):
    itens = [_item("Agora o governo anunciou o fim do imposto sobre bicicletas",
                   "Agora o governo anunciou o fim do imposto sobre bicicletas",
                   consulta="agora governo anunciou fim imposto bicicletas", marcadores=["agora"])]
    af = _extrair(monkeypatch, "Agora o governo anunciou o fim do imposto sobre bicicletas", itens, False)[0]
    assert af.nucleo.startswith("Agora o governo") and af.consulta.startswith("agora governo")
    assert af.marcadores_tempo == ["agora"]  # o campo é informativo e vale com a flag desligada


def test_consulta_esvaziada_pela_limpeza_volta_ao_nucleo(monkeypatch):
    itens = [_item("Hoje, o ministro foi preso em Brasília", "Hoje, o ministro foi preso em Brasília",
                   consulta="hoje agora é verdade")]
    af = _extrair(monkeypatch, "Hoje, o ministro foi preso em Brasília", itens, True)[0]
    assert af.consulta == "ministro preso Brasília"


def test_negacao_e_atribuicao_continuam_com_a_flag(monkeypatch):
    # negação dentro de oração relatada = afirma; atribuição preservada; só o "ontem" sai do núcleo
    itens = [_item("Ontem Fulano disse que a vacina não funciona", "Ontem Fulano disse que a vacina não funciona",
                   "afirma", "Fulano disse vacina não funciona")]
    af = _extrair(monkeypatch, "Ontem Fulano disse que a vacina não funciona", itens, True)[0]
    assert af.polaridade == "afirma" and af.nucleo == "Fulano disse que a vacina não funciona"
    assert af.consulta == "Fulano vacina funciona"
    # negação do próprio usuário continua invertendo a direção
    itens = [_item("Hoje o café não cura câncer", "Hoje o café cura câncer", "nega", "café cura câncer")]
    af = _extrair(monkeypatch, "Hoje o café não cura câncer", itens, True)[0]
    assert af.polaridade == "nega" and af.nucleo == "O café cura câncer" and af.marcadores_tempo == ["hoje"]


def test_fallback_regex_tambem_preenche_e_limpa(monkeypatch):
    monkeypatch.setattr(afirmacoes.config, "EXTRATOR_LIMPEZA", False)
    off = afirmacoes._fallback("Hoje, o ministro foi preso em Brasília durante a operação", 3)[0]
    assert off.marcadores_tempo == ["hoje"] and off.nucleo.startswith("Hoje")
    monkeypatch.setattr(afirmacoes.config, "EXTRATOR_LIMPEZA", True)
    on = afirmacoes._fallback("Hoje, o ministro foi preso em Brasília durante a operação", 3)[0]
    assert on.marcadores_tempo == ["hoje"] and on.nucleo.startswith("O ministro")
    assert "hoje" not in on.consulta.lower()


# ----------------------------------------------------------------------------- E4 ainda vê o marcador
def test_e4_ve_o_marcador_mesmo_fora_do_nucleo_e_da_consulta(monkeypatch):
    itens = [_item("Hoje o ministro foi preso em Brasília", "Hoje o ministro foi preso em Brasília",
                   consulta="ministro preso Brasília", marcadores=["hoje"]),
             _item("A ponte nova de Beta caiu", consulta="ponte nova Beta caiu")]
    afs = _extrair(monkeypatch, "Hoje o ministro foi preso em Brasília. A ponte nova de Beta caiu.", itens, True)
    a0, a1 = afs
    assert "hoje" not in a0.nucleo.lower() and "hoje" not in a0.consulta.lower()
    # com 2 afirmações, só o marcador da própria afirmação conta; a 2ª não herda
    texto_usuario = "Hoje o ministro foi preso em Brasília. A ponte nova de Beta caiu."
    j0 = aplicabilidade.marcas_da_afirmacao(a0.texto_temporal(), texto_usuario, 2, REF)[0]
    j1 = aplicabilidade.marcas_da_afirmacao(a1.texto_temporal(), texto_usuario, 2, REF)[0]
    assert j0 == aplicabilidade.janela_temporal("hoje") and j1 is None


def test_e4_recupera_marcador_que_o_llm_tirou_da_afirmacao(monkeypatch):
    # o LLM mandou o marcador só no campo novo: texto_temporal() o devolve à regra de data
    itens = [_item("O ministro foi preso em Brasília", consulta="ministro preso Brasília", marcadores=["ontem"]),
             _item("A ponte nova de Beta caiu", consulta="ponte nova Beta caiu")]
    a0, a1 = _extrair(monkeypatch, "Ontem o ministro foi preso em Brasília. A ponte nova de Beta caiu.", itens, True)
    assert a0.marcadores_tempo == ["ontem"]
    usuario = "Ontem o ministro foi preso em Brasília. A ponte nova de Beta caiu."
    assert aplicabilidade.marcas_da_afirmacao(a0.texto, usuario, 2, REF)[0] is None  # sem o gancho
    assert aplicabilidade.marcas_da_afirmacao(a0.texto_temporal(), usuario, 2, REF)[0] == \
        aplicabilidade.janela_temporal("ontem")
    assert aplicabilidade.marcas_da_afirmacao(a1.texto_temporal(), usuario, 2, REF)[0] is None


def test_texto_temporal_sem_marcadores_e_o_proprio_texto():
    from factcheck_mvp.schemas import Afirmacao
    assert Afirmacao(texto="A ponte caiu").texto_temporal() == "A ponte caiu"


# ----------------------------------------------------------------------------- separar alegações
def test_duas_alegacoes_independentes_viram_duas_afirmacoes(monkeypatch):
    itens = [_item("A ponte nova de Beta caiu", consulta="ponte nova Beta caiu"),
             _item("O prefeito de Gama foi preso", consulta="prefeito Gama preso")]
    afs = _extrair(monkeypatch, "A ponte nova de Beta caiu e o prefeito de Gama foi preso", itens, True)
    assert [a.indice for a in afs] == [0, 1]
    assert afs[0].alvo() == "A ponte nova de Beta caiu" and afs[1].alvo() == "O prefeito de Gama foi preso"


def test_boato_unico_continua_uma_afirmacao(monkeypatch):
    texto = "Circula um vídeo dizendo que o chá de erva Delta cura a gripe em dois dias, e que isso foi provado"
    itens = [_item("O chá de erva Delta cura a gripe em dois dias, segundo estudo",
                   consulta="chá erva Delta cura gripe dois dias")]
    afs = _extrair(monkeypatch, texto, itens, True)
    assert len(afs) == 1


def test_prompt_traz_as_regras_novas_e_nenhum_caso_de_teste(monkeypatch):
    vistos = []
    monkeypatch.setattr(afirmacoes.config, "EXTRATOR_LIMPEZA", False)
    _setup(monkeypatch, [json.dumps([_item("A ponte nova de Beta caiu", consulta="ponte nova Beta caiu")])], vistos)
    afirmacoes.extrair_afirmacoes("A ponte nova de Beta caiu")
    p = vistos[0][-1]["content"]
    for trecho in ("marcadores_tempo", "INDEPENDENTES", "NÃO divida", "moldura de entrega", "TODOS os termos"):
        assert trecho in p
    for caso in ("Claudia Leitte", "Gisele", "Hytalo", "Sacani", "Rebeca Abravanel"):
        assert caso not in p
