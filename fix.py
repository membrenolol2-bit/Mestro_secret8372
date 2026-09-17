import os

with open("bot.py", "w", encoding="utf-8") as f:
    f.write('''import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button
from discord.ext import tasks
import asyncio, os, json, sys, traceback, io
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

load_dotenv()
from storage import *

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_USER_TOKEN")
if not BOT_TOKEN: raise ValueError("Missing Discord token")

ALLOWED_GUILD_IDS = [int(g.strip()) for g in os.getenv("ALLOWED_GUILD_IDS", "").split(",") if g.strip().isdigit()]
ADMIN_USER_IDS = {s.strip() for s in os.getenv("ADMIN_USER_IDS", "").split(",") if s.strip().isdigit()}

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="token", description="Get a unique session token")
async def token_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from refresh_token import pop_and_rotate_public_token
    
    # FIX: Explicitly pops the token from stock so nobody else ever gets it again!
    rotated = await pop_and_rotate_public_token()
    if not rotated:
        await interaction.followup.send("❌ **POOL EMPTY:** No tokens available inside stock files! Support rotations by clicking Donate!", ephemeral=True); return
    
    clean_tok = rotated['token']
    clean_ref = rotated['refresh_token']
    
    file_payload = {"bearer": clean_tok, "refresh_token": clean_ref}
    json_bytes = json.dumps(file_payload, indent=2).encode("utf-8")
    discord_file = discord.File(fp=io.BytesIO(json_bytes), filename="token.json")
    
    await interaction.followup.send(f"🎉 **Unique Token Generated Successfully!**\\n\\n```text\\n{clean_tok}\\n```", file=discord_file, ephemeral=True)
    
    diff_text = "```diff\\nFixed:\\n+ tokens\\nAdded:\\n- None\\nRemoved:\\n- 1 Token (Claimed)\\n```"
    await send_to_log_channel(client, "⚡ Token Claimed", f"User: {interaction.user.mention}\\n{diff_text}", color=discord.Color.green())

async def send_to_log_channel(client, title, description, color=discord.Color.blue()):
    try:
        ch = client.get_channel(1549154833372549200)
        if ch:
            embed = discord.Embed(title=title, description=description, color=color)
            if os.path.exists("normal_stock.json"):
                await ch.send(embed=embed, file=discord.File("normal_stock.json"))
            else: await ch.send(embed=embed)
    except Exception as e: print(f"Log Error: {e}")
''')
print("Part 1 written successfully with Pop and Rotate Engine!")
