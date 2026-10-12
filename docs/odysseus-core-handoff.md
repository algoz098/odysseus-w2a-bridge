# Report: Lacunas em `Odysseus` core que afetam tarefas LLM agendadas por extensions OXS

**Projeto:** `odysseus-w2a` (extension Whats2API Bridge para Odysseus)
**Autor:** extension: `whats2api_bridge` v0.5.0
**Severidade:** média — funcionalidade utilizável com mitigações na extension; correção total exige patches em core
**Branch sugerido:** `core/scheduled-task-auth-and-tool-routing`

---

## TL;DR

Ao construir uma rotina de **Conversational Analyzer** que roda como `ScheduledTask(task_type="llm")` a partir de uma extension OXS (`extensions/whats2api_bridge/`), esbarramos em quatro problemas do Odysseus core que precisam ser endereçados por vocês:

1. `restore_task_authority` devolve authority **vazia** para `task_type='llm'` (sem grants de tools).
2. `register_in_process_tool` salva `description = "[<ext_id>] <name>"` — sem conteúdo semântico — então `tool_index` RAG não casa MCP tools de extensions no `k=8` retrieval.
3. `stream_agent_loop` **bloqueia todas as `known_tool_names`** quando o caller não passa `external_tool_schemas` (afeta tasks em background).
4. `run_task_now` é coroutine mas o canal de error reporting (`oxs_admin_analyzer_run_now`) a trata como retorno síncrono.

Patch 1 já tem contorno aplicado via `request_authority_json` mintado na extension. Patches 2–4 estão pendentes em core.

---

## Contexto

A extension `whats2api_bridge` ganhou um módulo "Conversation Analyzer" (analisador periódico de conversas WhatsApp). É um `ScheduledTask` com `task_type="llm"`, `prompt` rico, `crew_member_id` apontando para um `CrewMember` com `enabled_tools` filtrando WhatsApp writes.

A rotina está aqui, no caso de vocês quererem rodar:

```
odysseus-w2a/extensions/whats2api_bridge/plugin.py   (linha 1981+: _analyzer_reconcile,
                                                        2046+: _analyzer_run_now,
                                                        2342+: _admin_analyzer_patch)
odysseus-w2a/tests/test_analyzer.py                  (smoke tests cobrindo o reconcile)
```

A `ScheduledTask` é criada via `extensions/whats2api_bridge/plugin.py:2029` (insert) com `request_authority_json` mintado no `_patch_analyzer`. A tarefa roda via `task_scheduler.run_task_now(task_id)` e o scheduler chama `stream_agent_loop` com o prompt + tools filtradas + a authority carregada.

---

## Finding 1 — `RequestAuthority` vazia para tarefas LLM

**Arquivo:** `src/task_scheduler.py:2132`, `src/agent_runtime/authority.py:509-516`

### Onde

```python
# src/agent_runtime/authority.py (linha 509-516)
def task_operation(task_type, action, prompt):
    if task_type == "action":
        tool = ("bash" if action in {"run_local", ...} else ...)
        return ExactOperation.normalize(tool, str(prompt or ""))
    if task_type == "research":
        return ExactOperation.normalize("trigger_research", str(prompt or ""))
    return None    # ← LLM tasks caem aqui
```

```python
# src/task_scheduler.py (linha 2130-2137)
authority = restore_task_authority(
    getattr(task, "request_authority_json", None), task.prompt, task.task_type,
    getattr(task, "action", None), owner=task.owner)
authority = authority.restrict(disabled_tools=get_setting("disabled_tools", []) or ())
```

```python
# src/agent_runtime/authority.py (linha 199-201)
@classmethod
def empty(cls, *, owner=None, session_id=None, workspace=None):
    return cls(uuid4().hex, _owner(owner), str(session_id or ""), str(workspace or ""),
               resource_roots=(), backend_resources=(), owned_scopes=(), launch_scopes=(),
               job_resources=(), browser_sessions=(), browser_pages=())
```

### Por quê

`task_operation` retorna `None` para `task_type="llm"`. `seal_task_authority` constrói authority com `grants=()` (zero grants). `_patch_analyzer` de uma extension **não tem como** criar authority com grants significativos via `seal_task_authority` porque ela delega à `task_operation`, e essa não retorna operation para LLM.

Resultado: `restore_task_authority` em uma task LLM sem `request_authority_json` salvo retorna `RequestAuthority.empty(...)` — denials = vazio, grants = vazio, `permits()` cai no `any(g.permits(...) for g in self.grants)` → sempre False.

### Repro

```python
from src.task_scheduler import TaskScheduler
from core.database import SessionLocal, ScheduledTask
db = SessionLocal()
task = ScheduledTask(task_type="llm", prompt="hello", name="t", status="active")
from src.agent_runtime.authority import RequestAuthority
# Sem snapshot authority_json → empty → permits() False → todo tool retorna request_authority_denied
```

### Consequência observada

O LLM recebe tools no schema mas toda chamada volta com `request_authority_denied`. `manage_tasks` / `manage_notes` retornam `"Tool 'manage_X' is disabled by the current request policy"`. Em log:

```
2026-10-11 00:08:33 src.task_scheduler WARNING request_authority_json is empty for LLM task
2026-10-11 00:08:33 src.agent_loop   ERROR   manage_notes: request_authority_denied
```

### Fix proposto

Possibilitar que `task_operation` retorne uma `ExactOperation` simbólica para LLM tasks (ex.: com tool name `__agent_loop__`, especial — usado apenas para fixar os grants quando o caller calcula a partir do prompt). Alternativa mais limpa: estender `restore_task_authority` para que, se `task_type="llm"` e `snapshot` fornecido, **parse do prompt** para detectar tools via `interpret_request(...)` (já existe em `create_request_authority`) e montar grants correspondentes — mesma heurística do foreground chat.

```python
def restore_task_authority(snapshot, prompt, task_type, action, *, owner=None, session_id=None):
    try:
        value = json.loads(snapshot)
        ...
    except (ValueError, TypeError, KeyError, AttributeError):
        if task_type == "llm" and prompt:
            # Fallback: parse the prompt intent and mint a fresh authority
            # using create_request_authority — same code path as the chat
            # turn, no snapshot required.
            return create_request_authority(str(prompt), owner=owner)
        return RequestAuthority.empty(owner=owner, session_id=session_id)
```

**Severidade:** alta. Sem isso, todo LLM task criado por extension (não pelo chat UI) só funciona se o extension mints authority manualmente — caminho que nem tem helper público limpo.

---

## Finding 2 — Descrições vazias em `register_in_process_tool` quebram RAG

**Arquivo:** `core/extensions/mcp_bridge.py:52-67`, `src/mcp_manager.py:540-609`

### Onde

```python
# core/extensions/mcp_bridge.py (linha 58)
description=f"[{extension_id}] {reg.tool_name}",
```

```python
# src/mcp_manager.py (linha 565-569)
self._in_process_tools[server_id][tool_name] = {
    "input_schema": input_schema,
    "description": description,
    "handler": handler,
}
```

### Por quê

`install_tools` (chamado em `host.register_mcp_tool`) passa descrição mínima pra `McpManager.register_in_process_tool`. Essa descrição entra em `mgr._in_process_tools[server_id][tool_name]["description"]`, depois é indexada por `tool_index.index_mcp_tools()`:

```python
# src/tool_index.py (linha 326-333)
doc_text = f"Tool: {name}{server_ctx}\n{desc}"
```

O texto indexado acaba sendo `Tool: mcp__oxs_X__list_messages (server: oxs_X)\n[<ext_id>] list_messages` — sem nenhuma palavra semântica (sem "messages", "list", "read", etc.). Quando a query do LLM é "WhatsApp conversation recent messages", o RAG (k=8) rankeia outras 8 ferramentas antes das MCP — a bridge nunca aparece em `relevant_tools`.

### Repro

```python
from src.tool_index import get_tool_index
idx = get_tool_index()
ids = idx.get_tools_for_query(
    "Analyze recent WhatsApp messages from the operator's contacts",
    k=20,
)
# Espera `mcp__oxs_whats2api_bridge__list_messages` aparecer — nunca aparece
```

### Consequência observada

O LLM recebe o tool no `system_prompt` (em formato inline-tools) mas o **runtime** rejeita como `unoffered textual call`:

```
2026-10-11 00:06:40 src.agent_loop WARNING -> DROPPED unoffered textual call(s): ['mcp__oxs_whats2api_bridge__list_messages']
```

O LLM responde "I don't have access to `list_messages`" mesmo com a extension registrada ativamente.

### Fix proposto

Duas opções:

**(A)** **No OXS install path**: `core/extensions/mcp_bridge.py` deve aceitar (ou extrair de `MCPToolRegistration`) um campo opcional `description_rich: str | None = None` e usar isso se fornecido. Default permanece o atual `"[<ext_id>] <name>"`.

```python
# MCPToolRegistration ganha:
description_rich: str | None = None

# mcp_bridge.py:install_tools
rich = reg.description_rich or f"[{extension_id}] {reg.tool_name}"
manager.register_in_process_tool(
    server_id=server_id,
    tool_name=reg.tool_name,
    input_schema=input_schema,
    description=rich,
    handler=reg.handler,
)
```

**(B)** **No McpManager**: oferecer `set_tool_description(server_id, tool_name, description)` que muta `self._in_process_tools[server_id][tool_name]["description"]` em lugar e dispara `index_mcp_tools` reindex.

Eu recomendo (A) — mais explícito e consegue preservar anotações em manifests.

**Severidade:** média. Afeta toda extension OXS que expõe MCP tools com nomes não auto-descritivos.

---

## Finding 3 — `stream_agent_loop` bloqueia `_COMMON_TOOL_NAMES` quando caller não passa `external_tool_schemas`

**Arquivo:** `src/agent_loop.py:21306-21324`, `src/agent_loop.py:21021-21066`

### Onde

```python
# src/agent_loop.py:21306
_caller_disabled_tools = set(disabled_tools or [])
if tool_policy:
    _caller_disabled_tools.update(tool_policy.all_disabled_names())

disabled_tools = set(disabled_tools or [])
# ... guide-only branch ...

if normalized_external_tool_schemas:
    _declared_external_names = {
        schema["function"]["name"]
        for schema in normalized_external_tool_schemas
    }
    _request_scoped_disabled = known_tool_names() - _declared_external_names
    _caller_disabled_tools.update(_request_scoped_disabled)
    disabled_tools.update(_request_scoped_disabled)
```

```python
# src/tool_policy.py:204-217
def known_tool_names() -> Set[str]:
    names = set(_COMMON_TOOL_NAMES)
    ...
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        for schema in FUNCTION_TOOL_SCHEMAS:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if name:
                names.add(name)
    except Exception:
        pass
    ...
    return names
```

```python
# src/tool_policy.py:75-148 (_COMMON_TOOL_NAMES)
_COMMON_TOOL_NAMES = {
    "manage_calendar", "manage_memory", "manage_notes", "manage_tasks",
    "search_chats", ...   # ← my analyzer's whole allowlist
}
```

### Por quê

`known_tool_names()` retorna **todos** os tool names do build. O check em `if normalized_external_tool_schemas:` (linha 21317) faz:

```python
_request_scoped_disabled = known_tool_names() - _declared_external_names
```

Se `_declared_external_names` é vazio (branch só roda quando caller passa `external_tool_schemas`), e considerando que TODOS os builtin tools estão em `_COMMON_TOOL_NAMES`, isso **bloqueia todos eles**. O bloco é gated, então só executa quando há `external_tool_schemas` — e em background tasks o scheduler **não passa** (verificado em `src/task_scheduler.py:2019-2033`). Logo, em teoria, esse branch não dispara.

Acontece que o LLM **ainda reporta tool bloqueada** em alguns casos. Suspeita: o operador executou LLM task via chat (foreground), ou um caminho secundário em agent_loop.py. O root cause exato está num dos ~5 estágios de `_enforce_caller_disabled_tool_policy` → `_request_scoped_disabled`. **Reprodução estável** ainda não foi isolada — pode ser intermitente dependendo do `client_runtime_context`.

### Repro

```bash
# 1. Configure user-scope default task policy:
curl -X POST -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"disabled_tools":["manage_calendar","manage_notes"]}' \
  http://localhost:7011/api/agent-config/policy
# 2. Trigger scheduled LLM task that wants manage_*:
curl -X POST -b cookies.txt .../ext-api/w2a/admin/analyzer/run-now
# Result: agent reports "Tool 'manage_calendar' is disabled by current request policy"
#         even though task's own allowlist enables it
```

### Fix proposto

Mudar para `requested_tools = known_tool_names() & allowed_set` em vez de denylist-negation — usar **explicit allow** baseado em interseção de:
- `crew_member.enabled_tools` (allowlist do task)
- Tools que o caller declarou via `external_tool_schemas`
- Tools já filtrados via RAG (`relevant_tools`)

```python
# Em _enforce_caller_disabled_tool_policy — preferir allow em vez de deny:
if not hard_disabled and not protected_mcp_filter:
    return relevant_tools, base_relevant_tools, tool_policy
# Quando hard_disabled tem entries, intersect com allowlist em vez de subtrair.
safe_disabled = {t for t in hard_disabled if t not in allowed_set}
```

**Severidade:** média. Hoje meu workaround no extension é mintar authority manualmente com grants específicos — mas isso é só um paliativo.

---

## Finding 4 — `run_task_now` é coroutine, mas canal de error reporting trata como síncrono

**Arquivo:** `src/task_scheduler.py:2384` (`run_task_now`); nossa extension: handler de error reporting

### Onde

```python
# src/task_scheduler.py:2384
async def run_task_now(self, task_id: str, *, force: bool = False):
    """Manually trigger a task execution."""
    if force:
        asyncio.create_task(self._execute_task(task_id, b...))
```

### Por quê

`run_task_now` é uma `async def` que faz `asyncio.create_task(...)` — retorna um handle de coroutine (não o ID da execução). Agents que tratam o retorno como se fosse um `run_id` síncrono recebem `True` (truthy coroutine object) e reportam success mesmo quando o dispatch falha imediatamente.

No caso da extension `whats2api_bridge`, o handler que invoca `sched.run_task_now(task_id, force=True)` em `plugin.py:_analyzer_run_now` (linha ~2050) tem que explicitamente `await asyncio.wait_for(scheduled, timeout=3.0)` para extrair ID e capturar erros síncronos antes que a coroutine seja descartada. **Esse é um detalhe de uso que força callers a conhecer internals do scheduler.**

### Repro

```python
from src.event_bus import get_task_scheduler
sched = get_task_scheduler()
result = sched.run_task_now("nonexistent-task-id")  # returns a coroutine, NOT None
# result is truthy
print(bool(result))  # True
# But the task did NOT execute (silently, in a background task).
```

### Fix proposto

Mudar `run_task_now` para retornar **o ID da `TaskRun` recém-criada** (uma string síncrona) ou `None` quando falha. Wrap do `asyncio.create_task` continua interno.

```python
def run_task_now(self, task_id: str, *, force: bool = False):
    """Manually trigger a task execution. Returns the TaskRun.id or None."""
    if not self._can_run_now(task_id, force=force):
        return None
    run_id = self._create_run_record(task_id)  # synchronous row insert
    asyncio.create_task(self._execute_task(task_id, ..., run_id=run_id))
    return run_id
```

**Severidade:** baixa — só afeta observability e debugging. Tasks ainda rodam, só perdemos o ID da run para inspeção.

---

## Tarefas que já contornei na extension (não são mais bloqueantes)

Estes foram consertados com patches em `extensions/whats2api_bridge/plugin.py` — descrevo aqui apenas como referência pra vocês saberem o que NÃO precisa ser corrigido no core:

| Workaround na extension | Local |
|---|---|
| Mint de `RequestAuthority` com grants `manage_*` + `mcp__*` + re-patch via `_admin_analyzer_patch` | `plugin.py:1665-1740` (`_patch_analyzer`) |
| Substituição dos placeholders no reconcile | `plugin.py:2019-2023` |
| `_enrich_mcp_tool_descriptions` em on_enable + bump `_generation` + `index_mcp_tools` | `plugin.py:441-487, 2586-2596` |

---

## Reprodução consolidada (passo-a-passo)

```bash
cd ~/Documents/odysseus-w2a
docker cp extensions/whats2api_bridge/plugin.py odysseus-odysseus-1:/app/extensions/whats2api_bridge/plugin.py
docker compose -f ~/Documents/odysseus/docker-compose.yml restart odysseus

# Login no SPA, copy cookie, then:
COOKIE='odysseus_session=...'

# Seed messages
docker cp /tmp/seed_messages.py odysseus-odysseus-1:/tmp/
docker exec odysseus-odysseus-1 python3 /tmp/seed_messages.py  # 20 msgs across 4 chats

# Trigger
curl -s -X PATCH -H 'Content-Type: application/json' -b "$COOKIE" \
  -d '{"enabled":true,"interval_minutes":30,"lookback_hours":24,"min_messages":1,"endpoint_id":"2f53cf74","model":"MiniMax-M3","prompt":""}' \
  http://localhost:7011/ext-api/whats2api_bridge/admin/analyzer

curl -s -X POST -b "$COOKIE" \
  http://localhost:7011/ext-api/whats2api_bridge/admin/analyzer/run-now

# Observe:
docker logs odysseus-odysseus-1 --since 1m | grep -iE "request_authority|DROPPED|disabled by"
# You will see:
# - "manage_X: Tool is disabled by the current request policy" (Finding 3 variant)
# - "DROPPED unoffered textual call(s): ['mcp__oxs_whats2api_bridge__list_messages']" (Finding 2)
```

---

## Estimativa de impacto

| Finding | Severidade | Esforço estimado | Quem toca |
|---|---|---|---|
| 1 — Authority vazia para LLM | alta | 0.5d (1 patch + tests) | agent_runtime/authority.py |
| 2 — MCP descriptions vazias | média | 0.5d (mcp_bridge.py + OXS host) | core/extensions/mcp_bridge.py + OXS host.py |
| 3 — `known_tool_names` deny-when-no-schema | média | 1d (refactor tool_policy + agent_loop) | tool_policy.py, agent_loop.py |
| 4 — `run_task_now` async/sync confusion | baixa | 0.25d (sync wrapper) | task_scheduler.py |

Total: ~2.5 dias para a versão limpa.

---

## Pontos de contato

- Extension: `~/Documents/odysseus-w2a/extensions/whats2api_bridge/`
- Smoke-tests reproduzindo bugs: `~/Documents/odysseus-w2a/tests/test_analyzer.py`
- Container live: `odysseus-odysseus-1` (docker compose em `~/Documents/odysseus/`)
- Logs audit: dentro do container em `/app/data/oxs/audit.log`

Se precisarem de mais detalhes sobre qualquer finding, é só pedir.
