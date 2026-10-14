#!/usr/bin/env python3
"""Extrai do dataset.zip os 3 CSVs que o treino usa para treinamento_modelo/data/.

Uso: python3 extrair_dataset.py [--zip CAMINHO]

Padrao: ../entrega/dataset/dataset.zip. Ignora a pasta __MACOSX do zip e
nao reextrai um CSV que ja existe com o mesmo tamanho.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZIP_PADRAO = HERE.parent / "entrega" / "dataset" / "dataset.zip"
DESTINO = HERE / "data"
NECESSARIOS = (
    "FakenewsBR_sanitized_v6.csv",
    "FakenewsBR_v6_labels.csv",
    "FakenewsBR_v6_provenance.csv",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", default=str(ZIP_PADRAO))
    args = ap.parse_args()

    zip_path = Path(args.zip)
    if not zip_path.exists():
        print(f"[ERRO] zip nao encontrado: {zip_path}")
        return 1
    DESTINO.mkdir(exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        membros = {Path(i.filename).name: i for i in zf.infolist()
                   if not i.filename.startswith("__MACOSX/")}
        faltando = [n for n in NECESSARIOS if n not in membros]
        if faltando:
            print(f"[ERRO] faltam no zip: {', '.join(faltando)}")
            return 1
        for nome in NECESSARIOS:
            info, alvo = membros[nome], DESTINO / nome
            if alvo.exists() and alvo.stat().st_size == info.file_size:
                print(f"  ok (ja existe)  {nome}")
                continue
            print(f"  extraindo       {nome} ({info.file_size / 1e6:.0f} MB)")
            with zf.open(info) as src, open(alvo, "wb") as dst:
                while bloco := src.read(1 << 20):
                    dst.write(bloco)
    print(f"CSVs em {DESTINO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
