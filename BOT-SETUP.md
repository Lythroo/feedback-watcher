# Feedback Watcher setup

The bot watches every project in `projects.json`. GitHub issues become individual posts in a project forum; later GitHub comments are copied into the matching post. CurseForge comments go to a project comments channel. Closed issues are marked and archived by default, and reopened issues return to the active list.

## Discord layout

With `DISCORD_GUILD_ID` configured, the bot creates and reuses a `Feedback Watcher` category containing:

- `watcher-dashboard` — an automatically refreshed project and status panel
- `<project>-issues` — a forum for the project's GitHub issues, with project and issue-type tags
- `<project>-comments` — a channel for CurseForge comments, when that project has a CurseForge URL

It creates missing channels on startup and reuses matching channels on later runs. Adding a project to `projects.json` adds its channels automatically. Removing a project stops polling it; existing channels and their history are retained.

## 1. Create and invite the bot

Create a Discord application and bot at <https://discord.com/developers/applications>. Copy its bot token and application ID. No privileged gateway intents are required.

Invite it to your server with these permissions:

- View Channels, Send Messages, Embed Links, Read Message History
- Manage Channels (create the category and project channels)
- Manage Threads (create, update, archive, and reopen issue posts)

The bot role must be allowed to see and manage channels in the server. Do not share the bot token.

## 2. Configure GitHub Actions secrets

In the repository that runs the workflow, open **Settings → Secrets and variables → Actions** and add:

- `DISCORD_BOT_TOKEN` — the bot token
- `DISCORD_GUILD_ID` — the Discord server ID (enable Developer Mode, then right-click the server and copy its ID)
- `ISSUES_WEBHOOK` — optional fallback for GitHub issues if bot forum access is unavailable
- `COMMENTS_WEBHOOK` — optional fallback for CurseForge comments if automatic channel setup is not used
- `ISSUES_FORUM_ID` — optional legacy mode for an existing shared forum; leave unset for the per-project managed forums

The workflow supplies `GITHUB_TOKEN` automatically. A separate GitHub token is optional for public repositories and helps avoid API rate limits.

## 3. Configure projects and run

Edit `projects.json` to add or remove mods. Each entry can have a `github` repository (`owner/repo`), a `curseforge` page URL, or both. The supplied configuration includes Simple Blood, Falling Wind, and Wavelength.

Commit the files to a GitHub repository with Actions enabled. Run **Actions → Watch mod feedback → Run workflow** once to create the layout and start syncing. The schedule checks every 15 minutes. `state.json` is committed back to the repository to prevent duplicate posts.

For a local one-time setup, install `requirements.txt`, set `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID`, then run `python bot.py --setup`. `python bot.py --test` checks the issue forum or configured webhook. Use `python bot.py --debug-curseforge [project name]` to inspect comment scraping without posting anything.

## Closing issues

The workflow defaults to `ON_CLOSE: archive`: the bot updates the issue card, adds the `Closed` tag, posts a closing note, then locks and archives the thread. Set it to `mark` to leave the thread open or `delete` to remove it permanently.

## Existing installations

The bot keeps the old `ISSUES_FORUM_ID` and webhook paths available. To switch to automatic per-project forums, set `DISCORD_GUILD_ID` and remove `ISSUES_FORUM_ID` after reviewing where your existing posts live. Discord does not support moving an existing forum post between forums; keep the old forum available as an archive. The bot adopts old webhook-created threads in whichever configured forum it is syncing.

