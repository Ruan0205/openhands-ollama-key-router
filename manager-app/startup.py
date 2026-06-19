#!/usr/bin/env python3
import base64
import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_FILE = Path(os.environ.get("API_KEYS_FILE", "/data/api-keys.json"))
MODELS_FILE = Path(os.environ.get("FREE_MODELS_FILE", "/data/free-models.json"))
DIRECT_BASE = os.environ.get("DIRECT_CLOUD_BASE_URL", "https://ollama.com").rstrip("/")
QUOTA_LIMIT = int(float(os.environ.get("DEFAULT_QUOTA_LIMIT_TOKENS", "60000") or "60000"))
RESET_HOURS = float(os.environ.get("DEFAULT_QUOTA_RESET_HOURS", "3.1666666667") or "5")
RESET_SECONDS = max(1, int(RESET_HOURS * 3600))
RECHECK_SECONDS = max(60, int(float(os.environ.get("ACCOUNT_RECHECK_SECONDS", "300") or "300")))
PROBE_TIMEOUT = max(10, int(float(os.environ.get("MODEL_PROBE_TIMEOUT", "90") or "90")))
MODEL_PROBE_ON_START = os.environ.get("MODEL_PROBE_ON_START", "true").lower() in ("1", "true", "yes", "on")
MANAGER_USERNAME = os.environ.get("MANAGER_USERNAME", "admin")
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "change-me")
PORT = int(os.environ.get("PORT", "8080"))
_stop = threading.Event()
_child = None
_file_lock = threading.Lock()


def now():
    return int(time.time())


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def update_store(mutator):
    with _file_lock:
        store = read_json(DATA_FILE, {})
        if not isinstance(store, dict):
            store = {}
        changed = mutator(store)
        if changed:
            atomic_write(DATA_FILE, store)
        return store


def apply_quota_defaults():
    current = now()

    def mutate(store):
        changed = False
        keys = store.get("keys", [])
        if not isinstance(keys, list):
            return False
        recovered = False
        for item in keys:
            if not isinstance(item, dict):
                continue
            if int(item.get("quota_limit_tokens") or 0) != QUOTA_LIMIT:
                item["quota_limit_tokens"] = QUOTA_LIMIT
                changed = True
            if float(item.get("reset_period_hours") or 0) != RESET_HOURS:
                item["reset_period_hours"] = RESET_HOURS
                changed = True

            used = max(0, int(float(item.get("used_tokens") or 0)))
            reset_at = int(float(item.get("reset_at") or 0))
            if reset_at <= 0:
                base = int(float(item.get("last_used_at") or current))
                reset_at = base + RESET_SECONDS
                item["reset_at"] = reset_at
                changed = True

            if reset_at <= current:
                item["used_tokens"] = 0
                item["last_reset_at"] = current
                item["reset_at"] = current + RESET_SECONDS
                item["runtime_blocked"] = False
                item["runtime_blocked_reason"] = ""
                item["runtime_blocked_at"] = None
                item["last_status"] = "cota local renovada apos 3 horas e 10 minutos"
                changed = True
                recovered = True
            elif used >= QUOTA_LIMIT:
                if not item.get("runtime_blocked") or item.get("runtime_blocked_reason") != "local_quota_60000":
                    item["runtime_blocked"] = True
                    item["runtime_blocked_reason"] = "local_quota_60000"
                    item["runtime_blocked_at"] = item.get("runtime_blocked_at") or current
                    item["last_status"] = f"limite local de {QUOTA_LIMIT} tokens; aguardando reset"
                    changed = True

        if recovered and (store.get("wait_mode") or {}).get("enabled"):
            store["wait_mode"] = {
                "enabled": False,
                "cleared_at": current,
                "reason": "quota_reset_5h",
            }
            changed = True
        return changed

    return update_store(mutate)


def enabled_keys(include_depleted=False):
    store = read_json(DATA_FILE, {})
    result = []
    for item in store.get("keys", []) if isinstance(store, dict) else []:
        if not isinstance(item, dict) or not item.get("enabled", True) or not item.get("value"):
            continue
        used = max(0, int(float(item.get("used_tokens") or 0)))
        if not include_depleted and used >= QUOTA_LIMIT:
            continue
        result.append(item)
    return result


def request_json(method, url, key, payload=None, timeout=None):
    data = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout or PROBE_TIMEOUT) as response:
        raw = response.read().decode("utf-8", "replace")
        return json.loads(raw or "{}")


def model_test(model, key):
    return request_json(
        "POST",
        f"{DIRECT_BASE}/v1/chat/completions",
        key,
        {
            "model": model,
            "messages": [{"role": "user", "content": "Responda somente: OK"}],
            "stream": False,
            "max_tokens": 1,
            "temperature": 0,
            "reasoning_effort": "none",
        },
        PROBE_TIMEOUT,
    )


def cached_models():
    data = read_json(MODELS_FILE, {})
    if isinstance(data, list):
        return [str(x) for x in data if str(x).strip()]
    if isinstance(data, dict):
        return [str(x) for x in data.get("models", []) if str(x).strip()]
    return []


def save_models(models, catalog_count=0):
    models = sorted(set(models), key=lambda name: (
        0 if name == "qwen3.5" or name.startswith("qwen3.5:") else
        1 if "qwen" in name.lower() else
        2 if name == "gpt-oss:20b" else
        3,
        name.lower(),
    ))
    atomic_write(MODELS_FILE, {
        "updated_at": now(),
        "source": "startup_live_probe",
        "catalog_count": catalog_count,
        "models": models,
    })
    return models


def discover_models():
    keys = enabled_keys(include_depleted=False)
    if not keys:
        keys = enabled_keys(include_depleted=True)
    if not keys:
        print("[startup] nenhuma API key ativa para descobrir modelos", flush=True)
        return cached_models()

    catalog = None
    for item in keys:
        try:
            catalog = request_json("GET", f"{DIRECT_BASE}/v1/models", item["value"], timeout=30)
            break
        except Exception as exc:
            print(f"[startup] falha ao listar modelos com {item.get('name')}: {exc}", flush=True)
    if not isinstance(catalog, dict):
        print("[startup] usando cache de modelos porque o catalogo nao respondeu", flush=True)
        return cached_models()

    model_ids = sorted({
        str(item.get("id"))
        for item in catalog.get("data", [])
        if isinstance(item, dict) and item.get("id")
    })
    available = []
    key_index = 0
    print(f"[startup] testando {len(model_ids)} modelos cloud com saida maxima de 1 token", flush=True)

    for position, model in enumerate(model_ids, 1):
        success = False
        subscription_denied = False
        attempts = 0
        while attempts < len(keys):
            item = keys[key_index % len(keys)]
            key_index += 1
            attempts += 1
            try:
                model_test(model, item["value"])
                success = True
                print(f"[startup] [{position}/{len(model_ids)}] LIVRE {model} via {item.get('name')}", flush=True)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                lower = body.lower()
                if exc.code == 403 and "requires a subscription" in lower:
                    subscription_denied = True
                    print(f"[startup] [{position}/{len(model_ids)}] PAGO {model}", flush=True)
                    break
                if exc.code in (401, 403, 408, 409, 425, 429, 500, 502, 503, 504):
                    print(f"[startup] [{position}/{len(model_ids)}] {model}: {item.get('name')} retornou {exc.code}; tentando outra conta", flush=True)
                    continue
                print(f"[startup] [{position}/{len(model_ids)}] {model}: HTTP {exc.code}", flush=True)
                break
            except Exception as exc:
                print(f"[startup] [{position}/{len(model_ids)}] {model}: {item.get('name')} falhou: {exc}", flush=True)
                continue
        if success and not subscription_denied:
            available.append(model)

    if available:
        save_models(available, len(model_ids))
        print(f"[startup] modelos cloud gratuitos/ativos atualizados: {len(available)}", flush=True)
        return available

    old = cached_models()
    print(f"[startup] nenhum modelo confirmado; mantendo cache com {len(old)} modelos", flush=True)
    return old


def preferred_model(models):
    for wanted in ("qwen3.5", "qwen3.5:cloud", "gpt-oss:20b"):
        if wanted in models:
            return wanted
    qwen = next((m for m in models if m.startswith("qwen3.5")), None)
    return qwen or (models[0] if models else None)


def basic_auth_header():
    token = base64.b64encode(f"{MANAGER_USERNAME}:{MANAGER_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def manager_json(method, path, payload=None, timeout=30):
    data = json.dumps(payload or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=data,
        headers=basic_auth_header(),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace") or "{}")


def configure_model_after_discovery(models):
    model = preferred_model(models)
    if not model:
        return
    for _ in range(120):
        if _stop.is_set():
            return
        try:
            status = manager_json("GET", "/api/status")
            current = str((status.get("openhands_llm") or {}).get("model") or "")
            if current.startswith("openai/"):
                current = current.split("/", 1)[1]
            current = current.removesuffix(":cloud")
            normalized = {m.removesuffix(":cloud") for m in models}
            if current not in normalized:
                manager_json("POST", "/api/apply-openhands", {
                    "model": model,
                    "stream": False,
                }, timeout=60)
                print(f"[startup] OpenHands ajustado automaticamente para {model}", flush=True)
            manager_json("POST", "/api/clear-wait", {}, timeout=30)
            return
        except Exception:
            time.sleep(2)


def recover_one_key(item, models):
    if not models:
        return False
    for model in models[:5]:
        try:
            model_test(model, item["value"])
            return True
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").lower()
            if exc.code == 403 and "requires a subscription" in body:
                continue
            if exc.code in (401, 403, 429):
                return False
        except Exception:
            return False
    return False


def recovery_loop():
    while not _stop.wait(RECHECK_SECONDS):
        apply_quota_defaults()
        models = cached_models()
        store = read_json(DATA_FILE, {})
        candidates = []
        for item in store.get("keys", []) if isinstance(store, dict) else []:
            if not isinstance(item, dict) or not item.get("enabled", True) or not item.get("value"):
                continue
            used = max(0, int(float(item.get("used_tokens") or 0)))
            if item.get("runtime_blocked") or used >= QUOTA_LIMIT:
                candidates.append(dict(item))

        for candidate in candidates:
            if _stop.is_set():
                return
            if not recover_one_key(candidate, models):
                continue
            name = candidate.get("name")

            def mutate(store):
                changed = False
                for item in store.get("keys", []):
                    if item.get("name") != name:
                        continue
                    item["used_tokens"] = 0
                    item["last_reset_at"] = now()
                    item["reset_at"] = now() + RESET_SECONDS
                    item["runtime_blocked"] = False
                    item["runtime_blocked_reason"] = ""
                    item["runtime_blocked_at"] = None
                    item["last_status"] = "conta respondeu novamente; cota local reaberta"
                    changed = True
                if changed:
                    store["wait_mode"] = {
                        "enabled": False,
                        "cleared_at": now(),
                        "reason": "account_responded_again",
                    }
                return changed

            update_store(mutate)
            print(f"[startup] conta {name} respondeu novamente e foi reativada", flush=True)


def discovery_task():
    try:
        models = discover_models()
        configure_model_after_discovery(models)
    except Exception as exc:
        print(f"[startup] erro na descoberta de modelos: {exc}", flush=True)


def stop_handler(signum, frame):
    _stop.set()
    if _child and _child.poll() is None:
        _child.terminate()


def main():
    global _child
    apply_quota_defaults()

    old_env_models = [x.strip() for x in os.environ.get("FREE_MODELS", "").split(",") if x.strip()]
    if old_env_models and not cached_models():
        save_models(old_env_models)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    _child = subprocess.Popen(["python", "/app/app.py"], env=os.environ.copy())
    threading.Thread(target=recovery_loop, name="account-recovery", daemon=True).start()
    if MODEL_PROBE_ON_START:
        threading.Thread(target=discovery_task, name="model-discovery", daemon=True).start()

    code = _child.wait()
    _stop.set()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
