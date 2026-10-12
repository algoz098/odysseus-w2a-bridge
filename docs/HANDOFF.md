# Handoff — `whats2api_bridge` / Conversation Analyzer

**Data:** 2026-10-12 (revisão 2)
**Para:** próxima equipe nesta extension + no core
**Estado:** ciclo de análise **e** ciclo de transporte validados ponta a ponta
**Última bateria:** transporte mock 53 ✅ · **transporte HTTP real 24** ✅ ·
**inbound real 28** ✅ · manifesto/AST/sync ✅ · 30 intervalos ✅ · 5 soak ✅ ·
crew-allowlist 5 ✅ · E2E ✅ · restart ✅ · sync repo→container ✅

> **Verificado contra o provedor real (2026-10-12).** `health`,
> `list_connections`, `is_on_whatsapp`, `call_method` (GetJoinedGroups) e um
> `send_message` de verdade — id `3EB031AA875C13D6DEAEA0`, engine `hypermeow`.
> Isso expôs quatro erros de contrato que nenhuma suite mockada pegaria:
> `params` é array posicional, o corpo é protojson `waE2E.Message`, os dois
> engines usam nomes de método diferentes, e `/connections` é envelope
> paginado. Ver §10.
>
> **O que ainda não foi provado:** nenhum evento inbound **real** chegou. A
> webhook configurada no painel do provider aponta para
> `http://192.168.1.164:7011` — IP de LAN. Se o whats2api for hospedado, ele
> não alcança isso. Ver P0.
>
> **Ação do operador pendente.** A suíte antiga escrevia no estado vivo e
> deixou `api_token: "fake"` e o token do webhook inbound igual a `"token"`
> (5 caracteres) em `data/oxs/permissions/whats2api_bridge.json`, que é
> bind-mount para o container. Ver **A-7**. Antes de confiar em qualquer
> tráfego WhatsApp real: reintroduza o token da API e reconfigure a URL do
> webhook em whats2api.com. Nada aqui pode ser corrigido por código — são
> credenciais que o teste sobrescreveu.

---

## 1. Objetivo

### Objetivo final

Ler conversas de WhatsApp e extrair ações para artefatos nativos do Odysseus
(task / calendar / note / memory), **em modo somente-leitura sobre o WhatsApp**,
**exatamente uma vez**, sem intervenção humana, com harness determinístico e
coberto por testes ponta a ponta.

### Objetivo atual

Os dois ciclos — análise e transporte — estão validados e publicados no
container. O core subiu. O que resta é operação (§5 P3/P4) e a rotação de
credencial acima.

### Não-objetivos (importante para não perder tempo)

- O **Analyzer** nunca responde WhatsApp. Não é bug, é o produto — e agora é
  **verificado em runtime**, não só escrito no prompt (ver invariante 7).
- Não é multi-tenant. Escopo é do operador.
- O provedor é `api.whats2api.com`; a extensão não implementa protocolo
  WhatsApp, ela fala HTTP com um serviço.

---

## 2. O que a extensão é (conceito em 30 segundos)

Dois papéis que não devem ser confundidos:

- **A ponte** é *transporte*: conectividade WhatsApp ↔ Odysseus. 16 tools
  outbound (SPEC §4.1/§4.2) + 5 tools locais de leitura. Não interpreta nada.
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
7. **O Analyzer é read-only sobre WhatsApp** — cada tool de transporte
   consulta `_refuse_if_analyzer()` e recusa a chamada vinda da sessão do
   Analyzer. Isto **não** pode ser derivado de allowlist de crew: ver C-8,
   onde o Analyzer foi *oferecido* `send_message` ao vivo.

---

## 3. Achados no CORE do Odysseus

**Dez defeitos. Todos corrigidos, todos com teste de regressão.**

O padrão: **nenhum aparece em conversa interativa.** Todos aparecem quando o
cliente é uma extension rodando *desatendida, com prompt-manifesto, de forma
recurrente*. Se a próxima equipe achar estranho o core ceder tanto, o motivo é
esse — é a primeira classe de tráfego real desse formato.

### C-8 — Allowlist de crew não cobre tools `mcp__*`  ← **o mais importante**

- **Onde:** `src/task_scheduler.py`, `_execute_llm_task`
- **Por quê:** `disabled_tools` é derivado invertendo `enabled_tools` do crew
  sobre `BUILTIN_TOOL_DESCRIPTIONS.keys()`. Esse dict **não contém nenhum
  nome `mcp__*`**. Logo o crew do Analyzer — cujo allowlist existe
  precisamente para impedir escrita no WhatsApp — não bloqueia nada.
  O scheduler tem o outro meio da fórmula: `always_include` força-inclui os
  `mcp__*` que o crew **lista**. O "deny" para os que ele não lista não existe.
- **Consequência:** comprovado ao vivo, no log de um run real às 17:00 — o
  Analyzer recebeu no schema `send_message`, `call_method`, `send_media`,
  `mark_read`, `send_presence`, `send_reaction`, `create_connection`,
  `remove_connection`, `start_connection`, `stop_connection`. A frase
  "NEVER call send_message" no prompt era a única coisa entre o Analyzer e
  uma mensagem para o seu chefe.
- **Mitigação aplicada:** o enforcement foi para dentro da extension
  (`_refuse_if_analyzer`, teste `test_analyzer_is_refused_every_transport_tool`).
- **Por que não corrigi no core:** fechar a metade que falta muda a
  semântica de *todo* crew com `enabled_tools` — todos os MCP tools não
  listados passam a sumir. É a leitura correta de "allowlist", mas é uma
  mudança de produto que precisa de decisão do time de core. Ver P4.

### C-9 — `_INPROC_SESSION` é truthy e vaza para o dispatch

- **Onde:** `src/mcp_manager.py`, `call_tool`
- **Por quê:** tools in-process são registradas com um sentinel
  `_INPROC_SESSION = object()` em `self._sessions`. Ele é *truthy*, então um
  nome de tool que **não** está em `_in_process_tools` caía no caminho de
  sessão e chamava `session.call_tool(...)` num `object()`.
- **Consequência:** `'object' object has no attribute 'call_tool'` — erro
  interno do manager escondendo a causa real (a extension não registrou a
  tool). Foi exatamente a mensagem que mascarou A-5 por semanas.
- **Fix:** devolve erro nomeando o server, a tool e a causa.
- **Teste:** `test_send_message_requires_connection_id` (via A-5).

### C-10 — Envelope de erro do plugin perdia os campos extras

- **Onde:** `src/mcp_manager.py`, `_format_in_process_result`
- **Por quê:** com `{"error": ...}` no dict, o formatador devolvia só
  `{"error", "exit_code"}`. O SPEC §4.3 manda o envelope carregar
  `status_code` e `body` (distinguir "o upstream me rejeitou" de "eu
  falhei"), e extensions anexam marcadores próprios.
- **Consequência:** um handler que funcionava corretamente ainda expunha uma
  string crua;arkers machine-readable (`blocked_by`) sumiam.
- **Fix:** preserva as chaves extras do envelope, sem `ok` (que é marcador
  interno e concorre com `exit_code`).

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
  *Exceção consciente:* inspeção pontual de leitura com
  `sqlite3.connect("file:...?mode=ro", uri=True)` é segura **com o
  `journal_mode=DELETE` do C-7 no lugar** (não há WAL para checkpointar).
  Verificar o PRAGMA antes de confiar nisso.

---

## 4. Achados na EXTENSÃO

### A-7 — A suíte de testes escrevia no estado vivo do operador  ← **dano já feito**

- **Onde:** `test_extension_whats2api_bridge.py` (sem isolamento) + ausência de `conftest.py`
- **Por quê:** `SettingsNamespace.set()` persiste em disco, de forma
  síncrona, em `data/oxs/permissions/<id>.json`. O teste escrevia
  `api_token="fake"` e `default_connection_id="DEFAULT_CID"`; outro escrevia
  `webhook_url_token="token"`. `./data` é bind-mount (`docker-compose.yml`:
  `${APP_DATA_DIR:-./data}:/app/data`), então o estrago do host **chegou ao
  container rodando** — o mesmo arquivo, literalmente.
- **Consequência:** `api_token` real substituído por `"fake"` → todo outbound
  dá HTTP 401. Token do webhook inbound virando a string de 5 caracteres
  `"token"` → qualquer URL configurada em whats2api.com responde 404. E
  ninguém percebia: a suíte continuava verde.
- **Fix:** `conftest.py` na raiz do repo redireciona `DATABASE_URL`,
  `OXS_PERMISSIONS_DIR` e `DATA_EXTENSIONS_DIR` para uma árvore temporária.
  `W2A_TEST_LIVE_STATE=1` é o opt-out explícito, só para investigar incidente
  em produção.
- **Verificado:** com um sentinel plantado no arquivo de settings, a suíte
  roda inteira sem tocá-lo.
- **Pendente do operador:** reintroduzir o token real e reconfigurar a URL do
  webhook. Não é corrigível por código.

### A-6 — Adoption selava intervalos vivos  ← **perda silenciosa, bloqueou o E2E**

- **Onde:** `plugin.py`, `_adopt_existing_intervals`
- **Por quê:** a migração pré-intervalização rodava "enquanto a tabela de
  estado estivesse vazia" e lia isso como "nunca houve rastreamento". Mas
  tabela vazia também é o estado de um container novo, um data-dir apagado ou
  um bump de schema — e então **tudo** que estivesse na janela era marcado
  `analyzed` com o resumo `adopted: pre-intervalization content`, sem nunca
  ter sido analisado.
- **Consequência:** o E2E falhou 4/4 com "No new conversation intervals." A
  tabela tinha exatamente uma linha, selando a mensagem que o teste tinha
  acabado de semear. O mesmo mecanismo engole qualquer conversa que chegue
  com `ts` antigo (clock skew, webhook reprocessado) — sem erro em lugar
  nenhum.
- **Fix, em duas partes:**
  1. **Fronteira explícita** (`analyzer_tracking_since`, armada uma vez no
     primeiro enable). Só mensagem *anterior* à fronteira pode ser adotada;
     o resto é trabalho vivo.
  2. **Opt-in** (`analyzer_adopt_existing`, default `False`, auto-limpa após
     rodar). Adoption marca conteúdo como analisado sem ninguém ter
     analisado — errar só pode perder dados; não rodar custa uma
     re-offerência visível e recuperável.
  - **Por que opt-in e não só a fronteira:** a fila só oferece intervalos
    silenciosos há `gap_seconds` (45 min), então conteúdo testável é
    necessariamente *backdated* em relação a qualquer fronteira de
    wall-clock. Adoption automática e um E2E que semeia são
    incompatíveis por construção.
- **Teste:** `test_adoption_never_seals_content_that_arrived_after_tracking_started`,
  `test_adoption_refuses_to_run_without_a_boundary` (28 → 30).

### A-5 — Os 11 tools de transporte nunca existiram

- **Onde:** `plugin.py`, loop de registro de MCP tools
- **Por quê:** o loop iterava **só** `_read_tools(...)`. Não havia factory
  para a família de transporte. `send_message`, `call_method`, `health` e o
  ciclo de vida de conexções estavam no docstring do módulo, na tabela §4.1/4.2
  do SPEC, na lista de tools proibidos do Analyzer — e em nenhum lugar como
  código. Confirmei em **todos** os commits desde `1a39bb0c` e no backup
  `.zip`: nunca foram registrados.
- **Consequência:** o "papel de transporte" do produto não existia. Os 4
  testes que o cobriam falhavam com `'object' object has no attribute
  'call_tool'` (C-9) desde o primeiro commit — e foram reclassificados como
  "expectations obsoletas". **Uma suíte cujas falhas são ruído conhecido
  deixa de ser lida**; foi assim que a metade inteira do produto passou
  semanas invisível.
- **Fix:** `_transport_tools()` com as 16 tools do SPEC, funiladas por
  `POST /connections/request {connectionId, method, params}`; registradas
  com `read_only=False` (escrevem no WhatsApp — o gate de aprovação tem de
  falhar alto), cada uma embrulhada em `_refuse_if_analyzer`.
- **Teste:** 26 checks novos — mapa RPC por tool, rejeição de args
  incompletos *antes* da rede, rota por operação de conexão, `health` que
  responde em vez de explodir, evento `whats2api.message.sent`, e o guard do
  Analyzer nos 16 tools. Suíte: 26 → 52.

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

### P0 — Credenciais do provider  ← **única prova que falta**

`data/oxs/permissions/whats2api_bridge.json` (bind-mount, container incluso)
está com `api_token: "fake"` e `default_connection_id: "DEFAULT_CID"`. Ver A-7.

**A rotação do webhook já foi feita e verificada:** apagando o arquivo e
reabilitando, o token passa a 43 chars URL-safe (32 bytes), é persistido, o
antigo responde 404 e o novo responde 202. A URL para colar no whats2api.com
é `POST http://<host>:7011/api/ext/whats2api_bridge/webhook/<token>` com
`whats2api: true`, `userId` e `connectionId`.

**O que falta:** reintroduzir o token real da API. Sem ele o transporte não
foi provado contra o provedor — só contra o stub local
(`test_transport_integration.py`). É o único item que não é código, e nenhuma
quantidade de suíte verde substitui ele.

**Depois de colocar o token**, o smoke é:

```bash
python3 - <<'EOF'
# health -> list_connections -> is_on_whatsapp -> send_message para si mesmo
EOF
```

Eu rodo se você mandar o token; enviar mensagem para fora é ação do
operador, não minha.

### P1 — ~~Cobertura de transporte~~ **feito**

A suíte de transporte cobre os 16 tools, o ciclo de vida, o guard do Analyzer e
os envelopes de erro. 52/52. Era P0 na revisão anterior com a premissa errada
de que os tools existiam e só faltava rede de segurança — ver A-5.

### P2 — ~~Check repo→container~~ **feito** · `tests/test_deploy_sync.py`

O core não é bind-mount: `src/` é embutido na imagem e o deploy Historically
era `docker cp` + `restart`. Já aconteceu duas vezes nesta sessão de o
container rodar código velho. Agora há um check automático de hash nos
**três** hops que existem (w2a → core checkout → cópia instalada → container),
cobre os `file_hashes` do manifesto, e faz o deploy com `--deploy`.

```bash
python3 tests/test_deploy_sync.py                  # reporta
python3 tests/test_deploy_sync.py --rehash         # recalcula file_hashes
python3 tests/test_deploy_sync.py --rehash --deploy # sincroniza e restarta
```

### P2b — Deploy agora é *build*, não *copy*  ← **muda o fluxo**

O jeito certo de subir mudança no core é rebuildar a imagem, porque é o que
elimina a categoria inteira de bug do P2 — nada mais depende de alguém
lembrar de `docker cp`:

```bash
cd ~/Documents/odysseus
docker compose build odysseus          # src/ e extensions/ entram via COPY . .
docker compose up -d odysseus          # recria o container
```

A extensão entra junto: `extensions/` **não** está no `.dockerignore`, então a
w2a bridge já vai embutida. `ODYSSEUS_IMAGE=odysseus-local:dev` no `.env`.

`--deploy` do `test_deploy_sync.py` continua sendo o atalho para mudança só de
extension — é mais rápido que rebuild e não toca no resto da imagem. Mas para
mudança em `src/`, build.

> **Atenção ao disco:** a imagem tem ~3,6 GB e a máquina estava com 3,6 GB
> livres. O build incremental só invalida de `COPY . .` em diante, então
> cacheado é barato — mas uma build do zero não cabe. `docker builder prune`
> libera ~4,8 GB se precisar.

### P3 — ~~Teste de restart~~ **feito** · `tests/test_restart.py`

`docker restart` e afirma: extension re-habilitada, task do Analyzer `active`,
**mesmo token de webhook antes e depois** (a rotação silenciosa era o defeito
mais caro e é invisível de dentro do app, porque o app aceita o token novo
dele mesmo), `plugin.py` inalterado, storage legível.

### P4 — Levar os fixes de core para upstream

C-1 a C-10 estão em `dev` e afetam **qualquer** usuário do Odysseus, não só
esta extension. Ficarem aqui é conhecimento perdido.

**O C-8 foi corrigido no core e é a mudança com mais chance de tocar outros
usuários** — todo crew com `enabled_tools` que nomeie alguma tool `mcp__*`
passa a ter o resto bloqueado. Foi feito assim deliberadamente: a inversão só
ativa para quem **declara** tools MCP, então crew nativo-only fica intocado.
Ainda assim é mudança de semântica e quer uma revisão do time de core.

Documentado em `docs/odysseus-core-handoff.md` (addendum: findings 5, 6, 7).

### P5 — Soak em tempo real

20 runs em 14 min não é soak. O hang de 51 min só apareceu com horas de
uptime. O soak determinístico (relógio virtual) cobre lógica; não cobre
comportamento de provider real ao longo do tempo. Bloqueado por P0.

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
- **Nota:** os 4/4 "No new conversation intervals." que o E2Evia mostrar antes
  da revisão 2 **não** eram este comportamento — eram o A-6 engolindo o
  intervalo semeado. Distinga os dois antes de "confirmar" a taxa de 8%.

---

## 7. Armadilhas do harness (custa caro rediscover)

1. **SQLite do container: nunca abrir por fora.** Já trunca dados. Ver C-7.
2. **OXS faz AST audit no enable.** Uma chamada proibida no plugin = extensão
   morta, sem erro visível. `test_analyzer.py` roda o scanner do framework.
3. **Core não é bind-mount.** `restart` **não** é deploy. Subir mudança em
   `src/` é rebuildar a imagem (`docker compose build odysseus && up -d`).
   O atalho `docker cp` + `restart` do `test_deploy_sync.py --deploy` serve
   para mudança só de extension. Ver P2/P2b.
4. **Três cópias da extension, e a discovery nem sempre escolhe a que você
   editou.** `odysseus-w2a/extensions/…` (dev), `odysseus/extensions/…`
   (bundled, tem prioridade na discovery) e
   `odysseus/data/extensions/…` (instalada, `data_path`). As suítes carregam a
   **bundled**. Editar a de dev e rodar os testes não muda nada — foi assim
   que A-5 ficou invisível por um tempo. `test_analyzer.py` exige
   byte-idêntico w2a↔core, e `test_deploy_sync.py` fecha o segundo hop.
5. **WAL sobre FUSE trunca escritas.** Sintoma: rows que somem ou "regredem".
   Corrigido em C-7 (`journal_mode=DELETE`).
6. **Settings de extension persistem em disco a cada `set()`**, e o `data/`
   é bind-mount do container. Um `settings.set()` num teste reescreve a
   credencial do operador *no container rodando*. Foi o A-7. Números de porta
   não são o problema — o diretório é o mesmo.
7. **Allowlist de crew não alcança tools `mcp__*`.** Não confie nela para
   conter escrita em WhatsApp. Ver C-8.
8. **Uma falha que sempre acontece deixa de ser lida.** Os 4 testes de
   transporte estavam vermelhos desde o primeiro commit e foram reclassificados
   como "expectations obsoletas" — era a única evidência de que a metade do
   produto não existia. Distinga "teste desatualizado" de "teste descrevendo
   algo que nunca foi implementado", e escreva o motivo no teste.

---

## 8. Como rodar

```bash
export PYTHONPATH=~/Documents/odysseus          # obrigatório: suítes importam o core

cd ~/Documents/odysseus-w2a
python3 -m pytest test_extension_whats2api_bridge.py -q  # transporte (mock), 53
python3 -m pytest test_transport_integration.py -q      # transporte (HTTP real), 24
python3 -u tests/test_inbound_integration.py            # inbound real, 28 checks
python3 tests/test_analyzer.py                            # manifest, hashes, AST, sync
python3 tests/test_analyzer_intervals.py                  # 30 checks de fila
python3 tests/test_analyzer_soak.py                       # 5 checks, relógio virtual
python3 -u tests/test_analyzer_e2e.py                     # E2E contra o container live
python3 -u tests/test_restart.py                          # durabilidade de restart
python3 tests/test_deploy_sync.py                         # drift repo → container
```

**Deploy de mudança no core (o jeito certo):**

```bash
cd ~/Documents/odysseus
docker compose build odysseus && docker compose up -d odysseus
```

**Deploy de mudança só na extension (atalho):**

```bash
cd ~/Documents/odysseus-w2a
python3 tests/test_deploy_sync.py --rehash --deploy
```

Cobre os dois hops (w2a → core checkout → container), recalcula
`file_hashes` do manifesto e reinicia o serviço. `--rehash` sozinho é o que
se usa depois de editar `plugin.py` ou `extension.json`.

**Depois de mexer em `extension.json`:** `--rehash`. O smoke falha de
propósito, mas o cache do navegador não.

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
| Isolar testes do estado vivo | `conftest.py` · `W2A_TEST_LIVE_STATE=1` é o opt-out |
---

## 10. O contrato real do whats2api (aprendido com credencial, não na doc)

Tudo aqui veio de rejeições do `api.whats2api.com`. Um stub escrito a partir
da documentação pública erra em quatro coisas, e a doc é ambígua justamente
onde o provider é rigoroso.

### 10.1 Duas credenciais, não uma

| Credencial | Onde funciona |
|---|---|
| **Token MCP** (dashboard → `/p/mcp-tokens`) | `/mcp/<token>`; válido, mas **não** na REST |
| **JWT diário** (`POST /authentication`, email+senha) | `GET /connections`, `POST /connections/request` |

O token MCP na REST dá
`401 {"message":"jwt malformed","className":"JsonWebTokenError"}` — ele é
64 hex sem os dois pontos que um JWT tem. O `api_token` da extension agora
é *fallback*; o caminho principal é `api_email` + `api_password`, com
re-mint e **um** retry quando a REST devolve 401.

### 10.2 `params` é array posicional

```
POST /connections/request
{"connectionId": "...", "method": "SendMessage",
 "params": ["<jid>@s.whatsapp.net", {"extendedTextMessage": {"text": "..."}}]}
```

Objeto é recusado com `"/params" ... must be array`. E o layout é
**posicional e por método** — `SendMessage` é `[jid, message]`, `MarkRead` é
`[{chat, ids}]`, `IsOnWhatsApp` é `["<phone>", ...]` (uma string por número;
`{"phones": [...]}` e `[[...]]` são recusados).

### 10.3 O corpo é `waE2E.Message` protojson

`{"text": "..."}` → `proto: unknown field "text"`. O texto vive em
`extendedTextMessage.text`, e `contextInfo` (citação, menção) dentro dele.
`{"conversation": "..."}` também valida e é a forma simples legada, mas não
carrega citação.

### 10.4 Os engines não compartilham nomes

A conexão é `engine: hypermeow` e responde
`method not on whatsmeow client: onWhatsApp` para a grafia Baileys.

| Intenção | baileys | hypermeow |
|---|---|---|
| texto | `sendMessage` `[jid, {text}]` | `SendMessage` `[jid, {extendedTextMessage:{text}}]` |
| reação | `sendMessage` `[jid, {react:{text,key}}]` | `SendMessage` `[jid, {reactionMessage:{…}}]` |
| marcar lida | `readMessages` `[chat, ids]` | `MarkRead` `[{chat, ids}]` |
| presença | `sendPresenceUpdate` `[presence]` | `SendPresence` `[presence]` |
| no WhatsApp | `onWhatsApp` `["jid", …]` | `IsOnWhatsApp` `["jid", …]` |

O engine é lido do documento da conexão e memoizado por 1 min.

### 10.5 `/connections` é envelope paginado

whats2api é Feathers: `{"total": n, "limit": 10, "skip": 0, "data": [...]}`.
Assumir lista fazia `health` reportar `connection_count: 0` na conta real
**parecendo saudável**.

### 10.6 O que o provider recusou, e por quê importa

- `send_media` em hypermeow exige `fileLength` (bytes da mídia). Não é
  derivável, então a tool **recusa** em vez de mandar algo que vai falhar.
- `MarkRead` no path hypermeow pede `params` array e ao mesmo tempo tenta
  deserializar o array como struct — nenhuma forma testada passou. Não
  documentado como quebrado, apenas como não verificado.
- `SendPresence` com `[presence]` tem o shape certo e devolve
  `500 fetch failed` — falha interna do provider, não da extension.

---

## 11. Estado da credencial e do webhook

- `.env` (git-ignored, 0600) tem `WHATS2API_EMAIL` / `WHATS2API_PASSWORD`.
  `tests/load_credentials.py` carrega nos settings sem passar a senha por
  shell. **Nunca commitar.**
- Token do webhook: `"1"`, por decisão do operador. Persiste por
  `_apply_url_token`, que grava o pin em disco — verificado através de
  `docker restart` **e** disable/enable.
- URL: `http://192.168.1.164:7011/api/ext/whats2api_bridge/webhook/1`
- **Bloqueio para inbound real:** `192.168.1.164` é LAN privada. Um
  whats2api hospedado não alcança. Precisa de túnel (cloudflared/ngrok) ou
  de uma porta pública — nenhuma das duas é código, é decisão de rede.
