# --- START OF FILE app.py ---

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
# Make sessions last longer (e.g., 31 days) by default
app.permanent_session_lifetime = datetime.timedelta(days=31)

# Configure logging
log_format = '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
log_level = logging.DEBUG if app.debug else logging.INFO
logging.basicConfig(level=log_level, format=log_format)


# SocketIO Initialization - Specify async_mode
# Recommended async modes: 'eventlet' or 'gevent'. Ensure the chosen library is installed.
# If neither is installed, Flask-SocketIO will fall back to the Flask development server (werkzeug),
# which is not suitable for production.
ASYNC_MODE = os.getenv('SOCKETIO_ASYNC_MODE', 'eventlet') # Default to eventlet
try:
    if ASYNC_MODE == 'eventlet':
        import eventlet
        eventlet.monkey_patch() # Required for eventlet
        app.logger.info("Using eventlet async mode.")
    elif ASYNC_MODE == 'gevent':
        from gevent import monkey
        monkey.patch_all() # Required for gevent
        app.logger.info("Using gevent async mode.")
    elif ASYNC_MODE == 'gevent_uwsgi':
         app.logger.info("Using gevent_uwsgi async mode (requires uWSGI with gevent plugin).")
    else: # Default/Fallback (Werkzeug - Development only!)
        ASYNC_MODE = None # Let SocketIO choose default (Werkzeug)
        app.logger.warning("No preferred async mode specified or installed (eventlet/gevent). Falling back to Werkzeug (NOT FOR PRODUCTION).")

    socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=app.logger, engineio_logger=app.logger.getChild('engineio'))

except ImportError as e:
    app.logger.error(f"Failed to import async mode library '{ASYNC_MODE}': {e}. Install it (`pip install {ASYNC_MODE}`) or choose another.")
    app.logger.warning("Falling back to Werkzeug async mode (NOT FOR PRODUCTION).")
    ASYNC_MODE = None
    socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=app.logger, engineio_logger=app.logger.getChild('engineio'))


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
    app.logger.warning("Mail configuration missing or incomplete (MAIL_USERNAME, MAIL_PASSWORD, MAIL_DEFAULT_SENDER). Password reset emails will fail.")
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
    'autocommit': True # Automatically commit after each query (simplifies basic operations)
}

if not all([DB_CONFIG['host'], DB_CONFIG['user'], DB_CONFIG['password'], DB_CONFIG['database']]):
     app.logger.critical("FATAL: Database configuration is incomplete. Check DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in .env file.")
     raise ValueError("Database configuration is incomplete. Check .env file.")

# Database Connection Management
def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g or not g.db.open:
        try:
            g.db = pymysql.connect(**DB_CONFIG)
            app.logger.debug("Database connection established.")
        except pymysql.MySQLError as e:
            app.logger.error(f"Database connection failed: {e}")
            # Fail gracefully for the request, maybe render an error page or return JSON
            raise ConnectionError(f"Could not connect to the database: {e}") from e
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None and db.open:
        try:
            db.close()
            app.logger.debug("Database connection closed.")
        except pymysql.MySQLError as e:
            app.logger.error(f"Error closing database connection: {e}")
    elif db is not None:
         app.logger.debug("Database connection was already closed or not open at teardown.")

    if error: # Log any exceptions that caused the teardown
        app.logger.error(f"App Context teardown due to error: {error}", exc_info=app.debug) # Log traceback if debug


def initialize_database():
    """Checks if the database and required tables exist, creating them if necessary."""
    try:
        # Connect without specifying database initially to create it if needed
        conn = pymysql.connect(host=DB_CONFIG['host'], user=DB_CONFIG['user'], password=DB_CONFIG['password'], port=DB_CONFIG['port'], charset=DB_CONFIG['charset'])
        cursor = conn.cursor()
        db_name = DB_CONFIG['database']
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cursor.execute(f"USE `{db_name}`;")
        app.logger.info(f"Ensured database '{db_name}' exists.")

        # Create users table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL COMMENT 'Display name for the user',
            email VARCHAR(255) UNIQUE NOT NULL COMMENT 'User login email',
            password VARCHAR(255) NOT NULL COMMENT 'Hashed password',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'User registration timestamp'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stores user account information';
        """)
        app.logger.info("Checked/Created 'users' table.")

        # Create messages table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(10) NOT NULL COMMENT 'Identifier for the chat room (e.g., 4-letter code or PUBLIC)',
            user_id INT NULL COMMENT 'Foreign key to users table, NULL if user deleted or system message',
            user_name VARCHAR(255) NOT NULL COMMENT 'User name at the time of posting (denormalized for performance)',
            content TEXT NOT NULL COMMENT 'The chat message content',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'Timestamp when the message was recorded (UTC recommended)',
            INDEX room_time_idx (room_code, timestamp) COMMENT 'Index for efficient history fetching per room'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stores chat messages for all rooms';
        """)
        app.logger.info("Checked/Created 'messages' table.")

        # Add foreign key constraint (idempotent check)
        try:
            # Check if constraint already exists
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'messages' AND COLUMN_NAME = 'user_id' AND REFERENCED_TABLE_NAME = 'users';
            """, (db_name,))
            fk_exists = cursor.fetchone()

            if not fk_exists:
                cursor.execute("""ALTER TABLE messages ADD CONSTRAINT fk_user_id FOREIGN KEY (user_id)
                               REFERENCES users(id) ON DELETE SET NULL ON UPDATE CASCADE;""")
                app.logger.info("Added foreign key constraint messages.user_id -> users.id (ON DELETE SET NULL).")
            else:
                 app.logger.debug("Foreign key constraint 'fk_user_id' already exists on messages table.")

        except pymysql.MySQLError as fk_error:
             # Catch potential errors even with the check (e.g., locking issues)
             app.logger.warning(f"Could not add/verify foreign key constraint (may already exist or DB issue): {fk_error}")

        conn.commit() # Commit schema changes
        cursor.close()
        conn.close()
        app.logger.info("Database initialization check complete.")
    except pymysql.MySQLError as e:
        app.logger.critical(f"FATAL: Database could not be initialized: {e}", exc_info=True)
        # Re-raise a more generic error or exit
        raise RuntimeError(f"FATAL: Database could not be initialized: {e}") from e

# Initialize DB on startup
try:
    initialize_database()
except RuntimeError as e:
     print(f"Critical Error during startup: {e}", file=sys.stderr)
     sys.exit(1) # Exit if DB initialization fails

# In-memory room tracker: {'ROOMCODE': {'members': count, 'sids': {sid1, sid2,...}}}
# Using a set for SIDs allows efficient add/remove/check
rooms = {}

# --- Utility Functions ---
def generate_room_code(length=4):
    """Generates a unique uppercase letter room code."""
    while True:
        # Ensure only letters are used
        code = ''.join(random.choices(string.ascii_uppercase, k=length))
        # Check against current in-memory rooms and the reserved 'PUBLIC' code
        if code not in rooms and code != "PUBLIC":
            # Optional: Add a DB check here if rooms can persist beyond server restarts
            # without being in the 'rooms' dict initially.
            # conn = get_db(); cursor = conn.cursor()
            # cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (code,))
            # if not cursor.fetchone(): return code
            # cursor.close()
            return code # Return code if unique in memory

def is_valid_room(room_code):
    """Checks if a room exists either in memory or has history in DB."""
    if room_code in rooms:
        return True
    # Check DB for history if not in active memory
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (room_code,))
        exists_in_db = cursor.fetchone() is not None
        cursor.close()
        if exists_in_db:
            # If found in DB but not memory, maybe re-initialize its tracker
            if room_code not in rooms:
                rooms[room_code] = {"members": 0, "sids": set()}
                app.logger.info(f"Re-activated room tracker for '{room_code}' based on DB history check.")
            return True
    except pymysql.MySQLError as e:
         app.logger.error(f"DB error checking room existence for {room_code}: {e}")
         # Decide behavior on DB error: maybe assume exists to avoid blocking users?
         # Or return False / raise error? For now, let's return False.
         return False
    return False

# --- Routes (Authentication & Core Pages) ---
@app.route('/')
@app.route('/home')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard')) # Already logged in

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')

        # Server-side Validation
        errors = []
        if not name: errors.append("Username is required.")
        if not email: errors.append("Email is required.")
        # Basic email format check (not foolproof, but catches common errors)
        elif '@' not in email or '.' not in email.split('@')[-1]:
            errors.append("Invalid email format.")
        if not password: errors.append("Password is required.")
        elif len(password) < 6: errors.append("Password must be at least 6 characters long.")

        if errors:
            for error in errors: flash(error, "error")
            return render_template('register.html', name=name, email=email), 400 # Bad request

        hashed_password = generate_password_hash(password)

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Check if email or username already exists (more efficient in one query if possible, but separated for clarity)
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("This email address is already registered.", "error")
                return render_template('register.html', name=name, email=email), 409 # Conflict

            cursor.execute("SELECT id FROM users WHERE name = %s", (name,))
            if cursor.fetchone():
                flash("This username is already taken. Please choose another.", "error")
                return render_template('register.html', name=name, email=email), 409 # Conflict

            # Insert new user
            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                           (name, email, hashed_password))
            # 'autocommit=True' means commit happens automatically

            # Log the user in immediately after registration
            user_id = cursor.lastrowid # Get the ID of the inserted user
            session['user_id'] = user_id
            session['name'] = name
            session['email'] = email
            session.permanent = True # Make the session persistent
            app.logger.info(f"User '{name}' (ID: {user_id}) registered successfully and logged in.")

            flash(f"Account created successfully! Welcome, {name}!", "success")
            return redirect(url_for('dashboard'))

        except pymysql.MySQLError as e:
            app.logger.error(f"Registration DB error for user {name}/{email}: {e}")
            flash("An unexpected database error occurred during registration. Please try again later.", "error")
            return render_template('register.html', name=name, email=email), 500 # Internal server error
        finally:
            if cursor: cursor.close()

    # GET request
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
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
                session.permanent = True # Make session persistent
                session['user_id'] = user['id']
                session['name'] = user['name']
                session['email'] = user['email']
                app.logger.info(f"User '{user['name']}' (ID: {user['id']}) logged in successfully.")

                # Redirect to 'next' URL if provided and safe, otherwise dashboard
                next_url = request.args.get('next')
                # Basic Open Redirect prevention: Only allow relative paths within the app
                if next_url and next_url.startswith('/') and not next_url.startswith('//') and ':' not in next_url:
                    app.logger.debug(f"Redirecting logged-in user to requested 'next' URL: {next_url}")
                    return redirect(next_url)
                else:
                    if next_url: app.logger.warning(f"Ignoring potentially unsafe 'next' URL: {next_url}")
                    return redirect(url_for('dashboard'))
            else:
                flash("Invalid email or password. Please try again.", "error")
                return render_template('index.html', email=email), 401 # Unauthorized

        except pymysql.MySQLError as e:
            app.logger.error(f"Login DB error for email {email}: {e}")
            flash("An unexpected database error occurred during login. Please try again later.", "error")
            return render_template('index.html', email=email), 500
        finally:
            if cursor: cursor.close()

    # GET request
    return render_template('index.html')

@app.route('/logout')
def logout():
    user_name = session.get('name', 'User')
    # Clear user-specific session data
    session.pop('user_id', None)
    session.pop('name', None)
    session.pop('email', None)
    session.pop('room', None) # Also clear current room on logout
    session.pop('reset_otp', None) # Clear any pending password reset info
    session.pop('reset_email', None)
    session.pop('reset_otp_expires', None)
    # session.clear() # Alternatively, clear the entire session

    flash(f"You have been successfully logged out. Goodbye, {user_name}!", "info")
    app.logger.info(f"User '{user_name}' logged out.")
    return redirect(url_for('home'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash("Please log in to access the dashboard.", "warning")
        return redirect(url_for('login', next=request.url)) # Redirect back here after login

    # Ensure user isn't stuck in a room context when visiting dashboard
    if 'room' in session:
        # Log if we are clearing a room from session here, might indicate previous flow interruption
        app.logger.debug(f"Clearing room '{session.get('room')}' from session on dashboard access for user '{session.get('name')}'")
        session.pop('room', None)

    return render_template('dashboard.html', username=session.get('name'))

@app.route('/forgetPassword', methods=['GET', 'POST'])
def forget_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            flash("Email address is required.", "error")
            return render_template('forgetPassword.html'), 400

        # Check if mail is configured before proceeding
        if not all([app.config.get('MAIL_USERNAME'), app.config.get('MAIL_PASSWORD'), app.config.get('MAIL_DEFAULT_SENDER')]):
             flash("Password reset emails are currently disabled due to server configuration. Please contact support.", "error")
             app.logger.error("Password reset requested but mail is not configured.")
             return render_template('forgetPassword.html', email=email), 503 # Service unavailable

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                otp = ''.join(random.choices(string.digits, k=6))
                expiry_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)

                # Store OTP info securely in session
                session['reset_otp'] = generate_password_hash(otp) # Store hash of OTP
                session['reset_email'] = email
                session['reset_otp_expires'] = expiry_time.isoformat()
                session.permanent = True # Ensure session persists

                try:
                    message_body = f"Your OTP to reset your ChatApp password is: {otp}\n\nThis OTP is valid for 10 minutes."
                    message = Message(subject="Your ChatApp Password Reset OTP", recipients=[email], body=message_body)
                    mail.send(message)
                    app.logger.info(f"Password reset OTP sent to {email}")
                    flash("An OTP has been sent to your email address. Please check your inbox (and spam folder). It's valid for 10 minutes.", "info")
                    return redirect(url_for('reset_password'))
                except Exception as e:
                    app.logger.error(f"Failed to send OTP email to {email}: {e}", exc_info=True)
                    flash("Could not send the OTP email due to a server error. Please try again later or contact support.", "error")
                    # Clear potentially sensitive session data on failure
                    session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                    return render_template('forgetPassword.html', email=email), 500
            else:
                # IMPORTANT: Don't reveal if the email exists or not for security
                app.logger.warning(f"Password reset requested for non-existent or incorrect email: {email}")
                flash("If an account with that email exists, an OTP has been sent. Please check your inbox (and spam folder).", "info")
                # Still redirect to reset page to prevent email enumeration
                return redirect(url_for('reset_password'))
        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password forget request for {email}: {e}")
            flash("A database error occurred. Please try again.", "error")
            return render_template('forgetPassword.html', email=email), 500
        finally:
            if cursor: cursor.close()

    # GET request
    return render_template('forgetPassword.html')

@app.route('/resetPassword', methods=['GET', 'POST'])
def reset_password():
    # Check if the necessary session variables exist
    reset_email = session.get('reset_email')
    otp_hash = session.get('reset_otp')
    otp_expires_iso = session.get('reset_otp_expires')

    if not all([reset_email, otp_hash, otp_expires_iso]):
        flash("Invalid password reset request or session expired. Please request a new OTP.", "warning")
        return redirect(url_for('forget_password'))

    try:
        otp_expires = datetime.datetime.fromisoformat(otp_expires_iso)
        if datetime.datetime.utcnow() > otp_expires:
            # Clear expired OTP data from session
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

        # Validation
        errors = []
        if not otp_entered: errors.append("OTP is required.")
        if not new_password: errors.append("New password is required.")
        if not confirm_password: errors.append("Confirm password is required.")
        if new_password != confirm_password: errors.append("The new passwords do not match.")
        elif len(new_password) < 6: errors.append("Password must be at least 6 characters long.")
        # Check OTP (compare entered OTP against the stored hash)
        if not otp_entered or not check_password_hash(otp_hash, otp_entered):
             errors.append("The OTP entered is invalid or has expired.")

        if errors:
            for error in errors: flash(error, "error")
            return render_template('resetPassword.html'), 400 # Bad request

        # If validation passes, update password
        new_hashed_password = generate_password_hash(new_password)
        conn = get_db()
        cursor = conn.cursor()
        try:
            rows_affected = cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_hashed_password, reset_email))
            # autocommit handles commit

            if rows_affected == 1:
                app.logger.info(f"Password successfully reset for email: {reset_email}")
                # Clear reset info from session after successful reset
                session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                flash("Your password has been reset successfully. Please log in with your new password.", "success")
                return redirect(url_for('login'))
            else:
                 # This case should be rare if email/OTP was validated, but handle defensively
                 app.logger.error(f"Password reset failed for {reset_email}: user not found in DB during update, despite valid OTP session.")
                 flash("An unexpected error occurred while updating your password. Please try again.", "error")
                 return render_template('resetPassword.html'), 500

        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password reset update for {reset_email}: {e}")
            flash("An error occurred while resetting the password. Please try again.", "error")
            return render_template('resetPassword.html'), 500
        finally:
            if cursor: cursor.close()

    # GET request - Render the form
    return render_template('resetPassword.html')


# --- Chat Room Logic & Routes ---
@app.route('/join_room', methods=['POST'])
def join_room_route():
    if 'user_id' not in session:
        flash("Please log in first.", "warning")
        return redirect(url_for('login', next=url_for('dashboard'))) # Try redirecting back to dash

    name = session.get("name")
    user_id = session.get("user_id") # Get user_id as well
    room_code_input = request.form.get("room_code", "").strip().upper()
    join_room_action = request.form.get("join_room")
    create_room_action = request.form.get("create_room")

    target_room_code = None

    if join_room_action:
        # Validate input room code
        if not room_code_input:
            flash("Please enter a room code to join.", "error")
            return redirect(url_for('dashboard'))
        if len(room_code_input) != 4 or not room_code_input.isalpha():
            flash("Invalid room code format (must be exactly 4 letters).", "error")
            return redirect(url_for('dashboard'))
        if room_code_input == "PUBLIC":
             flash("Cannot join 'PUBLIC' room this way. Use the Public Chat link.", "warning")
             return redirect(url_for('dashboard'))

        # Check if room exists (memory or DB)
        if not is_valid_room(room_code_input):
             flash(f"Room code '{room_code_input}' does not exist or is invalid.", "error")
             return redirect(url_for('dashboard'))

        target_room_code = room_code_input

    elif create_room_action:
        new_code = generate_room_code()
        # Initialize room tracker immediately upon creation
        rooms[new_code] = {"members": 0, "sids": set()}
        target_room_code = new_code
        app.logger.info(f"User '{name}' (ID: {user_id}) created new private room '{target_room_code}'.")
        # Optional: Maybe save a "Room Created" system message to DB?
        # save_message(target_room_code, None, "System", f"Room created by {name}.")
    else:
        # Should not happen with the form structure, but handle defensively
        flash("Invalid action specified.", "error")
        return redirect(url_for('dashboard'))

    # Set the room in the session and redirect to the chat interface
    session['room'] = target_room_code
    app.logger.info(f"User '{name}' (ID: {user_id}) attempting to enter room '{target_room_code}'.")

    # Redirect based on room type (although currently only private rooms are handled here)
    if target_room_code == "PUBLIC":
         # This path shouldn't be reachable due to checks above, but for robustness:
         return redirect(url_for('public_chat'))
    else:
         return redirect(url_for('room')) # Redirect to the private room view

@app.route('/room') # Route for PRIVATE chat rooms
def room():
    if 'user_id' not in session:
        flash("Please log in to access chat rooms.", "warning")
        return redirect(url_for('login', next=request.url))

    room_code = session.get('room')
    user_name = session.get('name')

    # Validate necessary session info
    if not room_code or not user_name:
        flash("No active room selected or user session invalid. Please join or create one from the dashboard.", "warning")
        return redirect(url_for('dashboard'))

    # Prevent direct access to PUBLIC via this route
    if room_code == "PUBLIC":
         flash("Please use the dedicated Public Chat link from the dashboard.", "warning")
         session.pop('room', None) # Clear invalid room context
         return redirect(url_for('dashboard'))

    # Validate if the room is still valid (exists in memory or DB)
    if not is_valid_room(room_code):
         flash(f"The room '{room_code}' is no longer active or valid.", "error")
         session.pop('room', None) # Clear invalid room context
         return redirect(url_for('dashboard'))

    # Render the private chat room template
    app.logger.debug(f"Rendering private chat room '{room_code}' for user '{user_name}'.")
    return render_template('chatroom.html', room_code=room_code, username=user_name)

@app.route('/public_chat') # Route specifically for the PUBLIC chat room
def public_chat():
    if 'user_id' not in session:
        flash("Please log in to view the public chat.", "warning")
        return redirect(url_for('login', next=request.url))

    user_name = session.get('name')
    if not user_name: # Should not happen if user_id is set, but check
         flash("User information missing. Please log in again.", "error")
         return redirect(url_for('login'))

    # Set room context specifically to PUBLIC
    session['room'] = "PUBLIC"

    # Ensure PUBLIC room tracker exists in memory
    if "PUBLIC" not in rooms:
        rooms["PUBLIC"] = {"members": 0, "sids": set()}
        app.logger.info("Initialized PUBLIC room tracker in memory.")

    app.logger.info(f"User '{user_name}' entering public chat (room 'PUBLIC').")
    return render_template('public_chat.html', username=user_name) # No room_code needed here


# --- Database Helper Functions for Chat ---
def get_message_history(room_code, limit=50):
    """Fetches the last 'limit' messages for a given room_code from the database, ordered oldest to newest."""
    app.logger.debug(f"Fetching history for room_code: '{room_code}', Limit: {limit}")
    messages = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Fetch in reverse chronological order (latest first) then reverse in Python
        # This is often more efficient in SQL with an index on (room_code, timestamp DESC)
        # Alternatively, use a subquery or window function if DB supports it well.
        query = """
            SELECT user_id, user_name, content, timestamp
            FROM messages
            WHERE room_code = %s
            ORDER BY timestamp DESC
            LIMIT %s
        """
        cursor.execute(query, (room_code, limit))
        # Fetchall and then reverse the list
        messages = list(cursor.fetchall())
        messages.reverse() # Order from oldest to newest for display
        app.logger.debug(f"Fetched {len(messages)} messages for room '{room_code}'")
    except pymysql.MySQLError as e:
        app.logger.error(f"Error fetching history for room '{room_code}': {e}")
    except ConnectionError as e:
         app.logger.error(f"DB Connection error fetching history for room '{room_code}': {e}")
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
    return messages

def save_message(room_code, user_id, user_name, content):
    """Saves a chat message to the database."""
    app.logger.debug(f"Attempting to save message to room '{room_code}' by User: {user_name} (ID: {user_id})")
    success = False
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Use UTC time for timestamps
        utc_now = datetime.datetime.utcnow()
        query = """
            INSERT INTO messages (room_code, user_id, user_name, content, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(query, (room_code, user_id, user_name, content, utc_now))
        # autocommit=True handles the commit
        success = True
        app.logger.debug(f"Message saved to DB for room '{room_code}'")
    except pymysql.MySQLError as e:
        app.logger.error(f"Error saving message for room '{room_code}' by user {user_name}: {e}")
        # Rollback might be needed if autocommit was false, but not here.
    except ConnectionError as e:
         app.logger.error(f"DB Connection error saving message for room '{room_code}': {e}")
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
    return success


# --- SocketIO Event Handlers ---

@socketio.on('connect')
def handle_connect():
    """Handles new client connections."""
    sid = request.sid
    # Retrieve user and room info from session established during HTTP request
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Connect] SID={sid} attempting connection. Session state: User='{user_name}'(ID:{user_id}), Room='{room_code}'")

    # Validate session data is present for authenticated chat access
    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Socket connection REJECTED for SID {sid}: Missing room/user/id in session. User might need to re-authenticate or select a room.")
        # Send error message specifically to this client and disconnect
        socketio.emit('error', {'message': 'Invalid session. Please refresh or log in again.'}, room=sid)
        disconnect(sid)
        return False # Indicate connection failure

    # Ensure room tracker exists (especially important if server restarted)
    if room_code not in rooms:
        app.logger.info(f"[Connect] Room '{room_code}' not in memory tracker. Initializing/Re-activating. SID={sid}")
        # If it exists in DB (checked by is_valid_room implicitly if user got here), initialize
        # If it's a newly created room, it should have been added in join_room_route
        rooms[room_code] = {"members": 0, "sids": set()}
        # Double-check validity if it wasn't found? Could indicate an issue.
        if not is_valid_room(room_code) and room_code != "PUBLIC": # Don't need DB check for PUBLIC
             app.logger.error(f"Room '{room_code}' not found in memory OR DB history during connect for SID {sid}. Disconnecting.")
             socketio.emit('error', {'message': f'Room {room_code} is invalid or no longer exists.'}, room=sid)
             disconnect(sid); return False


    # Join the SocketIO room
    join_room(room_code)
    rooms[room_code]["members"] += 1
    rooms[room_code]["sids"].add(sid) # Track the SID
    app.logger.info(f"User '{user_name}' (SID: {sid}) successfully CONNECTED to room '{room_code}'. Active members: {rooms[room_code]['members']}.")

    # Send message history to the connecting client
    try:
        history = get_message_history(room_code)
        history_payload = []
        if history:
            for msg in history:
                # Ensure timestamp is timezone-aware UTC and in ISO format for JS
                ts_aware = msg['timestamp'].replace(tzinfo=datetime.timezone.utc)
                history_payload.append({
                    "name": msg['user_name'],
                    "message": msg['content'],
                    "timestamp": ts_aware.isoformat(), # Use ISO format
                    "user_id": msg['user_id']
                })
            app.logger.debug(f"Sending {len(history_payload)} history messages to {user_name} (SID: {sid}) in room '{room_code}'")
        else:
             app.logger.debug(f"No message history found for room '{room_code}'.")
             # Optionally send a specific 'history_empty' event or rely on client JS

        # Emit history (even if empty, client JS expects it)
        socketio.emit('message_history', {'messages': history_payload}, room=sid)

        # If history was empty, send a welcome message (avoids double welcome if history exists)
        if not history_payload:
             welcome_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
             socketio.emit('message', {
                  "name": "System",
                  "message": "Welcome! It looks like this is the start of the conversation.",
                  "timestamp": welcome_ts,
                  "user_id": None, # System messages have no user ID
                  "isSystem": True # Explicitly flag system messages if needed by client
             }, room=sid)


    except Exception as e:
        app.logger.error(f"Error retrieving/sending history for room '{room_code}' to {user_name} (SID: {sid}): {e}", exc_info=True)
        socketio.emit('error', {'message': 'Error loading message history.'}, room=sid)

    # Notify other users in the room about the new joiner
    join_message_content = f"{user_name} has joined the room."
    join_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    join_message = {
        "name": "System",
        "message": join_message_content,
        "timestamp": join_ts,
        "user_id": None,
        "isSystem": True
        }
    # Send to the room, excluding the user who just joined (skip_sid)
    socketio.emit('message', join_message, to=room_code, skip_sid=sid)
    app.logger.debug(f"Sent join notification for {user_name} to room '{room_code}' (skipped SID {sid})")
    return True # Indicate successful connection


@socketio.on('disconnect')
def handle_disconnect():
    """Handles client disconnections."""
    sid = request.sid
    # Retrieve user/room info from session (might be cleared already if logout initiated disconnect)
    # It's safer to find the room based on the SID from our tracker
    disconnected_room = None
    user_name = "Unknown User" # Default if session is gone

    for room_code, room_data in rooms.items():
        if sid in room_data.get("sids", set()):
            disconnected_room = room_code
            # Try to get username from session if possible, for logging/message
            user_name = session.get('name', user_name) # Use session name if available
            break # Found the room

    app.logger.debug(f"Disconnect attempt: SID={sid}. Found in room: '{disconnected_room}'. User from session (if available): '{user_name}'")

    if disconnected_room and disconnected_room in rooms:
        # Leave the SocketIO room (optional but good practice)
        leave_room(disconnected_room)

        # Update our tracker
        if sid in rooms[disconnected_room]["sids"]:
             rooms[disconnected_room]["sids"].remove(sid)
        rooms[disconnected_room]["members"] = max(0, rooms[disconnected_room]["members"] - 1) # Prevent negative count

        current_members = rooms[disconnected_room]["members"]
        app.logger.info(f"User '{user_name}' (SID: {sid}) DISCONNECTED from room '{disconnected_room}'. Remaining members: {current_members}.")

        # Notify remaining users
        leave_message_content = f"{user_name} has left the room."
        leave_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        leave_message = {
            "name": "System",
            "message": leave_message_content,
            "timestamp": leave_ts,
            "user_id": None,
            "isSystem": True
            }
        # Send to everyone remaining in the room
        socketio.emit('message', leave_message, to=disconnected_room)

        # Clean up empty private room tracker (keep PUBLIC tracker)
        if current_members <= 0 and disconnected_room != "PUBLIC":
            try:
                del rooms[disconnected_room]
                app.logger.info(f"Private room tracker '{disconnected_room}' deleted as it became empty.")
            except KeyError:
                app.logger.warning(f"Attempted to delete empty room tracker '{disconnected_room}', but it was already gone.")
        elif current_members <= 0 and disconnected_room == "PUBLIC":
             app.logger.info(f"Public room '{disconnected_room}' is empty, tracker retained.")

    else:
        app.logger.warning(f"Socket disconnected (SID: {sid}), but SID was not found in any active room tracker.")


@socketio.on('message')
def handle_message(data):
    """Handles incoming chat messages from clients."""
    sid = request.sid
    # Ensure data is a dictionary and contains 'data' key
    if not isinstance(data, dict) or 'data' not in data:
        app.logger.warning(f"Invalid message format received from SID {sid}. Data: {data}")
        socketio.emit('error', {'message': 'Invalid message format.'}, room=sid); return

    message_text = str(data.get('data', '')) # Get message text, ensure string

    # Retrieve user/room info from session
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Message] SID={sid}, User={user_name}({user_id}), Room='{room_code}', Received Data='{message_text[:50]}...'") # Log only start of msg

    # Validate session and message content
    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Message REJECTED from SID {sid}: Missing room/user/id in session. Data: {message_text[:50]}...")
        socketio.emit('error', {'message': 'Cannot send message: Invalid session.'}, room=sid); return

    # Check if SID is actually in the room it claims to be in via session (consistency check)
    if room_code not in rooms or sid not in rooms[room_code].get("sids", set()):
         app.logger.error(f"Message REJECTED from SID {sid}: User '{user_name}' session indicates room '{room_code}', but SID not found in that room's tracker.")
         socketio.emit('error', {'message': 'Room connection mismatch. Please refresh.'}, room=sid)
         # Consider disconnecting the user here as state is inconsistent
         # disconnect(sid)
         return

    message_content = message_text.strip()
    if not message_content:
        app.logger.debug(f"Empty message REJECTED from {user_name} (SID: {sid}) in room '{room_code}'.")
        # Optionally send feedback, or just ignore
        # socketio.emit('error', {'message': 'Cannot send empty message.'}, room=sid);
        return

    # Message length limit
    MAX_MSG_LENGTH = 2000
    if len(message_content) > MAX_MSG_LENGTH:
        app.logger.warning(f"Message REJECTED from {user_name} (SID: {sid}): Too long ({len(message_content)} chars). Limit: {MAX_MSG_LENGTH}")
        socketio.emit('error', {'message': f'Message is too long (max {MAX_MSG_LENGTH} characters).'}, room=sid); return

    # Save the message to the database
    if not save_message(room_code, user_id, user_name, message_content):
        app.logger.error(f"Failed to save message to DB for room '{room_code}' from {user_name} (SID: {sid}).")
        socketio.emit('error', {'message': 'Failed to save message to server. Please try again.'}, room=sid); return

    # Broadcast the message to the room
    timestamp_now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    content_to_broadcast = {
        "name": user_name,
        "message": message_content, # Send the sanitized (stripped) message
        "timestamp": timestamp_now,
        "user_id": user_id,
        "isSystem": False # Regular user message
        }
    socketio.emit('message', content_to_broadcast, to=room_code)
    app.logger.debug(f"Message from '{user_name}' (SID: {sid}) in room '{room_code}' saved and broadcasted.")


# --- General Error Handlers ---
@socketio.on_error_default
def default_error_handler(e):
    """Log unhandled SocketIO errors."""
    sid = request.sid if request else 'N/A'
    app.logger.error(f"Unhandled SocketIO Error: {e} (SID: {sid})", exc_info=True)
    # Optionally emit a generic error back to the client if the connection is still active
    # if request and sid:
    #     try: socketio.emit('error', {'message': f'An internal server error occurred.'}, room=sid)
    #     except Exception as emit_err: app.logger.error(f"Error emitting error message to SID {sid}: {emit_err}")

@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 Not Found errors."""
    app.logger.warning(f"404 Not Found: {request.url} (Referrer: {request.referrer})")
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Handle 500 Internal Server errors."""
    app.logger.error(f"500 Internal Server Error handling request for {request.url}: {e}", exc_info=True)
    # Ensure DB connection is closed in case the error occurred mid-request handling
    close_db(e)
    return render_template('500.html'), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle other uncaught exceptions."""
    # Skip handling for 404 and 500 as they have specific handlers
    if isinstance(e, (pymysql.MySQLError, ConnectionError)):
        app.logger.error(f"Database Exception handling request for {request.url}: {e}", exc_info=True)
        # Close DB connection if it was a DB error causing this
        close_db(e)
        # Render a user-friendly error page or return JSON
        flash("A database error occurred. Please try again later or contact support.", "error")
        # Decide where to redirect or what to render
        # Maybe redirect home if it's severe? Or show 500 page?
        return render_template('500.html'), 500 # Use 500 page for DB errors
    elif hasattr(e, 'code') and e.code == 404:
         # Let the 404 handler manage this
         return page_not_found(e)
    elif hasattr(e, 'code') and e.code == 500:
         # Let the 500 handler manage this
         return internal_server_error(e)
    else:
        # Log all other unexpected exceptions
        app.logger.error(f"Unhandled Exception handling request for {request.url}: {e}", exc_info=True)
        close_db(e) # Close db just in case
        return render_template('500.html'), 500


# --- Run the App ---
if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    host = os.getenv('HOST', '0.0.0.0') # Listen on all interfaces by default
    debug_mode = app.config['DEBUG']

    app.logger.info(f"Starting Flask-SocketIO application...")
    app.logger.info(f" ---> Host: {host}")
    app.logger.info(f" ---> Port: {port}")
    app.logger.info(f" ---> Debug Mode: {debug_mode}")
    app.logger.info(f" ---> Async Mode: {ASYNC_MODE or 'Werkzeug (Default - Dev Only!)'}")

    try:
        # Use `socketio.run` which handles starting the correct server based on async_mode
        socketio.run(app, host=host, port=port, debug=debug_mode, use_reloader=debug_mode)
    except Exception as start_error:
         app.logger.critical(f"Failed to start application: {start_error}", exc_info=True)
         sys.exit(1)

# --- END OF FILE app.py ---