"""/saude mostra o estado da chave do OpenRouter (F5): expirou em 07/10 sem aviso."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from factcheck_mvp import api, config, llm_openrouter as lor, replay


@pytest.fixture(autouse=True)
def _limpa_cache(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-teste-nao-vaza")
    lor._cache_status.clear()
    yield
    lor._cache_status.clear()


def _get(status, dados=None, chamadas=None):
    def fake(url, params=None, **kw):
        if chamadas is not None:
            chamadas.append((url, kw))
        return replay.Resposta(status, {}, __import__("json").dumps({"data": dados or {}}).encode())
    return fake


def test_chave_valida_traz_expiracao_e_saldo(monkeypatch):
    chamadas = []
    exp = (datetime.now(timezone.utc) + timedelta(days=13)).isoformat()
    monkeypatch.setattr(replay, "http_get", _get(200, {"expires_at": exp, "limit_remaining": 4.5,
                                                       "label": "sk-or-v1-abc...xyz"}, chamadas))
    st = lor.status_chave()
    assert st["ok"] is True and st["dias_restantes"] in (12, 13) and st["saldo"] == 4.5
    assert st["expira_em"] == exp[:10] and "aviso" not in st and "label" not in st
    assert chamadas[0][0] == "https://openrouter.ai/api/v1/key"
    assert "sk-teste-nao-vaza" not in str(st)
    lor.status_chave()
    assert len(chamadas) == 1  # cache de 10 min


def test_chave_expirada(monkeypatch):
    monkeypatch.setattr(replay, "http_get", _get(401))
    st = lor.status_chave()
    assert st == {"ok": False, "erro": "chave expirada ou inválida"}


def test_expira_em_menos_de_5_dias_avisa(monkeypatch):
    exp = (datetime.now(timezone.utc) + timedelta(days=2, hours=1)).isoformat()
    monkeypatch.setattr(replay, "http_get", _get(200, {"expires_at": exp}))
    st = lor.status_chave()
    assert st["ok"] is True and "expira em 2 dia" in st["aviso"]


def test_sem_chave_e_falha_de_rede(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert lor.status_chave() == {"ok": False, "erro": "OPENROUTER_API_KEY ausente"}
    lor._cache_status.clear()
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-x")

    def quebra(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(replay, "http_get", quebra)
    st = lor.status_chave()
    assert st["ok"] is None and "TimeoutError" in st["erro"]  # não sabe: não afirma que caiu


def test_saude_inclui_openrouter(monkeypatch):
    class _Serp:
        ativo, engine_default, uso_hoje = True, "google", 3

    class _Pipe:
        catalogo, idx_ver, idx_not, serpapi = [], [], [], _Serp()
    monkeypatch.setattr(api, "pipeline", lambda: _Pipe())
    monkeypatch.setattr(lor, "status_chave", lambda: {"ok": False, "erro": "chave expirada ou inválida"})
    r = TestClient(api.app).get("/saude").json()
    assert r["openrouter"] == {"ok": False, "erro": "chave expirada ou inválida"}


def test_bot_avisa_quando_llm_indisponivel(monkeypatch):
    from factcheck_mvp import telegram_bot as bot
    monkeypatch.setattr(lor, "status_chave", lambda: {"ok": False, "erro": "chave expirada ou inválida"})
    assert "avise o administrador" in bot.aviso_llm()
    monkeypatch.setattr(lor, "status_chave", lambda: {"ok": None, "erro": "TimeoutError"})
    assert bot.aviso_llm() == ""
    monkeypatch.setattr(lor, "status_chave", lambda: {"ok": True})
    assert bot.aviso_llm() == ""
