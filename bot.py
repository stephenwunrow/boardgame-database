import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
load_dotenv()

from query_data import find_titles

TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message):
    # Ignore self
    if message.author == bot.user:
        return

    # Only respond to DMs
    if message.guild is not None:
        return

    content = message.content.strip()
    if content.startswith("!"):
        search_phrase = content.strip('! ')
        found_titles = find_titles(search_phrase)
        if not found_titles:
            await message.channel.send(f"No boardgame found for phrase {search_phrase}.")
            return
        
        response = "\n".join(found_titles[:20])
        await message.channel.send(response)
        return


bot.run(TOKEN)
