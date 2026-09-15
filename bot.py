"""
bot.py — Unified Discord Bot (single app, single CommandTree)
================================================================
All commands live under ONE discord.Client + ONE CommandTree,
because they all belong to the same Discord application.
Running separate Client instances with the same bot token
causes duplicate gateway sessions and command-sync race
conditions (429 rate limits, "CommandNotFound" errors).

Commands:
  /token               → public pool (everyone, cooldown)
  /get-premium-token    → premium pool (role/whitelist, cooldown)
  /add-premium-token    → [ADMIN] add token to premium pool
  /add-premium-user     → [ADMIN] whitelist a user for premium
  /status                → live pool stats
  /donate-token          → [ADMIN] gift a token to a user
  /my-tokens              → user's gifted tokens
  /revoke-token           → [ADMIN] revoke a user's gifted tokens
"""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button
import asyncio
import os
import json
import sys
from discord import app_commands
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
   revoke_donated,
    get_premium_pool,
    get_env_accounts,
    redeem_promo_key,
    check_blacklist_status,
    modify_blacklist_entry
)

# ── Config ────────────────────────────────────────────────────────────────────
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

# ── Tier config ───────────────────────────────────────────────────────────────
# Tier 1 → cooldown corto  (5 min por default)
# Tier 2 → cooldown largo  (13 min por default)
# Si el usuario tiene ambos roles, gana el tier más corto (tier 1)
_t1_role = os.getenv("PREMIUM_TIER1_ROLE_ID", "")
_t2_role = os.getenv("PREMIUM_TIER2_ROLE_ID", "")
PREMIUM_TIER1_ROLE_ID  = int(_t1_role) if _t1_role.isdigit() else None
PREMIUM_TIER2_ROLE_ID  = int(_t2_role) if _t2_role.isdigit() else None
PREMIUM_TIER1_COOLDOWN = int(os.getenv("PREMIUM_TIER1_COOLDOWN", str(5 * 60)))   # 300s
PREMIUM_TIER2_COOLDOWN = int(os.getenv("PREMIUM_TIER2_COOLDOWN", str(13 * 60)))  # 780s

# Backward-compat: PREMIUM_ROLE_ID suelto → tier 2 si no se configuró tier 2
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


# ── Helpers ───────────────────────────────────────────────────────────────────
async def resolve_member(interaction: discord.Interaction) -> discord.Member | None:
    """
    Get the invoking member with their roles populated.
    Slash command interactions sometimes carry a partial guild object,
    so we go through client.get_guild() → fetch_member to guarantee
    we get a fully-hydrated Member (including roles).
    """
    # interaction.user is already a Member when the guild is cached
    if isinstance(interaction.user, discord.Member) and interaction.user.roles:
        return interaction.user

    if interaction.guild_id is None:
        return None

    # Use the client's cached guild (fully populated) instead of the
    # partial interaction.guild object
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
    """
    Resolves the invoking member fully before checking permissions.
    Always pass the full interaction — never a bare user/member object.
    """
    user = interaction.user
    # Fast path: ADMIN_USER_IDS check needs no guild data
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
    """
    Returns (tier_number, cooldown_seconds) for the best tier the user qualifies for,
    or None if the user has no premium access at all.
    Tier 1 beats Tier 2 (shorter cooldown wins).
    Whitelisted users (/add-premium-user) get Tier 2 by default.
    """
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
    # If no guild IDs are set, allow all servers (user token mode)
    if not ALLOWED_GUILD_IDS:
        return True
    return str(interaction.guild_id) in ALLOWED_GUILD_IDS_STR


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── /token (public pool) ───────────────────────────────────────────────────────
@tree.command(name="token", description="Get your session token")
async def token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            print(f"[PUBLIC] 🚫 Denied — incoming guild_id={interaction.guild_id!r} "
                  f"(type={type(interaction.guild_id).__name__}) "
                  f"allowed={ALLOWED_GUILD_IDS!r} "
                  f"match={interaction.guild_id in ALLOWED_GUILD_IDS}")
            await interaction.response.send_message(
                f"🚫 **Access Denied** — unauthorized server.\n"
                f"-# server id: `{interaction.guild_id}` | allowed: `{ALLOWED_GUILD_IDS}` | "
                f"match: `{interaction.guild_id in ALLOWED_GUILD_IDS}`",
                ephemeral=True
            )
            return

        user_id = str(interaction.user.id)

        # Use rotating token from env accounts
        tokens = get_rotating_token()
        if not tokens:
            await interaction.response.send_message(
                " **No valid token available** — add tokens to .env file",
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
            f"✅ **Token**{account_note}\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            file=file,
            ephemeral=True,
        )
        print(f"[PUBLIC] ✅ Token sent to {interaction.user} ({interaction.user.id}){account_note}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PUBLIC]  Error: {e}")
        traceback.print_exc()


# ── /get-premium-token ────────────────────────────────────────────────────────
@tree.command(name="get-premium-token", description="Get a premium session token (buyers only)")
async def get_premium_token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        member  = await resolve_member(interaction)

        # ── Debug log ─────────────────────────────────────────────────
        member_roles = [r.id for r in (member.roles or []) if r is not None] if member else []
        print(
            f"[PREMIUM] user={interaction.user} ({user_id}) | "
            f"member={'OK' if member else 'NONE ← intents/fetch failed'} | "
            f"TIER1_ROLE={PREMIUM_TIER1_ROLE_ID!r} | "
            f"TIER2_ROLE={PREMIUM_TIER2_ROLE_ID!r} | "
            f"role_ids={member_roles} | "
            f"whitelist={is_premium_user(user_id)}"
        )
        # ─────────────────────────────────────────────────────────────

        if not member:
            await interaction.response.send_message(
                " Member lookup failed — bot may be missing **Server Members Intent**.",
                ephemeral=True,
            )
            return

        tier_info = get_premium_tier(member, user_id)

        if tier_info is None:
            # Build a hint showing which roles grant access
            hints = []
            if PREMIUM_TIER1_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER1_ROLE_ID}>")
            if PREMIUM_TIER2_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER2_ROLE_ID}>")
            role_hint = " or ".join(hints) if hints else "a buyer role"
            await interaction.response.send_message(
                f"💎 **Premium Required** — only buyers with {role_hint} can use this.",
                ephemeral=True,
            )
            return

        tier_num, cooldown_secs = tier_info

        token_entry = pop_premium_token()
        if not token_entry:
            await interaction.response.send_message(
                " **Premium pool is empty** — ask an admin to run `/add-premium-token`.",
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
            f"💎 **Premium Token** (Tier {tier_num})\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] ✅ Token sent to {interaction.user} ({user_id}) — tier {tier_num}, cooldown {cooldown_secs}s")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PREMIUM]  Error: {e}")
        traceback.print_exc()


# ── /add-premium-token ────────────────────────────────────────────────────────
@tree.command(name="add-premium-token", description="[ADMIN] Add a token to the premium pool")
@app_commands.describe(token="JWT bearer token", refresh_token="JWT refresh token")
async def add_premium_token_cmd(interaction: discord.Interaction, token: str, refresh_token: str):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                " **Invalid tokens** — must be JWT strings starting with `ey...`",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f" **Token already expired** ({ttl}s remaining) — use a fresh one.",
                ephemeral=True,
            )
            return

        new_size = add_premium_token(token, refresh_token)
        await interaction.response.send_message(
            f"✅ **Token added to premium pool**\n"
            f"```json\n{json.dumps({'pool_size': new_size, 'token_expires_in': ttl}, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] ➕ Token added by {interaction.user} — pool now {new_size}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /add-premium-user ─────────────────────────────────────────────────────────
@tree.command(name="add-premium-user", description="[ADMIN] Grant premium access to a user")
@app_commands.describe(user="Discord user to grant premium access")
async def add_premium_user_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        add_premium_user(str(user.id), str(interaction.user.id))
        await interaction.response.send_message(
            f"✅ **{user.mention} now has premium access** — can use `/get-premium-token`.",
            ephemeral=True,
        )
        print(f"[PREMIUM] ✅ {interaction.user} granted premium to {user} ({user.id})")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /status ───────────────────────────────────────────────────────────────────
@tree.command(name="status", description="View live token pool status")
async def status_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
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
            f"📊 **System Status**\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[STATUS] Checked by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /view-tokens ───────────────────────────────────────────────────────────────
@tree.command(name="view-tokens", description="[ADMIN] View all available tokens")
async def view_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        # Get public token
        public_token = get_public_token()
        
        # Get premium pool
        premium_pool = get_premium_pool()
        
        # Get env accounts
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
            f"🔑 **All Available Tokens**",
            file=file,
            ephemeral=True,
        )
        print(f"[VIEW_TOKENS] Tokens viewed by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /remove-cooldown-all ───────────────────────────────────────────────────────
@tree.command(name="remove-cooldown-all", description="[ADMIN] Remove all cooldowns (including permanent) for all users")
async def remove_cooldown_all_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        count = reset_all_cooldowns()
        await interaction.response.send_message(
            f"✅ **Removed all cooldowns (including permanent) for {count} users**",
            ephemeral=True,
        )
        print(f"[COOLDOWN] All cooldowns removed by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /add-cooldown ───────────────────────────────────────────────────────────────
@tree.command(name="add-cooldown", description="[ADMIN] Add permanent cooldown to a specific user")
@app_commands.describe(user="Discord user to add cooldown to", pool="Pool type (public/premium)")
async def add_cooldown_cmd(interaction: discord.Interaction, user: discord.Member, pool: str = "public"):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if pool not in ["public", "premium"]:
            await interaction.response.send_message(" **Pool must be 'public' or 'premium'**", ephemeral=True)
            return

        set_permanent_cooldown(str(user.id), pool)
        await interaction.response.send_message(
            f"✅ **Added permanent {pool} cooldown to {user.mention}**",
            ephemeral=True,
        )
        print(f"[COOLDOWN] Permanent {pool} cooldown added to {user} by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /add-cooldown-all ──────────────────────────────────────────────────────────
@tree.command(name="add-cooldown-all", description="[ADMIN] Add permanent cooldowns to all users")
@app_commands.describe(pool="Pool type (public/premium)")
async def add_cooldown_all_cmd(interaction: discord.Interaction, pool: str = "public"):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if pool not in ["public", "premium"]:
            await interaction.response.send_message(" **Pool must be 'public' or 'premium'**", ephemeral=True)
            return

        # Get all members in the guild and add permanent cooldown to all
        guild = client.get_guild(interaction.guild_id)
        if not guild:
            await interaction.response.send_message(" **Could not access guild**", ephemeral=True)
            return

        count = 0
        async for member in guild.fetch_members(limit=None):
            set_permanent_cooldown(str(member.id), pool)
            count += 1

        await interaction.response.send_message(
            f"✅ **Added permanent {pool} cooldowns to {count} users**",
            ephemeral=True,
        )
        print(f"[COOLDOWN] Permanent {pool} cooldowns added to all users by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


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
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                " **Invalid tokens** — must be JWT strings starting with `ey...`",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f" **Token already expired** ({ttl}s remaining).",
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
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR]  Error in /donate-token: {e}")
        traceback.print_exc()


# ── /my-tokens ────────────────────────────────────────────────────────────────
@tree.command(name="my-tokens", description="See all tokens gifted to you")
async def my_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
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
                f" **All {expired_count} gifted token(s) have expired** — ask an admin for a new one.",
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
        header = f"🎁 **Your Gifted Tokens** — `{len(valid)}` valid, `{expired_count}` expired\n"

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

        print(f"[DONOR] 📋 /my-tokens: sent {len(valid)} tokens to {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR]  Error in /my-tokens: {e}")
        traceback.print_exc()


# ── /reset-cooldown ───────────────────────────────────────────────────────────
@tree.command(name="reset-cooldown", description="[ADMIN] Reset cooldowns for a user or everyone")
@app_commands.describe(
    user="User to reset (leave empty to reset ALL users)",
    pool="Which cooldown to reset (leave empty to reset all pools)",
)
@app_commands.choices(pool=[
    app_commands.Choice(name="public",    value="public"),
    app_commands.Choice(name="premium",   value="premium"),
    app_commands.Choice(name="my_tokens", value="my_tokens"),
])
async def reset_cooldown_cmd(
    interaction: discord.Interaction,
    user: discord.Member = None,
    pool: str = None,
):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        data = _read(COOLDOWNS_FILE, {})

        if user:
            # Reset one user
            uid = str(user.id)
            if uid not in data:
                await interaction.response.send_message(
                    f" **{user.mention}** has no active cooldowns.", ephemeral=True
                )
                return
            if pool:
                data[uid].pop(pool, None)
                desc = f"pool `{pool}`"
            else:
                del data[uid]
                desc = "all pools"
            _write(COOLDOWNS_FILE, data)
            await interaction.response.send_message(
                f"✅ Cooldown reset for {user.mention} — {desc}.", ephemeral=True
            )
            print(f"[ADMIN] 🔄 {interaction.user} reset cooldown for {user} ({desc})")
        else:
            # Reset everyone
            if pool:
                for uid in data:
                    data[uid].pop(pool, None)
                desc = f"pool `{pool}` for all users"
            else:
                data = {}
                desc = "all pools for all users"
            _write(COOLDOWNS_FILE, data)
            await interaction.response.send_message(
                f"✅ Cooldowns reset — {desc}.", ephemeral=True
            )
            print(f"[ADMIN] 🔄 {interaction.user} reset cooldowns — {desc}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[ADMIN]  Error in /reset-cooldown: {e}")
        traceback.print_exc()


# ── /revoke-token ─────────────────────────────────────────────────────────────
@tree.command(name="revoke-token", description="[ADMIN] Remove all donated tokens from a user")
@app_commands.describe(user="User whose donated tokens to revoke")
async def revoke_token_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        count = revoke_donated(str(user.id))
        await interaction.response.send_message(
            f" **Revoked `{count}` donated token(s)** from {user.mention}."
            if count > 0 else
            f" **{user.mention} had no donated tokens** to revoke.",
            ephemeral=True,
        )
        if count > 0:
            print(f"[DONOR]  {interaction.user} revoked {count} tokens from {user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DONOR]  Error in /revoke-token: {e}")
        traceback.print_exc()


# ── Dashboard Button ──────────────────────────────────────────────────────────
class DashboardView(discord.ui.View):
    """Persistent view — survives bot restarts (timeout=None)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get Token",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="dashboard_get_token",   # persistent ID
    )
    async def get_token_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            user_id = str(interaction.user.id)

            on_cd, remaining = check_cooldown(user_id, "public", PUBLIC_COOLDOWN_SECONDS)
            if on_cd:
                await interaction.response.send_message(
                    f"⏱️ **Cooldown Active** — available in `{format_time(remaining)}`",
                    ephemeral=True,
                )
                return

            tokens, source = get_public_token_with_fallback()
            if not tokens:
                await interaction.response.send_message(
                    " **No valid token available** — try again in 30 seconds.",
                    ephemeral=True,
                )
                return

            set_cooldown(user_id, "public")

            ttl = seconds_until_expiry(tokens["token"])
            payload = {
                "token":         tokens["token"],
                "refresh_token": tokens["refresh_token"],
                "expires_in":    ttl,
                "next_use_in":   PUBLIC_COOLDOWN_SECONDS,
            }

            fallback_note = (
                "\n> ⚡ *Public token refreshing — backup token served*"
                if source != "public" else ""
            )

            await interaction.response.send_message(
                f"✅ **Token** | Next use in `{format_time(PUBLIC_COOLDOWN_SECONDS)}`{fallback_note}\n"
                f"```json\n{json.dumps(payload, indent=2)}\n```",
                ephemeral=True,
            )
            print(f"[DASHBOARD] ✅ Token sent to {interaction.user} ({user_id}) — source: {source}")

        except Exception as e:
            try:
                await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
            except Exception:
                pass
            print(f"[DASHBOARD]  Error in button: {e}")
            traceback.print_exc()


# ── /dashboard ────────────────────────────────────────────────────────────────
@tree.command(name="dashboard", description="[ADMIN] Post a token button visible to everyone")
async def dashboard_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎫 Token Station",
            description=(
                "Press the button below to get your session token.\n"
                f"Cooldown: **{format_time(PUBLIC_COOLDOWN_SECONDS)}** per user."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Token is only visible to you • Ephemeral")

        await interaction.response.send_message(
            embed=embed,
            view=DashboardView(),
        )
        print(f"[DASHBOARD] 📌 Posted by {interaction.user} in channel {interaction.channel_id}")

    except Exception as e:
        try:
            await interaction.response.send_message(f" **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[DASHBOARD]  Error in /dashboard: {e}")
        traceback.print_exc()


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    # Register persistent views so buttons survive restarts
    client.add_view(DashboardView())

    if ALLOWED_GUILD_IDS:
        # Sync to specific guilds
        guild_objects = [discord.Object(id=gid) for gid in ALLOWED_GUILD_IDS]
        for guild_obj in guild_objects:
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
        print(f"\n{'='*55}")
        print(f"✅ [BOT] Connected as: {client.user}")
        print(f"✅ Synced {len(synced)} commands to guilds: {ALLOWED_GUILD_IDS}")
        print(f"✅ Commands: {[c.name for c in synced]}")
        print(f"✅ Dashboard view registered (persistent)")
        print(f"{'='*55}\n")
    else:
        # Sync globally (user token mode)
        synced = await tree.sync()
        print(f"\n{'='*55}")
        print(f"✅ [BOT] Connected as: {client.user}")
        print(f"✅ Synced {len(synced)} commands globally")
        print(f"✅ Commands: {[c.name for c in synced]}")
        print(f"✅ Dashboard view registered (persistent)")
        print(f"✅ Running in user token mode - commands available in all servers")
        print(f"{'='*55}\n")

@client.event
async def on_disconnect():
    print("[BOT]  Disconnected")

@client.event
async def on_resumed():
    print("[BOT] ✅ Reconnected")

# Note: Use main.py to start the bot with token input
# This file can also be run directly if DISCORD_BOT_TOKEN is set
# ── MESTRO TOKENS: DATABASE & LOGGER STORAGE ENGINE ────────────────────────────
async def send_to_log_channel(client, title, description, color=discord.Color.blue()):
    try:
        log_channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID")
        if not log_channel_id: return
        channel = client.get_channel(int(log_channel_id))
        if channel:
            embed = discord.Embed(title=title, description=description, color=color)
            embed.set_footer(text="Mestro Tokens Live Monitor")
            await channel.send(embed=embed)
    except Exception as e: print(f"[LOG_ERROR] Alert failed: {e}")

def append_universal_token(token_data_str, pool_type="normal"):
    filename = "heroic_stock.json" if pool_type == "heroic" else "normal_stock.json"
    stock = []
    try:
        if os.path.exists(filename):
            with open(filename, "r") as f: stock = json.load(f)
    except Exception: pass
    stock.append({"input_data": token_data_str.strip(), "_source_type": "user_donated"})
    with open(filename, "w") as f: json.dump(stock, f, indent=2)
# ── MESTRO TOKENS: USER MODAL POPUP SUBMISSION WINDOWS ────────────────────────
class SingleTokenModal(Modal, title="Donate a Token"):
    token_input = TextInput(label="Paste Access Token / Session Key", style=discord.TextStyle.long, placeholder="Paste your token string here...", required=True)
    def __init__(self, client): super().__init__(); self.client = client
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); raw_data = self.token_input.value.strip()
        append_universal_token(raw_data, pool_type="normal")
        await interaction.followup.send("🎉 **Success!** Your token donation has been safely added to the pool!", ephemeral=True)
        await send_to_log_channel(self.client, "🎁 Single Token Received", f"**Donor:** {interaction.user.mention} (`{interaction.user.id}`)\n**Target Database:** `normal_stock.json`", color=discord.Color.green())

class MultipleTokensModal(Modal, title="Donate Multiple Tokens"):
    tokens_input = TextInput(label="Paste Multiple Tokens Below", style=discord.TextStyle.paragraph, placeholder="token 1: [paste first token]\ntoken 2: [paste second token]", required=True)
    def __init__(self, client): super().__init__(); self.client = client
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); raw_data = self.tokens_input.value.strip()
        append_universal_token(raw_data, pool_type="normal")
        await interaction.followup.send("🎉 **Success!** Your multiple token submissions have been saved directly!", ephemeral=True)
        await send_to_log_channel(self.client, "📦 Bulk Tokens Received", f"**Donor:** {interaction.user.mention} (`{interaction.user.id}`)\n**Target Database:** `normal_stock.json` \n\n*Multi-line text block has been recorded.*", color=discord.Color.purple())
# ── MESTRO TOKENS: AUTOMATED ACTIVATION SYSTEM ───────────────────────────────
def setup_donation_dashboard(tree):
    @tree.client.event
    async def on_ready():
        print(f"[BOT] Unified system connected as {tree.client.user}")
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            try:
                guild_object = discord.Object(id=int(guild_id)); tree.copy_global_to(guild=guild_object)
                synced = await tree.sync(guild=guild_object); print(f"[SYNC] Guild specific commands synced instantly: {len(synced)}")
            except Exception as e: print(f"[SYNC_ERROR] Guild tracking sync failed: {e}")
        else:
            try: await tree.sync(); print("[SYNC] Global application commands sent.")
            except Exception as e: print(f"[SYNC_ERROR] Global sync failed: {e}")
        await send_to_log_channel(tree.client, "🚀 Bot System Online", "**Mestro Tokens Dashboard** has successfully initialized.\nAll application slash commands are synced, and channel interaction loops are **Active**.", color=discord.Color.gold())

    @tree.command(name="donate_dashboard", description="Launch the customized Mestro Tokens donation menu panel")
    async def donate_dashboard(interaction: discord.Interaction):
        embed = discord.Embed(title="🎁 Mestro Tokens Donation Center", description="Click the button panels below to support active system pool stock rotations!\n\n> To submit multiple tokens at once, click **Donate Multiple Tokens** and group your lines clearly using numbers like `token 1:`, `token 2:`, etc.", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, view=MestroDonationDashboardView(tree.client))

    @tree.command(name="add", description="Force-add a fresh token to normal stock using only its refresh token")
    @app_commands.describe(refresh_token="Paste the raw cryptographic refresh token string here")
    async def add_token_command(interaction: discord.Interaction, refresh_token: str):
        await interaction.response.defer(ephemeral=True)
        admin_env = os.getenv("ADMIN_USER_IDS", "")
        admin_list = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip()]
        if interaction.user.id not in admin_list:
            await interaction.followup.send("❌ **Access Denied:** Only authorized administrators can manually supply entries.", ephemeral=True); return
        clean_refresh = refresh_token.strip()
        if not clean_refresh or len(clean_refresh) < 10:
            await interaction.followup.send("❌ **Error:** Invalid refresh token formatting.", ephemeral=True); return
        try:
            filename = "normal_stock.json"; stock = []
            if os.path.exists(filename):
                with open(filename, "r") as f: stock = json.load(f)
            stock.append({"token": clean_refresh, "refresh_token": clean_refresh, "_source_type": "admin_forced_upload"})
            with open(filename, "w") as f: json.dump(stock, f, indent=2)
        except Exception as file_err:
            await interaction.followup.send(f"❌ **Database Error:** Failed to commit token string: {file_err}", ephemeral=True); return
        await interaction.followup.send(f"🚀 **Success!** Refresh token has been appended directly into your active `normal_stock.json` pool. It will duplicate next minute!", ephemeral=True)
        await send_to_log_channel(tree.client, "⚡ Admin Supply Action", f"**Administrator:** {interaction.user.mention} (`{interaction.user.id}`)\n**Action:** Forced custom token entry via `/add` command.\n**Target Pool:** Normal Stock Line Array (`normal_stock.json`)", color=discord.Color.red())

    @tree.command(name="gen_key", description="[ADMIN] Generate a premium redeem key code string")
    @app_commands.describe(uses="How many total members can claim this code", role="The premium role to grant upon claim")
    async def gen_key_command(interaction: discord.Interaction, uses: int, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        if not await has_admin_access(interaction):
            await interaction.followup.send("🚫 **Access Denied:** Admins only.", ephemeral=True); return
        import random, string
        rand_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6)); new_key = f"MESTRO-{rand_str}"
        filename = "promo_keys.json"; keys = {}
        if os.path.exists(filename):
            try:
                with open(filename, "r") as f: keys = json.load(f)
            except Exception: pass
        keys[new_key] = {"uses": uses, "role_id": role.id, "claimed_by": []}
        with open(filename, "w") as f: json.dump(keys, f, indent=2)
        await interaction.followup.send(f"🔑 **Key Generated successfully!**\nCode: `{new_key}`\nMax Uses: `{uses}`\nGrants Role: {role.mention}\n\n*Copy this string code and share it with your community!*", ephemeral=True)

    @tree.command(name="redeem", description="Claim a custom key code to unlock premium hub role access")
    @app_commands.describe(key="Paste your active premium code string here")
    async def redeem_command(interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True); from storage import redeem_promo_key
        result = redeem_promo_key(key, interaction.user.id)
        if result["status"] == "error":
            await interaction.followup.send(result["msg"], ephemeral=True); return
        member = await resolve_member(interaction); role = interaction.guild.get_role(int(result["role_id"]))
        if not member or not role:
            await interaction.followup.send("❌ **System Error:** Could not assign role parameters. Verify bot role ordering permissions.", ephemeral=True); return
        try: await member.add_roles(role)
        except Exception as err:
            await interaction.followup.send(f"❌ **Permissions Error:** Failed to assign role: {err}", ephemeral=True); return
        await interaction.followup.send(f"🎉 **Congratulations!** Key `{key.upper()}` claimed successfully! You have been granted the **{role.name}** access role status!", ephemeral=True)
        await send_to_log_channel(tree.client, "🔑 Key Code Redeemed", f"**Member:** {interaction.user.mention} (`{interaction.user.id}`)\n**Code Claimed:** `{key.upper()}`\n**Granted Privilege Role:** {role.mention}\n**Uses Remaining:** `{result['remaining']}`", color=discord.Color.teal())

    # ── MESTRO TOKENS: HARD OWNER SECURITY BLACKLIST PIPELINE ────────────────
    @tree.command(name="block", description="[OWNER EXCLUSIVE] Hard-blacklist a user ID or server guild ID from using this bot")
    @app_commands.describe(target_id="Enter the raw Discord User ID or Guild Server ID string")
    async def block_command(interaction: discord.Interaction, target_id: str):
        if interaction.user.id != 1447021186835025921:
            await interaction.response.send_message("❌ **Critical Security Error:** You are not authorized to invoke owner configurations.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); from storage import modify_blacklist_entry
        if modify_blacklist_entry(target_id, "add"):
            await interaction.followup.send(f"🛡️ **Blacklist Updated:** ID `{target_id}` has been barred from the system.", ephemeral=True)
            # Instantly drop bot if target is an active guild server connection
            guild = tree.client.get_guild(int(target_id))
            if guild: await guild.leave()
        else: await interaction.followup.send("❌ Failed to update database sheet layers.", ephemeral=True)

    @tree.command(name="unblock", description="[OWNER EXCLUSIVE] Remove a user ID or server guild ID from the blacklist")
    @app_commands.describe(target_id="Enter the blocked Discord User ID or Guild Server ID string")
    async def unblock_command(interaction: discord.Interaction, target_id: str):
        if interaction.user.id != 1447021186835025921:
            await interaction.response.send_message("❌ **Critical Security Error:** Access Denied.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); from storage import modify_blacklist_entry
        if modify_blacklist_entry(target_id, "remove"):
            await interaction.followup.send(f"🔓 **Blacklist Cleared:** ID `{target_id}` can now use the bot again.", ephemeral=True)
        else: await interaction.followup.send("❌ Failed to clear database record.", ephemeral=True)

    # ── MESTRO TOKENS: PUBLIC LIVE INVENTORY DIAGNOSTIC ─────────────────────
    @tree.command(name="global_live_stock", description="Display a complete live operational analysis of all system pool stock")
    async def global_live_stock(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False); from storage import safe_seconds_until_expiry
        filename = "normal_stock.json"; active_lines, dead_lines = [], []
        if os.path.exists(filename):
            try:
                with open(filename, "r") as f: stock = json.load(f)
                for i, entry in enumerate(stock, 1):
                    tok = entry.get("token", entry.get("input_data", ""))
                    if safe_seconds_until_expiry(tok) > 0: active_lines.append(f"🟢 `Token {i}` | Active Lifetime: {int(safe_seconds_until_expiry(tok)//60)}m")
                    else: dead_lines.append(f"🔴 `Token {i}` | Session Status: Expired / Stale")
            except Exception: pass
        
        embed = discord.Embed(title="📈 Mestro Tokens Live Stock Diagnostic", color=discord.Color.green())
        embed.add_field(name=f"📦 Working Stock ({len(active_lines)})", value="\n".join(active_lines) if active_lines else "None available", inline=False)
        embed.add_field(name=f"⚠️ Expired Stock ({len(dead_lines)})", value="\n".join(dead_lines) if dead_lines else "None recorded", inline=False)
        await interaction.followup.send(embed=embed)

    @tree.client.event
    async def on_ready():
        print(f"[BOT] Core systems connected as {tree.client.user}")
        
        # 1. Automatic Server Block Security Guard Enforcement check
        for guild in list(tree.client.guilds):
            from storage import check_blacklist_status
            if check_blacklist_status(guild.id):
                print(f"[SECURITY] Auto-dropped blacklisted server connection node: {guild.id}")
                await guild.leave()
                
        # 2. Sync configurations instantly directly to target testing guild parameters
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            try:
                guild_obj = discord.Object(id=int(guild_id)); tree.copy_global_to(guild=guild_obj)
                await tree.sync(guild=guild_obj)
            except Exception: pass
            
        # 3. IMMEDIATELY dispatch raw update diff block straight to your logs channel id
        log_channel = tree.client.get_channel(1549154833372549200)
        if log_channel:
            diff_text = "```diff\nFixed:\n+ tokens\nAdded:\n- None\nRemoved:\n- None\n```"
            embed = discord.Embed(title="🚀 System Update / Bot Online", description=f"**Mestro Tokens** has successfully redeployed and initialized command sync trees.\n\n{diff_text}", color=discord.Color.gold())
            await log_channel.send(embed=embed)

if __name__ == "__main__":
    setup_donation_dashboard(tree)
    print("[BOT] Launching connection gateway layers...")
    client.run(BOT_TOKEN)
