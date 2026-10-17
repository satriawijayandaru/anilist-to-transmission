import os
import re
import time
import socket
import urllib.request
import json
import requests
import feedparser
import urllib.parse
import schedule
from dotenv import load_dotenv
from transmission_rpc import Client

# Load environment variables
load_dotenv()

ANILIST_USERNAME = os.getenv("ANILIST_USERNAME")
TRANSMISSION_HOST = os.getenv("TRANSMISSION_HOST", "11.11.11.111")
TRANSMISSION_PORT = int(os.getenv("TRANSMISSION_PORT", 9091))
TRANSMISSION_USER = os.getenv("TRANSMISSION_USER", "")
TRANSMISSION_PASS = os.getenv("TRANSMISSION_PASS", "")
RESOLUTION = os.getenv("RESOLUTION", "1080p")
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", 1))

HISTORY_FILE = "data/history.json"

# --- DNS OVER HTTPS (DoH) BYPASS ---
_original_getaddrinfo = socket.getaddrinfo

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'nyaa.si':
        try:
            req = urllib.request.Request(
                'https://1.1.1.1/dns-query?name=nyaa.si&type=A',
                headers={'accept': 'application/dns-json'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                if 'Answer' in data:
                    ip = data['Answer'][0]['data']
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
        except Exception as e:
            print(f"[-] DoH resolution failed: {e}")
    return _original_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = patched_getaddrinfo

# --- HISTORY & TRANSMISSION TRACKER ---
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=4)

def get_transmission_torrents():
    try:
        client = Client(
            host=TRANSMISSION_HOST, 
            port=TRANSMISSION_PORT, 
            username=TRANSMISSION_USER, 
            password=TRANSMISSION_PASS
        )
        return [torrent.name for torrent in client.get_torrents()]
    except Exception as e:
        print(f"[-] Could not fetch torrents from Transmission: {e}")
        return []

def check_exists_in_transmission(title, episode_num, is_batch, existing_torrents):
    clean_target_title = re.sub(r'[^\w\s]', ' ', title.lower())
    title_words = clean_target_title.split()
    
    for t_name in existing_torrents:
        clean_t_name = re.sub(r'[^\w\s]', ' ', t_name.lower())
        
        if all(word in clean_t_name for word in title_words):
            if is_batch:
                if "batch" in clean_t_name or "complete" in clean_t_name or re.search(r'01\s+\d+', clean_t_name):
                    return True
            else:
                ep_pattern = r'(?:\b|e)0?' + str(episode_num) + r'(?:\b|v\d+)'
                if re.search(ep_pattern, clean_t_name):
                    return True
    return False

# --- CORE LOGIC ---
def get_anilist_watchlist(username):
    url = 'https://graphql.anilist.co'
    query = '''
    query ($name: String) {
      MediaListCollection(userName: $name, type: ANIME) {
        lists {
          entries {
            status
            progress
            media {
              title { romaji english }
              status
              episodes
            }
          }
        }
      }
    }
    '''
    response = requests.post(url, json={'query': query, 'variables': {'name': username}})
    response.raise_for_status()
    data = response.json()
    
    watchlist = []
    if 'data' in data and data['data']['MediaListCollection']:
        for anime_list in data['data']['MediaListCollection']['lists']:
            for entry in anime_list['entries']:
                if entry.get('status') in ['CURRENT', 'PLANNING']:
                    # Extract both titles so we can search both
                    romaji = entry['media']['title'].get('romaji')
                    english = entry['media']['title'].get('english')
                    primary_title = romaji or english
                    
                    watchlist.append({
                        'title': primary_title,
                        'romaji': romaji,
                        'english': english,
                        'status': entry['media']['status'],
                        'progress': entry['progress'],
                        'total_episodes': entry['media']['episodes']
                    })
    return watchlist

def extract_magnet(entry):
    info_hash = entry.get('nyaa_infohash')
    if not info_hash:
        return None
    safe_title = urllib.parse.quote(entry.title)
    return (
        f"magnet:?xt=urn:btih:{info_hash}&dn={safe_title}"
        f"&tr=http%3A%2F%2Fnyaa.tracker.wf%3A7777%2Fannounce"
        f"&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    )

def search_nyaa(title, is_batch=False, episode_num=None):
    clean_title = re.sub(r'[^\w\s]', ' ', title)
    clean_title = " ".join(clean_title.split())
    
    # 1. GENERATE SMART SEARCH VARIATIONS
    queries = []
    if is_batch:
        queries.append(f"{clean_title} batch {RESOLUTION}")
        queries.append(f"{clean_title} complete {RESOLUTION}")
        # Add a raw title search as a last-resort for old torrents that don't say "batch"
        queries.append(f"{clean_title} {RESOLUTION}") 
    else:
        queries.append(f"{clean_title} {episode_num:02d} {RESOLUTION}")
        
        season_match = re.search(r'(\d)(?:st|nd|rd|th)\s+season', clean_title, re.IGNORECASE)
        if season_match:
            s_num = int(season_match.group(1))
            base = re.sub(r'\d(?:st|nd|rd|th)\s+season', '', clean_title, flags=re.IGNORECASE).strip()
            queries.append(f"{base} Season {s_num} {episode_num:02d} {RESOLUTION}")
            queries.append(f"{base} S{s_num:02d}E{episode_num:02d} {RESOLUTION}")
            
        season_match_2 = re.search(r'season\s+(\d)', clean_title, re.IGNORECASE)
        if season_match_2 and not season_match:
            s_num = int(season_match_2.group(1))
            base = re.sub(r'season\s+\d', '', clean_title, flags=re.IGNORECASE).strip()
            queries.append(f"{base} S{s_num:02d}E{episode_num:02d} {RESOLUTION}")
    
    priority_groups = ["judas", "erai-raws", "coalgirls"] # Added coalgirls for older shows
    
    for search_query in queries:
        print(f"[*] Searching Nyaa: {search_query}")
        encoded_query = urllib.parse.quote(search_query)
        url = f"https://nyaa.si/?page=rss&q={encoded_query}&c=1_2&f=0&s=seeders&o=desc"
        
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue
            
        if not feed.entries:
            print("    [-] No results for this variation.")
            continue
            
        best_priority_result = None
        max_priority_seeders = -1
        best_general_result = None
        max_general_seeders = -1
        
        tgt_lower = title.lower()
        
        def extract_season(text):
            m = re.search(r'(?:season\s*(\d+)|\b(\d+)(?:st|nd|rd|th)\s+season|s0*(\d+)|part\s*(\d+)|cour\s*(\d+))', text)
            if m:
                return int(next(g for g in m.groups() if g is not None))
            if "final season" in text:
                return 99
            return 1
            
        tgt_season = extract_season(tgt_lower)
        
        for entry in feed.entries:
            try:
                seeders = int(entry.get('nyaa_seeders', 0))
            except ValueError:
                seeders = 0
                
            if seeders == 0:
                continue
                
            t_lower = entry.title.lower()
            
            # --- STRICT TITLE VALIDATION ---
            bad_keywords = ["gaiden", "spin off", "spin-off", "movie", "ova", "ona", "special", "recap"]
            is_bad = any(word in t_lower and word not in tgt_lower for word in bad_keywords)
            if is_bad:
                continue
                
            if re.search(r'(season|part|cour|s\d+)', t_lower):
                tor_season = extract_season(t_lower)
                if tor_season != tgt_season:
                    continue
                    
            if not is_batch:
                ep_pattern = r'(?:^|\s|-|e|ep)0*' + str(episode_num) + r'\b(?:v\d+)?(?!-?bit)'
                if not re.search(ep_pattern, t_lower):
                    continue
            # -------------------------------
                
            is_priority = any(group in t_lower for group in priority_groups)
            
            if is_priority and seeders > max_priority_seeders:
                max_priority_seeders = seeders
                best_priority_result = entry
                
            if seeders > max_general_seeders:
                max_general_seeders = seeders
                best_general_result = entry

        if best_priority_result:
            print(f"    [+] Priority Match: {best_priority_result.title} (Seeders: {max_priority_seeders})")
            return extract_magnet(best_priority_result)
        elif best_general_result:
            print(f"    [+] Fallback Match: {best_general_result.title} (Seeders: {max_general_seeders})")
            return extract_magnet(best_general_result)
            
    print("    [-] Exhausted all search variations. No valid torrents found.")
    return None

def upload_to_transmission(magnet_link, save_path=None):
    try:
        client = Client(
            host=TRANSMISSION_HOST, 
            port=TRANSMISSION_PORT, 
            username=TRANSMISSION_USER, 
            password=TRANSMISSION_PASS
        )
        
        kwargs = {}
        if save_path:
            kwargs['download_dir'] = save_path
            
        client.add_torrent(magnet_link, **kwargs)
        
        if save_path:
            print(f"    [+] Sent to Transmission -> Folder: {save_path}\n")
        else:
            print(f"    [+] Sent to Transmission -> Default Folder\n")
            
    except Exception as e:
        print(f"    [-] Failed to add to Transmission: {e}\n")

def job():
    print(f"\n--- Starting Sync: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    if not ANILIST_USERNAME:
        print("[-] ANILIST_USERNAME is not set in .env")
        return

    try:
        watchlist = get_anilist_watchlist(ANILIST_USERNAME)
    except Exception as e:
        print(f"[-] Failed to fetch watchlist: {e}")
        return

    history = load_history()
    existing_torrents = get_transmission_torrents()

    # Connect to Transmission to get the base download directory
    try:
        client = Client(
            host=TRANSMISSION_HOST, 
            port=TRANSMISSION_PORT, 
            username=TRANSMISSION_USER, 
            password=TRANSMISSION_PASS
        )
        base_dir = client.get_session().download_dir
    except Exception as e:
        print(f"[-] Could not connect to Transmission to get base directory: {e}")
        return

    for show in watchlist:
        title = show['title']
        status = show['status']
        progress = show['progress']
        total_episodes = show['total_episodes']
        
        search_titles = []
        if show['romaji']: 
            search_titles.append(show['romaji'])
        if show['english'] and show['english'] != show['romaji']: 
            search_titles.append(show['english'])
        
        if title not in history:
            history[title] = {}

        # Sanitize the title and aggressively format the path for Linux
        safe_folder_name = re.sub(r'[\\/*?:"<>|]', "", title).strip()
        custom_download_dir = f"{base_dir.rstrip('/')}/{safe_folder_name}"

        # 1. Batch logic
        if status == "FINISHED" and not history[title].get('batch_downloaded'):
            if check_exists_in_transmission(title, None, True, existing_torrents):
                print(f"[*] Skipping {title} Batch: Already exists in Transmission.")
                history[title]['batch_downloaded'] = True
                save_history(history)
                continue
            
            magnet = None
            for search_title in search_titles:
                magnet = search_nyaa(search_title, is_batch=True)
                if magnet:
                    break
                    
            if magnet:
                upload_to_transmission(magnet, save_path=custom_download_dir)
                history[title]['batch_downloaded'] = True
                save_history(history)
                continue
            else:
                print(f"    [-] No batch found for {title}, falling back to single episodes.")

        # 2. Sequential logic
        last_downloaded = history[title].get('latest_ep', 0)
        start_ep = max(progress, last_downloaded) + 1
        end_ep = total_episodes if total_episodes else 9999
        
        while start_ep <= end_ep:
            if check_exists_in_transmission(title, start_ep, False, existing_torrents):
                print(f"[*] Skipping {title} Episode {start_ep:02d}: Already exists in Transmission.")
                history[title]['latest_ep'] = start_ep
                save_history(history)
                start_ep += 1
                continue

            magnet = None
            for search_title in search_titles:
                magnet = search_nyaa(search_title, is_batch=False, episode_num=start_ep)
                if magnet:
                    break

            if magnet:
                upload_to_transmission(magnet, save_path=custom_download_dir)
                history[title]['latest_ep'] = start_ep
                save_history(history)
                start_ep += 1
                time.sleep(2)
            else:
                break

    print("--- Sync Complete ---\n")
    
def main():
    print("Initializing Nyaa-Transmission Auto-Sync...")
    print(f"Target: {TRANSMISSION_HOST}:{TRANSMISSION_PORT}")
    print(f"Schedule: Every {CHECK_INTERVAL_HOURS} hour(s)")
    
    job()
    schedule.every(CHECK_INTERVAL_HOURS).hours.do(job)
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()