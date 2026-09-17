import os
import json
import asyncio
import aiohttp
import base64
from datetime import datetime, timezone

def safe_seconds_until_expiry(token_str):
    """FORCED 1-MINUTE ENGINE: Enforces your strict 60-second limit."""
    return 60

async def execute_nakama_refresh(token, refresh_token):
    """Sends a high-speed POST payload to the Nakama server with Basic Auth headers."""
    primary_host = os.getenv("NAKAMA_HOST", "https://nakamacloud.io")
    url = f"{primary_host.rstrip('/')}/v2/account/session/refresh"
    
    # 🔐 SECURE AUTHENTICATION LAYER: Bundles your Nakama Server Key into HTTP Basic Auth
    server_key = "6URuTSlDKKfYbuDW:"
    encoded_key = base64.b64encode(server_key.encode("utf-8")).decode("utf-8")
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {encoded_key}"
    }
    
    payload = {
        "token": token,
        "refresh_token": refresh_token if refresh_token else token
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=8) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"[API_ERROR] Nakama rejected request with HTTP status: {response.status}")
    except Exception as e:
        print(f"[NETWORK_ERROR] Failed connecting to Nakama gateway: {e}")
    return None

def run_stock_auto_refresh():
    """Loops every 10 seconds, forcing Nakama to return completely new working token strings."""
    filename = "normal_stock.json"
    try:
        if not os.path.exists(filename): return False
        with open(filename, "r") as f: stock = json.load(f)
        if not stock: return False

        loop = asyncio.get_event_loop()
        updated_stock = []
        has_changed = False

        for entry in stock:
            raw_token = entry.get("token", entry.get("input_data", "")).strip()
            refresh_token = entry.get("refresh_token", raw_token).strip()
            if not raw_token: continue

            # Executes the authenticated rotation request
            new_data = loop.run_until_complete(execute_nakama_refresh(raw_token, refresh_token))
            
            if new_data and "token" in new_data:
                entry["token"] = new_data["token"]
                entry["refresh_token"] = new_data["refresh_token"]
                entry["input_data"] = new_data["token"]
                entry["_source_type"] = "auto_high_speed_rotation"
                has_changed = True
                updated_stock.append(entry)
            else:
                # AUTOMATIC PURGE SWEEP: Drops the token entirely if it becomes un-refreshable
                has_changed = True
                continue

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

        target_entry = stock.pop(0)
        with open(filename, "w") as f: json.dump(stock, f, indent=2)

        raw_token = target_entry.get("token", target_entry.get("input_data", "")).strip()
        refresh_token = target_entry.get("refresh_token", raw_token).strip()

        new_data = await execute_nakama_refresh(raw_token, refresh_token)
        if new_data and "token" in new_data:
            return {"token": new_data["token"], "refresh_token": new_data["refresh_token"]}
        
        return {"token": raw_token, "refresh_token": refresh_token}
    except Exception as e:
        print(f"[ROTATION_ERROR] Pop sequence failed: {e}")
        return None
