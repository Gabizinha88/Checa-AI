"""Correções gerais da auditoria da rodada com web (13/10): reformulação no Google News, listagens,
rótulo do gate, contador de uso e regra de silêncio do juiz. Sem rede."""
import json
import os

from factcheck_mvp import agente, aplicabilidade, config, juiz_llm, llm, pipeline, replay, telemetria


def _alvo(texto="Ibuprofeno cura dengue em 3 dias", consulta="ibuprofeno cura dengue"):
    return agente.Alvo(indice=0, texto=texto, consulta=consulta)


# ---------------------------------------------------------------- reformulação sem vocabulário de checagem
def test_sem_vocabulario_de_checagem():
    assert agente.sem_vocabulario_de_checagem("Hytalo Santos morte boato é falso checagem") == "Hytalo Santos morte é"
    assert agente.sem_vocabulario_de_checagem("vacina altera DNA fake news") == "vacina altera DNA news"


def test_prompt_de_reformulacao_no_google_news_nao_pede_checagem(monkeypatch):
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    p = agente.montar_prompt_reformular("Ibuprofeno cura dengue", ["a b c"], [])
    assert '"é falso"' not in p and "Google News" in p and "TODOS os termos" in p
    monkeypatch.setattr(config, "SERP_ENGINE", "google")
    p = agente.montar_prompt_reformular("Ibuprofeno cura dengue", ["a b c"], [])
    assert '"é falso", "checagem"' in p and "TODOS os termos" not in p  # Google orgânico: como antes


def test_reformular_llm_no_news_remove_vocabulario_de_checagem(monkeypatch):
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(llm, "_local", lambda m, mt, t, f: (json.dumps(
        {"consulta": "ibuprofeno dengue hemorragia é falso checagem"}), "fake"))
    q, motor = agente.reformular(_alvo(), ["ibuprofeno cura dengue"], [])
    assert q == "ibuprofeno dengue hemorragia é" and motor.startswith("llm-reformular")


def test_fallback_de_reformulacao_no_news_nao_usa_vocabulario_de_checagem(monkeypatch):
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(telemetria, "fallback", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_local", lambda *a, **k: (_ for _ in ()).throw(llm.ErroLLM("sem modelo")))
    q, motor = agente.reformular(_alvo(), ["ibuprofeno cura dengue"], [])
    assert motor == agente.MOTOR_REFORMULAR_FALLBACK and q
    assert not agente._VOCAB_CHECAGEM.search(q)


# ---------------------------------------------------------------- páginas de listagem
def test_paginacao_de_listagem_nao_e_artigo():
    for u in ("https://projetocomprova.com.br/page/4/?utm_page=x", "https://projetocomprova.com.br/page/2/",
              "https://site.test/noticias/pagina/3", "https://site.test/pagina-5"):
        assert pipeline._eh_homepage(u), u
    for u in ("https://projetocomprova.com.br/publicações/audio-de-vorcaro-e-falso/",
              "https://www.boatos.org/politica/exemplo.html", "https://lupa.uol.com.br/jornalismo/2026/10/01/x"):
        assert not pipeline._eh_homepage(u), u


# ---------------------------------------------------------------- rótulo do gate
def test_gate_distingue_juiz_indisponivel_de_nao_trata():
    args = (True, True, "FALSO", "Café cura câncer", "2026-09-22", "2026-10-09")
    assert aplicabilidade.e_aplicavel(None, *args) == (False, "juiz indisponível")
    assert aplicabilidade.e_aplicavel("NAO_TRATA", *args) == (False, "juiz não trata do fato")


# ---------------------------------------------------------------- contador de uso (OneDrive)
def test_contador_de_uso_que_nao_grava_nao_derruba_a_busca(tmp_path, monkeypatch):
    arq = tmp_path / "serpapi_uso.json"
    monkeypatch.setattr(replay, "arq_uso_serpapi", lambda: arq)
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    eventos = []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo="", **k: eventos.append(onde))
    chamadas = []

    def replace_negado(a, b):
        chamadas.append(1)
        raise PermissionError("acesso negado")
    monkeypatch.setattr(replay.os, "replace", replace_negado)
    replay._reservar_serpapi("https://serpapi.com/search.json?engine=google_news&q=x")  # não levanta
    assert len(chamadas) == 4 and eventos == ["serpapi.contador"]


def test_contador_de_uso_tenta_de_novo_e_grava(tmp_path, monkeypatch):
    arq = tmp_path / "serpapi_uso.json"
    monkeypatch.setattr(replay, "arq_uso_serpapi", lambda: arq)
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    real, falhas = os.replace, []

    def replace_uma_falha(a, b):
        if not falhas:
            falhas.append(1)
            raise PermissionError("acesso negado")
        return real(a, b)
    monkeypatch.setattr(replay.os, "replace", replace_uma_falha)
    replay._reservar_serpapi("https://serpapi.com/search.json?engine=google_news&q=x")
    assert json.loads(arq.read_text(encoding="utf-8"))["total_live"] == 1


# ---------------------------------------------------------------- juiz: silêncio não refuta
def test_prompt_do_juiz_diz_que_silencio_nao_refuta():
    p = juiz_llm.INSTRUCAO.format(afirmacao="x", n=1, itens="{}")
    assert "Silêncio ou inferência não refuta" in p and "REFUTA exige que a própria página" in p
