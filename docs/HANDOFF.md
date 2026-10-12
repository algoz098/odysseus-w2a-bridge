# Handoff — `whats2api_bridge` / Conversation Analyzer

**Data:** 2026-10-12
**Para:** próxima equipe nesta extension + no core
**Estado:** ciclo de análise validado ponta a ponta · ciclo de transporte **não** validado
**Última bateria:** smoke ✅ · 28 intervalos ✅ · 5 soak ✅ · E2E ✅ · 20/20 runs live ✅

---

## 1. Objetivo

### Objetivo final

Ler conversas de WhatsApp e extrair ações para artefatos nativos do Odysseus
(task / calendar / note / memory), **em modo somente-leitura sobre o WhatsApp**,
**exatamente uma vez**, sem intervenção humana, com harness determinístico e
coberto por testes ponta a ponta.

### Objetivo atual

O ciclo de **análise** está validado. O ciclo de **transporte** não está. Três
correções feitas no core do Odysseus ainda vivem em `dev` e precisam subir.
O maior risco residual não é código — é o **deploy manual**.

### Não-objetivos (importante para não perder tempo)

- A extensão **nunca** responde WhatsApp. Não é bug, é o produto.
- Não é multi-tenant. Escopo é do operador.
- O provedor é `api.whats2api.com`; a extensão não implementa protocolo
  WhatsApp, ela fala HTTP com um serviço.

---

## 2. O que a extensão é (conceito em 30 segundos)

Dois papéis que não devem ser confundidos:

- **A ponte** é *transporte*: conectividade WhatsApp → Odysseus. Entra mensagem,
  sai evento. Não interpreta nada.
- **O Analyzer** é *produto*: conversa → artefato nativo. Não transporta nada.

O ponto onde se tocam é a tabela `whats2api_bridge__messages`.

### Invariantes (o contrato real)

Estes são os que definem se a extensão funciona. Cada um tem teste:

1. **Nada se perde** — todo intervalo recebido é analisado ou fica pendente visível.
2. **Nada se duplica** — intervalo analisado uma vez não é reanalisado.
3. **Só se sela com resposta** — intervalo só vira "feito" se a execução disse
   algo sobre ele. Sem isso, uma execução que falha em silêncio descarta a
   conversa para sempre. *(foi o bug mais grave desta sessão — ver A-1)*
4. **Falha não vira sucesso** — exceção/cancelamento/travamento devolvem o
   intervalo à fila.
5. **O harness decide, o modelo extrai** — a fronteira de "o que é novo" é do
   harness. Se ela depender do modelo obedecer, não é determinístico.
6. **Idempotência** — rodar duas vezes não produz o dobro; rodar depois de
   falha não perde nada.

---

## 3. Achados no CORE do Odysseus

**Sete defeitos. Todos corrigidos, todos com teste de regressão.**

O padrão: **nenhum aparece em conversa interativa.** Todos aparecem quando o
cliente é uma extension rodando *desatendida, com prompt-manifesto, de forma
recurrente*. Se a próxima equipe achar estranho o core ceder tanto, o motivo é
esse — é a primeira classe de tráfego real desse formato.

### C-1 — Prompt de manifesto lido como pedido do usuário

- **Onde:** `src/agent_loop.py`, cadeia `_parse_explicit_*` → `_qwen_explicit_tool`
- **Por quê:** o prompt do Analyzer é um documento que *nomeia* tools
  (`manage_memory`, `manage_calendar`, …) para instruir o agente, e menciona
  conversas "recent" e uma linha de saída `memory:`.
  `_parse_explicit_memory_lookup_request` casa com essas palavras comuns e
  devolvia `manage_memory list`.
- **Consequência:** o loop injetava a chamada todo round. Round com texto e sem
  tool call é a saída natural (`if not tool_blocks: break`), então injetar
  qualquer tool block **apagava a resposta do modelo** e mantinha o loop girando
  até um upstream travar. Todo run de cron persistia só a disclosure
  ("External operation … reported success") em vez da resposta.
- **Agravante:** um run ficou `running` **51 minutos**.
- **Fix:** `_is_tool_manifest_prompt()` — pula a normalização quando o prompt
  nomeia ≥2 tools da app (MCP-qualified contam pelo segmento final).
- **Commit:** `efeb2e87` · **Teste:** `tests/test_tool_manifest_prompt.py`

### C-2 — `_base_relevant_tools` sem atribuição

- **Onde:** `src/agent_loop.py` (lê em ~22992, atribuía só em ~22812 dentro de
  `if _exact_forced_native_chain:`)
- **Por quê:** todo caminho fora do exact-native-chain levantava
  `UnboundLocalError`.
- **Consequência:** **21 testes de `test_tool_policy.py` quebrados.** Pre-existente
  no `HEAD`, não introduzido por esta extension.
- **Fix:** snapshot do set do RAG *antes* da mescla de forced tools — que é o que
  a união na linha de leitura queria (forçar web/native não pode descartar tools
  MCP que o RAG achou).
- **Commit:** `9d006298`

### C-3 — Prompt de manifesto classificado como continuação de conversa

- **Onde:** `src/agent_loop.py`, `_classify_agent_request`
- **Por quê:** casou com `_ACTION_CONTINUATION_RE` **no próprio texto de regra**:
  "Only read/write data belonging to **this** operator" parseia como
  `read` … `this`. Mandava o turno pelo caminho de continuação e trocava a
  retrieval query por contexto recente truncado.
- **Consequência:** RAG com query corrompida em toda execução.
- **Fix:** manifest prompt é autocontido, logo não continua nada.
- **Commit:** `9d006298` · **Teste:** `tests/test_tool_manifest_prompt.py`

### C-4 — Sem watchdog de first-event em provider hospedado

- **Onde:** `src/llm_core.py`, `_first_token_timeout`
- **Por quê:** `read` do httpx é timeout de **inatividade**. Um upstream que pinga
  um byte a cada poucos minutos nunca o dispara. Endpoints locais tinham budget
  de 60s; hospedados (`api.minimax.io`, `opencode.ai`) tinham **zero**.
- **Consequência:** round preso por 988s no primário e depois 1990s no fallback,
  com `tools_sent=0`. TaskRun fica `running` esse tempo todo — foi o que estourou
  o timeout do E2E.
- **Fix:** default 180s clampado pelo `stream_timeout`;
  `ODYSSEUS_FIRST_TOKEN_TIMEOUT` continua mandando.
- **Commit:** `efeb2e87`

### C-5 — `final_response` vazio apaga resposta já coletada

- **Onde:** `src/agent_runtime/completion.py`, `with_completion_gate`
- **Por quê:** atribuía `answer` em **todo** `final_response`, inclusive vazio.
- **Consequência:** run que emite a resposta real e depois uma cauda de rounds
  "thinking-only" substituía a própria resposta por `''` → só a disclosure sobrava.
- **Fix:** ignora `final_response` vazio quando já existe resposta.
- **Commit:** `4e438f3e`

### C-6 — `{since_ts}` fixo em 24h

- **Onde:** `src/task_scheduler.py`, `_expand_live_prompt_placeholders`
- **Por quê:** expandia com janela fixa de 24h no fire-time.
- **Consequência:** task configurada com outro `lookback_hours` lia uma janela que
  nunca pediu.
- **Fix:** aceita `{since_ts:N}` (1–168, default 24).
- **Commit:** `4e438f3e` · **Teste:** `tests/test_task_scheduler_placeholders.py`

### C-7 — SQLite em WAL sobre bind mount do Docker

- **Onde:** `core/database.py`
- **Por quê:** WAL sobre FUSE do Docker Desktop não tem coerência de SHM. Qualquer
  processo externo (seed via `docker exec`, backup, probe) dispara checkpoint sem
  ver os frames do app e **trunca escritas já commitadas**.
- **Consequência:** runs "regredindo", seeds sumindo — parecia bug de lógica,
  era corruption de storage.
- **Fix:** `PRAGMA journal_mode=DELETE` (correto em FS de rede).
- **Regra que sobrou:** **todo I/O de teste passa pela API HTTP. Nunca abrir o
  SQLite do container por fora.**

---

## 4. Achados na EXTENSÃO

### A-1 — `seal_claims` selava intervalo sem ter produzido nada  ← **grave**

- **Onde:** `extensions/whats2api_bridge/plugin.py`, `seal_claims`
- **Por quê:** a única condição era `status == "success"`. E "success" só significa
  que o loop não deu erro.
- **Consequência:** um modelo que recusa ("I acknowledge the context, but I need to
  point out an issue") ou que responde do prompt sem ler a fila também termina
  `success` — e o intervalo era **selado para sempre**. As promesas nunca viravam
  findings e nenhum run posterior via o conteúdo de novo, porque o harness já o
  reportava como done. **Perda silenciosa.**
- **Como apareceu:** attempt 1 reclamou e persistiu nada; attempts 2, 3, 4 já viram
  fila vazia.
- **Fix:** `_run_produced_an_answer()` — sela só se o resultado tiver algo além da
  disclosure de efeito; senão **release**.
- **Efeito no E2E:** 3/5 → 6/6.
- **Commit:** `1a193cae` · **Teste:** `tests/test_analyzer_intervals.py` (25 → 28)

### A-2 — `re.compile` derrubou a extensão inteira

- **Onde:** `extensions/whats2api_bridge/plugin.py`
- **Por quê:** OXS faz **AST audit antes do `enable()`** e recusa carregar se
  disparar. `compile` está na lista de chamadas proibidas (é vetor de exec) e o
  scanner casa chamada por atributo também — então um único `re.compile` no
  plugin (o único do arquivo inteiro) fez todo caminho de enable falhar:
  `OXSSecurityError: forbidden call 'compile'`.
- **Consequência:** extension desabilitada, **todo endpoint `ext-api` 404**, e
  **nenhum teste reclamou** — as suítes importam `plugin.py` direto, então
  chamada proibida não levanta nada. Só falha em runtime, silenciosamente.
- **Fix:** match da disclosure com string ops (o texto é fixo do servidor).
- **Commit:** `1a193cae`

### A-3 — Race no pinning do token do webhook

- **Onde:** `plugin.py`, `_pin_webhook_token`
- **Por quê:** rodava no mesmo call stack que ainda ia instalar o webhook record.
- **Consequência:** todo enable deixava um token recém-rotacionado no disco — a URL
  do operador no whats2api.com dava 404 (18 POSTs) até um restart por acaso.
- **Fix:** adia para o próximo tick do event loop + poll breve pelo record;
  loga warning se nunca aparecer.
- **Commit:** `4e438f3e`

### A-4 — `{since_ts:N}` não parametrizado

- **Onde:** `plugin.py`, `_analyzer_reconcile`
- **Fix:** o token é carimbado com `lookback_hours` no reconcile; o scheduler
  expande no fire-time. **O token fica vivo (não expandido) de propósito.**
- **Commit:** `4e438f3e`

---

## 5. O que falta — priorizado

### P0 — Transporte sem cobertura (nosso, bloqueia dizer "validado")

`test_extension_whats2api_bridge.py` está em **19/26**. As 7 falhas são
**expectativas obsoletas, não defeito de produto**:

- fixa versão `0.1.0` (hoje `0.5.2`)
- espera `call_method` como primeira tool registrada (`get_analysis_queue` ordena primeiro)
- confere conjunto exato de tools e tabelas — o Analyzer adicionou `whats2api_bridge__intervals`

**Por que é P0:** essa é a suíte que cobre `send_message`, `send_media`,
`send_reaction`, `mark_read`, `send_presence`, `is_on_whatsapp`, `call_method`,
ciclo de vida de conexões e o token do webhook. **Todo o papel de transporte está
sem rede de segurança.** Mecânico, ~15 min.

### P1 — Container pode estar servindo código velho

**Por que é o maior risco residual:** o core **não é bind-mount**. `src/` é
embutido na imagem; o deploy é `docker cp` + `restart` manual. Já aconteceu duas
vezes nesta sessão: (a) o container rodando `agent_loop.py` pré-fix depois de um
`restart` que eu achei que bastava; (b) a extensão morta por um patch que só
existia no repo.

**O que fazer:** um check automático repo→container (hash dos 5 arquivos de core
que alteramos + os 4 da extension). Hoje só existe o sync repo↔repo.

### P2 — Teste de restart

**Por que:** a extensão tem que auto-habilitar e reconciliar a task sozinha. Foi o
que quebrou duas vezes e **não há teste**. Um `docker restart` + assert de
`is_enabled` + `task.status == active` cobre.

### P3 — Soak em tempo real

**Por que:** 20 runs em 14 min não é soak. O hang de 51 min só apareceu com horas
de uptime. O soak determinístico (relógio virtual) cobre lógica; não cobre
comportamento de provider real ao longo do tempo.

### P4 — Levar os fixes de core para upstream

**Por que:** C-1 a C-7 estão em `src/` no branch `dev` e afetam **qualquer**
usuário do Odysseus, não só esta extension. Ficarem aqui é conhecimento
perdido. Já documentados em `docs/odysseus-core-handoff.md` para o time de core.

---

## 6. Comportamento conhecido e aceito

**~8% dos runs o modelo pula `get_analysis_queue`.** Responde
"No new conversation intervals." sem perguntar à fila.

- **Medido:** 7/83 runs em 24h. **Não muda** com C-1, C-2, C-3, C-4. Um frame
  "execute this procedure now" no prompt foi testado e **revertido** (10% vs 8%,
  sem ganho) — não subir mudança de prompt sem evidência.
- **Impacto:** seguro por construção — sem claim não há seal, o intervalo fica na
  fila e o run seguinte pega. Custo: um run de atraso + uma linha de relatório errada.
- **Correção real (não feita):** entregar a fila no prompt em vez de depender da
  tool. Exige um hook de fire-time que a API de extensões **não tem hoje**
  (`register_task_action` não serve). É decisão de arquitetura, não bug.

---

## 7. Armadilhas do harness (custa caro rediscover)

1. **SQLite do container: nunca abrir por fora.** Já trunca dados. Ver C-7.
2. **OXS faz AST audit no enable.** Uma chamada proibida no plugin = extensão
   morta, sem erro visível. Hoje `test_analyzer.py` roda o scanner do framework.
3. **Core não é bind-mount.** `docker cp` + `restart`, sempre. Ver P1.
4. **Duas cópias da extension.** `odysseus-w2a/extensions/…` (dev, onde as suítes
   carregam) e `odysseus/extensions/…` (core, vai pro container). Já divergiram
   uma vez. `test_analyzer.py` agora exige byte-idêntico.
5. **WAL sobre FUSE trunca escritas.** Sintoma: rows que somem ou "regredem".

---

## 8. Como rodar

```bash
export PYTHONPATH=~/Documents/odysseus          # obrigatório: suítes importam o core

cd ~/Documents/odysseus-w2a
python3 tests/test_analyzer.py                  # manifest, hashes, AST, sync
python3 tests/test_analyzer_intervals.py        # 28 checks de fila
python3 tests/test_analyzer_soak.py             # 5 checks, relógio virtual
python3 -u tests/test_analyzer_e2e.py            # E2E contra o container live
python3 -m pytest test_extension_whats2api_bridge.py -q   # transporte (19/26 — P0)
```

**Deploy da extension:**

```bash
LOCAL=~/Documents/odysseus-w2a/extensions/whats2api_bridge
docker exec odysseus-odysseus-1 rm -rf /app/extensions/whats2api_bridge/__pycache__
docker cp "$LOCAL/plugin.py"      odysseus-odysseus-1:/app/extensions/whats2api_bridge/plugin.py
docker cp "$LOCAL/extension.json" odysseus-odysseus-1:/app/extensions/whats2api_bridge/extension.json
docker cp "$LOCAL/ui/index.html"  odysseus-odysseus-1:/app/extensions/whats2api_bridge/ui/index.html
docker cp "$LOCAL/ui/script.js"   odysseus-odysseus-1:/app/extensions/whats2api_bridge/ui/script.js
docker compose -f ~/Documents/odysseus/docker-compose.yml restart odysseus
```

**Depois de mexer em `extension.json`:** recalcular `file_hashes` — o smoke falha
de propósito, mas o cache do navegador não.

---

## 9. Referência

| | |
|---|---|
| Repos | `~/Documents/odysseus-w2a/` (dev) · `~/Documents/odysseus/` (core) |
| Container | `odysseus-odysseus-1`, porta `7011` |
| Branch core | `dev` |
| Spec da extension | `extensions/whats2api_bridge/SPEC.md` |
| Estado vivo | `docs/STATUS.md` |
| Handoff anterior (findings 1–7) | `docs/odysseus-core-handoff.md` |