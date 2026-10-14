"""Missão F / A4: trecho do juiz = título + lide + conclusão/veredito + frases relevantes, sem boilerplate,
dentro de JUIZ_TRECHO_MAX; n_chars_trecho da telemetria = tamanho do trecho do prompt. Sem rede."""
import json

import pytest

from factcheck_mvp import aprofundar, avaliador, config, juiz_llm, llm, telemetria, trecho_juiz

AFIRMACAO = "Chá de boldo cura diabetes"


def _enchimento(n, prefixo="Frase de contexto"):
    return " ".join(f"{prefixo} número {i} sobre economia, esporte e clima da cidade." for i in range(n))


def _corpo_longo(final):
    return _enchimento(60) + "\n" + final


# ------------------------------------------------------------------ unificação
def test_so_existe_uma_montagem_de_trecho():
    assert juiz_llm.montar_trecho is trecho_juiz.montar_trecho
    assert aprofundar.montar_trecho is trecho_juiz.montar_trecho


# ------------------------------------------------------------------ conclusão no fim
def test_corpo_maior_que_limite_com_veredito_no_fim_mantem_o_veredito():
    veredito = "Conclusão: é falso que o chá de boldo cure diabetes, segundo especialistas."
    corpo = _corpo_longo(veredito)
    assert len(corpo) > 2500
    t = trecho_juiz.montar_trecho("Boldo e diabetes", corpo, AFIRMACAO)
    assert veredito in t
    assert len(t) <= config.JUIZ_TRECHO_MAX


@pytest.mark.parametrize("veredito", [
    "Não há evidências científicas de que o boldo trate a doença.",   # sem termo da afirmação, no fim
    "#FAKE Mensagem sobre chá circula em grupos de mensagens.",
    "A informação é enganosa e foi classificada como falsa pela equipe.",
])
def test_veredito_sem_termo_da_afirmacao_no_fim_tambem_entra(veredito):
    t = trecho_juiz.montar_trecho("Título", _corpo_longo(veredito), AFIRMACAO)
    assert veredito in t and len(t) <= config.JUIZ_TRECHO_MAX


def test_veredito_de_outra_alegacao_no_meio_nao_e_puxado():
    # página de debate/FAQ: veredito sobre OUTRO assunto no meio, sem termo da afirmação, fora do fim
    meio = "Isso não é verdade, respondeu o ministro sobre o orçamento."
    corpo = _enchimento(8) + " " + meio + " " + _enchimento(60, "Outro contexto") + "\nConclusão: é falso que o chá de boldo cure diabetes."
    t = trecho_juiz.montar_trecho("Título", corpo, AFIRMACAO)
    assert "Conclusão: é falso que o chá de boldo cure diabetes." in t
    assert meio not in t


def test_cabecalho_de_conclusao_leva_as_frases_do_paragrafo():
    corpo = _corpo_longo("Conclusão\nA mensagem circula desde 2020. Os especialistas ouvidos desmentem a alegação.")
    t = trecho_juiz.montar_trecho("Título", corpo, AFIRMACAO)
    assert "Os especialistas ouvidos desmentem a alegação." in t


# ------------------------------------------------------------------ boilerplate
def test_boilerplate_removido_mesmo_quando_o_corpo_cabe():
    corpo = ("O boldo não cura diabetes, diz a checagem.\n"
             "Leia também: É falso que limão cura gripe\n"
             "Compartilhe no WhatsApp\n"
             "Publicidade\n"
             "Foto: Reprodução\n"
             "Siga a gente nas redes sociais\n"
             "Mesmo assim o chá é popular.\n"
             "Como falar com o UOL Confere")
    t = trecho_juiz.montar_trecho("Boldo", corpo, AFIRMACAO)
    assert "O boldo não cura diabetes" in t and "Mesmo assim o chá é popular." in t
    for lixo in ("Leia também", "Compartilhe", "Publicidade", "Foto:", "Siga a gente", "Como falar com"):
        assert lixo not in t, lixo


def test_manchetes_soltas_apos_leia_tambem_sao_descartadas_e_o_rodape_corta():
    meio = [f"Parágrafo {i} da apuração sobre o boldo e a diabetes foi publicado hoje." for i in range(10)]
    corpo = "\n".join(meio + ["Leia também", "É falso que vacina cause autismo", "É falso que 5G espalhe vírus",
                              "Todos os direitos reservados", "Outro texto de rodapé qualquer"])
    t = trecho_juiz.montar_trecho("Título", corpo, AFIRMACAO)
    assert "É falso que vacina" not in t and "5G" not in t and "rodapé" not in t
    assert "Parágrafo 9 da apuração" in t


def test_whatsapp_dentro_da_apuracao_nao_e_boilerplate():
    corpo = "A mensagem que circula no WhatsApp diz que o chá de boldo cura diabetes. É falso."
    t = trecho_juiz.montar_trecho("Título", corpo, AFIRMACAO)
    assert "circula no WhatsApp" in t and "É falso." in t


def test_boato_transcrito_entre_aspas_nao_e_boilerplate():
    corpo = "“Compartilhe com 10 amigos para o chá de boldo curar sua diabetes.”\nO texto é falso."
    t = trecho_juiz.montar_trecho("Título", corpo, AFIRMACAO)
    assert "Compartilhe com 10 amigos" in t


# ------------------------------------------------------------------ limite e título
@pytest.mark.parametrize("limite", [300, 800, 2500])
def test_limite_respeitado(limite):
    corpo = _corpo_longo("Conclusão: é falso que o chá de boldo cure diabetes.")
    t = trecho_juiz.montar_trecho("Um título qualquer", corpo, AFIRMACAO, limite=limite)
    assert len(t) <= limite and t.startswith("Um título qualquer")


def test_limite_vem_de_juiz_trecho_max(monkeypatch):
    monkeypatch.setattr(config, "JUIZ_TRECHO_MAX", 700)
    t = trecho_juiz.montar_trecho("T", _corpo_longo("Conclusão: é falso que o boldo cure diabetes."), AFIRMACAO)
    assert len(t) <= 700


def test_titulo_nao_e_duplicado():
    titulo = "É falso que chá de boldo cure diabetes"
    curto = trecho_juiz.montar_trecho(titulo, titulo + "\n" + "O boldo é uma planta usada em chás.", AFIRMACAO)
    assert curto.lower().count(titulo.lower()) == 1
    longo = trecho_juiz.montar_trecho(titulo, titulo + "\n" + _corpo_longo("Conclusão: é falso."), AFIRMACAO)
    assert longo.lower().count(titulo.lower()) == 1
    sozinho = trecho_juiz.montar_trecho(titulo, "Intro.\n" + titulo + "\nO boldo é uma planta usada em chás.", AFIRMACAO)
    assert sozinho.lower().count(titulo.lower()) == 1


def test_corpo_curto_sem_boilerplate_volta_inteiro():
    assert trecho_juiz.montar_trecho("T", "Texto curto sobre boldo.", AFIRMACAO) == "T\nTexto curto sobre boldo."
    assert trecho_juiz.montar_trecho("", "curto", "x") == "curto"


def test_lide_vem_primeiro_e_ordem_original_com_reticencias():
    t = trecho_juiz.montar_trecho("T", _corpo_longo("Conclusão: é falso que o boldo cure diabetes."), AFIRMACAO)
    assert "Frase de contexto número 0" in t and " … " in t
    assert t.index("Frase de contexto número 0") < t.index("Conclusão")


# ------------------------------------------------------------------ modo antigo
def test_modo_antigo_recupera_a_selecao_anterior(monkeypatch):
    monkeypatch.setattr(config, "JUIZ_TRECHO_MODO", "antigo")
    corpo = " ".join([f"Frase de enchimento número {i} sobre economia e esporte." for i in range(200)])
    corpo += " Conclusão: não há evidência de que o café cure câncer, diz o INCA."
    t = trecho_juiz.montar_trecho("Título", corpo, "Café cura câncer", limite=800)
    assert len(t) <= 800 and t.startswith("Título") and "café cure câncer" in t
    # no modo antigo o boilerplate não é tirado
    t2 = trecho_juiz.montar_trecho("T", "Texto do boldo.\nLeia também: outra matéria", AFIRMACAO)
    assert "Leia também" in t2


# ------------------------------------------------------------------ telemetria = prompt
def _peca(corpo, **kw):
    p = {"url": "https://exemplo.org/a", "titulo": "Boldo e diabetes", "corpo": corpo, "corpo_lido": True,
         "veiculo": "Exemplo", "dominio": "exemplo.org", "snippet": "curto"}
    p.update(kw)
    return p


def test_trecho_da_peca_e_o_que_o_avaliador_manda_ao_juiz(monkeypatch):
    vistos = []

    def fake(afirmacao, itens):
        vistos.extend(itens)
        return [{"classe": "REFUTA", "citacao": "", "motor": "fake"} for _ in itens]
    monkeypatch.setattr(juiz_llm, "julgar_lote", fake)
    peca = _peca(_corpo_longo("Conclusão: é falso que o chá de boldo cure diabetes."),
                 trecho_juiz="OUTRO TEXTO DIFERENTE " * 20)  # o campo que a telemetria usava antes
    r = avaliador.avaliar(AFIRMACAO, peca)
    assert vistos[0]["trecho"] == avaliador.trecho_da_peca(AFIRMACAO, peca)
    assert r["n_chars_trecho"] == len(vistos[0]["trecho"])
    assert "OUTRO TEXTO" not in vistos[0]["trecho"]


def test_trecho_da_peca_vazio_sem_corpo_lido():
    assert avaliador.trecho_da_peca(AFIRMACAO, _peca("x" * 900, corpo_lido=False)) == ""
    assert avaliador.trecho_da_peca(AFIRMACAO, _peca("curto")) == ""
    assert avaliador.trecho_da_peca(AFIRMACAO, _peca("x" * 900, titulo="")) == ""


def test_pipeline_n_chars_trecho_igual_ao_do_prompt(monkeypatch):
    """Roda `Pipeline._julgar` com o LLM do juiz falso e compara o evento `avaliador` com o prompt."""
    import asyncio
    from factcheck_mvp import pipeline
    from factcheck_mvp.catalogo import Catalogo
    from factcheck_mvp.indice import Indice
    from factcheck_mvp.modelo_fake import MockDetector
    from factcheck_mvp.schemas import Afirmacao
    from factcheck_mvp.serpapi_layer import SerpAPIClient

    prompts = []

    def fake_local(messages, max_tokens, timeout_s, finalidade):
        prompts.append(messages[-1]["content"])
        return json.dumps({"itens": [{"i": 0, "classe": "NAO_TRATA", "citacao": ""}]}), "m"
    monkeypatch.setattr(llm.config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(llm, "_local", fake_local)
    corpo = _corpo_longo("Conclusão: é falso que o chá de boldo cure diabetes.")
    pecas = [_peca(corpo, trecho_juiz="texto antigo " * 30)]
    af = Afirmacao(texto=AFIRMACAO, consulta=AFIRMACAO)
    eventos = []
    monkeypatch.setattr(telemetria, "evento", lambda tipo, **kw: eventos.append((tipo, kw)))
    pipe = pipeline.Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(),
                             serpapi=SerpAPIClient(api_key=""), detector=MockDetector())
    asyncio.run(pipe._julgar([af], pecas, [(0, 0)], True))
    aval = [kw for t, kw in eventos if t == "fonte" and kw.get("estagio") == "avaliador"]
    assert len(aval) == 1 and prompts
    item = json.loads([ln for ln in prompts[0].splitlines() if ln.startswith('{"i": 0')][0])
    assert aval[0]["n_chars_trecho"] == len(item["trecho"]) > 0
    juiz = [kw for t, kw in eventos if t == "fonte" and kw.get("estagio") == "juiz"]
    assert juiz[0]["n_chars_trecho"] == len(item["trecho"])
