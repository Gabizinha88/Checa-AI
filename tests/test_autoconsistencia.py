"""Missão F / A3: autoconsistência do juiz quando UM julgamento decide o nível (sem rede, juiz falso).

Cobre: voto (3 concordam, 2×1, divergência de direção, sem maioria), detecção de "1 julgamento decide"
com a noção de cluster do agregador (2+ clusters não rejulga), flag desligada, falha do LLM numa
repetição (fallback registrado, abstenção, falha segura), variação sem cache no replay (chave própria,
gravada em record e relida em replay) e o gancho no pipeline.
"""
import asyncio
import json
from types import SimpleNamespace

import curl_cffi.requests as _curl
import pytest

from factcheck_mvp import autoconsistencia as ac
from factcheck_mvp import avaliador, config, decisao, llm, replay
from factcheck_mvp import telemetria as tel

from test_pipeline import TRES_REFUTAM, _juiz_falso, _pag, _prep, _rodar, amb  # noqa: F401 (amb é fixture)

URL = "https://www.lupa.news/checagem/cafe"


# ----------------------------------------------------------------------------- montagem
def _item(classe="REFUTA", url=URL, cluster="c1", af=0, veredito=None, origem=None):
    return decisao.ItemEvidencia(
        url=url, afirmacao=af, cluster=cluster, classe=classe, motor="llm-juiz:fake", citacao_verificada=True,
        curada=True, corpo_lido=True, veredito=veredito, origem_veredito=origem, veiculo="Lupa")


def _cenario(itens, n_af=1, polaridade="afirma"):
    """(ev, pecas, afs, julg) coerentes entre si: peça i ↔ item i."""
    afirmacoes = [decisao.AfirmacaoDecisao(texto=f"afirmação {i}", nucleo=f"afirmação {i}", polaridade=polaridade)
                  for i in range(n_af)]
    ev = decisao.Evidencias(afirmacoes=afirmacoes, itens=itens, n_lidas=len(itens), n_consultadas=len(itens))
    pecas = [{"url": it.url, "titulo": "t", "corpo": "x" * 400, "corpo_lido": True} for it in itens]
    afs = [SimpleNamespace(alvo=(lambda i=i: f"afirmação {i}")) for i in range(n_af)]
    julg = {(pi, it.afirmacao): {"classe": it.classe, "citacao": "cit original", "citacao_verificada": True,
                                 "raciocinio": "rac original", "motor": "llm-juiz:fake"}
            for pi, it in enumerate(itens)}
    return ev, pecas, afs, julg


def _selo_fake():
    """1 só cluster, REFUTA + selo FALSO do índice: alta."""
    return _item("REFUTA", veredito="FALSO", origem="indice")


class JuizFalso:
    """Substitui `avaliador.avaliar`: devolve as respostas na ordem e registra a variação ativa."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.variacoes = []

    def __call__(self, alvo, peca):
        self.variacoes.append((replay.sal_atual(), replay._temperatura_ctx.get()))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        if r is None:
            return {"posicao": None, "erro": "LLM fora do ar", "motor": "fallback-juiz", "corpo_lido": True}
        return {"posicao": r, "citacao": f"cit {r}", "citacao_score": 1.0, "citacao_verificada": True,
                "pagina_diz": "", "raciocinio": f"rac {r}", "motor": "llm-juiz:fake", "erro": None,
                "corpo_lido": True}


@pytest.fixture
def ligada(monkeypatch):
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA", True)
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA_K", 3)
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA_TEMP", 0.5)


@pytest.fixture
def trace(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRIA", "1")
    monkeypatch.setenv("TELEMETRIA_DIR", str(tmp_path / "runs"))
    rid = tel.iniciar_run({})

    def eventos(tipo):
        linhas = (tmp_path / "runs" / rid / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(x)["dados"] for x in linhas if json.loads(x)["tipo"] == tipo]
    yield eventos
    tel.finalizar_run(None)


def _aplicar(monkeypatch, itens, respostas, **kw):
    ev, pecas, afs, julg = _cenario(itens, **kw)
    juiz = JuizFalso(respostas)
    monkeypatch.setattr(avaliador, "avaliar", juiz)
    return ev, julg, juiz, ac.aplicar(ev, pecas, afs, julg)


# ----------------------------------------------------------------------------- voto
@pytest.mark.parametrize("classes,k,esperado", [
    (["REFUTA", "REFUTA", "REFUTA"], 3, ("REFUTA", "unanime")),
    (["REFUTA", "REFUTA", "RELATA_SEM_ENDOSSO"], 3, ("REFUTA", "maioria")),
    (["REFUTA", "RELATA_SEM_ENDOSSO", "REFUTA"], 3, ("REFUTA", "maioria")),
    (["REFUTA", "NAO_TRATA", "NAO_TRATA"], 3, ("NAO_TRATA", "maioria")),
    (["SUSTENTA", "SUSTENTA", "REFUTA"], 3, ("RELATA_SEM_ENDOSSO", "instavel")),   # direção divergente
    (["REFUTA", "SUSTENTA", "SUSTENTA"], 3, ("RELATA_SEM_ENDOSSO", "instavel")),
    (["REFUTA", "RELATA_SEM_ENDOSSO", "NAO_TRATA"], 3, ("RELATA_SEM_ENDOSSO", "instavel")),  # sem maioria
    (["REFUTA", "REFUTA", None], 3, ("REFUTA", "maioria")),        # falha = abstenção, 2 de 3 ainda é maioria
    (["REFUTA", None, None], 3, ("RELATA_SEM_ENDOSSO", "instavel")),  # falha segura
    (["REFUTA", "REFUTA"], 2, ("REFUTA", "unanime")),
    (["REFUTA", "RELATA_SEM_ENDOSSO"], 2, ("RELATA_SEM_ENDOSSO", "instavel")),
])
def test_votar(classes, k, esperado):
    assert ac.votar(classes, k) == esperado


def test_a_amostra_nunca_vira_a_direcao_oposta():
    """Falha segura: de qualquer mistura, o resultado é a classe da maioria ou RELATA, nunca a oposta."""
    import itertools
    todas = ["SUSTENTA", "REFUTA", "RELATA_SEM_ENDOSSO", "NAO_TRATA", None]
    for original in ("SUSTENTA", "REFUTA"):
        oposta = "REFUTA" if original == "SUSTENTA" else "SUSTENTA"
        for a, b in itertools.product(todas, todas):
            final, _ = ac.votar([original, a, b], 3)
            assert final != oposta, (original, a, b, final)


# ----------------------------------------------------------------------------- aplicação
def test_tres_concordam_mantem_e_nao_muda_o_nivel(ligada, monkeypatch, trace):
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], ["REFUTA", "REFUTA"])
    assert r["aplicada"] and r["nivel_antes"] == r["nivel_depois"] == "alta"
    assert ev.itens[0].classe == "REFUTA" and ev.itens[0].veredito == "FALSO"
    assert julg[(0, 0)]["classe"] == "REFUTA"
    assert julg[(0, 0)]["autoconsistencia"] == {"classes": ["REFUTA"] * 3, "desfecho": "unanime"}
    # sem cache: cada repetição com temperatura > 0 e sal próprio (chave de cassete distinta, como no A1)
    assert juiz.variacoes == [("autoc1", 0.5), ("autoc2", 0.5)]
    e = trace("autoconsistencia")[0]
    assert e["classes"] == ["REFUTA"] * 3 and e["desfecho"] == "unanime" and e["mudou"] is False
    assert e["nivel_antes"] == e["nivel_depois"] == "alta" and e["k"] == 3 and e["validas"] == 3


def test_dois_contra_um_vale_a_maioria(ligada, monkeypatch):
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], ["RELATA_SEM_ENDOSSO", "REFUTA"])
    assert ev.itens[0].classe == "REFUTA" and r["nivel_depois"] == "alta"
    assert r["itens"][0]["desfecho"] == "maioria"


def test_maioria_que_troca_de_classe_adota_a_citacao_da_repeticao(ligada, monkeypatch):
    """Postura sem selo: REFUTA, depois RELATA ×2 → RELATA (maioria) e a fonte deixa de votar."""
    ev, julg, juiz, r = _aplicar(monkeypatch, [_item("REFUTA")], ["RELATA_SEM_ENDOSSO", "RELATA_SEM_ENDOSSO"])
    assert r["nivel_antes"] == "media" and r["nivel_depois"] == "indeterminada"
    assert ev.itens[0].classe == "RELATA_SEM_ENDOSSO"
    assert julg[(0, 0)]["classe"] == "RELATA_SEM_ENDOSSO" and julg[(0, 0)]["citacao"] == "cit RELATA_SEM_ENDOSSO"
    assert julg[(0, 0)]["raciocinio"] == "rac RELATA_SEM_ENDOSSO"


@pytest.mark.parametrize("respostas", [["SUSTENTA", "REFUTA"], ["SUSTENTA", "SUSTENTA"],
                                       ["RELATA_SEM_ENDOSSO", "NAO_TRATA"]])
def test_divergencia_vira_relata_e_o_nivel_cai(ligada, monkeypatch, trace, respostas):
    """Com selo FALSO do índice, a RELATA não deixa o selo votar (P2): o nível cai para indeterminada."""
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], respostas)
    it = ev.itens[0]
    assert it.classe == "RELATA_SEM_ENDOSSO" and it.veredito == "FALSO"
    assert r["nivel_antes"] == "alta" and r["nivel_depois"] == "indeterminada"
    assert julg[(0, 0)]["classe"] == "RELATA_SEM_ENDOSSO" and julg[(0, 0)]["citacao"] == ""
    assert "divergiram" in julg[(0, 0)]["raciocinio"]
    e = trace("autoconsistencia")[0]
    assert e["desfecho"] == "instavel" and e["nivel_depois"] == "indeterminada"


def test_polaridade_nega_usa_a_direcao_do_voto(ligada, monkeypatch):
    """Usuário nega e a fonte SUSTENTA o núcleo: o voto é de propensão alta; o item decisivo é o SUSTENTA."""
    ev, julg, juiz, r = _aplicar(monkeypatch, [_item("SUSTENTA", veredito="VERDADEIRO", origem="indice")],
                                 ["SUSTENTA", "REFUTA"], polaridade="nega")
    assert r["aplicada"] and r["nivel_antes"] == "alta" and r["nivel_depois"] == "indeterminada"


def test_dois_clusters_nao_rejulga(ligada, monkeypatch, trace):
    itens = [_item("REFUTA", url="https://a.com/1", cluster="c1"),
             _item("REFUTA", url="https://b.com/2", cluster="c2")]
    ev, julg, juiz, r = _aplicar(monkeypatch, itens, [])
    assert not r["aplicada"] and r["motivo"] == "clusters=2" and juiz.variacoes == []
    assert [i.classe for i in ev.itens] == ["REFUTA", "REFUTA"]
    assert trace("autoconsistencia")[0]["desfecho"] == "nao_aplica"


def test_duas_afirmacoes_com_um_cluster_cada_nao_rejulga(ligada, monkeypatch):
    itens = [_item("REFUTA", url="https://a.com/1", af=0), _item("REFUTA", url="https://a.com/1", af=1)]
    ev, pecas, afs, julg = _cenario(itens, n_af=2)
    pecas = [pecas[0]]
    juiz = JuizFalso([])
    monkeypatch.setattr(avaliador, "avaliar", juiz)
    r = ac.aplicar(ev, pecas, afs, julg)
    assert not r["aplicada"] and r["motivo"] == "clusters=2" and juiz.variacoes == []


def test_um_cluster_com_varias_republicacoes_e_um_so_voto_e_rejulga(ligada, monkeypatch):
    """Mesma noção de cluster do agregador: 2 páginas do mesmo cluster = 1 voto = 1 julgamento decisivo cada."""
    itens = [_item("REFUTA", url="https://a.com/1", cluster="agencia:x"),
             _item("REFUTA", url="https://b.com/2", cluster="agencia:x")]
    ev, julg, juiz, r = _aplicar(monkeypatch, itens, ["REFUTA"] * 4)
    assert r["aplicada"] and len(juiz.variacoes) == 4 and r["nivel_antes"] == r["nivel_depois"]


def test_indeterminada_ou_sem_postura_nao_rejulga(ligada, monkeypatch):
    ev, julg, juiz, r = _aplicar(monkeypatch, [_item("RELATA_SEM_ENDOSSO")], [])
    assert not r["aplicada"] and r["motivo"] == "nivel_indeterminada" and juiz.variacoes == []
    # P2: RELATA + selo do índice não vota
    ev, julg, juiz, r = _aplicar(monkeypatch, [_item("RELATA_SEM_ENDOSSO", veredito="FALSO", origem="indice")], [])
    assert not r["aplicada"] and r["motivo"] == "nivel_indeterminada" and juiz.variacoes == []


def test_flag_desligada_nada_muda(monkeypatch):
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA", False)
    assert not ac.habilitada()
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], [])
    assert r == {"aplicada": False, "motivo": "desligada"} and juiz.variacoes == []
    assert ev.itens[0].classe == "REFUTA" and ev.itens[0].veredito == "FALSO" and "autoconsistencia" not in julg[(0, 0)]


def test_k_menor_que_2_desliga(monkeypatch):
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA", True)
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA_K", 1)
    assert not ac.habilitada()


# ----------------------------------------------------------------------------- falhas do LLM
def test_repeticao_que_falha_e_abstencao_e_registra_fallback(ligada, monkeypatch, trace):
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], ["REFUTA", None])
    assert ev.itens[0].classe == "REFUTA" and r["nivel_depois"] == "alta"  # 2 de 3 votos válidos = maioria
    fb = [f for f in trace("fallback") if f["onde"] == "autoconsistencia"]
    assert len(fb) == 1 and "repeticao 2" in fb[0]["motivo"] and "LLM fora do ar" in fb[0]["motivo"]
    e = trace("autoconsistencia")[0]
    assert e["classes"] == ["REFUTA", "REFUTA", None] and e["validas"] == 2 and e["desfecho"] == "maioria"


def test_todas_as_repeticoes_falham_cai_para_a_falha_segura(ligada, monkeypatch, trace):
    ev, julg, juiz, r = _aplicar(monkeypatch, [_selo_fake()], [None, RuntimeError("timeout")])
    assert ev.itens[0].classe == "RELATA_SEM_ENDOSSO"
    assert r["nivel_antes"] == "alta" and r["nivel_depois"] == "indeterminada"
    fb = [f for f in trace("fallback") if f["onde"] == "autoconsistencia"]
    assert len(fb) == 2 and "RuntimeError: timeout" in fb[1]["motivo"]


def test_peca_do_item_ausente_registra_fallback_e_nao_levanta(ligada, monkeypatch, trace):
    ev, pecas, afs, julg = _cenario([_selo_fake()])
    juiz = JuizFalso([])
    monkeypatch.setattr(avaliador, "avaliar", juiz)
    r = ac.aplicar(ev, [], afs, julg)
    assert not r["aplicada"] and ev.itens[0].classe == "REFUTA"
    assert any(f["onde"] == "autoconsistencia" for f in trace("fallback"))


def test_erro_inesperado_nao_derruba_a_consulta(ligada, monkeypatch, trace):
    ev, pecas, afs, julg = _cenario([_selo_fake()])
    monkeypatch.setattr(ac, "itens_decisivos", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    assert ac.aplicar(ev, pecas, afs, julg) == {"aplicada": False, "motivo": "erro"}
    assert any(f["onde"] == "autoconsistencia" for f in trace("fallback"))


# ----------------------------------------------------------------------------- replay: chave própria
class _Resp:
    def __init__(self, texto):
        self._p = {"choices": [{"message": {"content": texto}}]}
        self.status_code = 200
        self.content = json.dumps(self._p).encode()
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._p


@pytest.fixture
def cassetes(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRIA", "1")
    monkeypatch.setenv("TELEMETRIA_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CASSETES_DIR", str(tmp_path / "cass"))
    monkeypatch.delenv("IO_MODO", raising=False)
    replay.definir_modo(None)
    yield tmp_path / "cass"
    replay.definir_modo(None)


URL_LLM = "http://127.0.0.1:8888/v1/chat/completions"
PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "julgue"}], "temperature": 0.0, "max_tokens": 50}


def _chamar(**kw):
    return replay.llm_post(URL_LLM, PAYLOAD, finalidade="juiz", **kw).json()["choices"][0]["message"]["content"]


def test_sal_e_temperatura_dao_chave_propria_gravam_em_record_e_relêem_em_replay(cassetes, monkeypatch):
    enviados = []

    def post(url, **k):
        enviados.append(k["json"])
        return _Resp(f"resposta {len(enviados)}")
    monkeypatch.setattr(_curl, "post", post)
    replay.definir_modo("record")
    saidas = {"orig": _chamar()}
    for nome in ("autoc1", "autoc2"):
        with replay.com_sal(nome), replay.temperatura(0.5):
            saidas[nome] = _chamar()
    assert len(enviados) == 3 and len(list(cassetes.glob("*.json"))) == 3     # 3 chaves distintas
    assert enviados[0]["temperature"] == 0.0                                   # original intacto
    assert enviados[1]["temperature"] == enviados[2]["temperature"] == 0.5
    with replay.com_sal("autoc1"), replay.temperatura(0.5):  # record reusa o cassete da repetição
        assert _chamar() == saidas["autoc1"]
    assert len(enviados) == 3

    monkeypatch.setattr(_curl, "post", lambda *a, **k: pytest.fail("replay não pode chamar a rede"))
    replay.definir_modo("replay")
    lido = {}
    for nome in ("autoc2", "autoc1"):
        with replay.com_sal(nome), replay.temperatura(0.5):
            lido[nome] = _chamar()
    lido["orig"] = _chamar()
    assert lido == saidas and len(set(saidas.values())) == 3                    # determinístico, 3 respostas
    with replay.com_sal("autoc3"), replay.temperatura(0.5), pytest.raises(replay.ReplayMiss):  # não gravada
        _chamar()


def test_temperatura_vai_no_evento_llm_e_nao_afeta_decisions(cassetes, monkeypatch):
    monkeypatch.setattr(_curl, "post", lambda url, **k: _Resp("x"))
    rid = tel.iniciar_run({})
    with replay.com_sal("autoc2"), replay.temperatura(0.5):
        _chamar()
        enviado = {}
        monkeypatch.setattr(_curl, "post", lambda url, **k: enviado.update(k) or _Resp("x"))
        replay.llm_post("https://openrouter.ai/api/alpha/decisions", {"model": "m", "state": {}, "questions": {}})
    tel.finalizar_run(None)
    assert enviado["json"] == {"model": "m", "state": {}, "questions": {}}      # sem "messages": intacto
    linhas = (cassetes.parent / "runs" / rid / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    llms = [json.loads(x)["dados"] for x in linhas if json.loads(x)["tipo"] == "llm"]
    assert llms[0]["temperatura"] == 0.5 and llms[0]["sal"] == "autoc2"


# ----------------------------------------------------------------------------- gancho no pipeline
def _juiz_com_deriva(monkeypatch, classe_rep2):
    """Juiz falso do pipeline; na repetição `autoc2` troca a classe (sem mexer na citação)."""
    base = _juiz_falso()
    chamadas = []

    def fake(messages, max_tokens, timeout_s, finalidade):
        texto, motor = base(messages, max_tokens, timeout_s, finalidade)
        if finalidade == "juiz":
            var = (replay.sal_atual(), replay._temperatura_ctx.get())
            chamadas.append(var)
            if var[0].endswith("autoc2"):
                d = json.loads(texto)
                for it in d["itens"]:
                    it["classe"] = classe_rep2
                texto = json.dumps(d)
        return texto, motor
    monkeypatch.setattr(llm, "_local", fake)
    return chamadas


UM_CLUSTER = [_pag("https://g1.globo.com/saude/noticia/2024/01/cafe.ghtml", "Café cura câncer? Checagem",
                   "É falso que café cura câncer, segundo o INCA.")]


def test_pipeline_flag_ligada_estavel_mantem_o_nivel_e_conta_as_chamadas(amb, monkeypatch, ligada):
    chamadas = _juiz_com_deriva(monkeypatch, "REFUTA")
    rel = _rodar("Café cura câncer", _prep(amb, UM_CLUSTER))
    assert rel.propensao == "media" and rel.decisao["contagem"]["refuta"] == 1
    assert chamadas == [("", None), ("autoc1", 0.5), ("autoc2", 0.5)]  # 1 original + 2 repetições sem cache


def test_pipeline_flag_ligada_divergencia_vira_indeterminada(amb, monkeypatch, ligada):
    _juiz_com_deriva(monkeypatch, "SUSTENTA")
    rel = _rodar("Café cura câncer", _prep(amb, UM_CLUSTER))
    assert rel.propensao == "indeterminada"
    f = rel.fontes[0]
    assert f.postura == "RELATA_SEM_ENDOSSO"


def test_pipeline_flag_desligada_nao_rejulga(amb, monkeypatch):
    monkeypatch.setattr(config, "JUIZ_AUTOCONSISTENCIA", False)
    chamadas = _juiz_com_deriva(monkeypatch, "SUSTENTA")
    rel = _rodar("Café cura câncer", _prep(amb, UM_CLUSTER))
    assert rel.propensao == "media" and chamadas == [("", None)]


def test_pipeline_varios_clusters_nao_rejulga(amb, monkeypatch, ligada):
    chamadas = _juiz_com_deriva(monkeypatch, "SUSTENTA")
    rel = _rodar("Café cura câncer", _prep(amb, TRES_REFUTAM))
    assert rel.propensao == "alta" and chamadas == [("", None)] * 3


def test_refuta_de_agencia_curada_pesa_como_selo_e_tambem_passa_pela_autoconsistencia(ligada, monkeypatch):
    """B4/R1: sozinha, a REFUTA de agência curada já dá alta (1,5): é exatamente o julgamento único que A3 confere."""
    it = _item("REFUTA")
    it.agencia = True
    ev, julg, juiz, r = _aplicar(monkeypatch, [it], ["SUSTENTA", "REFUTA"])
    assert r["nivel_antes"] == "alta" and r["nivel_depois"] == "indeterminada"
    assert ev.itens[0].classe == "RELATA_SEM_ENDOSSO"
