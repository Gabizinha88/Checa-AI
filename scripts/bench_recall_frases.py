"""Recall do `Indice.buscar_checagens` sobre a simulação de usuário (sem rede, sem LLM).

Uso: python3 scripts/bench_recall_frases.py [--verbose]

Entrada: eval/frases_simulacao.json (gerado por scripts/gerar_frases_simulacao.py). A CONSULTA
de cada frase é o campo `afirmacao` (o texto sem a moldura de mensagem). Índice: o real,
data/checagens.jsonl, com Indice.de_checagens() e os padrões de factcheck_mvp.indice.

Medidas, com k = 5 primeiros devolvidos:
* checadas_falsas, verdadeiras, antigas_como_atuais: a checagem de origem (`url_checagem`,
  comparada com factcheck_mvp.ingestor.chave_url) está entre os k? e em 1º? Só entram frases
  com `url_checagem`; as demais aparecem na coluna "sem url" e não contam.
* fora_da_base: a checagem foi excluída da base de propósito; conta quantas frases têm
  QUALQUER retorno (todo retorno é falso positivo).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from factcheck_mvp import indice as mod  # noqa: E402
from factcheck_mvp.indice import Indice  # noqa: E402
from factcheck_mvp.ingestor import chave_url  # noqa: E402

ARQ_FRASES = RAIZ / "eval" / "frases_simulacao.json"
K = 5
ACHAR = ("checadas_falsas", "verdadeiras", "antigas_como_atuais")


def carregar_frases(caminho: Path = ARQ_FRASES):
    return json.loads(caminho.read_text(encoding="utf-8"))["frases"]


def _rotulo(hit: dict) -> str:
    return str(hit.get("afirmacao_checada") or hit.get("titulo") or "")


def medir(idx: Indice, frases, verbose: bool = False) -> dict:
    res = {c: {"n": 0, "achadas": 0, "primeiro": 0, "sem_url": 0} for c in ACHAR}
    res["fora_da_base"] = {"n": 0, "com_retorno": 0}
    for f in frases:
        cat = f.get("categoria")
        if cat in ACHAR:
            if not f.get("url_checagem"):
                res[cat]["sem_url"] += 1
                continue
            hits = idx.buscar_checagens(f["afirmacao"], k=K)
            chaves = [chave_url(h["url"]) for h in hits]
            alvo = chave_url(f["url_checagem"])
            res[cat]["n"] += 1
            res[cat]["achadas"] += alvo in chaves[:K]
            res[cat]["primeiro"] += chaves[:1] == [alvo]
            if verbose and alvo not in chaves[:K]:
                print(f"  MISS {f['id']}: {f['afirmacao'][:80]!r} -> "
                      f"{[_rotulo(h)[:50] for h in hits[:3]]}")
        elif cat == "fora_da_base":
            hits = idx.buscar_checagens(f["afirmacao"], k=K)
            res[cat]["n"] += 1
            res[cat]["com_retorno"] += bool(hits)
            if verbose and hits:
                print(f"  FP {f['id']}: {f['afirmacao'][:80]!r} -> "
                      f"{[(h.get('agencia'), _rotulo(h)[:50]) for h in hits[:3]]}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="lista os casos que falharam")
    a = ap.parse_args(argv)
    idx = Indice.de_checagens()
    frases = carregar_frases()
    print(f"checagens no índice: {len(idx.checagens)} | stemming RSLP: {mod.radicalizacao_ativa()} "
          f"| atalho ligado: {mod.habilitado()} | frases: {len(frases)}")
    res = medir(idx, frases, verbose=a.verbose)
    print(f"{'categoria':<20}{'n':>5}{'achadas@5':>12}{'em 1º':>8}{'sem url':>9}")
    for c in ACHAR:
        r = res[c]
        print(f"{c:<20}{r['n']:>5}{r['achadas']:>7}/{r['n']:<4}{r['primeiro']:>8}{r['sem_url']:>9}")
    r = res["fora_da_base"]
    print(f"{'fora_da_base':<20}{r['n']:>5}  com algum retorno: {r['com_retorno']}/{r['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
