"""Auditoria E, A1: fonte deixada de fora e repetições sem cache do juiz (só avaliação). Sem rede."""
import asyncio
import json

import curl_cffi.requests as _curl
import pytest

from factcheck_mvp import config, eval_modo, replay, telemetria
from factcheck_mvp.catalogo import Catalogo
from factcheck_mvp.indice import Indice
from factcheck_mvp.modelo_fake import MockDetector
from factcheck_mvp.pipeline import Pipeline
from factcheck_mvp.schemas import EntradaConsulta
from test_pipeline import FakeSerp, _pag, amb  # noqa: F401  (fixture `amb` do pipeline com juiz falso)


# ---------------------------------------------------------------- normalização
def test_normalizar_url_ignora_esquema_www_query_e_barra():
    base = eval_modo.normalizar_url("https://www.Lupa.uol.com.br/jornalismo/2026/10/01/x/?utm=1#topo")
    assert base == "lupa.uol.com.br/jornalismo/2026/10/01/x"
    assert eval_modo.normalizar_url("http://lupa.uol.com.br/jornalismo/2026/10/01/x") == base
    assert eval_modo.normalizar_url("https://lupa.uol.com.br/jornalismo/2026/10/01/y") != base


def test_so_age_com_eval_modo(monkeypatch):
    entrada = EntradaConsulta(tipo="titulo", conteudo="Café cura câncer", eval_excluir_urls=["https://a.test/x/"],
                              eval_repeticao=2)
    avisos = []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo, **k: avisos.append((onde, motivo)))
    monkeypatch.setattr(config, "EVAL_MODO", False)
    assert eval_modo.urls_excluidas(entrada) == frozenset() and eval_modo.sal_do_juiz(entrada) == ""
    assert [a[0] for a in avisos] == ["eval", "eval"]  # nada some em silêncio
    monkeypatch.setattr(config, "EVAL_MODO", True)
    assert eval_modo.urls_excluidas(entrada) == frozenset({"a.test/x"})
    assert eval_modo.sal_do_juiz(entrada) == "rep2"
    assert eval_modo.sal_do_juiz(EntradaConsulta(tipo="titulo", conteudo="abc")) == ""  # repetição 0: cassetes de sempre


def test_filtrar_pecas_emite_evento(monkeypatch):
    eventos = []
    monkeypatch.setattr(telemetria, "evento", lambda tipo, **d: eventos.append((tipo, d)))
    ex = frozenset({"a.test/x"})
    pecas = [{"url": "https://www.a.test/x/?a=1"}, {"url": "https://a.test/y"}]
    assert eval_modo.filtrar_pecas(pecas, ex, "web") == [{"url": "https://a.test/y"}]
    assert eventos == [("eval_exclusao", {"url": "https://www.a.test/x/?a=1", "onde": "web"})]
    assert eval_modo.filtrar_pecas(pecas, frozenset(), "web") == pecas


# ---------------------------------------------------------------- sal do cassete do LLM
class _Resp:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, texto):
        self.content = json.dumps({"choices": [{"message": {"content": texto}}]}).encode()

    def json(self):
        return json.loads(self.content)


def test_sal_cria_amostra_nova_so_para_o_juiz(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSETES_DIR", str(tmp_path))
    chamadas = []
    monkeypatch.setattr(_curl, "post", lambda url, **k: (chamadas.append(1), _Resp(f"v{len(chamadas)}"))[1])
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    with replay.modo("record"):
        a = replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz").json()
        b = replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz").json()
        assert a == b and len(chamadas) == 1                      # sem sal: o cassete é reusado
        with replay.com_sal("rep1"):
            replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz")
            replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz")
            assert len(chamadas) == 2                             # com sal: amostra nova, depois reusada
            with replay.com_sal("a3-1"):
                assert replay.sal_atual() == "rep1:a3-1"
                replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz")
            assert len(chamadas) == 3
            replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="afirmacoes")
            assert len(chamadas) == 3                             # outras finalidades não ganham sal
        assert replay.sal_atual() == ""
    with replay.modo("replay"):  # a simulação inteira é reproduzível: a repetição 1 volta do cassete
        with replay.com_sal("rep1"):
            assert replay.llm_post("http://llm.test/v1/chat/completions", payload, finalidade="juiz").cache == "hit"


# ---------------------------------------------------------------- pipeline: a fonte do caso some
def _base_com(url):
    return Indice.de_checagens(
        [{"url": url, "titulo": "É falso que café cura câncer", "afirmacao_checada": "Café cura câncer",
          "selo_original": "Falso", "veredito": "FALSO", "agencia": "aos-fatos",
          "trecho": "É falso que café cura câncer, segundo especialistas."}]
        + [{"url": f"https://lupa.uol.com.br/x{i}", "titulo": t, "afirmacao_checada": t, "selo_original": "Falso",
            "veredito": "FALSO", "agencia": "lupa", "trecho": t}
           for i, t in enumerate(["Vacina altera o DNA humano", "Urnas foram fraudadas em 2022",
                                  "Limão em jejum cura diabetes", "Governo vai confiscar poupança",
                                  "Água gelada causa gripe"])])


def test_fonte_deixada_de_fora_sai_do_indice_e_da_web(amb, monkeypatch):
    monkeypatch.setenv("INDICE_CHECAGENS", "1")
    monkeypatch.setattr(config, "EVAL_MODO", True)
    url = "https://www.aosfatos.org/noticias/cafe-cura-cancer-falso/"
    amb[url] = "É falso que café cura câncer, segundo especialistas. " * 50
    idx = _base_com(url)
    assert idx.buscar_checagens("Café cura câncer")                       # guarda anti-vácuo
    r_outra, corpo = _pag("https://g1.globo.com/saude/noticia/cafe.ghtml", "Café e câncer",
                          "Pesquisadores desmente que café cura câncer.")
    amb["https://g1.globo.com/saude/noticia/cafe.ghtml"] = corpo
    web = [{"link": url + "?utm=1", "title": "É falso que café cura câncer", "snippet": "É falso que café cura câncer"},
           r_outra]
    ent = EntradaConsulta(tipo="titulo", conteudo="Café cura câncer",
                          eval_excluir_urls=["http://aosfatos.org/noticias/cafe-cura-cancer-falso"])
    pipe = Pipeline(Catalogo.carregar(), idx, Indice(), serpapi=FakeSerp(web), detector=MockDetector())
    rel = asyncio.run(pipe.executar(ent))
    urls = [f.url for f in rel.fontes]
    assert not any("aosfatos" in u for u in urls) and any("globo.com" in u for u in urls)
    assert rel.onde_encontrado == "web"                                    # sem a fonte, o atalho não fecha o caso


def test_sem_eval_modo_a_fonte_continua(amb, monkeypatch):
    monkeypatch.setenv("INDICE_CHECAGENS", "1")
    monkeypatch.setattr(config, "EVAL_MODO", False)
    url = "https://www.aosfatos.org/noticias/cafe-cura-cancer-falso/"
    amb[url] = "É falso que café cura câncer, segundo especialistas. " * 50
    ent = EntradaConsulta(tipo="titulo", conteudo="Café cura câncer", eval_excluir_urls=[url])
    pipe = Pipeline(Catalogo.carregar(), _base_com(url), Indice(), serpapi=FakeSerp([]), detector=MockDetector())
    rel = asyncio.run(pipe.executar(ent))
    assert any("aosfatos" in f.url for f in rel.fontes) and rel.onde_encontrado == "base"
