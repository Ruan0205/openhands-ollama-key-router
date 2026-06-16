#!/usr/bin/env python3
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def env(name, default=""):
    return os.environ.get(name, default)


OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
DIRECT_CLOUD_BASE_URL = env("DIRECT_CLOUD_BASE_URL", "https://ollama.com").rstrip("/")
OPENHANDS_API_URL = env("OPENHANDS_API_URL", "http://openhands-app:3000").rstrip("/")
OPENHANDS_PUBLIC_URL = env("OPENHANDS_PUBLIC_URL", "")
OPENWEBUI_PUBLIC_URL = env("OPENWEBUI_PUBLIC_URL", "")
SEARXNG_URL = env("SEARXNG_URL", "http://openhands-search:8080").rstrip("/")
DEFAULT_MODEL = env("DEFAULT_MODEL", "nemotron-3-super:cloud")
OPENHANDS_LLM_BASE_URL = env("OPENHANDS_LLM_BASE_URL", "http://ollama-manager:8080/llm/v1").rstrip("/")
LLM_API_KEY = env("LLM_API_KEY", "ollama")
OPENHANDS_STREAM = env("OPENHANDS_STREAM", "false").lower() in ("1", "true", "yes", "on")
MANAGER_USERNAME = env("MANAGER_USERNAME", "admin")
MANAGER_PASSWORD = env("MANAGER_PASSWORD", "change-me")
PROXY_API_KEY = env("PROXY_API_KEY", LLM_API_KEY)
REQUEST_TIMEOUT = float(env("REQUEST_TIMEOUT", "30"))
API_KEYS_FILE = env("API_KEYS_FILE", "/data/api-keys.json")
DEFAULT_QUOTA_RESET_HOURS = float(env("DEFAULT_QUOTA_RESET_HOURS", "0") or "0")
SWITCH_THRESHOLD_PERCENT = float(env("SWITCH_THRESHOLD_PERCENT", "10") or "10")
HANDOFF_DIR = env("HANDOFF_DIR", "/data/handoffs")
EVENT_LOG_FILE = env("EVENT_LOG_FILE", "/data/events.jsonl")
MAX_HANDOFF_CHARS = int(env("MAX_HANDOFF_CHARS", "12000") or "12000")


def parse_env_api_keys():
    raw = env("OLLAMA_API_KEYS", "").strip()
    if not raw:
        single = env("OLLAMA_API_KEY", "").strip()
        return [{"name": "default", "value": single, "enabled": True}] if single else []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return [
                {"name": str(name), "value": str(value), "enabled": True}
                for name, value in loaded.items()
                if str(value).strip()
            ]
        if isinstance(loaded, list):
            out = []
            for idx, item in enumerate(loaded, start=1):
                if isinstance(item, dict):
                    value = str(item.get("value") or item.get("key") or "").strip()
                    name = str(item.get("name") or f"key-{idx}")
                    enabled = bool(item.get("enabled", True))
                else:
                    value = str(item).strip()
                    name = f"key-{idx}"
                    enabled = True
                if value:
                    out.append({"name": name, "value": value, "enabled": enabled})
            return out
    except json.JSONDecodeError:
        pass
    out = []
    for idx, value in enumerate([v.strip() for v in raw.split(",") if v.strip()], start=1):
        out.append({"name": f"key-{idx}", "value": value, "enabled": True})
    return out


def now_ts():
    return int(time.time())


def to_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def normalize_reset_at(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def iso_time(ts):
    ts = normalize_reset_at(ts)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def normalize_key_item(item, existing=None):
    existing = existing or {}
    value = str(item.get("value") or item.get("key") or existing.get("value") or "").strip()
    name = str(item.get("name") or existing.get("name") or "").strip()
    changed_value = bool(existing.get("value")) and value != existing.get("value")
    reset_period_hours = to_float(
        item.get("reset_period_hours", existing.get("reset_period_hours", DEFAULT_QUOTA_RESET_HOURS)),
        DEFAULT_QUOTA_RESET_HOURS,
    )
    reset_at = normalize_reset_at(item.get("reset_at", existing.get("reset_at")))
    quota_limit_tokens = to_int(item.get("quota_limit_tokens", existing.get("quota_limit_tokens", 0)))
    if not reset_at and quota_limit_tokens > 0 and reset_period_hours > 0:
        reset_at = now_ts() + int(reset_period_hours * 3600)
    if item.get("used_tokens") is not None:
        used_tokens = to_int(item.get("used_tokens"), 0)
    else:
        used_tokens = 0 if changed_value else to_int(existing.get("used_tokens", 0))
    return {
        "name": name,
        "value": value,
        "enabled": bool(item.get("enabled", existing.get("enabled", True))),
        "quota_limit_tokens": quota_limit_tokens,
        "used_tokens": used_tokens,
        "reset_period_hours": reset_period_hours,
        "reset_at": reset_at,
        "last_reset_at": normalize_reset_at(item.get("last_reset_at", existing.get("last_reset_at"))),
        "last_used_at": normalize_reset_at(item.get("last_used_at", existing.get("last_used_at"))),
        "last_test_at": normalize_reset_at(item.get("last_test_at", existing.get("last_test_at"))),
        "last_test_ok": item.get("last_test_ok", existing.get("last_test_ok")),
        "last_test_error": str(item.get("last_test_error", existing.get("last_test_error", "")))[:1000],
        "last_test_model_count": to_int(item.get("last_test_model_count", existing.get("last_test_model_count", 0))),
        "last_status": str(item.get("last_status", existing.get("last_status", "")))[:120],
    }


def normalize_store(store):
    store = dict(store or {})
    raw_keys = store.get("keys", [])
    existing_by_name = {
        str(item.get("name", "")).strip(): item
        for item in raw_keys
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    keys = []
    for item in raw_keys:
        if not isinstance(item, dict):
            continue
        normalized = normalize_key_item(item, existing_by_name.get(str(item.get("name", "")).strip()))
        if normalized["name"] and normalized["value"]:
            keys.append(normalized)
    active_key = store.get("active_key")
    if active_key and not any(item["name"] == active_key for item in keys):
        active_key = None
    if not active_key and keys:
        active_key = keys[0]["name"]
    mode = store.get("upstream_mode", "external_ollama")
    if mode not in ("external_ollama", "direct_cloud"):
        mode = "external_ollama"
    return {
        "upstream_mode": mode,
        "active_key": active_key,
        "auto_fallback": bool(store.get("auto_fallback", False)),
        "switch_threshold_percent": to_float(store.get("switch_threshold_percent", SWITCH_THRESHOLD_PERCENT), SWITCH_THRESHOLD_PERCENT),
        "last_switch": store.get("last_switch") or {},
        "keys": keys,
    }


def apply_due_resets(store):
    changed = False
    current = now_ts()
    for item in store.get("keys", []):
        reset_at = normalize_reset_at(item.get("reset_at"))
        if reset_at and reset_at <= current:
            item["used_tokens"] = 0
            item["last_reset_at"] = current
            period = to_float(item.get("reset_period_hours"), 0.0)
            item["reset_at"] = current + int(period * 3600) if period > 0 else None
            item["last_status"] = "usage reset"
            changed = True
    return changed


def remaining_tokens(item):
    limit = to_int(item.get("quota_limit_tokens"), 0)
    if limit <= 0:
        return None
    return max(0, limit - to_int(item.get("used_tokens"), 0))


def remaining_percent(item):
    limit = to_int(item.get("quota_limit_tokens"), 0)
    remaining = remaining_tokens(item)
    if limit <= 0 or remaining is None:
        return None
    return round((remaining / limit) * 100, 4)


def quota_status(item, threshold_percent=None):
    threshold = SWITCH_THRESHOLD_PERCENT if threshold_percent is None else to_float(threshold_percent, SWITCH_THRESHOLD_PERCENT)
    remaining = remaining_tokens(item)
    percent = remaining_percent(item)
    if remaining is None or percent is None:
        return "unknown"
    if remaining <= 0:
        return "depleted"
    if percent <= threshold:
        return "low"
    return "healthy"


def seconds_to_reset(item):
    reset_at = normalize_reset_at(item.get("reset_at"))
    if not reset_at:
        return None
    return max(0, reset_at - now_ts())


def default_key_store():
    keys = parse_env_api_keys()
    return normalize_store({
        "upstream_mode": env("UPSTREAM_MODE", "external_ollama"),
        "active_key": keys[0]["name"] if keys else None,
        "auto_fallback": env("API_KEY_AUTO_FALLBACK", "false").lower() in ("1", "true", "yes", "on"),
        "keys": keys,
    })


def load_key_store():
    path = Path(API_KEYS_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = normalize_store(data)
            if apply_due_resets(data):
                save_key_store(data)
            return data
        except Exception:
            return default_key_store()
    store = default_key_store()
    if store["keys"]:
        save_key_store(store)
    return store


def save_key_store(store):
    path = Path(API_KEYS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = normalize_store(store)
    path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return safe


def mask_key(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "..." + value[-2:]
    return value[:6] + "..." + value[-4:]


def key_fingerprint(value):
    value = str(value or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def public_key_item(item, threshold_percent=None):
    remaining = remaining_tokens(item)
    reset_seconds = seconds_to_reset(item)
    percent = remaining_percent(item)
    return {
        "name": item.get("name"),
        "enabled": bool(item.get("enabled", True)),
        "masked": mask_key(item.get("value")),
        "fingerprint": key_fingerprint(item.get("value")),
        "quota_limit_tokens": to_int(item.get("quota_limit_tokens"), 0),
        "used_tokens": to_int(item.get("used_tokens"), 0),
        "remaining_tokens": remaining,
        "remaining_percent": percent,
        "quota_status": quota_status(item, threshold_percent),
        "reset_period_hours": to_float(item.get("reset_period_hours"), 0.0),
        "reset_at": normalize_reset_at(item.get("reset_at")),
        "reset_at_iso": iso_time(item.get("reset_at")),
        "seconds_to_reset": reset_seconds,
        "last_reset_at": normalize_reset_at(item.get("last_reset_at")),
        "last_used_at": normalize_reset_at(item.get("last_used_at")),
        "last_test_at": normalize_reset_at(item.get("last_test_at")),
        "last_test_ok": item.get("last_test_ok"),
        "last_test_error": item.get("last_test_error") or "",
        "last_test_model_count": to_int(item.get("last_test_model_count"), 0),
        "last_status": item.get("last_status") or "",
    }


def public_key_store():
    store = load_key_store()
    threshold = to_float(store.get("switch_threshold_percent"), SWITCH_THRESHOLD_PERCENT)
    keys = [public_key_item(item, threshold) for item in store.get("keys", [])]
    known_quota = [item for item in keys if item["remaining_tokens"] is not None]
    return {
        "upstream_mode": store.get("upstream_mode", "external_ollama"),
        "active_key": store.get("active_key"),
        "auto_fallback": bool(store.get("auto_fallback", False)),
        "switch_threshold_percent": threshold,
        "last_switch": store.get("last_switch") or {},
        "keys": keys,
        "totals": {
            "total_known_quota_tokens": sum(item["quota_limit_tokens"] for item in known_quota),
            "total_used_tokens": sum(item["used_tokens"] for item in keys),
            "total_remaining_tokens": sum(item["remaining_tokens"] for item in known_quota),
            "known_quota_accounts": len(known_quota),
            "unknown_quota_accounts": len(keys) - len(known_quota),
            "enabled_accounts": len([item for item in keys if item["enabled"]]),
            "healthy_accounts": len([item for item in keys if item["enabled"] and item["quota_status"] == "healthy"]),
            "low_accounts": len([item for item in keys if item["enabled"] and item["quota_status"] == "low"]),
            "depleted_accounts": len([item for item in keys if item["enabled"] and item["quota_status"] == "depleted"]),
        },
        "quota_source": "local_estimate",
        "quota_note": "A API publica uso por resposta, mas não expõe oficialmente saldo/reset total da conta. O manager calcula saldo a partir dos limites configurados por chave.",
    }


def get_key(name):
    store = load_key_store()
    for item in store.get("keys", []):
        if item.get("name") == name and item.get("enabled", True):
            return item
    return None


def ordered_keys_by_quota(keys, active_key=None, threshold_percent=None):
    keys = list(keys)
    if not keys:
        return []
    has_known_quota = any(remaining_tokens(item) is not None for item in keys)
    if not has_known_quota:
        return [item for item in keys if item.get("name") == active_key] + [
            item for item in keys if item.get("name") != active_key
        ]

    def score(item):
        remaining = remaining_tokens(item)
        active_bonus = 1 if item.get("name") == active_key else 0
        status = quota_status(item, threshold_percent)
        status_score = {"healthy": 3, "unknown": 2, "low": 1, "depleted": 0}.get(status, 0)
        if remaining is None:
            return (status_score, 0, active_bonus)
        return (status_score, remaining, active_bonus)

    return sorted(keys, key=score, reverse=True)


def active_api_keys():
    store = load_key_store()
    keys = [k for k in store.get("keys", []) if k.get("enabled", True)]
    active = store.get("active_key")
    threshold = store.get("switch_threshold_percent", SWITCH_THRESHOLD_PERCENT)
    if store.get("auto_fallback", False):
        return ordered_keys_by_quota(keys, active, threshold)
    ordered = [item for item in keys if item.get("name") == active]
    return ordered or ordered_keys_by_quota(keys, active, threshold)[:1]


def extract_usage_tokens_from_obj(obj):
    if not isinstance(obj, dict):
        return 0
    usage = obj.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if total is not None:
            return to_int(total, 0)
        return to_int(usage.get("prompt_tokens"), 0) + to_int(usage.get("completion_tokens"), 0)
    native_total = to_int(obj.get("prompt_eval_count"), 0) + to_int(obj.get("eval_count"), 0)
    if native_total:
        return native_total
    if isinstance(obj.get("response"), dict):
        return extract_usage_tokens_from_obj(obj["response"])
    return 0


def extract_usage_tokens(raw_body, content_type=""):
    if not raw_body:
        return 0
    text = raw_body.decode("utf-8", "ignore") if isinstance(raw_body, (bytes, bytearray)) else str(raw_body)
    total = 0
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                total += extract_usage_tokens_from_obj(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
        return total
    try:
        return extract_usage_tokens_from_obj(json.loads(text))
    except json.JSONDecodeError:
        return 0


def record_key_usage(name, tokens):
    tokens = to_int(tokens, 0)
    if not name or tokens <= 0:
        return public_key_store()
    store = load_key_store()
    for item in store.get("keys", []):
        if item.get("name") == name:
            item["used_tokens"] = to_int(item.get("used_tokens"), 0) + tokens
            item["last_used_at"] = now_ts()
            item["last_status"] = f"used {tokens} tokens"
            save_key_store(store)
            return public_key_store()
    return public_key_store()


def update_key_test_status(name, ok, error="", model_count=0):
    store = load_key_store()
    for item in store.get("keys", []):
        if item.get("name") == name:
            item["last_test_at"] = now_ts()
            item["last_test_ok"] = bool(ok)
            item["last_test_error"] = str(error or "")[:1000]
            item["last_test_model_count"] = to_int(model_count, 0)
            item["last_status"] = "test ok" if ok else "test failed"
            save_key_store(store)
            break


def reset_usage(name=None):
    store = load_key_store()
    current = now_ts()
    for item in store.get("keys", []):
        if name and item.get("name") != name:
            continue
        item["used_tokens"] = 0
        item["last_reset_at"] = current
        period = to_float(item.get("reset_period_hours"), 0.0)
        if period > 0:
            item["reset_at"] = current + int(period * 3600)
        item["last_status"] = "manual usage reset"
    save_key_store(store)
    return public_key_store()


def normalize_direct_model_name(model):
    model = str(model or "")
    if model.startswith("openai/"):
        model = model.split("/", 1)[1]
    if model.endswith(":cloud"):
        model = model[:-6]
    return model


def prepare_direct_cloud_body(body, content_type=""):
    if not body or "json" not in str(content_type).lower():
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    changed = False
    if isinstance(payload, dict) and payload.get("model"):
        normalized = normalize_direct_model_name(payload.get("model"))
        if normalized != payload.get("model"):
            payload["model"] = normalized
            changed = True
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def clip_text(value, limit=4000):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def parse_chat_payload(body):
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {"raw": clip_text(body.decode("utf-8", "replace"), 2000)}
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        payload = dict(payload)
        payload["messages"] = messages[-12:]
    return payload if isinstance(payload, dict) else {"raw": clip_text(payload, 2000)}


def extract_response_text(response_body):
    if not response_body:
        return ""
    try:
        obj = json.loads(response_body.decode("utf-8", "replace"))
    except Exception:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    parts = []
    if message.get("content"):
        parts.append(str(message.get("content")))
    if message.get("reasoning"):
        parts.append("Reasoning:\n" + str(message.get("reasoning")))
    return "\n\n".join(parts)


def store_event(event):
    event = dict(event)
    event.setdefault("created_at", now_ts())
    path = Path(EVENT_LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def recent_events(limit=20):
    path = Path(EVENT_LOG_FILE)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def key_snapshot(name, store=None):
    store = store or load_key_store()
    threshold = store.get("switch_threshold_percent", SWITCH_THRESHOLD_PERCENT)
    for item in store.get("keys", []):
        if item.get("name") == name:
            return public_key_item(item, threshold)
    return None


def next_key_name(store, previous_name=None):
    keys = [k for k in store.get("keys", []) if k.get("enabled", True) and k.get("name") != previous_name]
    ordered = ordered_keys_by_quota(keys, store.get("active_key"), store.get("switch_threshold_percent", SWITCH_THRESHOLD_PERCENT))
    return ordered[0].get("name") if ordered else None


def set_active_key(name, reason, previous_key=None, handoff_path=None):
    if not name:
        return public_key_store()
    store = load_key_store()
    old = previous_key or store.get("active_key")
    store["active_key"] = name
    store["last_switch"] = {
        "from": old,
        "to": name,
        "reason": reason,
        "at": now_ts(),
        "handoff_path": handoff_path,
    }
    save_key_store(store)
    return public_key_store()


def write_handoff(reason, previous_key, next_key, request_body=None, response_body=None, tokens=0, error=None):
    payload = parse_chat_payload(request_body)
    response_text = extract_response_text(response_body)
    model = payload.get("model") or DEFAULT_MODEL
    filename = f"handoff-{now_ts()}-{str(previous_key or 'none').replace(' ', '_')}-to-{str(next_key or 'none').replace(' ', '_')}.md"
    path = Path(HANDOFF_DIR) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    messages = payload.get("messages") or []
    lines = [
        "# OpenHands Handoff",
        "",
        f"- Criado em: {iso_time(now_ts())}",
        f"- Motivo: {reason}",
        f"- Modelo: `{model}`",
        f"- Key anterior: `{previous_key or ''}`",
        f"- Próxima key: `{next_key or ''}`",
        f"- Tokens contabilizados nesta chamada: `{tokens}`",
        "",
        "## Como continuar",
        "",
        "O OpenHands normalmente preserva o contexto por conta própria, porque o gateway troca apenas a API key do provedor.",
        "Use este arquivo como fallback caso uma conversa precise ser retomada manualmente depois de limite, erro ou reinício.",
        "",
    ]
    if error:
        lines.extend(["## Erro", "", "```text", clip_text(error, 3000), "```", ""])
    if messages:
        lines.extend(["## Últimas mensagens enviadas ao modelo", ""])
        for idx, message in enumerate(messages, start=1):
            role = message.get("role", "unknown") if isinstance(message, dict) else "unknown"
            content = message.get("content", "") if isinstance(message, dict) else message
            lines.extend([f"### {idx}. {role}", "", "```text", clip_text(content, 2500), "```", ""])
    if response_text:
        lines.extend(["## Última resposta capturada", "", "```text", clip_text(response_text, 4000), "```", ""])
    text = "\n".join(lines)
    if len(text) > MAX_HANDOFF_CHARS:
        text = text[:MAX_HANDOFF_CHARS] + "\n\n...[handoff truncated]\n"
    path.write_text(text, encoding="utf-8")
    event = store_event({
        "type": "handoff",
        "reason": reason,
        "previous_key": previous_key,
        "next_key": next_key,
        "tokens": tokens,
        "model": model,
        "path": str(path),
    })
    return str(path), event


def maybe_switch_after_usage(key_name, request_body=None, response_body=None, tokens=0):
    store = load_key_store()
    threshold = store.get("switch_threshold_percent", SWITCH_THRESHOLD_PERCENT)
    current = next((item for item in store.get("keys", []) if item.get("name") == key_name), None)
    if not current or quota_status(current, threshold) not in ("low", "depleted"):
        return public_key_store()
    nxt = next_key_name(store, previous_name=key_name)
    if not nxt or nxt == key_name:
        return public_key_store()
    handoff_path, _ = write_handoff(
        "threshold_after_completed_request",
        key_name,
        nxt,
        request_body=request_body,
        response_body=response_body,
        tokens=tokens,
    )
    return set_active_key(nxt, "threshold_after_completed_request", previous_key=key_name, handoff_path=handoff_path)


def latest_handoff():
    path = Path(HANDOFF_DIR)
    files = sorted(path.glob("handoff-*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if path.exists() else []
    if not files:
        return {"ok": False, "error": "Nenhum handoff gerado ainda.", "events": recent_events()}
    latest = files[0]
    return {
        "ok": True,
        "path": str(latest),
        "content": latest.read_text(encoding="utf-8", errors="replace"),
        "events": recent_events(),
    }


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if not length:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def http_json(method, url, payload=None, headers=None, timeout=REQUEST_TIMEOUT):
    headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        if not raw:
            return None
        return json.loads(raw)


def classify_model(model):
    name = model.get("name") or model.get("model") or model.get("id") or ""
    return {
        "name": name,
        "remote_model": model.get("remote_model"),
        "remote_host": model.get("remote_host"),
        "context_length": (model.get("details") or {}).get("context_length"),
        "capabilities": model.get("capabilities") or [],
        "kind": "cloud" if ":cloud" in name or model.get("remote_model") else "local",
    }


def list_direct_cloud_models():
    keys = active_api_keys()
    if not keys:
        return []
    data = http_json(
        "GET",
        f"{DIRECT_CLOUD_BASE_URL}/v1/models",
        headers={"Authorization": f"Bearer {keys[0].get('value')}"},
        timeout=30,
    )
    models = []
    for item in (data or {}).get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        models.append({
            "name": model_id,
            "remote_model": model_id,
            "remote_host": DIRECT_CLOUD_BASE_URL,
            "context_length": None,
            "capabilities": ["cloud"],
            "kind": "cloud",
        })
    return models


def collect_status():
    key_store = public_key_store()
    direct_mode = key_store.get("upstream_mode") == "direct_cloud"
    out = {
        "ok": True,
        "ollama_base_url": OLLAMA_BASE_URL,
        "direct_cloud_base_url": DIRECT_CLOUD_BASE_URL,
        "openhands_api_url": OPENHANDS_API_URL,
        "openhands_public_url": OPENHANDS_PUBLIC_URL,
        "openwebui_public_url": OPENWEBUI_PUBLIC_URL,
        "manager_proxy_base_url": OPENHANDS_LLM_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "default_openhands_stream": OPENHANDS_STREAM,
        "key_store": key_store,
        "checks": {},
    }

    try:
        out["ollama_version"] = http_json("GET", f"{OLLAMA_BASE_URL}/api/version")
        out["checks"]["ollama_version"] = "ok"
    except Exception as exc:
        if not direct_mode:
            out["ok"] = False
        out["checks"]["ollama_version"] = str(exc)

    try:
        out["ollama_account"] = http_json("POST", f"{OLLAMA_BASE_URL}/api/me", {})
        out["checks"]["ollama_account"] = "ok"
    except Exception as exc:
        out["checks"]["ollama_account"] = str(exc)

    try:
        tags = http_json("GET", f"{OLLAMA_BASE_URL}/api/tags")
        models = [classify_model(item) for item in tags.get("models", [])]
        out["models"] = models
        out["cloud_models"] = [m for m in models if m["kind"] == "cloud"]
        out["local_models"] = [m for m in models if m["kind"] == "local"]
        out["checks"]["ollama_models"] = "ok"
    except Exception as exc:
        out["checks"]["ollama_models"] = str(exc)
        if direct_mode:
            try:
                models = list_direct_cloud_models()
                out["models"] = models
                out["cloud_models"] = models
                out["local_models"] = []
                out["checks"]["direct_cloud_models"] = "ok"
            except Exception as direct_exc:
                out["ok"] = False
                out["models"] = []
                out["cloud_models"] = []
                out["local_models"] = []
                out["checks"]["direct_cloud_models"] = str(direct_exc)
        else:
            out["ok"] = False
            out["models"] = []
            out["cloud_models"] = []
            out["local_models"] = []

    try:
        settings = http_json("GET", f"{OPENHANDS_API_URL}/api/v1/settings")
        llm = settings.get("agent_settings", {}).get("llm", {})
        out["openhands_llm"] = {
            "model": llm.get("model"),
            "base_url": llm.get("base_url"),
            "stream": llm.get("stream"),
            "reasoning_effort": llm.get("reasoning_effort"),
        }
        out["checks"]["openhands_settings"] = "ok"
    except Exception as exc:
        out["checks"]["openhands_settings"] = str(exc)

    try:
        with urllib.request.urlopen(f"{SEARXNG_URL}/healthz", timeout=10) as resp:
            out["searxng"] = resp.read().decode("utf-8", "replace").strip()
        out["checks"]["searxng"] = "ok"
    except Exception as exc:
        out["checks"]["searxng"] = str(exc)

    return out


def test_stream(model, upstream_mode=None, key_name=None):
    store = load_key_store()
    mode = upstream_mode or store.get("upstream_mode", "external_ollama")
    keys = []
    if mode == "direct_cloud":
        if key_name:
            key = get_key(key_name)
            keys = [key] if key else []
        else:
            keys = active_api_keys()
        if not keys:
            return {
                "ok": False,
                "model": model,
                "error": "Nenhuma API key ativa configurada para o modo direct_cloud.",
            }
    else:
        keys = [{"name": "external-ollama", "value": LLM_API_KEY}]

    payload = {
        "model": normalize_direct_model_name(model) if mode == "direct_cloud" else model,
        "messages": [{"role": "user", "content": "Responda somente: ok"}],
        "stream": True,
        "max_tokens": 8,
        "temperature": 0,
        "reasoning_effort": "none",
    }
    errors = []
    target_base = DIRECT_CLOUD_BASE_URL if mode == "direct_cloud" else OLLAMA_BASE_URL
    for key in keys:
        req = urllib.request.Request(
            f"{target_base}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key.get('value') or LLM_API_KEY}"},
            method="POST",
        )
        start = time.time()
        chunks = 0
        text = ""
        reasoning = ""
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if not line.startswith("data: "):
                        continue
                    chunks += 1
                    obj = json.loads(line[6:])
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    text += delta.get("content") or ""
                    reasoning += delta.get("reasoning") or ""
            return {
                "ok": text.strip().lower() == "ok",
                "model": model,
                "upstream_mode": mode,
                "key_name": key.get("name"),
                "text": text.strip(),
                "chunks": chunks,
                "reasoning_chars": len(reasoning),
                "seconds": round(time.time() - start, 2),
            }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            errors.append({"key_name": key.get("name"), "status": exc.code, "error": body[:1000]})
            if exc.code not in (401, 403, 429):
                break
        except Exception as exc:
            errors.append({"key_name": key.get("name"), "error": str(exc)})
            break
    return {"ok": False, "model": model, "upstream_mode": mode, "errors": errors}


def apply_openhands(model, base_url=None, stream=None):
    llm_model = model if model.startswith("openai/") else f"openai/{model}"
    use_stream = OPENHANDS_STREAM if stream is None else bool(stream)
    payload = {
        "agent_settings_diff": {
            "llm": {
                "model": llm_model,
                "api_key": LLM_API_KEY,
                "base_url": base_url or OPENHANDS_LLM_BASE_URL,
                "stream": use_stream,
                "drop_params": True,
                "modify_params": True,
                "disable_vision": True,
                "reasoning_effort": "none",
                "max_input_tokens": 30000,
                "max_output_tokens": 2048,
            }
        }
    }
    return http_json("POST", f"{OPENHANDS_API_URL}/api/v1/settings", payload)


def upsert_api_key(name, value, enabled=True, quota_limit_tokens=None, reset_period_hours=None, reset_at=None, used_tokens=None):
    name = str(name or "").strip()
    value = str(value or "").strip()
    store = load_key_store()
    existing = next((item for item in store.get("keys", []) if item.get("name") == name), None)
    if not value and existing:
        value = str(existing.get("value", "")).strip()
    if not name or not value:
        raise ValueError("Informe nome e chave. Para chave nova, cole a API key pelo menos uma vez.")
    keys = [item for item in store.get("keys", []) if item.get("name") != name]
    payload = {
        "name": name,
        "value": value,
        "enabled": bool(enabled),
        "quota_limit_tokens": quota_limit_tokens,
        "reset_period_hours": reset_period_hours,
        "reset_at": reset_at,
        "used_tokens": used_tokens,
    }
    if quota_limit_tokens is None and existing:
        payload["quota_limit_tokens"] = existing.get("quota_limit_tokens", 0)
    if reset_period_hours is None and existing:
        payload["reset_period_hours"] = existing.get("reset_period_hours", DEFAULT_QUOTA_RESET_HOURS)
    if reset_at is None and existing:
        payload["reset_at"] = existing.get("reset_at")
    keys.append(normalize_key_item(payload, existing))
    store["keys"] = keys
    if not store.get("active_key"):
        store["active_key"] = name
    save_key_store(store)
    return public_key_store()


def delete_api_key(name):
    store = load_key_store()
    keys = [item for item in store.get("keys", []) if item.get("name") != name]
    store["keys"] = keys
    if store.get("active_key") == name:
        store["active_key"] = keys[0]["name"] if keys else None
    save_key_store(store)
    return public_key_store()


def update_routing(mode=None, active_key=None, auto_fallback=None, switch_threshold_percent=None):
    store = load_key_store()
    if mode:
        if mode not in ("external_ollama", "direct_cloud"):
            raise ValueError("Modo inválido. Use external_ollama ou direct_cloud.")
        store["upstream_mode"] = mode
    if active_key is not None:
        if active_key and not any(item.get("name") == active_key for item in store.get("keys", [])):
            raise ValueError("API key não encontrada.")
        store["active_key"] = active_key or None
    if auto_fallback is not None:
        store["auto_fallback"] = bool(auto_fallback)
    if switch_threshold_percent is not None:
        store["switch_threshold_percent"] = to_float(switch_threshold_percent, SWITCH_THRESHOLD_PERCENT)
    save_key_store(store)
    return public_key_store()


def find_key(name, include_disabled=False):
    store = load_key_store()
    for item in store.get("keys", []):
        if item.get("name") == name and (include_disabled or item.get("enabled", True)):
            return item
    return None


def test_api_key(name, include_disabled=True):
    key = find_key(name, include_disabled=include_disabled)
    if not key:
        return {"ok": False, "error": "API key não encontrada.", "name": name}
    req = urllib.request.Request(
        f"{DIRECT_CLOUD_BASE_URL}/v1/models",
        headers={"Authorization": f"Bearer {key.get('value')}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        models = [item.get("id") for item in data.get("data", [])]
        update_key_test_status(name, True, model_count=len(models))
        return {
            "ok": True,
            "name": name,
            "fingerprint": key_fingerprint(key.get("value")),
            "models": models,
        }
    except urllib.error.HTTPError as exc:
        error = exc.read().decode("utf-8", "replace")[:1000]
        update_key_test_status(name, False, error=error)
        return {"ok": False, "name": name, "status": exc.code, "error": error}
    except Exception as exc:
        update_key_test_status(name, False, error=str(exc))
        return {"ok": False, "name": name, "error": str(exc)}


def test_all_api_keys():
    store = load_key_store()
    results = []
    for item in store.get("keys", []):
        results.append(test_api_key(item.get("name"), include_disabled=True))
    return {"ok": all(item.get("ok") for item in results) if results else False, "results": results, "key_store": public_key_store()}


INDEX_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ollama Agent Manager</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #0f172a; color: #e5e7eb; }
    header { padding: 22px 28px; background: #111827; border-bottom: 1px solid #334155; }
    main { padding: 24px; display: grid; gap: 18px; max-width: 1180px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .card { background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 16px; }
    h1, h2 { margin: 0 0 10px; }
    .muted { color: #94a3b8; }
    .ok { color: #86efac; }
    .bad { color: #fca5a5; }
    button, select, input { background: #1f2937; color: #e5e7eb; border: 1px solid #475569; border-radius: 10px; padding: 10px; }
    button { cursor: pointer; }
    button:hover { background: #334155; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 9px; border-bottom: 1px solid #334155; vertical-align: top; }
    code, pre { background: #020617; border-radius: 8px; padding: 2px 6px; }
    pre { padding: 12px; overflow: auto; white-space: pre-wrap; }
    a { color: #93c5fd; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .statgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 12px 0; }
    .stat { background: #020617; border: 1px solid #334155; border-radius: 12px; padding: 12px; }
    .stat strong { display: block; font-size: 22px; margin-top: 4px; }
    .pill { display: inline-block; border-radius: 999px; padding: 3px 8px; background: #1f2937; border: 1px solid #475569; }
    .warn { color: #fde68a; }
    .danger { color: #fca5a5; }
    .mini { font-size: 12px; }
  </style>
</head>
<body>
  <header>
    <h1>Ollama Agent Manager</h1>
    <div class="muted">Gerenciador local: Ollama Windows externo, gateway interno e OpenHands.</div>
  </header>
  <main>
    <section class="card">
      <div class="row">
        <button onclick="loadStatus()">Atualizar status</button>
        <button onclick="testStream()">Testar streaming</button>
        <button onclick="applyOpenHands()">Aplicar no OpenHands</button>
        <a href="https://ollama.com/signin" target="_blank">Abrir login oficial Ollama no Chrome</a>
      </div>
    </section>

    <section class="grid">
      <div class="card"><h2>Ollama</h2><pre id="ollama">Carregando...</pre></div>
      <div class="card"><h2>OpenHands</h2><pre id="openhands">Carregando...</pre></div>
      <div class="card"><h2>Links</h2><pre id="links">Carregando...</pre></div>
    </section>

    <section class="card">
      <h2>Modelo ativo</h2>
      <div class="row">
        <select id="model"></select>
        <label><input type="checkbox" id="stream"> streaming no OpenHands</label>
        <span class="muted">Modelos Cloud dependem da conta autenticada no Ollama oficial.</span>
      </div>
    </section>

    <section class="card">
      <h2>Roteamento e API keys</h2>
      <p class="muted">O saldo exibido é uma estimativa local: configure o limite/reset de cada key e o gateway desconta os tokens retornados pelas respostas. A Ollama não expõe oficialmente saldo total/reset por API.</p>
      <div class="statgrid">
        <div class="stat"><span class="muted">Tokens restantes conhecidos</span><strong id="totalRemaining">-</strong></div>
        <div class="stat"><span class="muted">Tokens usados rastreados</span><strong id="totalUsed">-</strong></div>
        <div class="stat"><span class="muted">Contas com quota</span><strong id="knownAccounts">-</strong></div>
        <div class="stat"><span class="muted">Contas sem quota</span><strong id="unknownAccounts">-</strong></div>
      </div>
      <div class="row">
        <label>Modo
          <select id="upstreamMode">
            <option value="external_ollama">Ollama externo autenticado</option>
            <option value="direct_cloud">Ollama Cloud direto com API key</option>
          </select>
        </label>
        <label>Chave ativa <select id="activeKey"></select></label>
        <label><input type="checkbox" id="autoFallback"> fallback automático</label>
        <label>trocar em %
          <input id="switchThreshold" type="number" min="0" max="100" step="1" value="10" style="width: 90px">
        </label>
        <button onclick="saveRouting()">Salvar roteamento</button>
        <button onclick="testActiveKey()">Testar chave ativa</button>
        <button onclick="testAllKeys()">Testar todas</button>
        <button onclick="resetSelectedUsage()">Resetar uso da ativa</button>
        <button onclick="resetAllUsage()">Resetar uso de todas</button>
        <button onclick="loadHandoff()">Ver handoff</button>
      </div>
      <div class="row">
        <input id="keyName" placeholder="nome da chave">
        <input id="keyValue" placeholder="cole a API key" type="password" size="44">
        <input id="keyLimit" placeholder="limite de tokens" type="number" min="0" step="1000">
        <input id="keyUsed" placeholder="tokens usados agora" type="number" min="0" step="1000">
        <input id="keyResetHours" placeholder="reset em horas" type="number" min="0" step="0.5">
        <label><input type="checkbox" id="keyEnabled" checked> ativa</label>
        <button onclick="saveApiKey()">Salvar e testar chave</button>
        <button onclick="deleteSelectedKey()">Excluir chave ativa</button>
      </div>
      <table>
        <thead><tr><th>Conta/key</th><th>Status</th><th>Uso</th><th>Reset</th><th>Último teste</th></tr></thead>
        <tbody id="keyRows"></tbody>
      </table>
      <pre id="keys">Carregando...</pre>
      <p class="muted">Use apenas chaves de contas suas ou autorizadas. Elas ficam no arquivo persistente do manager e aparecem mascaradas na interface.</p>
    </section>

    <section class="card">
      <h2>Modelos</h2>
      <table>
        <thead><tr><th>Nome</th><th>Tipo</th><th>Remoto</th><th>Contexto</th><th>Capacidades</th></tr></thead>
        <tbody id="models"></tbody>
      </table>
    </section>

    <section class="card">
      <h2>Resultado</h2>
      <pre id="result">Pronto.</pre>
    </section>
  </main>
<script>
let statusCache = null;

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...options });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(JSON.stringify(data, null, 2));
  return data;
}

function pretty(x) { return JSON.stringify(x, null, 2); }
function fmtNumber(x) { return x === null || x === undefined ? 'desconhecido' : Number(x).toLocaleString('pt-BR'); }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString('pt-BR') : 'desconhecido'; }
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return 'desconhecido';
  seconds = Math.max(0, Number(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}min`;
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadStatus() {
  document.getElementById('result').textContent = 'Atualizando...';
  try {
    const data = await api('/api/status');
    statusCache = data;
    const account = data.ollama_account || {};
    document.getElementById('ollama').textContent = pretty({
      ok: data.ok,
      base_url: data.ollama_base_url,
      version: data.ollama_version,
      account: account.email ? { email: account.email, name: account.name, plan: account.plan } : account,
      checks: data.checks
    });
    document.getElementById('openhands').textContent = pretty({
      public_url: data.openhands_public_url,
      api_url: data.openhands_api_url,
      llm: data.openhands_llm,
      gateway_for_openhands: data.manager_proxy_base_url
    });
    document.getElementById('links').textContent = pretty({
      openhands: data.openhands_public_url,
      open_webui: data.openwebui_public_url,
      gateway_models: location.origin + '/llm/v1/models'
    });
    renderKeys(data.key_store || {});
    const select = document.getElementById('model');
    select.innerHTML = '';
    for (const m of data.models || []) {
      const opt = document.createElement('option');
      opt.value = m.name;
      opt.textContent = `${m.name} (${m.kind})`;
      if (m.name === data.default_model || `openai/${m.name}` === (data.openhands_llm || {}).model) opt.selected = true;
      select.appendChild(opt);
    }
    document.getElementById('stream').checked = Boolean((data.openhands_llm || {}).stream);
    document.getElementById('models').innerHTML = (data.models || []).map(m => `
      <tr>
        <td><code>${m.name}</code></td>
        <td>${m.kind}</td>
        <td>${m.remote_model || ''}</td>
        <td>${m.context_length || ''}</td>
        <td>${(m.capabilities || []).join(', ')}</td>
      </tr>
    `).join('');
    document.getElementById('result').textContent = 'Status atualizado.';
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

function renderKeys(store) {
  window.keyStore = store;
  document.getElementById('upstreamMode').value = store.upstream_mode || 'external_ollama';
  document.getElementById('autoFallback').checked = Boolean(store.auto_fallback);
  document.getElementById('switchThreshold').value = store.switch_threshold_percent ?? 10;
  const totals = store.totals || {};
  document.getElementById('totalRemaining').textContent = fmtNumber(totals.total_remaining_tokens);
  document.getElementById('totalUsed').textContent = fmtNumber(totals.total_used_tokens || 0);
  document.getElementById('knownAccounts').textContent = fmtNumber(totals.known_quota_accounts || 0);
  document.getElementById('unknownAccounts').textContent = fmtNumber(totals.unknown_quota_accounts || 0);
  const select = document.getElementById('activeKey');
  select.innerHTML = '<option value="">nenhuma</option>';
  for (const key of store.keys || []) {
    const opt = document.createElement('option');
    opt.value = key.name;
    const rem = key.remaining_tokens === null || key.remaining_tokens === undefined ? 'quota ?' : `${fmtNumber(key.remaining_tokens)} tokens`;
    opt.textContent = `${key.name} ${key.enabled ? '' : '(desativada)'} ${key.masked || ''} - ${key.quota_status || '?'} - ${rem}`;
    if (key.name === store.active_key) opt.selected = true;
    select.appendChild(opt);
  }
  document.getElementById('keyRows').innerHTML = (store.keys || []).map(key => {
    const remainingClass = key.remaining_tokens === 0 ? 'danger' : '';
    const test = key.last_test_at ? `${key.last_test_ok ? 'OK' : 'falhou'} em ${fmtTime(key.last_test_at)}` : 'não testada';
    const err = key.last_test_error ? `<div class="danger mini">${escapeHtml(key.last_test_error)}</div>` : '';
    return `<tr>
      <td><strong>${escapeHtml(key.name)}</strong><br><span class="muted mini">${escapeHtml(key.masked || '')}</span></td>
      <td><span class="pill">${key.enabled ? 'ativa' : 'desativada'}</span> <span class="pill">${escapeHtml(key.quota_status || 'unknown')}</span><br><span class="muted mini">${escapeHtml(key.last_status || '')}</span></td>
      <td><span class="${remainingClass}">${fmtNumber(key.remaining_tokens)} restantes</span><br><span class="muted mini">${key.remaining_percent ?? '??'}% sobrando; ${fmtNumber(key.used_tokens || 0)} usados / ${key.quota_limit_tokens ? fmtNumber(key.quota_limit_tokens) : 'limite desconhecido'}</span></td>
      <td>${fmtDuration(key.seconds_to_reset)}<br><span class="muted mini">${fmtTime(key.reset_at)}</span></td>
      <td>${test}${err}<br><span class="muted mini">${fmtNumber(key.last_test_model_count || 0)} modelos listados</span></td>
    </tr>`;
  }).join('');
  fillKeyFormFromSelection();
  document.getElementById('keys').textContent = pretty(store);
}

function fillKeyFormFromSelection() {
  const store = window.keyStore || {};
  const selected = document.getElementById('activeKey').value;
  const key = (store.keys || []).find(item => item.name === selected);
  if (!key) return;
  document.getElementById('keyName').value = key.name || '';
  document.getElementById('keyLimit').value = key.quota_limit_tokens || '';
  document.getElementById('keyUsed').value = key.used_tokens || '';
  document.getElementById('keyResetHours').value = key.reset_period_hours || '';
  document.getElementById('keyEnabled').checked = Boolean(key.enabled);
}

async function testStream() {
  const model = document.getElementById('model').value;
  document.getElementById('result').textContent = `Testando streaming com ${model}...`;
  try {
    const data = await api('/api/test-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model,
        upstream_mode: document.getElementById('upstreamMode').value,
        key_name: document.getElementById('activeKey').value || null
      })
    });
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function saveRouting() {
  document.getElementById('result').textContent = 'Salvando roteamento...';
  try {
    const data = await api('/api/routing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: document.getElementById('upstreamMode').value,
        active_key: document.getElementById('activeKey').value || null,
        auto_fallback: document.getElementById('autoFallback').checked,
        switch_threshold_percent: document.getElementById('switchThreshold').value || 10
      })
    });
    renderKeys(data);
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function saveApiKey() {
  const name = document.getElementById('keyName').value;
  document.getElementById('result').textContent = 'Salvando e testando API key...';
  try {
    const data = await api('/api/api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        key: document.getElementById('keyValue').value,
        enabled: document.getElementById('keyEnabled').checked,
        quota_limit_tokens: document.getElementById('keyLimit').value || 0,
        used_tokens: document.getElementById('keyUsed').value || null,
        reset_period_hours: document.getElementById('keyResetHours').value || 0
      })
    });
    document.getElementById('keyValue').value = '';
    renderKeys(data);
    const test = await api('/api/test-api-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    await loadStatus();
    document.getElementById('result').textContent = pretty({ saved: true, test });
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function deleteSelectedKey() {
  const name = document.getElementById('activeKey').value;
  if (!name) {
    document.getElementById('result').textContent = 'Nenhuma chave ativa selecionada.';
    return;
  }
  document.getElementById('result').textContent = `Excluindo ${name}...`;
  try {
    const data = await api('/api/delete-api-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    renderKeys(data);
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function testActiveKey() {
  const name = document.getElementById('activeKey').value;
  if (!name) {
    document.getElementById('result').textContent = 'Nenhuma chave ativa selecionada.';
    return;
  }
  document.getElementById('result').textContent = `Testando ${name}...`;
  try {
    const data = await api('/api/test-api-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    await loadStatus();
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function testAllKeys() {
  document.getElementById('result').textContent = 'Testando todas as chaves...';
  try {
    const data = await api('/api/test-all-api-keys', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    renderKeys(data.key_store || {});
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function resetSelectedUsage() {
  const name = document.getElementById('activeKey').value;
  if (!name) {
    document.getElementById('result').textContent = 'Nenhuma chave ativa selecionada.';
    return;
  }
  document.getElementById('result').textContent = `Resetando uso de ${name}...`;
  try {
    const data = await api('/api/reset-usage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    renderKeys(data);
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function resetAllUsage() {
  document.getElementById('result').textContent = 'Resetando uso de todas as chaves...';
  try {
    const data = await api('/api/reset-usage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    renderKeys(data);
    document.getElementById('result').textContent = pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function loadHandoff() {
  document.getElementById('result').textContent = 'Carregando handoff...';
  try {
    const data = await api('/api/handoff');
    document.getElementById('result').textContent = data.ok ? data.content : pretty(data);
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

async function applyOpenHands() {
  const model = document.getElementById('model').value;
  const stream = document.getElementById('stream').checked;
  document.getElementById('result').textContent = `Aplicando ${model} no OpenHands...`;
  try {
    const data = await api('/api/apply-openhands', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, stream })
    });
    document.getElementById('result').textContent = pretty(data);
    await loadStatus();
  } catch (err) {
    document.getElementById('result').textContent = String(err);
  }
}

document.getElementById('activeKey').addEventListener('change', fillKeyFormFromSelection);
loadStatus();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OllamaAgentManager/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send_json(self, code, value):
        body = json_bytes(value)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_basic_auth(self):
        if self.path.startswith("/llm/") or self.path.startswith("/ollama/") or self.path == "/healthz":
            return True
        expected = "Basic " + base64.b64encode(f"{MANAGER_USERNAME}:{MANAGER_PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ollama Agent Manager"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def check_proxy_auth(self):
        if not PROXY_API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {PROXY_API_KEY}" or auth == f"Bearer {LLM_API_KEY}"

    def proxy(self, prefix):
        if not self.check_proxy_auth():
            self.send_json(401, {"error": "missing or invalid bearer token"})
            return
        store = load_key_store()
        mode = store.get("upstream_mode", "external_ollama")
        parsed = urllib.parse.urlsplit(self.path)
        suffix = parsed.path[len(prefix):]
        target_base = DIRECT_CLOUD_BASE_URL if mode == "direct_cloud" else OLLAMA_BASE_URL
        if mode == "direct_cloud":
            keys = active_api_keys()
            if not keys:
                self.send_json(502, {"error": "Modo direct_cloud selecionado, mas nenhuma API key ativa foi configurada."})
                return
        else:
            keys = [{"name": "external-ollama", "value": LLM_API_KEY}]
        target = target_base + suffix
        if parsed.query:
            target += "?" + parsed.query
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None
        headers = {}
        for key in ("Content-Type", "Accept", "Authorization"):
            if self.headers.get(key):
                headers[key] = self.headers.get(key)
        if mode == "direct_cloud":
            body = prepare_direct_cloud_body(body, headers.get("Content-Type", ""))
        last_error = None
        for idx, api_key in enumerate(keys):
            upstream_headers = dict(headers)
            if mode == "direct_cloud":
                upstream_headers["Authorization"] = f"Bearer {api_key.get('value')}"
            elif "Authorization" not in upstream_headers:
                upstream_headers["Authorization"] = f"Bearer {LLM_API_KEY}"
            req = urllib.request.Request(target, data=body, headers=upstream_headers, method=self.command)
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    content_type = resp.headers.get("Content-Type", "application/octet-stream")
                    if "text/event-stream" in content_type:
                        self.send_response(resp.status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Connection", "close")
                        self.send_header("X-Manager-Upstream-Mode", mode)
                        self.send_header("X-Manager-Key-Name", str(api_key.get("name", "")))
                        self.end_headers()
                        sample = bytearray()
                        while True:
                            chunk = resp.read(8192)
                            if not chunk:
                                break
                            if len(sample) < 5_000_000:
                                sample.extend(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        tokens = extract_usage_tokens(bytes(sample), content_type)
                    else:
                        response_body = resp.read()
                        tokens = extract_usage_tokens(response_body, content_type)
                        self.send_response(resp.status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(response_body)))
                        self.send_header("Connection", "close")
                        self.send_header("X-Manager-Upstream-Mode", mode)
                        self.send_header("X-Manager-Key-Name", str(api_key.get("name", "")))
                        if tokens:
                            self.send_header("X-Manager-Usage-Tokens", str(tokens))
                        self.end_headers()
                        self.wfile.write(response_body)
                    if mode == "direct_cloud" and tokens:
                        record_key_usage(api_key.get("name"), tokens)
                        maybe_switch_after_usage(api_key.get("name"), request_body=body, response_body=response_body if "text/event-stream" not in content_type else bytes(sample), tokens=tokens)
                    return
            except urllib.error.HTTPError as exc:
                body_text = exc.read()
                last_error = (exc.code, exc.headers, body_text, api_key.get("name"))
                if mode == "direct_cloud" and store.get("auto_fallback", False) and exc.code in (401, 403, 429) and idx < len(keys) - 1:
                    next_name = keys[idx + 1].get("name")
                    handoff_path, _ = write_handoff(
                        f"upstream_http_{exc.code}_fallback",
                        api_key.get("name"),
                        next_name,
                        request_body=body,
                        error=body_text.decode("utf-8", "replace")[:3000],
                    )
                    set_active_key(next_name, f"upstream_http_{exc.code}_fallback", previous_key=api_key.get("name"), handoff_path=handoff_path)
                    continue
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
                self.send_header("Connection", "close")
                self.send_header("X-Manager-Upstream-Mode", mode)
                self.send_header("X-Manager-Key-Name", str(api_key.get("name", "")))
                self.end_headers()
                self.wfile.write(body_text)
                return
            except Exception as exc:
                self.send_json(502, {"error": str(exc), "target": target, "upstream_mode": mode})
                return
        if last_error:
            code, headers_obj, body_text, key_name = last_error
            self.send_response(code)
            self.send_header("Content-Type", headers_obj.get("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.send_header("X-Manager-Upstream-Mode", mode)
            self.send_header("X-Manager-Key-Name", str(key_name))
            self.end_headers()
            self.wfile.write(body_text)
            return
        self.send_json(502, {"error": "Nenhum upstream disponivel.", "upstream_mode": mode})

    def do_GET(self):
        if not self.require_basic_auth():
            return
        if self.path.startswith("/llm/"):
            self.proxy("/llm")
            return
        if self.path.startswith("/ollama/"):
            self.proxy("/ollama")
            return
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        if self.path == "/" or self.path.startswith("/?"):
            self.send_html(200, INDEX_HTML)
            return
        if self.path == "/api/status":
            self.send_json(200, collect_status())
            return
        if self.path == "/api/api-keys":
            self.send_json(200, public_key_store())
            return
        if self.path == "/api/usage":
            self.send_json(200, public_key_store())
            return
        if self.path == "/api/handoff":
            self.send_json(200, latest_handoff())
            return
        if self.path == "/api/events":
            self.send_json(200, {"events": recent_events(100)})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.require_basic_auth():
            return
        if self.path.startswith("/llm/"):
            self.proxy("/llm")
            return
        if self.path.startswith("/ollama/"):
            self.proxy("/ollama")
            return
        try:
            payload = read_json(self)
            if self.path == "/api/test-stream":
                self.send_json(200, test_stream(
                    payload.get("model") or DEFAULT_MODEL,
                    upstream_mode=payload.get("upstream_mode"),
                    key_name=payload.get("key_name"),
                ))
                return
            if self.path == "/api/apply-openhands":
                model = payload.get("model") or DEFAULT_MODEL
                base_url = payload.get("base_url") or OPENHANDS_LLM_BASE_URL
                stream = payload.get("stream")
                self.send_json(200, apply_openhands(model, base_url=base_url, stream=stream))
                return
            if self.path == "/api/api-keys":
                self.send_json(200, upsert_api_key(
                    payload.get("name"),
                    payload.get("key") or payload.get("value"),
                    enabled=payload.get("enabled", True),
                    quota_limit_tokens=payload.get("quota_limit_tokens"),
                    used_tokens=payload.get("used_tokens"),
                    reset_period_hours=payload.get("reset_period_hours"),
                    reset_at=payload.get("reset_at"),
                ))
                return
            if self.path == "/api/delete-api-key":
                self.send_json(200, delete_api_key(payload.get("name")))
                return
            if self.path == "/api/routing":
                self.send_json(200, update_routing(
                    mode=payload.get("mode"),
                    active_key=payload.get("active_key"),
                    auto_fallback=payload.get("auto_fallback"),
                    switch_threshold_percent=payload.get("switch_threshold_percent"),
                ))
                return
            if self.path == "/api/test-api-key":
                self.send_json(200, test_api_key(payload.get("name")))
                return
            if self.path == "/api/test-all-api-keys":
                self.send_json(200, test_all_api_keys())
                return
            if self.path == "/api/reset-usage":
                self.send_json(200, reset_usage(payload.get("name")))
                return
            self.send_json(404, {"error": "not found"})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            self.send_json(exc.code, {"error": body})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Ollama Agent Manager listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
