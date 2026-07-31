import os
import sys
import asyncio
import re
import logging
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

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

sheet = None

@bot.event
async def on_ready():
    global sheet
    print(f"Bot logged in as {bot.user}")
    
    client = get_sheets_client()
    if client:
        sheet = safe_sheet_action(client.open_by_key, CONFIG_SPREADSHEET_ID)
        if sheet:
            print("Successfully connected to Google Sheets!")
        else:
            print("Could not load spreadsheet. Bot will remain online and retry when commands run.")
    else:
        print("Could not authorize Google client. Bot remaining online.")

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
