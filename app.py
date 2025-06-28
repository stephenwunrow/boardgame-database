import os
import csv
import tempfile
from flask import Flask, request, render_template, redirect, url_for, flash, session
import json
from werkzeug.utils import secure_filename
import requests
import xml.etree.ElementTree as ET
from gdrive_helper import download_tsv_from_gdrive, upload_tsv_to_gdrive
from google import genai
from google.genai import types
import string
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "devkey")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "letmein")

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
    fieldnames = ['ID', 'Title', 'MinPlayers', 'MaxPlayers', 'Publisher', 'Designer', 'Weight', 'MinPlaytime', 'MaxPlaytime', 'Mechanics', 'IsExpansion', 'Notes']
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

    def try_model(model_name):
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg"
                ),
                "What are the titles of all the board games in this image? Return the titles only, with no other text, separated by line breaks."
            ]
        )
        return response

    try:
        response = try_model("gemini-2.5-flash")
        flash("Used model: gemini-2.5-flash", "info")
    except Exception as e:
        flash(f"gemini-2.5-flash failed with error: {e}. Trying gemini-2.0-flash...", "warning")
        try:
            response = try_model("gemini-2.0-flash")
            flash("Used model: gemini-2.0-flash", "info")
        except Exception as e2:
            flash(f"Both models failed. Last error: {e2}", "error")
            return []

    titles_text = response.text.strip()
    titles = [line.strip() for line in titles_text.split('\n') if line.strip()]
    if titles:
        flash(f"Gemini extracted {len(titles)} title(s): " + ", ".join(titles), "info")
    else:
        flash("Gemini returned no titles from the image.", "warning")

    return titles

def strip_punctuation(text):
    return text.translate(str.maketrans('', '', string.punctuation))

def search_bgg_games(title):
    """Search BGG for board games by title. Return a list of potential matches."""
    url = "https://boardgamegeek.com/xmlapi2/search"
    params = {'query': title, 'type': 'boardgame'}
    r = requests.get(url, params=params)

    if r.status_code != 200:
        return []

    root = ET.fromstring(r.content)
    items = root.findall('item')
    if not items:
        return []

    title_clean = strip_punctuation(title.lower())
    matches = []

    for item in items:
        game_id = item.attrib.get('id')
        year_el = item.find("yearpublished")
        year = year_el.attrib.get("value", "") if year_el is not None else ""
        
        name_el = item.find("name[@type='primary']")
        if name_el is not None:
            game_title = name_el.attrib.get('value', '')
            game_title_clean = strip_punctuation(game_title.lower())

            # Match exact title or partial match containing search term
            if title_clean in game_title_clean:
                matches.append({
                    'id': game_id,
                    'title': game_title,
                    'year': year
                })
    return matches

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

    # Min/max playtime from value attribute
    min_playtime = get_attr_value('minplaytime')
    max_playtime = get_attr_value('maxplaytime')

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
        "MinPlaytime": min_playtime,
        "MaxPlaytime": max_playtime,
        "Mechanics": mechanics_str,
        "IsExpansion": is_expansion,
        "Notes": notes
    }

def sort_games(games, sort_by):
    key_funcs = {
        'title': lambda g: g.get('Title', '').lower(),
        'weight': lambda g: float(g.get('Weight', '0') or 0),
        'designer': lambda g: g.get('Designer', '').lower(),
        'publisher': lambda g: g.get('Publisher', '').lower(),
        'notes': lambda g: g.get('Notes', '').lower(),
    }

    if sort_by in key_funcs:
        return sorted(games, key=key_funcs[sort_by])
    else:
        return games  # return as-is for default order

# --- Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == SITE_PASSWORD:
            session['logged_in'] = True
            flash("Logged in successfully.", "success")
            return redirect(url_for('index'))
        else:
            flash("Incorrect password.", "error")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    download_tsv_from_gdrive()
    sort_by = request.args.get('sort')

    if 'search_results' in session:
        games = json.loads(session['search_results'])  # load filtered games
        searched = True
    else:
        games = load_tsv()
        searched = False

    if sort_by:
        if sort_by == 'title':
            games.sort(key=lambda g: g['Title'].lower())
        elif sort_by == 'weight':
            games.sort(key=lambda g: float(g['Weight']) if g['Weight'] else 0)
        elif sort_by == 'designer':
            games.sort(key=lambda g: g['Designer'].lower() if g['Designer'] else '')
        elif sort_by == 'publisher':
            games.sort(key=lambda g: g['Publisher'].lower() if g['Publisher'] else '')
        elif sort_by == 'notes':
            games.sort(key=lambda g: g['Notes'].lower() if g['Notes'] else '')

    return render_template('index.html', games=games, searched=searched, sort_by=sort_by)


@app.route('/upload-image', methods=['POST'])
def upload_image():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

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
    existing_titles = {g['Title'].lower() for g in games}

    # Queue titles not already in the TSV
    session['pending_titles'] = [t for t in titles if t.lower() not in existing_titles]
    session['selected_games'] = []
    session.modified = True

    if not session['pending_titles']:
        flash("All titles are already in the database.", "info")
        return redirect(url_for('index'))

    return redirect(url_for('process_next_title'))


@app.route('/process-next-title', methods=['GET', 'POST'])
def process_next_title():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    pending_titles = session.get('pending_titles', [])
    selected_games = session.get('selected_games', [])

    if not pending_titles:
        # When done, prepare 'pending_games' for confirmation
        # Fetch details for all selected games
        pending_games = []
        for game_id in selected_games:
            details = get_bgg_game_details(game_id)
            if details:
                pending_games.append({'original_title': details['Title'], 'matches': [details]})
        session['pending_games'] = pending_games

        # Clear pending_titles and selected_games
        session.pop('pending_titles', None)
        # session.pop('selected_games', None)
        session.modified = True

        return redirect(url_for('confirm_add_all'))

    current_title = pending_titles[0]

    if request.method == 'POST':
        selected_game_id = request.form.get('selected_game_id')
        if not selected_game_id:
            flash("Please select a game to add.", "error")
            # We'll re-render page below

        else:
            selected_games.append(selected_game_id)
            pending_titles.pop(0)
            session['pending_titles'] = pending_titles
            session['selected_games'] = selected_games
            session.modified = True

            if pending_titles:
                return redirect(url_for('process_next_title'))
            else:
                # Prepare 'pending_games' for confirmation immediately
                pending_games = []
                for game_id in selected_games:
                    details = get_bgg_game_details(game_id)
                    if details:
                        pending_games.append({'original_title': details['Title'], 'matches': [details]})
                session['pending_games'] = pending_games

                # Clear pending_titles and selected_games since done
                session.pop('pending_titles', None)
                # session.pop('selected_games', None)
                session.modified = True

                return redirect(url_for('confirm_add_all'))

    # GET request or POST with no selection: search matches
    matches = search_bgg_games(current_title)

    if not matches:
        flash(f"Could not find '{current_title}' on BoardGameGeek.", "warning")
        # skip this title, remove from pending and continue
        pending_titles.pop(0)
        session['pending_titles'] = pending_titles
        session.modified = True
        return redirect(url_for('process_next_title'))

    if len(matches) == 1:
        # Automatically select single match
        selected_games.append(matches[0]['id'])
        pending_titles.pop(0)
        session['pending_titles'] = pending_titles
        session['selected_games'] = selected_games
        session.modified = True
        return redirect(url_for('process_next_title'))

    # Multiple matches: render selection page
    return render_template('choose_many_games.html', matches=matches, original_title=current_title)

@app.route('/confirm-add-all', methods=['GET', 'POST'])
def confirm_add_all():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    selected_game_ids = session.get('selected_games', [])
    if not selected_game_ids:
        flash("No games selected to add.", "info")
        return redirect(url_for('index'))

    if request.method == 'POST':
        games = load_tsv()
        existing_titles = {g['Title'].lower() for g in games}
        newly_added = 0
        for game_id in selected_game_ids:
            details = get_bgg_game_details(game_id)
            if details and details['Title'].lower() not in existing_titles:
                games.insert(0, details)
                newly_added += 1
                existing_titles.add(details['Title'].lower())

        save_tsv(games)
        flash(f"Added {newly_added} new games to the database.", "success")

        # Clear session data
        session.pop('pending_titles', None)
        session.pop('selected_games', None)
        session.modified = True

        return redirect(url_for('index'))

    # GET: show all selected games details for final confirmation
    detailed_games = []
    for game_id in selected_game_ids:
        details = get_bgg_game_details(game_id)
        if details:
            detailed_games.append(details)

    return render_template('confirm_add_all.html', games=detailed_games)

@app.route('/add-by-title', methods=['POST'])
def add_by_title():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    session.pop('pending_titles', None)
    session.pop('selected_games', None)
    session.pop('pending_games', None)

    title = request.form.get('title')
    if not title:
        flash("Please enter a game title", "error")
        return redirect(url_for('index'))

    games = load_tsv()
    if any(g['Title'].lower() == title.lower() for g in games):
        flash(f"{title} is already in the database.", "info")
        return redirect(url_for('index'))

    # Search BGG for multiple matches
    matches = search_bgg_games(title)
    if not matches:
        flash(f"No matches found for '{title}' on BoardGameGeek.", "error")
        return redirect(url_for('index'))

    # If only one match, add it directly
    if len(matches) == 1:
        return redirect(url_for('confirm_add', selected_game_id=matches[0]['id']))

    # Otherwise, show selection template
    return render_template('choose_game.html', matches=matches, original_title=title)

@app.route('/confirm-add', methods=['GET', 'POST'])
def confirm_add():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        selected_game_id = request.form.get('selected_game_id')

        if not selected_game_id:
            flash("Please select a game to add.", "error")
            return redirect(url_for('index'))

        details = get_bgg_game_details(selected_game_id)
        if not details:
            flash("Could not retrieve game details.", "error")
            return redirect(url_for('index'))

        games = load_tsv()
        if any(g['Title'].lower() == details['Title'].lower() for g in games):
            flash(f"'{details['Title']}' is already in the database.", "info")
        else:
            games.insert(0, details)
            save_tsv(games)
            flash(f"Added '{details['Title']}' to the database.", "success")

        return redirect(url_for('index'))

    # GET request: maybe redirected here with ?selected_game_id=
    selected_game_id = request.args.get('selected_game_id')
    if selected_game_id:
        details = get_bgg_game_details(selected_game_id)
        if not details:
            flash("Could not retrieve game details.", "error")
            return redirect(url_for('index'))

        return render_template('confirm_add.html', original_title=details)

    # Default fallback
    flash("Nothing to confirm.", "warning")
    return redirect(url_for('index'))



@app.route('/search', methods=['GET', 'POST'])
def search():
    download_tsv_from_gdrive()
    games = load_tsv()

    if request.method == 'POST':
        # Get sort param from query or default None
        sort_by = request.args.get('sort') or None

        # Get all search fields, default empty strings
        title = request.form.get('title', '').lower()
        publisher = request.form.get('publisher', '').lower()
        designer = request.form.get('designer', '').lower()
        mechanics = request.form.get('mechanics', '').lower()
        notes = request.form.get('notes', '').lower()
        players = request.form.get('players', '')
        playtime = request.form.get('playtime')
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
            if playtime:
                try:
                    num = int(playtime)
                    game_min = int(game['MinPlaytime']) if game['MinPlaytime'] else None
                    game_max = int(game['MaxPlaytime']) if game['MaxPlaytime'] else None
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

        # Sort filtered if sort_by present
        if sort_by:
            filtered = sort_games(filtered, sort_by)

        # Store filtered results in session for consistency
        session['search_results'] = json.dumps(filtered)

        return render_template('index.html', games=filtered, sort_by=sort_by, searched=True)

    # GET request shows all games
    sort_by = request.args.get('sort')
    if 'search_results' in session:
        games = json.loads(session['search_results'])
        searched = True
    else:
        searched = False

    if sort_by:
        games = sort_games(games, sort_by)

    return render_template('index.html', games=games, sort_by=sort_by, searched=searched)

@app.route('/edit/<title>', methods=['GET', 'POST'])
def edit(title):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
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
        game['MinPlaytime'] = request.form.get('min_playtime', game['MinPlaytime'])
        game['MaxPlaytime'] = request.form.get('max_playtime', game['MaxPlaytime'])
        game['IsExpansion'] = request.form.get('is_expansion', game['IsExpansion'])
        game['Notes'] = request.form.get('notes', game['Notes'])

        save_tsv(games)
        flash("Game updated successfully", "success")
        return redirect(url_for('index'))

    return render_template('edit.html', game=game)

@app.route('/delete/<game_id>', methods=['POST'])
def delete_game(game_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    games = load_tsv()
    updated_games = [g for g in games if str(g.get('ID')) != str(game_id)]

    if len(updated_games) == len(games):
        flash("Game not found.", "error")
    else:
        save_tsv(updated_games)
        flash("Game deleted successfully.", "success")

    return redirect(url_for('index'))

@app.route('/clear')
def clear():
    session.pop('search_results', None)
    return redirect(url_for('index'))

@app.route('/search-by-image', methods=['POST'])
def search_by_image():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
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

    return render_template('index.html', games=results, searched=True)

if __name__ == '__main__':
    app.run(debug=True)
