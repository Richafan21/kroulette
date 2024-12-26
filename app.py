import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

from flask import Flask, redirect, url_for, session, request, render_template, jsonify, make_response, flash
import requests
import random
import string
from urllib.parse import urlencode
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import time
from threading import Thread
from datetime import timedelta
import gc
from collections import defaultdict
import uuid
from flask_socketio import SocketIO, emit, join_room, leave_room
import pkg_resources

app = Flask(__name__)
app.secret_key = 'FzoY?LYL5moT:Iex"m18/0.pa!K-wG'
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)

# Configure Flask logger to use the same format
app.logger.handlers = []
for handler in logging.getLogger().handlers:
    app.logger.addHandler(handler)

# Log spotipy version
spotipy_version = pkg_resources.get_distribution('spotipy').version
app.logger.info(f"Using spotipy version: {spotipy_version}")

socketio = SocketIO(app)

# Configure Flask session to be more robust
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS

# Spotify API credentials and settings
CLIENT_ID = '04703f4623b846f1ae4202c56e9424ff'
CLIENT_SECRET = 'ae11f2cacd8c403ebdaa7f221f8cd062'
SCOPE = 'user-library-read playlist-read-private playlist-read-collaborative user-read-private'

# Global variables
port = 5000  # Default port
loading_status = {}  # Track loading status and progress
rooms = {}  # Store room information
active_rooms = {}  # Store active room info
user_tracks = {}  # Store tracks for each session

def get_redirect_uri(port=5000):
    return f'http://127.0.0.1:{port}/callback'

def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=get_redirect_uri(port),
        scope=SCOPE,
        cache_path=None,  # Don't cache tokens to file
        show_dialog=True,  # Always show the Spotify login dialog
        open_browser=False
    )

@app.route('/login')
def login():
    """Start the Spotify OAuth flow"""
    app.logger.info("Starting login process")
    
    # Generate a new session ID
    session['session_id'] = str(uuid.uuid4())
    session.modified = True
    
    # Get the authorization URL
    auth_url = get_spotify_oauth().get_authorize_url()
    app.logger.info(f"Redirecting to Spotify auth URL for session {session['session_id']}")
    
    return redirect(auth_url)

@app.route('/callback')
def callback():
    """Handle the Spotify OAuth callback"""
    app.logger.info("Received callback from Spotify")
    
    # Ensure session ID exists
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
        session.modified = True
    
    app.logger.info(f"Session ID: {session.get('session_id')}")
    
    try:
        # Get the authorization code
        code = request.args.get('code')
        if not code:
            app.logger.error("No code in callback")
            flash('Failed to authenticate with Spotify.')
            return redirect(url_for('index'))
        
        # Exchange code for tokens
        token_info = get_spotify_oauth().get_access_token(code, check_cache=False)
        if not token_info:
            app.logger.error("Failed to get access token")
            flash('Failed to authenticate with Spotify.')
            return redirect(url_for('index'))
        
        # Store tokens in session
        session['access_token'] = token_info['access_token']
        session['refresh_token'] = token_info['refresh_token']
        session['token_expiry'] = time.time() + token_info['expires_in']
        session.modified = True
        
        app.logger.info("Successfully authenticated with Spotify")
        return redirect(url_for('loading'))
        
    except Exception as e:
        app.logger.error(f"Error in callback: {str(e)}")
        flash('An error occurred during authentication.')
        return redirect(url_for('index'))

def update_loading_status(session_id, status):
    """Update loading status for a session"""
    global loading_status
    loading_status[session_id] = status
    app.logger.info(f"Updated loading status for session {session_id}: {status}")

def fetch_all_tracks(session_id, access_token):
    """Fetch all tracks from user's playlists"""
    app.logger.info(f"=== Starting fetch_all_tracks for session {session_id} ===")
    
    try:
        # Initialize Spotify client
        sp = spotipy.Spotify(auth=access_token)
        
        # Test connection
        try:
            user = sp.current_user()
            app.logger.info(f"Connected as user: {user['id']}")
        except Exception as e:
            app.logger.error(f"Failed to connect to Spotify: {str(e)}")
            update_loading_status(session_id, {
                'is_loading': False,
                'error': 'Failed to connect to Spotify',
                'progress': 0,
                'track_count': 0,
                'current_playlist': ''
            })
            return

        # Get user's playlists
        app.logger.info("Fetching playlists")
        playlists = []
        offset = 0
        
        while True:
            try:
                response = sp.current_user_playlists(offset=offset, limit=50)
                playlists.extend(response['items'])
                if len(response['items']) < 50:
                    break
                offset += 50
            except spotipy.exceptions.SpotifyException as e:
                if e.http_status == 429:  # Rate limit error
                    retry_after = int(e.headers.get('Retry-After', 1))
                    app.logger.info(f"Rate limit hit, waiting {retry_after} seconds")
                    time.sleep(retry_after)
                    continue
                raise

        app.logger.info(f"Found {len(playlists)} playlists")
        
        # Process each playlist
        tracks = []
        track_ids = set()
        
        for i, playlist in enumerate(playlists):
            try:
                playlist_name = playlist['name']
                app.logger.info(f"Processing playlist {i+1}/{len(playlists)}: {playlist_name}")
                
                # Update status
                update_loading_status(session_id, {
                    'is_loading': True,
                    'progress': int((i / len(playlists)) * 100),
                    'track_count': len(tracks),
                    'current_playlist': playlist_name,
                    'error': None
                })
                
                # Get tracks with retry logic
                max_retries = 3
                playlist_tracks = None
                
                for retry in range(max_retries):
                    try:
                        playlist_tracks = sp.playlist_tracks(playlist['id'])
                        break
                    except spotipy.exceptions.SpotifyException as e:
                        if e.http_status == 429 and retry < max_retries - 1:
                            retry_after = int(e.headers.get('Retry-After', 1))
                            time.sleep(retry_after)
                            continue
                        raise
                        
                if not playlist_tracks:
                    app.logger.error(f"Failed to fetch tracks for playlist {playlist_name}")
                    continue
                
                # Process tracks
                for item in playlist_tracks['items']:
                    if item and item.get('track'):
                        track = item['track']
                        if track and track.get('id') and track['id'] not in track_ids:
                            track_ids.add(track['id'])
                            tracks.append({
                                'id': track['id'],
                                'name': track['name'],
                                'artists': [artist['name'] for artist in track['artists']],
                                'album': track['album']['name'],
                                'uri': track['uri'],
                                'playlist_name': playlist_name
                            })
                            
            except Exception as e:
                app.logger.error(f"Error processing playlist {playlist_name}: {str(e)}")
                continue
        
        app.logger.info(f"Successfully loaded {len(tracks)} tracks")
        
        # Store tracks and update final status
        user_tracks[session_id] = tracks
        update_loading_status(session_id, {
            'is_loading': False,
            'progress': 100,
            'track_count': len(tracks),
            'current_playlist': 'Done!',
            'error': None
        })
        
    except Exception as e:
        app.logger.error(f"Error in fetch_all_tracks: {str(e)}")
        update_loading_status(session_id, {
            'is_loading': False,
            'error': str(e),
            'progress': 0,
            'track_count': 0,
            'current_playlist': ''
        })

@app.route('/loading')
def loading():
    """Start loading tracks from Spotify"""
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
        session.modified = True
    
    session_id = session['session_id']
    app.logger.info(f"Loading route - Session ID: {session_id}")
    app.logger.info(f"Session contents: {dict(session)}")
    
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return redirect(url_for('index'))
    
    # Start loading tracks if not already started
    if session_id not in loading_status:
        app.logger.info("Initializing loading status")
        update_loading_status(session_id, {
            'is_loading': True,
            'progress': 0,
            'track_count': 0,
            'current_playlist': 'Starting...',
            'error': None
        })
        
        # Start loading tracks in background
        app.logger.info(f"Starting background thread for session {session_id}")
        app.logger.info(f"Access token: {session['access_token'][:20]}...")
        
        try:
            thread = Thread(target=fetch_all_tracks, args=(session_id, session['access_token']))
            thread.daemon = True
            thread.start()
            app.logger.info("Background thread started successfully")
        except Exception as e:
            app.logger.error(f"Failed to start background thread: {str(e)}")
            update_loading_status(session_id, {
                'is_loading': False,
                'error': f"Failed to start loading: {str(e)}",
                'progress': 0,
                'track_count': 0,
                'current_playlist': ''
            })
    else:
        app.logger.info("Loading already in progress")
    
    return render_template('loading.html')

@app.route('/check_loading_status')
def check_loading_status():
    """Check the current loading status"""
    app.logger.info("Checking loading status...")
    
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
        session.modified = True
    
    session_id = session['session_id']
    app.logger.info(f"Checking status for session: {session_id}")
    
    status = loading_status.get(session_id, {
        'is_loading': True,
        'progress': 0,
        'track_count': 0,
        'current_playlist': 'Starting...',
        'error': None
    })
    
    app.logger.info(f"Current loading status: {status}")
    
    # If loading is complete, add tracks to session
    if status.get('is_loading') == False and status.get('track_count', 0) > 0:
        session['tracks'] = user_tracks.get(session_id, [])
        session.modified = True
    
    return jsonify(status)

@app.before_request
def make_session_permanent():
    session.permanent = True

@app.route('/')
def index():
    if 'access_token' in session:
        return redirect(url_for('home'))
    return render_template('index.html')

@app.route('/home')
def home():
    # Check if user is authenticated
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return redirect(url_for('index'))

    # Get Spotify client
    sp = get_spotify_client()
    if not sp:
        app.logger.error("Failed to get Spotify client")
        return redirect(url_for('index'))

    try:
        # Get user info
        user_info = sp.current_user()
        return render_template('home.html', username=user_info['display_name'])
    except Exception as e:
        app.logger.error(f"Error getting user info: {str(e)}")
        return redirect(url_for('index'))

@app.route('/get_random_song')
def get_random_song():
    if 'tracks' not in session:
        return render_template('home.html', random_track="No tracks available. Please try logging in again.", track_image_url=None, artist_name=None, playlist_name=None)

    all_tracks = session['tracks']
    if not all_tracks:
        return render_template('home.html', random_track="No tracks found. Please try again.", track_image_url=None, artist_name=None, playlist_name=None)
    
    random_track_info = random.choice(all_tracks)
    random_track_name = random_track_info['name']
    track_image_url = random_track_info.get('album', {}).get('images', [{}])[0].get('url')
    artist_name = random_track_info['artists'][0] if random_track_info['artists'] else 'Unknown Artist'
    playlist_name = random_track_info.get('playlist_name', 'Unknown Playlist')
    
    return render_template('home.html', random_track=random_track_name, track_image_url=track_image_url, artist_name=artist_name, playlist_name=playlist_name)

@app.route('/create')
def create_room():
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return redirect(url_for('index'))
    
    session_id = session.get('session_id')
    if not session_id:
        app.logger.error("No session ID")
        return redirect(url_for('index'))

    # Generate unique room code
    room_code = None
    while True:
        room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if room_code not in active_rooms:
            break
    
    # Create room
    active_rooms[room_code] = {
        'host': session_id,
        'users': {session_id},
        'created_at': time.time()
    }
    
    app.logger.info(f"Created room {room_code} for session {session_id}")
    return render_template('room.html', room_code=room_code)

@app.route('/join/<room_code>')
def join_room_route(room_code):
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return redirect(url_for('index'))
    
    session_id = session.get('session_id')
    if not session_id:
        app.logger.error("No session ID")
        return redirect(url_for('index'))
    
    if room_code not in active_rooms:
        app.logger.error(f"Room {room_code} not found")
        flash('Invalid room code')
        return redirect(url_for('home'))
    
    # Join room
    room = active_rooms[room_code]
    room['users'].add(session_id)
    
    app.logger.info(f"User {session_id} joined room {room_code}")
    return render_template('room.html', room_code=room_code)

@app.route('/room/<room_id>')
def room(room_id):
    if 'access_token' not in session:
        flash('Please log in first')
        return redirect(url_for('index'))
    
    app.logger.info(f"Accessing room: {room_id}")
    app.logger.info(f"Available rooms: {list(rooms.keys())}")
    
    if room_id not in rooms:
        app.logger.error(f"Room not found: {room_id}")
        flash('Room not found or has expired')
        return redirect(url_for('home'))

    # Check if room is full
    if len(rooms[room_id]['users']) >= 2 and session.get('session_id') not in [u['session_id'] for u in rooms[room_id]['users']]:
        flash('Room is full')
        return redirect(url_for('home'))
    
    app.logger.info(f"Successfully accessed room: {room_id}")
    return render_template('room.html', room_id=room_id)

@socketio.on('connect')
def on_connect():
    app.logger.info("Client connected")
    session_id = session.get('session_id')
    if session_id:
        app.logger.info(f"Client connected with session {session_id}")

@socketio.on('create_room')
def on_create_room():
    session_id = session.get('session_id')
    if not session_id:
        return {'error': 'Invalid session'}
    
    # Generate unique room code
    while True:
        room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if room_code not in active_rooms:
            break
    
    # Create room
    active_rooms[room_code] = {
        'host': session_id,
        'users': {session_id},
        'created_at': time.time()
    }
    
    # Join socket.io room
    join_room(room_code)
    
    app.logger.info(f"Created room {room_code} for session {session_id}")
    return {'room_code': room_code}

@socketio.on('join_room')
def on_join(data):
    room_code = data.get('room_code')
    session_id = session.get('session_id')
    
    if not session_id:
        return {'error': 'Invalid session'}
    
    if not room_code or room_code not in active_rooms:
        return {'error': 'Invalid room code'}
    
    # Join the room
    room = active_rooms[room_code]
    room['users'].add(session_id)
    join_room(room_code)
    
    # Notify others in room
    emit('user_joined', {'user_count': len(room['users'])}, room=room_code)
    
    app.logger.info(f"User {session_id} joined room {room_code}")
    return {'success': True, 'user_count': len(room['users'])}

@socketio.on('roll')
def on_roll(data):
    room_code = data.get('room_code')
    session_id = session.get('session_id')
    
    app.logger.info(f"Roll requested for room {room_code} by session {session_id}")
    
    if not session_id:
        return {'error': 'Invalid session'}
    
    if not room_code or room_code not in active_rooms:
        return {'error': 'Invalid room code'}
    
    room = active_rooms[room_code]
    
    # Check if we have enough users
    if len(room['users']) < 2:
        return {'error': 'Need two users to roll songs'}
    
    # Find shared tracks if not already computed
    if 'shared_tracks' not in room:
        app.logger.info("Computing shared tracks...")
        
        # Get track IDs for each user
        user_track_ids = {}
        user_track_playlists = {}
        
        for user_id in room['users']:
            if user_id in user_tracks:
                user_track_ids[user_id] = {track['id']: track for track in user_tracks[user_id]}
                user_track_playlists[user_id] = {track['id']: track.get('playlist_name', 'Unknown Playlist') for track in user_tracks[user_id]}
                app.logger.info(f"User {user_id} has {len(user_track_ids[user_id])} tracks")
            else:
                app.logger.error(f"No tracks found for user {user_id}")
                return {'error': 'Some users have not loaded their tracks yet'}
        
        # Find intersection of track IDs
        shared_track_ids = set.intersection(*[set(tracks.keys()) for tracks in user_track_ids.values()])
        app.logger.info(f"Found {len(shared_track_ids)} shared tracks")
        
        # Get full track info for shared tracks with playlist information
        shared_tracks = []
        for track_id in shared_track_ids:
            # Use the first user's track info as base
            first_user_id = list(user_track_ids.keys())[0]
            track = user_track_ids[first_user_id][track_id]
            
            # Collect playlists from all users
            track['playlists'] = {
                user_id: user_track_playlists[user_id][track_id] 
                for user_id in user_track_ids.keys()
            }
            shared_tracks.append(track)
        
        room['shared_tracks'] = shared_tracks
        app.logger.info(f"Stored {len(shared_tracks)} shared tracks in room")
    
    if not room['shared_tracks']:
        app.logger.error("No shared tracks found")
        return {'error': 'No shared tracks found between users'}
    
    # Select a random track
    track = random.choice(room['shared_tracks'])
    app.logger.info(f"Selected track: {track['name']} by {', '.join(track['artists'])}")
    
    # Remove the track so it won't be selected again
    room['shared_tracks'].remove(track)
    
    # Emit the track to all users in the room, including playlist information
    emit('song_rolled', {
        'song': {
            'name': track['name'],
            'artist': ', '.join(track['artists']),
            'playlists': ', '.join(track['playlists'].values())
        }
    }, room=room_code)
    
    return {'success': True}

@socketio.on('leave_room')
def on_leave(data):
    room_code = data.get('room_code')
    session_id = session.get('session_id')
    
    if room_code and room_code in active_rooms:
        room = active_rooms[room_code]
        if session_id in room['users']:
            room['users'].remove(session_id)
            leave_room(room_code)
            
            # If room is empty, remove it
            if not room['users']:
                del active_rooms[room_code]
            else:
                # Notify others
                emit('user_left', {'user_count': len(room['users'])}, room=room_code)
        
        app.logger.info(f"User {session_id} left room {room_code}")

@socketio.on('disconnect')
def on_disconnect():
    session_id = session.get('session_id')
    if session_id:
        # Remove user from all rooms they're in
        for room_code in list(active_rooms.keys()):
            room = active_rooms[room_code]
            if session_id in room['users']:
                room['users'].remove(session_id)
                if not room['users']:
                    del active_rooms[room_code]
                else:
                    emit('user_left', {'user_count': len(room['users'])}, room=room_code)

@app.route('/logout')
def logout():
    # Clear all session data
    session.clear()
    # Clear loading status for this session if it exists
    session_id = session.get('session_id')
    if session_id and session_id in loading_status:
        del loading_status[session_id]
    return redirect(url_for('index'))

def refresh_token():
    """Refresh the Spotify access token"""
    app.logger.info("Refreshing access token")
    try:
        auth_manager = get_spotify_oauth()
        if 'refresh_token' in session:
            token_info = auth_manager.refresh_access_token(session['refresh_token'])
            session['access_token'] = token_info['access_token']
            session['token_expiry'] = time.time() + token_info['expires_in']
            session.modified = True
            app.logger.info("Successfully refreshed access token")
            return True
    except Exception as e:
        app.logger.error(f"Error refreshing token: {str(e)}")
        return False

def get_spotify_client():
    """Get a valid Spotify client, refreshing token if necessary"""
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return None
        
    # Check if token needs refresh
    if time.time() > session.get('token_expiry', 0):
        app.logger.info("Token expired, refreshing...")
        if not refresh_token():
            app.logger.error("Failed to refresh token")
            return None
            
    try:
        sp = spotipy.Spotify(auth=session['access_token'])
        sp.current_user()  # Test the connection
        return sp
    except Exception as e:
        app.logger.error(f"Error creating Spotify client: {str(e)}")
        return None

if __name__ == '__main__':
    import socket
    port = 5000
    
    # Configure Flask logger to use the same format
    app.logger.handlers = []
    for handler in logging.getLogger().handlers:
        app.logger.addHandler(handler)
    
    socketio.run(app, host='127.0.0.1', port=port, debug=True, use_reloader=False)
