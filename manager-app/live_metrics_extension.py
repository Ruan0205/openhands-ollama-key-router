#!/usr/bin/env python3
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

_LOCK = threading.RLock()
_THREAD = threading.local()
_METRICS_FILE = Path(os.environ.get("LIVE_METRICS_FILE", "/data/live-metrics.json"))
_MAX_RECENT = max(10, int(os.environ.get("LIVE_METRICS_RECENT_LIMIT", "50") or "50"))
_REFRESH_MS = max(1000, int(float(os.environ.get("LIVE_METRICS_REFRESH_SECONDS", "2") or "2") * 1000))
_CHARS_PER_TOKEN = max(1.0, float(os.environ.get("LIVE_METRICS_CHARS_PER_TOKEN", "4") or "4"))
_STATE = {"models": {}, "recent": [], "active": {}}


def _to_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _load_state():
    if not _METRICS_FILE.exists():
        return
    try:
        data = json.loads(_METRICS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _STATE["models"] = data.get("models") if isinstance(data.get("models"), dict) else {}
            _STATE["recent"] = data.get("recent") if isinstance(data.get("recent"), list) else []
    except Exception as exc:
        print(f"live metrics: não foi possível ler {_METRICS_FILE}: {exc}", flush=True)


def _save_state_locked():
    try:
        _METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = _METRICS_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"models": _STATE["models"], "recent": _STATE["recent"][-_MAX_RECENT:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, _METRICS_FILE)
    except Exception as exc:
        print(f"live metrics: não foi possível salvar {_METRICS_FILE}: {exc}", flush=True)


def _estimate_tokens_from_chars(chars):
    chars = _to_int(chars)
    return int(round(chars / _CHARS_PER_TOKEN)) if chars else 0


def _text_from_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for field in ("text", "content", "input_text", "output_text"):
                    if isinstance(item.get(field), str):
                        parts.append(item[field])
        return "".join(parts)
    return ""


def _request_details(body):
    result = {"model": "", "stream": False, "prompt_tokens_estimated": 0}
    if not body:
        return result
    try:
        raw = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return result
        result["model"] = str(payload.get("model") or "").removeprefix("openai/")
        result["stream"] = bool(payload.get("stream"))
        text_parts = []
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                text_parts.append(str(message.get("role") or ""))
                text_parts.append(_text_from_content(message.get("content")))
        for field in ("prompt", "input", "system"):
            text_parts.append(_text_from_content(payload.get(field)))
        result["prompt_tokens_estimated"] = _estimate_tokens_from_chars(sum(len(x) for x in text_parts if x))
    except Exception:
        pass
    return result


def _usage_details(raw_body, content_type=""):
    details = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_eval_count": 0,
        "eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_duration": 0,
        "total_duration": 0,
    }

    def merge(obj):
        if not isinstance(obj, dict):
            return
        usage = obj.get("usage")
        if isinstance(usage, dict):
            details["prompt_tokens"] = max(details["prompt_tokens"], _to_int(usage.get("prompt_tokens") or usage.get("input_tokens")))
            details["completion_tokens"] = max(details["completion_tokens"], _to_int(usage.get("completion_tokens") or usage.get("output_tokens")))
            details["total_tokens"] = max(
                details["total_tokens"],
                _to_int(usage.get("total_tokens")),
                details["prompt_tokens"] + details["completion_tokens"],
            )
        for field in ("prompt_eval_count", "eval_count", "prompt_eval_duration", "eval_duration", "total_duration"):
            details[field] = max(details[field], _to_int(obj.get(field)))
        if isinstance(obj.get("response"), dict):
            merge(obj["response"])

    if not raw_body:
        return details
    text = raw_body.decode("utf-8", "ignore") if isinstance(raw_body, (bytes, bytearray)) else str(raw_body)
    if "text/event-stream" in str(content_type).lower() or text.lstrip().startswith("data: "):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                merge(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    else:
        try:
            merge(json.loads(text))
        except json.JSONDecodeError:
            pass

    details["prompt_tokens"] = max(details["prompt_tokens"], details["prompt_eval_count"])
    details["completion_tokens"] = max(details["completion_tokens"], details["eval_count"])
    details["total_tokens"] = max(details["total_tokens"], details["prompt_tokens"] + details["completion_tokens"])
    return details


def _merge_usage_into_thread(details):
    if not isinstance(details, dict):
        return
    current = dict(getattr(_THREAD, "usage_details", {}) or {})
    for key, value in details.items():
        current[key] = max(_to_int(current.get(key)), _to_int(value))
    _THREAD.usage_details = current
    request_id = getattr(_THREAD, "request_id", None)
    if not request_id:
        return
    with _LOCK:
        item = _STATE["active"].get(request_id)
        if not item:
            return
        prompt = max(_to_int(current.get("prompt_tokens")), _to_int(current.get("prompt_eval_count")), _to_int(item.get("prompt_tokens_estimated")))
        completion = max(_to_int(current.get("completion_tokens")), _to_int(current.get("eval_count")), _to_int(item.get("output_tokens_estimated")))
        item["prompt_tokens_live"] = prompt
        item["completion_tokens_live"] = completion
        item["total_tokens_live"] = prompt + completion
        eval_count = _to_int(current.get("eval_count"))
        eval_duration = _to_int(current.get("eval_duration"))
        if eval_count and eval_duration:
            item["live_tokens_per_second"] = round(eval_count / (eval_duration / 1_000_000_000), 4)
            item["live_tps_basis"] = "exato: eval_count/eval_duration"


def _expected_key(globals_dict):
    try:
        keys = globals_dict["active_api_keys"]()
        if keys:
            return str(keys[0].get("name") or "")
    except Exception:
        pass
    return ""


def _start_request(handler, prefix, globals_dict):
    request_id = uuid.uuid4().hex[:12]
    now = time.time()
    item = {
        "id": request_id,
        "path": urlsplit(handler.path).path,
        "prefix": prefix,
        "model": "desconhecido",
        "key_name": _expected_key(globals_dict),
        "started_at": now,
        "first_output_at": None,
        "output_chars": 0,
        "output_tokens_estimated": 0,
        "prompt_tokens_estimated": 0,
        "live_tokens_per_second": 0.0,
        "live_tps_basis": "aguardando saída",
        "status": "gerando",
    }
    with _LOCK:
        _STATE["active"][request_id] = item
    _THREAD.request_id = request_id
    _THREAD.started_at = now
    _THREAD.model = ""
    _THREAD.key_name = item["key_name"]
    _THREAD.usage_details = {}
    _THREAD.completed = False
    return request_id


def _update_active_from_body(body):
    details = _request_details(body)
    model = details.get("model") or ""
    if model:
        _THREAD.model = model
    request_id = getattr(_THREAD, "request_id", None)
    if not request_id:
        return details
    with _LOCK:
        item = _STATE["active"].get(request_id)
        if item:
            if model:
                item["model"] = model
            item["stream"] = bool(details.get("stream"))
            item["prompt_tokens_estimated"] = _to_int(details.get("prompt_tokens_estimated"))
            item["prompt_tokens_live"] = _to_int(details.get("prompt_tokens_estimated"))
            item["total_tokens_live"] = item["prompt_tokens_live"] + _to_int(item.get("completion_tokens_live"))
    return details


def _extract_stream_text(obj):
    if not isinstance(obj, dict):
        return ""
    parts = []
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                parts.append(_text_from_content(delta.get("content")))
                parts.append(_text_from_content(delta.get("reasoning")))
                parts.append(_text_from_content(delta.get("thinking")))
            parts.append(_text_from_content(choice.get("text")))
    message = obj.get("message")
    if isinstance(message, dict):
        parts.append(_text_from_content(message.get("content")))
        parts.append(_text_from_content(message.get("thinking")))
    parts.append(_text_from_content(obj.get("response")))
    parts.append(_text_from_content(obj.get("output_text")))
    return "".join(part for part in parts if part)


def _observe_sse_object(obj):
    details = _usage_details(json.dumps(obj, ensure_ascii=False), "application/json")
    if any(_to_int(value) for value in details.values()):
        _merge_usage_into_thread(details)
    text = _extract_stream_text(obj)
    if not text:
        return
    request_id = getattr(_THREAD, "request_id", None)
    if not request_id:
        return
    now = time.time()
    with _LOCK:
        item = _STATE["active"].get(request_id)
        if not item:
            return
        if not item.get("first_output_at"):
            item["first_output_at"] = now
        item["output_chars"] = _to_int(item.get("output_chars")) + len(text)
        estimated = _estimate_tokens_from_chars(item["output_chars"])
        item["output_tokens_estimated"] = estimated
        item["completion_tokens_live"] = max(_to_int(item.get("completion_tokens_live")), estimated)
        item["total_tokens_live"] = _to_int(item.get("prompt_tokens_live")) + item["completion_tokens_live"]
        generation_elapsed = max(0.001, now - _to_float(item.get("first_output_at"), now))
        if estimated > 0:
            item["live_tokens_per_second"] = round(estimated / generation_elapsed, 4)
            item["live_tps_basis"] = f"estimado ao vivo: caracteres/{_CHARS_PER_TOKEN:g} ÷ tempo desde o primeiro fragmento"


class _MetricsWriter:
    def __init__(self, base):
        self._base = base
        self._buffer = b""

    def write(self, data):
        result = self._base.write(data)
        try:
            if isinstance(data, str):
                data = data.encode("utf-8", "ignore")
            if isinstance(data, (bytes, bytearray)) and (b"data:" in data or self._buffer):
                self._buffer += bytes(data)
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == b"[DONE]":
                        continue
                    try:
                        _observe_sse_object(json.loads(payload.decode("utf-8", "replace")))
                    except Exception:
                        continue
        except Exception:
            pass
        return result

    def flush(self):
        return self._base.flush()

    def __getattr__(self, name):
        return getattr(self._base, name)


def _finish_request(key_name, fallback_total_tokens=0, force_estimate=False):
    request_id = getattr(_THREAD, "request_id", None)
    if not request_id or getattr(_THREAD, "completed", False):
        return
    now = time.time()
    started = _to_float(getattr(_THREAD, "started_at", now), now)
    elapsed = max(0.001, now - started)
    details = dict(getattr(_THREAD, "usage_details", {}) or {})
    model = str(getattr(_THREAD, "model", "") or "desconhecido")
    with _LOCK:
        active = dict(_STATE["active"].get(request_id) or {})
    if active.get("model") and model == "desconhecido":
        model = active["model"]

    prompt_tokens = max(_to_int(details.get("prompt_tokens")), _to_int(details.get("prompt_eval_count")))
    completion_tokens = max(_to_int(details.get("completion_tokens")), _to_int(details.get("eval_count")))
    estimated_prompt = _to_int(active.get("prompt_tokens_estimated"))
    estimated_completion = _to_int(active.get("output_tokens_estimated"))
    if not prompt_tokens and force_estimate:
        prompt_tokens = estimated_prompt
    if not completion_tokens and force_estimate:
        completion_tokens = estimated_completion
    total_tokens = max(_to_int(details.get("total_tokens")), _to_int(fallback_total_tokens), prompt_tokens + completion_tokens)

    eval_count = _to_int(details.get("eval_count"))
    eval_duration_ns = _to_int(details.get("eval_duration"))
    if eval_count > 0 and eval_duration_ns > 0:
        tps = eval_count / (eval_duration_ns / 1_000_000_000)
        tps_basis = "exato: eval_count/eval_duration"
    elif completion_tokens > 0:
        first_output = _to_float(active.get("first_output_at"), 0)
        speed_elapsed = max(0.001, now - first_output) if first_output else elapsed
        tps = completion_tokens / speed_elapsed
        tps_basis = "estimado: tokens de saída/tempo observado"
    else:
        tps = 0.0
        tps_basis = "indisponível: provedor não enviou uso nem texto mensurável"

    record = {
        "id": request_id,
        "model": model,
        "key_name": str(key_name or active.get("key_name") or ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "duration_seconds": round(elapsed, 4),
        "tokens_per_second": round(tps, 4),
        "tps_basis": tps_basis,
        "estimated": not bool(eval_count and eval_duration_ns),
        "finished_at": int(now),
        "status": "concluída",
    }

    with _LOCK:
        _STATE["active"].pop(request_id, None)
        metric = _STATE["models"].setdefault(model, {
            "model": model,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tps_sum": 0.0,
            "tps_samples": 0,
        })
        metric["requests"] = _to_int(metric.get("requests")) + 1
        metric["prompt_tokens"] = _to_int(metric.get("prompt_tokens")) + prompt_tokens
        metric["completion_tokens"] = _to_int(metric.get("completion_tokens")) + completion_tokens
        metric["total_tokens"] = _to_int(metric.get("total_tokens")) + total_tokens
        if tps > 0:
            metric["tps_sum"] = _to_float(metric.get("tps_sum")) + tps
            metric["tps_samples"] = _to_int(metric.get("tps_samples")) + 1
        metric.update({
            "last_key_name": record["key_name"],
            "last_prompt_tokens": prompt_tokens,
            "last_completion_tokens": completion_tokens,
            "last_total_tokens": total_tokens,
            "last_duration_seconds": round(elapsed, 4),
            "last_tokens_per_second": round(tps, 4),
            "average_tokens_per_second": round(_to_float(metric.get("tps_sum")) / max(1, _to_int(metric.get("tps_samples"))), 4),
            "last_tps_basis": tps_basis,
            "last_tps_estimated": record["estimated"],
            "last_finished_at": int(now),
        })
        _STATE["recent"].append(record)
        _STATE["recent"] = _STATE["recent"][-_MAX_RECENT:]
        _save_state_locked()
    _THREAD.completed = True


def _finish_without_usage(status="finalizada sem métricas de uso"):
    request_id = getattr(_THREAD, "request_id", None)
    if not request_id or getattr(_THREAD, "completed", False):
        return
    now = time.time()
    started = _to_float(getattr(_THREAD, "started_at", now), now)
    with _LOCK:
        active = _STATE["active"].pop(request_id, None)
        if active:
            active.update({"duration_seconds": round(max(0.001, now - started), 4), "finished_at": int(now), "status": status})
            _STATE["recent"].append(active)
            _STATE["recent"] = _STATE["recent"][-_MAX_RECENT:]
    _THREAD.completed = True


def _snapshot(globals_dict):
    now = time.time()
    with _LOCK:
        models = []
        active_by_model = {}
        active = []
        for item in _STATE["active"].values():
            row = dict(item)
            row["elapsed_seconds"] = round(max(0.0, now - _to_float(row.get("started_at"), now)), 2)
            first_output = _to_float(row.get("first_output_at"), 0)
            if first_output and _to_int(row.get("output_tokens_estimated")) > 0 and not str(row.get("live_tps_basis", "")).startswith("exato"):
                generation_elapsed = max(0.001, now - first_output)
                row["live_tokens_per_second"] = round(_to_int(row.get("output_tokens_estimated")) / generation_elapsed, 4)
            active.append(row)
            name = row.get("model") or "desconhecido"
            active_by_model.setdefault(name, []).append(row)
        for name, value in _STATE["models"].items():
            row = dict(value)
            running = active_by_model.get(name, [])
            row["active_requests"] = len(running)
            if running:
                newest = running[-1]
                row["current_tokens_per_second"] = newest.get("live_tokens_per_second", 0)
                row["current_tokens"] = newest.get("total_tokens_live", 0)
                row["current_tps_basis"] = newest.get("live_tps_basis", "")
            row.pop("tps_sum", None)
            row.pop("tps_samples", None)
            models.append(row)
        for name, running in active_by_model.items():
            if name not in _STATE["models"]:
                newest = running[-1]
                models.append({
                    "model": name,
                    "requests": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "last_tokens_per_second": 0,
                    "average_tokens_per_second": 0,
                    "active_requests": len(running),
                    "current_tokens_per_second": newest.get("live_tokens_per_second", 0),
                    "current_tokens": newest.get("total_tokens_live", 0),
                    "current_tps_basis": newest.get("live_tps_basis", ""),
                })
        models.sort(key=lambda row: (row.get("active_requests", 0), row.get("last_finished_at", 0)), reverse=True)
        recent = list(reversed(_STATE["recent"][-20:]))
    try:
        key_store = globals_dict["public_key_store"]()
    except Exception as exc:
        key_store = {"error": str(exc), "keys": []}
    return {
        "ok": True,
        "server_time": int(now),
        "refresh_seconds": _REFRESH_MS / 1000,
        "active": active,
        "models": models,
        "recent": recent,
        "key_store": key_store,
    }


def _inject_html(index_html):
    marker = '<section class="card" id="liveMetricsCard">'
    if marker not in index_html:
        section = '''
    <section class="card" id="liveMetricsCard">
      <h2>Monitor ao vivo</h2>
      <div class="statgrid">
        <div class="stat"><span class="muted">Requisições ativas</span><strong id="liveActiveCount">0</strong></div>
        <div class="stat"><span class="muted">TPS atual</span><strong id="liveLastTps">-</strong></div>
        <div class="stat"><span class="muted">Tokens observados</span><strong id="liveTotalTokens">0</strong></div>
        <div class="stat"><span class="muted">Atualização</span><strong id="liveUpdatedAt">-</strong></div>
      </div>
      <p class="muted mini">Durante streaming, o TPS aparece como estimativa ao vivo (~) pelos fragmentos de texto. Ao concluir, usa eval_count/eval_duration quando o provedor envia; senão mantém uma estimativa.</p>
      <h3>Requisições em andamento</h3>
      <div id="liveActiveRequests" class="muted">Nenhuma requisição ativa.</div>
      <h3>Uso e velocidade por modelo</h3>
      <table>
        <thead><tr><th>Modelo</th><th>Conta</th><th>Req.</th><th>Entrada</th><th>Saída</th><th>Total</th><th>TPS atual/último</th><th>TPS médio</th><th>Duração</th><th>Atualizado</th></tr></thead>
        <tbody id="liveModelRows"></tbody>
      </table>
      <h3>Últimas requisições</h3>
      <pre id="liveRecentRequests">Nenhuma requisição registrada.</pre>
    </section>
'''
        anchor = '    <section class="card">\n      <h2>Resultado</h2>'
        if anchor in index_html:
            index_html = index_html.replace(anchor, section + "\n" + anchor, 1)
        else:
            index_html = index_html.replace("</main>", section + "\n</main>", 1)
    else:
        index_html = index_html.replace("Último TPS", "TPS atual")
        index_html = index_html.replace("Tokens processados", "Tokens observados")
        index_html = index_html.replace("<th>Último TPS</th>", "<th>TPS atual/último</th>")
        index_html = index_html.replace(
            "Atualiza automaticamente. TPS usa eval_count/eval_duration quando o provedor envia essas métricas; caso contrário, usa tokens de saída divididos pelo tempo total da requisição.",
            "Durante streaming, o TPS aparece como estimativa ao vivo (~) pelos fragmentos de texto. Ao concluir, usa eval_count/eval_duration quando o provedor envia; senão mantém uma estimativa.",
        )

    start = index_html.find("// BEGIN LIVE_METRICS_UI")
    end = index_html.find("// END LIVE_METRICS_UI")
    if start != -1 and end != -1:
        end = end + len("// END LIVE_METRICS_UI")
        index_html = index_html[:start] + index_html[end:]

    js = f'''
// BEGIN LIVE_METRICS_UI
let liveMetricsBusy = false;

function liveFmtTps(value, estimated = false) {{
  const n = Number(value || 0);
  return n > 0 ? `${{estimated ? '~' : ''}}${{n.toLocaleString('pt-BR', {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} tok/s` : '-';
}}

function renderLiveQuota(store) {{
  if (!store || !Array.isArray(store.keys)) return;
  const totals = store.totals || {{}};
  const setText = (id, value) => {{ const el = document.getElementById(id); if (el) el.textContent = value; }};
  setText('totalRemaining', fmtNumber(totals.total_remaining_tokens));
  setText('totalUsed', fmtNumber(totals.total_used_tokens || 0));
  setText('knownAccounts', fmtNumber(totals.known_quota_accounts || 0));
  setText('unknownAccounts', fmtNumber(totals.unknown_quota_accounts || 0));
  const wait = store.wait_mode || {{}};
  setText('waitMode', wait.enabled ? `aguardando (${{wait.reason || 'sem motivo'}})` : 'ativo');
  const body = document.getElementById('keyRows');
  if (!body) return;
  body.innerHTML = store.keys.map(key => {{
    const remainingClass = key.remaining_tokens === 0 ? 'danger' : '';
    const test = key.last_test_at ? `${{key.last_test_ok ? 'OK' : 'falhou'}} em ${{fmtTime(key.last_test_at)}}` : 'não testada';
    const err = key.last_test_error ? `<div class="danger mini">${{escapeHtml(key.last_test_error)}}</div>` : '';
    const blocked = key.runtime_blocked ? `<div class="danger mini">bloqueada: ${{escapeHtml(key.runtime_blocked_reason || '')}}</div>` : '';
    return `<tr>
      <td><strong>${{escapeHtml(key.name)}}</strong><br><span class="muted mini">${{escapeHtml(key.masked || '')}}</span></td>
      <td><span class="pill">${{key.enabled ? 'ativa' : 'desativada'}}</span> <span class="pill">${{escapeHtml(key.quota_status || 'unknown')}}</span>${{blocked}}<br><span class="muted mini">${{escapeHtml(key.last_status || '')}}</span></td>
      <td><span class="${{remainingClass}}">${{fmtNumber(key.remaining_tokens)}} restantes</span><br><span class="muted mini">${{key.remaining_percent ?? '??'}}% sobrando; ${{fmtNumber(key.used_tokens || 0)}} usados / ${{key.quota_limit_tokens ? fmtNumber(key.quota_limit_tokens) : 'limite desconhecido'}}</span></td>
      <td>${{fmtDuration(key.seconds_to_reset)}}<br><span class="muted mini">${{fmtTime(key.reset_at)}}</span></td>
      <td>${{test}}${{err}}<br><span class="muted mini">${{fmtNumber(key.last_test_model_count || 0)}} modelos listados</span></td>
    </tr>`;
  }}).join('');
}}

function renderLiveMetrics(data) {{
  renderLiveQuota(data.key_store || {{}});
  const models = data.models || [];
  const active = data.active || [];
  document.getElementById('liveActiveCount').textContent = fmtNumber(active.length);
  const running = active.slice().sort((a, b) => Number(b.started_at || 0) - Number(a.started_at || 0))[0];
  const latest = models.slice().sort((a, b) => Number(b.last_finished_at || 0) - Number(a.last_finished_at || 0))[0];
  document.getElementById('liveLastTps').textContent = running
    ? liveFmtTps(running.live_tokens_per_second, !String(running.live_tps_basis || '').startsWith('exato'))
    : (latest ? liveFmtTps(latest.last_tokens_per_second, Boolean(latest.last_tps_estimated)) : '-');
  const completedTokens = models.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
  const activeTokens = active.reduce((sum, item) => sum + Number(item.total_tokens_live || 0), 0);
  document.getElementById('liveTotalTokens').textContent = fmtNumber(completedTokens + activeTokens);
  document.getElementById('liveUpdatedAt').textContent = new Date().toLocaleTimeString('pt-BR');

  const activeBox = document.getElementById('liveActiveRequests');
  activeBox.innerHTML = active.length ? active.map(item =>
    `<span class="pill">${{escapeHtml(item.model || 'desconhecido')}}</span> ` +
    `${{escapeHtml(item.key_name || 'conta em seleção')}} — ${{Number(item.elapsed_seconds || 0).toFixed(1)}}s — ` +
    `~${{fmtNumber(item.total_tokens_live || 0)}} tokens — ` +
    `${{liveFmtTps(item.live_tokens_per_second, !String(item.live_tps_basis || '').startsWith('exato'))}}` +
    `<div class="muted mini">${{escapeHtml(item.live_tps_basis || 'aguardando fragmentos')}}</div>`
  ).join('<br>') : '<span class="muted">Nenhuma requisição ativa.</span>';

  document.getElementById('liveModelRows').innerHTML = models.map(item => {{
    const isActive = Number(item.active_requests || 0) > 0;
    const shownTps = isActive ? item.current_tokens_per_second : item.last_tokens_per_second;
    const basis = isActive ? item.current_tps_basis : item.last_tps_basis;
    const estimated = isActive ? !String(basis || '').startsWith('exato') : Boolean(item.last_tps_estimated);
    const shownTotal = Number(item.total_tokens || 0) + Number(item.current_tokens || 0);
    return `<tr>
      <td><code>${{escapeHtml(item.model || 'desconhecido')}}</code>${{isActive ? '<br><span class="warn mini">gerando agora</span>' : ''}}</td>
      <td>${{escapeHtml(item.last_key_name || '')}}</td>
      <td>${{fmtNumber(item.requests || 0)}}</td>
      <td>${{fmtNumber(item.prompt_tokens || 0)}}</td>
      <td>${{fmtNumber(item.completion_tokens || 0)}}</td>
      <td>${{fmtNumber(shownTotal)}}</td>
      <td title="${{escapeHtml(basis || '')}}">${{liveFmtTps(shownTps, estimated)}}</td>
      <td>${{liveFmtTps(item.average_tokens_per_second, true)}}</td>
      <td>${{item.last_duration_seconds ? Number(item.last_duration_seconds).toFixed(2) + 's' : '-'}}</td>
      <td>${{item.last_finished_at ? fmtTime(item.last_finished_at) : '-'}}</td>
    </tr>`;
  }}).join('') || '<tr><td colspan="10" class="muted">As métricas aparecerão quando o modelo enviar o primeiro fragmento.</td></tr>';

  document.getElementById('liveRecentRequests').textContent = pretty((data.recent || []).slice(0, 10));
}}

async function refreshLiveMetrics() {{
  if (liveMetricsBusy || document.visibilityState === 'hidden') return;
  liveMetricsBusy = true;
  try {{
    const data = await api('/api/live-metrics');
    renderLiveMetrics(data);
  }} catch (err) {{
    const updated = document.getElementById('liveUpdatedAt');
    if (updated) updated.textContent = 'erro';
  }} finally {{
    liveMetricsBusy = false;
  }}
}}

refreshLiveMetrics();
setInterval(refreshLiveMetrics, {_REFRESH_MS});
document.addEventListener('visibilitychange', () => {{ if (document.visibilityState === 'visible') refreshLiveMetrics(); }});
// END LIVE_METRICS_UI
'''
    index_html = index_html.replace("</script>", js + "\n</script>", 1)
    return index_html


def install_live_metrics(Handler, index_html, globals_dict):
    _load_state()

    original_prepare = globals_dict.get("prepare_direct_cloud_body")
    if original_prepare and not getattr(original_prepare, "_live_metrics_wrapped", False):
        def prepare_direct_cloud_body(body, content_type=""):
            prepared = original_prepare(body, content_type)
            _update_active_from_body(prepared)
            try:
                if prepared and "json" in str(content_type).lower():
                    payload = json.loads(prepared.decode("utf-8") if isinstance(prepared, (bytes, bytearray)) else str(prepared))
                    if isinstance(payload, dict) and payload.get("stream") is True:
                        stream_options = payload.get("stream_options")
                        if not isinstance(stream_options, dict):
                            stream_options = {}
                        stream_options["include_usage"] = True
                        payload["stream_options"] = stream_options
                        prepared = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except Exception:
                pass
            return prepared
        prepare_direct_cloud_body._live_metrics_wrapped = True
        globals_dict["prepare_direct_cloud_body"] = prepare_direct_cloud_body

    original_extract = globals_dict.get("extract_usage_tokens")
    if original_extract and not getattr(original_extract, "_live_metrics_wrapped", False):
        def extract_usage_tokens(raw_body, content_type=""):
            details = _usage_details(raw_body, content_type)
            _merge_usage_into_thread(details)
            return original_extract(raw_body, content_type)
        extract_usage_tokens._live_metrics_wrapped = True
        globals_dict["extract_usage_tokens"] = extract_usage_tokens

    original_record = globals_dict.get("record_key_usage")
    if original_record and not getattr(original_record, "_live_metrics_wrapped", False):
        def record_key_usage(name, tokens):
            result = original_record(name, tokens)
            _finish_request(name, tokens)
            return result
        record_key_usage._live_metrics_wrapped = True
        globals_dict["record_key_usage"] = record_key_usage

    original_send_header = Handler.send_header
    if not getattr(original_send_header, "_live_metrics_wrapped", False):
        def send_header(self, keyword, value):
            if str(keyword).lower() == "x-manager-key-name" and getattr(_THREAD, "request_id", None):
                _THREAD.key_name = str(value or "")
                with _LOCK:
                    item = _STATE["active"].get(_THREAD.request_id)
                    if item:
                        item["key_name"] = _THREAD.key_name
            return original_send_header(self, keyword, value)
        send_header._live_metrics_wrapped = True
        Handler.send_header = send_header

    original_proxy = Handler.proxy
    if not getattr(original_proxy, "_live_metrics_wrapped", False):
        def proxy(self, prefix):
            _start_request(self, prefix, globals_dict)
            original_wfile = self.wfile
            self.wfile = _MetricsWriter(original_wfile)
            try:
                return original_proxy(self, prefix)
            finally:
                self.wfile = original_wfile
                if not getattr(_THREAD, "completed", False):
                    request_id = getattr(_THREAD, "request_id", None)
                    with _LOCK:
                        active = dict(_STATE["active"].get(request_id) or {}) if request_id else {}
                    estimated_total = _to_int(active.get("prompt_tokens_estimated")) + _to_int(active.get("output_tokens_estimated"))
                    if _to_int(active.get("output_tokens_estimated")) > 0:
                        selected_key = getattr(_THREAD, "key_name", "") or active.get("key_name")
                        try:
                            if original_record:
                                original_record(selected_key, estimated_total)
                        except Exception as exc:
                            print(f"live metrics: falha registrando uso estimado: {exc}", flush=True)
                        _finish_request(selected_key, estimated_total, force_estimate=True)
                    else:
                        _finish_without_usage()
                for field in ("request_id", "started_at", "model", "key_name", "usage_details", "completed"):
                    if hasattr(_THREAD, field):
                        delattr(_THREAD, field)
        proxy._live_metrics_wrapped = True
        Handler.proxy = proxy

    original_get = Handler.do_GET
    if not getattr(original_get, "_live_metrics_wrapped", False):
        def do_GET(self):
            if urlsplit(self.path).path == "/api/live-metrics":
                if not self.require_basic_auth():
                    return
                self.send_json(200, _snapshot(globals_dict))
                return
            return original_get(self)
        do_GET._live_metrics_wrapped = True
        Handler.do_GET = do_GET

    return Handler, _inject_html(index_html)
