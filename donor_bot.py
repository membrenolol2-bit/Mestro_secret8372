"""
donor_bot.py — Token Donation System
======================================
Commands:
  /donate-token  → [ADMIN] Da un token a un usuario específico
  /my-tokens     → [USER] Ve sus tokens recibidos en JSON
  /revoke-token  → [ADMIN] Revoca todos los tokens de un usuario
"""

import json
import os
import traceback
import discord
from discord import app_commands

from storage import (
    add_donated,
    get_donated,
    revoke_donated,
    check_cooldown,
    set_cooldown,
    format_time,
    is_expired,
    seconds_until_expiry,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("DONOR_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_GUILD_IDS = [
    int(g.strip())
    for g in os.getenv("ALLOWED_GUILD_IDS", "").split(",")
    if g.strip().isdigit()
]
ADMIN_ROLE_ID_STR = os.getenv("ADMIN_ROLE_ID", "")
ADMIN_ROLE_ID = int(ADMIN_ROLE_ID_STR) if ADMIN_ROLE_ID_STR.isdigit() else None

if not BOT_TOKEN:
    raise ValueError("❌ Missing DONOR_BOT_TOKEN / DISCORD_BOT_TOKEN")


# ── Helpers ───────────────────────────────────────────────────────────────────
def has_admin_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in member.roles):
        return True
    return False


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── /donate-token ─────────────────────────────────────────────────────────────
@tree.command(name="donate-token", description="[ADMIN] Gift a token to a specific user")
@app_commands.describe(
    user="Discord user to receive the token",
    token="JWT bearer token",
    refresh_token="JWT refresh token",
)
async def donate_token_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    token: str,
    refresh_token: str,
):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if member is None and interaction.guild:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                member = None
        if not member or not has_admin_access(member):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                "❌ **Invalid tokens** — must be JWT strings starting with `ey...`",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"❌ **Token already expired** ({ttl}s remaining).",
                ephemeral=True,
            )
            return

        add_donated(
            target_id=str(user.id),
            token=token,
            refresh_token=refresh_token,
            given_by=str(interaction.user.id),
        )

        await interaction.response.send_message(
            f"🎁 **Token donated to {user.mention}**\n"
            f"```json\n{json.dumps({'expires_in': ttl, 'recipient': str(user.id), 'given_by': str(interaction.user.id)}, indent=2)}\n```\n"
            f">>> They can claim it with `/my-tokens`.",
            ephemeral=True,
        )
        print(f"[DONOR] 🎁 Token donated to {user} ({user.id}) by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR] ❌ Error in /donate-token: {e}")
        traceback.print_exc()


# ── /my-tokens ────────────────────────────────────────────────────────────────
@tree.command(name="my-tokens", description="See all tokens gifted to you")
async def my_tokens_cmd(interaction: discord.Interaction):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)

        on_cd, remaining = check_cooldown(user_id, "my_tokens", 60)
        if on_cd:
            await interaction.response.send_message(
                f"⏱️ Slow down — try again in `{format_time(remaining)}`.",
                ephemeral=True,
            )
            return
        set_cooldown(user_id, "my_tokens")

        donated = get_donated(user_id)
        if not donated:
            await interaction.response.send_message(
                "🎁 **No gifted tokens** — ask an admin to run `/donate-token` for you.",
                ephemeral=True,
            )
            return

        valid = [t for t in donated if not is_expired(t["token"])]
        expired_count = len(donated) - len(valid)

        if not valid:
            await interaction.response.send_message(
                f"⚠️ **All {expired_count} gifted token(s) have expired** — ask an admin for a new one.",
                ephemeral=True,
            )
            return

        # Build JSON payload — one entry per valid token
        payload = []
        for i, t in enumerate(valid, 1):
            ttl = seconds_until_expiry(t["token"])
            payload.append({
                "gift": i,
                "token": t["token"],
                "refresh_token": t["refresh_token"],
                "expires_in": ttl,
                "given_by": t["given_by"],
            })

        # Discord message cap is 2000 chars — split if needed
        raw = json.dumps(payload, indent=2)

        header = (
            f"🎁 **Your Gifted Tokens** — `{len(valid)}` valid, `{expired_count}` expired\n"
        )

        if len(header) + len(raw) + 10 <= 1990:
            await interaction.response.send_message(
                f"{header}```json\n{raw}\n```",
                ephemeral=True,
            )
        else:
            # Send one token per message
            await interaction.response.send_message(
                f"{header}*(Sending {len(valid)} token(s) separately)*",
                ephemeral=True,
            )
            for entry in payload:
                chunk = json.dumps(entry, indent=2)
                await interaction.followup.send(
                    f"```json\n{chunk}\n```",
                    ephemeral=True,
                )

        print(f"[DONOR] 📋 /my-tokens: sent {len(valid)} tokens to {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR] ❌ Error in /my-tokens: {e}")
        traceback.print_exc()


# ── /revoke-token ─────────────────────────────────────────────────────────────
@tree.command(name="revoke-token", description="[ADMIN] Remove all donated tokens from a user")
@app_commands.describe(user="User whose donated tokens to revoke")
async def revoke_token_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if member is None and interaction.guild:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                member = None
        if not member or not has_admin_access(member):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        count = revoke_donated(str(user.id))
        await interaction.response.send_message(
            f"🗑️ **Revoked `{count}` donated token(s)** from {user.mention}."
            if count > 0 else
            f"ℹ️ **{user.mention} had no donated tokens** to revoke.",
            ephemeral=True,
        )
        if count > 0:
            print(f"[DONOR] 🗑️ {interaction.user} revoked {count} tokens from {user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR] ❌ Error in /revoke-token: {e}")
        traceback.print_exc()


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    print(f"\n{'='*55}")
    print(f"✅ [DONOR BOT] Connected as: {client.user}")
    print(f"✅ Admin role ID: {ADMIN_ROLE_ID}")
    print(f"{'='*55}\n")

@client.event
async def on_disconnect():
    print("[DONOR BOT] ⚠️ Disconnected")

@client.event
async def on_resumed():
    print("[DONOR BOT] ✅ Reconnected")

if __name__ == "__main__":
    print("[DONOR BOT] Starting...")
    client.run(BOT_TOKEN)

# ── UNIVERSAL LOGGING AND DONATION DASHBOARD PIPELINE ─────────────────────────
import discord
from discord.ui import Modal, TextInput, View, Button
import os
import json

async def send_to_log_channel(client, title, description, color=discord.Color.blue()):
    """Broadcasts a real-time status update embed directly into the private server logging channel."""
    try:
        log_channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID")
        if not log_channel_id:
            return
            
        channel = client.get_channel(int(log_channel_id))
        if channel:
            embed = discord.Embed(title=title, description=description, color=color)
            embed.set_footer(text="Mestro Tokens System Monitor")
            await channel.send(embed=embed)
    except Exception as e:
        print(f"[LOG_ERROR] Could not broadcast system alert: {e}")

def append_universal_token(token_data_str, pool_type="normal"):
    """Appends raw text token entries straight into the local database JSON storage stock file."""
    filename = "heroic_stock.json" if pool_type == "heroic" else "normal_stock.json"
    stock = []
    try:
        if os.path.exists(filename):
            with open(filename, "r") as f:
                stock = json.load(f)
    except Exception:
        stock = []
        
    stock.append({
        "input_data": token_data_str.strip(),
        "_source_type": "user_donated"
    })
    
    with open(filename, "w") as f:
        json.dump(stock, f, indent=2)

# ── 1. SINGLE TOKEN DONATION POPUP ───────────────────────────────────────────
class SingleTokenModal(Modal, title="Donate a Token"):
    token_input = TextInput(
        label="Paste Access Token / Session Key",
        style=discord.TextStyle.long,
        placeholder="Paste your token string here...",
        required=True
    )

    def __init__(self, client):
        super().__init__()
        self.client = client

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw_data = self.token_input.value.strip()
        
        append_universal_token(raw_data, pool_type="normal")
        await interaction.followup.send("🎉 **Success!** Your token donation has been safely added to the pool!", ephemeral=True)
        
        await send_to_log_channel(
            self.client, 
            "🎁 Single Token Received", 
            f"**Donor:** {interaction.user.mention} (`{interaction.user.id}`)\n**Target:** Normal Stock Pool\n\n*Raw input data has been appended into normal_stock.json.*",
            color=discord.Color.green()
        )

# ── 2. MULTIPLE TOKENS DONATION POPUP ────────────────────────────────────────
class MultipleTokensModal(Modal, title="Donate Multiple Tokens"):
    tokens_input = TextInput(
        label="Paste Multiple Tokens Below",
        style=discord.TextStyle.paragraph,
        placeholder="Label them clearly, for example:\ntoken 1: [paste first token]\ntoken 2: [paste second token]",
        required=True
    )

    def __init__(self, client):
        super().__init__()
        self.client = client

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw_data = self.tokens_input.value.strip()
        
        append_universal_token(raw_data, pool_type="normal")
        await interaction.followup.send("🎉 **Success!** Your multiple token submissions have been saved directly!", ephemeral=True)
        
        await send_to_log_channel(
            self.client, 
            "📦 Bulk Tokens Received", 
            f"**Donor:** {interaction.user.mention} (`{interaction.user.id}`)\n**Target:** Normal Stock Pool\n\n*The multi-line input text block has been recorded directly into normal_stock.json.*",
            color=discord.Color.purple()
        )

# ── 3. BUTTON INTERFACE LAYOUT VIEW PANEL ────────────────────────────────────
class MestroDonationDashboardView(View):
    def __init__(self, client):
        super().__init__(timeout=None)
        self.client = client
        
    @discord.ui.button(label="Donate a Token", style=discord.ButtonStyle.success, custom_id="donate_single_universal")
    async def donate_single_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleTokenModal(self.client))
        
    @discord.ui.button(label="Donate Multiple Tokens", style=discord.ButtonStyle.primary, custom_id="donate_multiple_universal")
    async def donate_multiple_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MultipleTokensModal(self.client))

# ── 4. SLASH COMMAND & SYSTEM REBOOT ROUTER ──────────────────────────────────
def setup_donation_dashboard(tree):
    # Sends a log broadcast automatically whenever the bot performs a boot setup update
    @tree.client.event
    async def on_ready():
        print(f"[BOT] Unified system connected as {tree.client.user}")
        await send_to_log_channel(
            tree.client, 
            "🚀 Bot System Online", 
            "**Mestro Tokens Dashboard** has successfully initialized.\nAll active dashboard interaction layers and button listeners are online.",
            color=discord.Color.gold()
        )

    @tree.command(name="donate_dashboard", description="Launch the customized Mestro Tokens donation command menu panel")
    async def donate_dashboard(interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎁 Mestro Tokens Donation Center", 
            description="Click the button panels below to support active system pool stock rotations!\n\n> To submit multiple tokens at once, click **Donate Multiple Tokens** and group your lines clearly using numbers like `token 1:`, `token 2:`, etc.", 
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=MestroDonationDashboardView(tree.client))
