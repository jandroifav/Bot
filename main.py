import os
import sys
import json
import logging
import re
import threading
import traceback
import requests
import gspread
import discord
from discord.ext import commands
from flask import Flask

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RegimentBot")

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_SHEET_ID = os.environ.get("SPREADSHEET_ID", "1F1V-fgge7UhaQmqgZsEtf6mExGNJU_JFSHfHr7fJ2lQ")
REGIMENT_CONFIGS = {}

def get_gspread_client():
    creds_raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_raw:
        raise ValueError("Missing GOOGLE_APPLICATION_CREDENTIALS environment variable.")
    
    if creds_raw.strip().startswith("{"):
        creds_dict = json.loads(creds_raw)
        return gspread.service_account_from_dict(creds_dict)
    else:
        return gspread.service_account(filename=creds_raw)

def safe_sheet_action(func, *args, **kwargs):
    """Wrapper to handle automatic retry or client re-auth on sheet calls."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"Sheet action error: {e}", flush=True)
        raise e

def extract_spreadsheet_id(url_or_id: str) -> str:
    """Extracts raw spreadsheet ID from a full Google Sheets URL or raw ID string."""
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id.strip()

def load_configs():
    """Reads regiment channel configurations from Spreadsheet Info Storage."""
    global REGIMENT_CONFIGS
    try:
        gc = get_gspread_client()
        ss = gc.open_by_key(CONFIG_SHEET_ID)
        config_ws = ss.worksheet("Spreadsheet Info Storage")
        
        rows = config_ws.get_all_values()
        if len(rows) < 2:
            print("No configuration rows found in Spreadsheet Info Storage.", flush=True)
            return

        new_configs = {}
        for row in rows[1:]:
            if len(row) >= 5 and row[4].strip().isdigit():
                channel_id = int(row[4].strip())
                raw_sheet_val = row[1].strip()
                
                new_configs[channel_id] = {
                    "regiment_name": row[0].strip(),
                    "spreadsheet_id": extract_spreadsheet_id(raw_sheet_val),
                    "script_url": row[2].strip(),
                    "staff_role": row[3].strip(),
                    "error_channel_id": None
                }
        
        REGIMENT_CONFIGS = new_configs
        print(f"Successfully loaded {len(REGIMENT_CONFIGS)} regiment channel configurations.", flush=True)
    except Exception as e:
        print(f"Failed to load spreadsheet configurations: {e}", flush=True)
        traceback.print_exc()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})", flush=True)
    load_configs()

@bot.command(name="reload")
async def reload_config_command(ctx):
    """Allows admins to hot-reload spreadsheet settings without restarting."""
    load_configs()
    await ctx.send(f"✅ Successfully reloaded configurations! Loaded {len(REGIMENT_CONFIGS)} regiment channel configs.")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.channel.id not in REGIMENT_CONFIGS:
        return

    raw_text = message.content.strip()
    if not raw_text.lower().startswith("event type:"):
        return

    cfg = REGIMENT_CONFIGS[message.channel.id]
    await message.add_reaction("⏳")
    print(f"--> Processing audit for channel {message.channel.id}...", flush=True)

    try:
        gc = get_gspread_client()
        reg_ss = gc.open_by_key(cfg["spreadsheet_id"])
        input_ws = reg_ss.worksheet("Input")

        safe_sheet_action(input_ws.batch_clear, ["P6:P37"])

        safe_sheet_action(input_ws.update_acell, "C3", raw_text)

        if cfg.get("script_url"):
            print(f"Calling Apps Script: {cfg['script_url']}", flush=True)
            response = requests.post(cfg["script_url"], json={"action": "run"}, timeout=45)
            
            if response.status_code != 200:
                print(f"Warning: Apps Script endpoint returned {response.status_code}. Continuing sheet read...", flush=True)

        missing_vals = safe_sheet_action(input_ws.get, "P6:P37")
        missing_players = []
        if missing_vals:
            for row in missing_vals:
                if row and len(row) > 0 and str(row[0]).strip():
                    missing_players.append(str(row[0]).strip())

        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("✅")

        if missing_players:
            target_channel_id = 1506368484529934476
            target_chan = bot.get_channel(target_channel_id)
            if not target_chan:
                try:
                    target_chan = await bot.fetch_channel(target_channel_id)
                except Exception as fetch_err:
                    print(f"Could not fetch target missing players channel: {fetch_err}", flush=True)

            if target_chan:
                staff_role_val = cfg.get("staff_role", "").strip()
                if staff_role_val.isdigit():
                    staff_role_ping = f"<@&{staff_role_val}>"
                elif staff_role_ping_str := re.search(r"\d+", staff_role_val):
                    staff_role_ping = f"<@&{staff_role_ping_str.group(0)}>"
                else:
                    staff_role_ping = staff_role_val

                embed = discord.Embed(
                    title="📋 Players missing in the spreadsheet:",
                    description="\n".join(missing_players),
                    color=discord.Color.from_rgb(238, 44, 44)
                )

                await target_chan.send(content=staff_role_ping if staff_role_ping else None, embed=embed)

    except Exception as e:
        print(f"!!! ERROR processing audit in channel {message.channel.id}: {e}", flush=True)
        traceback.print_exc()
        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("❌")
        
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("FATAL: DISCORD_TOKEN environment variable not set.", flush=True)
        sys.exit(1)
        
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(token)
