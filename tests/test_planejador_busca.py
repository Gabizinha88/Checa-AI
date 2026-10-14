"""Planejador de busca (Missão F / A5): relaxar consulta vazia, motor por domínio, ler mais, orçamento.
Sem rede: SerpAPI, LLM e deep crawl são falsos."""
import asyncio
import json
import re
import threading

import curl_cffi.requests as _curl
import pytest

from factcheck_mvp import agente, config, llm, planejador_busca as plan, replay, telemetria
from factcheck_mvp import pipeline as pl
from factcheck_mvp import serpapi_layer as camada
from factcheck_mvp.aprofundar import CorpoLido
from factcheck_mvp.catalogo import Catalogo
from factcheck_mvp.indice import Indice
from factcheck_mvp.modelo_fake import MockDetector
from factcheck_mvp.pipeline import Pipeline
from factcheck_mvp.schemas import EntradaConsulta

AGENCIAS = [{"site": "boatos.org"}, {"site": "lupa.uol.com.br"}]


def _org(url, titulo, snippet="snip"):
    return {"link": url, "title": titulo, "snippet": snippet, "displayed_link": url, "position": 1}


class Serp:
    """SerpAPI falsa: `rota(params) -> [resultados]` (None = payload vazio); guarda os params completos."""
    ativo = True
    ultimo_motivo = "ok"
    uso_hoje = 0

    def __init__(self, rota):
        self.rota = rota
        self.params = []
        self._t = threading.Lock()

    def buscar(self, params):
        with self._t:
            self.params.append(dict(params))
        r = self.rota(params)
        chave = "news_results" if params.get("engine") == "google_news" else "organic_results"
        return None if r is None else {chave: r}

    @property
    def chamadas(self):
        return [p["q"] for p in self.params]

    def engine_de(self, q):
        return q.get("engine", "google")

    def result_key(self, q):
        return "news_results" if q.get("engine") == "google_news" else "organic_results"


@pytest.fixture
def ligado(monkeypatch):
    monkeypatch.setattr(config, "BUSCA_PLANEJADOR", True)
    monkeypatch.setattr(config, "SERP_ENGINE", "google")


@pytest.fixture
def eventos(monkeypatch):
    ev = []
    orig = telemetria.evento
    monkeypatch.setattr(telemetria, "evento", lambda tipo, **d: (ev.append((tipo, d)), orig(tipo, **d))[1])
    return ev


def _planej(ev):
    return [d for t, d in ev if t == "planejador"]


def _alvo(texto, consulta=""):
    return agente.Alvo(indice=0, texto=texto, consulta=consulta or agente.consulta_padrao(texto))


def _onda1(alvos, cli, **kw):
    est = agente.EstadoBusca.novo(**kw)
    asyncio.run(agente.onda1(est, cli, alvos))
    return est


# ----------------------------------------------------------------------------- 1. relaxar (regras puras)
def test_passos_de_relaxamento_tiram_operadores_ruido_e_termos():
    q = '"disse" vídeo mostra Sérgio Sacani idosos decidir eleições 2026 (site:boatos.org OR site:lupa.uol.com.br)'
    passos = plan.passos_relaxamento(q)
    assert passos[0] == "disse vídeo mostra Sérgio Sacani idosos decidir eleições 2026"  # sem site:, aspas, OR
    assert passos[1] == "Sérgio Sacani idosos decidir eleições"                         # sem ruído e sem ano
    assert len(passos[2].split()) == 3 and set(passos[2].split()) <= set(passos[1].split())
    for p in passos:
        assert "site:" not in p and '"' not in p and " OR " not in p and len(p.split()) >= 2
    assert len({p.lower() for p in passos}) == len(passos)


def test_passos_nao_repetem_a_consulta_nem_relaxam_uma_palavra():
    assert plan.passos_relaxamento("ibuprofeno dengue") == []          # já mínima: 2 palavras e nada a tirar
    assert plan.passos_relaxamento("") == []
    assert plan.passos_relaxamento("vacina") == []


def test_proxima_relaxada_pula_as_ja_feitas():
    q = "vídeo mostra ibuprofeno curando dengue em hospitais 2026 (site:boatos.org)"
    p = plan.passos_relaxamento(q)
    assert plan.proxima_relaxada(q, []) == p[0]
    assert plan.proxima_relaxada(q, [p[0].upper()]) == p[1]            # comparação sem caixa
    assert plan.proxima_relaxada(q, p) is None


# ----------------------------------------------------------------------------- 2. motor por domínio
def test_site_vai_ao_organico_e_pura_ao_google_news(monkeypatch, ligado):
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    plano = agente.plano_onda1([_alvo("Vacina da covid altera o DNA humano")], AGENCIAS)
    pura, site = plano[0][0], plano[1][0]
    assert pura["engine"] == "google_news" and "num" not in pura
    assert "site:boatos.org" in site["q"] and site["engine"] == "google" and site["num"] == 10
    assert site["gl"] == "br" and site["hl"] == "pt-br"


def test_planejador_desligado_mantem_o_motor_unico(monkeypatch):
    monkeypatch.setattr(config, "BUSCA_PLANEJADOR", False)
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    plano = agente.plano_onda1([_alvo("Vacina da covid altera o DNA humano")], AGENCIAS)
    assert [q["engine"] for q, _, _ in plano] == ["google_news", "google_news"]
    assert all("num" not in q for q, _, _ in plano)


def test_troca_de_motor_emite_evento_so_quando_o_motor_muda(monkeypatch, ligado, eventos):
    agente.plano_onda1([_alvo("Vacina da covid altera o DNA humano")], AGENCIAS)  # SERP_ENGINE=google
    assert not [d for d in _planej(eventos) if d["decisao"] == "trocou_motor"]
    monkeypatch.setattr(config, "SERP_ENGINE", "google_news")
    agente.plano_onda1([_alvo("Vacina da covid altera o DNA humano")], AGENCIAS)
    ev = [d for d in _planej(eventos) if d["decisao"] == "trocou_motor"]
    assert len(ev) == 1 and ev[0]["de"] == "google_news" and ev[0]["para"] == "google" and "site:" in ev[0]["consulta"]


def test_resultado_do_organico_tem_o_mesmo_formato_do_news():
    est = agente.EstadoBusca.novo()
    org = {"search_metadata": {"created_at": "2026-10-13 12:00:00 UTC"}, "organic_results": [
        {"title": "Sérgio Sacani disse... é falso", "link": "https://www.boatos.org/a.html", "snippet": "s",
         "displayed_link": "https://www.boatos.org › a", "date": "13 de out. de 2026"}]}
    news = {"search_metadata": {"created_at": "2026-10-13 12:00:00 UTC"}, "news_results": [
        {"title": "Lupa: é falso", "link": "https://lupa.uol.com.br/a", "source": {"name": "Lupa"},
         "iso_date": "2026-10-13T10:00:00Z"}]}
    assert est.absorver(0, 1, {"q": "x site:boatos.org", "engine": "google"}, org) == (1, 1)
    assert est.absorver(0, 1, {"q": "x", "engine": "google_news"}, news) == (1, 1)
    d_org, d_news = (est.docs[k] for k in sorted(est.docs, key=lambda c: "lupa" in c))
    assert set(d_org) == set(d_news)                       # mesmas chaves de item
    assert d_org["origem"] == d_news["origem"] == "serpapi" and d_org["_engine"] == "google"
    assert d_org["fonte"]["nome"] and d_org["data_pub"]


# ----------------------------------------------------------------------------- 1. relaxar (execução)
def test_vazio_relaxa_e_acha(ligado, eventos):
    achado = _org("https://www.boatos.org/saude/ibuprofeno-dengue.html", "Ibuprofeno cura dengue? É falso")
    consulta = "disse vídeo mostra ibuprofeno curando dengue"
    cli = Serp(lambda p: [achado] if p["q"] == "ibuprofeno curando dengue" else [])
    est = _onda1([_alvo("Ibuprofeno cura dengue", consulta)], cli)
    assert cli.chamadas[:2] == [consulta, f"{consulta} (site:" + cli.chamadas[1].split("(site:", 1)[1]]
    assert "ibuprofeno curando dengue" in cli.chamadas                 # a relaxada sem ruído achou
    assert [d["url"] for d in est.pecas()] == ["https://www.boatos.org/saude/ibuprofeno-dengue.html"]
    rel = [d for d in _planej(eventos) if d["decisao"] == "relaxou"]
    assert rel and rel[0]["de"] == consulta and rel[0]["para"] == "ibuprofeno curando dengue" and rel[0]["nivel"] == 1
    assert est.buscas == len(cli.chamadas) <= est.teto


def test_vazio_sem_planejador_nao_relaxa(monkeypatch):
    monkeypatch.setattr(config, "BUSCA_PLANEJADOR", False)
    cli = Serp(lambda p: [])
    est = _onda1([_alvo("Ibuprofeno cura dengue", "disse vídeo mostra ibuprofeno curando dengue")], cli)
    assert len(cli.chamadas) == 2 and est.pecas() == []               # só pura + site:, como antes


def test_relaxamento_nao_repete_consulta_nem_relaxa_erro(ligado):
    cli = Serp(lambda p: None)                                         # payload None: erro/sem resposta
    cli.ultimo_motivo = "erro"
    _onda1([_alvo("x", "disse vídeo mostra ibuprofeno curando dengue")], cli)
    assert len(cli.chamadas) == 2                                      # erro não é vazio: não relaxa
    cli2 = Serp(lambda p: [])
    _onda1([_alvo("x", "disse vídeo mostra ibuprofeno curando dengue")], cli2)
    assert len(set(cli2.chamadas)) == len(cli2.chamadas)               # nenhuma consulta repetida
    assert all("site:" not in q for q in cli2.chamadas[2:])            # as relaxadas não têm site:


def test_relaxamento_respeita_o_teto_do_caso(ligado, eventos):
    cli = Serp(lambda p: [])
    est = _onda1([_alvo("x", "disse vídeo mostra ibuprofeno curando dengue hoje")], cli, teto=3, max_extras=0)
    assert est.buscas == 3 == len(cli.chamadas)                        # 2 da onda 1 + 1 relaxada; a 4ª não sai
    assert any(d["decisao"] == "parou_orcamento" for d in _planej(eventos))


def test_relaxamento_nao_come_a_reserva_das_ondas_extras(ligado):
    cli = Serp(lambda p: [])
    est = _onda1([_alvo("x", "disse vídeo mostra ibuprofeno curando dengue hoje")], cli, teto=3, max_extras=1)
    assert est.buscas == 2 and est.restante() == 1                     # o 3º ficou para a onda extra


def test_relaxamento_respeita_o_teto_global_da_serpapi(tmp_path, monkeypatch, ligado):
    """Busca real do cliente: `replay._reservar_serpapi` conta cada uma e recusa a que passa do teto."""
    monkeypatch.setenv("SERPAPI_USO_ARQ", str(tmp_path / "uso.json"))
    monkeypatch.delenv("SERPAPI_ORCAMENTO_RESTANTE", raising=False)
    monkeypatch.delenv("IO_MODO", raising=False)
    replay.definir_modo(None)
    replay.definir_teto_serpapi(2)
    vivas = []

    class Resp:
        status_code, headers = 200, {"Content-Type": "application/json"}
        content = b'{"organic_results": []}'

        def json(self):
            return {"organic_results": []}
    monkeypatch.setattr(_curl, "get", lambda url, **k: (vivas.append(k["params"]["q"]), Resp())[1])
    fb = []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo="", **k: fb.append((onde, motivo)))
    try:
        est = _onda1([_alvo("x", "disse vídeo mostra ibuprofeno curando dengue hoje")],
                     camada.SerpAPIClient(api_key="k"))
        assert len(vivas) == 2 and replay.uso_serpapi()["usadas_processo"] == 2   # a 3ª foi recusada, não paga
        assert json.loads((tmp_path / "uso.json").read_text(encoding="utf-8"))["total_live"] == 2
    finally:
        replay.definir_teto_serpapi(None)
    assert est.parar_motivo == "orcamento" and ("serpapi", "orcamento") in fb


# ----------------------------------------------------------------------------- 3. ler mais
def _payload(n):
    return {"organic_results": [_org(f"https://ex{i}.com/a{i}", f"t{i}") for i in range(n)]}


def test_ler_mais_absorve_alem_dos_8_ate_o_teto_sem_gastar_busca():
    est = agente.EstadoBusca.novo()
    est.planejador = True
    q = {"q": "vacina dna", "engine": "google", "num": 10}
    est.resultados_q["vacina dna"] = camada.ResultadoBusca(_payload(20), "ok")
    est.consultas["vacina dna"] = q
    est.queries[0] = ["vacina dna"]
    assert est.absorver(0, 1, q, _payload(20)) == (8, 8)               # leitura normal: 8
    assert est.ler_mais(0, 2, 12) == 4                                 # teto configurável: 12
    assert est.ler_mais(0, 2, 12) == 0                                 # nada além do teto
    assert est.ler_mais(0, 3, 16) == 4
    assert est.buscas == 0
    assert {est.onda_de[(c, 0)] for c in est.ordem[0]} == {1, 2, 3}


def _resumo(n, clusters=0):
    return agente.ResumoJulgamento(afirmacao=0, n={"SUSTENTA": 0, "REFUTA": 0, "RELATA_SEM_ENDOSSO": 0,
                                                   "NAO_TRATA": 0, "sem_juiz": 0, **n},
                                   clusters_postura=clusters, veredito_aplicavel=False, fora_do_tema=[])


def test_fora_do_tema_exige_maioria_nao_trata_e_nenhuma_postura():
    assert plan.fora_do_tema(_resumo({"NAO_TRATA": 5, "RELATA_SEM_ENDOSSO": 3}))
    assert not plan.fora_do_tema(_resumo({"NAO_TRATA": 2, "RELATA_SEM_ENDOSSO": 6}))
    assert not plan.fora_do_tema(_resumo({"NAO_TRATA": 7, "SUSTENTA": 1}, clusters=1))
    assert not plan.fora_do_tema(_resumo({}))                          # nada julgado: é busca vazia, não fora do tema


def test_ler_mais_so_com_planejador_e_so_para_fora_do_tema(monkeypatch):
    est = agente.EstadoBusca.novo()
    q = {"q": "vacina dna", "engine": "google"}
    est.resultados_q["vacina dna"], est.consultas["vacina dna"] = camada.ResultadoBusca(_payload(14), "ok"), q
    est.queries[0] = ["vacina dna"]
    est.absorver(0, 1, q, _payload(14))
    monkeypatch.setattr(config, "BUSCA_PLANEJADOR", True)
    fora = [(0, _resumo({"NAO_TRATA": 8}))]
    assert plan.ler_mais(est, fora, 2) == set()                        # estado criado sem planejador: não lê
    est.planejador = True
    assert plan.ler_mais(est, [(0, _resumo({"NAO_TRATA": 2, "SUSTENTA": 1}, clusters=1))], 2) == set()
    assert plan.ler_mais(est, fora, 2) == {0}
    assert plan.ler_mais(est, fora, 3) == set()                        # esgotou: o crítico cai na busca nova


# ----------------------------------------------------------------------------- 4. orçamento que sobra
def test_limite_de_ondas_extras():
    est = agente.EstadoBusca.novo(max_extras=1)
    assert (est.limite_extras(), est.n_passes()) == (1, 2)             # sem o planejador: como antes
    est.planejador = True
    assert est.limite_extras() == max(1, config.BUSCA_EXTRAS_MAX) and est.n_passes() == est.limite_extras() + 2
    zero = agente.EstadoBusca.novo(max_extras=0)
    zero.planejador = True
    assert zero.limite_extras() == 0                                   # quem desligou as extras não as ganha de volta


def test_criticar_respeita_o_limite_do_planejador():
    est = agente.EstadoBusca.novo(teto=9, max_extras=1)
    est.extras[0] = 1
    assert agente.criticar(est, _resumo({"NAO_TRATA": 3}))["decisao"] == "parar"
    est.planejador = True
    assert agente.criticar(est, _resumo({"NAO_TRATA": 3}))["decisao"] == "nova_onda"


# ----------------------------------------------------------------------------- flag desligada = comportamento atual
@pytest.mark.parametrize("motor", ["google", "google_news"])
def test_desligado_pede_as_mesmas_urls_de_sempre(monkeypatch, motor):
    """Compara as consultas pedidas (params completos) com a construção legada, sem o planejador."""
    monkeypatch.setattr(config, "SERP_ENGINE", motor)
    esperado = []
    for q in (camada._base("ibuprofeno cura dengue", "google"),
              camada.query_agencias("ibuprofeno cura dengue", AGENCIAS)[0]):
        esperado.append(agente.com_motor(q))
    pedidos = []
    for flag in (False, True):
        monkeypatch.setattr(config, "BUSCA_PLANEJADOR", flag)
        cli = Serp(lambda p: [_org("https://ex.com/a", "a")])
        est = agente.EstadoBusca.novo()
        asyncio.run(agente.onda1(est, cli, [_alvo("Ibuprofeno cura dengue", "ibuprofeno cura dengue")], AGENCIAS))
        pedidos.append(cli.params)
    assert pedidos[0] == esperado                                      # desligado: idêntico ao legado
    if motor == "google":
        assert pedidos[1] == pedidos[0]                                # ligado, motor já orgânico, achou: idêntico
    else:
        assert pedidos[1][0] == pedidos[0][0] and pedidos[1][1]["engine"] == "google"


# ----------------------------------------------------------------------------- pipeline (integração)
REGRAS = [(r"[^.]*(ibuprofeno[^.]*(é falso|não cura)|(é falso|não cura)[^.]*ibuprofeno)[^.]*", "REFUTA")]


def _llm(reformuladas):
    fila = list(reformuladas)
    chamadas = []

    def fake(messages, max_tokens, timeout_s, finalidade):
        conteudo = messages[-1]["content"]
        chamadas.append(finalidade)
        if finalidade == "afirmacoes":
            texto = conteudo.split('"""')[1].strip()
            return json.dumps([{"afirmacao": texto, "nucleo": texto, "polaridade": "afirma",
                                "consulta": texto.lower()}]), "fake"
        if finalidade == "padroes":
            return "NENHUM", "fake"
        if finalidade == "reformular":
            return json.dumps({"consulta": fila.pop(0)}), "fake"
        itens = [json.loads(l) for l in conteudo.split("ITENS:\n", 1)[1].splitlines() if l.strip()]
        out = []
        for it in itens:
            texto = f"{it['titulo']}\n{it['trecho']}"
            classe, cit = "NAO_TRATA", ""
            for rx, c in REGRAS:
                m = re.search(rx, texto, re.I)
                if m:
                    classe, cit = c, m.group(0).strip()
                    break
            out.append({"i": it["i"], "classe": classe, "citacao": cit})
        return json.dumps({"itens": out}), "fake"
    fake.chamadas = chamadas
    return fake


@pytest.fixture
def amb(monkeypatch, eventos):
    monkeypatch.setenv("INDICE_CHECAGENS", "0")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "SERP_ESTRATEGIA", "agente")
    monkeypatch.setattr(config, "SERP_ENGINE", "google")
    monkeypatch.setattr(config, "DISCOVERY_MAX_SITES", 0)
    monkeypatch.setattr(config, "AGENTE_MAX_BUSCAS", 9)
    monkeypatch.setattr(config, "AGENTE_MAX_ONDAS_EXTRAS", 1)
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
    return {"corpos": corpos, "eventos": eventos}


def _pag(amb, url, titulo, frase):
    dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    contexto = " ".join(f"Apuração própria de {dom}, parágrafo {k}, com detalhes do caso." for k in range(12))
    amb["corpos"][url] = f"{titulo}. {contexto} {frase} Mais contexto de {dom} sem relação direta."
    return _org(url, titulo, frase[:120])


def _rodar(texto, serp):
    pipe = Pipeline(Catalogo.carregar(), Indice.de_checagens([]), Indice(), serpapi=serp, detector=MockDetector())
    return asyncio.run(pipe.executar(EntradaConsulta(tipo="titulo", conteudo=texto)))


def _cenario_ler_mais(amb):
    """Os 8 primeiros resultados da consulta pura são fora do tema; as checagens estão do 9º em diante."""
    bolos = [_pag(amb, f"https://exame.com/casa/receita-bolo-{i}/", f"Receita de bolo {i}",
                  "O bolo leva três ovos e cobertura de chocolate.") for i in range(8)]
    lupa = _pag(amb, "https://www.agencialupa.org/verificacao/2026/09/ibuprofeno-dengue/",
                "Ibuprofeno não cura dengue", "É falso que o ibuprofeno cura dengue; o remédio aumenta o risco.")
    g1 = _pag(amb, "https://g1.globo.com/saude/noticia/2026/09/ibuprofeno-dengue.ghtml",
              "Médicos alertam sobre dengue", "Especialistas explicam que o ibuprofeno não cura dengue.")
    pura = "ibuprofeno cura dengue em 3 dias"
    return Serp(lambda p: bolos + [lupa, g1] if p["q"] == pura else [])


def test_pipeline_ler_mais_acha_a_checagem_sem_reformular(amb, monkeypatch, ligado):
    fake = _llm(["nunca usada"])
    monkeypatch.setattr(llm, "_local", fake)
    serp = _cenario_ler_mais(amb)
    rel = _rodar("Ibuprofeno cura dengue em 3 dias", serp)
    assert "reformular" not in fake.chamadas                            # nenhuma busca/LLM extra: leu o que já tinha
    assert {f.url: f.postura for f in rel.fontes}["https://www.agencialupa.org/verificacao/2026/09/ibuprofeno-dengue/"] == "REFUTA"
    assert rel.propensao == "alta", rel.justificativa
    leu = [d for t, d in amb["eventos"] if t == "planejador" and d["decisao"] == "leu_mais"]
    assert len(leu) == 1 and leu[0]["novos"] == 2 and leu[0]["onda"] == 2
    assert len(serp.chamadas) <= config.AGENTE_MAX_BUSCAS


def test_pipeline_sem_planejador_nao_chega_nas_checagens_alem_dos_8(amb, monkeypatch):
    monkeypatch.setattr(config, "BUSCA_PLANEJADOR", False)
    fake = _llm(["ibuprofeno dengue risco hemorragia checagem"])
    monkeypatch.setattr(llm, "_local", fake)
    serp = _cenario_ler_mais(amb)
    rel = _rodar("Ibuprofeno cura dengue em 3 dias", serp)
    assert serp.chamadas[-1] == "ibuprofeno dengue risco hemorragia checagem" and len(serp.chamadas) == 3
    assert rel.propensao == "indeterminada"                            # comportamento de antes: parou na 1ª extra


def test_pipeline_usa_o_orcamento_que_sobra_em_mais_de_uma_onda_extra(amb, monkeypatch, ligado):
    bolo = _pag(amb, "https://exame.com/casa/receita-bolo/", "Receita de bolo", "O bolo leva três ovos.")
    lupa = _pag(amb, "https://www.agencialupa.org/verificacao/2026/09/ibuprofeno-dengue/",
                "Ibuprofeno não cura dengue", "É falso que o ibuprofeno cura dengue; o remédio aumenta o risco.")
    g1 = _pag(amb, "https://g1.globo.com/saude/noticia/2026/09/ibuprofeno-dengue.ghtml",
              "Médicos alertam sobre dengue", "Especialistas explicam que o ibuprofeno não cura dengue.")
    n1, n2 = "ibuprofeno dengue risco hemorragia", "anvisa ibuprofeno contraindicado dengue"
    monkeypatch.setattr(llm, "_local", _llm([n1, n2]))

    def rota(p):
        if p["q"] == n2:
            return [lupa, g1]
        return [bolo] if p["q"] in ("ibuprofeno cura dengue em 3 dias", n1) else []
    serp = Serp(rota)
    rel = _rodar("Ibuprofeno cura dengue em 3 dias", serp)
    assert n1 in serp.chamadas and n2 in serp.chamadas                  # 2 ondas extras (antes: só 1)
    assert rel.propensao == "alta", rel.justificativa
    assert len(serp.chamadas) <= config.AGENTE_MAX_BUSCAS
