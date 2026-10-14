"""Agente com SERP_ENGINE=google_news: consultas no motor certo e leitura de news_results (sem rede)."""
from factcheck_mvp import agente, config

AGENCIAS = [{"site": "lupa.uol.com.br"}, {"site": "boatos.org"}]


def _alvos():
    return agente.alvos_de(["Vacina da covid altera o DNA humano"])


def test_padrao_google_nao_muda(monkeypatch):
    monkeypatch.setattr(config, "SERP_ENGINE", "google")
    plano = agente.plano_onda1(_alvos(), AGENCIAS)
    assert [q["engine"] for q, _, _ in plano] == ["google", "google"]
    assert all(q.get("num") == 10 for q, _, _ in plano)


def test_google_news_troca_o_motor_sem_num(monkeypatch):
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    plano = agente.plano_onda1(_alvos(), AGENCIAS)
    assert len(plano) == 2 and "site:" in plano[1][0]["q"]
    for q, _, _ in plano:
        assert q["engine"] == "google_news" and "num" not in q and q["gl"] == "br" and q["hl"] == "pt-br"


def test_onda_extra_usa_o_motor_configurado(monkeypatch):
    chamadas = []

    class Cli:
        ativo = True

        def buscar(self, q):
            chamadas.append(q)
            return {"news_results": []}
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    import asyncio
    est = agente.EstadoBusca.novo(teto=3)
    asyncio.run(agente.onda_extra(est, Cli(), [(0, "vacina covid dna", "determinística")], 2))
    assert chamadas and chamadas[0]["engine"] == "google_news"


def test_absorver_le_news_results_e_achata_grupos_de_historias():
    est = agente.EstadoBusca.novo()
    payload = {"search_metadata": {"created_at": "2026-10-13 12:00:00 UTC"}, "news_results": [
        {"title": "Lupa: é falso", "link": "https://lupa.uol.com.br/a", "source": {"name": "Lupa"}, "date": "13/10/2026"},
        {"stories": [  # grupo sem link próprio: as histórias entram no lugar
            {"title": "Boatos: boato", "link": "https://www.boatos.org/b", "source": {"name": "Boatos.org"}},
            {"title": "G1 explica", "link": "https://g1.globo.com/c", "source": {"name": "g1"}}]},
        "lixo", {"title": "sem link"}]}
    n, novos = est.absorver(0, 1, {"q": "x", "engine": "google_news"}, payload)
    assert (n, novos) == (3, 3)
    assert sorted(d["url"] for d in est.docs.values()) == [
        "https://g1.globo.com/c", "https://lupa.uol.com.br/a", "https://www.boatos.org/b"]


def test_absorver_google_organico_continua_igual():
    est = agente.EstadoBusca.novo()
    payload = {"organic_results": [{"title": "t", "link": "https://exemplo.test/a", "snippet": "s"}],
               "top_stories": [{"title": "u", "link": "https://exemplo.test/b", "source": "X"}]}
    n, novos = est.absorver(0, 1, {"q": "x", "engine": "google"}, payload)
    assert (n, novos) == (2, 2)
