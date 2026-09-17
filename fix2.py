with open("bot.py", "a", encoding="utf-8") as f:
    f.write('''
def append_universal_token(token_data_str):
    filename = "normal_stock.json"; stock = []
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f: stock = json.load(f)
        except Exception: pass
    stock.append({"token": token_data_str.strip(), "refresh_token": token_data_str.strip(), "_source_type": "user_donated"})
    with open(filename, "w") as f: json.dump(stock, f, indent=2)

class SingleTokenModal(discord.ui.Modal, title="Donate a Token"):
    token_input = discord.ui.TextInput(label="Paste Token String", required=True)
    def __init__(self, client): super().__init__(); self.client = client
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True); append_universal_token(self.token_input.value.strip())
        await interaction.followup.send("🎉 Donation Saved!", ephemeral=True)
        await send_to_log_channel(self.client, "🎁 Token Received", f"Donor: {interaction.user.mention}", color=discord.Color.green())

class MestroDonationDashboardView(discord.ui.View):
    def __init__(self, client): super().__init__(timeout=None); self.client = client
    @discord.ui.button(label="Donate a Token", style=discord.ButtonStyle.success, custom_id="donate_single_universal")
    async def donate_single_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleTokenModal(self.client))

class DashboardView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Get Token", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="dashboard_get_token")
    async def get_token_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        from refresh_token import pop_and_rotate_public_token
        
        # FIX: Pinned server buttons now also call unique popping engine distribution loops!
        tokens = await pop_and_rotate_public_token()
        if not tokens:
            await interaction.followup.send("❌ No valid token available inside databases.", ephemeral=True); return
        clean_tok = tokens['token']
        file_payload = {"bearer": clean_tok, "refresh_token": tokens['refresh_token']}
        discord_file = discord.File(fp=io.BytesIO(json.dumps(file_payload, indent=2).encode("utf-8")), filename="token.json")
        await interaction.followup.send(f"✅ **Token Sent:**\\n```text\\n{clean_tok}\\n```", file=discord_file, ephemeral=True)

def setup_donation_dashboard(tree):
    @tree.client.event
    async def on_ready():
        client.add_view(MestroDonationDashboardView(client))
        client.add_view(DashboardView())
        print(f"[BOT] Connected as {tree.client.user}")
        for guild in list(tree.client.guilds):
            from storage import check_blacklist_status
            if check_blacklist_status(guild.id): await guild.leave()
        try:
            guild_obj = discord.Object(id=1548129826676809798)
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"[SYNC] Success! Server registry force-synced {len(synced)} commands instantly.")
        except Exception as e: print(f"[SYNC_ERROR] Direct sync failed: {e}")
        log_channel = tree.client.get_channel(1549154833372549200)
        if log_channel: await log_channel.send(embed=discord.Embed(title="🚀 Bot Online / High-Speed 10s Loops Active", color=discord.Color.gold()))

    @tree.command(name="donate_dashboard", description="Launch donation dashboard")
    async def donate_dashboard(interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="🎁 Donation Center"), view=MestroDonationDashboardView(tree.client))

    @tree.command(name="dashboard", description="Post button dashboard")
    async def dashboard_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="🎫 Token Station"), view=DashboardView())

    @tree.command(name="add", description="Force-add a token")
    async def add_token_command(interaction: discord.Interaction, refresh_token: str):
        await interaction.response.defer(ephemeral=True)
        if str(interaction.user.id) not in ADMIN_USER_IDS: await interaction.followup.send("❌ Denied", ephemeral=True); return
        filename = "normal_stock.json"; stock = []
        if os.path.exists(filename):
            with open(filename, "r") as f: stock = json.load(f)
        stock.append({"token": refresh_token.strip(), "refresh_token": refresh_token.strip(), "_source_type": "admin"})
        with open(filename, "w") as f: json.dump(stock, f, indent=2)
        await interaction.followup.send("🚀 Token Added", ephemeral=True)

    @tree.command(name="remove_token", description="[ADMIN] Delete a specific token number from inventory")
    @app_commands.describe(number="The token index number to remove from /global_live_stock")
    async def remove_token_command(interaction: discord.Interaction, number: int):
        await interaction.response.defer(ephemeral=True)
        admin_env = os.getenv("ADMIN_USER_IDS", "")
        admin_list = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip()]
        if interaction.user.id not in admin_list: await interaction.followup.send("❌ Access Denied.", ephemeral=True); return
        filename = "normal_stock.json"
        if not os.path.exists(filename): await interaction.followup.send("❌ Stock file is empty.", ephemeral=True); return
        try:
            with open(filename, "r") as f: stock = json.load(f)
            if number < 1 or number > len(stock):
                await interaction.followup.send(f"❌ Invalid index number. Current pool total is: {len(stock)}", ephemeral=True); return
            removed_entry = stock.pop(number - 1)
            with open(filename, "w") as f: json.dump(stock, f, indent=2)
            await interaction.followup.send(f"🗑️ **Token Removed:** Purged Token number `{number}`.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @tree.command(name="remove_all_tokens", description="[ADMIN] Complete database wipeout — clear all stock files")
    async def remove_all_tokens_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        admin_env = os.getenv("ADMIN_USER_IDS", "")
        admin_list = [int(uid.strip()) for uid in admin_env.split(",") if uid.strip()]
        if interaction.user.id not in admin_list: await interaction.followup.send("❌ Access Denied.", ephemeral=True); return
        filename = "normal_stock.json"
        try:
            with open(filename, "w") as f: json.dump([], f, indent=2)
            await interaction.followup.send("💥 **Database Wiped:** All active stock tokens have been permanently cleared out.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @tree.command(name="block", description="Hard-blacklist a server ID")
    async def block_command(interaction: discord.Interaction, target_id: str):
        if interaction.user.id != 1447021186835025921: await interaction.response.send_message("❌ Denied", ephemeral=True); return
        await interaction.response.defer(ephemeral=True); from storage import modify_blacklist_entry
        if modify_blacklist_entry(target_id, "add"):
            await interaction.followup.send("🛡️ Blacklisted", ephemeral=True)
            try:
                g = tree.client.get_guild(int(target_id.strip()))
                if g: await g.leave()
            except Exception: pass

    @tasks.loop(seconds=10)
    async def token_refresh_loop():
        try:
            from refresh_token import run_stock_auto_refresh
            run_stock_auto_refresh()
        except Exception as e: print(f"Loop Error: {e}")

    @token_refresh_loop.before_loop
    async def before_token_refresh():
        await tree.client.wait_until_ready(); await token_refresh_loop.start()

if __name__ == "__main__":
    setup_donation_dashboard(tree)
    client.run(BOT_TOKEN)
''')
print("Part 2 appended successfully with Unique Pop Logic!")
