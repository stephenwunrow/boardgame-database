import os
import csv
import tempfile
from flask import Flask, request, render_template, redirect, url_for, flash
from werkzeug.utils import secure_filename
import requests
import xml.etree.ElementTree as ET
from gdrive_helper import download_tsv_from_gdrive, upload_tsv_to_gdrive
import base64
from google import genai
from google.genai import types
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "devkey")

# Temporary local TSV file - sync to Google Drive for persistence
TSV_FILE = 'boardgames.tsv'

# Gemini API Setup (You will plug your key here)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def load_tsv():
    if not os.path.exists(TSV_FILE):
        return []
    with open(TSV_FILE, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))

def save_tsv(games):
    fieldnames = ['ID', 'Title', 'MinPlayers', 'MaxPlayers', 'Publisher', 'Designer', 'Weight', 'Mechanics', 'IsExpansion', 'Notes']
    with open(TSV_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        writer.writeheader()
        for game in games:
            writer.writerow(game)
    upload_tsv_to_gdrive()

def extract_titles_from_image(image_path):
    client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1alpha'})

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    response = client.models.generate_content(
        model="gemini-2.0-flash-lite",  # or gemini-2.5-flash if that's what you're using
        contents=[
            types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg"
            ),
            "What are the titles of all the board games in this image? Return the titles only, with no other text, separated by line breaks."
        ]
    )

    # Parse and return titles from response
    titles_text = response.text.strip()
    titles = [line.strip() for line in titles_text.split('\n') if line.strip()]
    print(titles)
    return titles


def search_bgg_game(title):
    """Search BGG for a game by title; return the game ID of an exact match or first result."""
    url = "https://boardgamegeek.com/xmlapi2/search"
    params = {'query': title, 'type': 'boardgame'}
    r = requests.get(url, params=params)
    if r.status_code != 200:
        return None
    root = ET.fromstring(r.content)
    items = root.findall('item')
    if not items:
        return None

    title_lower = title.lower()
    first_id = None

    for item in items:
        if first_id is None:
            first_id = item.attrib.get('id')

        # Find the 'name' element with type='primary'
        name_el = item.find("name[@type='primary']")
        if name_el is not None:
            game_name = name_el.attrib.get('value', '').lower()
            if game_name == title_lower:
                return item.attrib.get('id')  # exact match found

    # No exact match, return first result id
    return first_id

def get_bgg_game_details(game_id):
    """Fetch detailed info for a BGG game by ID using get_values helper"""
    url = "https://boardgamegeek.com/xmlapi2/thing"
    params = {'id': game_id, 'stats': 1}
    r = requests.get(url, params=params)
    if r.status_code != 200:
        return None

    root = ET.fromstring(r.content)
    item = root.find('item')
    if item is None:
        return None

    # Helper to safely extract 'value' attribute
    def get_attr_value(tag):
        el = item.find(tag)
        return el.attrib['value'] if el is not None and 'value' in el.attrib else ''

    def get_values(item, link_type, limit=None):
        values = [link.attrib['value'] for link in item.findall(f"link[@type='{link_type}']")]
        return ", ".join(values[:limit]) if limit else ", ".join(values)

    # Title
    name_el = item.find("name[@type='primary']")
    title = name_el.attrib['value'] if name_el is not None else ''

    # Publishers (limit to 2)
    publisher = get_values(item, "boardgamepublisher", limit=2)

    # Designers (limit to 2)
    designer_str = get_values(item, "boardgamedesigner", limit=2)

    # Mechanics (no limit)
    mechanics_str = get_values(item, "boardgamemechanic")

    # Categories: check for "expansion"
    categories = [link.attrib['value'].lower() for link in item.findall("link[@type='boardgamecategory']")]
    is_expansion = 'Yes' if any('expansion' in cat for cat in categories) else 'No'

    # Min/max players from value attribute
    min_players = get_attr_value('minplayers')
    max_players = get_attr_value('maxplayers')

    # Weight
    weight_el = item.find("statistics/ratings/averageweight")
    weight = weight_el.attrib['value'] if weight_el is not None else ''

    # Notes: blank for now
    notes = ''

    return {
        "ID": game_id,
        "Title": title,
        "MinPlayers": min_players,
        "MaxPlayers": max_players,
        "Publisher": publisher,
        "Designer": designer_str,
        "Weight": weight,
        "Mechanics": mechanics_str,
        "IsExpansion": is_expansion,
        "Notes": notes
    }



# --- Routes ---

@app.route('/')
def index():
    download_tsv_from_gdrive()
    games = load_tsv()
    return render_template('index.html', games=games)

@app.route('/upload-image', methods=['POST'])
def upload_image():
    if 'image' not in request.files:
        flash("No image uploaded", "error")
        return redirect(url_for('index'))
    file = request.files['image']
    if file.filename == '':
        flash("No selected file", "error")
        return redirect(url_for('index'))
    filename = secure_filename(file.filename)
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    file.save(temp_path)

    # 1. Extract titles with Gemini
    titles = extract_titles_from_image(temp_path)

    # 2. For each title, fetch BGG details
    games = load_tsv()
    existing_titles = {g['Title'].lower() for g in games}
    newly_added = 0
    for title in titles:
        if title.lower() in existing_titles:
            flash(f"{title} is already in the database", "info")
        else:
            game_id = search_bgg_game(title)
            if game_id:
                details = get_bgg_game_details(game_id)
                if details:
                    games.insert(0, details)
                    flash(f"{title} added to the database", "success")
                    newly_added += 1
                else:
                    flash(f"Details not found for {title}", "warning")
            else:
                flash(f"Could not find {title} on BoardGameGeek", "warning")

    save_tsv(games)
    flash(f"Added {newly_added} new games from image", "success")
    return redirect(url_for('index'))


@app.route('/add-by-title', methods=['POST'])
def add_by_title():
    title = request.form.get('title')
    if not title:
        flash("Please enter a game title", "error")
        return redirect(url_for('index'))

    games = load_tsv()
    if any(g['Title'].lower() == title.lower() for g in games):
        flash(f"{title} is already in the database.", "info")
        return redirect(url_for('index'))

    game_id = search_bgg_game(title)
    if game_id is None:
        flash(f"Could not find '{title}' on BoardGameGeek.", "error")
        return redirect(url_for('index'))

    details = get_bgg_game_details(game_id)
    if details is None:
        flash(f"Could not retrieve details for '{title}'.", "error")
        return redirect(url_for('index'))

    games.insert(0, details)
    save_tsv(games)
    flash(f"Added '{title}' to the database.", "success")
    return redirect(url_for('index'))

@app.route('/search', methods=['GET', 'POST'])
def search():
    download_tsv_from_gdrive()
    games = load_tsv()
    if request.method == 'POST':
        # Get all search fields, default empty strings
        title = request.form.get('title', '').lower()
        publisher = request.form.get('publisher', '').lower()
        designer = request.form.get('designer', '').lower()
        mechanics = request.form.get('mechanics', '').lower()
        notes = request.form.get('notes', '').lower()
        players = request.form.get('players', '')
        weight = request.form.get('weight', '')
        is_expansion = request.form.get('is_expansion', '')

        def matches(game):
            if title and title not in game['Title'].lower():
                return False
            if publisher and publisher not in game['Publisher'].lower():
                return False
            if designer and designer not in game.get('Designer', '').lower():
                return False
            if mechanics and mechanics not in game.get('Mechanics', '').lower():
                return False
            if notes and notes not in game.get('Notes', '').lower():
                return False
            if players:
                try:
                    num = int(players)
                    game_min = int(game['MinPlayers']) if game['MinPlayers'] else None
                    game_max = int(game['MaxPlayers']) if game['MaxPlayers'] else None
                    if (game_min is not None and num < game_min) or (game_max is not None and num > game_max):
                        return False
                except ValueError:
                    return False  # ignore invalid input
            if weight:
                try:
                    if game['Weight']:
                        w = float(game['Weight'])
                        target = float(weight)
                        if not (target - 0.3 <= w <= target + 0.3):
                            return False
                except ValueError:
                    return False
            if is_expansion and is_expansion != '' and game['IsExpansion'].lower() != is_expansion.lower():
                return False
            return True

        filtered = [g for g in games if matches(g)]
        return render_template('index.html', games=filtered, searched=True)

    # GET: show all
    return render_template('index.html', games=games)


@app.route('/edit/<title>', methods=['GET', 'POST'])
def edit(title):
    download_tsv_from_gdrive()
    games = load_tsv()
    game = next((g for g in games if g['Title'].lower() == title.lower()), None)
    if game is None:
        flash("Game not found", "error")
        return redirect(url_for('index'))

    if request.method == 'POST':
        # Update game info from form fields
        game['Title'] = request.form.get('title', game['Title'])
        game['Publisher'] = request.form.get('publisher', game['Publisher'])
        game['MinPlayers'] = request.form.get('min_players', game['MinPlayers'])
        game['MaxPlayers'] = request.form.get('max_players', game['MaxPlayers'])
        game['Weight'] = request.form.get('weight', game['Weight'])
        game['IsExpansion'] = request.form.get('is_expansion', game['IsExpansion'])
        game['Notes'] = request.form.get('notes', game['Notes'])

        save_tsv(games)
        flash("Game updated successfully", "success")
        return redirect(url_for('index'))

    return render_template('edit.html', game=game)

@app.route('/delete/<game_id>', methods=['POST'])
def delete_game(game_id):
    games = load_tsv()
    updated_games = [g for g in games if str(g.get('ID')) != str(game_id)]

    if len(updated_games) == len(games):
        flash("Game not found.", "error")
    else:
        save_tsv(updated_games)
        flash("Game deleted successfully.", "success")

    return redirect(url_for('index'))

@app.route('/search-by-image', methods=['POST'])
def search_by_image():
    if 'image' not in request.files:
        flash("No image uploaded", "error")
        return redirect(url_for('index'))
    file = request.files['image']
    if file.filename == '':
        flash("No selected file", "error")
        return redirect(url_for('index'))
    filename = secure_filename(file.filename)
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    file.save(temp_path)

    titles = extract_titles_from_image(temp_path)
    if not titles:
        flash("No titles detected in image", "error")
        return redirect(url_for('index'))

    download_tsv_from_gdrive()
    games = load_tsv()
    results = []
    lower_games = {g['Title'].lower(): g for g in games}
    for title in titles:
        g = lower_games.get(title.lower())
        if g:
            results.append(g)

    if not results:
        flash("No matching games found for detected titles", "info")

    return render_template('index.html', games=results)

if __name__ == '__main__':
    app.run(debug=True)
