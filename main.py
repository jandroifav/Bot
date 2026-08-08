import os
import sys
import asyncio
import re
import logging
import time
import socket
from threading import Thread
from flask import Flask
from werkzeug.serving import run_simple

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    run_simple('0.0.0.0', port, app, use_reloader=False, use_debugger=False, threaded=True)

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
ERROR_CHANNEL_ID = 1535622483019960372

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
        first_line = message.content.split('\n')[0].strip()
        if not first_line.lower().startswith("event type:"):
            return

        cfg = REGIMENT_CONFIGS[message.channel.id]
        
        try:
            await message.add_reaction("⏳")
        except Exception as e:
            print(f"[Warning] Could not add hourglass reaction: {e}")

        loop = asyncio.get_running_loop()
        missing_players, error_msg, error_type = await loop.run_in_executor(None, process_audit, cfg, message.content)

        try:
            await message.remove_reaction("⏳", bot.user)
            if error_msg:
                reaction_emoji = "⚠️" if error_type in ["INVALID_DATE", "SLOTS_FULL"] else "❌"
                await message.add_reaction(reaction_emoji)
                
                error_channel = bot.get_channel(ERROR_CHANNEL_ID)
                if not error_channel:
                    try:
                        error_channel = await bot.fetch_channel(ERROR_CHANNEL_ID)
                    except Exception as fetch_err:
                        print(f"[Error] Could not fetch error log channel: {fetch_err}")

                if error_channel:
                    err_embed = discord.Embed(
                        title="⚠️ Audit Processing Error",
                        color=discord.Color.from_rgb(241, 196, 15) if reaction_emoji == "⚠️" else discord.Color.from_rgb(231, 76, 60)
                    )
                    err_embed.add_field(name="Error Reason", value=f"`{error_type}`: {error_msg}", inline=False)
                    err_embed.add_field(name="Audit Message Link", value=f"[Jump to Message]({message.jump_url})", inline=False)
                    err_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                    err_embed.add_field(name="Posted By", value=message.author.mention, inline=True)
                    
                    await error_channel.send(embed=err_embed)
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
        return None, "Google Sheets client not initialized.", "INIT_ERROR"

    try:
        reg_sheet = safe_sheet_action(sheets_client.open_by_url, cfg["sheet_url"])
        if not reg_sheet:
            return None, "Failed to open regimental spreadsheet.", "SHEET_OPEN_ERROR"

        input_ws = safe_sheet_action(reg_sheet.worksheet, "Input")
        if not input_ws:
            return None, "Worksheet 'Input' not found.", "WORKSHEET_ERROR"

        lines = [line.strip() for line in raw_audit_text.split('\n') if line.strip()]
        if len(lines) < 2:
            return None, "Audit message missing date line or invalid format.", "FORMAT_ERROR"

        first_line = lines[0]
        second_line = lines[1]

        row5_vals = safe_sheet_action(input_ws.get, "K5:AC5")
        if not row5_vals or len(row5_vals) == 0:
            return None, "Failed reading row 5 dates from spreadsheet.", "SHEET_READ_ERROR"

        date_row = row5_vals[0]
        target_col_idx = -1

        for col_i in range(0, len(date_row), 3):
            cell_val = date_row[col_i].strip() if col_i < len(date_row) else ""
            if cell_val and cell_val in second_line:
                target_col_idx = 11 + col_i
                break

        if target_col_idx == -1:
            return None, f"Date line '{second_line}' does not match any date in row 5 (K5:AC5).", "INVALID_DATE"

        col_letter = gspread.utils.rowcol_to_a1(1, target_col_idx)[:-1]

        t6_val = safe_sheet_action(input_ws.acell, f"{col_letter}6")
        t6_p1 = safe_sheet_action(input_ws.acell, f"{col_letter}7")
        t77_val = safe_sheet_action(input_ws.acell, f"{col_letter}77")
        t77_p1 = safe_sheet_action(input_ws.acell, f"{col_letter}78")
        t138_val = safe_sheet_action(input_ws.acell, f"{col_letter}138")
        t138_p1 = safe_sheet_action(input_ws.acell, f"{col_letter}139")

        def is_tier_empty(dropdown_obj, player_obj):
            d_val = dropdown_obj.value.strip() if dropdown_obj and dropdown_obj.value else ""
            p_val = player_obj.value.strip() if player_obj and player_obj.value else ""
            return (d_val in ["", "N/A", "NA"]) and (p_val == "")

        t6_empty = is_tier_empty(t6_val, t6_p1)
        t77_empty = is_tier_empty(t77_val, t77_p1)
        t138_empty = is_tier_empty(t138_val, t138_p1)

        start_tier = 6
        if "Ceremony" in first_line:
            start_tier = 138
        elif "AS" in first_line:
            toolbox_ws = safe_sheet_action(reg_sheet.worksheet, "Toolbox")
            m_tz = ""
            if toolbox_ws:
                m_tz_cell = safe_sheet_action(toolbox_ws.acell, "O61")
                if m_tz_cell and m_tz_cell.value:
                    m_tz = m_tz_cell.value.strip()
            start_tier = 6 if m_tz == "AS/OC" else 138
        elif "NA" in first_line or "N/A" in first_line:
            start_tier = 77
        elif "EU" in first_line or "Main" in first_line:
            start_tier = 6

        tiers_to_check = [t for t in [6, 77, 138] if t >= start_tier]
        slot_found = False

        for t in tiers_to_check:
            if t == 6 and t6_empty:
                slot_found = True
                break
            elif t == 77 and t77_empty:
                slot_found = True
                break
            elif t == 138 and t138_empty:
                slot_found = True
                break

        if not slot_found:
            return None, f"All available event tiers (rows 6, 77, 138) for date '{second_line}' are already filled.", "SLOTS_FULL"

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

        return missing_players, None, None

    except Exception as e:
        return None, f"Audit processing failed: {str(e)}", "SYSTEM_ERROR"

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
