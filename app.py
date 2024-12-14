from flask import Flask, redirect, url_for, session, request, render_template, jsonify, make_response
import requests
import random
import string
from urllib.parse import urlencode
from threading import Thread

app = Flask(__name__)
app.secret_key = 'FzoY?LYL5moT:Iex"m18/0.pa!K-wG'

# Spotify API credentials
CLIENT_ID = '04703f4623b846f1ae4202c56e9424ff'
CLIENT_SECRET = 'ae11f2cacd8c403ebdaa7f221f8cd062'
REDIRECT_URI = 'http://localhost:5000/callback'
SCOPE = 'user-library-read playlist-read-private'

# Global dictionary to track loading status and progress
loading_status = {}

def generate_session_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

def fetch_all_tracks(access_token, session_id):
    headers = {'Authorization': f'Bearer {access_token}'}
    url = 'https://api.spotify.com/v1/me/playlists?limit=50'
    playlists = []

    # Fetch all playlists with pagination
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Error fetching playlists: {response.status_code} - {response.text}")
            loading_status[session_id]['tracks_loaded'] = True
            return
        data = response.json()
        playlists.extend(data.get('items', []))
        url = data.get('next')  # Get the next page of playlists

    all_tracks = []
    total_playlists = len(playlists)
    loaded_playlists = 0

    # Fetch tracks for each playlist
    for playlist in playlists:
        playlist_name = playlist['name']
        playlist_id = playlist['id']
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100'

        while url:
            response = requests.get(url, headers=headers)
            if response.status_code == 429:  # Rate limit hit
                retry_after = int(response.headers.get('Retry-After', 1))
                print(f"Rate limit hit. Retrying after {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            if response.status_code != 200:
                print(f"Error fetching tracks for playlist {playlist_name}: {response.status_code} - {response.text}")
                break
            data = response.json()
            for track in data.get('items', []):
                track_info = track['track']
                if track_info:  # No longer depend on preview_url
                    track_info['playlist_name'] = playlist_name
                    all_tracks.append(track_info)
            url = data.get('next')  # Get the next page of tracks

        # Update progress
        loaded_playlists += 1
        loading_status[session_id]['progress'] = (loaded_playlists / total_playlists) * 100
        print(f"Progress: {loading_status[session_id]['progress']}% - Tracks loaded so far: {len(all_tracks)}")

    # Save all tracks and mark loading as complete
    print(f"Total tracks fetched: {len(all_tracks)}")
    loading_status[session_id]['all_tracks'] = all_tracks
    loading_status[session_id]['tracks_loaded'] = True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    session.clear()
    auth_url = "https://accounts.spotify.com/authorize"
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
        'show_dialog': 'true'
    }
    return redirect(f"{auth_url}?{urlencode(params)}")

@app.route('/callback')
def callback():
    code = request.args.get('code')
    token_url = "https://accounts.spotify.com/api/token"
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }
    response = requests.post(token_url, data=token_data)
    response_data = response.json()
    session['access_token'] = response_data['access_token']
    
    # Generate a session ID and store it in the session
    session_id = generate_session_id()
    session['session_id'] = session_id
    
    # Initialize loading status for this session
    loading_status[session_id] = {'tracks_loaded': False, 'progress': 0, 'all_tracks': []}
    
    # Start a thread to fetch tracks in the background
    Thread(target=fetch_all_tracks, args=(session['access_token'], session_id)).start()
    return redirect(url_for('loading'))

@app.route('/loading')
def loading():
    return render_template('loading.html')

@app.route('/check_loading_status')
def check_loading_status():
    session_id = session.get('session_id')
    if not session_id:
        return jsonify({'tracks_loaded': False, 'progress': 0})
    
    status = loading_status.get(session_id, {'tracks_loaded': False, 'progress': 0})
    return jsonify({
        'tracks_loaded': status.get('tracks_loaded', False),
        'progress': status.get('progress', 0),
        'message': f"Loaded {int(status.get('progress', 0))}% of playlists"
    })

@app.route('/logout')
def logout():
    session.pop('access_token', None)
    session.pop('session_id', None)
    session.clear()
    session.modified = True
    response = redirect(url_for('index'))
    response.set_cookie('session', '', expires=0)
    return response

@app.route('/home')
def home():
    print("Session Data:", session)
    print("Loading Status:", loading_status.get(session.get('session_id'), {}))
    if 'access_token' not in session:
        return redirect(url_for('index'))
    
    response = make_response(render_template('home.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/get_random_song')
def get_random_song():
    session_id = session.get('session_id')
    if not session_id:
        return render_template('home.html', random_track="Session expired. Please log in again.", track_image_url=None, artist_name=None, playlist_name=None)

    status = loading_status.get(session_id, {})
    all_tracks = status.get('all_tracks', [])
    if not all_tracks:
        return render_template('home.html', random_track="Tracks are still loading or failed to load. Please try again later.", track_image_url=None, artist_name=None, playlist_name=None)
    
    random_track_info = random.choice(all_tracks)
    random_track_name = random_track_info['name']
    track_image_url = random_track_info['album']['images'][0]['url'] if random_track_info['album']['images'] else None
    artist_name = random_track_info['artists'][0]['name'] if random_track_info['artists'] else "Unknown Artist"
    playlist_name = random_track_info['playlist_name']
    return render_template('home.html', random_track=random_track_name, track_image_url=track_image_url, artist_name=artist_name, playlist_name=playlist_name)

if __name__ == '__main__':
    app.run(debug=True)
