"""Índice de checagens, P1/P4/P6: 1ª frase do trecho (Boatos.org), anos fora da exigência de
números, e o evento `indice_descarte` da telemetria. Dados sintéticos (sem rede, sem LLM)."""
import pytest

from factcheck_mvp import indice, telemetria
from factcheck_mvp.indice import Indice, primeira_frase_trecho

BOATO_URL = "https://www.boatos.org/saude/alho-agua-fervida-coronavirus.html"
DESCRICAO = ("Médico chinês comprova que uma tigela de água fervida com alho pode curar a infecção "
             "pelo novo coronavírus")


@pytest.fixture(autouse=True)
def _atalho_ligado(monkeypatch):
    monkeypatch.delenv("INDICE_CHECAGENS", raising=False)


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRIA", "1")
    monkeypatch.setenv("TELEMETRIA_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _reg(url, afirmacao, titulo=None, trecho="", agencia="lupa", veredito="FALSO"):
    return {"url": url, "titulo": titulo or f"É falso que {afirmacao}", "agencia": agencia,
            "afirmacao_checada": afirmacao, "selo_original": "Falso", "veredito": veredito,
            "data_pub": "2026-09-20", "trecho": trecho, "origem": "rss-titulo"}


def _ruido(n=12):
    """Documentos sem relação com as consultas. Parte deles tem as palavras de 'outro fato'
    (prêmio, senador, melhor, ano, recebeu): sem isso, um termo ausente do corpus tem IDF zero e
    some do denominador da cobertura, e o teste não representaria um índice real."""
    fundo = [_reg(f"https://ruido.test/{i}", f"Assunto variado número {i} sobre política e economia do país")
             for i in range(n)]
    palavras = [_reg(f"https://ruido.test/p{i}", f"O senador recebeu prêmio de melhor projeto do ano {i}")
                for i in range(3)]
    return fundo + palavras


def _boato():
    """Estilo Boatos.org: título reescrito; a descrição do boato só aparece no trecho."""
    return {"url": BOATO_URL, "titulo": "Alho com água fervida cura o novo coronavírus #boato",
            "afirmacao_checada": "Alho com água fervida cura o novo coronavírus", "agencia": "boatos",
            "veredito": "FALSO", "selo_original": "Falso", "data_pub": "2020-02-10",
            "trecho": f"Boato – {DESCRICAO}. Outra frase do resumo do Boatos.org."}


def _eventos(rid):
    return [e for e in telemetria.ler_run(rid)["eventos"] if e.get("tipo") == "indice_descarte"]


# ---------------------------------------------------------------- P1: 1ª frase do trecho
def test_registro_estilo_boatos_e_achado_pela_descricao():
    idx = Indice.de_checagens([_boato()] + _ruido())
    hits = idx.buscar_checagens(DESCRICAO, k=5)
    assert hits and hits[0]["url"] == BOATO_URL
    assert hits[0]["score_rel"] >= 0.8  # entrou pelo caminho da 1ª frase (cobertura_min_trecho)
    # o título reescrito sozinho não basta: sem o caminho novo, a mesma consulta não acha nada
    assert idx.buscar_checagens(DESCRICAO, k=5, limiar={"cobertura_min_trecho": 1.01}) == []


def test_quase_acerto_com_outro_fato_continua_rejeitado():
    # mesma pessoa e cidade, outro fato: só os nomes e a cidade batem; o resto não tem a ver
    flavio = _reg("https://www.boatos.org/politica/flavio-camiseta.html",
                  "Flávio Bolsonaro usou camiseta contra nordestinos", agencia="boatos",
                  titulo="Flávio Bolsonaro usou camiseta contra nordestinos #boato",
                  trecho="Boato – Flávio Bolsonaro usou uma camiseta com a frase “nordestino é preguiçoso” "
                         "em Brasília. Outra frase.")
    idx = Indice.de_checagens([flavio] + _ruido())
    assert idx.buscar_checagens("Flávio Bolsonaro recebeu prêmio de melhor senador do ano em Brasília") == []


@pytest.mark.parametrize("trecho, esperado", [
    ("Boato – Médico chinês comprova que X. Outra frase.", "Médico chinês comprova que X."),
    ("Hoax – Algo acontece! Depois vem outra.", "Algo acontece!"),
    ("Boato - Traço hífen? Sim.", "Traço hífen?"),
    ("Boato — Travessão longo. Fim", "Travessão longo."),
    ("Vídeo mostra um tubarão no mar. Outra coisa.", "Vídeo mostra um tubarão no mar."),
    ("Boato – Sem ponto final nenhum aqui", "Sem ponto final nenhum aqui"),
    ("", ""),
    (None, ""),
])
def test_primeira_frase_trecho_com_e_sem_prefixo_e_pontuacao(trecho, esperado):
    assert primeira_frase_trecho(trecho) == esperado


def test_primeira_frase_trecho_sem_fim_de_frase_usa_250_caracteres():
    assert primeira_frase_trecho("Boato – " + "x" * 400) == "x" * 250


# ---------------------------------------------------------------- P4: anos não são quantidade
def test_ano_na_consulta_nao_bloqueia_mas_quantidade_continua_bloqueando(monkeypatch):
    regs = [_reg("https://c.test/vacina", "Vacina contra covid causa infertilidade em mulheres"),
            _reg("https://c.test/sete", "Lula gastou R$ 7 bilhões em viagens com Janja"),
            _reg("https://c.test/sessenta", "Lula gastou R$ 62 milhões em viagens com Janja")] + _ruido()
    # ruído realista de 1 termo (como em test_indice_checagens): sem ele, com 2 docs casando, a
    # mediana do corte relativo é o próprio 2º candidato e nenhum dos dois passa
    regs += [_reg(f"https://fraco.test/{i}", f"Pacote de viagens para o litoral número {i}") for i in range(20)]
    idx = Indice.de_checagens(regs)
    consulta_ano = "Vacina contra covid causa infertilidade em mulheres segundo estudo de 2021"
    assert [h["url"] for h in idx.buscar_checagens(consulta_ano)] == ["https://c.test/vacina"]
    # com a regra antiga (anos contam como quantidade), a mesma consulta é barrada
    real = indice._e_ano
    monkeypatch.setattr(indice, "_e_ano", lambda n: False)
    assert idx.buscar_checagens(consulta_ano) == []
    monkeypatch.setattr(indice, "_e_ano", real)
    # quantidade diferente continua barrada, mesmo com um ano na consulta
    urls = [h["url"] for h in idx.buscar_checagens("Lula gastou R$ 7 bilhões em viagens com Janja em 2022")]
    assert urls == ["https://c.test/sete"]


def test_e_ano_so_pega_inteiros_entre_1900_e_2100():
    assert indice._e_ano("2022") and indice._e_ano("1900") and indice._e_ano("2100")
    assert not indice._e_ano("1899") and not indice._e_ano("2101") and not indice._e_ano("7")
    assert not indice._e_ano("62")


# ---------------------------------------------------------------- P6: evento indice_descarte
def _corpus_numeros():
    fortes = [_reg(f"https://f.test/{i}", f"Lula e Janja gastaram R$ {60 + i} bilhões em viagens com dinheiro público")
              for i in range(5)]
    fracos = [_reg(f"https://w.test/{i}", f"Pacote de viagens para o litoral número {i}") for i in range(25)]
    return fortes + fracos + _ruido()


def test_descarte_por_numero_emite_no_maximo_3_eventos(tdir):
    idx = Indice.de_checagens(_corpus_numeros())
    rid = telemetria.iniciar_run({"entrada": "teste indice_descarte"})
    try:
        assert idx.buscar_checagens("Lula e Janja gastaram 7 bilhões em viagens", k=5) == []
    finally:
        telemetria.finalizar_run(None)
    ev = _eventos(rid)
    assert len(ev) == 3  # há 5 candidatos rejeitados; o teto é 3 por chamada
    assert all(e["dados"]["motivo"] == "numeros" for e in ev)
    assert all(e["dados"]["url"].startswith("https://f.test/") for e in ev)
    assert all({"score", "cobertura"} <= set(e["dados"]) for e in ev)


def test_motivos_min_termos_e_cobertura(tdir):
    regs = [_reg("https://m.test/aceito", "Lula e Janja gastaram dinheiro público em viagens"),
            _reg("https://m.test/cobertura", "Viagens internacionais de avião oficial"),
            _reg("https://m.test/poucos", "Janja comprou um vestido caro em Paris")] + _ruido()
    idx = Indice.de_checagens(regs)
    # corte baixo de propósito: todo registro que casa algo vira candidato e precisa ser julgado
    baixo = {"piso_abs": 1e-9, "fator_mediana": 0.0, "fracao_top1": 0.0}
    consulta = "Lula e Janja gastaram dinheiro público em viagens internacionais de avião"
    rid = telemetria.iniciar_run({"entrada": "teste motivos"})
    try:
        hits = idx.buscar_checagens(consulta, k=5, limiar=baixo)
    finally:
        telemetria.finalizar_run(None)
    assert [h["url"] for h in hits] == ["https://m.test/aceito"]
    motivos = {e["dados"]["url"]: e["dados"]["motivo"] for e in _eventos(rid)}
    assert motivos == {"https://m.test/cobertura": "cobertura", "https://m.test/poucos": "min_termos"}


def test_sem_run_ativo_o_descarte_nao_grava_nada(tdir):
    idx = Indice.de_checagens(_corpus_numeros())
    assert idx.buscar_checagens("Lula e Janja gastaram 7 bilhões em viagens", k=5) == []
    assert not tdir.exists()
