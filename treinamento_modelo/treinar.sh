#!/usr/bin/env bash
# Reproduz o modelo final (run D1_dedup_s42): zip -> CSVs -> parquets -> dedup -> treino.
# Uso: ./treinar.sh [flags extras do train_bertimbau_v6.py, ex.: --smoke ou --device mps]
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 extraindo CSVs do dataset.zip"
python3 extrair_dataset.py

echo "== 2/4 prepare: CSVs -> prepare/processed/ (pool + splits por cluster)"
if [[ -f prepare/processed/v6_pool.parquet && -f prepare/processed/v6_splits.parquet ]]; then
  echo "  ok (ja existe; apague prepare/processed/ para refazer)"
else
  python3 prepare/prepare.py
fi

echo "== 3/4 dedup: remove duplicatas exatas -> prepare/processed_dedup/"
if [[ -f prepare/processed_dedup/v6_pool.parquet && -f prepare/processed_dedup/v6_splits.parquet ]]; then
  echo "  ok (ja existe; apague prepare/processed_dedup/ para refazer)"
else
  python3 prepare/dedup_exatas.py
fi

echo "== 4/4 treino BERTimbau (config da D1_dedup_s42) -> train/artifacts/D1_dedup_s42/"
python3 train/train_bertimbau_v6.py \
  --data prepare/processed_dedup/v6_pool.parquet \
  --splits prepare/processed_dedup/v6_splits.parquet \
  --run-id D1_dedup_s42 \
  --seed 42 \
  --best-metric macro_f1 \
  --warmup-steps 400 \
  --no-predict-all \
  "$@"
