"""Google Fact Check Tools API como base extra de checagens (porte do backup de 05/10)."""
import asyncio
import json

import pytest

from factcheck_mvp import config, factcheck_api as fca, replay, telemetria

PAYLOAD = {"claims": [
    {"text": "Ibuprofeno cura dengue", "claimant": "post no Facebook", "claimDate": "2026-09-01T00:00:00Z",
     "claimReview": [
         {"publisher": {"name": "Aos Fatos", "site": "aosfatos.org"},
          "url": "https://www.aosfatos.org/noticias/ibuprofeno-dengue/", "title": "Ibuprofeno não cura dengue",
          "reviewDate": "2026-09-02T10:00:00Z", "textualRating": "Falso", "languageCode": "pt"},
         {"publisher": {"name": "Agência X", "site": "agenciax.com"},
          "url": "https://agenciax.com/ibu", "title": "Ibuprofeno e dengue", "textualRating": "Selo estranho"},
         {"publisher": {"name": "Aos Fatos"}, "url": "https://www.aosfatos.org/noticias/ibuprofeno-dengue/",
          "textualRating": "Falso"}]},  # mesma URL repetida: 1 hit
    {"text": "Outra coisa", "claimReview": [{"publisher": {"name": "Boatos.org", "site": "boatos.org"},
                                            "url": "https://www.boatos.org/saude/x.html", "textualRating": "boato"}]}]}


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    fca._cache.clear()
    monkeypatch.setattr(config, "FACTCHECK_API_KEY", "chave-secreta-de-teste-123")
    monkeypatch.setattr(fca, "_excluidas", lambda: set())
    yield
    fca._cache.clear()


def _http(status=200, payload=PAYLOAD, chamadas=None):
    def fake(url, params=None, **kw):
        if chamadas is not None:
            chamadas.append((url, dict(params or {}), kw))
        return replay.Resposta(status, {}, json.dumps(payload).encode())
    return fake


def test_sem_chave_nao_chama_nada(monkeypatch):
    monkeypatch.setattr(config, "FACTCHECK_API_KEY", "")
    chamadas = []
    monkeypatch.setattr(replay, "http_get", _http(chamadas=chamadas))
    assert not fca.ativo() and fca.buscar("Ibuprofeno cura dengue em 3 dias") == [] and chamadas == []


def test_hits_no_formato_do_indice(monkeypatch):
    chamadas = []
    monkeypatch.setattr(replay, "http_get", _http(chamadas=chamadas))
    hits = fca.buscar("Ibuprofeno cura dengue em 3 dias")
    assert [h["url"] for h in hits] == ["https://www.aosfatos.org/noticias/ibuprofeno-dengue/",
                                        "https://agenciax.com/ibu", "https://www.boatos.org/saude/x.html"]
    h = hits[0]
    assert h["agencia"] == "aos-fatos" and h["agencia_nome"] == "Aos Fatos"
    assert h["veredito"] == "FALSO" and h["selo_original"] == "Falso"
    assert h["afirmacao_checada"] == "Ibuprofeno cura dengue" and h["titulo"] == "Ibuprofeno não cura dengue"
    assert h["data_pub"] == "2026-09-02T10:00:00Z" and "post no Facebook" in h["trecho"]
    assert hits[1]["veredito"] is None and hits[1]["selo_original"] == "Selo estranho"  # selo sem tabela: não vota
    assert hits[2]["agencia"] == "boatos" and hits[2]["veredito"] == "FALSO"
    url, params, kw = chamadas[0]
    assert url == fca.URL and params["languageCode"] == "pt-BR" and params["key"] == "chave-secreta-de-teste-123"
    fca.buscar("Ibuprofeno cura dengue em 3 dias")
    assert len(chamadas) == 1  # cache


def test_exclusoes_do_holdout_nao_entram(monkeypatch):
    from factcheck_mvp.ingestor import chave_url
    monkeypatch.setattr(fca, "_excluidas", lambda: {chave_url("https://www.boatos.org/saude/x.html")})
    monkeypatch.setattr(replay, "http_get", _http())
    assert "https://www.boatos.org/saude/x.html" not in [h["url"] for h in fca.buscar("Ibuprofeno cura dengue")]


def test_erro_vira_fallback_sem_vazar_a_chave(monkeypatch):
    fb = []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo, **kw: fb.append((onde, str(motivo), kw)))
    monkeypatch.setattr(replay, "http_get", _http(status=403, payload={"error": {"message": "API key not valid"}}))
    assert fca.buscar("Ibuprofeno cura dengue") == []
    assert fb and fb[0][0] == "factcheck_api" and "403" in fb[0][1]
    assert "chave-secreta" not in repr(fb)

    def quebra(*a, **k):
        raise TimeoutError("timeout em https://x?key=chave-secreta-de-teste-123")
    fca._cache.clear()
    monkeypatch.setattr(replay, "http_get", quebra)
    assert fca.buscar("Ibuprofeno cura dengue") == [] and "chave-secreta" not in repr(fb)


def test_consulta_curta_nao_busca(monkeypatch):
    chamadas = []
    monkeypatch.setattr(replay, "http_get", _http(chamadas=chamadas))
    assert fca.buscar("oi") == [] and chamadas == []


def test_chave_e_segredo_da_telemetria(monkeypatch):
    monkeypatch.setenv("GOOGLE_FACTCHECK_API_KEY", "valor-bem-secreto-999")
    assert "valor-bem-secreto-999" not in telemetria.redigir("x valor-bem-secreto-999 y")


def test_pipeline_usa_factcheck_como_base(monkeypatch):
    """Com o atalho da base ligado e a chave presente, os hits viram peças da base (origem factcheck-api)."""
    from factcheck_mvp.catalogo import Catalogo
    from factcheck_mvp.indice import Indice
    from factcheck_mvp.pipeline import Pipeline
    from factcheck_mvp.schemas import EntradaConsulta
    monkeypatch.setenv("INDICE_CHECAGENS", "1")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SITES", 0)
    monkeypatch.setattr(replay, "http_get", _http())
    ev = []
    orig = telemetria.evento
    monkeypatch.setattr(telemetria, "evento", lambda tipo, **d: (ev.append((tipo, d)), orig(tipo, **d))[1])

    class SemSerp:
        ativo, ultimo_motivo, uso_hoje = False, "sem chave", 0

        def buscar(self, params):
            return []
    pipe = Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(), serpapi=SemSerp())
    rel = asyncio.run(pipe.executar(EntradaConsulta(tipo="titulo", conteudo="Ibuprofeno cura dengue em 3 dias")))
    etapas = {e.nome: e for e in rel.etapas}
    assert "Google Fact Check" in etapas["base-checagem"].detalhe
    assert any(f.url == "https://www.aosfatos.org/noticias/ibuprofeno-dengue/" for f in rel.fontes)


def test_busca_aberta_do_eval_nao_chama_a_api(monkeypatch):
    """INDICE_CHECAGENS=0 (métrica principal do eval) desliga também a API: mede só a busca aberta."""
    from factcheck_mvp.catalogo import Catalogo
    from factcheck_mvp.indice import Indice
    from factcheck_mvp.pipeline import Pipeline
    from factcheck_mvp.schemas import EntradaConsulta
    monkeypatch.setenv("INDICE_CHECAGENS", "0")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SITES", 0)
    chamadas = []
    monkeypatch.setattr(replay, "http_get", _http(chamadas=chamadas))

    class SemSerp:
        ativo, ultimo_motivo, uso_hoje = False, "sem chave", 0

        def buscar(self, params):
            return []
    asyncio.run(Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(), serpapi=SemSerp())
                .executar(EntradaConsulta(tipo="titulo", conteudo="Ibuprofeno cura dengue em 3 dias")))
    assert not [c for c in chamadas if c[0] == fca.URL]
