"""
Opens a creator's DM in the app's own browser and pre-fills the message.

It NEVER sends. It navigates to the chat, types the text into the compose box,
and leaves the window in front so the human reads it and clicks Send. The send
action is always a human keystroke — that's the line, and it's also what keeps
this off TikTok's automated-send radar.

Uses the same persistent profile as scraper.py, so the TikTok login (done once)
sticks for later clicks.
"""
import queue
import re
import threading
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

APP_DIR     = Path(__file__).parent
# Messaging gets its OWN browser profile, separate from the harvest's Seller
# Center profile. Two reasons: (1) DMs go from a different TikTok account than
# the shop, and (2) the harvest and the messenger would otherwise fight over the
# same profile — only one Chrome can hold a profile at a time.
PROFILE_DIR = APP_DIR / "browser_profile_dm"
DM_URL      = "https://www.tiktok.com/messages?u={user_id}"
LOGIN_URL   = "https://www.tiktok.com/login"

# The compose box is a contenteditable div; TikTok's markup shifts, so match on
# the stable placeholder text first, then fall back to any message contenteditable.
COMPOSE_SELECTORS = (
    'div[contenteditable="true"][data-placeholder*="message" i]',
    'div[contenteditable="true"][placeholder*="message" i]',
    '[data-e2e="message-input-area"] div[contenteditable="true"]',
    'div[contenteditable="true"]',
)

# Signs we landed somewhere other than an open, sendable chat.
LOGGED_OUT_HINT = ("Log in", "log in to", "Sign up")
RESTRICTED_HINT = ("cannot send", "can't send", "not accepting", "follow each other",
                   "only receive messages")


def _find_compose(page):
    for sel in COMPOSE_SELECTORS:
        box = page.query_selector(sel)
        if box and box.is_visible():
            return box
    return None


def open_and_prefill(page, user_id, message, timeout_ms=20_000):
    """Drive an already-open page to the DM and type the message. No send.

    Returns a status string: "ready" (typed, waiting for the human to send),
    "login" (browser isn't logged into TikTok), "restricted" (creator doesn't
    accept this DM), or "no_compose" (couldn't find the box).
    """
    page.goto(DM_URL.format(user_id=user_id), wait_until="domcontentloaded")

    try:
        page.wait_for_selector(",".join(COMPOSE_SELECTORS), timeout=timeout_ms)
    except PWTimeout:
        text = (page.inner_text("body")[:600] if page.query_selector("body") else "")
        if any(h.lower() in text.lower() for h in RESTRICTED_HINT):
            return "restricted"
        if any(h.lower() in text.lower() for h in LOGGED_OUT_HINT):
            return "login"
        return "no_compose"

    box = _find_compose(page)
    if not box:
        return "no_compose"

    box.click()
    # Clear any half-typed leftover without touching Enter.
    page.keyboard.press("Control+A" if not _is_mac(page) else "Meta+A")
    page.keyboard.press("Backspace")

    # Two things that would wreck a real message, both handled here:
    #  1. In TikTok DMs a bare Enter SENDS. The template has line breaks, so each
    #     newline is a Shift+Enter (soft break), never a plain Enter — otherwise
    #     the message fires off in fragments.
    #  2. insert_text() drops each line in as one unit, so multi-codepoint emoji
    #     (👋🏻 = wave + skin tone) stay intact. Char-by-char typing can split them.
    lines = message.split("\n")
    for i, line in enumerate(lines):
        if i > 0:
            page.keyboard.press("Shift+Enter")
        if line:
            page.keyboard.insert_text(line)

    page.bring_to_front()
    return "ready"


def _is_mac(page):
    try:
        return "Mac" in page.evaluate("navigator.platform")
    except Exception:
        return False


# Reads the profile owner's numeric id from the page's embedded rehydration
# JSON. This is far more reliable than the "Message" link — that link only
# renders when logged in and sometimes not at all, whereas the id is always in
# webapp.user-detail. Falls back to the message link if the blob is missing.
_READ_UID_JS = r"""
(handle) => {
  try {
    const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
    if (el) {
      const scope = (JSON.parse(el.textContent)['__DEFAULT_SCOPE__']) || {};
      const u = scope['webapp.user-detail'];
      const id = u && u.userInfo && u.userInfo.user && u.userInfo.user.id;
      if (id) return id;
    }
  } catch (e) {}
  for (const a of document.querySelectorAll('a[href*="/messages"]')) {
    const m = /[?&]u=(\d+)/.exec(a.getAttribute('href') || '');
    if (m) return m[1];
  }
  return null;
}
"""


def _read_user_id(page, handle):
    try:
        return page.evaluate(_READ_UID_JS, handle)
    except Exception:
        return None


def resolve_user_id(page, handle, timeout_ms=25_000):
    """Load the creator's profile and read their real TikTok user id.

    TikTok gates profile loads behind a "Please wait..." bot interstitial, so we
    poll it out (and reload once, which usually clears it) before reading the id
    from the embedded data. Runs one creator at a time, only when the human
    clicks Message — never 120 in a burst.
    """
    handle = handle.lstrip("@")
    page.goto(f"https://www.tiktok.com/@{handle}", wait_until="domcontentloaded")

    start = time.monotonic()
    reloaded = False
    while (time.monotonic() - start) * 1000 < timeout_ms:
        body = page.inner_text("body")[:80] if page.query_selector("body") else ""
        low = body.lower()
        if "denied" in low:
            return None  # hard block — caller reports it as a throttle
        if "please wait" not in low and body.strip():
            uid = _read_user_id(page, handle)
            if uid:
                return uid
        page.wait_for_timeout(1500)
        # One reload past ~8s usually clears a stuck "Please wait..." challenge.
        if not reloaded and (time.monotonic() - start) > 8:
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            reloaded = True

    return _read_user_id(page, handle)


class MessengerService:
    """One long-lived, app-controlled browser, driven from Flask.

    Playwright's sync API must be used from a single thread, so the browser
    lives in one worker thread and requests reach it through a queue. The window
    stays open across clicks, so the TikTok login (done once, by hand) sticks.
    """

    def __init__(self, region="MY"):
        self.region = region
        self._cmd = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self.state = "stopped"      # stopped | starting | ready | error
        self.detail = ""

    def ensure_started(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.state = "starting"
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        PROFILE_DIR.mkdir(exist_ok=True)
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                self.state = "ready"
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
                finally:
                    ctx.close()
                    self.state = "stopped"
        except Exception as exc:
            self.state = "error"
            self.detail = str(exc)

    def _do(self, fn, timeout=90):
        self.ensure_started()
        holder = {"event": threading.Event()}
        self._cmd.put((fn, holder))
        if not holder["event"].wait(timeout):
            return {"error": "timed out waiting for the browser"}
        if "error" in holder:
            return {"error": holder["error"]}
        return holder.get("result", {"error": "no result"})

    def message(self, handle, user_id, message):
        """Open the DM for one creator and pre-fill the message. No send.

        Resolves the user id first if we don't have it — in a BACKGROUND tab, so
        the window the user watches only ever shows the DM, never the profile.
        The DM id lives only on the profile (confirmed: it's nowhere in the
        Affiliate Center), so this one lookup is unavoidable, but it's invisible
        and cached after the first time.

        Returns `status` (ready | login | restricted | no_compose | no_id |
        throttled) and the `user_id` (so the caller can cache a fresh one).
        """
        def fn(page):
            uid = user_id
            if not uid:
                scratch = page.context.new_page()   # background lookup tab
                try:
                    uid = resolve_user_id(scratch, handle)
                    if not uid:
                        body = scratch.inner_text("body")[:200] if scratch.query_selector("body") else ""
                        # The profile's "Message" link only exists when logged in.
                        # Missing + a login prompt = we're logged out (fixable);
                        # missing without one = the creator has DMs off.
                        logged_out = bool(scratch.query_selector(
                            '[data-e2e="top-login-button"], a[href="/login"]')) \
                            or "log in to tiktok" in body.lower()
                        if "denied" in body.lower():
                            st = "throttled"
                        elif logged_out:
                            st = "login"
                        else:
                            st = "no_id"
                        return {"status": st, "user_id": None}
                finally:
                    scratch.close()
            status = open_and_prefill(page, uid, message)   # visible tab → straight to the DM
            return {"status": status, "user_id": uid}
        return self._do(fn, timeout=90)

    def open_login(self):
        """Open tiktok.com's login page in the DM browser and surface it, so the
        user can sign into the account they DM from. The session persists in this
        profile, so it's a one-time step."""
        def fn(page):
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.bring_to_front()
            return {"status": "opened"}
        return self._do(fn, timeout=45)

    def login_state(self):
        """True if the DM browser is signed into tiktok.com."""
        def fn(page):
            page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            login_btn = page.query_selector('[data-e2e="top-login-button"], a[href="/login"]')
            return {"logged_in": login_btn is None}
        return self._do(fn, timeout=30)


def prefill_dm(user_id, message, region="MY", keep_open_seconds=None):
    """Standalone entry: launch the app browser, prefill one DM, hold it open.

    keep_open_seconds=None blocks until Enter, so you can eyeball the box and
    click Send yourself. Used for the first manual test before wiring into Flask.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            status = open_and_prefill(page, user_id, message)
            print(f"[status] {status}")
            if status == "ready":
                print("Message is typed in. Review it, then click Send yourself.")
            if keep_open_seconds is None:
                input("Press Enter here to close the browser… ")
            else:
                page.wait_for_timeout(keep_open_seconds * 1000)
            return status
        finally:
            ctx.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python messenger.py <tiktok_user_id> <message>")
        raise SystemExit(1)
    prefill_dm(sys.argv[1], sys.argv[2])
