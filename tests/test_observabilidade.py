"""Observabilidade (Missão F, item 8): saída de LLM completa com TELEMETRIA_PROMPTS, `usage` e `finish_reason`
no evento `llm`, "juiz indisponível" x "nada para julgar" e o mapeamento de usage no exportador Langfuse.
Sem rede."""
import asyncio
import json

import pytest

from factcheck_mvp import agente, config, decisao, llm, observabilidade, replay
from factcheck_mvp import langfuse_export as lf
from factcheck_mvp import telemetria as tel
from factcheck_mvp.decisao import AfirmacaoDecisao, Evidencias, ItemEvidencia, decidir
from test_pipeline import FakeSerp, TRES_REFUTAM, _prep, _rodar, amb  # noqa: F401  (fixture `amb`: juiz falso)


@pytest.fixture
def tele(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRIA", "1")
    monkeypatch.setenv("TELEMETRIA_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TELEMETRIA_PROMPTS", raising=False)
    monkeypatch.delenv("IO_MODO", raising=False)
    replay.definir_modo(None)
    yield tmp_path
    replay.definir_modo(None)


def _eventos(tmp, rid, tipo):
    linhas = (tmp / "runs" / rid / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x)["dados"] for x in linhas if json.loads(x)["tipo"] == tipo]


# ------------------------------------------------------------------ (a) saída do LLM completa
def _openrouter_devolve(monkeypatch, conteudo, **extra):
    """OpenRouter falso: devolve `conteudo` como mensagem do modelo (e campos extras da resposta)."""
    corpo = {"choices": [{"message": {"content": conteudo}, "finish_reason": "stop"}], **extra}
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "chave-de-teste-nao-vaza")
    monkeypatch.setattr(replay, "http_post",
                        lambda url, json=None, **k: replay.Resposta(200, {}, b"{}", url, _json_pronto=corpo))


def _reformular_longo(tele, monkeypatch, prompts):
    # raciocínio longo antes do JSON (modelo de raciocínio): bem acima do LIMITE_STR de 2000
    longo = "pensando na melhor consulta " * 150 + ' {"consulta": "ibuprofeno dengue hemorragia anvisa"}'
    _openrouter_devolve(monkeypatch, longo)
    if prompts:
        monkeypatch.setenv("TELEMETRIA_PROMPTS", "1")
    rid = tel.iniciar_run({})
    q, motor = agente.reformular(agente.Alvo(indice=0, texto="Ibuprofeno cura dengue em 3 dias",
                                             consulta="ibuprofeno cura dengue"),
                                 ["ibuprofeno cura dengue"], [])
    tel.finalizar_run(None)
    ev = [e for e in _eventos(tele, rid, "llm") if e["finalidade"] == "reformular"]
    return longo, q, motor, ev


def test_saida_de_reformular_completa_com_telemetria_prompts(tele, monkeypatch):
    longo, q, motor, ev = _reformular_longo(tele, monkeypatch, prompts=True)
    assert len(longo) > tel.LIMITE_STR and q == "ibuprofeno dengue hemorragia anvisa"
    assert len(ev) == 1 and ev[0]["saida"] == longo  # sem corte e sem "…[+N chars]"
    assert "chave-de-teste-nao-vaza" not in next((tele / "runs").rglob("trace.jsonl")).read_text(encoding="utf-8")


def test_saida_de_reformular_truncada_sem_telemetria_prompts(tele, monkeypatch):
    longo, _, _, ev = _reformular_longo(tele, monkeypatch, prompts=False)
    assert len(ev) == 1 and ev[0]["saida"] != longo
    assert len(ev[0]["saida"]) < len(longo) and ev[0]["saida"].endswith("chars]")  # trace padrão não infla
    assert "prompt" not in ev[0]


def test_saida_com_prompts_respeita_limite_prompt_e_redacao(tele, monkeypatch):
    monkeypatch.setenv("TELEMETRIA_PROMPTS", "1")
    monkeypatch.setattr(tel, "LIMITE_PROMPT", 3000)
    segredo = "Authorization: Bearer sk-or-v1-segredosegredo1234567890"
    _openrouter_devolve(monkeypatch, "y" * 5000 + " " + segredo)
    rid = tel.iniciar_run({})
    replay.llm_post("http://h/v1/chat/completions", {"model": "m", "messages": []}, finalidade="reformular")
    tel.finalizar_run(None)
    saida = _eventos(tele, rid, "llm")[0]["saida"]
    assert len(saida) < 3100 and saida.endswith("chars]")  # LIMITE_PROMPT vale mesmo com a flag
    assert "segredosegredo" not in saida


# ------------------------------------------------------------------ (c) usage e finish_reason no evento llm
USAGE_OR = {"prompt_tokens": 917, "completion_tokens": 297, "total_tokens": 1214, "cost": 0.0002402,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 258}}


def test_evento_llm_grava_usage_e_finish_reason(tele, monkeypatch):
    corpo = {"choices": [{"message": {"content": '{"consulta": "Sérgio Sacani e'}, "finish_reason": "length"}],
             "usage": USAGE_OR}
    monkeypatch.setattr(replay, "http_post",
                        lambda url, json=None, **k: replay.Resposta(200, {}, b"{}", url, _json_pronto=corpo))
    rid = tel.iniciar_run({})
    replay.llm_post("http://h/v1/chat/completions", {"model": "m", "messages": []}, finalidade="reformular")
    tel.finalizar_run(None)
    ev = _eventos(tele, rid, "llm")[0]
    assert ev["usage"] == {"prompt_tokens": 917, "completion_tokens": 297, "total_tokens": 1214,
                           "custo": 0.0002402, "reasoning_tokens": 258, "cached_tokens": 5}
    assert ev["finish_reason"] == "length"  # explica a saída cortada: o raciocínio consumiu os max_tokens


def test_evento_llm_sem_usage_nao_inventa_campos(tele, monkeypatch):
    monkeypatch.setattr(replay, "http_post", lambda url, json=None, **k: replay.Resposta(
        200, {}, b"{}", url, _json_pronto={"choices": [{"message": {"content": "ok"}}]}))
    rid = tel.iniciar_run({})
    replay.llm_post("http://h/v1/chat/completions", {"model": "m", "messages": []}, finalidade="juiz")
    tel.finalizar_run(None)
    ev = _eventos(tele, rid, "llm")[0]
    assert ev["saida"] == "ok" and "usage" not in ev and "finish_reason" not in ev


def test_usage_da_resposta_normaliza_chat_e_decisions():
    assert observabilidade.usage_da_resposta({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == {
        "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert observabilidade.usage_da_resposta({"usage": {"input_tokens": 10, "output_tokens": 2}}) == {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}  # endpoint Decisions (Jev)
    for ruim in (None, {}, {"usage": None}, {"usage": "x"}, {"usage": {}}, {"usage": {"cost": True}}, [1]):
        assert observabilidade.usage_da_resposta(ruim) is None
    assert observabilidade.finish_reason_da_resposta({"choices": []}) is None
    assert observabilidade.finish_reason_da_resposta({"choices": [{"finish_reason": "stop"}]}) == "stop"


def _trace_com_llm(tmp_path, **llm_extra):
    pasta = tmp_path / "20261014-000000-000001"
    pasta.mkdir()
    ev = [{"ts": "2026-10-14T18:00:00.000+00:00", "t_rel_ms": 0, "tipo": "run_inicio", "dados": {"entrada": "x"}},
          {"ts": "2026-10-14T18:00:01.000+00:00", "t_rel_ms": 1000, "tipo": "llm",
           "dados": {"motor": "openrouter", "modelo": "m", "finalidade": "reformular", "latencia_ms": 500,
                     "saida": "{}", "erro": None, **llm_extra}}]
    (pasta / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    return pasta


def test_exportador_mapeia_usage_para_usage_details_e_cost_details(tmp_path):
    pasta = _trace_com_llm(tmp_path, cache="live", finish_reason="length",
                           usage={"prompt_tokens": 917, "completion_tokens": 297, "total_tokens": 1214,
                                  "custo": 0.0002402, "reasoning_tokens": 258})
    gen = next(e["body"] for e in lf.eventos_do_run(pasta) if e["type"] == "generation-create")
    assert gen["usageDetails"] == {"input": 917, "output": 297, "total": 1214}
    assert gen["costDetails"] == {"total": 0.0002402}
    assert gen["metadata"]["reasoning_tokens"] == 258 and gen["metadata"]["finish_reason"] == "length"


def test_exportador_cassete_leva_tokens_mas_nao_custo(tmp_path):
    pasta = _trace_com_llm(tmp_path, cache="hit", usage={"prompt_tokens": 10, "completion_tokens": 5,
                                                          "total_tokens": 15, "custo": 0.5})
    gen = next(e["body"] for e in lf.eventos_do_run(pasta) if e["type"] == "generation-create")
    assert gen["usageDetails"]["total"] == 15 and "costDetails" not in gen


def test_exportador_sem_usage_nao_quebra(tmp_path):
    gen = next(e["body"] for e in lf.eventos_do_run(_trace_com_llm(tmp_path)) if e["type"] == "generation-create")
    assert "usageDetails" not in gen and "costDetails" not in gen and gen["model"] == "m"


# ------------------------------------------------------------------ (b) juiz indisponível x nada para julgar
def test_estado_juiz():
    assert observabilidade.estado_juiz(0, False) == "nada_para_julgar"
    assert observabilidade.estado_juiz(0, True) == "nada_para_julgar"
    assert observabilidade.estado_juiz(3, False) == "indisponivel"
    assert observabilidade.estado_juiz(3, True) == "disponivel"


def test_etapa_juiz_rotulos(tele):
    rid = tel.iniciar_run({})
    s0, d0 = observabilidade.etapa_juiz(0, {}, False, {}, 0, 0, "")
    sem = {(0, 0): {"classe": None, "erro": "RuntimeError: Unsloth fora do ar"}}
    s1, d1 = observabilidade.etapa_juiz(1, sem, False, {}, 1, 0, "")
    cont = {"SUSTENTA": 1, "REFUTA": 0, "RELATA_SEM_ENDOSSO": 0, "NAO_TRATA": 0}
    ok = {(0, 0): {"classe": "SUSTENTA"}}
    s2, d2 = observabilidade.etapa_juiz(1, ok, True, cont, 0, 0, "fake")
    tel.finalizar_run(None)
    assert s0 == "pulada" and d0.startswith("Nada para julgar") and "indisponível" not in d0
    assert s1 == "falha" and d1.startswith("Juiz indisponível") and "Unsloth fora do ar" in d1
    assert s2 == "ok" and "1 julgamento(s) via fake" in d2 and "1 sustentam" in d2
    estados = [e["estado"] for e in _eventos(tele, rid, "juiz_estado")]
    assert estados == ["nada_para_julgar", "indisponivel", "disponivel"]


def _etapa(rel, nome):
    return next(e for e in rel.etapas if e.nome == nome)


def test_pipeline_zero_paginas_e_nada_para_julgar(amb, tele):
    rel = _rodar("Café cura câncer", [])
    e = _etapa(rel, "juiz")
    assert e.status == "pulada" and e.detalhe.startswith("Nada para julgar")
    juiz = _eventos(tele, tel.ultimo_run_id(), "juiz_estado")
    assert juiz[-1]["estado"] == "nada_para_julgar" and juiz[-1]["n_candidatas"] == 0
    assert _eventos(tele, tel.ultimo_run_id(), "decisao")[-1]["travas"]["juiz_estado"] == "nada_para_julgar"


def test_pipeline_llm_fora_e_juiz_indisponivel(amb, tele, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Unsloth fora do ar")
    monkeypatch.setattr(llm, "_local", boom)
    rel = _rodar("Café cura câncer", _prep(amb, TRES_REFUTAM))
    e = _etapa(rel, "juiz")
    assert e.status == "falha" and e.detalhe.startswith("Juiz indisponível")
    assert rel.propensao == "indeterminada"  # a decisão não mudou: continua sem voto
    rid = tel.ultimo_run_id()
    juiz = _eventos(tele, rid, "juiz_estado")[-1]
    assert juiz["estado"] == "indisponivel" and juiz["n_candidatas"] == 3 and juiz["n_sem_julgamento"] == 3
    travas = _eventos(tele, rid, "decisao")[-1]["travas"]
    assert travas["juiz_estado"] == "indisponivel" and travas["juiz_disponivel"] is False


def test_pipeline_juiz_normal_nao_muda_o_texto_da_etapa(amb, tele):
    rel = _rodar("Café cura câncer", _prep(amb, TRES_REFUTAM))
    e = _etapa(rel, "juiz")
    assert e.status == "ok" and e.detalhe.startswith("3 julgamento(s) via") and "3 refutam" in e.detalhe
    assert _eventos(tele, tel.ultimo_run_id(), "decisao")[-1]["travas"]["juiz_estado"] == "disponivel"


def _item(classe="REFUTA"):
    return ItemEvidencia(url="https://g1.globo.com/a", afirmacao=0, cluster="g1", classe=classe, motor="openrouter",
                         citacao_verificada=True, curada=True, corpo_lido=True)


@pytest.mark.parametrize("itens,juiz_ok,esperado,juiz_disponivel", [
    ([], False, "nada_para_julgar", False),   # o bool legado continua False; o rótulo novo distingue
    ([], True, "nada_para_julgar", True),
    ([_item(None)], False, "indisponivel", False),
    ([_item()], True, "disponivel", True),
])
def test_decidir_travas_juiz_estado_sem_mudar_decisao(itens, juiz_ok, esperado, juiz_disponivel):
    ev = Evidencias(afirmacoes=[AfirmacaoDecisao(texto="x", nucleo="x")], itens=itens, juiz_disponivel=juiz_ok,
                    n_lidas=len(itens), n_consultadas=len(itens))
    d = decidir(ev)
    assert d.travas["juiz_estado"] == esperado and d.travas["juiz_disponivel"] is juiz_disponivel
    assert d.resumo_trace()["juiz_estado"] == esperado
    if not itens or esperado == "indisponivel":
        assert d.nivel == "indeterminada"
