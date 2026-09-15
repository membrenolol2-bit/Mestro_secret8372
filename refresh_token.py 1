import os
import json
import aiohttp
from datetime import datetime, timezone

async def execute_nakama_refresh(token, refresh_token):
    """Sends a high-speed POST payload to the Nakama server endpoint to rotate credentials."""
    primary_host = os.getenv("NAKAMA_HOST", "https://nakamacloud.io")
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
async def pop_and_rotate_public_token():
    """Removes the top token, refreshes it instantly, and ensures users get unique tokens."""
    filename = "normal_stock.json"
    if not os.path.exists(filename):
        return None

    try:
        with open(filename, "r") as f:
            stock = json.load(f)
        if not stock:
            return None

        # Extract the first token from the array array layers
        target_entry = stock.pop(0)

        # Save the updated stock back immediately so the next user gets a different entry
        with open(filename, "w") as f:
            json.dump(stock, f, indent=2)

        raw_token = target_entry.get("token", target_entry.get("input_data", "")).strip()
        refresh_token = target_entry.get("refresh_token", raw_token).strip()

        # Execute instant Nakama API rotation
        new_data = await execute_nakama_refresh(raw_token, refresh_token)
        if new_data and "token" in new_data:
            return {
                "token": new_data["token"],
                "refresh_token": new_data["refresh_token"]
            }
        
        # Fallback tracking parameters if server response lags
        return {"token": raw_token, "refresh_token": refresh_token}
    except Exception as e:
        print(f"[ROTATION_ERROR] Pop sequence failed: {e}")
        return None
