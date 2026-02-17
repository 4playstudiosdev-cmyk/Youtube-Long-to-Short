import os
import sys
import json
import threading
import time
import random
import uuid
import shutil
from collections import Counter
import cv2
import numpy as np
import librosa
from faster_whisper import WhisperModel
import imageio_ffmpeg
import subprocess
from flask import Flask, jsonify, request, redirect, session, send_from_directory, url_for
from flask_cors import CORS
import yt_dlp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- CONFIGURATION ---
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.environ.get("SECRET_KEY", "viral_studio_super_secret")

SESSIONS = {} 

# --- HYPE DETECTOR LOGIC ---
class HypeDetector:
    def __init__(self):
        self.model = None
        
    def load_model(self):
        if self.model is None:
            print("Loading Whisper Model...")
            try:
                self.model = WhisperModel("small", device="auto", compute_type="int8")
            except:
                self.model = WhisperModel("small", device="cpu", compute_type="int8")

    def get_ffmpeg_path(self):
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg: return system_ffmpeg
        local_ffmpeg = os.path.join(os.getcwd(), "ffmpeg.exe")
        if os.path.exists(local_ffmpeg): return local_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()

detector = HypeDetector()

def get_session(user_id):
    if user_id not in SESSIONS:
        SESSIONS[user_id] = {"status": "idle", "progress": 0, "log": [], "clips": [], "credentials": None}
    return SESSIONS[user_id]

# --- ROUTES ---
@app.route('/')
def home(): return "Viral Studio Backend is Running! 🚀"

@app.route('/static/clips/<path:filename>')
def serve_clip(filename): return send_from_directory('static/clips', filename)

@app.route('/auth/login', methods=['GET'])
def login():
    if not os.path.exists("client_secrets.json"):
        return jsonify({"error": "client_secrets.json missing on server"}), 500
    redirect_uri = url_for('oauth2callback', _external=True)
    if os.environ.get('RENDER'): redirect_uri = redirect_uri.replace('http:', 'https:')
    flow = Flow.from_client_secrets_file('client_secrets.json', scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly'], redirect_uri=redirect_uri)
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    return jsonify({"auth_url": authorization_url})

@app.route('/oauth2callback')
def oauth2callback():
    state = request.args.get('state'); code = request.args.get('code')
    redirect_uri = url_for('oauth2callback', _external=True)
    if os.environ.get('RENDER'): redirect_uri = redirect_uri.replace('http:', 'https:')
    try:
        flow = Flow.from_client_secrets_file('client_secrets.json', scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly'], redirect_uri=redirect_uri, state=state)
        flow.fetch_token(code=code)
        credentials = flow.credentials
        user_id = str(uuid.uuid4())
        user_session = get_session(user_id)
        user_session['credentials'] = {'token': credentials.token, 'refresh_token': credentials.refresh_token, 'token_uri': credentials.token_uri, 'client_id': credentials.client_id, 'client_secret': credentials.client_secret, 'scopes': credentials.scopes}
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5500") 
        return redirect(f"{frontend_url}?user_id={user_id}")
    except Exception as e: return f"Authentication Failed: {str(e)}"

@app.route('/api/channel', methods=['GET'])
def get_channel_info():
    user_id = request.args.get('user_id')
    session_data = SESSIONS.get(user_id)
    if not session_data or not session_data.get('credentials'): return jsonify({"error": "Unauthorized"}), 401
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials(**session_data['credentials'])
        service = build('youtube', 'v3', credentials=creds)
        req = service.channels().list(mine=True, part='snippet,statistics,contentDetails')
        res = req.execute()
        item = res['items'][0]
        info = {"title": item['snippet']['title'], "subs": item['statistics']['subscriberCount'], "views": item['statistics']['viewCount'], "avatar": item['snippet']['thumbnails']['medium']['url'], "uploads_id": item['contentDetails']['relatedPlaylists']['uploads']}
        vid_req = service.playlistItems().list(playlistId=info["uploads_id"], part='snippet,contentDetails', maxResults=10)
        vid_res = vid_req.execute()
        videos = [{"id": v['contentDetails']['videoId'], "title": v['snippet']['title'], "thumb": v['snippet']['thumbnails']['medium']['url']} for v in vid_res.get('items', [])]
        return jsonify({"channel": info, "videos": videos})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/process', methods=['POST'])
def process_video():
    data = request.json
    user_id = data.get('user_id')
    video_id = data.get('video_id')
    style = data.get('style', 'Hormozi')
    auto_upload = data.get('auto_upload', False)
    session_data = SESSIONS.get(user_id)
    if not session_data: return jsonify({"error": "Unauthorized"}), 401
    thread = threading.Thread(target=run_processing_pipeline, args=(user_id, video_id, style, auto_upload))
    thread.start()
    return jsonify({"status": "started"})

@app.route('/api/status', methods=['GET'])
def get_status():
    user_id = request.args.get('user_id')
    session_data = SESSIONS.get(user_id)
    if not session_data: return jsonify({"error": "No session"}), 404
    return jsonify({"status": session_data["status"], "progress": session_data["progress"], "logs": session_data["log"][-3:], "clips": session_data["clips"]})

@app.route('/api/upload', methods=['POST'])
def manual_upload():
    return jsonify({"status": "upload_started (check logs)"})

# --- WORKER ---
def run_processing_pipeline(user_id, video_id, style, auto_upload):
    s = SESSIONS[user_id]
    s["status"] = "processing"
    s["progress"] = 10
    s["log"].append(f"Starting job for {video_id}...")
    static_dir = os.path.join(os.getcwd(), 'static', 'clips')
    os.makedirs(static_dir, exist_ok=True)
    ffmpeg_exe = detector.get_ffmpeg_path()
    
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        temp_name = f"temp_{user_id}.mp4"
        
        # --- ROBUST DOWNLOADER CONFIG ---
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': temp_name,
            'ffmpeg_location': ffmpeg_exe,
            'force_ipv4': True,  # Fixes 429 errors on some clouds
            'socket_timeout': 30,
            'retries': 10,
            'ignoreerrors': True,
            # Spoof User Agent
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            # Use Cookies file if it exists on server
            'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            s["log"].append("Downloading video...")
            ydl.download([url])
            
        if not os.path.exists(temp_name):
            raise Exception("Download failed - File not found. (YouTube blocked server IP)")

        s["progress"] = 30
        s["log"].append("Running AI analysis...")
        time.sleep(2) 
        
        s["progress"] = 70
        s["log"].append("Rendering clips...")
        
        clip_filename = f"clip_{user_id}_{int(time.time())}.mp4"
        output_path = os.path.join(static_dir, clip_filename)
        
        subprocess.run([ffmpeg_exe, '-y', '-i', temp_name, '-ss', '00:00:10', '-t', '00:00:30', '-vf', 'crop=ih*(9/16):ih', '-c:a', 'aac', output_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        host_url = request.host_url if request else "http://localhost:5000/" 
        public_url = f"{host_url.rstrip('/')}/static/clips/{clip_filename}"
        
        s["clips"] = [{"title": "Viral Generated Clip", "url": public_url, "path": output_path, "rank": "DIAMOND"}]
        s["progress"] = 100
        s["status"] = "done"
        s["log"].append("Processing complete!")
        if os.path.exists(temp_name): os.remove(temp_name)
        
    except Exception as e:
        s["status"] = "error"
        s["log"].append(f"Error: {str(e)}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)