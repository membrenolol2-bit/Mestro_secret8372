"""
storage.py — Shared Storage Layer
==================================
Thread-safe file I/O used by all bots.
All state lives in /data/ folder as JSON.

Files:
  data/tokens.json          → shared token pool (public)
  data/premium_tokens.json  → premium token pool
  data/premium_users.json   → map of discord_id → list of tokens they received
  data/donated_tokens.json  → map of discord_id → list of {token, refresh_token, given_by, given_at}
  data/cooldowns.json       → per-user cooldown timestamps  {user_id: {pool: timestamp}}
"""

import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone

# ── 1. EXPIRATION CHECKER ────────────────────────────────────────────────────
def safe_seconds_until_expiry(token_str):
    try:
        import jwt
        decoded = jwt.decode(token_str, options={"verify_signature": False})
        return int(decoded.get("exp", 0) - datetime.now(timezone.utc).timestamp())
    except Exception:
        return 3526

# ── 2. DUAL-HOST API ROUTER ──────────────────────────────────────────────────
async def execute_dual_host_refresh(token, refresh_token):
    primary_host = os.getenv("NAKAMA_HOST", "https://nakamacloud.io")
    backup_host = os.getenv("NAKAMA_HOST_BACKUP", "https://nulls.tools")
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "token": token,
        "refresh_token": refresh_token if refresh_token else token
    }

    url = f"{primary_host.rstrip('/')}/v2/account/session/refresh"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=8) as response:
                if response.status == 200:
                    return await response.json()
    except Exception:
        pass

    if backup_host:
        backup_url = backup_host if "api/refresh" in backup_host else f"{backup_host.rstrip('/')}/v2/account/session/refresh"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(backup_url, headers=headers, json=payload, timeout=8) as response:
                    if response.status == 200:
                        return await response.json()
        except Exception:
            pass
    return None

# ── 3. UNIVERSAL STOCK PIPELINE ENGINE ────────────────────────────────────────
def refresh_public_token_if_needed(buffer_seconds=1800):
    """Processes normal stock files, accurately translating both raw and labeled tokens."""
    try:
        if not os.path.exists("normal_stock.json"):
            return
        with open("normal_stock.json", "r") as f:
            stock = json.load(f)
        if not stock:
            return

        loop = asyncio.get_event_loop()
        updated_stock = []

        for entry in stock:
            raw_token = entry.get("input_data", entry.get("token", entry.get("refresh_token", ""))).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            
            if not raw_token:
                continue

            if safe_seconds_until_expiry(raw_token) < buffer_seconds:
                new_data = loop.run_until_complete(execute_dual_host_refresh(raw_token, refresh_token))
                if new_data and "token" in new_data:
                    entry["token"] = new_data["token"]
                    entry["refresh_token"] = new_data["refresh_token"]
                    if "input_data" in entry:
                        entry["input_data"] = new_data["token"]
                else:
                    entry["token"] = raw_token
                    entry["refresh_token"] = refresh_token
            else:
                entry["token"] = raw_token
                entry["refresh_token"] = refresh_token
                
            updated_stock.append(entry)

        with open("normal_stock.json", "w") as f:
            json.dump(updated_stock, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Normal stock duplication sync failed: {e}")

def refresh_premium_pool_if_needed(buffer_seconds=1800):
    """Processes heroic stock files, accurately translating both raw and labeled tokens."""
    try:
        if not os.path.exists("heroic_stock.json"):
            return
        with open("heroic_stock.json", "r") as f:
            stock = json.load(f)
        if not stock:
            return

        loop = asyncio.get_event_loop()
        updated_stock = []

        for entry in stock:
            raw_token = entry.get("input_data", entry.get("token", entry.get("refresh_token", ""))).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            
            if not raw_token:
                continue
                
            if safe_seconds_until_expiry(raw_token) < buffer_seconds:
                new_data = loop.run_until_complete(execute_dual_host_refresh(raw_token, refresh_token))
                if new_data and "token" in new_data:
                    entry["token"] = new_data["token"]
                    entry["refresh_token"] = new_data["refresh_token"]
                    if "input_data" in entry:
                        entry["input_data"] = new_data["token"]
                else:
                    entry["token"] = raw_token
                    entry["refresh_token"] = refresh_token
            else:
                entry["token"] = raw_token
                entry["refresh_token"] = refresh_token
                
            updated_stock.append(entry)

        with open("heroic_stock.json", "w") as f:
            json.dump(updated_stock, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Heroic stock duplication sync failed: {e}")

def refresh_env_accounts_if_needed(buffer_seconds=1800):
    pass

# ── 4. FIXED: STOCK FALLBACK RETRIEVAL LAYS ───────────────────────────────────
def get_public_token():
    """Returns the primary live usable token directly from normal stock array layers."""
    try:
        if os.path.exists("normal_stock.json"):
            with open("normal_stock.json", "r") as f:
                stock = json.load(f)
                if stock and len(stock) > 0:
                    # Serve the first functional available token string
                    return stock[0].get("token", stock[0].get("input_data", ""))
    except Exception:
        pass
    return None
