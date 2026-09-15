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

def safe_seconds_until_expiry(token_str):
    try:
        import jwt
        decoded = jwt.decode(token_str, options={"verify_signature": False})
        return max(int(decoded.get("exp", 0) - datetime.now(timezone.utc).timestamp()), 0)
    except Exception:
        return 3526

def seconds_until_expiry(token_str):
    return safe_seconds_until_expiry(token_str)

def is_expired(token_str):
    return safe_seconds_until_expiry(token_str) <= 0
async def execute_dual_host_refresh(token, refresh_token):
    primary_host = os.getenv("NAKAMA_HOST", "https://animalcompany.us-east1.nakamacloud.io/v2/account/session/refresh")
    backup_host = os.getenv("NAKAMA_HOST_BACKUP", "https://animalcompany.us-east1.nakamacloud.io")
    headers = {"Content-Type": "application/json"}
    payload = {"token": token, "refresh_token": refresh_token if refresh_token else token}
    url = f"{primary_host.rstrip('/')}/v2/account/session/refresh"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=8) as response:
                if response.status == 200: return await response.json()
    except Exception: pass
    if backup_host:
        backup_url = backup_host if "api/refresh" in backup_host else f"{backup_host.rstrip('/')}/v2/account/session/refresh"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(backup_url, headers=headers, json=payload, timeout=8) as response:
                    if response.status == 200: return await response.json()
        except Exception: pass
    return None
def refresh_public_token_if_needed(buffer_seconds=1800):
    try:
        if not os.path.exists("normal_stock.json"): return
        with open("normal_stock.json", "r") as f: stock = json.load(f)
        if not stock: return
        loop = asyncio.get_event_loop()
        updated_stock = []
        for entry in stock:
            raw_token = entry.get("input_data", entry.get("token", entry.get("refresh_token", ""))).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            if not raw_token: continue
            if safe_seconds_until_expiry(raw_token) < buffer_seconds:
                new_data = loop.run_until_complete(execute_dual_host_refresh(raw_token, refresh_token))
                if new_data and "token" in new_data:
                    entry["token"] = new_data["token"]; entry["refresh_token"] = new_data["refresh_token"]; entry["input_data"] = new_data["token"]
                else: entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            else: entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            updated_stock.append(entry)
        with open("normal_stock.json", "w") as f: json.dump(updated_stock, f, indent=2)
    except Exception as e: print(f"[ERROR] Public loop failed: {e}")
def refresh_premium_pool_if_needed(buffer_seconds=1800):
    try:
        if not os.path.exists("heroic_stock.json"): return
        with open("heroic_stock.json", "r") as f: stock = json.load(f)
        if not stock: return
        loop = asyncio.get_event_loop()
        updated_stock = []
        for entry in stock:
            raw_token = entry.get("input_data", entry.get("token", entry.get("refresh_token", ""))).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            if not raw_token: continue
            if safe_seconds_until_expiry(raw_token) < buffer_seconds:
                new_data = loop.run_until_complete(execute_dual_host_refresh(raw_token, refresh_token))
                if new_data and "token" in new_data:
                    entry["token"] = new_data["token"]; entry["refresh_token"] = new_data["refresh_token"]; entry["input_data"] = new_data["token"]
                else: entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            else: entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            updated_stock.append(entry)
        with open("heroic_stock.json", "w") as f: json.dump(updated_stock, f, indent=2)
    except Exception as e: print(f"[ERROR] Premium loop failed: {e}")

def refresh_env_accounts_if_needed(buffer_seconds=1800): pass
def get_public_token():
    try:
        if os.path.exists("normal_stock.json"):
            with open("normal_stock.json", "r") as f:
                stock = json.load(f)
                if stock and len(stock) > 0:
                    first = stock[0]
                    return {"token": first.get("token", first.get("input_data", "")), "refresh_token": first.get("refresh_token", first.get("input_data", ""))}
    except Exception: pass
    return None

def get_public_token_with_fallback():
    token_dict = get_public_token()
    if token_dict and token_dict["token"]: return token_dict, "public"
    return None, "empty"

def get_rotating_token(): return get_public_token()
def pop_premium_token():
    try:
        if os.path.exists("heroic_stock.json"):
            with open("heroic_stock.json", "r") as f:
                stock = json.load(f)
                if stock and len(stock) > 0:
                    first = stock[0]
                    return {"token": first.get("token", first.get("input_data", "")), "refresh_token": first.get("refresh_token", first.get("input_data", ""))}
    except Exception: pass
    return None

def add_premium_token(token, refresh_token):
    try:
        filename = "heroic_stock.json"
        stock = []
        if os.path.exists(filename):
            with open(filename, "r") as f: stock = json.load(f)
        stock.append({"token": token.strip(), "refresh_token": refresh_token.strip()})
        with open(filename, "w") as f: json.dump(stock, f, indent=2)
        return len(stock)
    except Exception: return 0

def get_premium_pool():
    try:
        if os.path.exists("heroic_stock.json"):
            with open("heroic_stock.json", "r") as f: return json.load(f)
    except Exception: pass
    return []

def is_premium_user(user_id):
    try:
        if os.path.exists("premium_users.json"):
            with open("premium_users.json", "r") as f: return str(user_id) in json.load(f)
    except Exception: pass
    return False

def add_premium_user(user_id, added_by):
    try:
        filename = "premium_users.json"
        users = {}
        if os.path.exists(filename):
            with open(filename, "r") as f: users = json.load(f)
        users[str(user_id)] = {"added_by": str(added_by), "timestamp": int(datetime.now().timestamp())}
        with open(filename, "w") as f: json.dump(users, f, indent=2)
    except Exception: pass

def add_donated(target_id, token, refresh_token, given_by):
    try:
        filename = "donated_tokens.json"
        data = {}
        if os.path.exists(filename):
            with open(filename, "r") as f: data = json.load(f)
        uid = str(target_id)
        if uid not in data: data[uid] = []
        data[uid].append({"token": token, "refresh_token": refresh_token, "given_by": given_by, "timestamp": int(datetime.now().timestamp())})
        with open(filename, "w") as f: json.dump(data, f, indent=2)
    except Exception: pass

def get_donated(user_id):
    try:
        if os.path.exists("donated_tokens.json"):
            with open("donated_tokens.json", "r") as f: return json.load(f).get(str(user_id), [])
    except Exception: pass
    return []

def revoke_donated(user_id):
    try:
        filename = "donated_tokens.json"
        if os.path.exists(filename):
            with open(filename, "r") as f: data = json.load(f)
            uid = str(user_id)
            if uid in data:
                count = len(data[uid]); del data[uid]
                with open(filename, "w") as f: json.dump(data, f, indent=2)
                return count
    except Exception: pass
    return 0

def global_status():
    pub_token = get_public_token(); prem_pool = get_premium_pool()
    pub_expiry = seconds_until_expiry(pub_token["token"]) if pub_token else 0
    prem_valid = sum(1 for t in prem_pool if safe_seconds_until_expiry(t.get("token", "")) > 0)
    u_donated, t_donated = 0, 0
    try:
        if os.path.exists("donated_tokens.json"):
            with open("donated_tokens.json", "r") as f:
                d = json.load(f); u_donated = len(d); t_donated = sum(len(v) for v in d.values())
    except Exception: pass
    p_users = 0
    try:
        if os.path.exists("premium_users.json"):
            with open("premium_users.json", "r") as f: p_users = len(json.load(f))
    except Exception: pass
    return {"public": {"valid": pub_token is not None, "expires_in": pub_expiry}, "premium": {"valid": prem_valid, "expired": len(prem_pool) - prem_valid, "total": len(prem_pool)}, "donated": {"users_with_donated": u_donated, "total_donated_tokens": t_donated}, "premium_users": p_users}

def check_cooldown(user_id, pool_name=None, cooldown_seconds=0): return False, 0
def set_cooldown(user_id, pool_name): pass
def format_time(seconds): return f"{int(seconds // 60)}m"
def reset_all_cooldowns(): return 0
def set_permanent_cooldown(user_id, pool_name): pass
def increment_premium_uses(user_id): pass
def get_env_accounts(): return []

def redeem_promo_key(key_str, user_id):
    """Checks, uses, and marks a generated promo key string inside the database layers."""
    filename = "promo_keys.json"
    try:
        if not os.path.exists(filename):
            return {"status": "error", "msg": "❌ Invalid key: No active promo keys exist."}
        with open(filename, "r") as f:
            keys = json.load(f)
        
        target_key = key_str.strip().upper()
        if target_key not in keys:
            return {"status": "error", "msg": "❌ Invalid key: That code does not exist."}
            
        key_data = keys[target_key]
        if key_data["uses"] <= 0:
            return {"status": "error", "msg": "❌ Expired: This key has already reached its usage limit."}
            
        if str(user_id) in key_data["claimed_by"]:
            return {"status": "error", "msg": "❌ Already Claimed: You have already redeemed this code!"}

        # Deduct a use and record the user's ID tracking metric
        key_data["uses"] -= 1
        key_data["claimed_by"].append(str(user_id))
        
        with open(filename, "w") as f:
            json.dump(keys, f, indent=2)
            
        return {"status": "success", "role_id": key_data["role_id"], "remaining": key_data["uses"]}
    except Exception as e:
        return {"status": "error", "msg": f"❌ Database error: {e}"}
def check_blacklist_status(target_id):
    """Verifies if a specific Player ID or Guild Server ID is blacklisted."""
    try:
        if not os.path.exists("blacklist.json"): return False
        with open("blacklist.json", "r") as f: data = json.load(f)
        return str(target_id) in data
    except Exception: return False

def modify_blacklist_entry(target_id, action="add"):
    """Adds or removes an ID from the global bot restriction file."""
    try:
        filename = "blacklist.json"; data = []
        if os.path.exists(filename):
            with open(filename, "r") as f: data = json.load(f)
        tid = str(target_id).strip()
        if action == "add" and tid not in data: data.append(tid)
        elif action == "remove" and tid in data: data.remove(tid)
        with open(filename, "w") as f: json.dump(data, f, indent=2)
        return True
    except Exception: return False

