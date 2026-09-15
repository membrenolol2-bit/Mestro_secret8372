"""
bot.py - Unified Discord Bot (single app, single CommandTree)
================================================================
All commands live under ONE discord.Client + ONE CommandTree,
because they all belong to the same Discord application.
Running separate Client instances with the same bot token
causes duplicate gateway sessions and command-sync race
conditions (429 rate limits, "CommandNotFound" errors).

Commands:
  /token               -> public pool (everyone, cooldown)
  /get-premium-token   -> premium pool (role/whitelist, cooldown)
  /add-premium-token   -> [ADMIN] add token to premium pool
  /add-premium-user    -> [ADMIN] whitelist a user for premium
  /status              -> live pool stats
  /donate-token        -> [ADMIN] gift a token to a user
  /my-tokens           -> user's gifted tokens
  /revoke-token        -> [ADMIN] revoke a user's gifted tokens
"""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button
from discord.ext import tasks
import asyncio
import os
import json
import sys
import traceback
from dotenv import load_dotenv

# Fix Windows console encoding BEFORE loading dotenv
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

load_dotenv()

from storage import (
    refresh_public_token_if_needed,
    refresh_premium_pool_if_needed,
    get_public_token_with_fallback,
    get_rotating_token,
    seconds_until_expiry,
    pop_premium_token,
    increment_premium_uses,
    add_premium_token,
    add_premium_user,
    is_premium_user,
    global_status,
    reset_all_cooldowns,
    set_permanent_cooldown,
    add_donated,
    get_donated,
    is_expired,
    format_time,
    check_cooldown,
    set_cooldown,
    revoke_donated,
    get_premium_pool,
    get_env_accounts,
    redeem_promo_key,
    check_blacklist_status,
    modify_blacklist_entry,
    log_name_change_submission,
    execute_nakama_name_update
)

# --- Config ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_USER_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing Discord token - set DISCORD_BOT_TOKEN or DISCORD_USER_TOKEN")

raw_guild_ids = os.getenv("ALLOWED_GUILD_IDS", "")
print(f"[CONFIG] Raw guild IDs: {raw_guild_ids!r}")

ALLOWED_GUILD_IDS = [
    int(g.strip())
    for g in raw_guild_ids.split(",")
    if g.strip().isdigit()
]

print(f"[CONFIG] Parsed guild IDs: {ALLOWED_GUILD_IDS}")
if not ALLOWED_GUILD_IDS:
    print("No ALLOWED_GUILD_IDS set - bot will work in all servers")
    print("   Consider setting ALLOWED_GUILD_IDS for better security")

print(f"[BOT] Raw ALLOWED_GUILD_IDS env: {os.getenv('ALLOWED_GUILD_IDS', '')!r}")
print(f"[BOT] Parsed ALLOWED_GUILD_IDS: {ALLOWED_GUILD_IDS}")

PUBLIC_COOLDOWN_SECONDS = int(os.getenv("PUBLIC_COOLDOWN_SECONDS", str(20 * 60)))

# --- Tier config ---
_t1_role = os.getenv("PREMIUM_TIER1_ROLE_ID", "")
_t2_role = os.getenv("PREMIUM_TIER2_ROLE_ID", "")
PREMIUM_TIER1_ROLE_ID  = int(_t1_role) if _t1_role.isdigit() else None
PREMIUM_TIER2_ROLE_ID  = int(_t2_role) if _t2_role.isdigit() else None
PREMIUM_TIER1_COOLDOWN = int(os.getenv("PREMIUM_TIER1_COOLDOWN", str(5 * 60)))
PREMIUM_TIER2_COOLDOWN = int(os.getenv("PREMIUM_TIER2_COOLDOWN", str(13 * 60)))

_legacy_role = os.getenv("PREMIUM_ROLE_ID", "")
if _legacy_role.isdigit() and PREMIUM_TIER2_ROLE_ID is None:
    PREMIUM_TIER2_ROLE_ID = int(_legacy_role)

ADMIN_ROLE_ID_STR = os.getenv("ADMIN_ROLE_ID", "")
ADMIN_ROLE_ID     = int(ADMIN_ROLE_ID_STR) if ADMIN_ROLE_ID_STR.isdigit() else None

ADMIN_USER_IDS = {
    s.strip()
    for s in os.getenv("ADMIN_USER_IDS", "").split(",")
    if s.strip().isdigit()
}

# --- Helpers ---
async def resolve_member(interaction: discord.Interaction) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member) and interaction.user.roles:
        return interaction.user

    if interaction.guild_id is None:
        return None

    guild = client.get_guild(interaction.guild_id)
    if guild is None:
        try:
            guild = await client.fetch_guild(interaction.guild_id)
        except (discord.Forbidden, discord.HTTPException):
            return None

    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.HTTPException):
            return None
    return member

async def has_admin_access(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if str(user.id) in ADMIN_USER_IDS:
        return True

    member = await resolve_member(interaction)
    if member is None:
        return False

    if isinstance(member, discord.Member):
        try:
            if member.guild_permissions.administrator:
                return True
        except Exception:
            pass
        if ADMIN_ROLE_ID and any(r is not None and r.id == ADMIN_ROLE_ID for r in (member.roles or [])):
            return True

    return False

def get_premium_tier(member: discord.Member, user_id: str) -> tuple[int, int] | None:
    role_ids = {r.id for r in (member.roles or []) if r is not None}

    if PREMIUM_TIER1_ROLE_ID and PREMIUM_TIER1_ROLE_ID in role_ids:
        return 1, PREMIUM_TIER1_COOLDOWN

    if PREMIUM_TIER2_ROLE_ID and PREMIUM_TIER2_ROLE_ID in role_ids:
        return 2, PREMIUM_TIER2_COOLDOWN

    if is_premium_user(user_id):
        return 2, PREMIUM_TIER2_COOLDOWN

    return None

ALLOWED_GUILD_IDS_STR = {str(g) for g in ALLOWED_GUILD_IDS}

def guild_allowed(interaction: discord.Interaction) -> bool:
    if not ALLOWED_GUILD_IDS:
        return True
    return str(interaction.guild_id) in ALLOWED_GUILD_IDS_STR

# --- Bot setup ---
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- /token (public pool) ---
@tree.command(name="token", description="Get your session token")
async def token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            print(f"[PUBLIC] Denied - incoming guild_id={interaction.guild_id!r} allowed={ALLOWED_GUILD_IDS!r}")
            await interaction.response.send_message(
                f"[ACCESS DENIED] Unauthorized server.\n"
                f"-# server id: `{interaction.guild_id}` | allowed: `{ALLOWED_GUILD_IDS}`",
                ephemeral=True
            )
            return

        user_id = str(interaction.user.id)
        tokens = get_rotating_token()
        if not tokens:
            await interaction.response.send_message(
                "No valid token available - add tokens to .env file",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(tokens["token"])
        payload = {
            "token": tokens["token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in": ttl,
            "_note": "Made by Mestro_ac",
        }

        account_num = tokens.get("account")
        account_note = f" (account {account_num})" if account_num else ""

        json_bytes = json.dumps(payload, indent=2).encode("utf-8")
        file = discord.File(
            fp=__import__("io").BytesIO(json_bytes),
            filename="token.json",
        )

        await interaction.response.send_message(
            f"Token{account_note}\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            file=file,
            ephemeral=True,
        )
        print(f"[PUBLIC] Token sent to {interaction.user} ({interaction.user.id}){account_note}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PUBLIC] Error: {e}")
        traceback.print_exc()

# --- /get-premium-token ---
@tree.command(name="get-premium-token", description="Get a premium session token (buyers only)")
async def get_premium_token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        member  = await resolve_member(interaction)

        if not member:
            await interaction.response.send_message(
                "Member lookup failed - bot may be missing Server Members Intent.",
                ephemeral=True,
            )
            return

        tier_info = get_premium_tier(member, user_id)

        if tier_info is None:
            hints = []
            if PREMIUM_TIER1_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER1_ROLE_ID}>")
            if PREMIUM_TIER2_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER2_ROLE_ID}>")
            role_hint = " or ".join(hints) if hints else "a buyer role"
            await interaction.response.send_message(
                f"Premium Required - only buyers with {role_hint} can use this.",
                ephemeral=True,
            )
            return

        tier_num, cooldown_secs = tier_info

        token_entry = pop_premium_token()
        if not token_entry:
            await interaction.response.send_message(
                "Premium pool is empty - ask an admin to run /add-premium-token.",
                ephemeral=True,
            )
            return

        increment_premium_uses(user_id)

        ttl = seconds_until_expiry(token_entry["token"])
        payload = {
            "token":         token_entry["token"],
            "refresh_token": token_entry["refresh_token"],
            "expires_in":    ttl,
            "tier":          tier_num,
            "_note": "Made by Mestro_ac",
        }

        await interaction.response.send_message(
            f"Premium Token (Tier {tier_num})\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] Token sent to {interaction.user} ({user_id}) - tier {tier_num}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PREMIUM] Error: {e}")
        traceback.print_exc()

# --- /add-premium-token ---
@tree.command(name="add-premium-token", description="[ADMIN] Add a token to the premium pool")
@app_commands.describe(token="JWT bearer token", refresh_token="JWT refresh token")
async def add_premium_token_cmd(interaction: discord.Interaction, token: str, refresh_token: str):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                "Invalid tokens - must be JWT strings starting with ey...",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"Token already expired ({ttl}s remaining) - use a fresh one.",
                ephemeral=True,
            )
            return

        new_size = add_premium_token(token, refresh_token)
        await interaction.response.send_message(
            f"Token added to premium pool\n"
            f"```json\n{json.dumps({'pool_size': new_size, 'token_expires_in': ttl}, indent=2)}\n```",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /add-premium-user ---
@tree.command(name="add-premium-user", description="[ADMIN] Grant premium access to a user")
@app_commands.describe(user="Discord user to grant premium access")
async def add_premium_user_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        add_premium_user(str(user.id), str(interaction.user.id))
        await interaction.response.send_message(
            f"{user.mention} now has premium access - can use /get-premium-token.",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /status ---
@tree.command(name="status", description="View live token pool status")
async def status_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        s = global_status()
        pub  = s["public"]
        prem = s["premium"]
        don  = s["donated"]

        payload = {
            "public_pool": {
                "status": "active" if pub["valid"] else "expired",
                "expires_in": pub["expires_in"],
            },
            "premium_pool": {
                "valid_tokens": prem["valid"],
                "expired_tokens": prem["expired"],
                "total_tokens": prem["total"],
            },
            "donated_tokens": {
                "users_with_gifts": don["users_with_donated"],
                "total_donated": don["total_donated_tokens"],
            },
            "premium_users_whitelisted": s["premium_users"],
        }

        await interaction.response.send_message(
            f"System Status\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /view-tokens ---
@tree.command(name="view-tokens", description="[ADMIN] View all available tokens")
async def view_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        public_token = get_public_token_with_fallback()[0]
        premium_pool = get_premium_pool()
        env_accounts = get_env_accounts()

        payload = {
            "public_token": public_token if public_token else "None or expired",
            "premium_pool": premium_pool,
            "env_accounts": env_accounts,
            "_note": "Made by Mestro_ac",
        }

        json_bytes = json.dumps(payload, indent=2).encode("utf-8")
        file = discord.File(
            fp=__import__("io").BytesIO(json_bytes),
            filename="all_tokens.json",
        )

        await interaction.response.send_message(
            "All Available Tokens",
            file=file,
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /remove-cooldown-all ---
@tree.command(name="remove-cooldown-all", description="[ADMIN] Remove all cooldowns (including permanent) for all users")
async def remove_cooldown_all_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        count = reset_all_cooldowns()
        await interaction.response.send_message(
            f"Removed all cooldowns (including permanent) for {count} users",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /donate-token ---
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
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                "Invalid tokens - must be JWT strings starting with ey...",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"Token already expired ({ttl}s remaining).",
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
            f"Token donated to {user.mention}\n"
            f"```json\n{json.dumps({'expires_in': ttl, 'recipient': str(user.id), 'given_by': str(interaction.user.id)}, indent=2)}\n```\n"
            f">>> They can claim it with /my-tokens.",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /my-tokens ---
@tree.command(name="my-tokens", description="See all tokens gifted to you")
async def my_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)

        on_cd, remaining = check_cooldown(user_id, "my_tokens", 60)
        if on_cd:
            await interaction.response.send_message(
                f"Slow down - try again in `{format_time(remaining)}`.",
                ephemeral=True,
            )
            return
        set_cooldown(user_id, "my_tokens")

        donated = get_donated(user_id)
        if not donated:
            await interaction.response.send_message(
                "No gifted tokens - ask an admin to run /donate-token for you.",
                ephemeral=True,
            )
            return

        valid = [t for t in donated if not is_expired(t["token"])]
        expired_count = len(donated) - len(valid)

        if not valid:
            await interaction.response.send_message(
                f"All {expired_count} gifted token(s) have expired - ask an admin for a new one.",
                ephemeral=True,
            )
            return

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

        raw = json.dumps(payload, indent=2)
        header = f"Your Gifted Tokens - `{len(valid)}` valid, `{expired_count}` expired\n"

        if len(header) + len(raw) + 10 <= 1990:
            await interaction.response.send_message(
                f"{header}```json\n{raw}\n```",
                ephemeral=True,
            )
        else:
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

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- /revoke-token ---
@tree.command(name="revoke-token", description="[ADMIN] Remove all donated tokens from a user")
@app_commands.describe(user="User whose donated tokens to revoke")
async def revoke_token_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("Admin Only.", ephemeral=True)
            return

        count = revoke_donated(str(user.id))
        await interaction.response.send_message(
            f"Revoked `{count}` donated token(s) from {user.mention}."
            if count > 0 else
            f"{user.mention} had no donated tokens to revoke.",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.response.send_message(f"Error: `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()

# --- Part A: Dashboard View & /dashboard ---
class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get Token",
        style=discord.ButtonStyle.primary,
        custom_id="dashboard_get_token",
    )
    async def get_token_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            user_id = str(interaction.user.id)
            on_cd, remaining = check_cooldown(user_id, "public", PUBLIC_COOLDOWN_SECONDS)
            if on_cd:
                await interaction.response.send_message(
                    f"Cooldown Active - available in `{format_time(remaining)}`",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            tokens, source = get_public_token_with_fallback()
            if not tokens:
                await interaction.followup.send("No valid token available inside databases.", ephemeral=True)
                return

            set_cooldown(user_id, "public")

            payload = {
                "token": tokens["token"],
                "refresh_token": tokens["refresh_token"]
            }
            await interaction.followup.send(f"Token Sent\n```json\n{json.dumps(payload, indent=2)}\n```", ephemeral=True)

        except Exception as e:
            try:
                await interaction.followup.send(f"Error: `{e}`", ephemeral=True)
            except Exception:
                pass
            traceback.print_exc()

@tree.command(name="dashboard", description="[ADMIN] Post a token button visible to everyone")
async def dashboard_cmd(interaction: discord.Interaction):
    if not await has_admin_access(interaction):
        await interaction.response.send_message("Admin Only.", ephemeral=True)
        return
    embed = discord.Embed(title="Token Station", description="Press the button below to claim a unique session token.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=DashboardView())

# --- Part B: Logger & Modals ---
async def send_to_log_channel(client, title, description, color=discord.Color.blue()):
    try:
        log_channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID", "1549154833372549200")
        if not log_channel_id: return
        channel = client.get_channel(int(log_channel_id))
        if channel:
            embed = discord.Embed(title=title, description=description, color=color)
            embed.set_footer(text="Mestro Tokens Master Monitor Logs")
            filename = "normal_stock.json"
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                await channel.send(embed=embed, file=discord.File(filename, filename=filename))
            else:
                await channel.send(embed=embed)
    except Exception as e: 
        print(f"[LOG_ERROR] Alert failed: {e}")

def append_universal_token(token_data_str):
    filename = "normal_stock.json"
    stock = []
    try:
        if os.path.exists(filename):
            with open(filename, "r") as f: stock = json.load(f)
    except Exception: pass
    stock.append({"token": token_data_str.strip(), "refresh_token": token_data_str.strip(), "_source_type": "user_donated"})
    with open(filename, "w") as f: json.dump(stock, f, indent=2)

class SingleTokenModal(Modal, title="Donate a Token"):
    token_input = TextInput(label="Paste Access Token / Session Key", style=discord.TextStyle.long, required=True)
    def __init__(self, client): super().__init__(); self.client = client
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        append_universal_token(self.token_input.value.strip())
        await interaction.followup.send("Success! Token addition saved directly!", ephemeral=True)
        await send_to_log_channel(self.client, "Token Received", f"Donor: {interaction.user.mention}", color=discord.Color.green())

class MestroDonationDashboardView(View):
    def __init__(self, client): super().__init__(timeout=None); self.client = client
    @discord.ui.button(label="Donate a Token", style=discord.ButtonStyle.success, custom_id="donate_single_universal")
    async def donate_single_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleTokenModal(self.client))

# --- Part C: Identity Changer ---
class NameChangerModal(Modal, title="Update Game Nickname"):
    token_input = TextInput(label="Paste Access Token", style=discord.TextStyle.long, required=True)
    name_input = TextInput(label="Enter Desired Nickname", max_length=15, required=True)
    def __init__(self, client): super().__init__(); self.client = client
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from storage import execute_nakama_name_update, log_name_change_submission
        tok = self.token_input.value.strip(); name = self.name_input.value.strip()
        res = await execute_nakama_name_update(tok, name)
        if res["status"] == "error":
            await interaction.followup.send(f"Failed: {res['msg']}", ephemeral=True); return
        log_name_change_submission(interaction.user.id, tok, name)
        await interaction.followup.send(f"Success! Name changed to `{name}`!", ephemeral=True)

class MestroNameDashboardView(View):
    def __init__(self, client): super().__init__(timeout=None); self.client = client
    @discord.ui.button(label="Change Token Name", style=discord.ButtonStyle.primary, custom_id="mestro_change_name_btn")
    async def name_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NameChangerModal(self.client))

# --- Part D: Automation Loops & System Setup ---
def setup_donation_dashboard(tree):
    @tree.client.event
    async def on_ready():
        tree.client.add_view(DashboardView())
        tree.client.add_view(MestroDonationDashboardView(tree.client))
        tree.client.add_view(MestroNameDashboardView(tree.client))
        print(f"[BOT] Connected as {tree.client.user}")
        for guild in list(tree.client.guilds):
            from storage import check_blacklist_status
            if check_blacklist_status(guild.id): await guild.leave()
        try:
            guild_obj = discord.Object(id=1549154833372549200)
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
        except Exception: pass
        log_channel = tree.client.get_channel(1549154833372549200)
        if log_channel:
            diff_text = "```diff\nFixed:\n+ tokens\nAdded:\n- None\n```"
            await log_channel.send(embed=discord.Embed(title="Bot Online", description=f"{diff_text}", color=discord.Color.gold()))

    @tree.command(name="block", description="[OWNER EXCLUSIVE] Blacklist a user or server guild ID")
    async def block_command(interaction: discord.Interaction, target_id: str):
        if interaction.user.id != 1447021186835025921:
            await interaction.response.send_message("Access Denied.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); from storage import modify_blacklist_entry
        if modify_blacklist_entry(target_id, "add"):
            await interaction.followup.send("Blacklisted successfully.", ephemeral=True)
            try:
                g = tree.client.get_guild(int(target_id.strip()))
                if g: await g.leave()
            except Exception: pass
        else: await interaction.followup.send("Error updating database.", ephemeral=True)

    @tree.command(name="global_live_stock", description="Live stock metrics checking layout")
    async def global_live_stock(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False); from storage import safe_seconds_until_expiry
        filename = "normal_stock.json"; active_lines = []
        if os.path.exists(filename):
            with open(filename, "r") as f: stock = json.load(f)
            for i, entry in enumerate(stock, 1):
                tok = entry.get("token", entry.get("input_data", ""))
                if safe_seconds_until_expiry(tok) > 0: active_lines.append(f"Token {i} | {int(safe_seconds_until_expiry(tok)//60)}m")
        embed = discord.Embed(title="Live Stock Monitor", color=discord.Color.green())
        embed.add_field(name="Stock Status", value="\n".join(active_lines) if active_lines else "Empty", inline=False)
        await interaction.followup.send(embed=embed)

    @tasks.loop(seconds=10)
    async def token_refresh_loop():
        try:
            from refresh_token import run_stock_auto_refresh
            if run_stock_auto_refresh():
                await send_to_log_channel(tree.client, "Stock Rotated", "Verified and refreshed.", color=discord.Color.blue())
        except Exception:
            try:
                refresh_public_token_if_needed()
                refresh_premium_pool_if_needed()
            except Exception as e:
                print(f"Loop Error: {e}")

    @token_refresh_loop.before_loop
    async def before_token_refresh():
        await tree.client.wait_until_ready()
        token_refresh_loop.start()

# --- Main Runtime Initiator ---
if __name__ == "__main__":
    setup_donation_dashboard(tree)
    print("[BOT] Launching connection gateway layers...")
    client.run(BOT_TOKEN)
