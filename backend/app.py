import os
import sys
import json
import threading
import time
import random
import uuid
import shutil
import traceback
import subprocess
from flask import Flask, jsonify, request, redirect, session, send_from_directory, url_for
from flask_cors import CORS
import yt_dlp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from faster_whisper import WhisperModel
import imageio_ffmpeg
import numpy as np

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.environ.get("SECRET_KEY", "viral_studio_super_secret")

SESSIONS = {} 

class HypeDetector:
    def __init__(self):
        self.model = None
        
    def load_model(self):
        if self.model is None:
            print("Loading Whisper Model...")
            try:
                self.model = WhisperModel("tiny", device="auto", compute_type="int8")
            except:
                self.model = WhisperModel("tiny", device="cpu", compute_type="int8")

    def get_ffmpeg_path(self):
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg: return system_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()

    def transcribe(self, audio_path):
        self.load_model()
        segments, _ = self.model.transcribe(audio_path, beam_size=5)
        return list(segments)

detector = HypeDetector()

def get_session(user_id):
    if user_id not in SESSIONS:
        SESSIONS[user_id] = {"status": "idle", "progress": 0, "log": [], "clips": [], "credentials": None}
    return SESSIONS[user_id]

# --- ROUTES ---
@app.route('/')
def home(): return "Viral Studio Engine (Robust Download Mode) Active 🚀"

@app.route('/static/clips/<path:filename>')
def serve_clip(filename): return send_from_directory('static/clips', filename)

@app.route('/auth/login', methods=['GET'])
def login():
    if not os.path.exists("client_secrets.json"): return jsonify({"error": "client_secrets.json missing"}), 500
    redirect_uri = url_for('oauth2callback', _external=True)
    if os.environ.get('RENDER'): redirect_uri = redirect_uri.replace('http:', 'https:')
    flow = Flow.from_client_secrets_file('client_secrets.json', scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly'], redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(access_type='offline', include_granted_scopes='true')
    return jsonify({"auth_url": auth_url})

@app.route('/oauth2callback')
def oauth2callback():
    state = request.args.get('state'); code = request.args.get('code')
    redirect_uri = url_for('oauth2callback', _external=True)
    if os.environ.get('RENDER'): redirect_uri = redirect_uri.replace('http:', 'https:')
    try:
        flow = Flow.from_client_secrets_file('client_secrets.json', scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly'], redirect_uri=redirect_uri, state=state)
        flow.fetch_token(code=code)
        user_id = str(uuid.uuid4())
        s = get_session(user_id)
        creds = flow.credentials
        s['credentials'] = {'token': creds.token, 'refresh_token': creds.refresh_token, 'token_uri': creds.token_uri, 'client_id': creds.client_id, 'client_secret': creds.client_secret, 'scopes': creds.scopes}
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5500") 
        return redirect(f"{frontend_url}?user_id={user_id}")
    except Exception as e: return f"Auth Failed: {str(e)}"

@app.route('/api/channel', methods=['GET'])
def get_channel_info():
    user_id = request.args.get('user_id')
    s = SESSIONS.get(user_id)
    if not s or not s.get('credentials'): return jsonify({"error": "Unauthorized"}), 401
    try:
        from google.oauth2.credentials import Credentials
        service = build('youtube', 'v3', credentials=Credentials(**s['credentials']))
        res = service.channels().list(mine=True, part='snippet,statistics,contentDetails').execute()
        item = res['items'][0]
        info = {"title": item['snippet']['title'], "subs": item['statistics']['subscriberCount'], "views": item['statistics']['viewCount'], "avatar": item['snippet']['thumbnails']['medium']['url'], "uploads_id": item['contentDetails']['relatedPlaylists']['uploads']}
        vid_res = service.playlistItems().list(playlistId=info["uploads_id"], part='snippet,contentDetails', maxResults=10).execute()
        videos = [{"id": v['contentDetails']['videoId'], "title": v['snippet']['title'], "thumb": v['snippet']['thumbnails']['medium']['url']} for v in vid_res.get('items', [])]
        return jsonify({"channel": info, "videos": videos})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/process', methods=['POST'])
def process_video():
    data = request.json
    user_id = data.get('user_id')
    s = SESSIONS.get(user_id)
    if not s: return jsonify({"error": "Unauthorized"}), 401
    thread = threading.Thread(target=run_robust_pipeline, args=(user_id, data.get('video_id'), data.get('auto_upload')))
    thread.start()
    return jsonify({"status": "started"})

@app.route('/api/status', methods=['GET'])
def get_status():
    user_id = request.args.get('user_id')
    s = SESSIONS.get(user_id)
    if not s: return jsonify({"error": "No session"}), 404
    return jsonify({"status": s["status"], "progress": s["progress"], "logs": s["log"][-5:], "clips": s["clips"]})

@app.route('/api/upload', methods=['POST'])
def manual_upload():
    # Placeholder for manual trigger if needed, currently handled in pipeline
    return jsonify({"status": "upload_started"})

# --- WORKER ---
def run_robust_pipeline(user_id, video_id, auto_upload):
    s = SESSIONS[user_id]
    s["status"] = "processing"
    s["progress"] = 5
    s["log"].append("Starting Robust Engine...")
    
    static_dir = os.path.join(os.getcwd(), 'static', 'clips')
    os.makedirs(static_dir, exist_ok=True)
    ffmpeg_exe = detector.get_ffmpeg_path()
    
    temp_name = f"temp_{user_id}.mp4"
    audio_name = f"audio_{user_id}.wav"
    
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        # 1. ROBUST DOWNLOAD (Using TV Client + Cookies)
        s["log"].append("Downloading Source (TV Client)...")
        ydl_opts = {
            'format': 'best[ext=mp4]/best', # Get best single file format
            'outtmpl': temp_name,
            'ffmpeg_location': ffmpeg_exe,
            'force_ipv4': True,
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['tv']}}, # Force TV Client
            'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
            'socket_timeout': 60,
            'retries': 10
        }
        
        if os.path.exists(temp_name): os.remove(temp_name)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            s["log"].append("TV Client Failed. Trying Web Client...")
            # Fallback to Web
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['web']}}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        if not os.path.exists(temp_name):
            raise Exception("Download failed. YouTube blocked IP or cookies invalid.")
        
        # 2. EXTRACT AUDIO FOR AI
        s["progress"] = 40
        s["log"].append("Extracting Audio...")
        subprocess.run([ffmpeg_exe, '-y', '-i', temp_name, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', audio_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 3. REAL AI ANALYSIS
        s["log"].append("AI Analyzing Content...")
        segments = detector.transcribe(audio_name)
        
        # Simple Viral Detector: Look for high-energy keywords
        hype_keywords = ["omg", "wow", "insane", "what", "crazy", "lol", "god", "stop", "win", "fail"]
        best_segment = None
        
        # Scan for keywords
        for seg in segments:
            text = seg.text.lower()
            if any(k in text for k in hype_keywords):
                best_segment = seg
                break
        
        # Fallback: Just take a segment from the middle if no keywords found
        if not best_segment:
            start_time = 60 if len(segments) > 0 else 0
        else:
            start_time = best_segment.start

        # 4. CUT CLIP
        s["progress"] = 70
        s["log"].append(f"Cutting Clip at {int(start_time)}s...")
        
        clip_filename = f"clip_{user_id}_{int(time.time())}.mp4"
        output_path = os.path.join(static_dir, clip_filename)
        
        # Robust Crop
        subprocess.run([
            ffmpeg_exe, '-y', 
            '-ss', str(start_time), '-t', '60', # 60s Clip
            '-i', temp_name, 
            '-vf', 'crop=ih*(9/16):ih', # Vertical Crop
            '-c:v', 'libx264', '-c:a', 'aac', 
            output_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 5. AUTO UPLOAD
        public_url = f"{request.host_url.rstrip('/')}/static/clips/{clip_filename}"
        clip_data = {"title": "Viral Clip AI", "url": public_url, "path": output_path, "rank": "DIAMOND"}
        
        if auto_upload:
            s["log"].append("Auto-Uploading to YouTube...")
            try:
                upload_to_youtube(user_id, output_path, "Viral Clip #shorts", "Generated by ViralStudio #shorts")
                s["log"].append("Upload Successful!")
            except Exception as ue:
                s["log"].append(f"Upload Failed: {str(ue)}")

        s["clips"] = [clip_data]
        s["progress"] = 100
        s["status"] = "done"
        s["log"].append("Success!")
        
        # Cleanup
        if os.path.exists(temp_name): os.remove(temp_name)
        if os.path.exists(audio_name): os.remove(audio_name)

    except Exception as e:
        s["status"] = "error"
        s["log"].append(f"FATAL: {str(e)}")
        print(traceback.format_exc())

def upload_to_youtube(user_id, path, title, desc):
    s = SESSIONS[user_id]
    from google.oauth2.credentials import Credentials
    creds = Credentials(**s['credentials'])
    service = build('youtube', 'v3', credentials=creds)
    
    body = {
        'snippet': {
            'title': title,
            'description': desc,
            'tags': ['shorts', 'viral'],
            'categoryId': '22'
        },
        'status': {
            'privacyStatus': 'public', # Change to 'private' for testing
            'selfDeclaredMadeForKids': False
        }
    }
    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    request = service.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)