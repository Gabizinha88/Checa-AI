# Treinamento do modelo de detecção (BERTimbau v6)

Pipeline de treino do modelo final usado como sinal de estilo/credibilidade no
CrossCheckBR (`factcheck_mvp/modelo_fake.py`, via `FAKE_MODEL_PATH`).
Copiado de `FakenewsBR_model/pipeline/`, só com a **última versão**: run
**D1_dedup_s42** (BERTimbau base + pesos DFR + calibração Platt, pool v6 sem
duplicatas exatas).

## Pipeline

```
entrega/dataset/dataset.zip
   │  extrair_dataset.py
   ▼
data/*.csv  (sanitized_v6, v6_labels, v6_provenance)
   │  prepare/prepare.py      rótulos em camadas, pool de treino, splits por cluster
   ▼
prepare/processed/        v6_pool.parquet + v6_splits.parquet + prepare_stats.json
   │  prepare/dedup_exatas.py remove cópias extras de textos idênticos
   ▼
prepare/processed_dedup/  v6_pool.parquet + v6_splits.parquet
   │  train/train_bertimbau_v6.py  fine-tuning + early stopping + Platt + avaliação
   ▼
train/artifacts/D1_dedup_s42/   pesos (best/) + métricas
```

## Como rodar

```bash
cd treinamento_modelo
pip install -r requirements.txt
./treinar.sh                 # pipeline completo
./treinar.sh --smoke         # teste rápido com 1.024 linhas
./treinar.sh --device mps    # GPU do Mac
```

Sem GPU (CUDA ou MPS) o treino completo é muito lento. Nesse caso, gere os
parquets localmente (passos 1–3) e rode só o passo 4 no Colab: o
`train_bertimbau_v6.py` é autocontido e só precisa dos dois parquets.

## Estrutura

| caminho | conteúdo |
|---|---|
| `extrair_dataset.py` | extrai do zip os 3 CSVs usados no treino |
| `treinar.sh` | roda os 4 passos com a configuração da D1_dedup_s42 |
| `prepare/prepare.py`, `prepare/data.py` | CSVs → pool rotulado + splits (`full_iid`, cluster, seed 42) |
| `prepare/dedup_exatas.py` | gera `processed_dedup/` |
| `train/train_bertimbau_v6.py` | treino, calibração e avaliação |
| `resultado_final/D1_dedup_s42/` | métricas, config e calibração da run oficial (referência) |

## Resultado de referência (teste, 13.661 textos)

| acurácia | macro-F1 | pior grupo | ROC-AUC | ECE |
|---:|---:|---:|---:|---:|
| 82,5% | 0,785 | 0,617 (LIAR-BR) | 0,896 | 0,024 |

Detalhes em `resultado_final/D1_dedup_s42/metrics.json` e no relatório
técnico do repositório `FakenewsBR_model`.

## Observações

- Os pesos treinados não vêm nesta pasta. O treino os grava em
  `train/artifacts/D1_dedup_s42/best/`.
- `--check-vendor` do script de treino depende do código `legacy/` do
  FakenewsBR_model e não funciona aqui. Ele não é necessário para treinar.
