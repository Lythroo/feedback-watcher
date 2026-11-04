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
from datetime import datetime, timedelta, timezone
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
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()   # Discord bot (forum posts for issues)
FORUM_ID = os.getenv("ISSUES_FORUM_ID", "").strip()       # channel ID of the #github forum
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()       # enables automatic channel provisioning
CATEGORY_NAME = (os.getenv("DISCORD_CATEGORY_NAME", "Feedback Watcher").strip() or "Feedback Watcher")[:100]
ON_CLOSE = os.getenv("ON_CLOSE", "archive").strip().lower()  # archive | mark | delete
if ON_CLOSE not in ("archive", "mark", "delete"):
    ON_CLOSE = "archive"
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
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError) as e:
        # Starting fresh would re-post every issue and comment, so refuse instead.
        sys.exit(f"state.json is unreadable ({e}); fix or delete it before running again.")


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


def sync_github_webhook(p: dict, ps: dict, save, backfill: bool) -> None:
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


# ----------------------------------------------------------------- Discord bot: one forum post per issue
API = "https://discord.com/api/v10"
BOT_MODE = bool(BOT_TOKEN and (FORUM_ID or GUILD_ID))
MANAGED_MODE = bool(BOT_TOKEN and GUILD_ID)
_forums: dict[str, dict] = {}
_all_threads: dict = {}
_managed_channels: dict[str, dict] = {}
_dashboard: dict = {}


class Later(Exception):
    """Discord told us to wait a long time. State is saved; the next run simply continues."""


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


def dapi(method: str, path: str, **kw):
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DiscordBot (feedback-watcher, 1.0)"}
    r = None
    for _ in range(6):
        try:
            r = requests.request(method, API + path, headers=headers, timeout=25, **kw)
        except requests.RequestException as e:
            log.error("discord network error: %s", e)
            time.sleep(3)
            continue
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            if wait > 90:
                raise Later(f"Discord rate limit ({int(wait)}s); continuing on the next run")
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 500:
            time.sleep(2)
            continue
        time.sleep(0.5)
        return r
    if r is None:
        raise RuntimeError("Discord is unreachable")
    return r


def project_forum_id(p: dict) -> str:
    return _managed_channels.get(project_key(p), {}).get("forum") or FORUM_ID


def project_comments_id(p: dict) -> str | None:
    return _managed_channels.get(project_key(p), {}).get("comments")


def channel_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (slug or "project")[:70]


def ensure_managed_channels(state: dict, projects: list[dict]) -> None:
    """Create and reuse a tidy, bot-owned channel layout under one category."""
    if not MANAGED_MODE:
        return
    saved = state.setdefault("discord", {})
    saved_projects = saved.setdefault("projects", {})
    r = dapi("GET", f"/guilds/{GUILD_ID}/channels")
    if not r.ok:
        raise RuntimeError(f"Bot cannot list server channels (HTTP {r.status_code}); check DISCORD_GUILD_ID and View Channels.")
    channels = r.json()

    category_id = saved.get("category")
    category = next((c for c in channels if c.get("id") == category_id and c.get("type") == 4), None)
    if not category:
        category = next((c for c in channels if c.get("type") == 4 and c.get("name") == CATEGORY_NAME), None)
    if not category:
        r = dapi("POST", f"/guilds/{GUILD_ID}/channels", json={"name": CATEGORY_NAME, "type": 4})
        if not r.ok:
            raise RuntimeError(f"Could not create the Feedback Watcher category (HTTP {r.status_code}): {r.text[:200]}")
        category = r.json()
        channels.append(category)
    saved["category"] = category["id"]

    def get_or_create(name: str, kind: int, topic: str = "", tags: list[str] | None = None) -> str:
        found = next((c for c in channels if c.get("parent_id") == category["id"]
                      and c.get("name") == name and c.get("type") == kind), None)
        if found:
            return found["id"]
        payload = {"name": name, "type": kind, "parent_id": category["id"]}
        if topic:
            payload["topic"] = topic[:1024]
        if tags:
            payload["available_tags"] = [{"name": t[:20], "moderated": False} for t in tags[:20]]
        created = dapi("POST", f"/guilds/{GUILD_ID}/channels", json=payload)
        if not created.ok:
            raise RuntimeError(f"Could not create #{name} (HTTP {created.status_code}): {created.text[:200]}")
        channels.append(created.json())
        return created.json()["id"]

    status_id = saved.get("dashboard")
    if not any(c.get("id") == status_id and c.get("parent_id") == category["id"] and c.get("type") == 0
               for c in channels):
        status_id = get_or_create("watcher-dashboard", 0, "Feedback watcher status and tracked projects")
        saved["dashboard"] = status_id
    _dashboard["id"] = status_id

    for p in projects:
        key = project_key(p)
        old = saved_projects.setdefault(key, {})
        slug = channel_slug(p["name"])
        if p.get("github"):
            old["forum"] = get_or_create(
                f"{slug}-issues", 15, f"GitHub issues for {p['name']} · {p['github']}",
                [p["name"], "Bug", "Enhancement", "Question", "Closed"],
            )
        if p.get("curseforge"):
            old["comments"] = get_or_create(
                f"{slug}-comments", 0, f"New CurseForge comments for {p['name']} · {p['curseforge']}"
            )
        _managed_channels[key] = old

    # Store channel layout before any polling so a later failure never makes duplicates.
    save_state(state)


def update_dashboard(state: dict, projects: list[dict]) -> None:
    channel_id = _dashboard.get("id")
    if not (MANAGED_MODE and channel_id):
        return
    lines = []
    for p in projects:
        sources = []
        if p.get("github"):
            sources.append("GitHub issues")
        if p.get("curseforge"):
            sources.append("CurseForge comments")
        lines.append(f"**{p['name']}** · {' + '.join(sources)}")
    embed = {"title": "Feedback Watcher", "description": clip("\n".join(lines) or "No projects configured.", 3800),
             "color": 0x5865F2, "footer": {"text": "Updates automatically · issues are organized by project"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    discord_state = state.setdefault("discord", {})
    message_id = discord_state.get("dashboard_message")
    if message_id:
        r = dapi("PATCH", f"/channels/{channel_id}/messages/{message_id}", json={"embeds": [embed]})
        if r.ok:
            return
        if r.status_code != 404:
            log.warning("could not update watcher dashboard (HTTP %s)", r.status_code)
    r = dapi("POST", f"/channels/{channel_id}/messages", json={"embeds": [embed], "allowed_mentions": {"parse": []}})
    if r.ok:
        discord_state["dashboard_message"] = r.json()["id"]


def post_channel(channel_id: str, embed: dict, content: str | None = None) -> bool:
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
    return dapi("POST", f"/channels/{channel_id}/messages", json=payload).ok


def forum_info(forum_id: str | None = None) -> dict:
    forum_id = forum_id or FORUM_ID
    if not forum_id:
        raise RuntimeError("Set DISCORD_GUILD_ID for automatic setup or ISSUES_FORUM_ID for an existing forum.")
    if forum_id not in _forums:
        r = dapi("GET", f"/channels/{forum_id}")
        if r.status_code in (401, 403, 404):
            raise RuntimeError(f"Bot can't see the forum channel (HTTP {r.status_code}). Check DISCORD_BOT_TOKEN, "
                               "ISSUES_FORUM_ID and that the bot was invited to your server.")
        r.raise_for_status()
        ch = r.json()
        if ch.get("type") not in (15, 16):
            raise RuntimeError("ISSUES_FORUM_ID is not a forum channel")
        _forums[forum_id] = {"tags": {t["name"].lower(): t["id"] for t in ch.get("available_tags", [])},
                             "guild": ch.get("guild_id")}
    return _forums[forum_id]


def tag_ids(*names: str, forum_id: str | None = None) -> list[str]:
    tags = forum_info(forum_id)["tags"]
    return [tags[n.lower()] for n in names if n.lower() in tags][:5]


def create_post(title: str, embed: dict, tags: list[str], forum_id: str | None = None) -> str:
    forum_id = forum_id or FORUM_ID
    r = dapi("POST", f"/channels/{forum_id}/threads", json={
        "name": title[:100], "auto_archive_duration": 10080, "applied_tags": tags,
        "message": {"embeds": [embed], "allowed_mentions": {"parse": []}},
    })
    if not r.ok:
        raise RuntimeError(f"Discord refused to create the forum post (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()["id"]  # the first message of a forum post has the same id as the post


def say(thread_id: str, content: str | None = None, embed: dict | None = None) -> bool:
    payload = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    return dapi("POST", f"/channels/{thread_id}/messages", json=payload).ok


def notify_issues(p: dict, msg: str) -> bool:
    title = f"⚠️ {p['name']} watcher alert"
    if BOT_MODE:
        try:
            forum_id = project_forum_id(p)
            create_post(title, {"description": clip(msg, 3800), "color": 0xD29922},
                        tag_ids(p["name"], forum_id=forum_id), forum_id)
            return True
        except Exception as e:
            log.error("could not post alert via bot: %s", e)
    if GH_HOOK:
        return post_webhook(GH_HOOK, "Watcher", content=msg, thread_name=title)
    return False


def forum_threads(forum_id: str | None = None) -> list[dict]:
    """All posts currently in the forum (active + archived), fetched once per run."""
    forum_id = forum_id or FORUM_ID
    if forum_id in _all_threads:
        return _all_threads[forum_id]
    out: list[dict] = []
    try:
        r = dapi("GET", f"/guilds/{forum_info(forum_id)['guild']}/threads/active")
        if r.ok:
            out += [t for t in r.json().get("threads", []) if t.get("parent_id") == forum_id]
        before = None
        for _ in range(5):
            params = {"limit": 100}
            if before:
                params["before"] = before
            r = dapi("GET", f"/channels/{forum_id}/threads/archived/public", params=params)
            if not r.ok:
                break
            data = r.json()
            ts = data.get("threads", [])
            out += ts
            if not data.get("has_more") or not ts:
                break
            before = ts[-1]["thread_metadata"]["archive_timestamp"]
    except Exception as e:
        log.warning("could not list existing forum posts: %s", e)
    _all_threads[forum_id] = out
    return out


def adopt_threads(name: str, forum_id: str | None = None) -> dict:
    """Link posts made earlier by the webhook version: '[Name] #12 title' -> post id."""
    pat = re.compile(rf"^\[{re.escape(name)}\] #(\d+) ")
    return {m.group(1): t["id"] for t in forum_threads(forum_id) if (m := pat.match(t.get("name", "")))}


def gh_issue(repo: str, n) -> tuple[int, dict | None]:
    r = requests.get(f"https://api.github.com/repos/{repo}/issues/{n}", headers=gh_headers(), timeout=30)
    return r.status_code, (r.json() if r.ok else None)


def _paged(url: str, params: dict, max_pages: int) -> list[dict]:
    out = []
    for page in range(1, max_pages + 1):
        r = requests.get(url, params={**params, "per_page": 100, "page": page}, headers=gh_headers(), timeout=30)
        r.raise_for_status()
        items = r.json()
        out += items
        if len(items) < 100:
            break
    return out


def fetch_issue_comments(repo: str, n) -> list[dict]:
    return _paged(f"https://api.github.com/repos/{repo}/issues/{n}/comments", {}, 3)


def fetch_repo_comments(repo: str, since: str) -> list[dict]:
    return _paged(f"https://api.github.com/repos/{repo}/issues/comments",
                  {"since": since, "sort": "created", "direction": "asc"}, 10)


def gh_comment_embed(c: dict) -> dict:
    return {
        "title": "💬 New comment",
        "url": c["html_url"],
        "description": clip(c.get("body") or "*(empty)*", 3800),
        "color": 0x768390,
        "author": {"name": c["user"]["login"], "url": c["user"]["html_url"], "icon_url": c["user"]["avatar_url"]},
        "timestamp": c["created_at"],
        "footer": {"text": "GitHub comment"},
    }


def closed_embed(issue: dict, name: str, repo: str) -> dict:
    e = issue_embed(issue, name, repo)
    e["title"] = clip("✅ [CLOSED] " + e["title"], 256)
    e["color"] = 0x6E7681 if issue.get("state_reason") == "not_planned" else 0x8957E5
    return e


def handle_closed(name: str, repo: str, n: str, tid: str, issue: dict | None, ps: dict,
                  forum_id: str | None = None) -> None:
    threads, closed = ps["threads"], ps["closed"]
    if ON_CLOSE == "delete":
        r = dapi("DELETE", f"/channels/{tid}")
        if not (r.ok or r.status_code == 404):
            raise RuntimeError(f"couldn't delete the post for #{n} (HTTP {r.status_code}); does the bot have 'Manage Threads'?")
        threads.pop(n, None)
        log.info("%s: deleted post for closed issue #%s", name, n)
        return
    if issue:
        dapi("PATCH", f"/channels/{tid}/messages/{tid}", json={"embeds": [closed_embed(issue, name, repo)]})
        by = (issue.get("closed_by") or {}).get("login")
        why = {"completed": "as completed", "not_planned": "as not planned", "duplicate": "as a duplicate"}.get(issue.get("state_reason") or "", "")
        msg = "✅ Closed on GitHub" + (f" {why}" if why else "") + (f" by **{by}**" if by else "") + "."
    else:
        msg = "🗑️ This issue was removed from GitHub."
    r = dapi("POST", f"/channels/{tid}/messages", json={"content": msg, "allowed_mentions": {"parse": []}})
    if r.status_code == 404:  # somebody deleted the post by hand
        threads.pop(n, None)
        return
    threads.pop(n, None)  # move first, so a failed archive below can never cause a second "closed" message
    closed[n] = tid
    patch = {"applied_tags": tag_ids(name, "Closed", forum_id=forum_id)}
    if ON_CLOSE == "archive":
        patch.update(archived=True, locked=True)
    r = dapi("PATCH", f"/channels/{tid}", json=patch)
    if not r.ok and r.status_code != 404:
        raise RuntimeError(f"couldn't update the post for #{n} (HTTP {r.status_code}); does the bot have 'Manage Threads'?")
    log.info("%s: closed issue #%s (%s)", name, n, ON_CLOSE)


def handle_reopened(name: str, repo: str, issue: dict, n: str, tid: str, ps: dict,
                    forum_id: str | None = None) -> None:
    ps["closed"].pop(n, None)
    r = dapi("PATCH", f"/channels/{tid}", json={"archived": False, "locked": False,
                                                   "applied_tags": tag_ids(name, forum_id=forum_id)})
    if r.status_code == 404:
        return
    dapi("PATCH", f"/channels/{tid}/messages/{tid}", json={"embeds": [issue_embed(issue, name, repo)]})
    say(tid, "🔄 Reopened on GitHub.")
    ps["threads"][n] = tid
    log.info("%s: reopened issue #%s", name, n)


def sync_github_bot(p: dict, ps: dict, save, backfill: bool) -> None:
    repo, name = p["github"], p["name"]
    forum_id = project_forum_id(p)
    forum_info(forum_id)  # fail early with a clear message if the bot/forum setup is wrong
    started = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    first = "github" not in ps
    seen = set(ps.get("github", []))
    threads = ps.setdefault("threads", {})
    closed = ps.setdefault("closed", {})
    comments_done = ps.setdefault("gh_comments", [])
    forum_comment_done = ps.setdefault("forum_comments", {}).setdefault(forum_id, {})
    issues = fetch_issues(repo)
    open_nums = {i["number"] for i in issues}
    log.info("%s / GitHub: %d open issues", name, len(issues))

    # Reconcile against this destination forum, not only source-level "seen" state.
    # This also migrates open issues out of a previously configured shared forum.
    target_posts = adopt_threads(name, forum_id)
    target_thread_ids = {t["id"] for t in forum_threads(forum_id)}
    for n, tid in target_posts.items():
        if int(n) in open_nums:
            threads[n] = tid
            closed.pop(n, None)

    skipped = ps.setdefault("forum_backfill_skipped", {}).setdefault(forum_id, [])
    skipped = set(skipped)
    if first and not backfill:
        skipped.update(str(n) for n in open_nums if str(n) not in target_posts)
        ps["forum_backfill_skipped"][forum_id] = sorted(skipped)
        ps["github"] = sorted(seen | open_nums)
        save()
    else:
        source_seen = set(ps.get("github", []))
        ps.setdefault("github", [])
        for i in issues:
            n = str(i["number"])
            tid = target_posts.get(n)
            if not tid and threads.get(n) in target_thread_ids:
                tid = threads[n]
            if not tid:
                if n in skipped:
                    continue
                tid = create_post(f"[{name}] #{n} {i['title']}", issue_embed(i, name, repo),
                                  tag_ids(name, forum_id=forum_id), forum_id)
                target_thread_ids.add(tid)
                log.info("%s: posted issue #%s", name, n)
            threads[n] = tid
            closed.pop(n, None)
            source_seen.add(i["number"])
            # Track delivery per forum: a comment already sent to the old shared forum
            # still needs to be copied into this project's new forum.
            if i.get("comments"):
                delivered = set(forum_comment_done.get(n, []))
                for c in fetch_issue_comments(repo, n):
                    if c["id"] in delivered:
                        continue
                    if say(tid, embed=gh_comment_embed(c)):
                        delivered.add(c["id"])
                        if c["id"] not in comments_done:
                            comments_done.append(c["id"])
                forum_comment_done[n] = sorted(delivered)[-10000:]
            save()
        ps["github"] = sorted(source_seen)
        ps["adopted"] = True
        save()

    # 2) issues that were closed earlier but are open again
    for n, tid in list(closed.items()):
        issue = next((i for i in issues if i["number"] == int(n)), None)
        if issue:
            handle_reopened(name, repo, issue, n, tid, ps, forum_id)
            save()

    # 3) tracked issues that are no longer open -> closed handling
    for n, tid in list(threads.items()):
        if int(n) in open_nums:
            continue
        code, issue = gh_issue(repo, n)
        if code in (404, 410):
            issue = None
        elif code != 200 or issue.get("state") != "closed":
            continue  # unclear right now, try again next run
        handle_closed(name, repo, n, tid, issue, ps, forum_id)
        save()

    # 4) new GitHub comments on open issues -> into their forum post
    all_ok = True
    since = ps.get("gh_since")
    if since:
        for c in fetch_repo_comments(repo, since):
            n = c["issue_url"].rsplit("/", 1)[-1]
            delivered = set(forum_comment_done.get(n, []))
            if c["id"] in delivered or n not in threads:
                continue
            if say(threads[n], embed=gh_comment_embed(c)):
                delivered.add(c["id"])
                forum_comment_done[n] = sorted(delivered)[-10000:]
                if c["id"] not in comments_done:
                    comments_done.append(c["id"])
            else:
                all_ok = False
    if all_ok:
        ps["gh_since"] = started
    ps["gh_comments"] = comments_done[-3000:]
    save()


def sync_github(p: dict, ps: dict, save, backfill: bool) -> None:
    if BOT_MODE:
        sync_github_bot(p, ps, save, backfill)
    else:
        sync_github_webhook(p, ps, save, backfill)


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
    comments_channel = project_comments_id(p)
    if not comments_channel and not CF_HOOK:
        log.warning("CURSEFORGE_WEBHOOK_URL not set, skipping CurseForge")
        return
    url, name = p["curseforge"], p["name"]
    first = "curseforge" not in ps
    seen = set(ps.get("curseforge", []))
    comments = scrape_comments(url, tag=re.sub(r"\W+", "_", name.lower()))
    log.info("%s / CurseForge: %d comments found", name, len(comments))
    if not comments:
        raise RuntimeError("0 comments parsed (blocked by CurseForge, the page has no comments, or the layout changed)")
    if comments_channel:
        destinations = ps.setdefault("curseforge_channels", {})
        delivered = set(destinations.get(comments_channel, []))
        if comments_channel not in destinations and not backfill:
            destinations[comments_channel] = sorted(c["id"] for c in comments)
            ps["curseforge"] = sorted(seen | {c["id"] for c in comments})
            save()
            return
        for c in comments:
            if c["id"] in delivered:
                continue
            if post_channel(comments_channel, comment_embed(c, name, url)):
                delivered.add(c["id"])
                seen.add(c["id"])
                destinations[comments_channel] = sorted(delivered)[-10000:]
                ps["curseforge"] = sorted(seen)
                save()
                log.info("%s: posted comment %s by %s", name, c["id"], c["author"])
        destinations[comments_channel] = sorted(delivered)[-10000:]
        save()
        return
    new = [c for c in comments if c["id"] not in seen]
    if first and not backfill:
        ps["curseforge"] = sorted(seen | {c["id"] for c in new})
        save()
        return
    ps.setdefault("curseforge", [])
    for c in new:
        sent = (post_channel(comments_channel, comment_embed(c, name, url)) if comments_channel
                else post_webhook(CF_HOOK, f"{name} · Comments", embed=comment_embed(c, name, url)))
        if sent:
            ps["curseforge"].append(c["id"])
            save()
            log.info("%s: posted comment %s by %s", name, c["id"], c["author"])


# ----------------------------------------------------------------- main
def alert(p: dict, kind: str, ps: dict, err: Exception | None) -> None:
    """Post ONE warning when a source starts failing; stay quiet until it recovers."""
    key = f"alert_{kind}"
    if err is None:
        ps.pop(key, None)
        return
    if ps.get(key):
        return
    label = "GitHub" if kind == "github" else "CurseForge"
    msg = f"⚠️ {p['name']}: the {label} check is failing: {str(err)[:300]}\nI'll stay quiet until it works again."
    if kind == "github":
        sent = notify_issues(p, msg)
    elif project_comments_id(p):
        sent = post_channel(project_comments_id(p), {"title": "⚠️ CurseForge watcher alert",
                          "description": clip(msg, 3800), "color": 0xD29922})
    else:
        sent = bool(CF_HOOK) and post_webhook(CF_HOOK, "Watcher", content=msg)
    if sent:
        ps[key] = True


def run_once(backfill: bool) -> None:
    state = load_state()
    projects = load_projects()
    ensure_managed_channels(state, projects)
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
            except Later as e:
                log.warning("%s / %s: %s", p["name"], kind, e)
                save()
                continue
            except Exception as e:  # one source failing must never stop the others
                log.error("%s / %s check failed: %s", p["name"], kind, e)
                err = e
                if isinstance(e, Blocked):
                    cf_blocked = True
            alert(p, kind, ps, err)
            save()

    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    update_dashboard(state, projects)
    save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--setup", action="store_true", help="create or reconcile the managed Discord layout, then exit")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--debug-curseforge", nargs="?", const="", metavar="NAME")
    a = ap.parse_args()

    if a.setup:
        if not MANAGED_MODE:
            sys.exit("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID to enable automatic Discord setup.")
        state = load_state()
        projects = load_projects()
        ensure_managed_channels(state, projects)
        update_dashboard(state, projects)
        save_state(state)
        print(f"Discord layout ready under the {CATEGORY_NAME!r} category.")
        return

    if a.test:
        if MANAGED_MODE:
            state = load_state()
            projects = load_projects()
            ensure_managed_channels(state, projects)
        if BOT_MODE and (FORUM_ID or any(p.get("github") for p in load_projects())):
            try:
                p = next((x for x in load_projects() if x.get("github")), None)
                forum_id = project_forum_id(p) if p else FORUM_ID
                tid = create_post("Watcher test", {"description": "✅ Bot works. Deleting this post again...", "color": 0x2DA44E}, [], forum_id)
                r = dapi("DELETE", f"/channels/{tid}")
                print("Issues (bot) ->", "ok" if r.ok else f"created, but could not delete it (HTTP {r.status_code}): bot needs 'Manage Threads'")
            except Exception as e:
                print("Issues (bot) -> FAILED:", e)
        elif GH_HOOK:
            ok = post_webhook(GH_HOOK, "Watcher · Issues", content="✅ Test message: webhook works.", thread_name="Watcher test")
            print("Issues (webhook) ->", "ok" if ok else "FAILED")
        else:
            print("Issues -> no GitHub project or issue forum configured")
        if CF_HOOK:
            print("Comments ->", "ok" if post_webhook(CF_HOOK, "Watcher · Comments", content="✅ Test message: webhook works.") else "FAILED")
        else:
            print("Comments -> webhook URL missing")
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

