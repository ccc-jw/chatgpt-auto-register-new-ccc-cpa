# SMS Price Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Web GUI page that shows all OpenAI SMS country prices, ranks recommendation for buying pools, and records successful phone-registration prices for later analysis.

**Architecture:** Keep the heavy logic out of `web_gui.py` by introducing a small shared helper module for SMS price analytics and persistent success-price storage. `auto_register.py` records successful SMS purchases, `web_gui.py` only renders the page and serves API data, and `phone_sms_adapter.py` exposes price-catalog helpers for all-country queries and per-country price breakdowns.

**Tech Stack:** Python 3, Flask, vanilla JavaScript, JSON file storage, existing `UnifiedSMS` adapter, existing pytest/unittest test style.

---

### Task 1: Add shared SMS price analytics helpers

**Files:**
- Create: `sms_price_check.py`
- Test: `test_sms_price_check.py`

- [ ] **Step 1: Write the failing test**

```python
from sms_price_check import load_success_price_rows, record_success_price, build_recommendation


def test_record_success_price_groups_by_provider_country_price(tmp_path):
    path = tmp_path / "sms_success_prices.json"
    rows = load_success_price_rows(path)
    rows = record_success_price(rows, "smsbower", "4", "0.027")
    rows = record_success_price(rows, "smsbower", "4", "0.027")
    assert rows == [{
        "sms_provider": "smsbower",
        "country": "4",
        "success_price": "0.027",
        "success_count": 2,
    }]


def test_build_recommendation_prefers_low_price_with_history():
    row = build_recommendation(
        configured_countries={"4"},
        country="52",
        lowest_price=0.025,
        second_price=0.031,
        lowest_count=8,
        second_count=3,
        history_success_count=12,
        history_success_price="0.026",
        max_price="0.03",
    )
    assert row["recommendation"].startswith("推荐")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_sms_price_check.py -v`
Expected: fail because `sms_price_check.py` does not exist yet.

- [ ] **Step 3: Write the minimal helper implementation**

Implement the helper module with these responsibilities only:

```python
def load_success_price_rows(path):
    """Read the JSON file or return []."""


def save_success_price_rows(path, rows):
    """Persist rows back to JSON."""


def record_success_price(rows, sms_provider, country, success_price):
    """Increment success_count for an existing (provider, country, price) bucket."""


def build_price_rows(price_catalog, configured_countries, success_rows, max_price):
    """Merge platform prices, purchase-pool membership, and history into table rows."""


def build_recommendation(...):
    """Return a dict with recommendation text and sortable scoring fields."""
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest test_sms_price_check.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_price_check.py test_sms_price_check.py
git commit -m "feat: add sms price analytics helpers"
```

---

### Task 2: Extend SMS adapter price catalog support

**Files:**
- Modify: `phone_sms_adapter.py`
- Test: `test_phone_sms_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
from phone_sms_adapter import UnifiedSMS


def test_price_catalog_returns_country_entries_for_all_prices(monkeypatch):
    sms = UnifiedSMS(provider="smsbower", api_key="k")

    def fake_get(url, params=None, timeout=0, verify=True):
        class Resp:
            def json(self):
                return {
                    "4": {"dr": {"p1": {"price": 0.02}, "p2": {"price": 0.03}}},
                    "16": {"dr": {"p3": {"price": 0.05}}},
                }
        return Resp()

    monkeypatch.setattr("phone_sms_adapter.requests.get", fake_get)
    rows = sms.get_price_catalog(service="dr")
    assert rows[0]["country"] == "4"
    assert rows[0]["lowest_price"] == 0.02
    assert rows[0]["second_price"] == 0.03
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_phone_sms_adapter.py -v`
Expected: fail because `get_price_catalog()` does not exist yet.

- [ ] **Step 3: Implement the adapter method**

Add a method on `UnifiedSMS` that queries the SMS provider price endpoint for the OpenAI service and normalizes each country into a row with:

```python
{
    "sms_provider": self.provider,
    "country": country_id,
    "lowest_price": lowest_price,
    "second_price": second_price,
    "lowest_count": lowest_count,
    "second_count": second_count,
}
```

Keep `get_sorted_countries_by_price()` intact for existing callers; this new method is only for the price-check page.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest test_phone_sms_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add phone_sms_adapter.py test_phone_sms_adapter.py
git commit -m "feat: expose sms country price catalog"
```

---

### Task 3: Record successful SMS prices in the registration flow

**Files:**
- Modify: `auto_register.py`
- Modify: `runner.py`
- Test: `test_auto_register_retry.py` or a new `test_sms_success_price_recording.py`

- [ ] **Step 1: Write the failing test**

```python
from auto_register import register_one


def test_register_one_returns_sms_price_on_phone_success(monkeypatch):
    class FakeSms:
        provider = "smsbower"
        def get_cheapest_provider(self, service="dr", country="4"):
            return "p1", 0.027
        def get_number(self, service="dr", country="4", provider_ids="", max_price=""):
            return "aid-1", "15550000000"

    result = register_one(FakeSms(), {
        "sms": {"provider": "smsbower", "api_key": "k", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.03"},
        "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
        "proxy": "",
        "code_timeout": 30,
    }, no_phase2=True, verbose=False)

    assert result["phone_ok"] is True
    assert result["sms_price"] == "0.027"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_auto_register_retry.py -v`
Expected: fail because the result does not yet include `sms_price` and the registration flow does not persist success-price stats.

- [ ] **Step 3: Implement the minimal flow change**

Update `_get_number_with_retry()` so it returns the chosen country plus the price that should be recorded for that activation. Thread that value through `register_one()` into the final result as `sms_price`.

Add a small helper in `auto_register.py` that appends the success record to the shared JSON file only when `phone_ok` is true:

```python
if phone_ok and sms_price:
    record_sms_success_price(
        sms_provider=sms_provider,
        country=used_country,
        success_price=sms_price,
    )
```

Keep the write best-effort so a stats-file failure does not change registration success/failure behavior.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest test_auto_register_retry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add auto_register.py runner.py test_auto_register_retry.py
git commit -m "feat: record sms success prices on phone success"
```

---

### Task 4: Add the SMS price check page and API

**Files:**
- Modify: `web_gui.py`
- Modify: `.gitignore`
- Test: `test_web_gui_stats.py` or a new `test_sms_price_check_web.py`

- [ ] **Step 1: Write the failing test**

```python
from web_gui import app


def test_sms_price_check_page_and_api():
    client = app.test_client()
    page = client.get("/sms-price-check")
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    assert "短信价格检查" in html
    assert "国家/地区id" in html
    assert "成功价格统计" in html
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_web_gui_stats.py -v`
Expected: fail because `/sms-price-check` and the new API do not exist yet.

- [ ] **Step 3: Implement the page and API**

Add:

- a new route for `/sms-price-check`
- a JSON API route that returns:
  - `price_rows`
  - `success_rows`
  - `configured_countries`
  - `errors`

Update the main template so the header buttons become:

```html
<button onclick="location.href='/'">下载结果</button>
<button onclick="location.href='/sms-price-check'">短信价格检查</button>
<button onclick="location.href='/balance'">余额</button>
```

Build the page with two tables and per-column filters in vanilla JS. Keep the table logic generic enough that all displayed columns can be filtered independently.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest test_web_gui_stats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_gui.py .gitignore test_web_gui_stats.py
git commit -m "feat: add sms price check page"
```

---

### Task 5: Verify the full flow end-to-end

**Files:**
- No new files expected; adjust only if tests uncover a gap.

- [ ] **Step 1: Run the targeted test suite**

Run:

```bash
pytest test_phone_sms_adapter.py test_sms_price_check.py test_auto_register_retry.py test_web_gui_stats.py -v
```

Expected: all pass.

- [ ] **Step 2: Run a focused manual check of the page**

Start the Web GUI and confirm:

- the new button appears between “下载结果” and “余额”
- `/sms-price-check` loads
- both tables render
- every column has a filter control
- success counts display and sort numerically

- [ ] **Step 3: Final commit if anything changed during verification**

```bash
git status --short
```

If verification changed code, commit only the final code/test fixes with a focused message.
