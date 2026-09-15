import os
import json
import aiohttp
import asyncio
import discord
from datetime import datetime, timezone

def safe_seconds_until_expiry(token_str):
    """Calculates remaining token lifetime seconds using fallback metrics."""
    try:
        import jwt
        decoded = jwt.decode(token_str, options={"verify_signature": False})
        return max(int(decoded.get("exp", 0) - datetime.now(timezone.utc).timestamp()), 0)
    except Exception:
        return 3526

async def execute_nakama_refresh(token, refresh_token):
    """Sends a high-speed POST payload to the Nakama server to rotate credentials."""
    primary_host = os.getenv("NAKAMA_HOST", "https://animalcompany.us-east1.nakamacloud.io")
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
    return None

def run_stock_auto_refresh(buffer_seconds=1800):
    """Processes normal stock files, automatically renewing tokens close to expiration."""
    filename = "normal_stock.json"
    try:
        if not os.path.exists(filename): return False
        with open(filename, "r") as f: stock = json.load(f)
        if not stock: return False

        loop = asyncio.get_event_loop()
        updated_stock = []
        has_changed = False

        for entry in stock:
            raw_token = entry.get("input_data", entry.get("token", entry.get("refresh_token", ""))).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            if not raw_token: continue

            if safe_seconds_until_expiry(raw_token) < buffer_seconds:
                new_data = loop.run_until_complete(execute_nakama_refresh(raw_token, refresh_token))
                if new_data and "token" in new_data:
                    entry["token"] = new_data["token"]
                    entry["refresh_token"] = new_data["refresh_token"]
                    entry["input_data"] = new_data["token"]
                    has_changed = True
                else:
                    entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            else:
                entry["token"] = raw_token; entry["refresh_token"] = refresh_token
            updated_stock.append(entry)

        with open(filename, "w") as f: json.dump(updated_stock, f, indent=2)
        return has_changed
    except Exception as e:
        print(f"[ERROR] Auto-rotation cycle error: {e}")
        return False

async def pop_and_rotate_public_token():
    """Removes the top token, refreshes it instantly, and ensures users get unique tokens."""
    filename = "normal_stock.json"
    if not os.path.exists(filename): return None

    try:
        with open(filename, "r") as f: stock = json.load(f)
        if not stock: return None

        # Extract the first token from the array array layers so nobody else can claim it
        target_entry = stock.pop(0)

        with open(filename, "w") as f: json.dump(stock, f, indent=2)

        raw_token = target_entry.get("token", target_entry.get("input_data", "")).strip()
        refresh_token = target_entry.get("refresh_token", raw_token).strip()

        # Instantly rotate the token via POST payload before giving it to the user
        new_data = await execute_nakama_refresh(raw_token, refresh_token)
        if new_data and "token" in new_data:
            return {"token": new_data["token"], "refresh_token": new_data["refresh_token"]}
        
        return {"token": raw_token, "refresh_token": refresh_token}
    except Exception as e:
        print(f"[ROTATION_ERROR] Pop sequence failed: {e}")
        return None
