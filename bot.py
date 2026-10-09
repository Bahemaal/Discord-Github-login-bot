import asyncio
import os
import sqlite3
import time

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/name"
CONTRIBUTOR_ROLE_ID = int(os.environ["CONTRIBUTOR_ROLE_ID"])
PANEL_CHANNEL_ID = int(os.environ["PANEL_CHANNEL_ID"])  # channel where the button message lives
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, raises the API rate limit

MAX_CONTRIBUTOR_PAGES = 10  # 10 * 100 = up to 1000 contributors
# Discord interaction tokens live 15 minutes, keep a safety margin
MAX_FLOW_SECONDS = 14 * 60

GITHUB_JSON = {"Accept": "application/json"}


# ---------- DB: GitHub <-> Discord links + panel message id ----------
db = sqlite3.connect("links.db")
db.execute(
    "CREATE TABLE IF NOT EXISTS links ("
    "github_login TEXT PRIMARY KEY, discord_id INTEGER NOT NULL)"
)
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.commit()


# ---------- GitHub Device Flow ----------
class FlowError(Exception):
    """An error whose message is safe to show to the user."""


async def request_device_code(session: aiohttp.ClientSession) -> dict:
    async with session.post(
        "https://github.com/login/device/code",
        headers=GITHUB_JSON,
        data={"client_id": GITHUB_CLIENT_ID},
    ) as r:
        data = await r.json()
    if "device_code" not in data:
        # most common cause: "Enable Device Flow" is not ticked in the OAuth App settings
        raise RuntimeError(f"Device code request failed: {data}")
    return data


async def poll_for_token(session: aiohttp.ClientSession, device_code: str, interval: int, deadline: float) -> str:
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        async with session.post(
            "https://github.com/login/oauth/access_token",
            headers=GITHUB_JSON,
            data={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        ) as r:
            data = await r.json()

        if "access_token" in data:
            return data["access_token"]

        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = data.get("interval", interval + 5)
            continue
        if error == "access_denied":
            raise FlowError("Authorization was cancelled on GitHub.")
        if error == "expired_token":
            break
        raise RuntimeError(f"Unexpected GitHub response: {data}")

    raise FlowError("The code has expired. Press the button again to get a new one.")


# ---------- GitHub API ----------
async def get_github_login(session: aiohttp.ClientSession, user_token: str) -> str:
    async with session.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github+json"},
    ) as r:
        r.raise_for_status()
        return (await r.json())["login"]


async def is_contributor(session: aiohttp.ClientSession, login: str, api_token: str) -> bool:
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {api_token}"}

    target = login.lower()
    for page in range(1, MAX_CONTRIBUTOR_PAGES + 1):
        async with session.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contributors",
            headers=headers,
            params={"per_page": 100, "page": page},
        ) as r:
            if r.status == 204:  # empty repository
                return False
            r.raise_for_status()
            items = await r.json()

        if any(c.get("login", "").lower() == target for c in items):
            return True
        if len(items) < 100:
            return False
    return False


# ---------- Verification flow ----------
async def respond(interaction: discord.Interaction, content: str, view: discord.ui.View | None):
    """Update the private (ephemeral) message the user is looking at."""
    try:
        await interaction.edit_original_response(content=content, view=view)
    except discord.HTTPException as e:
        print("Could not update ephemeral message:", e)


async def grant_role(interaction: discord.Interaction, login: str) -> str:
    """Assign the role and remember the link. Returns the text to show the user."""
    row = db.execute("SELECT discord_id FROM links WHERE github_login = ?", (login.lower(),)).fetchone()
    if row and row[0] != interaction.user.id:
        return "This GitHub account is already linked to another Discord user."

    guild = interaction.guild
    role = guild.get_role(CONTRIBUTOR_ROLE_ID)
    if not role:
        return "The contributor role was not found. Please contact an admin."

    try:
        member = await guild.fetch_member(interaction.user.id)
        await member.add_roles(role, reason=f"GitHub contributor: {login}")
    except discord.HTTPException as e:
        print("Discord error:", e)
        return "Could not assign the role. Please contact an admin."

    db.execute(
        "INSERT OR REPLACE INTO links (github_login, discord_id) VALUES (?, ?)",
        (login.lower(), interaction.user.id),
    )
    db.commit()
    return f"Verified as **{login}**. The contributor role has been assigned!"


async def verify_flow(interaction: discord.Interaction):
    try:
        async with aiohttp.ClientSession() as session:
            data = await request_device_code(session)

            seconds = min(int(data["expires_in"]), MAX_FLOW_SECONDS)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Open GitHub", url=data["verification_uri"]))
            await respond(
                interaction,
                f"Open GitHub, enter this code and confirm:\n# `{data['user_code']}`\n"
                f"The code is valid for {seconds // 60} minutes. "
                "The role is assigned automatically as soon as you're done.",
                view,
            )

            user_token = await poll_for_token(
                session, data["device_code"], int(data.get("interval", 5)), time.monotonic() + seconds
            )
            login = await get_github_login(session, user_token)
            contributor = await is_contributor(session, login, GITHUB_TOKEN or user_token)

        if contributor:
            text = await grant_role(interaction, login)
        else:
            text = "You are not a contributor to this project."
    except FlowError as e:
        text = str(e)
    except asyncio.CancelledError:
        raise  # a newer click replaced this flow, say nothing
    except Exception as e:
        print("Verification error:", repr(e))
        text = "Something went wrong while talking to GitHub. Please try again later."

    await respond(interaction, text, None)


class VerifyView(discord.ui.View):
    """Persistent view: the button keeps working after bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify with GitHub",
        style=discord.ButtonStyle.primary,
        custom_id="github_verify",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(CONTRIBUTOR_ROLE_ID)
        if role and role in interaction.user.roles:
            await interaction.response.send_message("You already have the contributor role.", ephemeral=True)
            return

        # thinking=True is required: for button clicks a plain defer() means "edit the
        # message the button is on", which would overwrite the public panel
        await interaction.response.defer(ephemeral=True, thinking=True)

        # a new click replaces the user's previous unfinished attempt
        uid = interaction.user.id
        old = bot.active.get(uid)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(verify_flow(interaction))
        bot.active[uid] = task
        task.add_done_callback(lambda t, uid=uid: bot.active.pop(uid, None) if bot.active.get(uid) is t else None)


# ---------- Bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.active: dict[int, asyncio.Task] = {}
        self._panel_checked = False

    async def setup_hook(self):
        self.add_view(VerifyView())

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        if not self._panel_checked:
            self._panel_checked = True
            await self.ensure_panel()

    async def ensure_panel(self):
        """Post the button message once; repost only if it was deleted."""
        channel = self.get_channel(PANEL_CHANNEL_ID) or await self.fetch_channel(PANEL_CHANNEL_ID)
        embed = discord.Embed(
            title="Contributor verification",
            description=(
                f"Are you a contributor to **{GITHUB_REPO}**?\n"
                "Press the button, confirm a short code on GitHub and get the contributor role."
            ),
            color=0x238636,
        )

        row = db.execute("SELECT value FROM meta WHERE key = 'panel_message_id'").fetchone()
        if row:
            try:
                existing = await channel.fetch_message(int(row[0]))
                # restore content/button in case the message was edited or damaged
                await existing.edit(content=None, embed=embed, view=VerifyView())
                return
            except discord.NotFound:
                pass

        msg = await channel.send(embed=embed, view=VerifyView())
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('panel_message_id', ?)", (str(msg.id),))
        db.commit()


bot = Bot()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)