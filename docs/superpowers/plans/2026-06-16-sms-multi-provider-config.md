# SMS Multi-Provider Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store SMSBower and Hero-SMS as independent provider configs, use the selected provider for registration, and query all configured providers on the SMS price check page.

**Architecture:** Normalize all loaded config through `auto_register.load_config()` so `sms.providers` is the canonical multi-provider store while legacy `sms.*` fields remain synced to the active provider. `web_gui.py` saves and displays one active provider at a time, and `/api/sms-price-check` iterates every provider with an API key.

**Tech Stack:** Python 3, Flask, vanilla JavaScript, JSON config file, existing unittest tests.

---

### Task 1: Normalize multi-provider SMS config in `auto_register.load_config()`

**Files:**
- Modify: `auto_register.py`
- Modify: `test_auto_register_retry.py`

- [ ] **Step 1: Write the failing tests**

Add tests to `test_auto_register_retry.py`:

```python
    def test_load_config_migrates_legacy_sms_config_into_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "sms": {
                            "provider": "hero-sms",
                            "api_key": "hero-key",
                            "countries": ["4", "16"],
                            "service": "dr",
                            "operator": "any",
                            "max_price": "0.025",
                        },
                        "register": {"password": "pw", "name": "A", "birthdate": "2000-01-01"},
                    },
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )

            cfg = ar.load_config(str(path))

        self.assertEqual(cfg["sms"]["active_provider"], "hero-sms")
        self.assertEqual(cfg["sms"]["provider"], "hero-sms")
        self.assertEqual(cfg["sms"]["api_key"], "hero-key")
        self.assertEqual(cfg["sms"]["countries"], ["4", "16"])
        self.assertEqual(cfg["sms"]["providers"]["hero-sms"]["api_key"], "hero-key")
        self.assertEqual(cfg["sms"]["providers"]["hero-sms"]["countries"], ["4", "16"])
        self.assertIn("smsbower", cfg["sms"]["providers"])

    def test_load_config_uses_active_provider_from_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "sms": {
                            "active_provider": "smsbower",
                            "providers": {
                                "smsbower": {"api_key": "bower-key", "countries": ["151"], "service": "dr", "operator": "any", "max_price": "0.03"},
                                "hero-sms": {"api_key": "hero-key", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.025"},
                            },
                        },
                        "register": {"password": "pw", "name": "A", "birthdate": "2000-01-01"},
                    },
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )

            cfg = ar.load_config(str(path))

        self.assertEqual(cfg["sms"]["provider"], "smsbower")
        self.assertEqual(cfg["sms"]["api_key"], "bower-key")
        self.assertEqual(cfg["sms"]["countries"], ["151"])
        self.assertEqual(cfg["sms"]["max_price"], "0.03")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_auto_register_retry.AutoRegisterRetryTests.test_load_config_migrates_legacy_sms_config_into_providers test_auto_register_retry.AutoRegisterRetryTests.test_load_config_uses_active_provider_from_providers`

Expected: FAIL because `sms.providers` and `sms.active_provider` are not normalized yet.

- [ ] **Step 3: Implement minimal config normalization**

In `auto_register.py`, add helper functions near config loading:

```python
_SUPPORTED_SMS_PROVIDERS = ("smsbower", "hero-sms")


def _default_sms_provider_config(provider: str) -> dict:
    return {
        "api_key": "",
        "countries": [],
        "service": "dr",
        "operator": "any",
        "max_price": DEFAULT_SMS_MAX_PRICE,
    }


def _normalize_sms_config(sms_cfg: dict, legacy_found: dict | None = None) -> dict:
    legacy_found = legacy_found or {}
    active_provider = str(sms_cfg.get("active_provider") or sms_cfg.get("provider") or "smsbower")
    if active_provider not in _SUPPORTED_SMS_PROVIDERS:
        active_provider = "smsbower"

    providers = {}
    for provider in _SUPPORTED_SMS_PROVIDERS:
        provider_cfg = dict(_default_sms_provider_config(provider))
        provider_cfg.update(dict((sms_cfg.get("providers") or {}).get(provider) or {}))
        provider_cfg["countries"] = parse_countries(provider_cfg.get("countries"))
        providers[provider] = provider_cfg

    active_provider_cfg = providers[active_provider]
    for key in ("api_key", "countries", "service", "operator", "max_price"):
        if key in sms_cfg and sms_cfg.get(key) not in (None, "", []):
            active_provider_cfg[key] = sms_cfg[key]
    active_provider_cfg["countries"] = parse_countries(active_provider_cfg.get("countries"))

    if "smsbower" in legacy_found:
        providers["smsbower"]["api_key"] = legacy_found["smsbower"].get("api_key", providers["smsbower"].get("api_key", ""))
        if legacy_found.get("country") and not providers["smsbower"].get("countries"):
            providers["smsbower"]["countries"] = parse_countries(legacy_found.get("country"))

    current = dict(providers[active_provider])
    return {
        "active_provider": active_provider,
        "provider": active_provider,
        "providers": providers,
        "api_key": current.get("api_key", ""),
        "countries": parse_countries(current.get("countries")),
        "service": current.get("service", "dr"),
        "operator": current.get("operator", "any"),
        "max_price": str(current.get("max_price") or DEFAULT_SMS_MAX_PRICE),
    }
```

Call `_normalize_sms_config(config["sms"], found)` before the existing country validation in `load_config()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_auto_register_retry.py`

Expected: PASS.

---

### Task 2: Update Web GUI config save/load to preserve per-provider settings

**Files:**
- Modify: `web_gui.py`
- Modify: `test_web_gui_stats.py`

- [ ] **Step 1: Write the failing test**

Add to `test_web_gui_stats.py`:

```python
    def test_api_config_saves_only_active_sms_provider(self):
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "sms": {
                        "active_provider": "hero-sms",
                        "providers": {
                            "hero-sms": {"api_key": "hero-old", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.025"},
                            "smsbower": {"api_key": "bower-old", "countries": ["151"], "service": "dr", "operator": "any", "max_price": "0.03"},
                        },
                    },
                    "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json={"sms_provider": "smsbower", "api_key": "bower-new", "countries": "33,151", "max_price": "0.02"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        sms = data["config"]["sms"]
        self.assertEqual(sms["active_provider"], "smsbower")
        self.assertEqual(sms["provider"], "smsbower")
        self.assertEqual(sms["api_key"], "bower-new")
        self.assertEqual(sms["countries"], ["33", "151"])
        self.assertEqual(sms["providers"]["smsbower"]["api_key"], "bower-new")
        self.assertEqual(sms["providers"]["smsbower"]["countries"], ["33", "151"])
        self.assertEqual(sms["providers"]["hero-sms"]["api_key"], "hero-old")
        self.assertEqual(sms["providers"]["hero-sms"]["countries"], ["4"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest test_web_gui_stats.WebGuiStatsTests.test_api_config_saves_only_active_sms_provider`

Expected: FAIL because `/api/config` currently writes only flat `sms.*` fields.

- [ ] **Step 3: Implement minimal Web GUI save/load changes**

In `web_gui.py` `api_config()` POST handling:

- Ensure `cfg["sms"]` has `providers` from `ar.load_config()`.
- Resolve `active_provider = d.get("sms_provider") or cfg["sms"].get("active_provider") or cfg["sms"].get("provider") or "smsbower"`.
- Write posted SMS fields into `cfg["sms"]["providers"][active_provider]` only.
- Sync current provider into flat compatibility fields:

```python
active_sms = cfg["sms"]["providers"][active_provider]
cfg["sms"].update({
    "active_provider": active_provider,
    "provider": active_provider,
    "api_key": active_sms.get("api_key", ""),
    "countries": active_sms.get("countries", []),
    "service": active_sms.get("service", "dr"),
    "operator": active_sms.get("operator", "any"),
    "max_price": active_sms.get("max_price", ar.DEFAULT_SMS_MAX_PRICE),
})
```

Keep existing legacy fields `sms_api_key`, `sms_countries`, `sms_service`, `sms_max_price` synced from current active provider.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_web_gui_stats.py`

Expected: PASS.

---

### Task 3: Make the frontend switch provider fields from saved per-provider configs

**Files:**
- Modify: `web_gui.py`
- Test: `test_web_gui_stats.py`

- [ ] **Step 1: Write the failing test**

Extend `test_sms_price_check_page_exists_with_two_filterable_tables` or add a focused HTML test:

```python
    def test_main_page_has_provider_switch_loader(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("function applySmsProviderConfig", html)
        self.assertIn("onchange=\"applySmsProviderConfig()\"", html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest test_web_gui_stats.WebGuiStatsTests.test_main_page_has_provider_switch_loader`

Expected: FAIL because the dropdown does not call the provider config loader yet.

- [ ] **Step 3: Implement minimal frontend provider switch**

In `_HTML`:

- Change the SMS provider select to:

```html
<select id="sms_provider" onchange="applySmsProviderConfig()" ...>
```

- In JS, add a global copy of loaded config and a function:

```javascript
var loadedConfig=null;
function applySmsProviderConfig(){
  var cfg=loadedConfig||{};
  var provider=G('sms_provider').value||'smsbower';
  var p=((cfg.sms||{}).providers||{})[provider]||{};
  G('api_key').value=p.api_key||'';
  G('countries').value=(p.countries||[]).join(',');
  G('service').value=p.service||'dr';
  G('max_price').value=p.max_price||'0.03';
}
```

Update the existing `loadConfig()` code so it sets `loadedConfig=c` before filling fields, then uses active provider config when available.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_web_gui_stats.py`

Expected: PASS.

---

### Task 4: Query all configured providers in SMS price check API

**Files:**
- Modify: `web_gui.py`
- Modify: `test_web_gui_stats.py`

- [ ] **Step 1: Write the failing test**

Add to `test_web_gui_stats.py`:

```python
    def test_sms_price_check_api_queries_all_configured_sms_providers(self):
        web_gui._state["config"] = {
            "sms": {
                "active_provider": "hero-sms",
                "provider": "hero-sms",
                "providers": {
                    "hero-sms": {"api_key": "hero-key", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.03"},
                    "smsbower": {"api_key": "bower-key", "countries": ["151"], "service": "dr", "operator": "any", "max_price": "0.02"},
                },
            }
        }

        class FakeSMS:
            def __init__(self, provider, api_key):
                self.provider = provider
                self.api_key = api_key

            def get_price_catalog(self, service="dr"):
                return [{"sms_provider": self.provider, "country": "4" if self.provider == "hero-sms" else "151", "lowest_price": 0.02, "second_price": None, "lowest_count": 3, "second_count": 0}]

        with mock.patch.object(web_gui, "UnifiedSMS", FakeSMS):
            with mock.patch.object(web_gui, "load_success_price_rows", return_value=[]):
                with web_gui.app.test_client() as client:
                    resp = client.get("/api/sms-price-check")

        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        providers = {row["sms_provider"] for row in payload["price_rows"]}
        self.assertEqual(providers, {"hero-sms", "smsbower"})
        by_provider = {row["sms_provider"]: row for row in payload["price_rows"]}
        self.assertTrue(by_provider["hero-sms"]["in_purchase_pool"])
        self.assertTrue(by_provider["smsbower"]["in_purchase_pool"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest test_web_gui_stats.WebGuiStatsTests.test_sms_price_check_api_queries_all_configured_sms_providers`

Expected: FAIL because the API currently queries only the active/current provider.

- [ ] **Step 3: Implement all-provider query**

Update `/api/sms-price-check` in `web_gui.py`:

- Read `providers = cfg["sms"].get("providers", {})`.
- For each provider config with `api_key`, call `UnifiedSMS(provider=provider, api_key=api_key).get_price_catalog(service=provider_cfg.get("service", "dr"))`.
- Build price rows per provider with that provider's own `countries` and `max_price`.
- Combine all rows into one `price_rows` list.
- Return `configured_countries` as a mapping by provider:

```python
"configured_countries": {provider: provider_cfg.get("countries", []) for provider, provider_cfg in providers.items()}
```

Keep API key and passwords out of the response.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_web_gui_stats.py`

Expected: PASS.

---

### Task 5: Verify full SMS config and price-check flow

**Files:**
- No planned code changes unless verification finds a concrete bug.

- [ ] **Step 1: Run targeted suite**

Run:

```bash
python3 -m unittest test_auto_register_retry.py test_phone_sms_adapter.py test_sms_price_check.py test_web_gui_stats.py
```

Expected: all tests pass.

- [ ] **Step 2: Restart Web GUI**

Run:

```bash
bash stop_webui.sh && bash start_webui.sh
```

Expected: script prints `WebUI started` and URL `http://127.0.0.1:7778`.

- [ ] **Step 3: Smoke test price check API**

Run:

```bash
python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:7778/api/sms-price-check', timeout=30) as r:
    data = json.loads(r.read().decode())
print(data['ok'])
print(sorted({row['sms_provider'] for row in data['price_rows']}))
print(len(data['price_rows']))
PY
```

Expected: first line is `True`; providers include every configured platform with API key; row count is greater than zero when at least one configured API key can query prices.
