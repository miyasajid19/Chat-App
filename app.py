# --- START OF FILE app.py (with specific PUBLIC chat debug logs) ---

import os
import random
import string
import pymysql
import datetime
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, session,
    flash, url_for, g, jsonify
)
from flask_socketio import (
    SocketIO, join_room, leave_room, send, disconnect
)
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
import logging
import sys # For sys.exit

# Load environment variables from .env file
load_dotenv()

# App Initialization
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default_very_unsafe_secret_key')
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')

# Configure logging
log_format = '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
log_level = logging.DEBUG if app.debug else logging.INFO
logging.basicConfig(level=log_level, format=log_format)


# SocketIO Initialization - Specify async_mode
socketio = SocketIO(app, async_mode='eventlet', logger=app.logger, engineio_logger=app.logger.getChild('engineio'))

# Mail Configuration
app.config.update(
    MAIL_SERVER=os.getenv('MAIL_SERVER', 'smtp.gmail.com'),
    MAIL_PORT=int(os.getenv('MAIL_PORT', 465)),
    MAIL_USE_SSL=os.getenv('MAIL_USE_SSL', 'True').lower() == 'true',
    MAIL_USE_TLS=os.getenv('MAIL_USE_TLS', 'False').lower() == 'true',
    MAIL_USERNAME=os.getenv('MAIL_USERNAME'),
    MAIL_PASSWORD=os.getenv('MAIL_PASSWORD'),
    MAIL_DEFAULT_SENDER=os.getenv('MAIL_DEFAULT_SENDER') or os.getenv('MAIL_USERNAME')
)

if not all([app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'], app.config['MAIL_DEFAULT_SENDER']]):
    app.logger.warning("Mail configuration missing or incomplete. Password reset may not work.")
mail = Mail(app)

# Database Configuration from Environment Variables
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'database': os.getenv('DB_NAME'),
    'charset': "utf8mb4",
    'cursorclass': pymysql.cursors.DictCursor,
    'connect_timeout': 10,
    'read_timeout': 30,
    'write_timeout': 30,
    'autocommit': True
}

if not all([DB_CONFIG['host'], DB_CONFIG['user'], DB_CONFIG['password'], DB_CONFIG['database']]):
     raise ValueError("Database configuration is incomplete. Check DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in .env")

# Database Connection Management
def get_db():
    if 'db' not in g or not g.db.open:
        try:
            g.db = pymysql.connect(**DB_CONFIG)
            app.logger.debug("Database connection established.")
        except pymysql.MySQLError as e:
            app.logger.error(f"Database connection failed: {e}")
            raise ConnectionError(f"Could not connect to the database: {e}") from e
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None and db.open:
        try:
            db.close()
            app.logger.debug("Database connection closed.")
        except pymysql.MySQLError as e:
            app.logger.error(f"Error closing database connection: {e}")
    elif db is not None:
         app.logger.debug("Database connection was already closed or not open at teardown.")
    if error:
        app.logger.error(f"Exception during request handling (detected in teardown): {error}")

def initialize_database():
    try:
        conn = pymysql.connect(**{k:v for k,v in DB_CONFIG.items() if k != 'database'})
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cursor.execute(f"USE `{DB_CONFIG['database']}`;")
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL,
            email VARCHAR(255) UNIQUE NOT NULL, password VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""")
        app.logger.info("Checked/Created users table.")
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INT AUTO_INCREMENT PRIMARY KEY, room_code VARCHAR(10) NOT NULL,
            user_id INT, user_name VARCHAR(255) NOT NULL, content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, INDEX room_time_idx (room_code, timestamp));""")
        app.logger.info("Checked/Created messages table.")
        try:
            cursor.execute("""ALTER TABLE messages ADD CONSTRAINT fk_user_id FOREIGN KEY (user_id)
                           REFERENCES users(id) ON DELETE SET NULL;""")
            app.logger.info("Added/Verified foreign key constraint messages.user_id -> users.id.")
        except pymysql.MySQLError as fk_error:
            if fk_error.args[0] not in (1061, 1826): # 1061: Duplicate key name, 1826: FK exists
                 app.logger.warning(f"Could not add foreign key constraint (may already exist): {fk_error}")
        conn.commit()
        cursor.close()
        conn.close()
        app.logger.info("Database initialization check complete.")
    except pymysql.MySQLError as e:
        app.logger.error(f"Database initialization failed: {e}")
        raise RuntimeError(f"FATAL: Database could not be initialized: {e}") from e

try:
    initialize_database()
except RuntimeError as e:
     print(f"Error during startup: {e}", file=sys.stderr)
     sys.exit(1)

# In-memory room tracker: {'ROOMCODE': {'members': count}}
rooms = {}

# --- Utility Functions ---
def generate_room_code(length=4):
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=length))
        if code not in rooms and code != "PUBLIC":
            return code

# --- Routes (Authentication & Core Pages) ---
@app.route('/')
@app.route('/home')
def home():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # ... (Register logic - Keep as is) ...
    if 'user_id' in session:
        return redirect(url_for('dashboard')) # Already logged in

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')

        # Basic Validation
        if not name or not email or not password:
             flash("All fields (Username, Email, Password) are required.", "error")
             return render_template('register.html', name=name, email=email), 400 # Bad request
        if len(password) < 6: # Basic password length check
             flash("Password must be at least 6 characters long.", "error")
             return render_template('register.html', name=name, email=email), 400

        hashed_password = generate_password_hash(password)

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Check if email exists
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("This email address is already registered.", "error")
                return render_template('register.html', name=name, email=email), 409 # Conflict

            # Check if username exists
            cursor.execute("SELECT id FROM users WHERE name = %s", (name,))
            if cursor.fetchone():
                flash("This username is already taken. Please choose another.", "error")
                return render_template('register.html', name=name, email=email), 409 # Conflict

            # Insert new user
            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                           (name, email, hashed_password))
            # conn.commit() # Autocommit is on

            # Log the user in immediately after registration
            user_id = cursor.lastrowid # Get the ID of the inserted user
            session['user_id'] = user_id
            session['name'] = name
            session['email'] = email
            app.logger.info(f"User '{name}' (ID: {user_id}) registered successfully.")

            flash(f"Account created successfully! Welcome, {name}!", "success")
            return redirect(url_for('dashboard'))

        except pymysql.MySQLError as e:
            # conn.rollback() # Not needed with autocommit
            app.logger.error(f"Registration DB error for user {name}/{email}: {e}")
            flash("An unexpected database error occurred during registration. Please try again later.", "error")
            return render_template('register.html', name=name, email=email), 500 # Internal server error
        finally:
            cursor.close() # Close cursor even if connection stays open via 'g'

    # GET request
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # ... (Login logic - Keep as is) ...
    if 'user_id' in session:
        return redirect(url_for('dashboard')) # Already logged in

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template('index.html', email=email), 400

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name, email, password FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user['password'], password):
                session.permanent = True # Make session last longer (e.g., 31 days)
                app.permanent_session_lifetime = datetime.timedelta(days=31)
                session['user_id'] = user['id']
                session['name'] = user['name']
                session['email'] = user['email']
                app.logger.info(f"User '{user['name']}' (ID: {user['id']}) logged in successfully.")
                # Check if there's a 'next' URL parameter from @login_required redirects
                next_url = request.args.get('next')
                # Add basic validation for next_url to prevent open redirect vulnerability
                if next_url and next_url.startswith('/'):
                    return redirect(next_url)
                else:
                    return redirect(url_for('dashboard'))
            else:
                flash("Invalid email or password. Please try again.", "error")
                return render_template('index.html', email=email), 401 # Unauthorized

        except pymysql.MySQLError as e:
            app.logger.error(f"Login DB error for email {email}: {e}")
            flash("An unexpected database error occurred during login. Please try again later.", "error")
            return render_template('index.html', email=email), 500
        finally:
            cursor.close()

    # GET request
    return render_template('index.html')

@app.route('/logout')
def logout():
    # ... (Logout logic - Keep as is) ...
    user_name = session.get('name', 'User')
    # Clear specific session keys related to user authentication
    session.pop('user_id', None)
    session.pop('name', None)
    session.pop('email', None)
    session.pop('room', None) # Also clear current room on logout
    flash(f"You have been successfully logged out. Goodbye, {user_name}!", "info")
    return redirect(url_for('home'))

@app.route('/dashboard')
def dashboard():
    # ... (Dashboard logic - Keep as is) ...
    if 'user_id' not in session:
        flash("Please log in to access the dashboard.", "warning")
        return redirect(url_for('login', next=request.url)) # Redirect back here after login
    session.pop('room', None) # Clear previous room selection
    return render_template('dashboard.html', username=session.get('name'))

@app.route('/forgetPassword', methods=['GET', 'POST'])
def forget_password():
    # ... (Forget Password logic - Keep as is) ...
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            flash("Email address is required.", "error")
            return render_template('forgetPassword.html'), 400

        if not app.config.get('MAIL_USERNAME'):
             flash("Password reset emails are currently disabled. Please contact support.", "error")
             return render_template('forgetPassword.html', email=email), 503 # Service unavailable

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                otp = ''.join(random.choices(string.digits, k=6))
                session['reset_otp'] = otp
                session['reset_email'] = email
                session['reset_otp_expires'] = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat() # Set 10 min expiry in UTC ISO format

                try:
                    message_body = f"Your OTP to reset your ChatApp password is: {otp}\n\nThis OTP is valid for 10 minutes."
                    message = Message(subject="Your ChatApp Password Reset OTP", recipients=[email], body=message_body)
                    mail.send(message)
                    app.logger.info(f"Password reset OTP sent to {email}")
                    flash("An OTP has been sent to your email address. Please check your inbox (and spam folder).", "info")
                    return redirect(url_for('reset_password'))
                except Exception as e:
                    app.logger.error(f"Failed to send OTP email to {email}: {e}")
                    flash("Could not send the OTP email due to a server error. Please try again later or contact support.", "error")
                    return render_template('forgetPassword.html', email=email), 500
            else:
                app.logger.warning(f"Password reset requested for non-existent email: {email}")
                flash("If an account with that email exists, an OTP has been sent.", "info") # Generic message
                return redirect(url_for('reset_password')) # Still redirect
        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password forget request for {email}: {e}")
            flash("A database error occurred. Please try again.", "error")
            return render_template('forgetPassword.html', email=email), 500
        finally:
            cursor.close()
    # GET request
    return render_template('forgetPassword.html')

@app.route('/resetPassword', methods=['GET', 'POST'])
def reset_password():
    # ... (Reset Password logic - Keep as is) ...
    reset_email = session.get('reset_email')
    otp_expires_iso = session.get('reset_otp_expires')
    if not reset_email or not otp_expires_iso:
        flash("Invalid password reset request or session expired. Please request a new OTP.", "warning")
        return redirect(url_for('forget_password'))
    try:
        otp_expires = datetime.datetime.fromisoformat(otp_expires_iso)
        if datetime.datetime.utcnow() > otp_expires:
            session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
            flash("Your OTP has expired. Please request a new one.", "error")
            return redirect(url_for('forget_password'))
    except ValueError:
         app.logger.error(f"Invalid ISO format for OTP expiry in session: {otp_expires_iso}")
         flash("Invalid session state. Please request a new OTP.", "error")
         return redirect(url_for('forget_password'))

    if request.method == 'POST':
        otp_entered = request.form.get('otp')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        session_otp = session.get('reset_otp')
        if not otp_entered or not new_password or not confirm_password:
            flash("Please fill in the OTP and both password fields.", "error")
            return render_template('resetPassword.html'), 400
        if new_password != confirm_password:
            flash("The new passwords do not match.", "error")
            return render_template('resetPassword.html'), 400
        if len(new_password) < 6:
             flash("Password must be at least 6 characters long.", "error")
             return render_template('resetPassword.html'), 400
        if not session_otp or otp_entered != session_otp:
            flash("The OTP entered is invalid or has expired.", "error")
            return render_template('resetPassword.html'), 400
        hashed_password = generate_password_hash(new_password)
        conn = get_db()
        cursor = conn.cursor()
        try:
            rows_affected = cursor.execute("UPDATE users SET password = %s WHERE email = %s", (hashed_password, reset_email))
            if rows_affected == 1:
                app.logger.info(f"Password successfully reset for email: {reset_email}")
                session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                flash("Your password has been reset successfully. Please log in with your new password.", "success")
                return redirect(url_for('login'))
            else:
                 app.logger.error(f"Password reset failed for {reset_email}, user not found or DB error despite valid OTP.")
                 flash("An unexpected error occurred while updating your password. Please try again.", "error")
                 return render_template('resetPassword.html'), 500
        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password reset update for {reset_email}: {e}")
            flash("An error occurred while resetting the password. Please try again.", "error")
            return render_template('resetPassword.html'), 500
        finally:
            cursor.close()
    # GET request
    return render_template('resetPassword.html')

# --- Chat Room Logic & Routes ---
@app.route('/join_room', methods=['POST'])
def join_room_route():
    # ... (Join Room logic - Keep as is) ...
    if 'user_id' not in session:
        flash("Please log in first.", "warning")
        return redirect(url_for('login', next=url_for('dashboard')))
    name = session.get("name")
    room_code_input = request.form.get("room_code", "").strip().upper()
    join_room_action = request.form.get("join_room")
    create_room_action = request.form.get("create_room")
    target_room_code = None
    if join_room_action:
        if not room_code_input:
            flash("Please enter a room code to join.", "error"); return redirect(url_for('dashboard'))
        if len(room_code_input) != 4:
            flash("Invalid room code format (must be 4 letters).", "error"); return redirect(url_for('dashboard'))
        if room_code_input == "PUBLIC":
             flash("Cannot join 'PUBLIC' room this way. Use the Public Chat link.", "warning"); return redirect(url_for('dashboard'))
        room_exists = False
        if room_code_input in rooms: room_exists = True
        else:
            conn = get_db(); cursor = conn.cursor()
            try:
                 cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (room_code_input,))
                 if cursor.fetchone():
                     room_exists = True
                     rooms[room_code_input] = {"members": 0}
                     app.logger.info(f"Re-activating room '{room_code_input}' tracker based on DB history.")
            except pymysql.MySQLError as e:
                 app.logger.error(f"DB error checking room existence for {room_code_input}: {e}")
                 flash("A database error occurred while checking the room. Please try again.", "error"); return redirect(url_for('dashboard'))
            finally: cursor.close()
        if not room_exists:
            flash(f"Room code '{room_code_input}' does not exist or has no history.", "error"); return redirect(url_for('dashboard'))
        target_room_code = room_code_input
    elif create_room_action:
        new_code = generate_room_code()
        rooms[new_code] = {"members": 0}
        target_room_code = new_code
        app.logger.info(f"User '{name}' created new private room '{target_room_code}'.")
    else:
        flash("Invalid action specified.", "error"); return redirect(url_for('dashboard'))
    session['room'] = target_room_code
    app.logger.info(f"User '{name}' attempting to join room '{target_room_code}'.")
    return redirect(url_for('room'))

@app.route('/room')
def room():
    # ... (Private Room logic - Keep as is) ...
    if 'user_id' not in session:
        flash("Please log in to access chat rooms.", "warning")
        return redirect(url_for('login', next=request.url))
    room_code = session.get('room')
    user_name = session.get('name')
    if not room_code or not user_name:
        flash("No active room selected. Please join or create one from the dashboard.", "warning")
        return redirect(url_for('dashboard'))
    if room_code == "PUBLIC":
         flash("Please use the dedicated Public Chat link from the dashboard.", "warning")
         session.pop('room', None); return redirect(url_for('dashboard'))
    room_exists_check = False
    if room_code in rooms: room_exists_check = True
    else:
         conn = get_db(); cursor = conn.cursor()
         try:
              cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (room_code,))
              if cursor.fetchone():
                  room_exists_check = True; rooms[room_code] = {"members": 0}
         except pymysql.MySQLError as e:
              app.logger.error(f"DB error checking room {room_code} existence on /room access: {e}"); room_exists_check = True # Assume exists if DB fails
         finally: cursor.close()
    if not room_exists_check:
         flash(f"The room '{room_code}' is no longer active or valid.", "error")
         session.pop('room', None); return redirect(url_for('dashboard'))
    app.logger.debug(f"Rendering private chat room '{room_code}' for user '{user_name}'.")
    return render_template('chatroom.html', room_code=room_code, username=user_name)

@app.route('/public_chat')
def public_chat():
    # ... (Public Chat route - Keep as is) ...
    if 'user_id' not in session:
        flash("Please log in to view the public chat.", "warning")
        return redirect(url_for('login', next=request.url))
    session['room'] = "PUBLIC"
    user_name = session.get('name')
    app.logger.info(f"User '{user_name}' entering public chat (room 'PUBLIC').")
    if "PUBLIC" not in rooms:
        rooms["PUBLIC"] = {"members": 0}
        app.logger.info("Initialized PUBLIC room tracker.")
    return render_template('public_chat.html', username=user_name)


# --- Database Helper Functions for Chat ---
def get_message_history(room_code, limit=50):
    """Fetches the last 'limit' messages for a given room_code from the database."""
    # --->>> ADDED DEBUG LOG <<<---
    app.logger.debug(f"Attempting to fetch history for room_code: '{room_code}' (Type: {type(room_code)})")
    # --->>> END DEBUG LOG <<<---
    conn = get_db()
    cursor = conn.cursor()
    messages = []
    try:
        cursor.execute("""
            SELECT user_id, user_name, content, timestamp FROM messages
            WHERE room_code = %s ORDER BY timestamp DESC LIMIT %s """, (room_code, limit))
        messages = list(cursor.fetchall())
        messages.reverse()
        app.logger.debug(f"Fetched {len(messages)} messages for room {room_code}")
    except pymysql.MySQLError as e:
        app.logger.error(f"Error fetching history for room {room_code}: {e}")
    finally:
        cursor.close()
    return messages

def save_message(room_code, user_id, user_name, content):
    """Saves a message to the database."""
    # --->>> ADDED DEBUG LOG <<<---
    app.logger.debug(f"Attempting to save message to room_code: '{room_code}' (Type: {type(room_code)}) by User: {user_name}")
    # --->>> END DEBUG LOG <<<---
    conn = get_db()
    cursor = conn.cursor()
    success = False
    try:
        utc_now = datetime.datetime.utcnow()
        cursor.execute("""INSERT INTO messages (room_code, user_id, user_name, content, timestamp)
                       VALUES (%s, %s, %s, %s, %s)""", (room_code, user_id, user_name, content, utc_now))
        success = True
        app.logger.debug(f"Message saved to DB for room {room_code} by user {user_name} (ID: {user_id})")
    except pymysql.MySQLError as e:
        app.logger.error(f"Error saving message for room {room_code} by user {user_name}: {e}")
    finally:
        cursor.close()
    return success


# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect(auth=None):
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')
    sid = request.sid
    # --->>> ADDED DEBUG LOG <<<---
    app.logger.debug(f"[Connect] SID={sid}, User={user_name}({user_id}), Session Room='{room_code}'")
    # --->>> END DEBUG LOG <<<---

    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Socket connection REJECTED for SID {sid}: Missing room/user/id in session.")
        send({"error": "Invalid session. Please refresh or log in again."}, room=sid)
        disconnect(sid); return False
    # --->>> ADDED DEBUG LOG <<<---
    if room_code == "PUBLIC":
        app.logger.info(f"[Connect] Handling connection for PUBLIC chat. SID={sid}")
    # --->>> END DEBUG LOG <<<---
    if room_code not in rooms:
         # --->>> ADDED DEBUG LOG <<<---
        app.logger.info(f"[Connect] Room '{room_code}' not in tracker. Initializing/Re-activating. SID={sid}")
         # --->>> END DEBUG LOG <<<---
        rooms[room_code] = {"members": 0}

    join_room(room_code)
    rooms[room_code]["members"] += 1
    app.logger.info(f"User '{user_name}' (SID: {sid}) successfully CONNECTED to room '{room_code}'. Active members: {rooms[room_code]['members']}.")

    try: # Send History
        history = get_message_history(room_code)
        history_payload = []
        if history:
            for msg in history:
                ts_iso = msg['timestamp'].replace(tzinfo=datetime.timezone.utc).isoformat()
                history_payload.append({"name": msg['user_name'], "message": msg['content'],
                                        "timestamp": ts_iso, "user_id": msg['user_id']})
            app.logger.debug(f"Sending {len(history_payload)} history messages to {user_name} (SID: {sid}) in room {room_code}")
            socketio.emit('message_history', {'messages': history_payload}, room=sid)
        else:
             app.logger.debug(f"No message history for room {room_code}. Sending welcome message.")
             welcome_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
             # Send welcome only to the new client
             socketio.emit('message', {"name": "System", "message": "Welcome! It looks like this is the start of the conversation.",
                                    "timestamp": welcome_ts }, room=sid)
    except Exception as e:
        app.logger.error(f"Error sending history for room {room_code} to {user_name} (SID: {sid}): {e}", exc_info=True)
        socketio.emit('error', {'message': 'Error loading message history.'}, room=sid)

    # Notify others of join
    join_message_content = f"{user_name} has joined the room."
    join_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    join_message = {"name": "System", "message": join_message_content, "timestamp": join_ts}
    send(join_message, to=room_code, skip_sid=sid)
    app.logger.debug(f"Sent join notification for {user_name} to room {room_code} (skipped SID {sid})")

@socketio.on('disconnect')
def handle_disconnect():
    # ... (Disconnect logic - Keep as is) ...
    sid = request.sid
    room_code = session.get('room')
    user_name = session.get('name')
    app.logger.debug(f"Disconnect attempt: SID={sid}, User={user_name}, Room={room_code} from session.")
    if not room_code or not user_name:
        app.logger.warning(f"Socket disconnected (SID: {sid}) but no room/user found in session.")
        return
    if room_code in rooms:
        leave_room(room_code)
        if rooms[room_code]["members"] > 0: rooms[room_code]["members"] -= 1
        else: app.logger.warning(f"Disconnect in room {room_code} (SID: {sid}) detected member count zero."); rooms[room_code]["members"] = 0
        current_members = rooms[room_code]["members"]
        app.logger.info(f"User '{user_name}' (SID: {sid}) DISCONNECTED from room '{room_code}'. Remaining members: {current_members}.")
        leave_message_content = f"{user_name} has left the room."
        leave_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        leave_message = {"name": "System", "message": leave_message_content, "timestamp": leave_ts}
        send(leave_message, to=room_code)
        if current_members <= 0 and room_code != "PUBLIC":
            try: del rooms[room_code]; app.logger.info(f"Private room tracker '{room_code}' deleted.")
            except KeyError: app.logger.warning(f"Attempted to delete room tracker '{room_code}', but already gone.")
        elif current_members <= 0 and room_code == "PUBLIC":
             app.logger.info(f"Public room '{room_code}' empty, tracker retained.")
             # Optionally delete PUBLIC tracker too if preferred:
             # try: del rooms[room_code]; app.logger.info(f"Public room tracker '{room_code}' deleted.")
             # except KeyError: pass
    else:
        app.logger.warning(f"User '{user_name}' (SID: {sid}) disconnected, but room '{room_code}' not in tracker.")

@socketio.on('message')
def handle_message(data):
    sid = request.sid
    app.logger.info(f"Received 'message' event from SID {sid}. Data: {data}") # Original log

    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    # --->>> ADDED DEBUG LOG <<<---
    app.logger.debug(f"[Message] SID={sid}, User={user_name}({user_id}), Session Room='{room_code}', Received Data='{data.get('data')}'")
    # --->>> END DEBUG LOG <<<---

    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Message REJECTED from SID {sid}: Missing room/user/id in session. Data: {data}")
        socketio.emit('error', {'message': 'Cannot send message: Invalid session.'}, room=sid); return
    message_text = data.get('data')
    if not message_text or not isinstance(message_text, str) or len(message_text.strip()) == 0:
        app.logger.debug(f"Empty/invalid message REJECTED from {user_name} (SID: {sid}) in room {room_code}. Data: {data}")
        socketio.emit('error', {'message': 'Cannot send empty message.'}, room=sid); return
    if len(message_text) > 2000:
        app.logger.warning(f"Message REJECTED from {user_name} (SID: {sid}): Too long ({len(message_text)} chars).")
        socketio.emit('error', {'message': 'Message is too long (max 2000 characters).'}, room=sid); return

    message_content = message_text.strip()
    if not save_message(room_code, user_id, user_name, message_content):
        app.logger.error(f"Failed to save message to DB for room {room_code} from {user_name} (SID: {sid}).")
        socketio.emit('error', {'message': 'Failed to save message to server. Please try again.'}, room=sid); return

    timestamp_now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    content_to_broadcast = {"name": user_name, "message": message_content, "timestamp": timestamp_now, "user_id": user_id}
    send(content_to_broadcast, to=room_code)
    app.logger.debug(f"Message from '{user_name}' (SID: {sid}) in room '{room_code}' saved and broadcasted.")


# --- General Error Handlers ---
@socketio.on_error_default
def default_error_handler(e):
    sid = request.sid if request else 'N/A'
    app.logger.error(f"Unhandled SocketIO Error: {e} (SID: {sid})", exc_info=True)
    if request and sid: socketio.emit('error', {'message': f'An internal server error occurred: {e}'}, room=sid)
@app.errorhandler(404)
def page_not_found(e):
    app.logger.warning(f"404 Not Found: {request.url}")
    return render_template('404.html'), 404
@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"500 Internal Server Error: {e} at {request.url}", exc_info=True)
    db = g.pop('db', None)
    if db is not None and db.open:
        try: db.close(); app.logger.info("Database connection closed after 500 error.")
        except pymysql.MySQLError as close_err: app.logger.error(f"Error closing DB after 500 error: {close_err}")
    return render_template('500.html'), 500
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error(f"Unhandled Exception: {e} at {request.url}", exc_info=True)
    db = g.pop('db', None)
    if db is not None and db.open:
        try: db.close(); app.logger.info("Database connection closed after unhandled exception.")
        except pymysql.MySQLError as close_err: app.logger.error(f"Error closing DB after unhandled exception: {close_err}")
    return render_template('500.html'), 500

# --- Run the App ---
if __name__ == "__main__":
    app.logger.info(f"Starting Flask-SocketIO application... Debug={app.config['DEBUG']}")
    try:
        socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)),
                     debug=app.config['DEBUG'], use_reloader=app.config['DEBUG'])
    except Exception as start_error:
         app.logger.critical(f"Failed to start application: {start_error}", exc_info=True)
         sys.exit(1)

# --- END OF FILE app.py ---