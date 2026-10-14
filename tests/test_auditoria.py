"""eval.auditoria: resumo legível de um trace (afirmações, juiz, decisão), sem rede."""
import json

from eval import auditoria


def _trace(pasta, eventos):
    pasta.mkdir(parents=True)
    (pasta / "trace.jsonl").write_text(
        "\n".join(json.dumps({"ts": "t", "t_rel_ms": 0, "tipo": t, "dados": d}, ensure_ascii=False)
                  for t, d in eventos), encoding="utf-8")
    return pasta


def test_resumo_traz_afirmacao_juiz_e_decisao(tmp_path):
    prompt_af = [{"role": "system", "content": "s"}, {"role": "user", "content": 'regras\nTEXTO: """\nTrump disse X\n"""'}]
    saida_af = json.dumps([{"afirmacao": "X", "nucleo": "X", "polaridade": "afirma", "consulta": "x"}])
    item = {"i": 0, "veiculo": "Boatos.org", "titulo": "Trump não disse X", "data": "2016-10-09",
            "trecho": "Resumindo: Trump não falou X."}
    prompt_j = [{"role": "user", "content": 'AFIRMAÇÃO A VERIFICAR: "X"\n\nregras\nITENS:\n' + json.dumps(item, ensure_ascii=False)}]
    saida_j = "```json\n" + json.dumps({"itens": [{"i": 0, "classe": "REFUTA", "pagina_diz": "nega",
                                                    "citacao": "Trump não falou X.", "raciocinio": "desmente"}]}) + "\n```"
    p = _trace(tmp_path / "20261013-000000-abc123", [
        ("run_inicio", {"entrada": "Trump disse X"}),
        ("llm", {"finalidade": "afirmacoes", "prompt": prompt_af, "saida": saida_af}),
        ("llm", {"finalidade": "juiz", "prompt": prompt_j, "saida": saida_j}),
        ("fallback", {"onde": "juiz", "motivo": "teste"}),
        ("evidencias", {"itens": [{"afirmacao": 0, "classe": "REFUTA", "citacao_verificada": True,
                                    "confiabilidade": "curada", "curada": True, "veredito": "FALSO",
                                    "origem_veredito": "indice", "data_pub": "2016-10-09", "url": "https://b/x"}]}),
        ("decisao", {"nivel": "alta", "score": 1.5, "why": "porque", "travas": {"L": 1.5, "motivo": "m", "votos": ["v"]}}),
    ])
    txt = auditoria.resumir_run(p, esperado="alta")
    assert "Esperado (gabarito): alta" in txt and "Texto enviado: 'Trump disse X'" in txt
    assert "[afirma] afirmacao='X'" in txt
    assert "Afirmação julgada: 'X'" in txt and "classe=REFUTA" in txt and "raciocinio='desmente'" in txt
    assert "Resumindo: Trump não falou X." in txt and "FALLBACK juiz: teste" in txt
    assert "nivel=alta" in txt and "AVISO" not in txt


def test_resumo_avisa_quando_prompt_nao_foi_gravado(tmp_path):
    p = _trace(tmp_path / "r1", [("run_inicio", {"entrada": "x"}),
                                 ("llm", {"finalidade": "afirmacoes", "saida": "não é json"})])
    txt = auditoria.resumir_run(p)
    assert "AVISO" in txt and "TELEMETRIA_PROMPTS" in txt and "Saída não-JSON" in txt


def test_cli_grava_um_md_por_run(tmp_path):
    _trace(tmp_path / "runs" / "r1", [("run_inicio", {"entrada": "x"})])
    assert auditoria.main(["r1", "--runs", str(tmp_path / "runs"), "--saida", str(tmp_path / "md")]) == 0
    assert (tmp_path / "md" / "r1.md").read_text(encoding="utf-8").startswith("# Run r1")
