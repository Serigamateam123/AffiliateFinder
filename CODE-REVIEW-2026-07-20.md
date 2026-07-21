# AffiliateFinder — full code review (2026-07-20)

**Scope:** entire repository at commit `329ae99` ("Add files via upload") — `ui_server.py`,
`scraper.py`, `messenger.py`, `ui.html`, `README.md`, `requirements.txt`, launcher script.

**Method:** seven parallel review agents, one per dimension (correctness, failure modes,
concurrency, security, scraping robustness, frontend, docs drift), each finding then passed
to an independent adversarial verifier instructed to refute it against the actual code.
29 agents total. **22 findings raised, 22 confirmed, 0 refuted** (two were duplicates of the
same bug, so 21 distinct findings below, plus one minor extra from a manual read-through).
Severities shown are post-verification (two were downgraded by the verifier; noted inline).

## Executive summary

The happy path is solidly built. The recurring problem is that **almost every failure path
fails open and silent**: when something goes wrong — a parse miss, a corrupt file, a dead
browser, a changed TikTok DOM — the code substitutes a default that is indistinguishable
from real data and reports success. Concretely:

- The creator store can be **silently corrupted and read back as empty** (no lock, no atomic
  write, decode errors swallowed).
- A degraded re-harvest **permanently overwrites good data with zeros**.
- An unparseable GMV renders as a **confirmed exact RM0** — precisely the fabricated value
  the README promises never to show.
- The scraper **cannot distinguish "zero creators" from "I'm broken"** and reports both as
  success.
- Scraped, creator-controlled text is injected into the dashboard via `innerHTML`
  (**stored XSS**), and an unused CORS grant lets any script on `affiliate.tiktok.com`
  wipe or poison the store.

## Suggested fix order

**Batch 1 — data safety (small diffs):** store lock + atomic write with surfaced errors (1, 11);
move the scraper import inside the harvest try-block (2); guard the upsert against
parse-default clobbering (3); escape all scraped text in the DOM (5, 12); delete the CORS hook (6).

**Batch 2 — robustness:** distinguish parse failure from real zero in scraper and store
(4, 13); validate scraped handles and extract incrementally during scrolling (8, 9);
messenger restart path + honest state reporting (7); frontend poll/error handling (10, 15, 19).

**Batch 3 — docs & product:** rewrite the README's DM section and file/endpoint tables
(16, 17, 20, 21); decide whether follower/GMV filters should be closed bands or "at least"
floors (18) — the README promises floors, the code implements bands; this is a product
decision, not just a doc fix.

---

## Findings

### 1. [CRITICAL] save_creators()/load_creators() have no atomic write and silently discard corrupted data as an empty store
**Where:** `ui_server.py:76` · **Dimension:** concurrency · **Verified:** confirmed by adversarial verifier

save_creators() writes the full JSON via Path.write_text(), which truncates and writes in place -- no temp-file+rename, no fsync. If two threads call save_creators() at overlapping times (any pair of put_creators/clear_creators/_cache_user_id callers -- e.g. a CSV import racing a harvest completion, or two concurrent Message clicks caching ids), their write() calls can interleave on the same file descriptor and leave creators.json truncated or with mismatched/invalid JSON. load_creators() then catches json.JSONDecodeError and OSError and silently returns [] with no logging or error surfaced anywhere in the app.

**Failure scenario:** Two concurrent writers (e.g. a manual /api/import_csv paste while a harvest's put_creators() is finishing, per the interleavings above) both call save_creators() around the same moment. Their write() syscalls interleave on the shared file, producing invalid JSON. The very next GET /api/creators or /api/search calls load_creators(), hits JSONDecodeError, and the store silently appears to have 0 creators -- with no error message, no log line, and no indication to the team that the entire harvested dataset (potentially hundreds of rows built up over weeks) was just wiped by a race rather than an intentional Clear.

```
def load_creators():
    if not CREATORS_JSON.exists():
        return []
    try:
        return json.loads(CREATORS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_creators(rows):
    CREATORS_JSON.write_text(json.dumps(rows, indent=2), encoding="utf-8")
```

---

### 2. [HIGH] Harvest thread can crash before entering try/finally, leaving `_harvest["running"]` stuck True forever
**Where:** `ui_server.py:437` · **Dimension:** failure-modes · **Verified:** confirmed by adversarial verifier

> **Note:** Reviewer rated critical; verifier downgraded one step — the trigger (scraper import failing) is an edge case, but the stuck state is real and needs a restart to clear.

`_run_harvest` does `from scraper import harvest as do_harvest` as its very first statement, OUTSIDE the surrounding try/except/finally block that starts two lines later. `start_harvest()` sets `_harvest["running"] = True` and spawns this function on a daemon thread. If that late import raises (e.g. Playwright's browser binaries aren't installed yet — a very plausible first-run/venv-setup gap for a `playwright`-based tool — or `scraper.py` has any import-time error), the exception is never caught: it isn't inside the `try:` block, so the `except Exception` handler never runs and, critically, the `finally: _harvest["running"] = False` never runs either. The background thread dies silently (Python just logs the traceback to stderr) while `_harvest` is left at `{"running": True, "stage": "starting", ...}` permanently.

**Failure scenario:** User runs `Start Affiliate Finder.command` before ever running `playwright install`, clicks "Find creators". `from scraper import harvest` succeeds (module import is fine) but the first call into Playwright inside `harvest()` would normally raise — however even a simpler case suffices: any ImportError/SyntaxError surfacing from `scraper.py` at import time (e.g. a bad edit, a missing dependency version) fires before the try block. `_harvest["running"]` stays True forever. Every subsequent `POST /api/harvest` now 409s with "A harvest is already running", the UI's poll loop (`if (!s.running)`) never terminates so the Find-creators button stays disabled showing "Searching TikTok…" indefinitely, and `GET /api/harvest/status` never reports the real error — the only fix is restarting the whole Flask process.

```
def _run_harvest(region, target):
    from scraper import harvest as do_harvest   # imported late: Chrome only spins up on demand

    def progress(stage, detail):
        _harvest.update(stage=stage, detail=detail)

    try:
        rows = do_harvest(region=region, target=target, on_progress=progress)
        ...
    except Exception as exc:
        _harvest.update(stage="error", detail="", error=str(exc))
    finally:
        _harvest["running"] = False
```

---

### 3. [HIGH] Upsert-by-handle unconditionally overwrites existing fields with defaults, clobbering good data on a degraded scrape or partial CSV
**Where:** `ui_server.py:243` · **Dimension:** correctness · **Verified:** confirmed by adversarial verifier

put_creators() upserts via existing[row["handle"]].update(row), where row comes from normalize() which always returns the FULL data-contract dict (every missing/unparseable field defaulted to 0/""/False). Only tiktok_user_id is special-cased to avoid being clobbered by a blank (lines 241-242); every other field — followers, gmv, items_sold, category, etc. — is unconditionally overwritten. Since scraper.py's toNum() returns null->0 whenever a row's DOM cell fails to parse (e.g. the demoIdx regex on line 41 misses, or a cell layout shifts), a single degraded re-harvest of an existing handle silently zeroes out previously-correct followers/gmv/items_sold in the persisted store, with no way to recover the prior values.

**Failure scenario:** Verified directly: stored creator with followers=50000, gmv=45000.0, items_sold=1200 upserted with an incoming row where TikTok's DOM changed slightly so those three fields parsed as 0. Result after existing.update(row): followers=0, gmv=0.0, items_sold=0 while tiktok_user_id='abc123' survives (the only protected field). Running 'Find creators' again after any TikTok markup tweak permanently wipes real follower/GMV/items-sold data for previously-known creators, corrupting every downstream filter, sort and percentile score for them.

```
if row["handle"] in existing:
    # A re-harvest from Find creators has no tiktok_user_id, so don't let
    # its blank clobber an id we already resolved from the profile.
    if not row["tiktok_user_id"]:
        row.pop("tiktok_user_id")
    existing[row["handle"]].update(row)
    updated += 1
```

---

### 4. [HIGH] Unparseable/malformed GMV (or other numeric) fields silently become a fabricated exact 0 instead of being flagged unknown
**Where:** `ui_server.py:119` · **Dimension:** failure-modes · **Verified:** confirmed by adversarial verifier

`normalize()` computes `gmv` via `_num(row.get("gmv"), float, 0.0)`, and `_num` (lines 85-103) returns the caller-supplied `default` (here `0.0`) on `None`, empty string, or any text it fails to regex-parse as a number — with no way for the caller to tell "really zero" apart from "couldn't parse". `gmv_is_floor` is decided completely independently in `_is_floor()` (lines 106-110), purely by checking for a literal `"+"` in the raw text. So a scraped/CSV GMV cell that fails to parse for any reason other than containing `+` (e.g. TikTok renders `"-"`, blank, or any format the scraper's regex doesn't match) ends up with `gmv=0.0` and `gmv_is_floor=False` — i.e. a row that looks like a *confirmed* exact `RM0` GMV creator. The README explicitly promises hidden-GMV creators "never show a fabricated exact value", but that guarantee only covers the one case the code special-cases (the trailing `+`); any other parse failure produces exactly the fabricated-exact-value outcome the promise is supposed to prevent.

**Failure scenario:** A row arrives with `gmv: "-"` (or any non-numeric, non-"+" text) because TikTok's markup changed or a cell failed to render. `_is_floor` sees no `"+"` so `gmv_is_floor=False`; `_num` fails to parse `"-"` and returns `0.0`. The creator is stored and displayed as an exact `RM0` GMV (no `≥`, no `GMV HIDDEN` badge, per ui.html line 338-343 which only special-cases `gmv_is_floor`). In `/api/search`, with GMV bucket `0-100` selected, `classify()` (ui_server.py line 380: `_in_range(row["gmv"], lo, hi)`) treats this creator as a genuine confirmed match for the lowest GMV bucket, when its real GMV is simply unknown — the opposite of fail-closed/flagged behavior the hidden-GMV design elsewhere in the same function is built to guarantee.

```
def _num(value, cast, default=0):
    if value is None or value == "":
        return default
    ...
    try:
        return cast(float(text) * mult)
    except ValueError:
        return default

def _is_floor(row):
    if "gmv_is_floor" in row:
        return bool(row["gmv_is_floor"])
    return "+" in str(row.get("gmv", ""))
...
"gmv":             _num(row.get("gmv"), float, 0.0),
"gmv_is_floor":    _is_floor(row),
```

---

### 5. [HIGH] Scraped creator fields injected into the DOM via innerHTML without escaping (stored XSS)
**Where:** `ui.html:341` · **Dimension:** security · **Verified:** confirmed by adversarial verifier

render() builds each table row with a template literal containing c.handle, c.nickname, c.profile_url and c.category, then assigns it via tr.innerHTML. None of these values are HTML-escaped. handle/nickname/category are scraped directly from TikTok's DOM text (scraper.py EXTRACT_JS) and are creator-controlled display fields, and profile_url is built from the scraped handle and also interpolated raw into an href attribute. Any HTML metacharacters a creator's nickname contains (TikTok display names are largely free-text) will be parsed as markup/script by the browser instead of displayed as text. This is independently exploitable via /api/import_csv or the CORS hole above, which accept arbitrary handle/nickname/category strings with no sanitization (ui_server.py normalize(), lines 113-129, only .strip()s the strings).

**Failure scenario:** A creator whose TikTok display name is set to something like `<img src=x onerror=fetch('/api/creators',{method:'DELETE'})>` gets scraped into the store; the next time the dashboard renders that row, the payload executes in the context of http://localhost:7374 -- with same-origin access to every API endpoint (read/wipe the creator store, exfiltrate the DM template, trigger /api/message against the DM browser). No click or hover is required; it fires as soon as the row renders.

```
const name = c.profile_url
      ? `<a href="${c.profile_url}" target="_blank" rel="noopener">@${c.handle}</a>`
      : "@" + c.handle;
...
    tr.innerHTML = `
      <td>${name}${c.nickname ? ` <span class="nick">${c.nickname}</span>` : ""}${
        c.gmv_is_floor ? '<span class="badge" title="...">GMV HIDDEN</span>' : ""}</td>
      <td style="color:var(--muted)">${c.category || "—"}</td>
```

---

### 6. [HIGH] CORS carve-out lets any page on affiliate.tiktok.com wipe or poison the local creator store
**Where:** `ui_server.py:57` · **Dimension:** security · **Verified:** confirmed by adversarial verifier

The after_request hook explicitly grants the external origin https://affiliate.tiktok.com permission to make cross-origin GET/POST/DELETE requests to this localhost server, including the destructive DELETE /api/creators endpoint. Browsers require a CORS preflight allowance before letting a cross-origin page send state-changing requests like DELETE or a JSON POST; this code is what grants that allowance. Cross-checking scraper.py, ui_server.py's _run_harvest, and README.md confirms the actual harvest flow never makes a real HTTP call from the TikTok page back to localhost -- scraped rows are extracted via Playwright's page.evaluate() into Python, then written directly via app.test_request_context() inside the same process (ui_server.py lines 437-448). So this CORS opt-in serves no function in the current code path and only adds attack surface: any script running in the context of affiliate.tiktok.com (a TikTok-hosted page, which the same machine's regular browser may well visit while the local server is running) could fetch('http://localhost:7374/api/creators', {method:'DELETE'}) to erase the whole store, or POST arbitrary/malicious rows into it -- with no authentication of any kind guarding the endpoint.

**Failure scenario:** While Affiliate Finder is running, the user (or anyone on the machine) has any tab open on affiliate.tiktok.com -- TikTok's own domain, not attacker-controlled infrastructure -- that happens to run a malicious/injected script (ad, compromised widget, XSS on TikTok's side). That script issues fetch('http://localhost:7374/api/creators', {method:'DELETE'}), the browser preflights it, sees the Allow-Origin/Allow-Methods headers match, and sends the DELETE -- wiping the team's entire scraped creator store with no warning and no confirm() dialog (the confirm() in ui.html clearAll() only guards the button, not the API). The same path also allows POSTing fabricated creator rows (e.g. with a malicious nickname, chaining into the XSS finding below).

```
@app.after_request
def allow_scraper_origin(resp):
    """The harvest script runs inside the Seller Center page and posts here.

    Scoped to that one origin rather than "*" so a stray tab can't push junk
    into the store while this is running.
    """
    if request.headers.get("Origin") == "https://affiliate.tiktok.com":
        resp.headers["Access-Control-Allow-Origin"] = "https://affiliate.tiktok.com"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp
```

---

### 7. [HIGH] MessengerService never recovers once the browser dies; state stays "ready" forever with no restart path
**Where:** `messenger.py:185` · **Dimension:** concurrency · **Verified:** confirmed by adversarial verifier

ensure_started() only launches a new worker thread when self._thread is None or not alive. The worker thread's loop only exits (running ctx.close(); self.state = "stopped") when it pulls a literal None sentinel off self._cmd queue -- but nothing in the codebase (grepped: no other _cmd.put call besides the (fn, holder) tuple at line 228) ever puts None on that queue, so there is no shutdown/restart mechanism at all. If the Chrome window is closed by the user or the browser process crashes, page/ctx becomes unusable, but self._run's while-loop is still blocked on self._cmd.get() and therefore still 'alive' -- so ensure_started() never spins up a fresh browser.

**Failure scenario:** Human closes the visible DM Chrome window (or it crashes) after using it once. self.state remains "ready" (it was only ever set to "ready" at startup and is never reset on a per-job exception) and self._thread.is_alive() stays True since the loop is just blocked on queue.get(). Every subsequent /api/message call goes through _get_messenger().message(...) -> _do(fn) -> ensure_started() (no-op, thread already 'alive') -> fn(page) throws e.g. 'Target page, context or browser has been closed', caught at messenger.py:214, returned as {"error": ...}. GET /api/message/state (ui_server.py:318-323) still reports {"state": "ready"} because self.state was never updated, so the UI has no signal that messaging is permanently broken -- every future click fails with an opaque browser error until the whole Flask process (python ui_server.py) is manually restarted.

```
def ensure_started(self):
    with self._lock:
        if self._thread and self._thread.is_alive():
            return
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
...
                try:
                    while True:
                        job = self._cmd.get()
                        if job is None:
                            break
                        fn, holder = job
                        try:
                            holder["result"] = fn(page)
                        except Exception as exc:
                            holder["error"] = str(exc)
                        finally:
                            holder["event"].set()
```

---

### 8. [HIGH] Creator handle is a hardcoded positional assumption (lines[0]); a DOM/markup change silently drops rows and reports a false 'done'
**Where:** `scraper.py:43` · **Dimension:** scraping-robustness · **Verified:** confirmed by adversarial verifier

`handle` is taken from the first line of the Creator cell's innerText with no validation that it actually looks like a TikTok handle. If TikTok inserts or reorders any line in that cell (a verified badge, a warning/flag icon's text, a new label, etc.), `lines[0]` stops being the handle for every row. `ROW_COUNT_JS`'s `wait_for_function` (line 152) only checks that `<tr>` rows exist, so it still passes — the scroll loop and extraction proceed normally. Rows with no parsed handle are then silently discarded by the filter at line 174, with no error, warning, or count of dropped rows anywhere. In ui_server.py's `_run_harvest` (lines 437-452), the resulting empty/short list is treated as a full success: `stage="done", detail=f"{len(rows)} creators", ... error=None`. A broken selector and 'legitimately zero new creators today' are indistinguishable to the team using the UI.

**Failure scenario:** TikTok adds any extra text/line to the Creator cell above the handle (e.g. a badge or tag). Every row's `handle` becomes "" or garbage. `wait_for_function` still succeeds because rows exist in the DOM. All rows get filtered out at line 174, `harvest()` returns `[]`, and ui_server.py reports `stage="done", detail="0 creators", added=0, updated=0, error=None` — a silent, unexplained zero that looks identical to a normal day with no new creators.

```
const lines = (cells[1]?.innerText || "").split("\n").map(s => s.trim()).filter(Boolean);
...
const handle = lines[0] || "";
...
return [r for r in rows if r.get("handle")]
```

---

### 9. [HIGH] Row data is extracted once after the entire scroll loop; a mid-scroll failure discards all already-collected rows
**Where:** `scraper.py:172` · **Dimension:** scraping-robustness · **Verified:** confirmed by adversarial verifier

`EXTRACT_JS`, which actually pulls creator data out of the DOM, is only called once, at line 172, after the whole scroll-until-target loop (lines 161-170) finishes. There is no try/except around the scroll loop, and no incremental extraction as rows load. If any `page.evaluate`/`page.mouse.wheel`/`page.wait_for_timeout` call raises during scrolling — page navigation, tab crash, Chrome auto-update reload, a network blip that bounces the SPA to an error page — the exception propagates straight out of `harvest()` (the outer `try/finally` at lines 143-176 only closes the context, it doesn't catch or return partial data) to `ui_server.py`'s `_run_harvest`, which sets `stage="error"` and keeps `added=0, updated=0`. Everything gathered up to that point, even if `say("scrolling", ...)` had already reported hundreds of rows loaded, is lost.

**Failure scenario:** A harvest targeting 400 creators has scrolled to 380 rows loaded (reported via `say("scrolling", "380 loaded")`) when Chrome silently reloads the tab (e.g. an OS-triggered tab discard, or TikTok's SPA throwing the user back to a login/error page mid-session). The next `page.evaluate(ROW_COUNT_JS)` or the final `page.evaluate(EXTRACT_JS)` throws. `harvest()` never returns any rows; ui_server.py reports `stage="error"` and the team gets nothing despite 380 creators having already been visible in the DOM moments earlier.

```
while seen < target and stalls < 4:
    page.mouse.move(720, 600)
    page.mouse.wheel(0, 4000)
    page.wait_for_timeout(900)
    now = page.evaluate(ROW_COUNT_JS)
    stalls = stalls + 1 if now == seen else 0
    seen = now
    say("scrolling", f"{seen} loaded")

rows = page.evaluate(EXTRACT_JS)
```

---

### 10. [HIGH] Harvest status poll loop has no error handling — a failed fetch hangs forever with the button stuck disabled
**Where:** `ui.html:503` · **Dimension:** frontend · **Verified:** confirmed by adversarial verifier

The `setInterval` callback that polls `/api/harvest/status` has no try/catch. If that fetch ever rejects (server process crashed/restarted mid-harvest, Playwright browser crash, connection refused), the promise inside the callback throws unhandled, so `clearInterval(poll)` and `resolve()` on lines 513/515 are never reached. Since `findCreators()` is `await`ing that Promise (line 503), the whole function hangs indefinitely.

**Failure scenario:** The Flask server (or the Playwright-driven browser it depends on) becomes unreachable while a harvest is in progress. Every 800ms `fetch("/api/harvest/status")` throws (e.g. 'Failed to fetch'), which is never caught. The 'Find creators' button (disabled at line 493 with text 'Searching TikTok…') stays disabled forever, the progress banner freezes on its last message with no error shown, and the only recovery is reloading the page.

```
await new Promise((resolve) => {
    const poll = setInterval(async () => {
      const s = await (await fetch("/api/harvest/status")).json();
      if (s.error) {
        showProgress(prog, "Couldn't finish: " + s.error, "err");
      } else {
        const tone = s.stage === "login" ? "warn" : "run";
        showProgress(prog, `${labels[s.stage] || s.stage}${s.detail ? " · " + s.detail : ""}`, tone);
      }
      if (!s.running) {
        clearInterval(poll);
        if (!s.error) showProgress(prog, `✓ Loaded ${s.added} creators from TikTok`, "ok");
        resolve();
      }
    }, 800);
  });
```

---

### 11. [MEDIUM] creators.json load-modify-save is unlocked, causing lost updates across concurrent requests
**Where:** `ui_server.py:223` · **Dimension:** concurrency · **Verified:** confirmed by adversarial verifier

> **Note:** Reviewer rated high; verifier downgraded — the race window is milliseconds in a single-user tool. Same fix (store lock) as finding 1.

put_creators() (POST /api/creators, also used by /api/import_csv and by _run_harvest's completion), clear_creators() (DELETE /api/creators), and _cache_user_id() (called after every successful /api/message) all do an unsynchronized read-load-mutate-save cycle against the same CREATORS_JSON file. Flask runs with threaded=True (line 480: app.run(port=PORT, debug=False, threaded=True)), and _run_harvest also runs on its own background thread, so these handlers can and do interleave. No lock (like the existing _harvest_lock / _messenger_lock) guards any of this.

**Failure scenario:** User clicks 'Find creators' (POST /api/harvest); the harvest thread runs harvest() for ~30-90s and, on completion, calls put_creators() via app.test_request_context (ui_server.py:445-446), which itself does load_creators() at T1 then save_creators(...) at T2. If the user clicks 'Clear stored' (ui.html:239/546-548 -> DELETE /api/creators -> clear_creators() -> save_creators([]) at ui_server.py:267-268) at any point between T1 and T2, the harvest's save_creators() call at T2 overwrites the file with its own in-memory snapshot taken before the clear -- silently reviving the exact data the user just told the app to delete. The same class of race hits _cache_user_id (ui_server.py:288-297): two Message clicks on different creators that both resolve a fresh tiktok_user_id concurrently each call load_creators() before either has saved, so whichever save_creators() call finishes second clobbers the first creator's newly-cached id, forcing a redundant (and slower, throttle-prone) profile lookup next time that creator is messaged.

```
existing = {r["handle"]: r for r in load_creators()}
    added = updated = skipped = 0
    for raw in incoming:
        row = normalize(raw)
        ...
        existing[row["handle"]].update(row)
        ...
    save_creators(list(existing.values()))
```

---

### 12. [MEDIUM] Category values injected into <option> markup via innerHTML without escaping
**Where:** `ui.html:323` · **Dimension:** security · **Verified:** confirmed by adversarial verifier

syncCategories() rebuilds the category <select> by concatenating each scraped category string directly into an <option value="${c}">${c}</option> template and assigning via innerHTML, with no escaping. category is scraped raw DOM text (scraper.py, EXTRACT_JS: category: lines[lastLv + 2]) and is included in DATA_CONTRACT as an unrestricted string, so it is not guaranteed to come from a closed enum by anything this codebase enforces.

**Failure scenario:** If a scraped or CSV-imported row carries a category value containing HTML (e.g. `"><script>...</script>`), rebuilding the dropdown (which happens on every /api/search response, ui.html line 309) injects and executes it in the dashboard's origin, same impact as the row-rendering XSS above.

```
sel.innerHTML = '<option value="">All categories</option>' +
    cats.map(c => `<option value="${c}">${c}</option>`).join("");
```

---

### 13. [MEDIUM] Level/nickname/category/followers parsing falls back to 0/"" on regex miss, silently corrupting data instead of signaling a parse failure
**Where:** `scraper.py:40` · **Dimension:** scraping-robustness · **Verified:** confirmed by adversarial verifier

`lastLv` and `demoIdx` are located via regexes tied to specific English text patterns (`Lv. N`, and `<number>[K|M], Male|Female`). If either regex fails to match a row — because TikTok changes the level-label text/locale, or because a given creator's demographic line is absent/hidden — the corresponding fields fall back to `0` or `""` (lines 46-49). Because `handle` is still populated for these rows, they pass the line-174 filter and are written into the store as if they were fully valid, indistinguishable from a creator who genuinely has 0 followers / level 0. No log, flag, or count anywhere records that parsing failed for these fields.

**Failure scenario:** A creator's demographic line is not rendered (e.g. privacy setting hides gender/age breakdown) or TikTok tweaks the demographic phrasing. `demoIdx` becomes -1, and that creator is stored with `followers: 0` even if they have 500K followers. Any downstream min-follower filter silently excludes a qualified creator, and there is no signal anywhere that this was a parsing miss rather than reality.

```
nickname: lastLv >= 0 ? (lines[lastLv + 1] || "") : "",
level: lastLv >= 0 ? toNum(lines[lastLv].replace("Lv.", "")) : 0,
category: lastLv >= 0 ? (lines[lastLv + 2] || "") : "",
followers: demoIdx >= 0 ? toNum(lines[demoIdx].split(",")[0]) : 0,
```

---

### 14. [MEDIUM] _is_floor() treats any non-empty string as truthy, misclassifying gmv_is_floor from CSV imports
**Where:** `ui_server.py:109` · **Dimension:** correctness · **Verified:** confirmed by adversarial verifier

> **Note:** Found independently by two review dimensions (correctness and failure-modes); the duplicate is folded into this entry.

_is_floor() does `return bool(row["gmv_is_floor"])` when the key is present. /api/import_csv parses pasted CSV text with csv.DictReader, so every cell — including gmv_is_floor — arrives as a Python string. bool("False") is True in Python, so a CSV row explicitly marked gmv_is_floor="False" is misclassified as floor=True. This corrupts the confirmed-vs-uncertain split that classify() (lines 358-387) depends on: floor rows are restricted to the 10000+ GMV bucket and routed to 'uncertain' instead of 'reject' when they miss the GMV-per-customer bar, so previously-confirmed creators get wrongly demoted.

**Failure scenario:** Verified directly: _is_floor({'gmv_is_floor': 'False', 'gmv': '5000'}) returns True instead of False. A user exporting the store to CSV (columns per DATA_CONTRACT, which import_csv's docstring says must match) and re-importing it via /api/import_csv flips gmv_is_floor to True for every row whose column literally read "False", mass-reclassifying confirmed creators as uncertain and wrongly restricting their GMV-bucket eligibility to 10000+ only.

```
def _is_floor(row):
    """Trust an explicit flag from the scraper; otherwise sniff a trailing '+'."""
    if "gmv_is_floor" in row:
        return bool(row["gmv_is_floor"])
    return "+" in str(row.get("gmv", ""))
```

---

### 15. [MEDIUM] tiktokLogin()'s delayed status check is not covered by its own try/catch
**Where:** `ui.html:438` · **Dimension:** frontend · **Verified:** confirmed by adversarial verifier

The `setTimeout` callback that fetches `/api/tiktok_login/state` 4 seconds later runs after the enclosing try block has already returned control to the event loop, so a rejection inside it is not caught by the `catch (e)` on line 450. There's no error handling inside the timer callback itself.

**Failure scenario:** If `/api/tiktok_login/state` fails 4 seconds after the login window opens (server busy spawning the browser, transient error, or process restart), the exception inside the setTimeout callback is unhandled and `status.textContent` never updates past 'Opening the DM browser… log into your TikTok DM account in that window' (set at line 440) — the user gets no feedback that anything went wrong.

```
try {
    await fetch("/api/tiktok_login", { method: "POST" });
    // Give the window a moment, then confirm whether the login took.
    setTimeout(async () => {
      const s = await (await fetch("/api/tiktok_login/state")).json();
      status.textContent = s.logged_in
        ? "✓ TikTok DM account connected — Message buttons will open real chats now"
        : "A TikTok login window is open — sign in there, then click a Message button";
    }, 4000);
  } catch (e) {
    status.textContent = "Couldn't open the DM browser — is the server running from the launcher?";
  }
```

---

### 16. [MEDIUM] README's 'DMing creators' section describes a copy/paste-to-profile flow that no longer exists
**Where:** `README.md:62` · **Dimension:** docs-drift · **Verified:** confirmed by adversarial verifier

README says the DM button 'copies a draft personalised to that creator and opens their TikTok profile in a new tab — click Message there and paste.' The actual code (ui.html dm(), ui_server.py POST /api/message, messenger.py MessengerService.message()) resolves the creator's real TikTok user id server-side (defeating TikTok's bot-check interstitial) and drives a separate, app-controlled Playwright Chrome window straight to the messages?u=<id> chat with the message already typed into the compose box. Nothing is copied to the clipboard and no profile tab is opened for manual paste.

**Failure scenario:** A user follows the README's mental model, expects their normal browser to open a TikTok profile tab to paste into, and is confused when an unexpected second always-open Chrome window (the DM browser, backed by browser_profile_dm/) appears instead and requires its own one-time 'Log into TikTok (for DMs)' step that the README never mentions.

```
Each row has a **DM** button. It copies a draft personalised to that creator and
opens their TikTok profile in a new tab — click **Message** there and paste.
```

---

### 17. [MEDIUM] README's stated rationale for NOT using the messages?u=<id> deep link describes exactly what messenger.py now does
**Where:** `README.md:72` · **Dimension:** docs-drift · **Verified:** confirmed by adversarial verifier

README explains the deep link is impractical because resolving handle→user id 'trips tiktok.com's bot check ("Please wait…")', and that opening the profile is the workaround. messenger.py's resolve_user_id() does precisely this resolution automatically (polls past the interstitial, reloads once, reads the id from __UNIVERSAL_DATA_FOR_REHYDRATION__) and then feeds it directly into the messages?u=<id> deep link via open_and_prefill(). The 'obstacle' the README describes has been solved and automated in code the README doesn't mention.

**Failure scenario:** A reader trying to understand or extend the DM feature will believe the deep link is unused/impractical and may duplicate work resolving user ids, unaware messenger.py already does this via resolve_user_id() (lines 135-166) and open_and_prefill() (lines 52-94).

```
**Why it opens the profile rather than the DM directly.** TikTok does have a
deep link — `tiktok.com/messages?u=<id>` — but it needs the creator's TikTok
user id, and the id on the Affiliate Center row is a *different* id space. Feed
it an affiliate id and TikTok silently drops the parameter and lands on your
inbox. Resolving handle → user id means loading each profile, which trips
tiktok.com's bot check ("Please wait…"). Opening the profile is one extra click
and always works.
```

---

### 18. [MEDIUM] 'GMV at least' / 'Followers at least' criteria table misdescribes dropdown-bucket filtering as free-text 'at least' thresholds
**Where:** `README.md:33` · **Dimension:** docs-drift · **Verified:** confirmed by adversarial verifier

The five-criteria table labels GMV and Followers as '...at least (RM)' / '...at least' fields, and the following paragraph says inputs accept typed formats like 50K, 1.2M, RM45,000. In the real UI these are <select> dropdowns bound to fixed buckets/tiers (ui.html lines 164-182: gmv_bucket, follower_tier), not free-text thresholds. Server-side, classify() in ui_server.py (lines 358-387) tests range membership via _in_range() against GMV_BUCKETS/FOLLOWER_TIERS (lines 339-351) — a closed [lo, hi) range, not a >= 'at least' comparison — so choosing e.g. the 'nano' follower tier excludes a creator with 500K followers, which a true 'at least 1,000 followers' filter would not.

**Failure scenario:** A user reading 'Followers at least' expects selecting a tier to be a floor (e.g. picking nano would still surface a 2M-follower creator), and instead gets creators excluded outside the chosen tier's upper bound — or tries to type '50K' into what is actually a fixed dropdown with no such option, per ui.html lines 174-182.

```
| GMV at least (RM) | scraped |
| GMV per customer (RM) | **derived** — see below |
| Creators to reach | caps how many come back |
| Followers at least | scraped |
| Items sold at least | scraped |

The four thresholds are ANDed... Inputs accept the formats Seller Center itself renders: `50K`, `1.2M`,
`RM45,000`, `1,200`.
```

---

### 19. [LOW] copyHandles() reports success even when the clipboard write fails
**Where:** `ui.html:374` · **Dimension:** frontend · **Verified:** confirmed by adversarial verifier

`navigator.clipboard.writeText(...)` returns a Promise that is neither awaited nor `.catch()`-handled, yet the very next line unconditionally sets the status text to a success message.

**Failure scenario:** If the clipboard write is rejected — e.g. the document briefly loses focus, OS-level clipboard permission is denied, or the page was opened via a machine's LAN IP rather than `localhost`/`127.0.0.1` (so it isn't a secure context and `navigator.clipboard` is undefined, making the call throw synchronously) — the status still reads 'Copied N handles', misleading the user into believing the paste buffer holds the handles when it does not.

```
function copyHandles() {
  if (!current.length) return;
  navigator.clipboard.writeText(current.map(c => "@" + c.handle).join("\n"));
  document.getElementById("status").textContent = `Copied ${current.length} handles`;
}
```

---

### 20. [LOW] messenger.py and its browser profile/settings files are absent from the README's Layout table
**Where:** `README.md:108` · **Dimension:** docs-drift · **Verified:** confirmed by adversarial verifier

The Layout table lists ui_server.py, ui.html, scraper.py, creators.json, and browser_profile/ but omits messenger.py entirely, along with the second Playwright profile directory it uses (browser_profile_dm/, messenger.py line 25) and settings.json (ui_server.py lines 17, 209-214) which stores the DM template.

**Failure scenario:** Someone auditing what files/state the app creates on disk (e.g. before sharing the project folder or a backup) won't know browser_profile_dm/ (a second logged-in TikTok session) or settings.json exist, since the README's file inventory doesn't list them.

```
| File | Role |
|---|---|
| `ui_server.py` | Flask app — store, filtering, ranking, harvest jobs |
| `ui.html` | the dashboard |
| `scraper.py` | Playwright: drives Chrome, reads Find creators |
| `creators.json` | the store (upserted by handle, so re-harvests don't duplicate) |
| `browser_profile/` | keeps you logged into Seller Center |
```

---

### 21. [LOW] Endpoints table omits six of the twelve real Flask routes
**Where:** `README.md:126` · **Dimension:** docs-drift · **Verified:** confirmed by adversarial verifier

The Endpoints table lists only /api/creators (GET/POST/DELETE), /api/search, /api/harvest, and /api/harvest/status. It does not list GET/POST /api/template (ui_server.py lines 204, 209), POST /api/import_csv (line 254), POST /api/message (line 300), GET /api/message/state (line 318), POST /api/tiktok_login (line 326), or GET /api/tiktok_login/state (line 333) — the entire messaging/DM API and the CSV import path are undocumented.

**Failure scenario:** A developer integrating with or debugging this Flask app via the README's endpoint list would not discover /api/message or /api/tiktok_login exist, and might miss that the DM flow depends on server-side state (messenger browser process) reachable only through those undocumented routes.

```
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/creators` | everything stored |
| POST | `/api/creators` | upsert rows |
| DELETE | `/api/creators` | wipe the store |
| POST | `/api/search` | the five knobs, plus `sort` and `include_uncertain` |
| POST | `/api/harvest` | start a scrape (`region`, `target`) |
| GET | `/api/harvest/status` | progress of the running scrape |
```

---

### 22. [LOW] Duplicate `clearAll()` definition (manual find)

**Where:** `ui.html:540` and `ui.html:546` · **Dimension:** manual read-through

The `clearAll()` function is defined twice, back to back, with identical bodies. The second
definition silently shadows the first. Harmless today, but a future edit to one copy and not
the other would be confusing. Delete one.

---

*Raw agent output (including full verifier explanations per finding):*
`/private/tmp/claude-501/-Users-justin-Documents-Claude-Projects/7a36ca8a-6622-4afa-8ad8-34b888e35ed0/tasks/wmj44eskr.output`
