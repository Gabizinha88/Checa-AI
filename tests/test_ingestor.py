"""Ingestor: veredito (ClaimReview > título > categoria > corpo), merge idempotente, exclusões. Offline."""
import gzip
import json
import re
from pathlib import Path

import pytest

from factcheck_mvp import ingestor as ing

FIX = Path(__file__).parent / "fixtures" / "jsonld"


@pytest.mark.parametrize("titulo,selo,afirm", [
    ("É #FAKE que Lula menosprezou garis; vídeo foi cortado", "#FAKE", "Lula menosprezou garis"),
    ("É #FATO: Voto nas eleições vale como prova de vida", "#FATO", "Voto nas eleições vale como prova de vida"),
    ("É FATO: Vaca se levanta após ser atingida por trem", "FATO", "Vaca se levanta após ser atingida por trem"),
    ("É falso que Cristiano Ronaldo anunciou compra do Santa Cruz", "falso",
     "Cristiano Ronaldo anunciou compra do Santa Cruz"),
    ("Não é verdade que Lula tenha proposta para salário de R$ 3 mil; vídeos são IA", "não é verdade",
     "Lula tenha proposta para salário de R$ 3 mil"),
    ("Nikolas Ferreira não disse que Virgem Maria foi estuprada", "não disse",
     "Nikolas Ferreira disse que Virgem Maria foi estuprada"),
    ("Vídeo não mostra ato de Flávio em Recife, mas festa em Campina Grande", "não mostra",
     "Vídeo mostra ato de Flávio em Recife"),
    ("Áudio atribuído a VAR da expulsão de Osório é falso", "falso", "Áudio atribuído a VAR da expulsão de Osório"),
    ("Não há registros de pagamentos de Vorcaro a Flávio Dino", "Não há registros",
     "Pagamentos de Vorcaro a Flávio Dino"),
])
def test_veredito_do_titulo(titulo, selo, afirm):
    s, a = ing.veredito_do_titulo(titulo)
    assert s == selo and a == afirm


@pytest.mark.parametrize("titulo", [
    "É verdade que o presidente Lula construiu uma ponte gigante em São Paulo?",  # pergunta (E-farsas)
    "Veja o que é #FATO ou #FAKE no debate de candidatos ao Senado",              # resumo múltiplo
    "Checamos o discurso de Lula na ONU",
    "Flávio mentiu ao menos 43 vezes sobre elo com Vorcaro",
])
def test_titulo_sem_selo(titulo):
    assert ing.veredito_do_titulo(titulo)[0] is None


def test_veredito_do_corpo_e_categorias():
    assert ing.veredito_do_corpo("Um vídeo alega que X gastou R$ 7 bi. É falso. Por WhatsApp…") == "É falso"
    assert ing.veredito_do_corpo("Boato – circula nas redes…") == "Boato"
    assert ing.veredito_do_corpo("Texto neutro sobre economia.") is None
    assert ing.veredito_das_categorias(["Conspirações", "Falso", "Humor"], "e-farsas") == "Falso"
    assert ing.veredito_das_categorias(["Falso", "Verdadeiro"], "e-farsas") is None  # ambíguo


def test_chave_e_canonizacao_url():
    a = ing.chave_url("http://www.e-farsas.com/x.html?utm_source=a#frag")
    b = ing.chave_url("https://e-farsas.com/x.html")
    assert a == b
    assert ing.chave_url("https://projetocomprova.com.br/publica%C3%A7%C3%B5es/x/") == \
        ing.chave_url("https://projetocomprova.com.br/publicações/x")
    assert ing.canonizar_url("https://A.com/p/?utm_source=x#f") == "https://a.com/p/"


RSS = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
<channel><title>t</title>
<item><title>&#201; falso que caf&#233; cura c&#226;ncer</title><link>https://ag.test/a/?utm_source=rss</link>
<pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate><content:encoded><![CDATA[<p>Boato &#8211; texto</p>]]></content:encoded></item>
<item><title>Checagem com ClaimReview</title><link>https://ag.test/b/</link><category>Falso</category></item>
<item><title>Reportagem sem selo</title><link>https://ag.test/c/</link></item>
<item><title>Duplicada</title><link>https://ag.test/a/</link></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Falso: X</title>
<link rel="alternate" href="https://ag.test/atom"/><updated>2026-09-01T00:00:00Z</updated>
<category term="S&#225;tira"/></entry></feed>"""


def test_parse_feed_rss_e_atom():
    itens = ing.parse_feed(RSS)
    assert len(itens) == 4 and itens[0]["titulo"] == "É falso que café cura câncer"
    assert itens[0]["data"].startswith("2026-09-28") and "Boato" in ing.texto_de_html(itens[0]["corpo_html"])
    assert itens[1]["categorias"] == ["Falso"]
    at = ing.parse_feed(ATOM)
    assert at[0]["link"] == "https://ag.test/atom" and at[0]["categorias"] == ["Sátira"]
    assert ing.parse_feed(b"lixo") == []


def test_montar_registro_claimreview_vence_titulo():
    html = gzip.decompress((FIX / "comprova.html.gz").read_bytes()).decode()
    item = {"titulo": "Áudio de Renan Santos foi criado por IA", "link": "https://projetocomprova.com.br/x/",
            "data": None, "corpo_html": "", "categorias": []}
    r = ing.montar_registro(item, "comprova", html)
    assert r["origem"] == "claimreview" and r["veredito"] == "FALSO" and r["selo_original"] == "Falso"
    assert "Intercept" in r["afirmacao_checada"]
    assert set(r) >= {"url", "titulo", "agencia", "afirmacao_checada", "selo_original", "veredito",
                      "data_pub", "trecho", "origem"}


def test_mesclar_idempotente_manual_e_exclusoes():
    velho = [{"url": "https://a.test/1", "veredito": "FALSO", "origem": "rss-titulo"},
             {"url": "https://a.test/2", "veredito": None, "origem": "manual", "nota": "curado"},
             {"url": "https://a.test/3", "veredito": None, "origem": "rss-titulo"}]
    novo = [{"url": "http://www.a.test/1/", "veredito": None, "origem": "rss-titulo"},   # não apaga veredito
            {"url": "https://a.test/2", "veredito": "FALSO", "origem": "claimreview"},  # manual fica
            {"url": "https://a.test/4", "veredito": "FALSO", "origem": "claimreview"}]
    m = ing.mesclar(velho, novo, excluidas={ing.chave_url("https://a.test/3")})
    por = {ing.chave_url(r["url"]): r for r in m}
    assert por["a.test/1"]["veredito"] == "FALSO"
    assert por["a.test/2"]["origem"] == "manual"
    assert "a.test/3" not in por and "a.test/4" in por
    assert ing.mesclar(m, novo, {ing.chave_url("https://a.test/3")}) == m  # idempotente


def test_ingerir_offline(tmp_path, monkeypatch):
    cr = ('<script type="application/ld+json">{"@type":"ClaimReview","claimReviewed":"Y é verdade",'
          '"reviewRating":{"alternateName":"Enganoso"}}</script>')
    paginas = {"https://feed.test/rss": (200, RSS), "https://ag.test/b/": (200, cr.encode())}

    def falso_baixar(self, url):
        cod, corpo = paginas.get(url, (404, b""))
        return cod, corpo, url

    monkeypatch.setattr(ing.Baixador, "baixar", falso_baixar)
    monkeypatch.setattr(ing, "FONTES", [{"agencia": "ag", "nome": "Ag", "feeds": ["https://feed.test/rss"]}])
    monkeypatch.setattr(ing, "ARQ_CATALOGO", tmp_path / "nao-existe.json")
    excl = tmp_path / "excl.json"
    excl.write_text(json.dumps({"checagens": [{"url": "https://ag.test/c"}]}), encoding="utf-8")
    monkeypatch.setattr(ing, "ARQ_EXCLUIDAS", excl)
    monkeypatch.setattr(ing, "INTERVALO_HOST", 0.0)
    saida = tmp_path / "checagens.jsonl"
    res = ing.ingerir(max_por_feed=10, saida=saida, log=lambda *a: None)
    regs = [json.loads(l) for l in saida.read_text(encoding="utf-8").splitlines()]
    por = {ing.chave_url(r["url"]): r for r in regs}
    assert set(por) == {"ag.test/a", "ag.test/b"}          # c excluída, duplicada deduplicada
    assert por["ag.test/a"]["veredito"] == "FALSO" and por["ag.test/a"]["origem"] == "rss-titulo"
    assert por["ag.test/a"]["url"] == "https://ag.test/a/"  # sem utm
    assert por["ag.test/b"]["origem"] == "claimreview" and por["ag.test/b"]["veredito"] == "ENGANOSO"
    assert res["estatisticas"]["ag"]["n"] == 2
    ing.ingerir(max_por_feed=10, saida=saida, log=lambda *a: None)  # 2ª execução: mesmo conteúdo
    assert len(saida.read_text(encoding="utf-8").splitlines()) == 2


def test_mesclar_nao_rebaixa_claimreview():
    velho = [{"url": "https://a.test/1", "veredito": "ENGANOSO", "origem": "claimreview"}]
    novo = [{"url": "https://a.test/1", "veredito": "FALSO", "origem": "rss-titulo"}]
    assert ing.mesclar(velho, novo)[0]["origem"] == "claimreview"


def _wp(posts):
    return json.dumps([{"link": f"https://ag.test/p{i}/", "date": "2020-01-01T00:00:00",
                        "title": {"rendered": t}, "excerpt": {"rendered": ""},
                        "content": {"rendered": c}} for i, (t, c) in posts]).encode()


def test_historico_wpjson_independe_do_rss_e_aplica_filtros(monkeypatch):
    """--historico pagina a API do WordPress (com categorias excluídas na URL) sem
    mexer na paginação do RSS; ignorar_titulo vale para os itens da API."""
    pedidos = []
    pagina = [(i, (f"É falso que boato {i} aconteceu", "Boato – texto")) for i in range(50)]

    def falso_baixar(self, url):
        pedidos.append(url)
        if "wp-json" in url:
            pg = int(re.search(r"[?&]page=(\d+)", url).group(1))
            if pg <= 2:
                return 200, _wp(pagina if pg == 1 else
                                [(i + 50, (f"É falso que outro {i}", "Boato – x")) for i in range(49)]
                                + [(99, ("Resultado das eleições 2026 em X", ""))]), url
            return 200, b"[]", url
        return 404, b"", url

    monkeypatch.setattr(ing.Baixador, "baixar", falso_baixar)
    monkeypatch.setattr(ing, "INTERVALO_HOST", 0.0)
    fonte = {"agencia": "ag", "feeds": [], "wpjson": "https://ag.test/wp-json/wp/v2/posts",
             "wpjson_params": "&categories_exclude=1,2", "ignorar_titulo": r"^resultado das eleicoes"}
    itens = ing.coletar_itens(ing.Baixador(), fonte, 100, 1, [], paginas_historico=5)
    wp = [u for u in pedidos if "wp-json" in u]
    assert len(wp) == 3 and all(u.endswith("&categories_exclude=1,2") for u in wp)  # parou na página vazia
    assert len(itens) == 99 and not any("eleições" in i["titulo"] for i in itens)
    pedidos.clear()
    ing.coletar_itens(ing.Baixador(), fonte, 100, 1, [])  # sem --historico: só paginas_feed (1)
    assert len([u for u in pedidos if "wp-json" in u]) == 1


def test_ingerir_so_agencias_pedidas(tmp_path, monkeypatch):
    vistos = []
    monkeypatch.setattr(ing, "processar_fonte", lambda b, f, *a, **k: vistos.append(f["agencia"]) or [])
    monkeypatch.setattr(ing, "FONTES", [{"agencia": "a", "feeds": []}, {"agencia": "b", "feeds": []}])
    monkeypatch.setattr(ing, "ARQ_CATALOGO", tmp_path / "nao-existe.json")
    ing.ingerir(saida=tmp_path / "c.jsonl", log=lambda *a: None, agencias=["b"])
    assert vistos == ["b"]


@pytest.mark.parametrize("titulo,selo,afirm", [
    ("Água gelada faz mal para você; causa câncer e infarto #boato", "#boato",
     "Água gelada faz mal para você; causa câncer e infarto"),       # sufixo: título inteiro
    ("Tecnologia 5G causou morte de pássaros #boato", "#boato", "Tecnologia 5G causou morte de pássaros"),
    ("Hoax – Snowden fala sobre cataclismo em tempestade solar", "Hoax",
     "Snowden fala sobre cataclismo em tempestade solar"),          # Boatos.org 2013-2015
])
def test_selo_boatos_antigo(titulo, selo, afirm):
    literal, af = ing.veredito_do_titulo(titulo, "boatos")
    assert literal == selo and af == afirm
    assert ing.selos.normalizar(literal, "boatos") == "FALSO"


def test_selo_do_resumo_quando_corpo_nao_abre_com_boato():
    reg = ing.montar_registro({"titulo": "Boato sobre greve geral se espalha", "link": "https://b.test/x",
                               "corpo_html": "<p>O clima de manifestações...</p>",
                               "subtitulo": "Boato – Haverá greve geral amanhã"}, "boatos", None)
    assert reg["veredito"] == "FALSO" and reg["origem"] == "articlebody"


def test_reprocessar_offline_da_selo_sem_rede(tmp_path):
    arq = tmp_path / "c.jsonl"
    regs = [
        {"url": "https://b.test/1", "agencia": "boatos", "titulo": "Água gelada faz mal; causa câncer #boato",
         "afirmacao_checada": "Água gelada faz mal", "veredito": None, "origem": "rss-titulo", "trecho": ""},
        {"url": "https://b.test/2", "agencia": "boatos", "titulo": "Boato sobre greve se espalha",
         "veredito": None, "origem": "rss-titulo", "trecho": "Hoax – Haverá greve geral amanhã"},
        {"url": "https://b.test/3", "agencia": "boatos", "titulo": "X #boato", "veredito": "ENGANOSO",
         "selo_original": "enganoso", "origem": "claimreview"},  # ClaimReview não é tocado
    ]
    ing.gravar_jsonl(arq, regs)
    res = ing.reprocessar(arq, log=lambda *a: None)
    por = {r["url"]: r for r in ing.carregar_jsonl(arq)}
    assert res["selo"] == 2
    assert por["https://b.test/1"]["veredito"] == "FALSO"
    assert por["https://b.test/1"]["afirmacao_checada"] == "Água gelada faz mal; causa câncer"
    assert por["https://b.test/2"]["veredito"] == "FALSO" and por["https://b.test/2"]["origem"] == "articlebody"
    assert por["https://b.test/3"]["veredito"] == "ENGANOSO"


def test_jsonl_gz_grava_le_e_e_deterministico(tmp_path):
    """F4: a base versionada é .jsonl.gz; gravar duas vezes o mesmo conteúdo dá os mesmos bytes
    (gzip com mtime=0), então o PR diário só muda quando a base muda."""
    from factcheck_mvp.indice import Indice
    regs = [{"url": "https://ag.test/a/", "titulo": "É falso que X", "afirmacao_checada": "X"},
            {"url": "https://ag.test/b/", "titulo": "Ação e coração", "afirmacao_checada": "ç"}]
    arq = tmp_path / "checagens.jsonl.gz"
    ing.gravar_jsonl(arq, regs)
    b1 = arq.read_bytes()
    assert b1[:2] == b"\x1f\x8b" and gzip.decompress(b1).decode("utf-8").count("\n") == 2
    ing.gravar_jsonl(arq, regs)
    assert arq.read_bytes() == b1
    assert ing.carregar_jsonl(arq) == regs
    assert Indice.de_checagens(arq).checagens == regs
    plano = tmp_path / "checagens.jsonl"  # .jsonl sem gzip segue funcionando
    ing.gravar_jsonl(plano, regs)
    assert ing.carregar_jsonl(plano) == regs and Indice.de_checagens(plano).checagens == regs


def test_base_padrao_e_gz():
    from factcheck_mvp import indice
    assert ing.ARQ_CHECAGENS.name == "checagens.jsonl.gz" == indice.ARQ_CHECAGENS.name


def _rss_cats(itens):
    xs = "".join(f"<item><title>{t}</title><link>{l}</link>" + "".join(f"<category>{c}</category>" for c in cs)
                 + "</item>" for t, l, cs in itens)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{xs}</channel></rss>'.encode()


def test_lupa_so_categorias_de_checagem(monkeypatch):
    """F3: RSS da Lupa traz reportagem/opinião; fica o que é Verificação/Checagem/Achado ou tem selo."""
    lupa = next(f for f in ing.FONTES if f["agencia"] == "lupa")
    assert "categories=142,147,6915" in lupa["wpjson_params"]
    rss = _rss_cats([("Vídeo antigo de Lula é editado", "https://lupa.test/v", ["Jornalismo", "Verificação"]),
                     ("Zema recorre a fake contra Lula", "https://lupa.test/r", ["Jornalismo", "Reportagem"]),
                     ("É falso que X fez Y", "https://lupa.test/s", ["Jornalismo", "Reportagem"]),
                     ("Sem categoria no feed", "https://lupa.test/n", [])])
    monkeypatch.setattr(ing.Baixador, "baixar", lambda self, url: (200, rss, url) if "feed" in url else (404, b"", url))
    fonte = dict(lupa, feeds=["https://lupa.test/feed/"], wpjson=None)
    b = ing.Baixador()
    try:
        links = [it["link"] for it in ing.coletar_itens(b, fonte, 100, 1, [], None)]
    finally:
        b.fechar()
    assert links == ["https://lupa.test/v", "https://lupa.test/s", "https://lupa.test/n"]


def test_podar_fora_das_categorias(monkeypatch):
    lupa = next(f for f in ing.FONTES if f["agencia"] == "lupa")
    pag1 = json.dumps([{"link": f"https://lupa.test/c{i}/"} for i in range(100)]).encode()
    pag2 = json.dumps([{"link": "https://lupa.test/c100/"}]).encode()
    pedidas = []

    def baixar(self, url):
        pedidas.append(url)
        return (200, pag1 if "page=1&" in url else pag2, url)
    monkeypatch.setattr(ing.Baixador, "baixar", baixar)
    regs = [{"agencia": "lupa", "url": "https://lupa.test/c5", "veredito": None},     # na categoria: fica
            {"agencia": "lupa", "url": "https://lupa.test/rep", "veredito": None},    # fora e sem selo: sai
            {"agencia": "lupa", "url": "https://lupa.test/rep2", "veredito": "FALSO"},  # fora com selo: fica
            {"agencia": "boatos", "url": "https://boatos.test/x", "veredito": None}]  # outra agência: fica
    b = ing.Baixador()
    try:
        ficam, saem = ing.podar_fora_das_categorias(b, lupa, regs, log=lambda *a: None)
    finally:
        b.fechar()
    assert [r["url"] for r in saem] == ["https://lupa.test/rep"]
    assert len(ficam) == 3 and all("categories=142,147,6915" in u and "_fields=link" in u for u in pedidas)
    assert len(pedidas) == 2


def test_podar_cancela_com_lista_incompleta(monkeypatch):
    lupa = next(f for f in ing.FONTES if f["agencia"] == "lupa")
    pag1 = json.dumps([{"link": f"https://lupa.test/c{i}/"} for i in range(100)]).encode()
    monkeypatch.setattr(ing.Baixador, "baixar",
                        lambda self, url: (200, pag1, url) if "page=1&" in url else (503, b"", url))
    regs = [{"agencia": "lupa", "url": "https://lupa.test/rep", "veredito": None}]
    b = ing.Baixador()
    try:
        ficam, saem = ing.podar_fora_das_categorias(b, lupa, regs, log=lambda *a: None)
    finally:
        b.fechar()
    assert ficam == regs and saem == []


def _urlset(urls):
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls) + "</urlset>").encode()


def _indice_sm(filhos):
    return ('<?xml version="1.0"?><sitemapindex>' + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in filhos)
            + "</sitemapindex>").encode()


def test_parse_sitemap_urlset_e_indice():
    tipo, itens = ing.parse_sitemap(_urlset([("https://ag.test/a", "2026-10-08"), ("https://ag.test/b", "2024-01-02T10:00Z")]))
    assert tipo == "urlset" and itens == [("https://ag.test/a", "2026-10-08"), ("https://ag.test/b", "2024-01-02")]
    tipo, itens = ing.parse_sitemap(_indice_sm(["https://ag.test/s1.xml"]))
    assert tipo == "indice" and itens == [("https://ag.test/s1.xml", None)]
    assert ing.parse_sitemap(b"<html>nada</html>") == ("", [])


def test_coletar_do_sitemap_filtra_pula_conhecidas_e_respeita_teto(monkeypatch):
    paginas = {
        "https://ag.test/sitemap.xml": _indice_sm(["https://ag.test/sm-answer.202610.xml", "https://ag.test/sm-tags.xml",
                                                   "https://ag.test/sm-answer.202609.xml", "https://ag.test/sm-answer.202608.xml"]),
        "https://ag.test/sm-answer.202610.xml": _urlset([("https://ag.test/checagem/a", "2026-10-08"),
                                                        ("https://ag.test/sobre", "2026-10-08"),
                                                        ("https://ag.test/checagem/b", "2026-10-07")]),
        "https://ag.test/sm-answer.202609.xml": _urlset([("https://ag.test/checagem/c", "2026-09-30"),
                                                        ("https://ag.test/checagem/d", "2026-09-29")]),
    }
    pedidas = []

    def baixar(self, url):
        pedidas.append(url)
        return (200, paginas[url], url) if url in paginas else (404, b"", url)
    monkeypatch.setattr(ing.Baixador, "baixar", baixar)
    fonte = {"agencia": "ag", "feeds": [], "sitemaps": ["https://ag.test/sitemap.xml"],
             "sitemap_filhos": r"sm-answer\.", "sitemap_urls": r"/checagem/"}
    b = ing.Baixador()
    try:
        its = ing.coletar_itens(b, fonte, 100, 1, [], None, ja_na_base={ing.chave_url("https://ag.test/checagem/b")},
                                max_sitemap=2)
    finally:
        b.fechar()
    assert [i["link"] for i in its] == ["https://ag.test/checagem/a", "https://ag.test/checagem/c"]
    assert its[0]["data"] == "2026-10-08" and its[0]["_sitemap"] is True and its[0]["titulo"] == ""
    assert "https://ag.test/sm-tags.xml" not in pedidas            # filho fora do filtro
    assert "https://ag.test/sm-answer.202608.xml" not in pedidas   # teto atingido: para de ler


def test_sitemap_so_entra_com_selo(monkeypatch, tmp_path):
    cr = ('<html><head><script type="application/ld+json">{"@type":"ClaimReview","claimReviewed":"X fez Y",'
          '"reviewRating":{"alternateName":"Falso"},"headline":"Checagem X"}</script></head></html>')
    paginas = {"https://ag.test/sm.xml": _urlset([("https://ag.test/c/1", "2026-10-08"), ("https://ag.test/c/2", "2026-10-08")]),
               "https://ag.test/c/1": cr.encode(), "https://ag.test/c/2": b"<html><h1>Reportagem</h1></html>"}
    monkeypatch.setattr(ing.Baixador, "baixar", lambda self, url: (200, paginas[url], url) if url in paginas else (404, b"", url))
    fonte = {"agencia": "ag", "feeds": [], "sitemaps": ["https://ag.test/sm.xml"]}
    b = ing.Baixador()
    try:
        regs = ing.processar_fonte(b, fonte, 100, 1, False, set(), [], log=lambda *a: None)
        assert [r["url"] for r in regs] == ["https://ag.test/c/1"] and regs[0]["veredito"] == "FALSO"
        assert ing.processar_fonte(b, fonte, 100, 1, True, set(), [], log=lambda *a: None) == []  # --sem-paginas: nada
    finally:
        b.fechar()


def test_fontes_com_sitemap():
    por = {f["agencia"]: f for f in ing.FONTES}
    for ag in ("aos-fatos", "comprova", "afp-checamos", "uol-confere"):
        assert por[ag].get("sitemaps"), ag


def test_titulo_da_pagina():
    assert ing.titulo_da_pagina('<meta property="og:title" content="Montagem simula &#8216;g1&#8217;"/>'
                                '<title>Outro | Aos Fatos</title>') == "Montagem simula ‘g1’"
    assert ing.titulo_da_pagina("<title>É falso que X | Aos Fatos</title>") == "É falso que X"
    assert ing.titulo_da_pagina("<p>sem título</p>") == ""
