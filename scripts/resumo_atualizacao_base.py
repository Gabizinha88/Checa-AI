"""Descrição do PR diário da base de checagens: antes/depois por agência + benchmark do índice.

Uso (no workflow .github/workflows/atualizar-base.yml):
    git show HEAD:factcheck_mvp/data/checagens.jsonl.gz > /tmp/antes.jsonl.gz
    python3 scripts/resumo_atualizacao_base.py /tmp/antes.jsonl.gz bench.log > resumo.md
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from factcheck_mvp import ingestor as ing  # noqa: E402


def resumo(antes: list, depois: list, bench: str = "") -> str:
    ea, ed = ing.estatisticas(antes), ing.estatisticas(depois)
    k_antes = {ing.chave_url(r.get("url", "")) for r in antes}
    novos = [r for r in depois if ing.chave_url(r.get("url", "")) not in k_antes]
    linhas = ["Atualização automática da base de checagens (`data/checagens.jsonl.gz`).", "",
              f"**{len(antes)} → {len(depois)} registros** ({len(novos)} novos).", "",
              "| agência | antes | depois | com selo |", "|---|---:|---:|---:|"]
    for ag in sorted(set(ea) | set(ed)):
        a, d = ea.get(ag, {"n": 0}), ed.get(ag, {"n": 0, "com_veredito": 0})
        pct = 100.0 * d.get("com_veredito", 0) / max(1, d["n"])
        linhas.append(f"| {ag} | {a['n']} | {d['n']} | {pct:.0f}% |")
    if novos:
        linhas += ["", "<details><summary>Novos (até 30)</summary>", ""]
        linhas += [f"- {r['agencia']} · {r.get('veredito') or 'sem selo'} · [{(r.get('titulo') or r['url'])[:100]}]({r['url']})"
                   for r in novos[:30]]
        linhas += ["", "</details>"]
    trava = [l.strip() for l in bench.splitlines() if l.strip().startswith("-> ")][:1]
    if trava:
        linhas += ["", f"Benchmark do índice (trava: recall@3 ≥ 0,75 e FP ≤ 10%): `{trava[0][3:]}`"]
    linhas += ["", "Revisar antes do merge: checagem nova da **mesma história** de um caso `fora_do_indice` "
               "do eval vai para `checagens_holdout_excluidas.json` com `motivo_exclusao`."]
    return "\n".join(linhas) + "\n"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    antes = ing.carregar_jsonl(Path(argv[0]))
    bench = Path(argv[1]).read_text(encoding="utf-8") if len(argv) > 1 and Path(argv[1]).exists() else ""
    sys.stdout.write(resumo(antes, ing.carregar_jsonl(ing.ARQ_CHECAGENS), bench))
    return 0


if __name__ == "__main__":
    sys.exit(main())
