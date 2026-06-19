import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path


def install_features(BaseHandler, index_html, namespace):
    load_key_store = namespace["load_key_store"]
    save_key_store = namespace["save_key_store"]
    normalize_key_item = namespace.get("normalize_key_item")
    read_json = namespace["read_json"]
    to_int = namespace.get("to_int", lambda value, default=0: default)
    to_float = namespace.get("to_float", lambda value, default=0.0: default)

    settings_file = Path(os.environ.get("EXTERNAL_API_SETTINGS_FILE", "/data/external-api.json"))

    def save_external_settings(settings):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        safe = {
            "enabled": bool(settings.get("enabled", False)),
            "api_key": str(settings.get("api_key") or secrets.token_urlsafe(32)),
            "updated_at": int(time.time()),
        }
        temporary = settings_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, settings_file)
        return safe

    def load_external_settings():
        if settings_file.exists():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        if not data.get("api_key"):
            data["api_key"] = secrets.token_urlsafe(32)
            data = save_external_settings(data)
        return {
            "enabled": bool(data.get("enabled", False)),
            "api_key": str(data.get("api_key")),
            "updated_at": data.get("updated_at"),
        }

    def bool_value(value, default=True):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}

    def export_keys_text():
        store = load_key_store()
        lines = [
            "# Ollama Agent Manager - backup de API keys",
            "# Formato: nome<TAB>chave<TAB>ativa<TAB>limite<TAB>usados<TAB>reset_horas<TAB>reset_at",
        ]
        for item in store.get("keys", []):
            values = [
                str(item.get("name") or ""),
                str(item.get("value") or ""),
                "true" if item.get("enabled", True) else "false",
                str(item.get("quota_limit_tokens") or 0),
                str(item.get("used_tokens") or 0),
                str(item.get("reset_period_hours") or 0),
                str(item.get("reset_at") or ""),
            ]
            lines.append("\t".join(values))
        return "\n".join(lines) + "\n"

    def next_manager_number(names):
        highest = 0
        for name in names:
            lowered = str(name).strip().lower()
            if lowered == "manager":
                highest = max(highest, 1)
                continue
            if lowered.startswith("manager "):
                try:
                    highest = max(highest, int(lowered.split()[-1]))
                except ValueError:
                    pass
        return highest + 1

    def import_keys_text(text):
        store = load_key_store()
        keys = list(store.get("keys", []))
        by_name = {str(item.get("name") or ""): item for item in keys}
        auto_number = next_manager_number(by_name)
        imported = 0
        updated = 0
        skipped = 0

        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            columns = []
            if "\t" in line:
                columns = [part.strip() for part in line.split("\t")]
            elif "|" in line:
                columns = [part.strip() for part in line.split("|")]
            elif "=" in line:
                name, value = line.split("=", 1)
                columns = [name.strip(), value.strip()]
            else:
                columns = [f"manager {auto_number}", line]
                auto_number += 1

            name = columns[0] if columns else ""
            value = columns[1] if len(columns) > 1 else ""
            if not name or not value:
                skipped += 1
                continue

            existing = by_name.get(name)
            payload = {
                "name": name,
                "value": value,
                "enabled": bool_value(columns[2], True) if len(columns) > 2 else True,
            }
            if len(columns) > 3 and columns[3] != "":
                payload["quota_limit_tokens"] = to_int(columns[3], 0)
            if len(columns) > 4 and columns[4] != "":
                payload["used_tokens"] = to_int(columns[4], 0)
            if len(columns) > 5 and columns[5] != "":
                payload["reset_period_hours"] = to_float(columns[5], 0.0)
            if len(columns) > 6 and columns[6] != "":
                payload["reset_at"] = columns[6]

            normalized = normalize_key_item(payload, existing) if normalize_key_item else payload
            if existing:
                position = keys.index(existing)
                keys[position] = normalized
                updated += 1
            else:
                keys.append(normalized)
                imported += 1
            by_name[name] = normalized

        store["keys"] = keys
        if not store.get("active_key") and keys:
            store["active_key"] = keys[0].get("name")
        saved = save_key_store(store)
        return {
            "ok": True,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "total": len(saved.get("keys", [])),
        }

    extra_buttons = '''
        <input id="importApiKeysFile" type="file" accept=".txt,text/plain" style="display:none" onchange="importApiKeysFile(this)">
        <button onclick="exportApiKeys()">Exportar chaves TXT</button>
        <button onclick="document.getElementById('importApiKeysFile').click()">Importar chaves TXT</button>'''
    anchor = '        <button onclick="deleteSelectedKey()">Excluir chave ativa</button>'
    if "Exportar chaves TXT" not in index_html and anchor in index_html:
        index_html = index_html.replace(anchor, anchor + extra_buttons, 1)

    external_card = '''
    <section class="card">
      <h2>Servidor API externo</h2>
      <p class="muted">Expõe esta ferramenta como uma API compatível com OpenAI para outras aplicações. O roteamento de contas, modelos e cotas continua sendo feito pelo Manager.</p>
      <div class="row">
        <button id="externalApiToggle" onclick="toggleExternalApi()">Carregando...</button>
        <button onclick="regenerateExternalApiKey()">Gerar nova chave de acesso</button>
        <button onclick="copyExternalApiUrl()">Copiar URL</button>
        <button onclick="copyExternalApiKey()">Copiar chave</button>
      </div>
      <div class="row">
        <label>Base URL <input id="externalApiUrl" size="55" readonly></label>
        <label>API key <input id="externalApiKey" type="password" size="44" readonly></label>
      </div>
      <pre id="externalApiInfo">Carregando...</pre>
    </section>

'''
    result_anchor = '    <section class="card">\n      <h2>Resultado</h2>'
    if "Servidor API externo" not in index_html and result_anchor in index_html:
        index_html = index_html.replace(result_anchor, external_card + result_anchor, 1)

    javascript = r'''
async function exportApiKeys() {
  document.getElementById('result').textContent = 'Exportando chaves...';
  try {
    const response = await fetch('/api/export-api-keys', { credentials: 'same-origin' });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `ollama-api-keys-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    document.getElementById('result').textContent = 'Arquivo TXT exportado.';
  } catch (error) {
    document.getElementById('result').textContent = String(error);
  }
}

async function importApiKeysFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = await api('/api/import-api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    document.getElementById('result').textContent = pretty(data);
    await loadStatus();
  } catch (error) {
    document.getElementById('result').textContent = String(error);
  } finally {
    input.value = '';
  }
}

async function loadExternalApi() {
  try {
    const data = await api('/api/external-api');
    window.externalApiState = data;
    document.getElementById('externalApiUrl').value = data.base_url || '';
    document.getElementById('externalApiKey').value = data.api_key || '';
    document.getElementById('externalApiToggle').textContent = data.enabled ? 'Desativar API externa' : 'Ativar API externa';
    document.getElementById('externalApiInfo').textContent = pretty({
      enabled: data.enabled,
      base_url: data.base_url,
      models_endpoint: `${data.base_url}/models`,
      chat_endpoint: `${data.base_url}/chat/completions`,
      authentication: 'Authorization: Bearer <API_KEY>'
    });
  } catch (error) {
    document.getElementById('externalApiInfo').textContent = String(error);
  }
}

async function toggleExternalApi() {
  const current = window.externalApiState || {};
  const data = await api('/api/external-api', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !current.enabled })
  });
  window.externalApiState = data;
  await loadExternalApi();
  document.getElementById('result').textContent = data.enabled ? 'API externa ativada.' : 'API externa desativada.';
}

async function regenerateExternalApiKey() {
  if (!confirm('Gerar uma nova chave? Aplicações que usam a chave atual deixarão de funcionar.')) return;
  const data = await api('/api/external-api', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ regenerate: true })
  });
  window.externalApiState = data;
  await loadExternalApi();
  document.getElementById('result').textContent = 'Nova chave externa gerada.';
}

async function copyValue(id) {
  const value = document.getElementById(id).value;
  await navigator.clipboard.writeText(value);
  document.getElementById('result').textContent = 'Copiado.';
}
function copyExternalApiUrl() { return copyValue('externalApiUrl'); }
function copyExternalApiKey() { return copyValue('externalApiKey'); }
'''
    js_anchor = "document.getElementById('activeKey').addEventListener('change', fillKeyFormFromSelection);"
    if "async function exportApiKeys()" not in index_html and js_anchor in index_html:
        index_html = index_html.replace(js_anchor, javascript + "\n" + js_anchor, 1)
    if "loadStatus();\nloadExternalApi();" not in index_html:
        index_html = index_html.replace("loadStatus();\n</script>", "loadStatus();\nloadExternalApi();\n</script>", 1)

    class FeatureHandler(BaseHandler):
        def _path_only(self):
            return urllib.parse.urlsplit(self.path).path

        def _external_url(self):
            proto = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "127.0.0.1:3005"
            return f"{proto}://{host}/v1"

        def _external_status(self):
            settings = load_external_settings()
            return {
                "enabled": settings["enabled"],
                "api_key": settings["api_key"],
                "base_url": self._external_url(),
                "updated_at": settings.get("updated_at"),
            }

        def require_basic_auth(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                return True
            return super().require_basic_auth()

        def check_proxy_auth(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                settings = load_external_settings()
                if not settings.get("enabled"):
                    return False
                return self.headers.get("Authorization", "") == f"Bearer {settings.get('api_key')}"
            return super().check_proxy_auth()

        def end_headers(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_OPTIONS(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_external_proxy(self):
            settings = load_external_settings()
            if not settings.get("enabled"):
                self.send_json(403, {"error": "API externa desativada no Manager."})
                return
            self.proxy("")

        def do_GET(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                self._serve_external_proxy()
                return
            if path in ("/api/export-api-keys", "/api/external-api"):
                if not self.require_basic_auth():
                    return
                if path == "/api/external-api":
                    self.send_json(200, self._external_status())
                    return
                body = export_keys_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="ollama-api-keys.txt"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

        def do_POST(self):
            path = self._path_only()
            if path == "/v1" or path.startswith("/v1/"):
                self._serve_external_proxy()
                return
            if path in ("/api/import-api-keys", "/api/external-api"):
                if not self.require_basic_auth():
                    return
                payload = read_json(self)
                if path == "/api/import-api-keys":
                    self.send_json(200, import_keys_text(payload.get("text") or ""))
                    return
                settings = load_external_settings()
                if "enabled" in payload:
                    settings["enabled"] = bool(payload.get("enabled"))
                if payload.get("regenerate"):
                    settings["api_key"] = secrets.token_urlsafe(32)
                settings = save_external_settings(settings)
                self.send_json(200, self._external_status())
                return
            return super().do_POST()

    return FeatureHandler, index_html
