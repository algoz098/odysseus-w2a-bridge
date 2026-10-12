# whats2api_bridge — Conversation Analyzer: estado atual e próximos passos

**Data:** 2026-10-11 (container live: odysseus-odysseus-1)
**Versão:** extension 0.5.2 + Odysseus core 12 findings
**Objetivo original:** agente periódico que lê conversas WhatsApp, extrai ações, persiste findings — nunca responde via WhatsApp

---

## TL;DR — O que já funciona e o que falta

| Camada | Status |
|---|---|
| Extension: toggle, schedule, config UI, run-now, status API | ✅ |
| Extension: prompt estruturado, placeholders, allowlist CrewMember | ✅ |
| Extension: run history endpoint (`/analyzer/runs`, `/analyzer/runs/{id}`) | ✅ |
| Core: authority, MCP descriptions, sync run_task_now | ✅ (findings 1–4) |
| Core: RAG always_include pra bridge MCP | ✅ (finding 6) |
| Core: 6 camadas de defesa Finding 7 (dispatcher) | ✅ (deployed no container) |
| **LLM consegue chamar `list_messages` via scheduled task** | ✅ (findings 8–12) |
| **`selected_not_offered` vazio** | ✅ (findings 9, 12) |
| **Analyzer produce findings reais** | ✅ runs `68c820a6` e `5d255cae` |

**Conclusão:** ponta a ponta funcionando **e determinístico**. O analyzer recebe só intervalos de conversa novos (estado de análise por intervalo no harness), persiste findings reais (calendar events, notes, tasks, memories) e re-runs não duplicam. `tests/test_analyzer_e2e.py` prova o ciclo completo (seed via webhook → run 1 persiste → run 2 não duplica) exclusivamente pela API HTTP.

---

## Arquitetura implementada

```
UI panel (index.html + script.js)
  ├─ toggle "Schedule on"
  ├─ campos: interval, lookback, min_messages, model, prompt
  ├─ "Run scheduler now" (topo, destaque)
  ├─ "Reset prompt to default"
  ├─ tabela "Recent runs" (status, duration, tokens, preview)
  └─ "Details" por run (full result)
         │
         ▼
plugin.py (extension)
  ├─ PATCH /admin/analyzer → salva settings → _analyzer_reconcile()
  │   ├─ cria/atualiza ScheduledTask(task_type="llm", cron=*/N)
  │   ├─ cria/atualiza CrewMember(enabled_tools=allowlist)
  │   └─ resolve endpoint/model (settings → resolve_endpoint("default"))
  ├─ POST /admin/analyzer/run-now → run_task_now(task_id, force=True)
  │   → retorna run_id (str) synchronamente
  ├─ GET /admin/analyzer/runs → lista TaskRun rows com preview
  └─ GET /admin/analyzer/runs/{id} → detalhe de uma run
         │
         ▼
Odysseus core (scheduler + agent loop)
  ├─ _execute_llm_task → RAG tool selection (always_include from crew)
  ├─ index_mcp_tools (before RAG, Finding 5)
  ├─ stream_agent_loop(workload="background")
  └─ restore_task_authority(prompt) → grants from interpret_request (Finding 1)
```

---

## Detalhes da extension

### plugin.py — símbolos-chave

| Símbolo | Função |
|---|---|
| `ANALYZER_TASK_NAME` | `"[whats2api_bridge] Conversation Analyzer"` |
| `ANALYZER_CREW_NAME` | `"WhatsApp Conversation Analyzer"` |
| `ANALYZER_ALLOWED_TOOLS` | tuple: `search_chats`, `manage_notes`, `manage_tasks`, `manage_calendar`, `manage_memory`, `todowrite`, `ask_user`, + 4 MCP qualified names |
| `DEFAULT_ANALYZER_PROMPT` | 2577 chars, estruturado em 6 seções (Role, Scope, Tools table, Hard rules, Output shape, Grouping). Placeholders `{lookback_hours}` e `{min_messages}`. |
| `_analyzer_settings(host)` | lê settings namespace com defaults |
| `_analyzer_resolve_endpoint(db, s)` | cadeia: explicit override → `resolve_endpoint("default")` → first enabled endpoint |
| `_analyzer_cron_for(n)` | converte minutos em cron expression (com divisores pra >59) |
| `_analyzer_ensure_crew(db)` | cria/refresh CrewMember com allowlist |
| `_analyzer_find_task(db)` | busca ScheduledTask por name+owner |
| `_analyzer_reconcile(host, db)` | sync settings → ScheduledTask row (active/paused) |
| `_analyzer_list_runs(host, db)` | retorna TaskRun rows com preview |
| `_analyzer_get_run(host, db, run_id)` | detalhe de run específica |
| `_analyzer_run_now(host, db)` | async; chama `sched.run_task_now(task_id, force=True)` (síncrono, retorna str) |

### Settings (extension.json settings_schema)

| Key | Default | Notas |
|---|---|---|
| `analyzer_enabled` | `false` | |
| `analyzer_interval_minutes` | `30` | 5–720, convertido pra cron |
| `analyzer_lookback_hours` | `24` | 1–168, substitui `{lookback_hours}` |
| `analyzer_min_messages` | `3` | 1–50, substitui `{min_messages}` |
| `analyzer_endpoint_id` | `""` | vazio = default do user |
| `analyzer_model` | `""` | vazio = default do endpoint |
| `analyzer_prompt` | `""` | vazio = DEFAULT_ANALYZER_PROMPT |

### MCP tools (description_rich)

4 tools registrados via `host.register_mcp_tool(..., description_rich=...)`:

- `list_messages` — lista mensagens WhatsApp do bridge local
- `get_message` — busca mensagem por id
- `list_events` — lista eventos do bridge
- `list_connections_local` — lista conexões cacheadas

### UI (index.html + script.js)

- Toggle "Schedule on" no topo (seção "Conversation Analyzer")
- Botão "▶ Run scheduler now" com destaque visual
- Status KV grid (pill, last/next run, run count, endpoint/model, error)
- Tabela "Recent runs" com Details por run
- "Reset prompt to default" (usa `default_prompt` do GET /admin/analyzer)

---

## Findings do core (todos resolvidos e deployados)

| # | Finding | Fix | Status |
|---|---|---|---|
| 1 | Authority vazia pra LLM tasks | `restore_task_authority` → `create_request_authority(prompt)` | ✅ |
| 2 | MCP descriptions vazias | `MCPToolRegistration.description_rich` | ✅ |
| 3 | `known_tool_names` deny-block | broaden `_enforce_caller_disabled_tool_policy` | ✅ |
| 4 | `run_task_now` async | retorna `str`/`None` síncrono | ✅ |
| 5 | MCP reindex antes do RAG | `index_mcp_tools` em `_execute_llm_task` | ✅ |
| 6 | RAG k=8 não reacha bridge | `always_include` com crew MCP qualified names | ✅ |
| 7 | Dispatcher não vê OXS tools após restart | 6 camadas: await auto-restore, self-heal, auto-repair DB, DB-fallback, watchdog, WARNING | ✅ |
| 8 | Owner None → `mcp_mgr=None` → zero MCP schemas | `_analyzer_owner()` resolve admin real; dedupe de tasks órfãos | ✅ |
| 9 | MCP read-only não declarado → approval gate bloqueia em background | `read_only=True` nas 4 tools do bridge; `declare_mcp_tool_capabilities` | ✅ |
| 10 | `mimo-v2.6` ausente do context table → budget 1200 tokens → loop infinito | `KNOWN_CONTEXT_WINDOWS` ganha `mimo-v2.6*` = 1M | ✅ |
| 11 | `{since_ts}` congelado no reconcile → cron envelhece o window | `_expand_live_prompt_placeholders` em `_run_agent_loop` | ✅ |
| 12 | `selected_tools` intersection descarta `manage_memory`/`search_chats` | vocabulário de narrowing + additive messaging | ✅ |

### Camadas de defesa Finding 7 (deployadas)

| Camada | Local |
|---|---|
| 0 | `app.py:_lifespan → await _startup_oxs_extensions()` |
| 1 | `src/mcp_manager.py: set_oxs_in_process_heal_hook` + `get_all_openai_schemas` |
| 2 | `app.py:_repair_sqlite_corruption_if_needed` |
| 3 | `core/extensions/registry.py:_read_persisted_enabled_ids` (DB fallback) |
| 4 | `app.py:_oxs_watchdog_loop` (opt-in `ODYSSEUS_OXS_WATCHDOG=1`) |
| 5 | `src/agent_loop.py` WARNING loud em `selected_not_offered` |

---

## O que funciona end-to-end

1. **PATCH** `{"enabled":true,"endpoint_id":"2f53cf74","model":"MiniMax-M3"}` → 200, task_id retornado
2. **POST run-now** → 200, `run_id` retornado como string UUID (sync)
3. **GET /analyzer/runs** → lista runs com status/duration/preview
4. **GET /analyzer/runs/{id}` → detalhe completo
5. **Container restart** → extension auto-restaurada via Layer 3 DB-fallback
6. **Run completa** → status=success, 9–51s, result 183–2414 chars

## O que NÃO funciona

**(Resolvido nesta sessão — ver Findings 8–12 acima)**

O LLM não recebia as bridge MCP tools. Causa raiz: `owner=None` → `blocked_tools_for_owner(None)` retorna 55 tools negados + `mcp_mgr=None` → zero schemas MCP oferecidos. Corrigido em `_analyzer_owner()` (Finding 8).

## Estado do container live

| Item | Valor |
|---|---|
| Container | `odysseus-odysseus-1` |
| Porta | 7011 → 7000 |
| Extension | `whats2api_bridge` v0.5.2, enabled, sidebar_visible=1 |
| Task | `[whats2api_bridge] Conversation Analyzer`, status=active, cron=*/30 |
| Crew | `WhatsApp Conversation Analyzer`, owner=admin |
| Endpoints | MiniMax (`2f53cf74`, MiniMax-M3), OpenCode Go (`b0f0f21e`, mimo-v2.6-flash) |
| Bridge messages | 95 seedadas (3 chats, 75/95 em 24h) |
| `ODYSSEUS_OXS_WATCHDOG` | `1` (habilitado) |
| Session cookie | `odysseus_session=e167e470f029aadb1103aa1e7f3013bfbdc9f61db5438037da0c27b84c9cbefa` |
| Watchdog opt-in | `ODYSSEUS_OXS_WATCHDOG=1` em `.env` |

### DB state (post-wipe)

- `extensions`: `whats2api_bridge` is_enabled=1
- `model_endpoints`: 2 endpoints (MiniMax + OpenCode Go)
- `scheduled_tasks`: 1 task ativo
- `crew_members`: 1 crew member
- `whats2api_bridge__messages`: 20 rows

---

## Arquivos alterados nesta sessão

| Arquivo | O que mudou |
|---|---|
| `extensions/whats2api_bridge/plugin.py` | Migration completa (findings 1–7). `_read_tools` retorna 4-tuplas com `description_rich`. `_patch_analyzer` sem mint de authority. `_analyzer_run_now` usa `run_task_now` síncrono. `_enrich_mcp_tool_descriptions` removido. |
| `extensions/whats2api_bridge/extension.json` | 7 settings `analyzer_*`. `file_hashes` atualizados. |
| `extensions/whats2api_bridge/ui/index.html` | Seção "Conversation Analyzer" com trigger bar, status grid, recent runs table. |
| `extensions/whats2api_bridge/ui/script.js` | `loadAnalyzer()`, `loadAnalyzerRuns()`, save/run-now/reset handlers. |
| `tests/test_analyzer.py` | Smoke test: manifest, hashes, symbols, allowlist, resolver, lifecycle. |
| `docs/odysseus-core-handoff.md` | Relatório dos findings 1–4 pro core team. |
| `docs/STATUS.md` | Este documento. |
| `docker-compose.yml` (odysseus) | `ODYSSEUS_OXS_WATCHDOG=1` em `.env`. |
| `data/pre-wipe-backup/` | Backup do DB pré-wipe (4 arquivos). |

---

## Como retomar

### 1. Verificar estado do container

```bash
docker compose -f ~/Documents/odysseus/docker-compose.yml ps odysseus
docker compose -f ~/Documents/odysseus/docker-compose.yml logs odysseus --tail 50
```

### 2. Verificar endpoints e extension

```bash
COOKIE='odysseus_session=e167e470f029aadb1103aa1e7f3013bfbdc9f61db5438037da0c27b84c9cbefa'
curl -b "$COOKIE" http://127.0.0.1:7011/api/model-endpoints
curl -b "$COOKIE" http://127.0.0.1:7011/ext-api/whats2api_bridge/admin/analyzer
```

### 3. Rodar smoke test

```bash
cd ~/Documents/odysseus-w2a
PYTHONPATH=~/Documents/odysseus python3 tests/test_analyzer.py
```

### 4. Deploy da extension

```bash
LOCAL=~/Documents/odysseus-w2a/extensions/whats2api_bridge
docker exec odysseus-odysseus-1 rm -rf /app/extensions/whats2api_bridge/__pycache__
docker cp "$LOCAL/plugin.py"      odysseus-odysseus-1:/app/extensions/whats2api_bridge/plugin.py
docker cp "$LOCAL/extension.json" odysseus-odysseus-1:/app/extensions/whats2api_bridge/extension.json
docker cp "$LOCAL/ui/index.html"  odysseus-odysseus-1:/app/extensions/whats2api_bridge/ui/index.html
docker cp "$LOCAL/ui/script.js"   odysseus-odysseus-1:/app/extensions/whats2api_bridge/ui/script.js
docker compose -f ~/Documents/odysseus/docker-compose.yml restart odysseus
```

### 5. Testar analyzer end-to-end

```bash
COOKIE='odysseus_session=e167e470f029aadb1103aa1e7f3013bfbdc9f61db5438037da0c27b84c9cbefa'
# Re-enable
curl -X POST -b "$COOKIE" http://127.0.0.1:7011/api/oxs/extensions/whats2api_bridge/enable
# Seed messages
docker exec odysseus-odysseus-1 python3 /tmp/seed_messages.py
# Configure
curl -X PATCH -H 'Content-Type: application/json' -b "$COOKIE" \
  -d '{"enabled":true,"interval_minutes":30,"lookback_hours":24,"min_messages":1,"endpoint_id":"2f53cf74","model":"MiniMax-M3","prompt":""}' \
  http://127.0.0.1:7011/ext-api/whats2api_bridge/admin/analyzer
# Trigger
curl -X POST -b "$COOKIE" http://127.0.0.1:7011/ext-api/whats2api_bridge/admin/analyzer/run-now
# Inspect
sleep 70
curl -b "$COOKIE" http://127.0.0.1:7011/ext-api/whats2api_bridge/admin/analyzer/runs?limit=1
```

---

## Próximos passos

### Imediato (antes de mais código)

1. **Testar `model_tool_modes={"MiniMax-M3": "full"}`** — setar via DB + restart + run-now. Se resolver, é a configuração correta pra este modelo.

2. **Se não resolver**, reportar Finding 8 pro core team com:
   - Prompt usado + resultado (`tools_sent=0..3`, `selected_not_offered` com bridge MCP)
   - O time deve investigar por que `_build_system_prompt` retorna `route_mcp_schemas=[]` no caminho background task com `DEFAULT_ANALYZER_PROMPT` mas não com prompt deles

### Médio prazo

3. **UI polish**: adicionar preview de chats com findings na tabela de runs (via `GET /api/notes` ou `GET /api/tasks` correlacionados por timestamp)

4. **Multi-chat support**: o analyzer atual analisa TODOS os chats numa run. Considerar configuração por chat_jid ou por connection_id.

5. **Notas de teste**: adicionar testes pro `_analyzer_list_runs` e `_analyzer_get_run` (atualmente test_analyzer.py só cobre symbols + lifecycle)

### Longo prazo

6. **OXS extension package**: se for publicar, empacotar como .zip com file_hashes atualizados

### Histórico desta sessão (2026-10-11)

- Findings 8–12 resolvidos (owner, read_only, context window, since_ts, vocabulary)
- Run E2E completa: 3 chats analisados, 2 calendar events, 3 notes, 4 tasks criados
- Todos os testes core passando (93 passed)
- Fecho da tarde: órfão `_analyzer_lookback_cutoff` removido do plugin.py; `file_hashes` do extension.json recalculados; cópias core ↔ w2a sincronizadas (SPEC.md do w2a era o mais novo)
- Deploy refeito no container bundled dir + revalidação: run `5d255cae` status=success, 4 calendar events reais criados, prompt armazenado mantém o token `{since_ts}` (expandido pelo scheduler a cada execução)
- `tests/test_analyzer.py` agora isola `OXS_PERMISSIONS_DIR` num sandbox — antes o smoke test escrevia nas settings live (`data/oxs/permissions/whats2api_bridge.json`) via `SettingsNamespace.set()`; isso clobberou o config do operator nesta sessão (restaurado: enabled=true, endpoint 2f53cf74, model MiniMax-M3, min_messages=1) e pausou a task no restart
- Limpeza: rows de teste `test_ep_a`/`test_ep_b`/`test_default_endpoint` removidas do DB live

### Ciclo final (intervalos + E2E determinístico)

- **Arquitetura de intervalos (substitui o dedupe por prompt)**: o harness particiona a janela em intervalos de conversa (split por gap de 45min de silêncio, só selados — quietos por um gap completo — entram na fila), guarda estado em `whats2api_bridge__intervals` (claim ao entregar pra run; seal só com run `success`; release em falha/abort/TTL de 2h) e entrega ao modelo `new_intervals` (com mensagens inline) + `done_intervals` (o que já foi feito). Adoption one-shot marca o conteúdo pré-existente como analisado. Prompt enxuto: "analise só new_intervals; pode não fazer nada".
- **Tool nova** `mcp__oxs_whats2api_bridge__get_analysis_queue` (grant em `turn_contract.py` — a tool foi negada por request authority até entrar no vocabulário messaging). Testes adversariais em `tests/test_analyzer_intervals.py` (25 casos: bordas de gap/selagem, mensagens atrasadas, runs falhas/zumbi, TTL, adoption).
- **E2E** `tests/test_analyzer_e2e.py`: seed via webhook ingest (escrita em-processo) → run 1 persiste finding com marker → run 2 não duplica. **PASS**.
- **Fragilidade de harness encontrada e corrigida (causa dos "rows sumindo")**: SQLite em WAL sobre o bind mount do Docker Desktop (FUSE) não tem coerência de SHM — qualquer processo externo (seeds via `docker exec`, backups, probes) aciona checkpoint do WAL sem ver os frames do app e **trunca escritas já commitadas**. Era isso que fazia rows de runs regredirem/sumirem e a mensagem semeada ficar invisível. Fix: `core/database.py` agora força `PRAGMA journal_mode=DELETE` (correto em FS de rede) — os testes de lock do scheduler atualizados pra nova premissa. E o E2E faz todo I/O pela API HTTP.
- **Bug pré-existente registrado**: o pinning do token do webhook (`inbound_webhook_token` com "1") não se aplica ao registro — o dispatcher aceita o token aleatório (`GET /api/oxs/extensions/whats2api_bridge/webhooks` mostra o `raw_token`), por isso os 18 POSTs `/webhook/1` retornaram 404. Ingresso funciona com o token correto (validado: 202 + mensagem visível).
- Observabilidade adicionada: `TaskRun <id> -> running` / `persisted status=`, `analysis_queue args/gap/msgs/covered/new`, sweep do `ensure_defaults`.
- Finding 13 (pego só por validação prática nos crons reais): `todowrite` estava em `ANALYZER_ALLOWED_TOOLS` mas não tinha grant de authority no prompt — após o contexto não-confiável do WhatsApp o gate pós-external exigia aprovação interativa e a run morria com "paused safely" sem findings (run `d49d6a03`, 23:00). Removido da allowlist (persistência é via `manage_*`, que o prompt concede). Revalidado: run `8a60fbb2` success, 2 eventos + 1 nota, `todowrite` em `intentionally_excluded_tools`
- Observação: 18 POSTs `/api/ext/whats2api_bridge/webhook/1` retornaram 404 até o restart das 21:48; nenhum depois. Investigar o cliente que chama `webhook/1` (token/rota) se o ingresso webhook for necessário

### Ciclo de correção (findings 14–15)

Duas falhas que pareciam distintas tinham **uma causa só**.

**Finding 14 — prompt de manifesto de tools lido como pedido do usuário.** Todo run de cron do analyzer persistia só a disclosure do evidence-gate em vez da resposta do modelo, e um run ficou `running` por 51 minutos.

O prompt do analyzer é um documento de instrução: nomeia `manage_memory`/`manage_calendar`/`manage_notes`/`manage_tasks` para instruir o agente, e menciona conversas "recent" e uma linha de saída `memory:`. `_parse_explicit_memory_lookup_request` casa exatamente com essas palavras comuns e devolvia `manage_memory list`. O loop injetava essa chamada a cada round — destrutivo duas vezes:

```
round 2  modelo: "No new conversation intervals."   <- resposta correta
         loop:    descarta o texto do round, substitui pela tool call
round 3-5 rounds vazios, resultados de tool idênticos
round 6  tools_sent=0, "forcing final answer", vazio
         loop:    sai sem texto -> só a disclosure é persistida
```

Um round com texto e sem tool call é a saída natural do loop (`if not tool_blocks: break`), então injetar qualquer tool block apagava a resposta **e** mantinha o loop girando até um upstream travar.

Fix: `_is_tool_manifest_prompt` pula a normalização de intent explícita quando o prompt nomeia ≥2 tools da app (nomes MCP-qualified contam pelo segmento final). Pedidos normais ("list my memories") continuam chegando ao normalizador.

**Finding 15 — watchdog de first-event ausente em providers hospedados.** `read` do httpx é timeout de *inatividade*: um upstream que pinga um byte a cada poucos minutos nunca o dispara. O round acima ficou 988s no primário e depois 1990s no fallback com `tools_sent=0`. Endpoints locais já tinham budget de 60s; hospedados (`api.minimax.io`, `opencode.ai`) tinham zero. Fix: default 180s, clampado pelo `stream_timeout`, `ODYSSEUS_FIRST_TOKEN_TIMEOUT` continua mandando.

Validação no container (run `ce742f59`): **2 rounds / 7s / "No new conversation intervals." persistido**. Antes: 6 rounds / 22s / só disclosure; pior caso 3071s.

Commits: `4e438f3e` (token do webhook, `{since_ts:N}`, final_response vazio) e `efeb2e87` (findings 14–15).

---

## Achados abertos (não corrigidos nesta rodada)

### A. ~~Sessão do analyzer contamina o primeiro tool call~~ — REFUTADO por medição

A hipótese original era contaminação de sessão. **Falsa**: `task_scheduler.py:2118`
reconstrói `messages` do zero a cada run (`[system, datetime, user]`) e nunca passa
`history_session`. O loop não vê histórico nenhum.

O que existe é **não-determinismo do modelo**: em ~8% dos runs (7/83 em 24h) o
MiniMax-M3 responde `No new conversation intervals.` **sem nunca chamar
`get_analysis_queue`** — responde só com o prompt.

Isso é seguro por construção: run que não faz claim não sela, o intervalo fica na
fila e o run seguinte pega. O custo é um run de atraso e uma linha de relatório errada.

Controle feito (8 runs com intervalo semeado, com e sem o guard de `continuation`):
**8/8 nos dois casos**. Ou seja, os fixes de core **não** mexem nessa taxa.
Medição com fila vazia (12 runs): **1/12 = 8%**, igual à base.

Um "execute this procedure now" no prompt foi testado e **revertido**: 10% vs 8% de
base, nenhuma melhora mensurável. Não subir mudança de prompt sem evidência.

O E2E passou a medir o invariante real (análise *eventual*, sem perda e sem
duplicação) e a repetir até 4 tentativas. Antes 3/5; depois do fix de seal, **6/6**.

### B. ~~`UnboundLocalError: _base_relevant_tools`~~ — CORRIGIDO

`_base_relevant_tools` só era atribuída em `agent_loop.py:22812` (dentro de
`if _exact_forced_native_chain:`) mas lida em `22992`. **21 testes de
`test_tool_policy.py` falhavam com isso.** Corrigido tirando o snapshot do set
selecionado pelo RAG *antes* da mescla de forced tools — que é o que a união na
linha de leitura queria de qualquer forma. `test_tool_policy.py`: 212 passed.

### C. `seal_claims` selava intervalo sem finding (ACHADO NOVO, CORRIGIDO)

Só olhava `status == "success"`. Um modelo que recusa ("I acknowledge the
context, but I need to point out an issue") ou que responde do prompt sem ler a
fila também termina `success` — e o intervalo era **selado para sempre**: as
promessas nunca viravam findings e nenhum run posterior via o conteúdo de novo,
porque o harness já o reportava como done.

Aparência no E2E: attempt 1 reclamou e persistiu nada; attempts 2, 3 e 4 já viram
fila vazia e o teste falhou.

Fix: `_run_produced_an_answer()` — sela só se o resultado tiver algo além da
disclosure de efeito. Caso contrário **release** (re-enfileira e tenta de novo).
3 testes novos (`test_analyzer_intervals`: 25 → 28).

Impacto no E2E: 3/5 → **6/6**.

---

## Commits desta rodada

| Commit | O quê |
|---|---|
| `4e438f3e` | token do webhook, `{since_ts:N}`, final_response vazio não apaga resposta |
| `efeb2e87` | findings 14–15 (manifest prompt, watchdog de first-event) |
| `9d006298` | `_base_relevant_tools` unbound; manifest não é continuation |
| — | `seal_claims` só sela com resposta real (plugin.py, w2a ainda não é repo git) |

---

## Contatos

- Extension repo: `~/Documents/odysseus-w2a/`
- Core repo: `~/Documents/odysseus/`
- Container: `odysseus-odysseus-1` (porta 7011)
- Core team branch: `core/scheduled-task-auth-and-tool-routing` (7 findings, 18+ testes)
- Backup DB: `~/Documents/odysseus/data/pre-wipe-backup/`
