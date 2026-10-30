#!/usr/bin/env python3
"""
Simple-Blood watcher
  GitHub open issues  ->  Discord #github channel      (via webhook)
  CurseForge comments ->  Discord #curseforge channel  (via webhook)

Usage:
  python bot.py                    run forever, poll every POLL_MINUTES
  python bot.py --once             run one check and exit (for cron / testing)
  python bot.py --test             send a test message to both webhooks
  python bot.py --no-backfill      first run: mark everything as seen, only post NEW things
  python bot.py --debug-curseforge scrape comments, save debug files, post nothing
"""
import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------- config
GITHUB_REPO = os.getenv("GITHUB_REPO", "Lythroo/Simple-Blood")
CF_URL = os.getenv("CURSEFORGE_URL", "https://www.curseforge.com/minecraft/mc-mods/simple-blood").rstrip("/")
GH_HOOK = os.getenv("GITHUB_WEBHOOK_URL", "").strip()
CF_HOOK = os.getenv("CURSEFORGE_WEBHOOK_URL", "").strip()
GH_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "10"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
CF_MAX_SCROLLS = int(os.getenv("CF_MAX_SCROLLS", "12"))

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
PROFILE_DIR = BASE / "browser_profile"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("watcher")


# ----------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            log.warning("state.json unreadable, starting fresh")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), "utf-8")
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------- discord
def post_webhook(hook: str, username: str, embed: dict | None = None, content: str | None = None) -> bool:
    payload = {"username": username, "allowed_mentions": {"parse": []}}
    if embed:
        payload["embeds"] = [embed]
    if content:
        payload["content"] = content
    for _ in range(6):
        try:
            r = requests.post(hook, json=payload, timeout=20)
        except requests.RequestException as e:
            log.error("webhook network error: %s", e)
            time.sleep(3)
            continue
        if r.status_code == 429:
            wait = 2.0
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                pass
            time.sleep(wait + 0.5)
            continue
        if r.ok:
            time.sleep(0.7)  # stay well under Discord's webhook rate limit
            return True
        log.error("webhook rejected (%s): %s", r.status_code, r.text[:300])
        return False
    return False


def clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------------------------------------------- GitHub
def fetch_issues() -> list[dict]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    out, page = [], 1
    while True:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            params={"state": "open", "per_page": 100, "page": page},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        items = r.json()
        out += [i for i in items if "pull_request" not in i]  # the issues API also returns PRs
        if len(items) < 100:
            break
        page += 1
    return sorted(out, key=lambda i: i["number"])


def issue_embed(i: dict) -> dict:
    labels = ", ".join(l["name"] for l in i.get("labels", [])) or "—"
    assignees = ", ".join(a["login"] for a in i.get("assignees", [])) or "—"
    fields = [
        {"name": "Labels", "value": clip(labels, 1000), "inline": True},
        {"name": "Assignees", "value": clip(assignees, 1000), "inline": True},
        {"name": "Comments", "value": str(i.get("comments", 0)), "inline": True},
    ]
    if i.get("milestone"):
        fields.append({"name": "Milestone", "value": i["milestone"]["title"], "inline": True})
    fields.append({"name": "Updated", "value": f"<t:{int(datetime.fromisoformat(i['updated_at'].replace('Z', '+00:00')).timestamp())}:R>", "inline": True})
    return {
        "title": clip(f"#{i['number']} {i['title']}", 256),
        "url": i["html_url"],
        "description": clip(i.get("body") or "*(no description)*", 3800),
        "color": 0x2DA44E,
        "author": {"name": i["user"]["login"], "url": i["user"]["html_url"], "icon_url": i["user"]["avatar_url"]},
        "fields": fields,
        "timestamp": i["created_at"],
        "footer": {"text": f"GitHub · {GITHUB_REPO}"},
    }


def sync_github(state: dict, backfill: bool) -> None:
    if not GH_HOOK:
        log.warning("GITHUB_WEBHOOK_URL not set, skipping GitHub")
        return
    first = "github" not in state
    seen = set(state.get("github", []))
    issues = fetch_issues()
    log.info("GitHub: %d open issues", len(issues))
    new = [i for i in issues if i["number"] not in seen]
    if first and not backfill:
        state["github"] = sorted(seen | {i["number"] for i in new})
        save_state(state)
        return
    state.setdefault("github", [])
    for i in new:
        if post_webhook(GH_HOOK, "GitHub Issues", embed=issue_embed(i)):
            state["github"].append(i["number"])
            save_state(state)
            log.info("posted issue #%s", i["number"])


# ----------------------------------------------------------------- CurseForge
TEXT_KEYS = ("content", "body", "text", "message", "comment", "commentText")
AUTHOR_KEYS = ("author", "user", "username", "createdBy", "poster", "member", "owner")
NAME_KEYS = ("username", "name", "displayName", "login")
DATE_KEYS = ("createdAt", "dateCreated", "created", "datePosted", "createdDate", "postedAt", "date", "timestamp")


def clean_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def author_name(a) -> tuple[str | None, str | None]:
    if isinstance(a, str):
        return a, None
    if isinstance(a, dict):
        name = next((a[k] for k in NAME_KEYS if isinstance(a.get(k), str)), None)
        avatar = next((a[k] for k in ("avatarUrl", "avatar", "avatar_url", "avatarURL") if isinstance(a.get(k), str)), None)
        return name, avatar
    return None, None


def to_iso(v) -> str | None:
    try:
        if isinstance(v, (int, float)):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        if isinstance(v, str):
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
    except Exception:
        pass
    return None


def walk_json(node, out: list, parent_author: str | None = None) -> None:
    """Find comment-shaped objects (has text + author) anywhere in a JSON blob."""
    if isinstance(node, dict):
        text = next((node[k] for k in TEXT_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
        akey = next((k for k in AUTHOR_KEYS if node.get(k)), None)
        name, avatar = author_name(node[akey]) if akey else (None, None)
        me = parent_author
        if text and name:
            date = next((to_iso(node[k]) for k in DATE_KEYS if node.get(k) is not None and to_iso(node[k])), None)
            body = clean_html(text)
            cid = str(node["id"]) if node.get("id") is not None else hashlib.sha1(f"{name}|{body}|{date}".encode()).hexdigest()[:16]
            out.append({"id": cid, "author": name, "avatar": avatar, "text": body, "date": date, "reply_to": parent_author})
            me = name
        for v in node.values():
            walk_json(v, out, me)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, out, parent_author)


def dom_fallback(page) -> list[dict]:
    """Last resort if no JSON response looked like comments: read visible text near author links."""
    rows = page.evaluate(
        """() => {
          const res = [], seen = new Set();
          for (const a of document.querySelectorAll('main a[href*="/members/"], #__next a[href*="/members/"]')) {
            if (a.closest('header, nav, footer, aside')) continue;
            const name = a.innerText.trim(); if (!name) continue;
            let el = a;
            for (let i = 0; i < 6 && el.parentElement; i++) {
              el = el.parentElement;
              if (el.innerText.trim().length > name.length + 20) break;
            }
            const txt = el.innerText.trim();
            if (seen.has(txt)) continue; seen.add(txt);
            res.push({name, txt});
          }
          return res;
        }"""
    )
    out = []
    for r in rows:
        body = r["txt"].replace(r["name"], "", 1).strip()
        if body:
            out.append({"id": hashlib.sha1(f"{r['name']}|{body}".encode()).hexdigest()[:16], "author": r["name"],
                        "avatar": None, "text": body, "date": None, "reply_to": None})
    return out


def scrape_comments(debug: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

    responses = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=HEADLESS, viewport={"width": 1280, "height": 900}, locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", lambda r: responses.append(r) if r.request.resource_type in ("xhr", "fetch") else None)
            page.goto(f"{CF_URL}/comments", wait_until="domcontentloaded", timeout=60000)

            for _ in range(30):  # wait out a Cloudflare "Just a moment..." check
                if "just a moment" not in page.title().lower():
                    break
                page.wait_for_timeout(2000)
            else:
                raise RuntimeError("Blocked by Cloudflare. Run once with HEADLESS=false and solve the check; it is remembered afterwards.")

            page.wait_for_timeout(4000)
            last = -1
            for n in range(CF_MAX_SCROLLS):  # scroll / click "load more" until nothing new arrives
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1200)
                btn = page.get_by_role("button", name=re.compile(r"load more|show more|more comments|view more", re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    page.wait_for_timeout(2000)
                if len(responses) == last and n >= 2:
                    break
                last = len(responses)

            blobs = []
            for r in responses:
                try:
                    if "json" in r.headers.get("content-type", ""):
                        blobs.append((r.url, r.json()))
                except Exception:
                    pass

            preferred = [b for b in blobs if "comment" in b[0].lower()]
            found: list[dict] = []
            for _, data in preferred or blobs:
                walk_json(data, found)
            if not found:
                log.warning("no comment JSON found, falling back to page text")
                found = dom_fallback(page)

            if debug:
                (BASE / "debug_comments.html").write_text(page.content(), "utf-8")
                (BASE / "debug_network.json").write_text(
                    json.dumps([{"url": u, "data": d} for u, d in blobs], indent=1)[:5_000_000], "utf-8")
        finally:
            ctx.close()

    uniq = {c["id"]: c for c in found}
    comments = list(uniq.values())
    if all(c["date"] for c in comments):
        comments.sort(key=lambda c: c["date"])
    else:
        comments.reverse()  # sites list newest first; post oldest first
    return comments


def comment_embed(c: dict) -> dict:
    title = f"Reply by {c['author']}" if c["reply_to"] else f"New comment by {c['author']}"
    e = {
        "title": clip(title, 256),
        "url": f"{CF_URL}/comments",
        "description": clip(c["text"], 3800),
        "color": 0xF16436,
        "footer": {"text": "CurseForge · Simple Blood"},
    }
    if c["reply_to"]:
        e["fields"] = [{"name": "In reply to", "value": clip(c["reply_to"], 200), "inline": True}]
    if c["date"]:
        e["timestamp"] = c["date"]
    if c["avatar"] and c["avatar"].startswith("http"):
        e["author"] = {"name": c["author"], "icon_url": c["avatar"]}
    return e


def sync_curseforge(state: dict, backfill: bool) -> None:
    if not CF_HOOK:
        log.warning("CURSEFORGE_WEBHOOK_URL not set, skipping CurseForge")
        return
    first = "curseforge" not in state
    seen = set(state.get("curseforge", []))
    comments = scrape_comments()
    log.info("CurseForge: %d comments found", len(comments))
    if not comments:
        raise RuntimeError("0 comments parsed (blocked by CurseForge, or the page layout changed)")
    new = [c for c in comments if c["id"] not in seen]
    if first and not backfill:
        state["curseforge"] = sorted(seen | {c["id"] for c in new})
        save_state(state)
        return
    state.setdefault("curseforge", [])
    for c in new:
        if post_webhook(CF_HOOK, "CurseForge Comments", embed=comment_embed(c)):
            state["curseforge"].append(c["id"])
            save_state(state)
            log.info("posted comment %s by %s", c["id"], c["author"])


# ----------------------------------------------------------------- main
def alert(name: str, state: dict, err: Exception | None) -> None:
    """Post ONE warning to the channel when a source starts failing; stay quiet until it recovers."""
    key = f"alert_{name}"
    hook = GH_HOOK if name == "GitHub" else CF_HOOK
    if err is None:
        state.pop(key, None)
    elif hook and not state.get(key):
        msg = f"⚠️ The {name} check is failing: {str(err)[:300]}\nI'll stay quiet until it works again."
        if post_webhook(hook, "Watcher", content=msg):
            state[key] = True


def run_once(backfill: bool) -> None:
    state = load_state()
    for name, fn in (("GitHub", sync_github), ("CurseForge", sync_curseforge)):
        err = None
        try:
            fn(state, backfill)
        except Exception as e:  # one source failing must never stop the other
            log.error("%s check failed: %s", name, e)
            err = e
        alert(name, state, err)
    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--debug-curseforge", action="store_true")
    a = ap.parse_args()

    if a.test:
        for hook, who in ((GH_HOOK, "GitHub Issues"), (CF_HOOK, "CurseForge Comments")):
            if hook:
                print(who, "->", "ok" if post_webhook(hook, who, content="✅ Test message: webhook works.") else "FAILED")
            else:
                print(who, "-> webhook URL missing in .env")
        return

    if a.debug_curseforge:
        cs = scrape_comments(debug=True)
        print(f"Parsed {len(cs)} comments. Debug files saved: debug_comments.html, debug_network.json")
        for c in cs[:5]:
            print(f"- {c['author']}: {c['text'][:80]!r}")
        return

    if a.once:
        run_once(not a.no_backfill)
        return

    log.info("Watching every %s min. Ctrl+C to stop.", POLL_MINUTES)
    backfill = not a.no_backfill
    while True:
        run_once(backfill)
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
