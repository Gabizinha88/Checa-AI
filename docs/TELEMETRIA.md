# Telemetria, replay e eval — contrato

Infra para rodar o pipeline, observar o que aconteceu em cada etapa e comparar mudanças de forma
determinística. Três peças:

| Peça | Arquivo | Para quê |
|---|---|---|
| Telemetria | `factcheck_mvp/telemetria.py` | trace JSONL por execução (`runs/<run_id>/`) |
| Replay | `factcheck_mvp/replay.py` | grava/reproduz HTTP e LLM (`eval/cassettes/`) |
| Eval | `eval/run.py` + `eval/casos*.jsonl` | métricas por caso rotulado, baseline e gate |

CLI: `python3 -m factcheck_mvp.cli {checar,trace,runs}` (ver `CLAUDE.md`).

## 1. Telemetria

```python
from factcheck_mvp import telemetria

rid = telemetria.iniciar_run({"entrada": "...", "tipo": "titulo"})  # ou: with telemetria.run(meta) as rid:
telemetria.evento("etapa", nome="afirmacoes", status="ok", detalhe="1 afirmação")
telemetria.fallback("juiz", "OPENROUTER_API_KEY ausente")          # atalho p/ evento fallback
with telemetria.span("deep-crawl", n=3) as sp:                       # também `async with`
    sp.dados["lidos"] = 2                                            # vai no span_fim
with telemetria.finalidade("juiz"):                                  # rotula chamadas LLM do bloco
    ...
telemetria.finalizar_run(relatorio.model_dump(mode="json"))
```

- Estado em `contextvars`: cada task asyncio tem seu run; `asyncio.to_thread` herda o run.
- Sem run ativo, `evento` é no-op. `iniciar_run` é **reentrante**: se já há run (o eval abre um
  por caso), reusa o id e só o `finalizar_run` mais externo fecha.
- `Pipeline.executar` abre/fecha o run sozinho (o nome na especificação era `checar`).
- Ambiente: `TELEMETRIA=0` desliga; `TELEMETRIA_DIR` (padrão `runs/` na raiz). Sob pytest fica
  desligada, salvo `TELEMETRIA` explícita (testes usam `tmp_path`).
- `TELEMETRIA_PROMPTS=1` (auditoria de prompts e raciocínio): o evento `llm` ganha `prompt` (as mensagens
  enviadas, estruturadas) e `saida` por inteiro (limite de 60000 chars por string, em vez de 2000), sempre com a
  redação de segredos. Padrão desligado: o trace guarda só `prompt_sha` e a saída truncada. Os traces ficam grandes
  (páginas inteiras no prompt do avaliador); `runs/` não é versionado.
- Antes de gravar: redação de segredos (`api_key=`, `key=`, `token`, `/bot<token>/`,
  `Authorization`/`Bearer`, `sk-...` e os valores de `SERPAPI_KEY`/`OPENROUTER_API_KEY`/
  `TELEGRAM_TOKEN` do ambiente) e truncamento de strings em 2000 chars (`…[+N chars]`).
- Nunca levanta: falha de disco vira silêncio.

### Arquivos

`runs/<run_id>/trace.jsonl` — uma linha por evento:

```json
{"ts": "2026-09-28T19:52:59.256+00:00", "t_rel_ms": 6491.3, "tipo": "llm", "dados": {...}}
```

`runs/<run_id>/resultado.json` — `{run_id, inicio, fim, dur_ms, erro, meta, relatorio, metricas}`,
onde `relatorio` é o `RelatorioChecagem` final e `metricas` =
`{eventos_por_tipo, n_llm, n_llm_erro, llm_latencia_total_ms, llm_por_finalidade, n_http, http_cache,
n_fallbacks, fallbacks_por_onde, descartes_por_motivo, n_erros, dur_ms, nivel}`.

`run_id` = `AAAAMMDD-HHMMSS-<6 hex>` (ordena por tempo; `last` resolve o mais recente).

### Tipos de evento (campos de `dados`)

| tipo | campos | quem emite hoje |
|---|---|---|
| `run_inicio` | meta: `entrada`, `tipo`, `usar_llm`, `config{IO_MODO, SERP_ESTRATEGIA, SERP_ENGINE, serpapi_ativo, openrouter_ativo, OPENROUTER_MODEL, UNSLOTH_*, detector, JUIZ_MAX_NOTICIAS, LLM_DAILY_CAP}` (+ `caso_id`, `rotulo`, `esperado` no eval; `eval{modo, excluir_urls, repeticao}` quando o caso usa recursos de avaliação) | `Pipeline.executar`, eval |
| `run_meta` | meta extra de um `iniciar_run` aninhado | idem |
| `eval_exclusao` | `url`, `onde` (indice\|web): uma página tirada do caso porque é a `url_checagem` que a avaliação deixou de fora (só com `EVAL_MODO=1`; ver §2 e `eval/README.md`) | `eval_modo.filtrar_pecas`, `agente.EstadoBusca.absorver` |
| `run_fim` | `dur_ms`, `ok`, `erro`, `nivel` | `finalizar_run` |
| `etapa` | `nome`, `status` (ok\|parcial\|falha\|pulada), `detalhe`, `n_fontes`. Com `nome=agente-onda` (1 por busca do agente; só no trace, não no recibo): `onda`, `afirmacao`, `query`, `motivo` (consulta\|agencias\|reformulada), `n_resultados`, `n_novos`, `serpapi` (ok\|vazio\|cap\|erro\|timeout\|reuso), `cache`, `reuso` | `etapa()` do pipeline; `agente._executar_onda` |
| `agente` | decisão do crítico pós-juiz, 1 por afirmação por rodada: `afirmacao`, `decisao` (parar\|nova_onda), `motivo`, `contagens{SUSTENTA, REFUTA, RELATA_SEM_ENDOSSO, NAO_TRATA, sem_juiz, clusters_postura, veredito_aplicavel}`, `extras_feitas`, `buscas_restantes` | `agente.criticar` |
| `planejador` | decisão do planejador de busca (Missão F / A5; só com `BUSCA_PLANEJADOR=1`): `decisao` = `relaxou` (`onda`, `afirmacoes`, `de`, `para`, `nivel`, `motor`, `buscas_restantes`: consulta vazia trocada por uma mais frouxa) \| `trocou_motor` (`de`, `para`, `consulta`, `motivo`: `site:` vai ao Google orgânico quando o motor configurado é o News) \| `leu_mais` (`afirmacao`, `onda`, `novos`, `teto_por_busca`, `motivo`: resultados além dos 8 de payloads já pagos, sem busca nova) \| `parou_orcamento` (`onda`, `motivo`, `consultas`: relaxamento/busca cancelados por teto do caso ou da SerpAPI) | `planejador_busca`, `agente._relaxar_vazias` |
| `llm` | `motor` (llm-local\|openrouter\|openrouter-decisions), `modelo`, `finalidade`, `latencia_ms`, `prompt_sha`, `n_chars_prompt`, `saida` (truncada), `erro`, `cache` (live\|hit\|miss), `status`, `sal` (só quando o cassete do juiz/sintetizador foi salgado: repetição ou voto extra), `temperatura` (só sob `replay.temperatura`, A3) | `replay.llm_post` |
| `http` | `metodo`, `url` (redigida), `status`, `bytes`, `latencia_ms`, `cache` (live\|hit\|miss), `erro`, `truncado`, `gravado` | todo wrapper do `replay` |
| `fonte` | `url`, `estagio` (deep-crawl\|descoberta-site\|relatorio\|**data**), `decisao` (mantida\|descartada\|**descontada**), `motivo` (+ `tipo_fonte`, `corpo_lido`, `confianca` no estágio relatorio; no estágio `data`: `afirmacao`, `data_pub`, `data_pub_bruta`, `precisao`, `r`, `bits`) | aprofundar, descoberta_site, `Pipeline.executar`, `Pipeline._emitir_decisao` (estágio `data`, 1 por fonte com desconto temporal) |
| `fonte` (`estagio=aplicabilidade`) | 1 por par (peça da base julgada, afirmação) na fase base: `url`, `afirmacao` (índice), `classe`, `corpo_lido`, `veredito`, `decisao` (aplicavel\|inaplicavel), `motivo` (de `e_aplicavel`: corpo não lido, juiz não trata do fato, sem citação verificada, sem selo com direção, data incompatível, aplicável). E4: `janela` (dias do marcador relativo; None = sem), `marco` (`[data_evento ISO, folga]` da data explícita do fato; None = sem), `excedente` (dias além da janela ou do marco; None = não mede), `referencia` (YYYY-MM-DD do "hoje" da checagem; None = ausente) | `Pipeline._fase_base` |
| `bert_pagina` | `url`, `prob_fake` (média dos blocos de 192 tokens; só páginas com `corpo_lido`) | `Pipeline._ler` (B1b) |
| `evidencias` | `afirmacoes[]`: `texto`, `nucleo`, `polaridade`, `janela` (dias do marcador; None = sem), `marco` (`[data_evento ISO, folga]`; None = sem), `calculada` (True = janela e marco vieram de `aplicabilidade.marcas_da_afirmacao`, a mesma medida do gate E1: então None quer dizer "sem marcador de fato" e a decisão não recalcula pelo texto; False = snapshot antigo, caminho legado). `itens[]`: `url`, `afirmacao`, `cluster`, `classe`, `motor`, `citacao_verificada`, `curada`, `corpo_lido`, `veredito`, `origem_veredito`, `veiculo`, `confiabilidade`, `data_pub` (normalizada), `data_pub_bruta`, `data_pub_precisao`, `prob_fake_pagina` (BERT; None = sem modelo real). Também `vago`, `opiniao`, `rumor`, `juiz_disponivel`, `n_lidas`, `n_consultadas`, `texto_usuario`, `data_referencia` (YYYY-MM-DD; None = E4 desligado). É o insumo de `eval.decisao --gerar-snapshot`. | `Pipeline._executar`, antes de `decidir` (também no caminho sem afirmação, com `afirmacoes=[]`) |
| `autoconsistencia` | Missão F / A3 (`JUIZ_AUTOCONSISTENCIA=1`). 1 evento por consulta em que o gancho rodou. `desfecho="nao_aplica"` + `motivo` (`nivel_indeterminada`, `clusters=N` (≠ 1 cluster votante, mesma noção do agregador), `sem_julgamento_sustenta_refuta`) + `nivel`; ou, por item decisivo rejulgado: `url`, `afirmacao`, `cluster`, `k`, `temperatura`, `classes` (as K classes: a original e as repetições; `null` = repetição que falhou), `validas`, `desfecho` (`unanime`\|`maioria`\|`instavel`), `classe_original`, `classe_final`, `mudou`, `nivel_antes`, `nivel_depois` | `autoconsistencia.aplicar`, antes do evento `evidencias` (que já mostra as classes finais) |
| `fallback` | `onde`, `motivo` (+ extras) | ver tabela abaixo |
| `indice_descarte` | `url`, `score` (BM25), `cobertura` (do caminho com a 1ª frase do trecho), `motivo` (`min_termos`\|`cobertura`\|`numeros`: o que barrou esse caminho); até 3 por busca | `indice.buscar_checagens` (candidato com score ≥ corte rejeitado) |
| `sinal` | `motor`, `rotulo`, `valor`, `confianca`, `direcao` (via `agregador._direcao`, `None` se não existir), `peso` (`agregador.PESOS`), `evidencias` | pipeline, antes de `agregar` |
| `decisao` | `nivel` (final), `nivel_agregador`, `score`, `sinais` (lista `motor:rotulo`), `why`, `travas` (ver abaixo), `decisao` (`Decisao.to_dict()`, ver abaixo) | pipeline (fim, e no retorno "sem afirmação") |
| | `travas` é o `decisao.resumo_trace()`: inclui `L`, `p`, `motivo`, `votos`, `vereditos`, `vereditos_ignorados`, `conflitos`, `contagem`, `L_sem_desconto`, `nivel_sem_desconto` (contrafactual sem desconto temporal, E4) e `descontos` (≤6, `"<url[:60]> +<dias>d r=<r> −<bits>b"`) e `posturas_fracas` (≤6, `"af<i> <classe>@<url[:60]> pf=<prob_fake>"`: postura com fator BERT abaixo de 5% do peso, `FRACAO_MIN_VOTO`; não vota) | |
| `sintetizador` | (Missão F / A2; só com `SINTETIZADOR=1`, 1 por consulta, antes de `decisao`) `nivel_antes`, `nivel_depois`, `efeito` (`nenhum`\|`rebaixado: X -> Y`\|`buscar_mais sinalizado (nível mantido)`\|`rebaixar pedido, sem efeito (já no piso)`\|`pulado: nível já indeterminado`), `buscar_mais` (índices das afirmações), `afirmacoes[]`: `afirmacao`, `n_itens`, `classes{}`, `agencias`, `acao` (efetiva depois das guardas), `acao_llm`, `consistente`, `conflitos[]`, `motivo`, `erro` (fallback), `modelo`. A chamada LLM tem `finalidade=sintetizador`. O evento `decisao` traz `nivel` final e `nivel_agregador` (antes do sintetizador); `travas.sintetizador_rebaixou` / `travas.sintetizador_buscar_mais` quando agiu | `sintetizador.rodar` |
| `erro` | `onde`, `erro` | pipeline (exceção), replay (gravar cassete), eval |
| `span_inicio` / `span_fim` | `nome`, extras; no fim `dur_ms`, `ok`, `erro` | quem usar `span` |

Travas do evento `decisao` (`travas` = `resumo_trace()`, com estas chaves de `Decisao.travas`):

- `vago`, `opiniao`, `rumor`: entrada do pipeline (afirmação vaga, opinião/sátira, relato de segunda mão).
- `juiz_disponivel`: o juiz LLM respondeu. Sem ele, os votos vêm de fallback léxico.
- `sem_fonte_confiavel`: o |L| foi cortado para 0,99τ porque nenhum voto confiável **atual** (curado,
  institucional ou muito acessado; dentro da janela) estava no mesmo sentido.
- `so_fontes_de_outro_periodo` (E4): o |L| foi cortado porque só fontes de outro período (descontadas)
  votaram na direção. Só age no caminho com desconto.
- `parte_sem_checagem_atual` (D-b, 09/10): alguma afirmação saiu da conjunção por ter só fonte de outro
  período (`fora_da_conjuncao` em `por_afirmacao`) e o nível sairia baixa: o |L| foi cortado para −0,99τ e
  o texto fica em média. A alta pelas partes atuais não é afetada.
- `data_incompativel`: há fontes com desconto temporal (`descontos_temporais` não vazio).
- `sintetizador_rebaixou` (Missão F / A2, `SINTETIZADOR=1`): o sintetizador reduziu o nível (alta/baixa -> media ou
  indeterminada; media -> indeterminada). Nunca sobe nem inverte o lado. `parametros.sintetizador` guarda `nivel_antes`,
  `conflitos` e a frase mostrada na justificativa; `log_odds`/`prob` continuam os do agregador.
- `sintetizador_buscar_mais` (Missão F / A2): o sintetizador pediu mais busca. É só um SINAL (limitação + esta trava):
  a onda extra depois da decisão não foi implementada (exigiria reabrir o laço de `_ondas_extras`, julgar de novo e
  chamar `decidir` outra vez), então o nível não muda por causa dele.

`finalidade` de LLM: explícita (`llm_post(finalidade=...)` ou `with telemetria.finalidade(...)`) ou
inferida do chamador: `afirmacoes`, `padroes`, `reformular` (agente: consulta da onda extra), `juiz-resumo` (`juiz_llm.resumir`), `juiz`
(`juiz_llm.termometro`), `decisions-tipo-site`, senão `modulo.funcao`.

### Pontos de `fallback` instrumentados (`onde`)

`afirmacoes`, `afirmacoes.openrouter`, `padroes`, `padroes.openrouter`, `openrouter.chat` (cada
modelo que falhou; teto diário), `juiz` (item julgado por fallback léxico; timeout 120s),
`juiz.lexico-top3` (C1 do review: 3 fontes marcadas relevantes sem leitura), `serpapi`
(erro de rede/HTTP; `motivo="orcamento"` quando o teto recusou; cota da conta esgotada),
`serpapi.cap` (teto diário interno), `serpapi.agencias` (catálogo indisponível: sem `site:` de agências),
`agente` (onda passou de `AGENTE_TIMEOUT_S`, parcial preservado; falha nas ondas extras),
`agente.reformular` (LLM de reformulação falhou → consulta determinística), `autoconsistencia` (A3: repetição do juiz que falhou — conta como abstenção no voto —, peça do item decisivo ausente ou erro inesperado no gancho), `deep-crawl`, `aprofundar`,
`descoberta-catalogo`, `descoberta-site`, `descoberta-site.jev`, `bert_pagina` (o detector falhou numa
página), `raciocinio` (C1: a linha do raciocínio do avaliador foi omitida por conter expressão proibida;
`motivo` = `expressao binaria omitida` ou `expressao proibida omitida`. Emitido na renderização do bot e da
web, então é no-op fora de um run ativo).
`relogio` (E4, em replay: sem data de referência → E4 desligado neste caso; `motivo` = `data de referência
não gravada` (não há cassete `relogio://hoje`) ou `cassete sem data` (o arquivo existe, mas não é um cassete de relógio com data válida: JSON quebrado, estrutura errada ou data inválida)),
`data_pub` (E4: `data_pub` não-vazia que `normalizar_data` não lê → a fonte fica sem data e sem desconto
temporal; `valor` = bruta[:40]; `motivo` = `formato não reconhecido`, ou `sem âncora` quando a data é
relativa, p.ex. "há 3 dias", e não há data para ancorá-la; na entrada por link, `motivo` = o erro do trafilatura quando ele falha ao extrair a data),
`dns` (P3: o domínio de uma página a ler não resolveu mesmo após uma nova tentativa; antes isso aparecia como
"url insegura (SSRF)". `motivo` = `não resolveu o domínio`, com `url`),
`eval` (`eval_excluir_urls`/`eval_repeticao` recebidos com `EVAL_MODO` desligado: ignorados, nada é escondido do pipeline),
`sintetizador` (Missão F / A2: LLM indisponível ou JSON inválido após retry, `rebaixar` sem conflito
declarado, ou falha ao aplicar; em todos o nível do agregador fica e a ação vale `manter`),
`config` (E4: `E4_*` (janelas e `E4_MARCO_MAX_DIAS`) inválido no ambiente, não inteiro > 0 → usa o
default; `motivo` = o aviso, p.ex. `E4_JANELA_HOJE='0' inválido (esperado inteiro > 0); usando 2`; emitido no
início de cada execução).

**Regra:** todo fallback novo deve chamar `telemetria.fallback(onde, motivo)`.

### Observabilidade do LLM e do juiz (Missão F, item 8)

Só descreve; não muda decisão nem nível. Código em `factcheck_mvp/observabilidade.py`.

**Campos novos do evento `llm`** (os de `replay.llm_post`, quando a resposta traz):

- `usage`: `prompt_tokens`, `completion_tokens`, `total_tokens`, `custo` (USD, `usage.cost` do OpenRouter),
  `reasoning_tokens` (`completion_tokens_details`), `cached_tokens` (`prompt_tokens_details`). Só as chaves presentes;
  o endpoint Decisions (`input_tokens`/`output_tokens`) é normalizado para os mesmos nomes. Sem `usage` na resposta,
  o campo não existe. Numa chamada de cassete (`cache=hit`) o `usage` é o da resposta gravada, não um gasto de agora.
- `finish_reason`: `choices[0].finish_reason` (`stop`, `length`…). `length` com `reasoning_tokens` próximo de
  `completion_tokens` é a causa de saída cortada no meio (ex.: `reformular` com `max_tokens=300`:
  `{"consulta": "Sérgio Sacani e`): o provedor devolveu o texto assim, o trace não truncou.

**`saida` do evento `llm`**: com `TELEMETRIA_PROMPTS=1` vai inteira (até `LIMITE_PROMPT`=60000 chars, já com
redação de segredos), para todas as finalidades, inclusive `reformular`. Sem a flag fica em `LIMITE_STR`=2000
(`…[+N chars]`), então o trace padrão não incha.

**Juiz indisponível x nada para julgar.** `juiz_disponivel=false` valia nos dois casos. Agora:

| estado | quando | etapa `juiz` |
|---|---|---|
| `nada_para_julgar` | zero páginas candidatas; o juiz nem foi chamado | `pulada`, "Nada para julgar: nenhuma fonte candidata…" |
| `indisponivel` | havia candidatas e nenhuma foi classificada (LLM falhou, sem chave, desligado ou teto) | `falha`, "Juiz indisponível: N/M julgamento(s) sem resposta do LLM (motivo)." |
| `disponivel` | o juiz classificou ao menos um item | `ok`/`parcial`, texto de sempre |

- Evento `juiz_estado` (1 por execução, junto da etapa `juiz`): `estado`, `n_candidatas`, `n_julgadas`,
  `n_sem_julgamento`, `motivo` (primeiro erro do juiz; só em `indisponivel`).
- `travas.juiz_estado` no evento `decisao` (mesmos três valores; sem itens = `nada_para_julgar`). O booleano
  `travas.juiz_disponivel` continua como era (snapshots do eval o leem).

**Langfuse** (`langfuse_export`): a generation ganha `usageDetails` {input, output, total} e, com custo conhecido
e chamada ao vivo, `costDetails` {total}; `reasoning_tokens`, `cached_tokens` e `finish_reason` vão em `metadata`
(o raciocínio já está em `output`, então não entra em `usageDetails` para não contar duas vezes). Chamada de
cassete leva os tokens, mas não o custo.

### Parâmetros do E4

Lidos de `factcheck_mvp/config.py`: inteiro > 0 pelo ambiente; valor inválido vira o padrão e emite `fallback onde=config` no início da execução. A janela é quantos dias a fonte pode estar defasada em relação à referência do marcador sem ser descontada.

| variável | padrão | marcador do texto |
|---|---|---|
| `E4_JANELA_HOJE` | 2 | hoje, agora, acaba(ram) de, há pouco, há instantes, esta/nesta manhã/tarde/noite/madrugada. Também é a **folga** do marco (data explícita do fato) |
| `E4_JANELA_ONTEM` | 3 | ontem |
| `E4_JANELA_ANTEONTEM` | 4 | anteontem |
| `E4_JANELA_SEMANA` | 8 | esta/nesta semana, este/nesse fim de semana, dia da semana com determinante ("nesta segunda"), "na última <dia>" |
| `E4_JANELA_SEMANA_PASSADA` | 15 | semana passada, na última semana |
| `E4_JANELA_MES` | 32 | neste/nesse/este/esse mês; "agora em <mês>" também conta como mês |
| `E4_MARCO_MAX_DIAS` | 60 | limite do ano inferido de data sem ano ("dia 8", "08/10"): se o evento está a mais de 60 dias da referência, a data não ancora o marco. Ano explícito não tem esse limite |

Não são marcador: "hoje em dia", "até agora", "nos dias de hoje" e "há pouco mais de dez anos" (quantidade). Os valores usados vão em `decisao.parametros.e4.janelas`.

### Leitura

- `telemetria.ler_run(id|"last") -> {run_id, dir, eventos, resultado, metricas}` (KeyError se não existe)
- `telemetria.resumir_run(id)` → texto compacto: etapas (status, detalhe, Δt desde a etapa anterior),
  chamadas LLM, HTTP com erro, fallbacks agrupados, fontes mantidas/descartadas por motivo,
  sinais com direção/peso, decisão (nível final × agregador × travas).
- `telemetria.listar_runs(n)`, `telemetria.eventos_de(id, ["llm","fallback"])`.
- Desconto temporal (E4): `cli trace <id>` mostra `L_sem_desconto`/`nivel_sem_desconto`/`descontos`
  no bloco DECISAO (via `travas`); `cli trace <id> --eventos fonte` lista os `estagio=data`.

### 1.x Langfuse (espelho externo)

O trace local é a fonte de verdade; `factcheck_mvp/langfuse_export.py` o espelha no Langfuse
(run → trace, etapa → span, llm → generation com prompt/saída, fonte/fallback/decisão → event).
IDs determinísticos: reexportar o mesmo run sobrescreve. Não passa por `replay.*` (nunca vai a cassete).
Credenciais no `.env`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`.

```bash
python3 -m factcheck_mvp.langfuse_export enviar --simulacao reports/<pasta>/dados.json --tag x
python3 -m factcheck_mvp.langfuse_export enviar <run_id…> --tag x
python3 -m factcheck_mvp.langfuse_export puxar --tag x --saida lf.json   # traces + observações
```

`LANGFUSE_AUTO=1` exporta cada run ao terminar (falha só avisa no stderr). Para ver prompts completos,
rode com `TELEMETRIA_PROMPTS=1`. O trace não guarda tokens, então o Langfuse não calcula custo.

## 2. Replay (I/O determinístico)

Modo: env `IO_MODO=live|record|replay` (padrão `live`), `replay.definir_modo(m)` (processo) ou
`with replay.modo(m):` (contexto async-safe). Precedência: contexto > definir_modo > env.

| modo | comportamento |
|---|---|
| `live` | rede de verdade, nada gravado |
| `record` | se já há cassete, **reusa** (não chama); senão chama e grava |
| `replay` | só cassete; faltou → `ReplayMiss` (subclasse de `replay.HTTPError`/curl_cffi) + evento `http` com `cache=miss` |

Wrappers (todos emitem `http`; `llm_post` também `llm`):

```python
replay.http_get(url, params=None, **kw) -> Resposta            # ~curl_cffi get
replay.http_post(url, json=None, **kw) -> Resposta             # ~curl_cffi post
await replay.ahttp_get(url, max_bytes=None, headers=None, timeout=10.0, follow_redirects=False)
replay.llm_post(url, payload, headers=None, timeout=60, motor="", finalidade=None, extrair=None)
```

`Resposta`: `status_code`, `headers` (case-insensitive), `content` (bytes), `text`, `url` (final),
`json()`, `raise_for_status()` (levanta `ErroStatusHTTP`, subclasse de `replay.HTTPError`), `cache`,
`truncado`. `ahttp_get` **não segue redirect**: devolve o 3xx com `headers["location"]` para o chamador
revalidar o próximo hop (semântica anti-SSRF do aprofundar/descoberta), e lê o corpo em streaming até
`max_bytes`. Em live, os wrappers chamam `curl_cffi.requests.get/post/AsyncSession` pelo atributo
do módulo (com `impersonate="chrome"` por padrão), então
`monkeypatch.setattr(curl_cffi.requests, "get", ...)` nos testes continua valendo.

Cassetes: `eval/cassettes/<sha256>.json` (`CASSETES_DIR`). Chave = sha256 de
`{método, URL canônica (sem api_key/key/token…, query ordenada, sem fragmento), corpo JSON}`. Para LLM
o corpo é o payload inteiro (modelo, mensagens, temperatura, max_tokens): mudou o prompt ⇒ miss. Para
SerpAPI a chave é só a query (q, engine, hl, gl, num…) — estável entre versões de código. Cassete
gravado com `max_bytes` menor que o pedido e truncado ⇒ miss (regrave). Requisições que falharam
por rede no record (timeout, SSL) não viram cassete: em replay aparecem como miss, com o mesmo efeito. Para regravar, apague o arquivo.
Autoconsistência (Missão F / A3): cada repetição do juiz roda com `replay.com_sal("autocN")` (chave de cassete própria: `record` grava, `replay` relê a mesma; repetição não gravada = `ReplayMiss`) e `with replay.temperatura(t):` (payload com `temperature=t` > 0, só chat com `messages`). Fora do bloco as chaves antigas continuam valendo.

Headers de requisição nunca são gravados. Em replay, `aprofundar._ip_privado` não resolve DNS (o
cassete só existe se a URL passou no SSRF ao gravar).

Pontos migrados: `afirmacoes` (2), `padroes_llm` (2), `llm_openrouter` (chat + decisions),
`serpapi_layer` (1), `aprofundar._baixar`, `descoberta_site._fetch_aberto`. **Código novo que faz I/O
deve usar esses wrappers** (fora deles não há replay nem telemetria).

### Sal do cassete (repetições e autoconsistência)

`replay.com_sal("rep1")` (contexto async-safe, `asyncio.to_thread` herda) acrescenta um sal à chave do cassete das
chamadas de `replay.FINALIDADES_COM_SAL` (`juiz`, `sintetizador`). Com sal, a mesma chamada é uma **amostra nova** do
modelo (live ou record grava um cassete próprio) e volta do cassete nas rodadas seguintes com o mesmo sal; sem sal, a
chave é a de sempre. O pipeline usa para as repetições da avaliação (`eval_repeticao >= 1`, ver `eval_modo.sal_do_juiz`)
e para os votos extras da autoconsistência (A3). O evento `llm` registra o `sal`.

### SerpAPI: contagem e cota

- Toda busca live é contada em `eval/serpapi_uso.json` (`total_live`, `por_dia`, `por_run`,
  `chamadas[]`); cassete não conta. `replay.uso_serpapi()` devolve o resumo.
- Teto opcional: `SERPAPI_ORCAMENTO_RESTANTE=N` (total no arquivo) e `replay.definir_teto_serpapi(n)`
  (só o processo; `eval.run --max-buscas-serpapi N`). Estourou → `OrcamentoSerpAPIEsgotado` +
  `fallback(onde="serpapi", motivo="orcamento")`. Sem a env, não há teto.
- HTTP 401/403/429 da SerpAPI (conta sem buscas / chave inválida) → `CotaSerpAPIEsgotada` +
  `fallback` pedindo nova `SERPAPI_KEY` no `.env`; a resposta não vira cassete.

## 3. Eval

Ver `eval/README.md` (formato dos casos, métricas, flags). Cada caso vira um run de telemetria; o
`relatorio.md` lista o comando `cli trace <run_id>` de cada caso errado.
