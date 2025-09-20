try:
    import ujson as json
except ImportError:
    import json

import os
import uuid
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta
import requests
import hashlib
from flask_cors import CORS
import functools
import traceback
import time
import concurrent.futures
# Add these imports for performance improvements
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import gzip
# New imports for Redis and scheduling
import redis
from apscheduler.schedulers.background import BackgroundScheduler
# PostgreSQL imports
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
# File upload imports
from werkzeug.utils import secure_filename
from PIL import Image
import io

app = Flask(__name__)
CORS(app)  # This is all you need for CORS handling

# Configuration
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', './uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

# Create upload directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'eblans'), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'lectures'), exist_ok=True)

# PostgreSQL Configuration
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'database': os.environ.get('DB_NAME', 'schedparse_db'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'password')
}

# Create connection pool
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        **DB_CONFIG
    )
    print("PostgreSQL connection pool created successfully")
except Exception as e:
    print(f"Error creating PostgreSQL connection pool: {e}")
    db_pool = None

@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    if not db_pool:
        raise Exception("Database pool not available")
    
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

def update_eblan_info_from_api(eblan_id, eblan_name):
    """Update eblan info in database from schedule API data."""
    if not db_pool:
        return
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO eblans (eblan_id, eblan_fio, updated_at) 
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (eblan_id) 
                    DO UPDATE SET 
                        eblan_fio = COALESCE(eblans.eblan_fio, EXCLUDED.eblan_fio),
                        updated_at = CURRENT_TIMESTAMP
                """, (eblan_id, eblan_name))
                conn.commit()
                print(f"Updated eblan info for {eblan_id}: {eblan_name}")
    except Exception as e:
        print(f"Error updating eblan info: {e}")

def init_database():
    """Initialize database tables."""
    if not db_pool:
        print("Skipping database initialization - no connection pool")
        return
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Create eblans table for storing lecturer info and ratings
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS eblans (
                        eblan_id INTEGER PRIMARY KEY,
                        eblan_fio VARCHAR(255),
                        eblan_img VARCHAR(500),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create comments table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS eblan_comments (
                        id SERIAL PRIMARY KEY,
                        eblan_id INTEGER NOT NULL,
                        rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                        comment TEXT,
                        features TEXT[], -- Array of feature strings
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ip_address INET,
                        FOREIGN KEY (eblan_id) REFERENCES eblans(eblan_id) ON DELETE CASCADE
                    )
                """)
                
                # Create lecture images table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS lecture_images (
                        id SERIAL PRIMARY KEY,
                        eblan_id INTEGER NOT NULL,
                        lect_string VARCHAR(100) NOT NULL,
                        image_path VARCHAR(500) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ip_address INET,
                        FOREIGN KEY (eblan_id) REFERENCES eblans(eblan_id) ON DELETE CASCADE
                    )
                """)
                
                # Create indexes for better performance
                cur.execute("CREATE INDEX IF NOT EXISTS idx_eblan_comments_eblan_id ON eblan_comments(eblan_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_lecture_images_eblan_lect ON lecture_images(eblan_id, lect_string)")
                
                conn.commit()
                print("Database tables initialized successfully")
    except Exception as e:
        print(f"Error initializing database: {e}")

def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def process_and_save_image(file_data, filename, subfolder):
    """Process and save image with compression and resizing."""
    try:
        # Open image
        image = Image.open(io.BytesIO(file_data))
        
        # Convert to RGB if necessary
        if image.mode in ('RGBA', 'P'):
            image = image.convert('RGB')
        
        # Resize if too large (max 1920x1080)
        max_width, max_height = 1920, 1080
        if image.width > max_width or image.height > max_height:
            image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        
        # Generate unique filename
        file_ext = 'jpg'  # Always save as JPEG for consistency
        unique_filename = f"{uuid.uuid4().hex}.{file_ext}"
        file_path = os.path.join(UPLOAD_FOLDER, subfolder, unique_filename)
        
        # Save with compression
        image.save(file_path, 'JPEG', quality=85, optimize=True)
        
        return f"/images/{subfolder}/{unique_filename}"
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

@app.after_request
def add_compression(response):
    """Compress response data with gzip for supported clients."""
    accept_encoding = request.headers.get('Accept-Encoding', '')
    
    if 'gzip' not in accept_encoding.lower():
        return response
    
    if (response.status_code < 200 or response.status_code >= 300 or
            'Content-Encoding' in response.headers):
        return response
    
    response.data = gzip.compress(response.data)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Vary'] = 'Accept-Encoding'
    response.headers['Content-Length'] = len(response.data)
    
    return response

# Serve uploaded images
@app.route('/images/<subfolder>/<filename>')
def serve_image(subfolder, filename):
    """Serve uploaded images."""
    if subfolder not in ['eblans', 'lectures']:
        return jsonify({"error": "Invalid subfolder"}), 404
        
    return send_from_directory(
        os.path.join(UPLOAD_FOLDER, subfolder), 
        filename,
        as_attachment=False
    )

# Configure requests to use connection pooling and retries
session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=0.1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

# Configure Redis connection
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

# Redis-based cache implementation (preserves SimpleCache interface)
class RedisCache:
    def __init__(self, prefix, default_ttl=3600):  # Default TTL: 1 hour
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.hits = 0
        self.misses = 0
    
    def _make_key(self, key):
        return f"{self.prefix}:{key}"
        
    def set(self, key, value, ttl=None):
        if ttl is None:
            ttl = self.default_ttl
        redis_key = self._make_key(key)
        
        # Enhanced logging for cache storage
        if value is None:
            print(f"WARNING: Attempting to cache None value for key {redis_key}")
            return
            
        # Log information about cached data
        data_type = type(value).__name__
        if isinstance(value, list):
            data_length = len(value)
            print(f"Caching {data_type} with {data_length} items for key {redis_key} (TTL: {ttl}s)")
            if data_length == 0:
                print(f"WARNING: Caching empty list for key {redis_key}")
        else:
            print(f"Caching {data_type} for key {redis_key} (TTL: {ttl}s)")
            
        redis_client.setex(redis_key, ttl, json.dumps(value))
    
    def get(self, key):
        redis_key = self._make_key(key)
        data = redis_client.get(redis_key)
        if data is None:
            self.misses += 1
            print(f"Cache MISS for key {redis_key}")
            return None
            
        self.hits += 1
        try:
            parsed_data = json.loads(data)
            data_type = type(parsed_data).__name__
            if isinstance(parsed_data, list):
                print(f"Cache HIT for key {redis_key}: {data_type} with {len(parsed_data)} items")
                if len(parsed_data) == 0:
                    print(f"WARNING: Retrieved empty list from cache for key {redis_key}")
            else:
                print(f"Cache HIT for key {redis_key}: {data_type}")
            return parsed_data
        except json.JSONDecodeError:
            print(f"ERROR: Invalid JSON in cache for key {redis_key}")
            self.misses += 1  # Adjust counter since this is effectively a miss
            return None

    def clear(self):
        keys = redis_client.keys(f"{self.prefix}:*")
        if keys:
            redis_client.delete(*keys)
        self.hits = 0
        self.misses = 0

    def stats(self):
        """Return cache statistics."""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits, 
            "misses": self.misses,
            "hit_rate": f"{hit_rate:.2f}%",
            "total_requests": total
        }
    
    def prune(self):
        """Redis automatically handles expiry, no manual pruning needed."""
        pass

# Initialize cache instances
schedule_cache = RedisCache(prefix="schedule", default_ttl=1800)  # 30 minutes for schedule data
search_cache = RedisCache(prefix="search", default_ttl=600)     # 10 minutes for search results
filter_cache = RedisCache(prefix="filter", default_ttl=1800)    # 30 minutes for filter options

# NEW RATING ENDPOINTS

@app.route('/api/getEblanRating', methods=['GET', 'POST'])
def get_eblan_rating():
    """Get lecturer rating and info."""
    try:
        if request.method == 'GET':
            eblan_id = request.args.get('eblanId')
            lect_string = request.args.get('lectString', '')
        else:  # POST
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
            data = request.get_json(silent=True) or {}
            eblan_id = data.get('eblanId')
            lect_string = data.get('lectString', '')

        if not eblan_id:
            return jsonify({"error": "eblanId is required"}), 400

        try:
            eblan_id = int(eblan_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid eblanId format"}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Get or create eblan record
                cur.execute("SELECT eblan_fio, eblan_img FROM eblans WHERE eblan_id = %s", (eblan_id,))
                eblan_row = cur.fetchone()
                
                if not eblan_row:
                    # If eblan doesn't exist, create with empty info
                    cur.execute(
                        "INSERT INTO eblans (eblan_id, eblan_fio, eblan_img) VALUES (%s, %s, %s) ON CONFLICT (eblan_id) DO NOTHING",
                        (eblan_id, None, None)
                    )
                    conn.commit()
                    eblan_fio, eblan_img = None, None
                else:
                    eblan_fio, eblan_img = eblan_row

                # Calculate rating stats
                cur.execute("""
                    SELECT 
                        ROUND(AVG(rating)::numeric, 1) as avg_rating,
                        COUNT(*) as rating_count
                    FROM eblan_comments 
                    WHERE eblan_id = %s
                """, (eblan_id,))
                
                rating_row = cur.fetchone()
                avg_rating = float(rating_row[0]) if rating_row[0] else 0.0
                rating_count = rating_row[1] if rating_row[1] else 0

        response = {
            "eblanImg": eblan_img,
            "eblanFio": eblan_fio,
            "rating": avg_rating,
            "ratingCount": rating_count
        }

        return jsonify(response)

    except Exception as e:
        print(f"Error in get_eblan_rating: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/createEblanComment', methods=['POST'])
def create_eblan_comment():
    """Create a new comment for lecturer."""
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400
            
        data = request.get_json(silent=True) or {}
        eblan_id = data.get('eblanId')
        rating = data.get('rating')
        comment = data.get('comment', '')
        features = data.get('features', [])

        # Validation
        if not eblan_id:
            return jsonify({"error": "eblanId is required"}), 400
        if not rating:
            return jsonify({"error": "rating is required"}), 400

        try:
            eblan_id = int(eblan_id)
            rating = int(rating)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid eblanId or rating format"}), 400

        if rating < 1 or rating > 5:
            return jsonify({"error": "Rating must be between 1 and 5"}), 400

        if not isinstance(features, list):
            return jsonify({"error": "Features must be an array"}), 400

        # Get client IP
        client_ip = request.environ.get('HTTP_X_FORWARDED_FOR', request.environ.get('REMOTE_ADDR'))

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Ensure eblan exists
                cur.execute(
                    "INSERT INTO eblans (eblan_id) VALUES (%s) ON CONFLICT (eblan_id) DO NOTHING",
                    (eblan_id,)
                )
                
                # Insert comment
                cur.execute("""
                    INSERT INTO eblan_comments (eblan_id, rating, comment, features, ip_address)
                    VALUES (%s, %s, %s, %s, %s)
                """, (eblan_id, rating, comment, features, client_ip))
                
                conn.commit()

                # Return all comments for this eblan
                cur.execute("""
                    SELECT rating, comment, features
                    FROM eblan_comments 
                    WHERE eblan_id = %s
                    ORDER BY created_at DESC
                """, (eblan_id,))
                
                comments = []
                for row in cur.fetchall():
                    comments.append({
                        "rating": row[0],
                        "comment": row[1] or "",
                        "features": row[2] or []
                    })

        return jsonify(comments)

    except Exception as e:
        print(f"Error in create_eblan_comment: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/getCommentsByEblan', methods=['GET', 'POST'])
def get_comments_by_eblan():
    """Get all comments for a lecturer."""
    try:
        if request.method == 'GET':
            eblan_id = request.args.get('eblanId')
        else:  # POST
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
            data = request.get_json(silent=True) or {}
            eblan_id = data.get('eblanId')

        if not eblan_id:
            return jsonify({"error": "eblanId is required"}), 400

        try:
            eblan_id = int(eblan_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid eblanId format"}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT rating, comment, features
                    FROM eblan_comments 
                    WHERE eblan_id = %s
                    ORDER BY created_at DESC
                """, (eblan_id,))
                
                comments = []
                for row in cur.fetchall():
                    comments.append({
                        "rating": row[0],
                        "comment": row[1] or "",
                        "features": row[2] or []
                    })

        return jsonify(comments)

    except Exception as e:
        print(f"Error in get_comments_by_eblan: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/uploadEblanImg', methods=['POST'])
def upload_eblan_img():
    """Upload lecturer image."""
    try:
        eblan_id = request.form.get('eblanId')
        if not eblan_id:
            return jsonify({"success": False, "error": "eblanId is required"}), 400

        try:
            eblan_id = int(eblan_id)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "Invalid eblanId format"}), 400

        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"}), 400

        if not allowed_file(file.filename):
            return jsonify({"success": False, "error": "Invalid file type"}), 400

        # Read and validate file size
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({"success": False, "error": "File too large"}), 400

        # Process and save image
        image_path = process_and_save_image(file_data, file.filename, 'eblans')
        if not image_path:
            return jsonify({"success": False, "error": "Error processing image"}), 500

        # Update database
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO eblans (eblan_id, eblan_img, updated_at) 
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (eblan_id) 
                    DO UPDATE SET eblan_img = EXCLUDED.eblan_img, updated_at = CURRENT_TIMESTAMP
                """, (eblan_id, image_path))
                conn.commit()

        return jsonify({"success": True, "error": None})

    except Exception as e:
        print(f"Error in upload_eblan_img: {e}")
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500

@app.route('/api/uploadLectImg', methods=['POST'])
def upload_lect_img():
    """Upload lecture image."""
    try:
        lect_string = request.form.get('lectString')
        eblan_id = request.form.get('eblanId')  # Optional, for association
        
        if not lect_string:
            return jsonify({"success": False, "error": "lectString is required"}), 400

        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"}), 400

        if not allowed_file(file.filename):
            return jsonify({"success": False, "error": "Invalid file type"}), 400

        # Parse eblan_id if provided
        eblan_id_int = None
        if eblan_id:
            try:
                eblan_id_int = int(eblan_id)
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "Invalid eblanId format"}), 400

        # Read and validate file size
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({"success": False, "error": "File too large"}), 400

        # Process and save image
        image_path = process_and_save_image(file_data, file.filename, 'lectures')
        if not image_path:
            return jsonify({"success": False, "error": "Error processing image"}), 500

        # Get client IP
        client_ip = request.environ.get('HTTP_X_FORWARDED_FOR', request.environ.get('REMOTE_ADDR'))

        # Save to database
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # If eblan_id provided, ensure eblan exists
                if eblan_id_int:
                    cur.execute(
                        "INSERT INTO eblans (eblan_id) VALUES (%s) ON CONFLICT (eblan_id) DO NOTHING",
                        (eblan_id_int,)
                    )

                cur.execute("""
                    INSERT INTO lecture_images (eblan_id, lect_string, image_path, ip_address)
                    VALUES (%s, %s, %s, %s)
                """, (eblan_id_int, lect_string, image_path, client_ip))
                conn.commit()

        return jsonify({"success": True, "error": None})

    except Exception as e:
        print(f"Error in upload_lect_img: {e}")
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500

@app.route('/api/getLectImgs', methods=['GET', 'POST'])
def get_lect_imgs():
    """Get lecture images."""
    try:
        if request.method == 'GET':
            eblan_id = request.args.get('eblanId')
            lect_string = request.args.get('lectString')
        else:  # POST
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
            data = request.get_json(silent=True) or {}
            eblan_id = data.get('eblanId')
            lect_string = data.get('lectString')

        if not lect_string:
            return jsonify({"error": "lectString is required"}), 400

        # Parse eblan_id if provided
        eblan_id_int = None
        if eblan_id:
            try:
                eblan_id_int = int(eblan_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid eblanId format"}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if eblan_id_int:
                    # Filter by both eblan_id and lect_string
                    cur.execute("""
                        SELECT image_path
                        FROM lecture_images 
                        WHERE eblan_id = %s AND lect_string = %s
                        ORDER BY created_at DESC
                    """, (eblan_id_int, lect_string))
                else:
                    # Filter only by lect_string
                    cur.execute("""
                        SELECT image_path
                        FROM lecture_images 
                        WHERE lect_string = %s
                        ORDER BY created_at DESC
                    """, (lect_string,))

                images = [row[0] for row in cur.fetchall()]

        return jsonify({"lectImgs": images})

    except Exception as e:
        print(f"Error in get_lect_imgs: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

# EXISTING SCHEDULE ENDPOINTS (keeping all existing functionality)

# Fetch schedule data from external API with caching
def fetch_schedule_data(start_date, end_date, group_id=None, person_id=None, language=3):
    """
    Fetch schedule data from the RUZ API with caching
    
    Args:
        start_date: Start date in format 'YYYY.MM.DD'
        end_date: End date in format 'YYYY.MM.DD'
        group_id: ID of the group to fetch schedule for (optional)
        person_id: ID of the person to fetch schedule for (optional)
        language: Language code (1=RU, 3=EN)
        
    Returns:
        List of schedule entries
    """
    # Generate cache key based on all parameters
    cache_key = f"schedule_{start_date}_{end_date}_{group_id}_{person_id}_{language}"
    
    # Check if we have this data in cache
    cached_data = schedule_cache.get(cache_key)
    if cached_data is not None:
        print(f"Using cached schedule data for: {start_date} to {end_date}, group_id={group_id}, person_id={person_id}")
        return cached_data
        
    print(f"Fetching fresh schedule data for: {start_date} to {end_date}, group_id={group_id}, person_id={person_id}")
    
    params = {
        "start": start_date,
        "finish": end_date,
        "lng": language
    }
    
    try:
        # Prioritize person schedule if both are provided
        if person_id:
            url = f"https://ruz.fa.ru/api/schedule/lecturer/{person_id}"
            print(f"Fetching person schedule: {url} with params {params}")
        elif group_id:
            url = f"https://ruz.fa.ru/api/schedule/group/{group_id}"
            print(f"Fetching group schedule: {url} with params {params}")
        else:
            # Default group ID if none provided
            url = f"https://ruz.fa.ru/api/schedule/group/154479"
            print(f"Fetching default group schedule: {url} with params {params}")

        response = session.get(url, params=params)
        print(f"API response status: {response.status_code}, Content-Type: {response.headers.get('Content-Type')}")
        response.raise_for_status()  # Raise an exception for HTTP errors
        data = response.json()
        
        # Enhanced logging for the retrieved data
        if isinstance(data, list):
            print(f"Retrieved {len(data)} schedule items from API")
            if len(data) == 0:
                print(f"WARNING: API returned empty schedule for {start_date} to {end_date}, group_id={group_id}, person_id={person_id}")
        else:
            print(f"WARNING: API returned non-list data: {type(data).__name__}")
            
        # Cache the result
        schedule_cache.set(cache_key, data)
        return data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching schedule data: {e}")
        # Don't cache errors
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing API response as JSON: {e}")
        print(f"Response content (first 200 chars): {response.text[:200]}")
        # Don't cache invalid responses
        return []

# New function to preload schedule for ИБ23-8
def preload_ib238_schedule():
    """Preload schedule data for group ИБ23-8 (ID: 154479)"""
    print("=" * 50)
    print("Preloading schedule for group ИБ23-8 (ID: 154479)...")
    print("=" * 50)
    
    try:
        # Get current date
        today = datetime.now()
        
        # Get first day of current month
        start_date = today.replace(day=1).strftime("%Y.%m.%d")
        
        # Get last day of current month
        if today.month == 12:
            end_date = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end_date = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        end_date = end_date.strftime("%Y.%m.%d")
        
        print(f"Fetching schedule for current month: {start_date} to {end_date}")
        current_month_data = fetch_schedule_data(start_date, end_date, group_id=154479)
        print(f"Current month data fetched: {len(current_month_data)} entries")
        
        # Get first day of next month
        if today.month == 12:
            next_month_start = datetime(today.year + 1, 1, 1)
        else:
            next_month_start = today.replace(month=today.month + 1, day=1)
        next_month_start_str = next_month_start.strftime("%Y.%m.%d")
        
        # Get last day of next month
        if next_month_start.month == 12:
            next_month_end = next_month_start.replace(year=next_month_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            next_month_end = next_month_start.replace(month=next_month_start.month + 1, day=1) - timedelta(days=1)
        next_month_end_str = next_month_end.strftime("%Y.%m.%d")
        
        print(f"Fetching schedule for next month: {next_month_start_str} to {next_month_end_str}")
        next_month_data = fetch_schedule_data(next_month_start_str, next_month_end_str, group_id=154479)
        print(f"Next month data fetched: {len(next_month_data)} entries")
        
        # Cache status report
        cache_stats = schedule_cache.stats()
        print(f"Cache statistics after preload: {cache_stats}")
        print("Schedule preloading for ИБ23-8 completed successfully")
        print("=" * 50)
    except Exception as e:
        print(f"ERROR during schedule preloading: {e}")
        print(f"Stack trace: {traceback.format_exc()}")
        print("=" * 50)

# Generate a stable ID based on name
def generate_stable_id(text):
    # Create a hash of the text and take first 8 digits to create a numeric ID
    hash_object = hashlib.md5(text.encode())
    hex_dig = hash_object.hexdigest()
    numeric_id = int(hex_dig, 16) % 100000000  # Take modulo to get an 8-digit number
    return numeric_id

# Format date for API request (YYYY.MM.DD)
def format_date_for_api(dt):
    return dt.strftime("%Y.%m.%d")

@app.route('/api/getFilterOptions', methods=['GET', 'POST'])
def get_filter_options():
    try:
        if request.method == 'GET':
            # For GET requests, use parameters from query string
            date_from_str = request.args.get('dateFrom', '2025-09-01T00:00:00Z')
            date_to_str = request.args.get('dateTo', '2025-09-30T23:59:59Z')
            group = request.args.get('group')
            eblan = request.args.get('eblan')  # Lecturer ID
        else:  # POST
            # Improved POST handling
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
                
            # For POST requests, get from JSON body
            data = request.get_json(silent=True) or {}
            date_from_str = data.get('dateFrom', '2025-09-01T00:00:00Z')
            date_to_str = data.get('dateTo', '2025-09-30T23:59:59Z')
            group = data.get('group')
            eblan = data.get('eblan')
        
        # Generate cache key
        cache_key = f"filter_{date_from_str}_{date_to_str}_{group}_{eblan}"
        
        # Check cache first
        cached_data = filter_cache.get(cache_key)
        if cached_data is not None:
            print(f"Cache hit for filter options: {cache_key}")
            return jsonify(cached_data)
    
        # Convert parameters to integers if provided
        if group is not None:
            if isinstance(group, int):
                group_id = group
            elif isinstance(group, str) and group.isdigit():
                group_id = int(group)
            else:
                group_id = None
        else:
            group_id = None
            
        if eblan is not None:
            if isinstance(eblan, int):
                person_id = eblan
            elif isinstance(eblan, str) and eblan.isdigit():
                person_id = int(eblan)
            else:
                person_id = None
        else:
            person_id = None
    
        # Parse date range with proper timezone handling
        try:
            date_from = datetime.fromisoformat(date_from_str.replace('Z', '+00:00'))
            date_to = datetime.fromisoformat(date_to_str.replace('Z', '+00:00'))
            
            # Handle case where dateTo is before dateFrom
            if date_to < date_from:
                date_from, date_to = date_to, date_from  # Swap them
        except ValueError:
            return jsonify({"error": "Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)"}), 400
        
        # Format dates for API request
        api_start_date = format_date_for_api(date_from)
        api_end_date = format_date_for_api(date_to)
        
        # Fetch schedule data from external API
        # Prioritize person_id if provided, otherwise use group_id
        if person_id is not None:
            schedule_data = fetch_schedule_data(api_start_date, api_end_date, 
                                               person_id=person_id)
        else:
            schedule_data = fetch_schedule_data(api_start_date, api_end_date, 
                                               group_id=group_id)
        
        # Initialize sets to store unique values
        disciplines = set()
        locations = set()
        lecturers = set()
        
        # Process schedule entries
        for entry in schedule_data:
            try:
                # Skip entries without a date
                if not entry.get('date'):
                    continue
                
                # Filter by eblan (lecturer) if provided
                if person_id and entry.get('lecturerOid') and int(entry.get('lecturerOid')) != person_id:
                    continue
                    
                # Extract discipline with stable ID based on name
                if entry.get('discipline'):
                    discipline_name = entry['discipline']
                    discipline_id = generate_stable_id(discipline_name)
                    disciplines.add((discipline_id, discipline_name))
                
                # Extract location with stable ID based on name
                if entry.get('building'):
                    building_name = entry['building']
                    building_id = generate_stable_id(building_name)
                    locations.add((building_id, building_name))
                
                # Extract lecturer with stable ID based on full name
                lecturer_field = entry.get('lecturer_title', entry.get('lecturer'))
                if lecturer_field:
                    full_name = lecturer_field
                    lecturer_id = entry.get('lecturerOid', generate_stable_id(full_name))
                    
                    # Generate short name
                    name_parts = full_name.split()
                    if len(name_parts) >= 2:
                        last_name = name_parts[0]
                        initials = ''.join([part[0] + '.' for part in name_parts[1:]])
                        short_name = f"{last_name} {initials}"
                    else:
                        short_name = full_name
                    
                    lecturers.add((lecturer_id, full_name, short_name))
            except Exception as e:
                # Skip entries with invalid format
                print(f"Error processing entry: {e}")
                continue
        
        # Format the response
        response = {
            "disciplines": [
                {"id": id, "name": name} 
                for id, name in disciplines
            ],
            "locations": [
                {"id": id, "name": name}
                for id, name in locations
            ],
            "eblans": [
                {"id": id, "name": name, "short": short}
                for id, name, short in lecturers
            ]
        }
        
        # Cache the result
        filter_cache.set(cache_key, response)
        
    except Exception as e:
        return jsonify({"error": f"Request processing error: {str(e)}"}), 400
    
    return jsonify(response)

@app.route('/api/getRUZ', methods=['GET', 'POST'])
def get_ruz():
    try:
        if request.method == 'GET':
            # For GET requests, use parameters from query string
            date_from_str = request.args.get('dateFrom', '2025-09-01T00:00:00Z')
            date_to_str = request.args.get('dateTo', '2025-09-30T23:59:59Z')
            filters_str = request.args.get('filters', '{}')
            try:
                filters = json.loads(filters_str)
            except json.JSONDecodeError:
                filters = {}
        else:  # POST
            # Improved POST request handling
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
                
            # For POST requests, get from JSON body
            data = request.get_json(silent=True) or {}
            date_from_str = data.get('dateFrom', '2025-09-01T00:00:00Z')
            date_to_str = data.get('dateTo', '2025-09-30T23:59:59Z')
            filters = data.get('filters', {})
        
        # Улучшенное формирование ключа кэша с чётким выделением group_id
        group_id_str = str(filters.get('groupId', 'default'))
        eblan_id_str = str(filters.get('eblanIds', ['default'])[0] if filters.get('eblanIds') else 'default')
        cache_key = f"ruz_{date_from_str}_{date_to_str}_group_{group_id_str}_filters_{hash(json.dumps(filters))}"
        
        # Добавим отладочную информацию
        print(f"Using cache key: {cache_key}, filters: {filters}")
        
        # Check cache first
        cached_data = schedule_cache.get(cache_key)
        if cached_data is not None:
            print(f"Cache hit for RUZ data: {cache_key}")
            return jsonify(cached_data)
    
        # Extract filter IDs
        discipline_ids = filters.get('disciplineIds')
        location_ids = filters.get('locationIds')
        eblan_ids = filters.get('eblanIds')
        group_ids = filters.get('groupIds') or ([filters.get('groupId')] if filters.get('groupId') else [])
        # group_ids теперь всегда список

        # Convert IDs to integers if they're provided
        if discipline_ids is not None:
            discipline_ids = [int(id) for id in discipline_ids]
        if location_ids is not None:
            location_ids = [int(id) for id in location_ids]
        if eblan_ids is not None:
            eblan_ids = [int(id) for id in eblan_ids]
        group_ids = [int(gid) for gid in group_ids if gid]

        # Parse date range
        try:
            date_from = datetime.fromisoformat(date_from_str.replace('Z', '+00:00'))
            date_to = datetime.fromisoformat(date_to_str.replace('Z', '+00:00'))
            
            # DEBUG: Print the dates we're processing
            print(f"Processing date range: {date_from} to {date_to}")
            
            # Handle case where dateTo is before dateFrom
            if date_to < date_from:
                date_from, date_to = date_to, date_from  # Swap them
                
            # Add a day buffer on each side to account for timezone differences
            date_from = date_from - timedelta(days=1)
            date_to = date_to + timedelta(days=1)
            print(f"Adjusted date range with buffer: {date_from} to {date_to}")
        except ValueError:
            return jsonify({"error": "Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SSZ)"}), 400
        
        # Format dates for API request
        api_start_date = format_date_for_api(date_from)
        api_end_date = format_date_for_api(date_to)
        print(f"API date range: {api_start_date} to {api_end_date}")
        
        # Fetch schedule data from external API
        # Если есть только eblanIds — ищем по каждому преподу!
        if eblan_ids and not group_ids:
            schedule_data = []
            for eblan_id in eblan_ids:
                schedule_data.extend(fetch_schedule_data(api_start_date, api_end_date, person_id=eblan_id))
        elif group_ids:
            schedule_data = []
            for gid in group_ids:
                schedule_data.extend(fetch_schedule_data(api_start_date, api_end_date, group_id=gid))
        else:
            schedule_data = fetch_schedule_data(api_start_date, api_end_date, group_id=154479)
        print(f"Found {len(schedule_data)} schedule entries before filtering")
        
        # Initialize list to store processed lessons
        lessons = []
        
        # Process schedule entries
        for entry in schedule_data:
            try:
                # Skip entries without a date or time info
                if not entry.get('date') or not entry.get('beginLesson') or not entry.get('endLesson'):
                    continue

                # Generate stable IDs for filtering
                discipline_name = entry.get('discipline', '')
                discipline_id = generate_stable_id(discipline_name) if discipline_name else None
                
                building_name = entry.get('building', '')
                location_id = generate_stable_id(building_name) if building_name else None
                
                # Get lecturer info - USE THE ACTUAL LECTURER ID FROM THE API
                lecturer_field = entry.get('lecturer_title', entry.get('lecturer', ''))
                eblan_id = entry.get('lecturerOid')  # Use the actual ID from the API
                
                # Make sure eblan_id is an integer for comparison
                if eblan_id:
                    try:
                        eblan_id = int(eblan_id)
                    except (ValueError, TypeError):
                        # Fall back to generating ID if actual ID isn't available
                        eblan_id = generate_stable_id(lecturer_field) if lecturer_field else None
                
                # Debug the filtering
                filtered_out = False
                filtered_out_reason = None
                
                # Apply filters
                if discipline_ids is not None and discipline_id is not None and discipline_id not in discipline_ids:
                    filtered_out = True
                    filtered_out_reason = f"discipline_id {discipline_id} not in filter list"
                    
                if not filtered_out and location_ids is not None and location_id is not None and location_id not in location_ids:
                    filtered_out = True
                    filtered_out_reason = f"location_id {location_id} not in filter list"
                    
                if not filtered_out and eblan_ids is not None and eblan_id is not None and eblan_id not in eblan_ids:
                    filtered_out = True
                    filtered_out_reason = f"eblan_id {eblan_id} not in filter list {eblan_ids}"
            
                if filtered_out:
                    print(f"Filtered out entry: {filtered_out_reason}")
                    continue
                
                # Prepare start and end times
                date_str = entry.get('date')
                start_time_str = entry.get('beginLesson', '00:00')
                end_time_str = entry.get('endLesson', '00:00')

                # Определяем лекция ли это
                kind_of_work = entry.get('kindOfWork', '').lower()
                is_lecture = any(
                    kw in kind_of_work
                    for kw in ['лекции', 'lecture', 'Лекции']
                )

                # Prepare lecturer (eblan) info
                eblan_name = lecturer_field
                
                # Generate short name
                eblan_short = ""
                if eblan_name:
                    name_parts = eblan_name.split()
                    if len(name_parts) >= 2:
                        last_name = name_parts[0]
                        initials = ''.join([part[0] + '.' for part in name_parts[1:]])
                        eblan_short = f"{last_name} {initials}"
                    else:
                        eblan_short = eblan_name
                
                # Create lesson object
                lesson = {
                    "start": f"{date_str}T{start_time_str}Z",
                    "end": f"{date_str}T{end_time_str}Z",
                    "isLecture": is_lecture,  # <--- Новое поле
                    "eblanInfo": {
                        "eblanId": eblan_id,
                        "eblanName": eblan_name,
                        "eblanNameShort": eblan_short
                    },
                    "locationInfo": {
                        "locationId": location_id,
                        "locationName": building_name,
                        "cabinet": entry.get('auditorium', '')
                    },
                    "disciplineInfo": {
                        "disciplineId": discipline_id,
                        "DisciplineName": discipline_name
                    }
                }

                lessons.append(lesson)
                
            except Exception as e:
                # Skip entries with invalid format
                print(f"Error processing entry: {e}")
                continue
        
        # Create the response object
        response = {"lessons": lessons}
        
        # Cache the result
        schedule_cache.set(cache_key, response)
    
    except Exception as e:
        return jsonify({"error": f"Request processing error: {str(e)}"}), 400
    
    return jsonify(response)

# Function to search the RUZ API with caching
def search_ruz_api(search_type, search_query):
    # Generate cache key
    cache_key = f"search_{search_type}_{search_query}"
    
    # Check if we have this search in cache
    cached_data = search_cache.get(cache_key)
    if cached_data is not None:
        print(f"Cache hit for search: {cache_key}")
        return cached_data
    
    print(f"Cache miss for search: {cache_key}")
    
    # Define search endpoints for different types
    if search_type == 1:  # Group
        url = "https://ruz.fa.ru/api/search"
        params = {"term": search_query, "type": "group"}
    elif search_type == 2:  # Lecturer (previously was type 3)
        url = "https://ruz.fa.ru/api/search"
        params = {"term": search_query, "type": "lecturer"}
    else:
        return []
            
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        
        # Ensure result is a list
        if not isinstance(result, list):
            print(f"API returned non-list result: {result}")
            result = []
        
        # Cache the result
        search_cache.set(cache_key, result)
        return result
    except Exception as e:
        print(f"Error searching RUZ API for type {search_type}: {e}")
        return []

@app.route('/api/search', methods=['GET', 'POST'])
def search():
    try:
        if request.method == 'GET':
            # For GET requests, use parameters from query string
            search_string = request.args.get('searchString', '')
            type_filter = request.args.get('type')
            if type_filter and type_filter.isdigit():
                type_filter = int(type_filter)
            else:
                type_filter = None
        else:  # POST
            # Improved POST handling
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
                
            # For POST requests, get from JSON body
            data = request.get_json(silent=True) or {}
            search_string = data.get('searchString', '')
            type_filter = data.get('type')
    
        # Return empty results if search string is too short
        if len(search_string) < 2:
            return jsonify({"result": [], "error": "Search string too short, minimum 2 characters"})
        
        # Generate cache key
        # handle None type_filter
        type_filter_str = str(type_filter) if type_filter is not None else "all"
        cache_key = f"search_result_{search_string}_{type_filter_str}"
        
        # Check if we have these search results in cache
        cached_results = search_cache.get(cache_key)
        if cached_results is not None:
            print(f"Cache hit for search results: {cache_key}")
            return jsonify({"result": cached_results})
        
        print(f"Cache miss for search results: {cache_key}")
        results = []
        
        # Process search based on type filter - only types 1 and 2
        types_to_search = [1, 2] if type_filter is None else [type_filter]
        
        # Use ThreadPoolExecutor to run searches in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(types_to_search)) as executor:
            # Submit all search tasks
            future_to_type = {
                executor.submit(search_ruz_api, search_type, search_string): search_type
                for search_type in types_to_search
            }
            
            # Process results as they complete
            for future in concurrent.futures.as_completed(future_to_type):
                search_type = future_to_type[future]
                try:
                    api_results = future.result()
                    for item in api_results:
                        # Skip non-dict items
                        if not isinstance(item, dict):
                            print(f"Skipping non-dict item: {item}")
                            continue
                            
                        result_item = {
                            "type": search_type,
                            "id": str(item.get("id", "")),
                            "name": item.get("label", ""),
                            "description": item.get("description", "")
                        }
                        results.append(result_item)
                except Exception as e:
                    print(f"Error in search task for type {search_type}: {e}")
        
        try:
            # Explicitly try to cache the results
            search_cache.set(cache_key, results)
            print(f"Successfully cached search results for key: {cache_key}")
        except Exception as cache_error:
            print(f"ERROR: Failed to cache search results: {cache_error}")
        
        # Return the results
        return jsonify({"result": results})
    except Exception as e:
        return jsonify({"error": f"Request processing error: {str(e)}"}), 400

# Add a route to clear caches if needed
@app.route('/api/clearCache', methods=['POST'])
def clear_cache():
    try:
        schedule_cache.clear()
        search_cache.clear()
        filter_cache.clear()
        return jsonify({"status": "success", "message": "All caches cleared"})
    except Exception as e:
        return jsonify({"error": f"Failed to clear cache: {str(e)}"}), 500

@app.route('/api/clearGroupCache', methods=['POST'])
def clear_group_cache():
    try:
        data = request.get_json(silent=True) or {}
        group_id = data.get('groupId')
        
        # Удаление ключей, связанных с группой
        if group_id:
            keys = redis_client.keys(f"schedule:ruz_*_group_{group_id}_*")
            if keys:
                redis_client.delete(*keys)
            return jsonify({"status": "success", "message": f"Cache cleared for group {group_id}"})
        else:
            return jsonify({"error": "No groupId provided"}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to clear cache: {str(e)}"}), 500

# Add a new route for cache stats
@app.route('/api/cacheStats', methods=['GET'])
def cache_stats():
    try:
        return jsonify({
            "schedule_cache": schedule_cache.stats(),
            "search_cache": search_cache.stats(),
            "filter_cache": filter_cache.stats()
        })
    except Exception as e:
        return jsonify({"error": f"Failed to get cache stats: {str(e)}"}), 500

# Add batch processing for large response generation
def batch_process(items, process_func, batch_size=100):
    """Process items in batches to avoid blocking the event loop too long."""
    results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        batch_results = [process_func(item) for item in batch]
        results.extend(batch_results)
    return results

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(preload_ib238_schedule, 'cron', hour=0, minute=0)

if __name__ == '__main__':
    # Initialize database on startup
    init_database()
    
    # Preload schedule data on startup
    try:
        preload_ib238_schedule()
    except Exception as e:
        print(f"Warning: Could not preload schedule data: {e}")
    
    # Start the scheduler
    try:
        scheduler.start()
        print("Background scheduler started successfully")
    except Exception as e:
        print(f"Warning: Could not start scheduler: {e}")
    
    # Start Flask application
    app.run(host='0.0.0.0', debug=True)