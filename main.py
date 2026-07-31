import os
import sys
import asyncio
import re
import logging
import time
from threading import Thread
from flask import Flask

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()

keep_alive()

import discord
from discord.ext import commands
import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
import requests
from dotenv import load_dotenv

logging.getLogger("google").setLevel(logging.ERROR)
logging.getLogger("google.auth").setLevel(logging.ERROR)
logging.getLogger("gspread").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")

CONFIG_SPREADSHEET_ID = "1F1V-fgge7UhaQmqgZsEtf6mExGNJU_JFSHfHr7fJ2lQ"
CENTRAL_CHANNEL_ID = 1506368484529934476

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

REGIMENT_CONFIGS = {}
sheets_client = None

def safe_sheet_action(action_func, *args, **kwargs):
    try:
        return action_func(*args, **kwargs)
    except APIError as e:
        status = getattr(e.response, "status_code", "Unknown")
        print(f"[Warning] Google Sheets call blocked/failed (HTTP {status}).")
        return None
    except Exception as e:
        print(f"[Warning] Sheet operation failed: {type(e).__name__}")
        return None

def get_sheets_client():
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"[Warning] Failed to initialize Google Credentials: {type(e).__name__}")
        return None

def load_configs():
    global REGIMENT_CONFIGS, sheets_client
    if not sheets_client:
        sheets_client = get_sheets_client()
    
    if not sheets_client:
        print("[Error] Couldn't initialize Google Sheets client for loading configs.")
        return False

    config_spreadsheet = safe_sheet_action(sheets_client.open_by_key, CONFIG_SPREADSHEET_ID)
    if not config_spreadsheet:
        print("[Error] Failed to open master configuration spreadsheet.")
        return False

    try:
        ws = config_spreadsheet.worksheet("Spreadsheet Info Storage")
        rows = ws.get_all_values()[1:]
        
        new_configs = {}
        for row in rows:
            if len(row) >= 5:
                sheet_url = row[1].strip()
                script_url = row[2].strip()
                role_id = row[3].strip()
                channel_id_str = row[4].strip()

                if channel_id_str.isdigit():
                    ch_id = int(channel_id_str)
                    new_configs[ch_id] = {
                        "sheet_url": sheet_url,
                        "script_url": script_url,
                        "role_id": role_id
                    }

        REGIMENT_CONFIGS = new_configs
        print(f"[Config Loaded] Configured {len(REGIMENT_CONFIGS)} regiment channel(s).")
        return True
    except Exception as e:
        print(f"[Error] Failed parsing 'Spreadsheet Info Storage': {e}")
        return False

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    load_configs()

@bot.command(name="reload")
async def reload_cmd(ctx):
    success = load_configs()
    if success:
        await ctx.send(f"✅ Successfully reloaded configurations! Loaded **{len(REGIMENT_CONFIGS)}** regiment channel configs.")
    else:
        await ctx.send("❌ Failed to reload configurations. Check bot console logs.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    if message.channel.id in REGIMENT_CONFIGS:
        cfg = REGIMENT_CONFIGS[message.channel.id]
        
        try:
            await message.add_reaction("⏳")
        except Exception as e:
            print(f"[Warning] Could not add hourglass reaction: {e}")

        loop = asyncio.get_running_loop()
        missing_players, error_msg = await loop.run_in_executor(None, process_audit, cfg, message.content)

        try:
            await message.remove_reaction("⏳", bot.user)
            if error_msg:
                await message.add_reaction("❌")
                print(f"[Audit Error] {error_msg}")
            else:
                await message.add_reaction("✅")
        except Exception as e:
            print(f"[Warning] Could not update reaction: {e}")

        if missing_players and not error_msg:
            central_channel = bot.get_channel(CENTRAL_CHANNEL_ID)
            if not central_channel:
                try:
                    central_channel = await bot.fetch_channel(CENTRAL_CHANNEL_ID)
                except Exception as e:
                    print(f"[Error] Could not fetch central channel {CENTRAL_CHANNEL_ID}: {e}")

            if central_channel:
                role_ping = f"<@&{cfg['role_id']}>" if cfg['role_id'] else ""
                
                embed_description = f"📋 [**Players missing in the spreadsheet:**]({cfg['sheet_url']})\n\n" + "\n".join(missing_players)

                embed = discord.Embed(
                    description=embed_description,
                    color=discord.Color.from_rgb(231, 76, 60)
                )

                await central_channel.send(content=role_ping if role_ping else None, embed=embed)

def process_audit(cfg, raw_audit_text):
    global sheets_client
    if not sheets_client:
        sheets_client = get_sheets_client()

    if not sheets_client:
        return None, "Google Sheets client not initialized."

    try:
        reg_sheet = safe_sheet_action(sheets_client.open_by_url, cfg["sheet_url"])
        if not reg_sheet:
            return None, "Failed to open regimental spreadsheet."

        input_ws = safe_sheet_action(reg_sheet.worksheet, "Input")
        if not input_ws:
            return None, "Worksheet 'Input' not found."

        safe_sheet_action(input_ws.batch_clear, ["H9:H40"])
        safe_sheet_action(input_ws.update_acell, "C3", raw_audit_text)

        if cfg["script_url"]:
            try:
                resp = requests.post(cfg["script_url"], json={"action": "run"}, timeout=45)
                print(f"[AppsScript Response] Code: {resp.status_code}")
            except Exception as script_err:
                print(f"[Warning] Failed calling script Web App URL: {script_err}")
        else:
            time.sleep(10)

        h9_val = safe_sheet_action(input_ws.acell, "H9")
        missing_players = []

        if h9_val and h9_val.value and h9_val.value.strip():
            missing_vals = safe_sheet_action(input_ws.get, "H9:H40")
            if missing_vals:
                for row in missing_vals:
                    if row and len(row) > 0 and row[0].strip():
                        missing_players.append(row[0].strip())

        return missing_players, None

    except Exception as e:
        return None, f"Audit processing failed: {str(e)}"

async def start_bot():
    while True:
        try:
            if TOKEN:
                await bot.start(TOKEN)
            else:
                print("[Error] DISCORD_TOKEN is missing from environment variables.")
                break
        except Exception as e:
            print(f"[Network Error Caught] {type(e).__name__}: {e}")
            print("Re-attempting connection in 15 seconds...")
            await asyncio.sleep(15)

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
