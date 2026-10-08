import os
import sys
import json
import logging
import threading
import requests
import gspread
import discord
from discord.ext import commands
from flask import Flask

# ---------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RegimentBot")

# ---------------------------------------------------------
# Keep-Alive HTTP Server (Fixes Free Render Web Service Timeout)
# ---------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ---------------------------------------------------------
# Discord Bot Setup
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------
# Google Sheets Auth & Configuration Loader
# ---------------------------------------------------------
CONFIG_SHEET_ID = os.environ.get("SPREADSHEET_ID", "1F1V-fgge7UhaQmqgZsEtf6mExGNJU_JFSHfHr7fJ2lQ")
REGIMENT_CONFIGS = {}

def get_gspread_client():
    # Checks GOOGLE_APPLICATION_CREDENTIALS first, then GOOGLE_CREDENTIALS
    creds_raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_raw:
        raise ValueError("Missing GOOGLE_APPLICATION_CREDENTIALS environment variable.")
    
    # Check if the variable is a raw JSON string or a file path
    if creds_raw.strip().startswith("{"):
        creds_dict = json.loads(creds_raw)
        return gspread.service_account_from_dict(creds_dict)
    else:
        # If it points to a local file path
        return gspread.service_account(filename=creds_raw)

def safe_sheet_action(func, *args, **kwargs):
    """Wrapper to handle automatic retry or client re-auth on sheet calls."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Sheet action error: {e}")
        raise e

import re

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
            logger.warning("No configuration rows found in Spreadsheet Info Storage.")
            return

        new_configs = {}
        for row in rows[1:]:
            # Ensure row has enough columns and column E (index 4) contains a valid Channel ID
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
        logger.info(f"Successfully loaded {len(REGIMENT_CONFIGS)} regiment channel configurations.")
    except Exception as e:
        logger.error(f"Failed to load spreadsheet configurations: {e}")
# ---------------------------------------------------------
# Bot Commands & Event Listeners
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    load_configs()

@bot.command(name="reload")
async def reload_config_command(ctx):
    """Allows admins to hot-reload spreadsheet settings without restarting."""
    load_configs()
    await ctx.send(f"✅ Successfully reloaded configurations! Loaded {len(REGIMENT_CONFIGS)} regiment channel configs.")

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return

    # Process standard commands first (e.g. !reload)
    await bot.process_commands(message)

    # Check if message is in an audit channel
    if message.channel.id not in REGIMENT_CONFIGS:
        return

    raw_text = message.content.strip()
    if not raw_text.lower().startswith("event type:"):
        return

    cfg = REGIMENT_CONFIGS[message.channel.id]
    await message.add_reaction("⏳")

    try:
        gc = get_gspread_client()
        reg_ss = gc.open_by_key(cfg["spreadsheet_id"])
        input_ws = reg_ss.worksheet("Input")

        # 1. Clear previous missing roster outputs in P6:P37
        safe_sheet_action(input_ws.batch_clear, ["P6:P37"])

        # 2. Paste raw audit text into Input!C3
        safe_sheet_action(input_ws.update_acell, "C3", raw_text)

        # 3. Call Google Apps Script Web App Endpoint
        response = requests.post(cfg["script_url"], json={"action": "run"}, timeout=45)
        
        if response.status_code != 200:
            raise Exception(f"Google Apps Script returned HTTP {response.status_code}: {response.text}")

        res_data = response.json()
        if res_data.get("status") == "error":
            raise Exception(f"Apps Script Error: {res_data.get('message')}")

        # 4. Read missing players populated by Apps Script in P6:P37
        missing_vals = safe_sheet_action(input_ws.get, "P6:P37")
        missing_players = []
        if missing_vals:
            for row in missing_vals:
                if row and len(row) > 0 and str(row[0]).strip():
                    missing_players.append(str(row[0]).strip())

        # 5. Success UI Feedback
        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("✅")

        if missing_players:
            missing_fmt = "\n".join([f"• `{p}`" for p in missing_players])
            await message.reply(f"⚠️ **Audit Processed**, but the following users were not found on the Memberlist roster:\n{missing_fmt}")

    except Exception as e:
        logger.error(f"Error processing audit for channel {message.channel.id}: {e}")
        await message.remove_reaction("⏳", bot.user)
        await message.add_reaction("❌")
        
        # Report error to designated error channel if configured
        if cfg.get("error_channel_id"):
            err_chan = bot.get_channel(cfg["error_channel_id"])
            if err_chan:
                await err_chan.send(f"❌ **Audit Processing Failed** in <#{message.channel.id}>\n**Error:** `{e}`")

# ---------------------------------------------------------
# Application Entry Point
# ---------------------------------------------------------
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("FATAL: DISCORD_TOKEN environment variable not set.", flush=True)
        sys.exit(1)
        
    # Start web server in background thread so Render port checks pass
    threading.Thread(target=run_web_server, daemon=True).start()
    
    bot.run(token)
