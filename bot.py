#!/usr/bin/env python3
"""
Feedback watcher: for every project listed in projects.json
  GitHub open issues  ->  Discord webhook "issues"
  CurseForge comments ->  Discord webhook "comments"

Usage:
  python bot.py                      run forever, poll every POLL_MINUTES
  python bot.py --once               run one check and exit (used by GitHub Actions)
  python bot.py --test               send a test message to both webhooks
  python bot.py --no-backfill        first run of a project: mark everything as seen, post only NEW things
  python bot.py --debug-curseforge [NAME]   scrape one project's comments, save debug files, post nothing
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
GH_HOOK = os.getenv("GITHUB_WEBHOOK_URL", "").strip()
CF_HOOK = os.getenv("CURSEFORGE_WEBHOOK_URL", "").strip()
GH_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "10"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
CF_MAX_SCROLLS = int(os.getenv("CF_MAX_SCROLLS", "12"))

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
PROFILE_DIR = BASE / "browser_profile"
PROJECTS_FILE = BASE / "projects.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("watcher")


class Blocked(RuntimeError):
    """CurseForge (Cloudflare) refused to show the page."""


# ----------------------------------------------------------------- projects
def load_projects() -> list[dict]:
    if PROJECTS_FILE.exists():
        projects = json.loads(PROJECTS_FILE.read_text("utf-8"))
    else:  # old single-project setup
        projects = [{
            "name": "Simple Blood",
            "github": os.getenv("GITHUB_REPO", "Lythroo/Simple-Blood"),
            "curseforge": os.getenv("CURSEFORGE_URL", "https://www.curseforge.com/minecraft/mc-mods/simple-blood"),
        }]
    for p in projects:
        p["curseforge"] = (p.get("curseforge") or "").rstrip("/") or None
        p["github"] = (p.get("github") or "").strip() or None
        if not (p["github"] or p["curseforge"]):
            raise ValueError(f"project {p.get('name')!r} needs a 'github' and/or 'curseforge' entry")
    return projects


def project_key(p: dict) -> str:
    return (p["github"] or p["curseforge"]).lower()


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


def migrate_state(state: dict, first_key: str) -> None:
    """Old single-project state.json -> per-project layout (keeps history, no re-posting)."""
    if "github" in state or "curseforge" in state:
        ps = state.setdefault("projects", {}).setdefault(first_key, {})
        for k in ("github", "curseforge"):
            if k in state:
                ps[k] = state.pop(k)
    for k in ("alert_GitHub", "alert_CurseForge"):
        state.pop(k, None)


# ----------------------------------------------------------------- discord
def post_webhook(hook: str, username: str, embed: dict | None = None, content: str | None = None,
                 thread_name: str | None = None) -> bool:
    """thread_name: required for forum channels (creates one post per call). If the channel turns out to be a
    normal text channel and Discord rejects it, we retry once without it."""
    payload = {"username": username[:80], "allowed_mentions": {"parse": []}}
    if embed:
        payload["embeds"] = [embed]
    if content:
        payload["content"] = content
    if thread_name:
        payload["thread_name"] = thread_name[:100]
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
        if r.status_code == 400 and "thread_name" in payload:
            log.warning("webhook rejected thread_name (not a forum channel?), retrying as a normal message: %s", r.text[:200])
            payload.pop("thread_name")
            continue
        log.error("webhook rejected (%s): %s", r.status_code, r.text[:300])
        return False
    return False


def clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------------------------------------------- GitHub
def fetch_issues(repo: str) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    out, page = [], 1
    while True:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/issues",
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


def issue_embed(i: dict, name: str, repo: str) -> dict:
    labels = ", ".join(l["name"] for l in i.get("labels", [])) or "—"
    assignees = ", ".join(a["login"] for a in i.get("assignees", [])) or "—"
    updated = int(datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00")).timestamp())
    fields = [
        {"name": "Labels", "value": clip(labels, 1000), "inline": True},
        {"name": "Assignees", "value": clip(assignees, 1000), "inline": True},
        {"name": "Comments", "value": str(i.get("comments", 0)), "inline": True},
    ]
    if i.get("milestone"):
        fields.append({"name": "Milestone", "value": i["milestone"]["title"], "inline": True})
    fields.append({"name": "Updated", "value": f"<t:{updated}:R>", "inline": True})
    return {
        "title": clip(f"#{i['number']} {i['title']}", 256),
        "url": i["html_url"],
        "description": clip(i.get("body") or "*(no description)*", 3800),
        "color": 0x2DA44E,
        "author": {"name": i["user"]["login"], "url": i["user"]["html_url"], "icon_url": i["user"]["avatar_url"]},
        "fields": fields,
        "timestamp": i["created_at"],
        "footer": {"text": f"{name} · GitHub · {repo}"},
    }


def sync_github(p: dict, ps: dict, save, backfill: bool) -> None:
    if not GH_HOOK:
        log.warning("GITHUB_WEBHOOK_URL not set, skipping GitHub")
        return
    repo, name = p["github"], p["name"]
    first = "github" not in ps
    seen = set(ps.get("github", []))
    issues = fetch_issues(repo)
    log.info("%s / GitHub: %d open issues", name, len(issues))
    new = [i for i in issues if i["number"] not in seen]
    if first and not backfill:
        ps["github"] = sorted(seen | {i["number"] for i in new})
        save()
        return
    ps.setdefault("github", [])
    for i in new:
        thread = f"[{name}] #{i['number']} {i['title']}"  # forum channel: one post per issue
        if post_webhook(GH_HOOK, f"{name} · Issues", embed=issue_embed(i, name, repo), thread_name=thread):
            ps["github"].append(i["number"])
            save()
            log.info("%s: posted issue #%s", name, i["number"])


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


def scrape_comments(cf_url: str, tag: str = "debug", debug: bool = False) -> list[dict]:
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
            page.goto(f"{cf_url}/comments", wait_until="domcontentloaded", timeout=60000)

            for _ in range(30):  # wait out a Cloudflare "Just a moment..." check
                if "just a moment" not in page.title().lower():
                    break
                page.wait_for_timeout(2000)
            else:
                raise Blocked("Blocked by Cloudflare. Run once with HEADLESS=false and solve the check; it is remembered afterwards.")

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
                (BASE / f"debug_{tag}_comments.html").write_text(page.content(), "utf-8")
                (BASE / f"debug_{tag}_network.json").write_text(
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


def comment_embed(c: dict, name: str, cf_url: str) -> dict:
    title = f"Reply by {c['author']}" if c["reply_to"] else f"New comment by {c['author']}"
    e = {
        "title": clip(title, 256),
        "url": f"{cf_url}/comments",
        "description": clip(c["text"], 3800),
        "color": 0xF16436,
        "footer": {"text": f"{name} · CurseForge"},
    }
    if c["reply_to"]:
        e["fields"] = [{"name": "In reply to", "value": clip(c["reply_to"], 200), "inline": True}]
    if c["date"]:
        e["timestamp"] = c["date"]
    if c["avatar"] and c["avatar"].startswith("http"):
        e["author"] = {"name": c["author"], "icon_url": c["avatar"]}
    return e


def sync_curseforge(p: dict, ps: dict, save, backfill: bool) -> None:
    if not CF_HOOK:
        log.warning("CURSEFORGE_WEBHOOK_URL not set, skipping CurseForge")
        return
    url, name = p["curseforge"], p["name"]
    first = "curseforge" not in ps
    seen = set(ps.get("curseforge", []))
    comments = scrape_comments(url, tag=re.sub(r"\W+", "_", name.lower()))
    log.info("%s / CurseForge: %d comments found", name, len(comments))
    if not comments:
        raise RuntimeError("0 comments parsed (blocked by CurseForge, the page has no comments, or the layout changed)")
    new = [c for c in comments if c["id"] not in seen]
    if first and not backfill:
        ps["curseforge"] = sorted(seen | {c["id"] for c in new})
        save()
        return
    ps.setdefault("curseforge", [])
    for c in new:
        if post_webhook(CF_HOOK, f"{name} · Comments", embed=comment_embed(c, name, url)):
            ps["curseforge"].append(c["id"])
            save()
            log.info("%s: posted comment %s by %s", name, c["id"], c["author"])


# ----------------------------------------------------------------- main
def alert(p: dict, kind: str, ps: dict, err: Exception | None) -> None:
    """Post ONE warning to the channel when a source starts failing; stay quiet until it recovers."""
    key = f"alert_{kind}"
    hook = GH_HOOK if kind == "github" else CF_HOOK
    if err is None:
        ps.pop(key, None)
    elif hook and not ps.get(key):
        label = "GitHub" if kind == "github" else "CurseForge"
        msg = f"⚠️ {p['name']}: the {label} check is failing: {str(err)[:300]}\nI'll stay quiet until it works again."
        thread = f"⚠️ {p['name']} watcher alert" if kind == "github" else None
        if post_webhook(hook, "Watcher", content=msg, thread_name=thread):
            ps[key] = True


def run_once(backfill: bool) -> None:
    state = load_state()
    projects = load_projects()
    migrate_state(state, project_key(projects[0]))
    save = lambda: save_state(state)
    cf_blocked = False  # if Cloudflare blocks once, don't waste time retrying the other projects this run

    for p in projects:
        ps = state.setdefault("projects", {}).setdefault(project_key(p), {})
        jobs = []
        if p["github"]:
            jobs.append(("github", sync_github))
        if p["curseforge"] and not cf_blocked:
            jobs.append(("curseforge", sync_curseforge))
        for kind, fn in jobs:
            err = None
            try:
                fn(p, ps, save, backfill)
            except Exception as e:  # one source failing must never stop the others
                log.error("%s / %s check failed: %s", p["name"], kind, e)
                err = e
                if isinstance(e, Blocked):
                    cf_blocked = True
            alert(p, kind, ps, err)
            save()

    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--debug-curseforge", nargs="?", const="", metavar="NAME")
    a = ap.parse_args()

    if a.test:
        for hook, who in ((GH_HOOK, "Issues"), (CF_HOOK, "Comments")):
            if hook:
                ok = post_webhook(hook, f"Watcher · {who}", content="✅ Test message: webhook works.",
                                  thread_name="Watcher test" if who == "Issues" else None)
                print(who, "->", "ok" if ok else "FAILED")
            else:
                print(who, "-> webhook URL missing")
        return

    if a.debug_curseforge is not None:
        cands = [p for p in load_projects() if p["curseforge"]]
        want = a.debug_curseforge.lower()
        p = next((x for x in cands if want in x["name"].lower()), cands[0] if cands else None)
        if not p:
            sys.exit("No project with a CurseForge URL in projects.json")
        tag = re.sub(r"\W+", "_", p["name"].lower())
        cs = scrape_comments(p["curseforge"], tag=tag, debug=True)
        print(f"{p['name']}: parsed {len(cs)} comments. Debug files saved: debug_{tag}_comments.html, debug_{tag}_network.json")
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
