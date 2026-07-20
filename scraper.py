"""
Harvests creators from TikTok Affiliate Center → Find creators.

Drives your installed Chrome through Playwright against a profile kept in this
folder, so you log into Seller Center once and it sticks for later runs.

Why a real browser: every affiliate.tiktok.com API call is signed with msToken /
X-Bogus / X-Gnarly, computed by obfuscated ByteDance JS. Replaying those from
Python isn't practical, so we read what the page renders instead.
"""
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

APP_DIR     = Path(__file__).parent
PROFILE_DIR = APP_DIR / "browser_profile"   # persists the Seller Center login
FIND_URL    = "https://affiliate.tiktok.com/connection/creator?shop_region={region}"
SELLER_URL  = "https://seller-{region}.tiktok.com/homepage"

# Pulls one page of rows out of the DOM. The list renders as two tables: the
# first holds only the sticky header, the second the rows. Cells run
# [checkbox, Creator, Video, GMV, Items sold, Avg. views, Engagement, Invite].
EXTRACT_JS = r"""
() => {
  const body = [...document.querySelectorAll("table")].find(t => t.querySelector("tbody tr"));
  if (!body) return [];
  const toNum = (s) => {
    if (!s) return null;
    const m = String(s).replace(/[, ]/g, "").match(/([\d.]+)\s*([KMB])?/i);
    if (!m) return null;
    const mult = { k: 1e3, m: 1e6, b: 1e9 }[(m[2] || "").toLowerCase()] || 1;
    return Math.round(parseFloat(m[1]) * mult);
  };
  return [...body.querySelectorAll("tbody tr")].map((tr) => {
    const cells = [...tr.children];
    const lines = (cells[1]?.innerText || "").split("\n").map(s => s.trim()).filter(Boolean);
    // Creator cell packs: handle, "Lv. N" (rendered twice), nickname, category,
    // ", +N", then "<followers>, <gender> <pct>, <age band>".
    const lastLv = lines.map(l => /^Lv\.\s*\d+$/.test(l)).lastIndexOf(true);
    const demoIdx = lines.findIndex(l => /^[\d.]+K?M?,\s*(Male|Female)/i.test(l));
    const gmvRaw = (cells[3]?.innerText || "").trim();
    const handle = lines[0] || "";
    return {
      handle,
      nickname: lastLv >= 0 ? (lines[lastLv + 1] || "") : "",
      level: lastLv >= 0 ? toNum(lines[lastLv].replace("Lv.", "")) : 0,
      category: lastLv >= 0 ? (lines[lastLv + 2] || "") : "",
      followers: demoIdx >= 0 ? toNum(lines[demoIdx].split(",")[0]) : 0,
      demographics: demoIdx >= 0 ? lines[demoIdx] : "",
      gmv: toNum(gmvRaw),
      // "RM10K+" means TikTok is hiding the exact figure — the number is a
      // lower bound, not a value. Roughly 4 in 10 rows arrive this way.
      gmv_is_floor: gmvRaw.includes("+"),
      items_sold: toNum(cells[4]?.innerText),
      avg_video_views: toNum(cells[5]?.innerText),
      engagement_rate: (cells[6]?.innerText || "").trim(),
      profile_url: handle ? "https://www.tiktok.com/@" + handle : "",
    };
  });
}
"""

ROW_COUNT_JS = """
() => {
  const t = [...document.querySelectorAll("table")].find(x => x.querySelector("tbody tr"));
  return t ? t.querySelectorAll("tbody tr").length : 0;
}
"""


def _signed_in(page, region):
    """True once we're on the region's Seller Center as an authenticated user.

    Logged out, TikTok bounces to the US marketing site (seller.tiktok.com) —
    not a region login form — so checking the host alone isn't enough.
    """
    host_ok = f"seller-{region.lower()}.tiktok.com" in page.url
    return host_ok and "Seller Center" in (page.title() or "")


def _sign_in(page, region, say, timeout_ms):
    """Park on the region's Seller Center and wait for a human to log in.

    Returns immediately when the stored profile is still authenticated, which is
    the normal case after the first run.
    """
    say("navigating", "Checking your Seller Center session")
    page.goto(SELLER_URL.format(region=region.lower()), wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if _signed_in(page, region):
        return

    # Make sure the human can actually find this window — it's a separate Chrome
    # profile, easily buried behind their everyday browser.
    try:
        page.bring_to_front()
    except Exception:
        pass
    say("login", "Log into TikTok Seller Center in the Chrome window")
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(3000)
        waited += 3000
        try:
            if _signed_in(page, region):
                say("login", "Signed in")
                return
            # A logged-out landing bounces off-region; nudge back to the login
            # page, but never while the user is mid-form on a passport screen.
            if ("seller" not in page.url) and ("passport" not in page.url) \
                    and ("login" not in page.url) and ("account" not in page.url):
                page.goto(SELLER_URL.format(region=region.lower()),
                          wait_until="domcontentloaded")
        except Exception:
            pass   # navigations mid-login race; keep waiting rather than dying
    raise RuntimeError(
        f"Timed out waiting for a Seller Center login for region {region}. "
        f"Log in inside the Chrome window that opens, then harvest again."
    )


def harvest(region="MY", target=120, on_progress=None, login_timeout=300_000):
    """Return creator rows from Find creators, scrolling until `target` is reached.

    on_progress(stage, detail) is called as it goes so the UI can narrate.
    """
    def say(stage, detail=""):
        if on_progress:
            on_progress(stage, detail)

    PROFILE_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        say("launching", "Opening Chrome")
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",          # reuse installed Chrome, don't fetch Chromium
            headless=False,            # visible, so login is possible when needed
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _sign_in(page, region, say, login_timeout)

            say("navigating", "Loading Find creators")
            page.goto(FIND_URL.format(region=region), wait_until="domcontentloaded")
            try:
                # ROW_COUNT_JS is an arrow function, so wrap and invoke it —
                # appending "> 0" to the bare literal is a syntax error.
                page.wait_for_function(f"({ROW_COUNT_JS})() > 0", timeout=60_000)
            except PWTimeout:
                raise RuntimeError(
                    f"Signed in, but the creator list never rendered. Check that this "
                    f"shop actually has Affiliate Center access for region {region}."
                )

            # Playwright's wheel is a trusted event, which is what the lazy list
            # listens for — scripted scrollTop alone gets ignored after a while.
            seen, stalls = page.evaluate(ROW_COUNT_JS), 0
            say("scrolling", f"{seen} loaded")
            while seen < target and stalls < 4:
                page.mouse.move(720, 600)
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(900)
                now = page.evaluate(ROW_COUNT_JS)
                stalls = stalls + 1 if now == seen else 0
                seen = now
                say("scrolling", f"{seen} loaded")

            rows = page.evaluate(EXTRACT_JS)
            say("done", f"{len(rows)} creators")
            return [r for r in rows if r.get("handle")]
        finally:
            ctx.close()


if __name__ == "__main__":
    import json, sys
    region = sys.argv[1] if len(sys.argv) > 1 else "MY"
    got = harvest(region=region, on_progress=lambda s, d: print(f"[{s}] {d}"))
    print(json.dumps(got[:3], indent=2))
    print(f"\n{len(got)} creators | {sum(r['gmv_is_floor'] for r in got)} with hidden GMV")
