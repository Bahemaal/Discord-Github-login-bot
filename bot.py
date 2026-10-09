import os
import secrets
import sqlite3
import time
from html import escape
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/name"
CONTRIBUTOR_ROLE_ID = int(os.environ["CONTRIBUTOR_ROLE_ID"])
PANEL_CHANNEL_ID = int(os.environ["PANEL_CHANNEL_ID"])  # channel where the button message lives
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, raises the API rate limit

REDIRECT_URI = f"{PUBLIC_URL}/callback"
STATE_TTL = 600  # seconds the user has to finish GitHub authorization
MAX_CONTRIBUTOR_PAGES = 10  # 10 * 100 = up to 1000 contributors


# ---------- DB: GitHub <-> Discord links + panel message id ----------
db = sqlite3.connect("links.db")
db.execute(
    "CREATE TABLE IF NOT EXISTS links ("
    "github_login TEXT PRIMARY KEY, discord_id INTEGER NOT NULL)"
)
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
db.commit()


# ---------- GitHub API ----------
async def exchange_code(session: aiohttp.ClientSession, code: str) -> str:
    async with session.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        json={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    ) as r:
        data = await r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"GitHub did not return a token: {data.get('error_description', data)}")
    return token


async def get_github_login(session: aiohttp.ClientSession, user_token: str) -> str:
    async with session.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github+json"},
    ) as r:
        r.raise_for_status()
        return (await r.json())["login"]


async def is_contributor(session: aiohttp.ClientSession, login: str) -> bool:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

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


# ---------- Discord UI ----------
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

        state = secrets.token_urlsafe(32)
        # keep the interaction so we can send a private (ephemeral) follow-up later
        bot.pending[state] = (interaction, time.time() + STATE_TTL)

        url = "https://github.com/login/oauth/authorize?" + urlencode(
            {"client_id": GITHUB_CLIENT_ID, "redirect_uri": REDIRECT_URI, "state": state}
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Continue to GitHub", url=url))
        await interaction.response.send_message(
            f"Sign in with GitHub to verify that you contribute to `{GITHUB_REPO}`. "
            f"The link is valid for {STATE_TTL // 60} minutes.",
            view=view,
            ephemeral=True,
        )


async def notify(interaction: discord.Interaction, text: str):
    """Private message only the clicking user can see."""
    try:
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException as e:
        print("Follow-up failed:", e)


def page(message: str, status: int = 200) -> web.Response:
    html = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<body style='font-family:sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;text-align:center'>"
        f"<p style='font-size:1.2rem'>{message}</p>"
        "<p style='color:#666'>You can close this tab and return to Discord.</p></body>"
    )
    return web.Response(text=html, status=status, content_type="text/html")


# ---------- Bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        # state -> (interaction, expires_at)
        self.pending: dict[str, tuple[discord.Interaction, float]] = {}
        self._panel_checked = False

    async def setup_hook(self):
        self.add_view(VerifyView())

        app = web.Application()
        app.add_routes([web.get("/callback", self.handle_callback)])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        print(f"OAuth callback listening on :{PORT}")

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        if not self._panel_checked:
            self._panel_checked = True
            await self.ensure_panel()

    async def ensure_panel(self):
        """Post the button message once; repost only if it was deleted."""
        channel = self.get_channel(PANEL_CHANNEL_ID) or await self.fetch_channel(PANEL_CHANNEL_ID)
        row = db.execute("SELECT value FROM meta WHERE key = 'panel_message_id'").fetchone()
        if row:
            try:
                await channel.fetch_message(int(row[0]))
                return
            except discord.NotFound:
                pass

        embed = discord.Embed(
            title="Contributor verification",
            description=(
                f"Are you a contributor to **{GITHUB_REPO}**?\n"
                "Press the button, sign in with GitHub and get the contributor role."
            ),
            color=0x238636,
        )
        msg = await channel.send(embed=embed, view=VerifyView())
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('panel_message_id', ?)", (str(msg.id),))
        db.commit()

    def cleanup_pending(self):
        now = time.time()
        for s in [s for s, (_, exp) in self.pending.items() if exp < now]:
            del self.pending[s]

    async def handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        self.cleanup_pending()

        pending = self.pending.pop(state, None) if state else None
        if not code or not pending:
            return page("This link is invalid or has expired. Press the button in Discord again.", 400)
        interaction, _ = pending

        try:
            async with aiohttp.ClientSession() as session:
                user_token = await exchange_code(session, code)
                login = await get_github_login(session, user_token)
                contributor = await is_contributor(session, login)
        except Exception as e:
            print("GitHub error:", e)
            await notify(interaction, "Could not reach GitHub. Please try again later.")
            return page("Could not reach GitHub. Please try again later.", 502)

        if not contributor:
            await notify(interaction, "You are not a contributor to this project.")
            return page("You are not a contributor to this project.", 403)

        # one GitHub account -> one Discord account
        row = db.execute(
            "SELECT discord_id FROM links WHERE github_login = ?", (login.lower(),)
        ).fetchone()
        if row and row[0] != interaction.user.id:
            msg = "This GitHub account is already linked to another Discord user."
            await notify(interaction, msg)
            return page(msg, 409)

        guild = interaction.guild
        role = guild.get_role(CONTRIBUTOR_ROLE_ID)
        if not role:
            await notify(interaction, "The contributor role was not found. Please contact an admin.")
            return page("Role not found. Please contact an admin.", 500)

        try:
            member = await guild.fetch_member(interaction.user.id)
            await member.add_roles(role, reason=f"GitHub contributor: {login}")
        except discord.HTTPException as e:
            print("Discord error:", e)
            await notify(interaction, "Could not assign the role. Please contact an admin.")
            return page("Could not assign the role. Please contact an admin.", 500)

        db.execute(
            "INSERT OR REPLACE INTO links (github_login, discord_id) VALUES (?, ?)",
            (login.lower(), interaction.user.id),
        )
        db.commit()

        await notify(interaction, f"Verified as **{login}**. The contributor role has been assigned!")
        return page(f"Verified as <b>{escape(login)}</b>. Role assigned!")


bot = Bot()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
