"""Exportação para o Langfuse: conversão do trace local (sem rede)."""
import json

from factcheck_mvp import langfuse_export as lf


def _run(tmp_path):
    pasta = tmp_path / "20261013-000000-000001"
    pasta.mkdir()
    ev = [
        {"ts": "2026-10-13T18:00:00.000+00:00", "t_rel_ms": 0, "tipo": "run_inicio",
         "dados": {"entrada": "Café cura câncer", "tipo": "titulo", "config": {"IO_MODO": "replay"}}},
        {"ts": "2026-10-13T18:00:01.000+00:00", "t_rel_ms": 1000, "tipo": "llm",
         "dados": {"motor": "openrouter", "modelo": "m", "finalidade": "juiz", "latencia_ms": 500,
                   "prompt": [{"role": "user", "content": "x"}], "saida": "{}", "erro": None}},
        {"ts": "2026-10-13T18:00:02.000+00:00", "t_rel_ms": 2000, "tipo": "etapa",
         "dados": {"nome": "juiz", "status": "ok", "detalhe": "1 julgamento"}},
        {"ts": "2026-10-13T18:00:02.500+00:00", "t_rel_ms": 2500, "tipo": "fallback",
         "dados": {"onde": "serpapi", "motivo": "cota"}},
        {"ts": "2026-10-13T18:00:03.000+00:00", "t_rel_ms": 3000, "tipo": "run_fim",
         "dados": {"dur_ms": 3000, "ok": True, "nivel": "alta"}},
    ]
    (pasta / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    return pasta


def test_trace_vira_trace_span_generation_e_eventos(tmp_path):
    out = lf.eventos_do_run(_run(tmp_path), tags=["t"], metadados={"caso": "x:1"})
    tipos = [e["type"] for e in out]
    assert tipos == ["trace-create", "generation-create", "span-create", "event-create"]
    tr, gen, span, fb = (e["body"] for e in out)
    assert tr["input"] == "Café cura câncer" and tr["output"]["propensao"] == "alta" and tr["tags"] == ["t"]
    assert tr["metadata"]["caso"] == "x:1"
    assert gen["traceId"] == tr["id"] and gen["name"] == "llm:juiz" and gen["model"] == "m"
    assert gen["startTime"].startswith("2026-10-13T18:00:00.5")  # fim - latência
    assert span["name"] == "etapa:juiz" and span["startTime"] == "2026-10-13T18:00:00.000+00:00"
    assert fb["name"] == "fallback:serpapi" and fb["level"] == "WARNING"


def test_ids_deterministicos_reexportar_nao_duplica(tmp_path):
    p = _run(tmp_path)
    assert [e["id"] for e in lf.eventos_do_run(p)] == [e["id"] for e in lf.eventos_do_run(p)]


def test_lotes_respeitam_limite(monkeypatch):
    monkeypatch.setattr(lf, "LOTE_MAX_BYTES", 200)
    evs = [{"id": str(i), "body": {"x": "a" * 80}} for i in range(5)]
    lotes = list(lf._lotes(evs))
    assert sum(len(l) for l in lotes) == 5 and len(lotes) > 1


def test_sem_credenciais_nao_configurado(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert not lf.configurado()
    assert lf.main(["enviar", "x"]) == 2
