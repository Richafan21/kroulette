from flask import Flask, redirect, url_for, session, request, render_template, jsonify, make_response, flash
import requests
import random
import string
from urllib.parse import urlencode
from threading import Thread
from flask_socketio import SocketIO, emit, join_room, leave_room
import uuid
import logging
import time
from datetime import timedelta
import gc
from collections import defaultdict
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = Flask(__name__)
app.secret_key = 'FzoY?LYL5moT:Iex"m18/0.pa!K-wG'
socketio = SocketIO(app)

# Configure Flask session to be more robust
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)

# Spotify API credentials and settings
CLIENT_ID = '04703f4623b846f1ae4202c56e9424ff'
CLIENT_SECRET = 'ae11f2cacd8c403ebdaa7f221f8cd062'
SCOPE = 'user-library-read playlist-read-private playlist-read-collaborative user-read-private user-read-email'
REDIRECT_URI = 'http://127.0.0.1:5000/callback'

# Initialize Spotify OAuth
sp_oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=SCOPE,
    cache_path=None,  # Don't cache tokens to file
    show_dialog=True  # Always show the Spotify login dialog
)

# Global variables
port = 5000  # Default port
loading_status = {}  # Track loading status and progress
rooms = {}  # Store room information: {room_id: {'users': [], 'shared_tracks': [], 'used_tracks': [], 'created_at': timestamp}}
active_rooms = {}  # Store room info: {room_code: {'host': session_id, 'users': set(), 'created_at': timestamp}}

def cleanup_old_rooms():
    """Remove rooms that are older than 30 minutes"""
    current_time = time.time()
    to_remove = []
    for room_id, room_data in rooms.items():
        if current_time - room_data.get('created_at', 0) > 1800:  # 30 minutes
            to_remove.append(room_id)
    for room_id in to_remove:
        del rooms[room_id]

def get_redirect_uri(port):
    return f'http://127.0.0.1:{port}/callback'

def generate_session_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

def generate_room_code():
    """Generate a 6-character room code"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_random_string():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

def fetch_all_tracks(session_id, session_data):
    app.logger.info(f"=== Starting fetch_all_tracks for session {session_id} ===")
    
    if not session_id:
        app.logger.error("No session ID for fetch_all_tracks")
        return

    with app.app_context():
        try:
            # Create Spotify client
            access_token = session_data.get('access_token')
            if not access_token:
                app.logger.error("No access token in session data")
                with app.test_request_context():
                    session['loading_status'] = {
                        'is_loading': False,
                        'error': 'No access token found. Please log in again.'
                    }
                    session.modified = True
                return

            app.logger.info("Creating Spotify client")
            sp = spotipy.Spotify(auth=access_token)
            
            # Test the client
            try:
                current_user = sp.current_user()
                app.logger.info(f"Successfully connected to Spotify as {current_user['id']}")
            except Exception as e:
                app.logger.error(f"Failed to connect to Spotify: {str(e)}")
                with app.test_request_context():
                    session['loading_status'] = {
                        'is_loading': False,
                        'error': 'Failed to connect to Spotify. Please try again.'
                    }
                    session.modified = True
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
                except Exception as e:
                    app.logger.error(f"Error fetching playlists: {str(e)}")
                    with app.test_request_context():
                        session['loading_status'] = {
                            'is_loading': False,
                            'error': 'Failed to fetch playlists. Please try again.'
                        }
                        session.modified = True
                    return
            
            app.logger.info(f"Found {len(playlists)} playlists")
            
            tracks = []
            track_ids = set()
            
            # Process each playlist
            for i, playlist in enumerate(playlists):
                try:
                    playlist_name = playlist['name']
                    app.logger.info(f"Processing playlist {i+1}/{len(playlists)}: {playlist_name}")
                    
                    with app.test_request_context():
                        session['loading_status'] = {
                            'is_loading': True,
                            'progress': int((i / len(playlists)) * 100),
                            'track_count': len(tracks),
                            'current_playlist': playlist_name
                        }
                        session.modified = True
                    
                    # Get tracks from playlist
                    playlist_tracks = []
                    offset = 0
                    while True:
                        try:
                            response = sp.playlist_tracks(playlist['id'], offset=offset, limit=100)
                            playlist_tracks.extend(response['items'])
                            if len(response['items']) < 100:
                                break
                            offset += 100
                        except Exception as e:
                            app.logger.error(f"Error fetching tracks for playlist {playlist_name}: {str(e)}")
                            continue
                    
                    # Process tracks
                    for item in playlist_tracks:
                        if item and item.get('track'):
                            track = item['track']
                            if track and track.get('id') and track['id'] not in track_ids:
                                track_ids.add(track['id'])
                                tracks.append({
                                    'id': track['id'],
                                    'name': track['name'],
                                    'artist': ', '.join(artist['name'] for artist in track['artists']),
                                    'playlist': playlist_name
                                })
                
                except Exception as e:
                    app.logger.error(f"Error processing playlist {playlist_name}: {str(e)}")
                    continue
            
            app.logger.info(f"Found {len(tracks)} total tracks")
            
            # Store everything in session
            with app.test_request_context():
                # Store tracks in chunks
                chunk_size = 1000
                chunks = [tracks[i:i + chunk_size] for i in range(0, len(tracks), chunk_size)]
                session['track_chunk_count'] = len(chunks)
                for i, chunk in enumerate(chunks):
                    session[f'user_tracks_chunk_{i}'] = chunk
                
                # Update final status
                session['loading_status'] = {
                    'is_loading': False,
                    'progress': 100,
                    'track_count': len(tracks),
                    'current_playlist': 'Done!'
                }
                session.modified = True
            
            app.logger.info(f"Successfully loaded {len(tracks)} tracks for session {session_id}")
            
        except Exception as e:
            app.logger.error(f"Error in fetch_all_tracks: {str(e)}")
            with app.test_request_context():
                session['loading_status'] = {
                    'error': 'Error loading tracks. Please try again.',
                    'is_loading': False
                }
                session.modified = True

@app.before_request
def make_session_permanent():
    session.permanent = True

@app.route('/')
def index():
    if 'access_token' in session:
        return redirect(url_for('home'))
    return render_template('index.html')

@app.route('/login')
def login():
    # Clear any existing session data
    session.clear()
    
    # Get authorization URL
    auth_url = sp_oauth.get_authorize_url()
    app.logger.info("Generated Spotify authorization URL")
    
    return redirect(auth_url)

@app.route('/callback')
def callback():
    # Get authorization code
    code = request.args.get('code')
    if not code:
        app.logger.error("No code in callback")
        return redirect(url_for('index'))

    try:
        # Exchange code for access token
        token_info = sp_oauth.get_access_token(code)
        if not token_info:
            app.logger.error("Failed to get token info")
            return redirect(url_for('index'))

        # Store tokens in session
        session['access_token'] = token_info['access_token']
        session['refresh_token'] = token_info['refresh_token']
        session['token_expiry'] = time.time() + token_info['expires_in']

        # Generate a unique session ID if not exists
        if 'session_id' not in session:
            session['session_id'] = str(uuid.uuid4())

        # Initialize loading status
        app.logger.info("Initializing loading status")
        session['loading_status'] = {
            'is_loading': True,
            'progress': 0,
            'track_count': 0,
            'current_playlist': '',
            'error': None
        }

        app.logger.info(f"Successfully got access token for session {session['session_id']}")
        return redirect(url_for('loading'))

    except Exception as e:
        app.logger.error(f"Error in callback: {str(e)}")
        return redirect(url_for('index'))

@app.route('/loading')
def loading():
    app.logger.info(f"Loading route - Session ID: {session.get('session_id')}")
    app.logger.info(f"Session contents: {dict(session)}")
    
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return redirect(url_for('index'))
    
    # Start loading tracks if not already started
    if 'loading_status' not in session:
        app.logger.info("Initializing loading status")
        session['loading_status'] = {
            'is_loading': True,
            'progress': 0,
            'track_count': 0,
            'current_playlist': 'Starting...',
            'error': None
        }
        session.modified = True
        
        # Start loading tracks in background
        session_id = session.get('session_id')
        session_data = dict(session)
        app.logger.info(f"Starting background thread for session {session_id}")
        app.logger.info(f"Access token: {session_data.get('access_token')[:20]}...")
        
        thread = Thread(target=fetch_all_tracks, args=(session_id, session_data))
        thread.daemon = True
        thread.start()
        app.logger.info("Background thread started")
    
    return render_template('loading.html')

@app.route('/check_loading_status')
def check_loading_status():
    app.logger.info("Checking loading status...")
    app.logger.info(f"Session contents: {dict(session)}")
    
    if 'loading_status' not in session:
        app.logger.error("No loading status in session")
        return jsonify({
            'is_loading': False,
            'error': 'Session expired or invalid'
        })
    
    status = session.get('loading_status', {})
    app.logger.info(f"Current loading status: {status}")
    return jsonify(status)

def get_spotify_client():
    # Check if token exists and is valid
    if 'access_token' not in session:
        app.logger.error("No access token in session")
        return None

    # Check if token is expired
    if time.time() > session.get('token_expiry', 0):
        # Try to refresh token
        if 'refresh_token' not in session:
            app.logger.error("No refresh token in session")
            return None

        try:
            token_info = sp_oauth.refresh_access_token(session['refresh_token'])
            session['access_token'] = token_info['access_token']
            session['token_expiry'] = time.time() + token_info['expires_in']
        except Exception as e:
            app.logger.error(f"Error refreshing token: {str(e)}")
            return None

    # Create and return Spotify client
    try:
        return spotipy.Spotify(auth=session['access_token'])
    except Exception as e:
        app.logger.error(f"Error creating Spotify client: {str(e)}")
        return None

@app.route('/logout')
def logout():
    # Clear all session data
    session.clear()
    # Clear loading status for this session if it exists
    session_id = session.get('session_id')
    if session_id and session_id in loading_status:
        del loading_status[session_id]
    return redirect(url_for('index'))

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
    playlist_name = random_track_info.get('playlist', 'Unknown Playlist')
    
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
    
    cleanup_old_rooms()  # Clean up old rooms
    
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
    
    if not session_id:
        return {'error': 'Invalid session'}
    
    if not room_code or room_code not in active_rooms:
        return {'error': 'Invalid room code'}
    
    room = active_rooms[room_code]
    
    # Check if we have enough users
    if len(room['users']) < 2:
        return {'error': 'Need two users to roll songs'}
    
    # Get tracks for both users
    user_tracks = {}
    for user_id in room['users']:
        # Reconstruct tracks from chunks
        tracks = []
        chunk_count = session.get('track_chunk_count', 0)
        for i in range(chunk_count):
            chunk = session.get(f'user_tracks_chunk_{i}', [])
            tracks.extend(chunk)
        user_tracks[user_id] = tracks
    
    # Find shared tracks
    if 'shared_tracks' not in room:
        # Get track IDs for each user
        user_track_ids = {
            user_id: {track['id'] for track in tracks}
            for user_id, tracks in user_tracks.items()
        }
        
        # Find intersection of track IDs
        shared_track_ids = set.intersection(*user_track_ids.values())
        
        # Get full track info for shared tracks
        shared_tracks = []
        for track in user_tracks[session_id]:  # Use current user's track info
            if track['id'] in shared_track_ids:
                shared_tracks.append(track)
        
        room['shared_tracks'] = shared_tracks
        
    if not room['shared_tracks']:
        return {'error': 'No shared tracks found'}
    
    # Select a random track
    track = random.choice(room['shared_tracks'])
    
    # Remove the track so it won't be selected again
    room['shared_tracks'].remove(track)
    
    # Emit the track to all users in the room
    emit('song_rolled', {'song': track}, room=room_code)
    
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
    
if __name__ == '__main__':
    import socket
    logging.basicConfig(level=logging.INFO)
    port = 5000
    app.logger.info(f"Starting server on http://127.0.0.1:{port}")
    socketio.run(app, host='127.0.0.1', port=port, debug=True)
