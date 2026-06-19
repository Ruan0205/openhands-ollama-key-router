#!/usr/bin/env python3

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


STORE_FILE = Path(os.getenv("API_KEYS_FILE", "/data/api-keys.json"))
MODELS_FILE = Path(os.getenv("FREE_MODELS_FILE", "/data/free-models.json"))
DIRECT_BASE = os.getenv("DIRECT_CLOUD_BASE_URL", "https://ollama.com").rstrip("/")

QUOTA_LIMIT = int(os.getenv("DEFAULT_QUOTA_LIMIT_TOKENS", "60000"))
RESET_HOURS = float(os.getenv("DEFAULT_QUOTA_RESET_HOURS", "3.1666666667"))
RESET_SECONDS = int(RESET_HOURS * 3600)
RECHECK_SECONDS = max(60, int(os.getenv("ACCOUNT_RECHECK_SECONDS", "300")))
MODEL_TEST_LIMIT = max(1, int(os.getenv("QUOTA_SYNC_MODEL_LIMIT", "12")))

ENABLED = os.getenv("QUOTA_SYNC_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}


def log(message):
    print(
        f"[quota-sync {time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True,
    )


def to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def quota_used(item):
    return to_int(item.get("effective_used_tokens", item.get("used_tokens")), 0)


def load_store():
    if not STORE_FILE.exists():
        return None

    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"erro lendo {STORE_FILE}: {exc}")
        return None


def save_store(store):
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STORE_FILE.with_suffix(".quota-sync.tmp")
    temporary.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, STORE_FILE)


def normalize_model_name(model):
    model = str(model or "").strip()

    if model.startswith("openai/"):
        model = model.split("/", 1)[1]

    if model.endswith(":cloud"):
        model = model[:-6]

    if model.endswith("-cloud"):
        model = model[:-6]

    return model


def extract_models(value, output):
    if isinstance(value, str):
        for part in value.split(","):
            model = normalize_model_name(part)
            if model and model not in output:
                output.append(model)
        return

    if isinstance(value, list):
        for item in value:
            extract_models(item, output)
        return

    if not isinstance(value, dict):
        return

    for field in ("id", "name", "model", "remote_model"):
        if isinstance(value.get(field), str):
            model = normalize_model_name(value[field])
            if model and model not in output:
                output.append(model)

    for field in ("models", "data", "free_models", "available_models"):
        if field in value:
            extract_models(value[field], output)


def local_models():
    models = []

    if MODELS_FILE.exists():
        try:
            extract_models(
                json.loads(MODELS_FILE.read_text(encoding="utf-8")),
                models,
            )
        except Exception as exc:
            log(f"não consegui ler {MODELS_FILE}: {exc}")

    extract_models(os.getenv("FREE_MODELS", ""), models)

    def priority(model):
        lowered = model.lower()

        if lowered == "qwen3.5":
            return (0, lowered)
        if lowered.startswith("qwen3.5"):
            return (1, lowered)
        if lowered.startswith("qwen"):
            return (2, lowered)
        if lowered.startswith("gpt-oss:20b"):
            return (3, lowered)
        if "gemma" in lowered:
            return (4, lowered)

        return (10, lowered)

    return sorted(models, key=priority)


def upstream_models(api_key):
    request = urllib.request.Request(
        f"{DIRECT_BASE}/v1/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))

    models = []
    extract_models(payload.get("data", []), models)
    return models


def get_probe_models(api_key):
    models = local_models()

    if not models:
        try:
            models = upstream_models(api_key)
        except Exception as exc:
            log(f"não foi possível consultar modelos para sincronização: {exc}")
            return []

    return models[:MODEL_TEST_LIMIT]


def probe_account(api_key):
    models = get_probe_models(api_key)

    if not models:
        return False, None, "nenhum modelo disponível para teste"

    last_error = ""

    for model in models:
        payload = json.dumps({
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Responda somente: OK",
                }
            ],
            "stream": False,
            "max_tokens": 1,
            "temperature": 0,
        }).encode("utf-8")

        request = urllib.request.Request(
            f"{DIRECT_BASE}/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                response.read()

            return True, model, "upstream respondeu com sucesso"

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            lowered = body.lower()
            last_error = f"HTTP {exc.code}: {body[:240]}"

            # Este modelo é pago para a conta, mas outro modelo pode funcionar.
            if exc.code == 403 and (
                "requires a subscription" in lowered
                or "upgrade for access" in lowered
            ):
                continue

            # Conta ainda sem uso disponível, sobrecarregada ou limitada.
            if exc.code in (402, 403, 408, 409, 425, 429, 500, 502, 503, 504):
                return False, model, last_error

            if exc.code == 401:
                return False, model, "API key não autorizada"

        except Exception as exc:
            last_error = str(exc)

    return False, None, last_error or "nenhum modelo respondeu"


def clear_key_block(item, now, reason):
    item["used_tokens"] = 0
    item["effective_used_tokens"] = 0
    item["model_usage"] = {}
    item["runtime_blocked"] = False
    item["runtime_blocked_reason"] = ""
    item["runtime_blocked_at"] = None
    item["last_reset_at"] = now
    item["reset_period_hours"] = RESET_HOURS
    item["reset_at"] = now + RESET_SECONDS
    item["last_status"] = reason


def synchronize():
    store = load_store()

    if not store:
        return

    now = int(time.time())
    changed = False
    released_accounts = []

    keys = store.get("keys", [])

    for item in keys:
        if not isinstance(item, dict):
            continue

        previous_period = float(item.get("reset_period_hours") or 0)
        used = quota_used(item)
        blocked = bool(item.get("runtime_blocked"))
        enabled = bool(item.get("enabled", True))

        if item.get("quota_limit_tokens") != QUOTA_LIMIT:
            item["quota_limit_tokens"] = QUOTA_LIMIT
            changed = True

        if abs(previous_period - RESET_HOURS) > 0.001:
            item["reset_period_hours"] = RESET_HOURS
            changed = True

        if used > 0 or blocked:
            base = (
                to_int(item.get("last_used_at"), 0)
                or to_int(item.get("runtime_blocked_at"), 0)
                or now
            )

            calculated_reset = base + RESET_SECONDS
            current_reset = to_int(item.get("reset_at"), 0)

            if (
                not current_reset
                or abs(previous_period - RESET_HOURS) > 0.001
                or current_reset > calculated_reset + 60
            ):
                item["reset_at"] = calculated_reset
                current_reset = calculated_reset
                changed = True

            # Reset local obrigatório depois de 3h10.
            if current_reset <= now:
                clear_key_block(
                    item,
                    now,
                    "reset local após 3h10; aguardando confirmação no próximo uso",
                )
                released_accounts.append(item.get("name"))
                changed = True
                continue

        depleted = used >= QUOTA_LIMIT

        # Antes das 3h10, testa periodicamente se o Ollama já liberou.
        if enabled and (blocked or depleted):
            last_probe = to_int(item.get("last_sync_probe_at"), 0)

            if now - last_probe >= RECHECK_SECONDS:
                item["last_sync_probe_at"] = now
                changed = True

                ok, model, reason = probe_account(str(item.get("value") or ""))

                item["last_sync_probe_model"] = model
                item["last_sync_probe_result"] = reason
                item["last_sync_probe_ok"] = ok

                if ok:
                    clear_key_block(
                        item,
                        now,
                        f"sincronizada com Ollama pelo modelo {model}",
                    )
                    item["last_sync_at"] = now
                    released_accounts.append(item.get("name"))
                    changed = True
                    log(
                        f"conta {item.get('name')} liberada antecipadamente "
                        f"pelo modelo {model}"
                    )
                else:
                    item["last_status"] = (
                        f"aguardando Ollama: {str(reason)[:180]}"
                    )
                    changed = True

    available = [
        item
        for item in keys
        if isinstance(item, dict)
        and item.get("enabled", True)
        and not item.get("runtime_blocked")
        and quota_used(item)
        < to_int(item.get("quota_limit_tokens"), QUOTA_LIMIT)
    ]

    if available:
        active = store.get("active_key")
        active_item = next(
            (item for item in keys if item.get("name") == active),
            None,
        )

        if (
            not active_item
            or active_item.get("runtime_blocked")
            or quota_used(active_item) >= QUOTA_LIMIT
        ):
            store["active_key"] = available[0].get("name")
            changed = True

        wait_mode = store.get("wait_mode") or {}

        if wait_mode.get("enabled"):
            store["wait_mode"] = {
                "enabled": False,
                "cleared_at": now,
                "reason": "quota_sync_account_available",
            }
            changed = True

    if changed:
        save_store(store)

    if released_accounts:
        log(
            "contas disponíveis: "
            + ", ".join(str(name) for name in released_accounts if name)
        )


def main():
    if not ENABLED:
        log("sincronização desativada")
        return

    log(
        f"iniciada: cota={QUOTA_LIMIT}, reset={RESET_SECONDS}s "
        f"({RESET_HOURS:.4f}h), verificação={RECHECK_SECONDS}s"
    )

    while True:
        try:
            synchronize()
        except Exception as exc:
            log(f"erro no ciclo: {exc}")

        time.sleep(RECHECK_SECONDS)


if __name__ == "__main__":
    main()
