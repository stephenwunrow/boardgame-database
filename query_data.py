import re
import csv

def load_titles():
    file_path = "boardgames.tsv"
    titles = []
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) > 1:
                titles.append(row[1])
    return titles
        
def find_titles(search_phrase):
    titles = load_titles()
    search_phrase = re.sub(r"(’|‘)", "'", search_phrase)
    search_phrase = re.sub(r'(”|“)', '"', search_phrase)

    if search_phrase.count('"') == 2:
        found_titles = []
        search_phrase = search_phrase.strip('"')
        for title in titles:
            found_title = re.search(re.escape(search_phrase), title, re.IGNORECASE)
            if found_title:
                found_titles.append(title)
    
    else:
        found_titles = []
        terms = search_phrase.split()
        for title in titles:
            title_lower = title.lower()
            if all(term.lower() in title_lower for term in terms):
                found_titles.append(title)

    print(found_titles)
    return found_titles
