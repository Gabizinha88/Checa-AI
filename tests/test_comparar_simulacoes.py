"""eval/comparar_simulacoes.py: estatística e agregação puras (sem rede, sem ler o .env)."""
import json

from eval import comparar_simulacoes as cmp


def test_wilson_e_mcnemar():
    lo, hi = cmp.wilson(39, 50)
    assert round(lo, 2) == 0.65 and round(hi, 2) == 0.87          # o IC do relatório E: 39/50 = [65%, 87%]
    assert cmp.wilson(0, 0) == (None, None)
    assert cmp.mcnemar_exato(0, 0) == 1.0
    assert abs(cmp.mcnemar_exato(1, 0) - 1.0) < 1e-9               # 1 discordante não prova nada
    assert cmp.mcnemar_exato(8, 0) < 0.01 and cmp.mcnemar_exato(5, 5) == 1.0


def _reg(cid, cat, rotulo, nivel, rep=0, resultado="acerto", **kw):
    return {"frase": {"id": cid, "categoria": cat, "rotulo": rotulo}, "erro": None, "nivel": nivel,
            "resultado": resultado, "repeticao": rep, "origem_resposta": kw.pop("origem", "web"), **kw}


def test_erro_grave_e_instabilidade():
    assert cmp.erro_grave("falso", "baixa") and cmp.erro_grave("verdadeiro", "alta")
    assert not cmp.erro_grave("falso", "media") and not cmp.erro_grave("verdadeiro", "baixa")
    dados = {"meta": {}, "casos": [
        _reg("a", "checadas_falsas", "falso", "alta", 0), _reg("a", "checadas_falsas", "falso", "alta", 1),
        _reg("b", "verdadeiras", "verdadeiro", "baixa", 0), _reg("b", "verdadeiras", "verdadeiro", "media", 1,
                                                                 resultado="inconclusivo"),
        _reg("c", "antigas_como_atuais", "enganoso", "indeterminada", 0, resultado="evidencia_insuficiente",
             alerta_data=True)]}
    res = cmp.resumo(dados)
    i = res["instabilidade"]
    assert i["casos"] == 2 and i["casos_que_mudaram"] == 1 and i["pares_discordantes"] == 1
    assert res["antigas"]["celulas"] == 1 and res["antigas"]["com_aviso_de_data"] == 1   # fora do geral
    assert res["geral_sem_antigas"]["celulas"] == 4 and res["geral_sem_antigas"]["ok"] == 3
    assert res["geral_sem_antigas"]["casos_ok"] == 1                # b: 1 de 2 repetições = empate = não


def test_pareado_por_celula_e_por_caso():
    a = cmp.resumo({"meta": {}, "casos": [_reg("x", "checadas_falsas", "falso", "alta", r) for r in range(3)]
                    + [_reg("y", "checadas_falsas", "falso", "media", r, resultado="inconclusivo") for r in range(3)]})
    b = cmp.resumo({"meta": {}, "casos": [_reg("x", "checadas_falsas", "falso", "media", r, resultado="inconclusivo")
                                          for r in range(3)]
                    + [_reg("y", "checadas_falsas", "falso", "alta", r) for r in range(3)]})
    p = cmp.pareado(a, b)
    assert p["celulas"]["n"] == 6 and p["celulas"]["so_A"] == 3 and p["celulas"]["so_B"] == 3
    assert p["casos"]["mudaram"] == ["x", "y"] and p["casos"]["p"] == 1.0


def test_metricas_de_trace(tmp_path):
    prompt = [{"role": "user", "content": "AFIRMAÇÃO...\nITENS:\n" + "\n".join(json.dumps(it, ensure_ascii=False) for it in [
        {"i": 0, "titulo": "Checagem", "trecho": "Lide. Conclusão: é falso que o café cura câncer."},
        {"i": 1, "titulo": "Notícia", "trecho": "Texto longo sem conclusão nenhuma sobre o tema."}])}]
    ev = [{"tipo": "llm", "dados": {"finalidade": "juiz", "prompt": prompt}},
          {"tipo": "etapa", "dados": {"nome": "agente-onda", "status": "parcial", "n_resultados": 0}},
          {"tipo": "etapa", "dados": {"nome": "agente-onda", "status": "ok", "n_resultados": 8}},
          {"tipo": "evidencias", "dados": {"itens": [{"agencia": True, "classe": "REFUTA"}]}}]
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    m = cmp.metricas_trace(tmp_path)
    assert (m["itens_juiz"], m["itens_com_veredito"]) == (2, 1)
    assert (m["buscas"], m["vazias"], m["achou_agencia"]) == (2, 1, True)
