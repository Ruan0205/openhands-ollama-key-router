# OpenHands + Ollama Cloud Gateway

This stack runs a manager app, OpenHands, Open WebUI, and SearXNG around Ollama Cloud API keys or an external Ollama endpoint.

The final recommended architecture is **API-key routing**: the manager stores multiple official Ollama API keys, monitors usage returned by model responses, and routes each new OpenHands request to the best available key. The tested local machine may still keep an existing Windows Ollama installation for local models/status, but Cloud routing does not require replacing that installation.

## Official Research Summary

Sources checked:

- Ollama Cloud: https://docs.ollama.com/cloud
- Ollama API authentication: https://docs.ollama.com/api/authentication
- Ollama API base URLs: https://docs.ollama.com/api/introduction
- Ollama CLI: https://docs.ollama.com/cli
- Ollama Docker: https://docs.ollama.com/docker
- Open WebUI provider connections: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/
- Open WebUI with Ollama: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-ollama/
- Open WebUI API endpoints: https://docs.openwebui.com/reference/api-endpoints/

Findings:

- Ollama Cloud models can be used through a local Ollama installation after `ollama signin`.
- The same Ollama API can be used directly at `https://ollama.com/api` with an official Ollama API key.
- Ollama API responses expose per-request usage metrics such as prompt and output token counts.
- The checked official docs do not expose an API endpoint for account-wide remaining tokens, quota reset time, or total balance across multiple API keys.
- The local Ollama API does not require local API auth, but authenticated Cloud calls are handled by the signed-in Ollama installation.
- Open WebUI connects to an existing provider endpoint. It does not replace Ollama account login by itself.
- The official Docker image `ollama/ollama` is appropriate for a bundled local Ollama server. No separate official web UI/container was found that manages Ollama Cloud account login, account switching, quotas, revocation, and streaming for OpenHands as a complete managed Cloud client.
- Because of that, this repository does not define a fake `bundled-cloud` profile.

## Tested Local Mode

The tested local architecture is:

```text
Browser
  |
  v
Linux VM: Manager app + OpenHands + Open WebUI + SearXNG
  |
  v
Manager gateway `/llm/v1`
  |
  +--> Ollama Cloud direct API using API keys
  |
  +--> optional existing Windows Ollama installation for local/external mode
```

The Windows Ollama installation is not part of Docker Compose.

## Recommended Mode: Direct Cloud API Keys

```bash
cp .env.direct-cloud.example .env
docker compose --profile external-ollama up -d --build
```

Then open the manager UI and add one API key per account:

```text
http://YOUR_SERVER_HOST:3005
```

Set:

- `Modo`: `Ollama Cloud direto com API key`
- `fallback automático`: enabled
- `trocar em %`: usually `10`
- `limite de tokens`, `tokens usados agora`, and `reset em horas` for each key

The manager calls `https://ollama.com/v1` directly and presents an OpenAI-compatible gateway to OpenHands at `/llm/v1`.

This is now the default production path for this project: the Linux server runs
the manager, OpenHands, Open WebUI, and search, while the manager talks to
Ollama Cloud directly with stored API keys. A Windows Ollama installation is no
longer required for Cloud routing.

## Production Defaults

The manager image starts through `manager-entrypoint.sh`, which launches:

- `startup.py`: applies defaults, probes Cloud models on startup, and applies a
  working free model to OpenHands when needed;
- `quota_sync.py`: keeps the local quota ledger aligned with the configured
  reset window and periodically tests blocked/depleted accounts;
- `app.py`: serves the UI and the OpenAI-compatible `/llm/v1` gateway.

Current defaults:

- `UPSTREAM_MODE=direct_cloud`
- `DEFAULT_QUOTA_LIMIT_TOKENS=60000`
- `DEFAULT_QUOTA_RESET_HOURS=3.1666666667` (3 hours and 10 minutes)
- `ACCOUNT_RECHECK_SECONDS=300`
- `MODEL_PROBE_ON_START=true`
- `QUOTA_SYNC_ENABLED=true`
- `OPENHANDS_STREAM=false`

If an account reaches the local 60000-token ledger, the manager pauses that key
until the 3h10 window expires or until a periodic probe proves the account is
answering again. When an account is released, wait mode is cleared and the
router can use it again.

## Free Cloud Model Discovery

On startup, the manager lists `https://ollama.com/v1/models` with an active API
key and probes each catalog model with a tiny one-token completion. Models that
return subscription errors are treated as paid/unavailable for the current
accounts and are not shown in the manager model selector or `/llm/v1/models`.

The filtered catalog is cached in:

```text
/data/free-models.json
```

You can seed or override the list with `FREE_MODELS`, but the normal mode is to
let the startup probe refresh it automatically whenever the manager starts.

## Import, Export, And External Endpoint

The manager UI includes:

- `Exportar chaves TXT`: downloads the configured API keys as a text file;
- `Importar chaves TXT`: imports one key per line or `name=key` lines;
- external API controls: enables/disables a host/port style endpoint setting
  for other applications to discover where they should connect.

The external endpoint setting is stored in:

```text
/data/external-api.json
```

The API key store remains in:

```text
/data/api-keys.json
```

Do not commit files from `/data`; they contain private API keys and local state.

## Live Metrics

The UI refreshes usage without a manual page reload. It also shows recent
request activity and estimated tokens per second using data stored in:

```text
/data/live-metrics.json
```

The refresh interval defaults to `LIVE_METRICS_REFRESH_SECONDS=2`.

## Profiles

### `external-ollama`

Use this when Ollama already runs outside Docker, for example on a Windows machine, or when you want the same Compose profile but the manager route points to direct Cloud API keys:

```bash
cp .env.external-ollama.example .env
docker compose --profile external-ollama up -d --build
```

Set these values in `.env`:

- `OLLAMA_BASE_URL`, for example `http://WINDOWS_OLLAMA_HOST:11434`
- `OPENAI_API_BASE_URL`, for example `http://WINDOWS_OLLAMA_HOST:11434/v1`
- `LLM_BASE_URL`, usually the same OpenAI-compatible URL
- `LLM_MODEL`, for example `openai/nemotron-3-super:cloud`
- `LLM_BASE_URL`, normally `http://YOUR_SERVER_HOST:3005/llm/v1`, because OpenHands sandbox containers must be able to reach it
- `OPENHANDS_STREAM=false` is the stable default for OpenHands. The manager UI still has a separate streaming test for the LLM endpoint.
- `SANDBOX_VOLUMES`, using absolute host paths
- `OPENHANDS_PUBLIC_URL`
- `OPENWEBUI_PUBLIC_URL`
- `MANAGER_USERNAME` and `MANAGER_PASSWORD`
- `UPSTREAM_MODE=external_ollama` for the Windows Ollama flow, or `direct_cloud` when using Ollama API keys directly
- `OLLAMA_API_KEYS` optionally seeds multiple direct Cloud API keys; you can also add them later in the manager UI
- `SWITCH_THRESHOLD_PERCENT=10` switches away from a key after a completed request leaves it at or below 10 percent remaining
- `SANDBOX_CONTAINER_URL_PATTERN`

### `bundled-ollama`

Use this for local models inside the official `ollama/ollama` container:

```bash
cp .env.bundled-ollama.example .env
docker compose --profile bundled-ollama up -d --build
```

This profile is not documented as a complete Ollama Cloud account-login solution. Pull a local model inside the container before using it:

```bash
docker exec -it ollama ollama pull qwen3.5:8b
```

## Login Notes

For the tested local Windows mode, use the official Windows Ollama app/CLI:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" signin
```

This should use the normal desktop browser flow. If you need to sign in manually, open the login in your normal Chrome profile so saved email credentials are available. Do not copy cookies or session files into containers.

## Validation Commands

From the Linux host:

```bash
curl http://WINDOWS_OLLAMA_HOST:11434/api/version
curl http://WINDOWS_OLLAMA_HOST:11434/api/tags
curl http://WINDOWS_OLLAMA_HOST:11434/v1/models
```

Minimal streaming test:

```bash
curl http://WINDOWS_OLLAMA_HOST:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer ollama' \
  -d '{
    "model": "nemotron-3-super:cloud",
    "messages": [{"role": "user", "content": "Reply only: ok"}],
    "stream": true,
    "max_tokens": 8
  }'
```

Manager gateway test:

```bash
curl http://YOUR_SERVER_HOST:3005/llm/v1/models \
  -H 'Authorization: Bearer ollama'
```

Manager quota/status test:

```bash
curl -u admin:change-me http://YOUR_SERVER_HOST:3005/api/usage
curl -u admin:change-me http://YOUR_SERVER_HOST:3005/api/test-all-api-keys \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Manager UI:

```text
http://YOUR_SERVER_HOST:3005
```

The manager UI can refresh status, list local and Cloud models, test streaming,
apply the selected model to OpenHands, and manage multiple Ollama API keys for
direct Cloud routing. It also keeps a local quota ledger for API keys when you
configure token limits and reset windows manually.

## Multiple Accounts And API Keys

The manager does not automate multiple Ollama account logins because no official
multi-account/headless login flow was found for this use case. Instead, it
supports multiple official Ollama API keys:

- open the manager UI;
- add each key with a friendly name;
- choose `direct_cloud` routing when you want the gateway to call
  `https://ollama.com/v1` directly;
- keep `external_ollama` routing when you want to use the already signed-in
  Windows Ollama installation;
- keep fallback enabled when you want the gateway to choose the key with the
  largest estimated remaining token balance before each new request;
- test one key or all stored keys from the UI. The test calls the official
  model-list endpoint and stores the last OK/error state.

Keys are persisted in the manager volume at `/data/api-keys.json` and are shown
masked in the UI.

## Quota Ledger And Dynamic Fallback

Ollama currently documents usage metrics per response, but not an official
account-balance API. For that reason, the manager uses real per-request usage
from responses and combines it with the quota you configure manually:

1. Add each API key in the manager UI.
2. Fill in `limite de tokens`, `tokens usados agora`, and `reset em horas` for
   that key. Leave the quota fields blank only if you do not know them; those
   accounts will show as `desconhecido`.
3. Click `Salvar e testar chave`. The manager saves the key, tests it, and
   records the result.
4. In `direct_cloud` mode with fallback enabled, every new OpenHands request is
   routed to the enabled key with the highest estimated remaining token count.
5. After a completed response, if the current key reaches `SWITCH_THRESHOLD_PERCENT`
   or lower, the manager changes the active key to the next best account.
6. If a key returns provider errors such as `401`, `403`, `404`, `408`, `429`,
   or `5xx`, or if the upstream stops responding before a response is returned,
   the manager marks that key as temporarily blocked and retries the next
   available key with the same request body.
7. Non-streaming responses are counted using `usage.total_tokens` or Ollama
   native `prompt_eval_count + eval_count` metrics when present. Streaming
   responses are counted only when the upstream includes usage data in the
   stream.
8. If the whole key loop fails, the manager enters wait mode. In wait mode,
   `/llm/v1` returns `503` instead of burning retries forever. Use the manager UI
   to adjust keys/quotas/model and click `Aplicar no OpenHands`, `Salvar
   roteamento`, or `Sair do aguarde` to restart attempts.

OpenHands is kept on `stream=false` in the tested setup so the manager can count
usage reliably and fail over with minimal interruption. A request that is
already mid-generation cannot be moved to another account without restarting
that individual request.

## Manual Account Control

The manager UI includes:

- `Proxima conta agora`: immediately switches to the next enabled, non-blocked
  API key and writes a handoff file;
- `Sair do aguarde`: clears wait mode and temporary runtime key blocks;
- `Aplicar no OpenHands`: applies the selected Cloud/API-key model and clears
  wait mode;
- `Aplicar modelo local/externo`: the only button allowed to intentionally apply
  a local or external Ollama model. Normal Cloud operation does not fall back to
  local models automatically.

When `UPSTREAM_MODE=direct_cloud`, the model dropdown is populated from
`https://ollama.com/v1/models` using the active API key. Local Windows Ollama
models are not shown in that mode, which prevents accidental fallback to a local
model.

## Context Handoff

With API-key routing, the model provider is stateless and OpenHands owns the
conversation state. That means switching API keys does not normally erase the
current chat context: the next OpenHands request is sent through the gateway
with the same OpenHands-managed conversation.

As a fallback, the manager writes a Markdown handoff file whenever it switches
keys due to threshold or provider errors:

```text
/data/handoffs/handoff-*.md
```

The handoff includes:

- reason for the switch;
- previous key and next key;
- model name;
- tokens counted for the completed request;
- recent messages from the request payload;
- captured response text when available.

You can view the latest handoff from the manager UI with `Ver handoff`, or via:

```bash
curl -u admin:change-me http://YOUR_SERVER_HOST:3005/api/handoff
```

This is the fallback path for manual recovery if OpenHands is interrupted.

## Compatibility Matrix

| Feature | External Ollama on Windows | Local Ollama in container | Cloud client in container |
| --- | ---: | ---: | ---: |
| Local models | Yes, if present in Windows Ollama | Yes | No profile created |
| Cloud models | Yes, when Windows Ollama is signed in | Not claimed | No profile created |
| Account login | Yes, via official Ollama app/CLI | Not claimed | No official complete component found |
| Multiple accounts | API keys through manager | API keys through manager | No official complete component found |
| Multiple API keys | Yes, via manager UI | Yes, via manager UI | No profile created |
| Streaming | Yes, tested through the manager gateway | Yes for local Ollama API | No profile created |
| API for OpenHands | Yes, tested through the manager gateway | Yes for local models | No profile created |
| Quota lookup | Local estimate in manager, not official account balance | Local estimate in manager | No profile created |
| Automatic account switching | Yes, API-key routing with threshold | Yes, if direct Cloud keys are used | No profile created |
| Context handoff | Yes, Markdown fallback | Yes, Markdown fallback | No profile created |

## Security Notes

- Do not commit `.env`, `htpasswd`, SSH keys, API keys, cookies, or session files.
- Expose the Windows Ollama port only to the Linux host, VPN, or tunnel that needs it.
- Keep `SANDBOX_VOLUMES` pointed at a dedicated SSH directory for OpenHands.
- Open WebUI is a provider UI; it should receive provider URLs and keys, not raw account passwords.

## Local Test Result

The tested local setup used an authenticated external Windows Ollama endpoint and the free Cloud model `nemotron-3-super:cloud`. The manager gateway successfully streamed a direct test request. OpenHands is intentionally configured with `stream=false` for stability while still using the manager gateway.

OpenHands successfully:

- loaded `openai/nemotron-3-super:cloud`;
- connected through the manager gateway and the OpenAI-compatible Ollama endpoint;
- completed a real conversation through `http://YOUR_SERVER_HOST:3005/llm/v1`;
- returned `ok`;
- finished without agent errors.
