"""P3 (auditoria de 13/10): leitura de páginas mais robusta a falha de rede, sem rede real."""
import asyncio
import socket

from factcheck_mvp import aprofundar as ap
from factcheck_mvp import replay, telemetria
from factcheck_mvp.aprofundar import CorpoLido


def _live(monkeypatch):
    monkeypatch.setattr(replay, "modo_atual", lambda: "live")
    monkeypatch.setattr(ap.time, "sleep", lambda s: None)


def test_dns_passageiro_resolve_na_segunda_tentativa(monkeypatch):
    _live(monkeypatch)
    chamadas = []

    def gai(host, porta):
        chamadas.append(host)
        if len(chamadas) == 1:
            raise socket.gaierror("falha temporária")
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(ap.socket, "getaddrinfo", gai)
    assert ap._url_segura("https://www.boatos.org/x") is True
    assert len(chamadas) == 2


def test_dns_que_nao_resolve_vira_erro_proprio_e_fallback(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(ap.socket, "getaddrinfo", lambda *a: (_ for _ in ()).throw(socket.gaierror("x")))
    eventos = []
    monkeypatch.setattr(telemetria, "fallback", lambda onde, motivo="", **kw: eventos.append((onde, motivo)))
    c = asyncio.run(ap._baixar("https://www.boatos.org/x", None, 5.0, 1000))
    assert c.erro == "dns: não resolveu o domínio" and "SSRF" not in c.erro
    assert eventos == [("dns", "não resolveu o domínio")]


def test_ip_privado_continua_sendo_ssrf(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(ap.socket, "getaddrinfo", lambda *a: [(socket.AF_INET, 1, 6, "", ("10.0.0.5", 0))])
    c = asyncio.run(ap._baixar("https://interno.exemplo.test/x", None, 5.0, 1000))
    assert c.erro == "url insegura (SSRF)"


def test_peca_da_base_tem_timeout_maior_e_uma_nova_tentativa(monkeypatch):
    monkeypatch.setattr(ap, "_cache", {})
    visto = []

    async def baixar(url, catalogo, timeout_pag, max_bytes, afirmacao=""):
        visto.append((url, timeout_pag))
        if len([v for v in visto if v[0] == url]) == 1:
            return CorpoLido(url=url, erro="timeout")
        return CorpoLido(url=url, corpo_lido=True, texto_completo="x" * 600, metodo="trafilatura")
    monkeypatch.setattr(ap, "_baixar", baixar)
    cands = [{"url": "https://base.test/a", "_timeout": 10.0, "_reler": True},
             {"url": "https://web.test/b"}]
    out = asyncio.run(ap.aprofundar(cands, None, por_afirm=2, timeout_pag=5.0, budget_total=30))
    assert out["https://base.test/a"].corpo_lido                        # leu na 2ª tentativa
    assert [t for u, t in visto if u == "https://base.test/a"] == [10.0, 10.0]
    assert [t for u, t in visto if u == "https://web.test/b"] == [5.0]  # web: 5 s, sem nova tentativa
    assert not out["https://web.test/b"].corpo_lido
