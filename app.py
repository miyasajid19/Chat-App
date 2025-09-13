# --- START OF FILE app.py ---

# Monkey patch MUST be done at the very beginning, before any other imports
import os
ASYNC_MODE = os.getenv('SOCKETIO_ASYNC_MODE', 'eventlet')  # Check env first

if ASYNC_MODE == 'eventlet':
    import eventlet
    eventlet.monkey_patch()
elif ASYNC_MODE == 'gevent':
    from gevent import monkey
    monkey.patch_all()

import random
import string
import pymysql
import datetime
import pytz # Added for timezone awareness
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, session,
    flash, url_for, g, jsonify, abort
)
from flask_socketio import (
    SocketIO, join_room, leave_room, send, disconnect, emit # Added emit
)
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
import logging
import sys # For sys.exit
from functools import wraps

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

# SocketIO Initialization
socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=app.logger if hasattr(app, 'logger') else None, 
                    engineio_logger=app.logger.getChild('engineio') if hasattr(app, 'logger') else None)

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
    print("WARNING: Mail configuration missing or incomplete (MAIL_USERNAME, MAIL_PASSWORD, MAIL_DEFAULT_SENDER). Password reset emails will fail.")
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
     print("FATAL: Database configuration is incomplete. Check DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in .env file.")
     raise ValueError("Database configuration is incomplete. Check .env file.")

# Database Connection Management
def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g or not g.db.open:
        try:
            g.db = pymysql.connect(**DB_CONFIG)
            print("Database connection established.")
        except pymysql.MySQLError as e:
            print(f"Database connection failed: {e}")
            raise ConnectionError(f"Could not connect to the database: {e}") from e
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None and db.open:
        try:
            db.close()
            print("Database connection closed.")
        except pymysql.MySQLError as e:
            print(f"Error closing database connection: {e}")
    elif db is not None:
         print("Database connection was already closed or not open at teardown.")

    if error: # Log any exceptions that caused the teardown
        print(f"App Context teardown due to error: {error}")


def initialize_database():
    """Checks if the database and required tables exist, creating them if necessary."""
    print("Starting database initialization check...")
    try:
        # Connect without specifying database initially to create it if needed
        conn = pymysql.connect(host=DB_CONFIG['host'], user=DB_CONFIG['user'], password=DB_CONFIG['password'], port=DB_CONFIG['port'], charset=DB_CONFIG['charset'])
        cursor = conn.cursor()
        db_name = DB_CONFIG['database']
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cursor.execute(f"USE `{db_name}`;")
        print(f"Ensured database '{db_name}' exists.")

        # Create users table with is_admin column
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL COMMENT 'Display name for the user',
            email VARCHAR(255) UNIQUE NOT NULL COMMENT 'User login email',
            password VARCHAR(255) NOT NULL COMMENT 'Hashed password',
            is_admin BOOLEAN DEFAULT FALSE NOT NULL COMMENT 'Flag indicating admin privileges',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT 'User registration timestamp'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stores user account information';
        """)
        print("Checked/Created 'users' table.")

        # Ensure is_admin column exists if table already existed (idempotent)
        try:
            cursor.execute("SELECT is_admin FROM users LIMIT 1;")
            print("'is_admin' column already exists in users table.")
        except pymysql.err.OperationalError as e:
             if "Unknown column 'is_admin'" in str(e):
                 print("Adding missing 'is_admin' column to existing 'users' table.")
                 cursor.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE NOT NULL COMMENT 'Flag indicating admin privileges';")
             else:
                  raise # Re-raise other operational errors

        # Create messages table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(10) NOT NULL COMMENT 'Identifier for the chat room (e.g., 4-letter code or PUBLIC)',
            user_id INT NULL COMMENT 'Foreign key to users table, NULL if user deleted or system message',
            user_name VARCHAR(255) NOT NULL COMMENT 'User name at the time of posting (denormalized for performance)',
            content TEXT NOT NULL COMMENT 'The chat message content',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'Timestamp when the message was recorded (UTC)',
            INDEX room_time_idx (room_code, timestamp) COMMENT 'Index for efficient history fetching per room'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stores chat messages for all rooms';
        """)
        print("Checked/Created 'messages' table.")

        # Add foreign key constraint for messages table (idempotent check)
        try:
            cursor.execute("""
                SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'messages' AND COLUMN_NAME = 'user_id' AND REFERENCED_TABLE_NAME = 'users';
            """, (db_name,))
            fk_exists = cursor.fetchone()

            if not fk_exists:
                cursor.execute("""ALTER TABLE messages ADD CONSTRAINT fk_user_id FOREIGN KEY (user_id)
                               REFERENCES users(id) ON DELETE SET NULL ON UPDATE CASCADE;""")
                print("Added foreign key constraint messages.user_id -> users.id (ON DELETE SET NULL).")
            else:
                 print("Foreign key constraint 'fk_user_id' already exists on messages table.")

        except pymysql.MySQLError as fk_error:
             print(f"Could not add/verify foreign key constraint (may already exist or DB issue): {fk_error}")

        conn.commit() # Commit schema changes
        print("Database schema initialization complete.")

    except pymysql.MySQLError as e:
        print(f"FATAL: Database schema could not be initialized: {e}")
        raise RuntimeError(f"FATAL: Database schema could not be initialized: {e}") from e
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn.open: conn.close()


def ensure_admin_user():
    """Creates or updates the specified admin user."""
    # !!! SECURITY WARNING: Storing credentials like this is NOT recommended for production.
    # Consider a setup script or environment variables only.
    # Prefer explicit environment variables (do not rely on undefined `env` object)
    admin_email = os.getenv('ADMIN_EMAIL', '').strip().lower() or None
    admin_pass = os.getenv('ADMIN_PASSWORD') or None
    # Default admin name to provided ADMIN_NAME or derive from email local-part or fallback to 'Administrator'
    admin_name = (os.getenv('ADMIN_NAME') or (admin_email.split('@')[0] if admin_email else 'Administrator')).strip()

    # Basic validation: if required credentials are missing, skip creating/updating admin and log a warning.
    if not admin_email or not admin_pass:
        print("WARNING: ADMIN_EMAIL and/or ADMIN_PASSWORD not set. Skipping admin user creation. Set these in the environment to enable automatic admin provisioning.")
        return

    print(f"Ensuring admin user '{admin_email}' exists and is configured...")
    try:
        with app.app_context():
            conn = get_db()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT id, password, is_admin FROM users WHERE email = %s", (admin_email,))
                admin_user = cursor.fetchone()

                hashed_pass = generate_password_hash(admin_pass)

                if not admin_user:
                    cursor.execute(
                        "INSERT INTO users (name, email, password, is_admin) VALUES (%s, %s, %s, TRUE)",
                        (admin_name, admin_email, hashed_pass)
                    )
                    print(f"Created admin user '{admin_email}' with the specified password.")
                else:
                    needs_update = False
                    update_sql = "UPDATE users SET"
                    update_params = []
                    if not check_password_hash(admin_user['password'], admin_pass):
                        update_sql += " password = %s,"
                        update_params.append(hashed_pass)
                        print(f"Updating password for admin user '{admin_email}'.")
                        needs_update = True
                    if not admin_user['is_admin']:
                        update_sql += " is_admin = TRUE,"
                        print(f"Setting admin status for user '{admin_email}'.")
                        needs_update = True

                    if needs_update:
                        update_sql = update_sql.rstrip(',') # Remove trailing comma
                        update_sql += " WHERE id = %s"
                        update_params.append(admin_user['id'])
                        cursor.execute(update_sql, tuple(update_params))
                    else:
                        print(f"Admin user '{admin_email}' already exists and is correctly configured.")

            except pymysql.MySQLError as e:
                print(f"Database error while ensuring admin user '{admin_email}': {e}")
            finally:
                 if cursor: cursor.close()
                 # Connection closed by teardown_appcontext

    except Exception as e:
         print(f"Failed to run ensure_admin_user within app context: {e}")


# Initialize DB and Admin User on startup
try:
    initialize_database()
    ensure_admin_user()
except (RuntimeError, ConnectionError) as e:
     print(f"Critical Error during startup: {e}", file=sys.stderr)
     sys.exit(1)


# In-memory room tracker: {'ROOMCODE': {'members': count, 'users': {user_id1, user_id2,...}}}
rooms = {}
# Simple SID mappings (primarily for disconnect handling)
# Caution: Only holds users *currently* connected via SocketIO
sid_to_info = {}

# --- Utility Functions ---
def generate_room_code(length=4):
    """Generates a unique uppercase letter room code."""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=length))
        # Check memory AND if code could clash with database room name convention (like "PUBLIC")
        if code not in rooms and code != "PUBLIC":
            # Minimal check in DB (optional, as rooms activate on join anyway)
            # This is just to reduce chance of collision if server restarts often
            # You might omit the DB check here if performance is critical
            # if not _room_exists_in_db(code): # Assuming a lightweight check function
                return code
    # Note: If _room_exists_in_db is added, handle its exceptions

def is_valid_room(room_code):
    """Checks if a room exists either in memory OR has history in DB."""
    if not room_code or not isinstance(room_code, str) or len(room_code) > 10 : # Basic validation
        app.logger.warning(f"Invalid room_code format checked: '{room_code}'")
        return False

    if room_code in rooms:
        return True

    # If not in memory, check DB history to see if it *could* be reactivated
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (room_code,))
        exists_in_db = cursor.fetchone() is not None
        cursor.close()

        if exists_in_db:
            # IMPORTANT: Only initialize room in memory if someone actually *joins* it.
            # This prevents filling memory with all historical rooms on simple checks.
            # We'll initialize the dict in `handle_connect` if needed.
            app.logger.debug(f"Room '{room_code}' exists in DB history but is not active in memory.")
            return True # Indicates it's a valid potential room code
    except pymysql.MySQLError as e:
         app.logger.error(f"DB error checking room existence for {room_code}: {e}")
         return False # Treat DB error as room not valid for now
    except ConnectionError as e:
         app.logger.error(f"DB Connection error checking room existence for {room_code}: {e}")
         return False

    app.logger.debug(f"Room code '{room_code}' not found in memory or DB history.")
    return False


# --- Decorators ---
def login_required(f):
    """Decorator to ensure user is logged in."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to ensure user is logged in AND is an admin."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login', next=request.url))
        if not session.get('is_admin'):
            flash("You do not have permission to access the admin area.", "danger")
            app.logger.warning(f"Unauthorized admin access attempt by user: {session.get('name')} (ID: {session.get('user_id')}) to {request.url}")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


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
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')

        errors = []
        if not name: errors.append("Username is required.")
        if not email: errors.append("Email is required.")
        elif '@' not in email or '.' not in email.split('@')[-1]:
            errors.append("Invalid email format.")
        if not password: errors.append("Password is required.")
        elif len(password) < 6: errors.append("Password must be at least 6 characters long.")

        if errors:
            for error in errors: flash(error, "error")
            return render_template('register.html', name=name, email=email), 400

        hashed_password = generate_password_hash(password)

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("This email address is already registered.", "error")
                return render_template('register.html', name=name, email=email), 409

            cursor.execute("SELECT id FROM users WHERE name = %s", (name,))
            if cursor.fetchone():
                flash("This username is already taken. Please choose another.", "error")
                return render_template('register.html', name=name, email=email), 409

            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                           (name, email, hashed_password))

            user_id = cursor.lastrowid
            session['user_id'] = user_id
            session['name'] = name
            session['email'] = email
            session['is_admin'] = False # New users are not admins
            session.permanent = True # Remember session across browser closes
            app.logger.info(f"User '{name}' (ID: {user_id}) registered successfully and logged in.")

            flash(f"Account created successfully! Welcome, {name}!", "success")
            return redirect(url_for('dashboard'))

        except pymysql.MySQLError as e:
            app.logger.error(f"Registration DB error for user {name}/{email}: {e}")
            flash("An unexpected database error occurred during registration. Please try again later.", "error")
            return render_template('register.html', name=name, email=email), 500
        except ConnectionError as e:
             app.logger.error(f"Registration DB Connection error for user {name}/{email}: {e}")
             flash("Could not connect to the database during registration. Please try again later.", "error")
             return render_template('register.html', name=name, email=email), 503
        finally:
            if cursor: cursor.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        remember = request.form.get('remember') # Check for remember me

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template('index.html', email=email), 400

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name, email, password, is_admin FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user['password'], password):
                # Set session permanence based on checkbox
                session.permanent = (remember == 'on')
                session['user_id'] = user['id']
                session['name'] = user['name']
                session['email'] = user['email']
                session['is_admin'] = user['is_admin']
                app.logger.info(f"User '{user['name']}' (ID: {user['id']}, Admin: {user['is_admin']}) logged in. Permanent session: {session.permanent}")

                next_url = request.args.get('next')
                # Improved URL validation (relative path or same host)
                if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                     try:
                         # Ensure the next_url doesn't point to external domains via clever path manipulation
                         # This check isn't foolproof but helps against basic open redirect
                         if url_for('home', _external=False) in url_for(next_url[1:], _external=False):
                              app.logger.debug(f"Redirecting logged-in user to safe 'next' URL: {next_url}")
                              return redirect(next_url)
                         else:
                              app.logger.warning(f"Ignoring potentially unsafe 'next' URL structure: {next_url}")
                     except Exception: # If url_for fails for next_url, it's likely invalid
                         app.logger.warning(f"Ignoring invalid 'next' URL: {next_url}")

                return redirect(url_for('dashboard')) # Default redirect

            else:
                flash("Invalid email or password. Please try again.", "error")
                return render_template('index.html', email=email), 401

        except pymysql.MySQLError as e:
            app.logger.error(f"Login DB error for email {email}: {e}")
            flash("An unexpected database error occurred during login. Please try again later.", "error")
            return render_template('index.html', email=email), 500
        except ConnectionError as e:
             app.logger.error(f"Login DB Connection error for email {email}: {e}")
             flash("Could not connect to the database during login. Please try again later.", "error")
             return render_template('index.html', email=email), 503
        finally:
            if cursor: cursor.close()

    return render_template('index.html')

@app.route('/logout')
def logout():
    user_name = session.get('name', 'User')
    # Store details before clearing session, for logging/flash message
    user_id = session.get('user_id')

    # Clear user-specific session data
    session.pop('user_id', None)
    session.pop('name', None)
    session.pop('email', None)
    session.pop('room', None)
    session.pop('is_admin', None)
    # Clear potentially sensitive password reset state
    session.pop('reset_otp', None)
    session.pop('reset_email', None)
    session.pop('reset_otp_expires', None)

    # You might want session.clear() to remove everything, but Flask keeps some internals.
    # The above should be sufficient.

    flash(f"You have been successfully logged out. Goodbye, {user_name}!", "info")
    app.logger.info(f"User '{user_name}' (ID: {user_id}) logged out.")
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Clean up potential leftover 'room' from previous session state
    if 'room' in session:
        app.logger.debug(f"Clearing lingering room '{session.get('room')}' from session on dashboard access for user '{session.get('name')}'")
        session.pop('room', None)

    is_admin = session.get('is_admin', False)
    return render_template('dashboard.html', username=session.get('name'), is_admin=is_admin)

# --- Password Reset Routes ---
@app.route('/forgetPassword', methods=['GET', 'POST'])
def forget_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            flash("Email address is required.", "error")
            return render_template('forgetPassword.html'), 400

        # Check mail config BEFORE DB lookup
        if not all([app.config.get('MAIL_USERNAME'), app.config.get('MAIL_PASSWORD'), app.config.get('MAIL_DEFAULT_SENDER')]):
             flash("Password reset emails are currently disabled due to server configuration. Please contact support.", "error")
             app.logger.error("Password reset requested but mail is not configured.")
             return render_template('forgetPassword.html', email=email), 503

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, name FROM users WHERE email = %s", (email,)) # Fetch name too
            user = cursor.fetchone()
            if user:
                otp = ''.join(random.choices(string.digits, k=6))
                # Use UTC time for expiry calculation
                expiry_time = datetime.datetime.now(pytz.utc) + datetime.timedelta(minutes=10)

                # Store OTP hash, associated email, and expiry timestamp (ISO format for session serialization)
                session['reset_otp'] = generate_password_hash(otp)
                session['reset_email'] = email
                session['reset_otp_expires'] = expiry_time.isoformat()
                session.permanent = False # OTP session shouldn't persist long-term

                try:
                    # Send the email
                    user_name = user['name']
                    message_body = (f"Hi {user_name},\n\n"
                                    f"Your OTP to reset your ChatApp password is: {otp}\n\n"
                                    f"This OTP is valid for 10 minutes.\n\n"
                                    f"If you didn't request this, please ignore this email.")
                    message = Message(subject="Your ChatApp Password Reset OTP",
                                      recipients=[email],
                                      body=message_body)
                    mail.send(message)

                    app.logger.info(f"Password reset OTP sent to {email} for user {user_name}")
                    flash("An OTP has been sent to your email address (if it's registered). Please check your inbox (and spam folder). It's valid for 10 minutes.", "info")
                    # Redirect to the reset page where OTP is entered
                    return redirect(url_for('reset_password'))

                except Exception as e:
                    app.logger.error(f"Failed to send OTP email to {email}: {e}", exc_info=True)
                    flash("Could not send the OTP email due to a server error. Please try again later or contact support.", "error")
                    # Clear failed OTP state from session
                    session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                    return render_template('forgetPassword.html', email=email), 500
            else:
                # SECURITY: Do NOT reveal if the email exists or not. Use the same message.
                app.logger.warning(f"Password reset requested for non-existent or incorrect email: {email}. Sent generic success message.")
                flash("If an account with that email exists, an OTP has been sent. Please check your inbox (and spam folder).", "info")
                # Still redirect to reset page to avoid confirming email existence via URL flow
                return redirect(url_for('reset_password')) # Redirect even if email doesn't exist

        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password forget request for {email}: {e}")
            flash("A database error occurred. Please try again.", "error")
            return render_template('forgetPassword.html', email=email), 500
        except ConnectionError as e:
             app.logger.error(f"DB Connection error during password forget for {email}: {e}")
             flash("Could not connect to the database. Please try again later.", "error")
             return render_template('forgetPassword.html', email=email), 503
        finally:
            if cursor: cursor.close()

    return render_template('forgetPassword.html')

@app.route('/resetPassword', methods=['GET', 'POST'])
def reset_password():
    reset_email = session.get('reset_email')
    otp_hash = session.get('reset_otp')
    otp_expires_iso = session.get('reset_otp_expires')

    # Check if the essential session variables exist
    if not all([reset_email, otp_hash, otp_expires_iso]):
        flash("Invalid password reset request or session expired. Please request a new OTP.", "warning")
        return redirect(url_for('forget_password'))

    try:
        # Parse expiry time, make it timezone-aware UTC
        otp_expires = datetime.datetime.fromisoformat(otp_expires_iso).replace(tzinfo=pytz.utc)
        # Get current time also as timezone-aware UTC
        now_utc = datetime.datetime.now(pytz.utc)

        if now_utc > otp_expires:
            session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
            flash("Your OTP has expired. Please request a new one.", "error")
            return redirect(url_for('forget_password'))
    except ValueError:
         app.logger.error(f"Invalid ISO format for OTP expiry in session: {otp_expires_iso}")
         flash("Invalid session state. Please request a new OTP.", "error")
         session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
         return redirect(url_for('forget_password'))

    if request.method == 'POST':
        otp_entered = request.form.get('otp')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        errors = []
        # Check OTP first
        if not otp_entered or not check_password_hash(otp_hash, otp_entered):
             errors.append("The OTP entered is invalid or has expired.")
        # Check passwords only if OTP is likely valid (avoid revealing too much info)
        else:
            if not new_password: errors.append("New password is required.")
            elif len(new_password) < 6: errors.append("Password must be at least 6 characters long.")
            if not confirm_password: errors.append("Confirm password is required.")
            elif new_password != confirm_password: errors.append("The new passwords do not match.")

        if errors:
            for error in errors: flash(error, "error")
            # Keep user on resetPassword page, don't clear session yet
            return render_template('resetPassword.html'), 400

        # If all checks pass, hash new password and update DB
        new_hashed_password = generate_password_hash(new_password)
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Ensure update only happens for the correct email linked to the OTP
            rows_affected = cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_hashed_password, reset_email))

            if rows_affected == 1:
                app.logger.info(f"Password successfully reset for email: {reset_email}")
                # IMPORTANT: Clear OTP state AFTER successful reset
                session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                flash("Your password has been reset successfully. Please log in with your new password.", "success")
                return redirect(url_for('login')) # Redirect to login page
            else:
                 # This case implies the email existed when OTP was sent but was deleted/changed before reset.
                 app.logger.error(f"Password reset failed for {reset_email}: User not found or DB issue during update, despite valid OTP session state.")
                 flash("An unexpected error occurred while updating your password. User may no longer exist. Please try again or contact support.", "error")
                 return render_template('resetPassword.html'), 500

        except pymysql.MySQLError as e:
            app.logger.error(f"Database error during password reset update for {reset_email}: {e}")
            flash("An error occurred while resetting the password. Please try again.", "error")
            return render_template('resetPassword.html'), 500
        except ConnectionError as e:
             app.logger.error(f"DB Connection error during password reset update for {reset_email}: {e}")
             flash("Could not connect to the database. Please try again later.", "error")
             return render_template('resetPassword.html'), 503
        finally:
            if cursor: cursor.close()

    # For GET request, just render the page
    return render_template('resetPassword.html')


# --- Chat Room Logic & Routes ---
@app.route('/join_room', methods=['POST'])
@login_required
def join_room_route():
    name = session.get("name")
    user_id = session.get("user_id")
    room_code_input = request.form.get("room_code", "").strip().upper()
    join_room_action = request.form.get("join_room")
    create_room_action = request.form.get("create_room")

    target_room_code = None

    if join_room_action:
        # Validate input code
        if not room_code_input:
            flash("Please enter a room code to join.", "error")
            return redirect(url_for('dashboard'))
        if len(room_code_input) != 4 or not room_code_input.isalpha():
            flash("Invalid room code format (must be exactly 4 uppercase letters).", "error")
            return redirect(url_for('dashboard'))
        if room_code_input == "PUBLIC":
             flash("Cannot join 'PUBLIC' room this way. Use the Public Chat link.", "warning")
             return redirect(url_for('dashboard'))

        # Check if the room exists (in memory or has history in DB)
        if not is_valid_room(room_code_input):
             flash(f"Room code '{room_code_input}' does not exist or has no message history.", "error")
             return redirect(url_for('dashboard'))

        target_room_code = room_code_input
        app.logger.info(f"User '{name}' (ID: {user_id}) joining existing private room '{target_room_code}'.")

    elif create_room_action:
        new_code = generate_room_code()
        # Initialize room tracker immediately on creation
        rooms[new_code] = {"members": 0, "users": set()}
        target_room_code = new_code
        app.logger.info(f"User '{name}' (ID: {user_id}) created new private room '{target_room_code}'.")
        # Optional: Save a "Room Created" system message to DB?
        # save_message(target_room_code, None, "System", f"Room created by {name}.") # Decide if needed

    else:
        flash("Invalid action specified.", "error")
        return redirect(url_for('dashboard'))

    # Store target room in session and redirect to the chat view
    session['room'] = target_room_code
    app.logger.info(f"User '{name}' (ID: {user_id}) being redirected to room '{target_room_code}'.")

    if target_room_code == "PUBLIC":
         return redirect(url_for('public_chat')) # Should not happen via this form, but handle defensively
    else:
         return redirect(url_for('room')) # Redirect to private room view

@app.route('/room') # Route for PRIVATE chat rooms
@login_required
def room():
    room_code = session.get('room')
    user_name = session.get('name')

    if not room_code or not user_name:
        flash("No active room selected or user session invalid. Please join or create one from the dashboard.", "warning")
        return redirect(url_for('dashboard'))

    # Ensure it's not the PUBLIC room accessed via the wrong URL
    if room_code == "PUBLIC":
         flash("Please use the dedicated Public Chat link for the PUBLIC room.", "warning")
         session.pop('room', None) # Clear invalid room state
         return redirect(url_for('dashboard'))

    # Double check the private room is valid (exists in memory OR DB history)
    # This check is slightly redundant if joined via form, but protects direct URL access
    if not is_valid_room(room_code):
         flash(f"The room '{room_code}' is no longer active or valid.", "error")
         session.pop('room', None) # Clear invalid room state
         return redirect(url_for('dashboard'))

    app.logger.debug(f"Rendering private chat room '{room_code}' for user '{user_name}'.")
    # Pass room code and username for the template to use
    return render_template('chatroom.html', room_code=room_code, username=user_name)

@app.route('/public_chat') # Route specifically for the PUBLIC chat room
@login_required
def public_chat():
    user_name = session.get('name')
    if not user_name:
         flash("User information missing. Please log in again.", "error")
         return redirect(url_for('login'))

    # Set the room in session
    session['room'] = "PUBLIC"

    # Ensure PUBLIC room tracker exists in memory
    if "PUBLIC" not in rooms:
        rooms["PUBLIC"] = {"members": 0, "users": set()}
        app.logger.info("Initialized PUBLIC room tracker in memory.")

    app.logger.info(f"User '{user_name}' entering public chat (room 'PUBLIC').")
    # Pass username for the template
    return render_template('public_chat.html', username=user_name)


# --- Admin Routes ---
@app.route('/admin')
@admin_required
def admin_dashboard():
    """Renders the main admin dashboard page."""
    user_count = 0
    active_room_count = len([r for r, data in rooms.items() if data['members'] > 0]) # Count rooms with active members

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users")
        result = cursor.fetchone()
        if result:
            user_count = result['count']
        cursor.close()
    except Exception as e:
        app.logger.error(f"Admin dashboard: Error getting user count: {e}")
        flash("Could not retrieve user count.", "warning")

    return render_template('admin/admin_dashboard.html',
                           username=session.get('name'),
                           user_count=user_count,
                           active_room_count=active_room_count)

@app.route('/admin/users')
@admin_required
def admin_users():
    """Displays a list of all registered users."""
    users = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Fetch all users, INCLUDING the current admin, but note if they are admins
        # Order by registration date, newest first
        cursor.execute("""
            SELECT id, name, email, created_at, is_admin
            FROM users
            ORDER BY created_at DESC
        """)
        users = cursor.fetchall()
        cursor.close()
    except pymysql.MySQLError as e:
        app.logger.error(f"Admin area: Error fetching users list: {e}")
        flash("Could not retrieve user list due to a database error.", "error")
    except ConnectionError as e:
         app.logger.error(f"Admin area: DB Connection error fetching users: {e}")
         flash("Could not connect to the database to retrieve users.", "error")

    current_user_id = session.get('user_id')
    return render_template('admin/admin_users.html',
                           users=users,
                           current_user_id=current_user_id, # Pass current admin's ID
                           username=session.get('name'))

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """Deletes a user account."""
    current_user_id = session.get('user_id')
    if user_id == current_user_id:
        flash("Admins cannot delete their own account via this interface.", "error")
        return redirect(url_for('admin_users'))

    try:
        conn = get_db()
        cursor = conn.cursor()
        # Need to know name for logging/flash and if they are admin
        cursor.execute("SELECT name, is_admin FROM users WHERE id = %s", (user_id,))
        user_to_delete = cursor.fetchone()

        if not user_to_delete:
            flash("User not found.", "error")
            return redirect(url_for('admin_users'))

        # Prevent deleting other admins (Policy Decision - Keep this?)
        if user_to_delete['is_admin']:
             flash("Deleting other administrator accounts is disabled.", "error")
             app.logger.warning(f"Admin {session.get('name')} ({current_user_id}) attempted to delete admin {user_to_delete['name']} ({user_id}). Operation blocked.")
             return redirect(url_for('admin_users'))

        # Proceed with deletion
        # The ON DELETE SET NULL constraint on messages table will handle foreign key.
        rows_affected = cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        cursor.close() # Close cursor before redirect

        if rows_affected > 0:
            flash(f"User '{user_to_delete['name']}' (ID: {user_id}) has been deleted successfully. Their past messages remain associated with their username.", "success")
            app.logger.info(f"Admin {session.get('name')} ({current_user_id}) deleted user {user_to_delete['name']} ({user_id}).")
        else:
            # This should theoretically not happen if user was found before
            flash(f"Could not delete user (ID: {user_id}). They may have already been deleted.", "warning")
            app.logger.warning(f"Admin {session.get('name')} ({current_user_id}) attempted delete for user ID {user_id}, but 0 rows were affected after finding the user.")

    except pymysql.MySQLError as e:
        app.logger.error(f"Admin area: Error deleting user ID {user_id}: {e}")
        flash("Could not delete user due to a database error.", "error")
    except ConnectionError as e:
         app.logger.error(f"Admin area: DB Connection error deleting user ID {user_id}: {e}")
         flash("Could not connect to the database to delete user.", "error")
    # No matter what, redirect back
    return redirect(url_for('admin_users'))

def get_all_rooms():
    """Gets all rooms with their status and message counts."""
    rooms_list = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get message counts for all room_codes
        cursor.execute("""
            SELECT room_code, COUNT(*) as message_count
            FROM messages
            GROUP BY room_code
        """)
        message_counts = {row['room_code']: row['message_count'] for row in cursor.fetchall()}
        
        # Active rooms
        for room_code, data in rooms.items():
            rooms_list.append({
                'code': room_code,
                'is_active': True,
                'members': data['members'],
                'message_count': message_counts.get(room_code, 0)
            })
        
        # Historical rooms (in DB but not active)
        for room_code in message_counts:
            if room_code not in rooms:
                rooms_list.append({
                    'code': room_code,
                    'is_active': False,
                    'members': 0,
                    'message_count': message_counts[room_code]
                })
        
        cursor.close()
    except Exception as e:
        app.logger.error(f"Error getting all rooms: {e}")
    
    return rooms_list

@app.route('/admin/rooms')
@admin_required
def admin_rooms():
    """Displays a list of all rooms for management."""
    rooms_list = get_all_rooms()
    return render_template('admin/admin_rooms.html',
                           rooms=rooms_list,
                           username=session.get('name'))

@app.route('/admin/room/<room_code>')
@admin_required
def admin_room_details(room_code):
    """Displays details and messages for a specific room."""
    # Validate room exists
    if not is_valid_room(room_code):
        flash(f"Room '{room_code}' does not exist.", "error")
        return redirect(url_for('admin_rooms'))
    
    # Get messages
    messages = get_message_history(room_code, limit=100)  # More for admin view
    
    return render_template('admin/admin_room_details.html',
                           room_code=room_code,
                           messages=messages,
                           username=session.get('name'))

@app.route('/admin/room/<room_code>/clear_messages', methods=['POST'])
@admin_required
def admin_clear_room_messages(room_code):
    """Clears all messages for a specific room."""
    if not is_valid_room(room_code):
        flash(f"Room '{room_code}' does not exist.", "error")
        return redirect(url_for('admin_rooms'))
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE room_code = %s", (room_code,))
        deleted_count = cursor.rowcount
        cursor.close()
        
        flash(f"Cleared {deleted_count} messages from room '{room_code}'.", "success")
        app.logger.info(f"Admin {session.get('name')} cleared {deleted_count} messages from room '{room_code}'.")
    except Exception as e:
        app.logger.error(f"Error clearing messages for room {room_code}: {e}")
        flash("Could not clear messages due to a database error.", "error")
    
    return redirect(url_for('admin_rooms'))

@app.route('/admin/room/<room_code>/delete', methods=['POST'])
@admin_required
def admin_delete_room(room_code):
    """Deletes a room entirely (removes from memory and DB)."""
    if not is_valid_room(room_code):
        flash(f"Room '{room_code}' does not exist.", "error")
        return redirect(url_for('admin_rooms'))
    
    try:
        # Remove from memory if active
        if room_code in rooms:
            # Disconnect all users in the room
            for sid, info in list(sid_to_info.items()):
                if info['room_code'] == room_code:
                    socketio.emit('error', {'message': 'Room has been deleted by admin.'}, room=sid)
                    disconnect(sid)
            del rooms[room_code]
        
        # Delete all messages
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE room_code = %s", (room_code,))
        deleted_count = cursor.rowcount
        cursor.close()
        
        flash(f"Room '{room_code}' has been deleted. Removed {deleted_count} messages.", "success")
        app.logger.info(f"Admin {session.get('name')} deleted room '{room_code}' and {deleted_count} messages.")
    except Exception as e:
        app.logger.error(f"Error deleting room {room_code}: {e}")
        flash("Could not delete room due to a database error.", "error")
    
    return redirect(url_for('admin_rooms'))

# Potential future admin action stub
# @app.route('/admin/user/<int:user_id>/edit', methods=['GET', 'POST'])
# @admin_required
# def admin_edit_user(user_id):
#     # Fetch user details for GET
#     # Handle form submission for POST (update name, email, potentially is_admin toggle - BE CAREFUL)
#     pass


# --- Database Helper Functions for Chat ---
def get_message_history(room_code, limit=75): # Increased limit slightly
    """Fetches the last 'limit' messages for a given room_code from the database, ordered oldest to newest."""
    app.logger.debug(f"Fetching history for room_code: '{room_code}', Limit: {limit}")
    messages = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Fetch latest messages first, then reverse
        query = """
            SELECT user_id, user_name, content, timestamp
            FROM messages
            WHERE room_code = %s
            ORDER BY timestamp DESC
            LIMIT %s
        """
        cursor.execute(query, (room_code, limit))
        db_messages = list(cursor.fetchall()) # Fetch all into a list
        db_messages.reverse() # Order oldest to newest for display
        app.logger.debug(f"Fetched {len(db_messages)} messages from DB for room '{room_code}'.")

        # Format for client, ensuring UTC ISO timestamps
        for msg in db_messages:
             # Assume DB timestamp is naive but represents UTC
             ts_naive = msg['timestamp']
             ts_aware_utc = ts_naive.replace(tzinfo=pytz.utc)

             messages.append({
                 "name": msg['user_name'], # Stored username at time of posting
                 "message": msg['content'],
                 "timestamp": ts_aware_utc.isoformat(), # Send ISO string to client
                 "user_id": msg['user_id'],
                 "isSystem": msg['user_id'] is None # System messages have NULL user_id
             })
        app.logger.debug(f"Formatted {len(messages)} history messages for room '{room_code}' for client.")

    except pymysql.MySQLError as e:
        app.logger.error(f"DB Error fetching history for room '{room_code}': {e}")
    except ConnectionError as e:
         app.logger.error(f"DB Connection Error fetching history for room '{room_code}': {e}")
    finally:
        # Ensure cursor is closed even if DB connection stays open for the request
        if 'cursor' in locals() and cursor:
             try: cursor.close()
             except Exception as cur_e: app.logger.error(f"Error closing history cursor: {cur_e}")
    return messages

def save_message(room_code, user_id, user_name, content):
    """Saves a chat message to the database."""
    app.logger.debug(f"Attempting to save message to room '{room_code}' by User: {user_name} (ID: {user_id})")
    success = False
    conn = None
    cursor = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        utc_now = datetime.datetime.now(pytz.utc)
        query = """
            INSERT INTO messages (room_code, user_id, user_name, content, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(query, (room_code, user_id, user_name, content, utc_now))
        conn.commit()
        success = True
        app.logger.debug(f"Message saved to DB for room '{room_code}'.")
    except pymysql.MySQLError as e:
        app.logger.error(f"DB Error saving message for room '{room_code}' by user {user_name}: {e}")
    except ConnectionError as e:
        app.logger.error(f"DB Connection Error saving message for room '{room_code}': {e}")
    finally:
        if cursor:
            try: cursor.close()
            except Exception as cur_e: app.logger.error(f"Error closing save message cursor: {cur_e}")
        if conn:
            try: conn.close()
            except Exception as conn_e: app.logger.error(f"Error closing save message connection: {conn_e}")
    return success


# --- SocketIO Event Handlers ---

def emit_user_count(room_code):
    """Helper to emit the current user count for a room."""
    if room_code in rooms:
        count = rooms[room_code]['members']
        app.logger.debug(f"Emitting user count for room '{room_code}': {count}")
        socketio.emit('update_user_count', {'count': count}, to=room_code)
    else:
         app.logger.warning(f"Tried to emit user count for non-existent room tracker: '{room_code}'")


@socketio.on('connect')
def handle_connect():
    """Handles new client connections."""
    sid = request.sid
    # Extract user info from session
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Connect] SID={sid}. Session: User='{user_name}'(ID:{user_id}), Room='{room_code}'")

    # Validation: Ensure user and room details are present in the session
    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Socket connection REJECTED for SID {sid}: Missing required session data (room, name, or id).")
        socketio.emit('error', {'message': 'Invalid session data. Please refresh or log in again.'}, room=sid)
        disconnect(sid) # Explicitly disconnect the client
        return False # Indicate connection failure

    # Check if the target room is valid (exists in memory OR DB history)
    # Use is_valid_room, but initialize tracker if needed *here*
    if room_code not in rooms:
        if is_valid_room(room_code): # Checks DB history too
             # Room exists in DB but not memory, initialize tracker now
             rooms[room_code] = {"members": 0, "users": set()}
             app.logger.info(f"Initialized in-memory tracker for room '{room_code}' based on DB history check during connect.")
        else:
             app.logger.error(f"Connection REJECTED for SID {sid}: Room '{room_code}' is invalid (not in memory or DB history).")
             socketio.emit('error', {'message': f'Room "{room_code}" is invalid or no longer exists.'}, room=sid)
             disconnect(sid)
             return False

    # --- Proceed with joining ---
    join_room(room_code)
    was_new_user = user_id not in rooms[room_code]["users"]
    if was_new_user:
        rooms[room_code]["users"].add(user_id)
        rooms[room_code]["members"] += 1
    sid_to_info[sid] = {'user_id': user_id, 'user_name': user_name, 'room_code': room_code} # Store SID info mapping

    app.logger.info(f"User '{user_name}' (SID: {sid}) successfully CONNECTED to room '{room_code}'. Active members: {rooms[room_code]['members']}. New user: {was_new_user}")

    # Emit the updated user count to everyone in the room
    emit_user_count(room_code)

    # Send message history ONLY to the connecting user
    try:
        history_messages = get_message_history(room_code)
        if history_messages: # Only emit if there's history
             app.logger.debug(f"Sending {len(history_messages)} history messages to {user_name} (SID: {sid}) in room '{room_code}'")
             socketio.emit('message_history', {'messages': history_messages}, room=sid)
        else:
            app.logger.debug(f"No message history found for room '{room_code}'. Sending welcome.")
            # Optionally send a specific "no history" or "welcome" message to the client if desired
            # socketio.emit('system_message', {'message': 'Welcome! No previous messages.'}, room=sid)
    except Exception as e:
        app.logger.error(f"Error retrieving/sending history for room '{room_code}' to {user_name} (SID: {sid}): {e}", exc_info=True)
        socketio.emit('error', {'message': 'Error loading message history.'}, room=sid)

    # Notify others about the join ONLY if this is a new user (not a reconnect)
    if was_new_user:
        join_message_content = f"{user_name} has joined the room."
        join_ts = datetime.datetime.now(pytz.utc).isoformat()
        join_message = {
            "name": "System",
            "message": join_message_content,
            "timestamp": join_ts,
            "user_id": None, # System messages have no user ID
            "isSystem": True
            }
        socketio.emit('message', join_message, to=room_code, skip_sid=sid) # skip_sid ensures the joiner doesn't get their own join message
        app.logger.debug(f"Sent join notification for '{user_name}' to room '{room_code}' (skipped SID {sid})")

        # Save join message to database for persistence
        save_message(room_code, None, "System", join_message_content)

    return True # Indicate successful connection


@socketio.on('disconnect')
def handle_disconnect():
    """Handles client disconnections."""
    sid = request.sid
    info = sid_to_info.pop(sid, None) # Get info and remove from mapping

    if not info:
        app.logger.debug(f"[Disconnect] SID={sid}. No info found, possibly stale connection.")
        return

    user_id = info['user_id']
    user_name = info['user_name']
    disconnected_room = info['room_code']

    app.logger.debug(f"[Disconnect] SID={sid}. User='{user_name}'(ID:{user_id}). Room: '{disconnected_room}'.")

    if disconnected_room and disconnected_room in rooms:
        # Formally leave the SocketIO room
        leave_room(disconnected_room)

        # Update our custom room tracker
        room_info = rooms[disconnected_room]
        if user_id in room_info["users"]:
             room_info["users"].remove(user_id)
             room_info["members"] = max(0, room_info["members"] - 1) # Decrement, ensure non-negative
             current_members = room_info["members"]
             app.logger.info(f"User '{user_name}' (SID: {sid}) DISCONNECTED from room '{disconnected_room}'. Remaining members: {current_members}.")

             # Notify remaining users in the room about the departure
             leave_message_content = f"{user_name} has left the room."
             leave_ts = datetime.datetime.now(pytz.utc).isoformat()
             leave_message = {
                 "name": "System",
                 "message": leave_message_content,
                 "timestamp": leave_ts,
                 "user_id": None,
                 "isSystem": True
                 }
             socketio.emit('message', leave_message, to=disconnected_room)

             # Emit updated user count to the room
             emit_user_count(disconnected_room)

             # Save leave message to database for persistence
             save_message(disconnected_room, None, "System", leave_message_content)

             # Clean up memory for empty PRIVATE rooms
             # Keep PUBLIC room tracker even if empty
             if current_members <= 0 and disconnected_room != "PUBLIC":
                 try:
                     del rooms[disconnected_room]
                     app.logger.info(f"Private room tracker '{disconnected_room}' deleted as it became empty.")
                 except KeyError:
                      # Should not happen if check was done correctly, but log just in case
                      app.logger.warning(f"Attempted to delete empty room tracker '{disconnected_room}', but it was already gone (potential race condition?).")
             elif current_members <= 0 and disconnected_room == "PUBLIC":
                  # Reset PUBLIC room members to 0, but keep the tracker entry
                  room_info["members"] = 0 # Ensure it's explicitly 0
                  app.logger.info(f"Public room '{disconnected_room}' is empty, tracker retained with 0 members.")

        else:
            # user_id was not in the users set, even though it was mapped. This indicates an inconsistency.
            app.logger.warning(f"Disconnect inconsistency: SID {sid} was mapped to room '{disconnected_room}', but user_id {user_id} not found in its 'users' set.")
            # Still try to emit count update as a safety measure
            emit_user_count(disconnected_room)

    else:
        app.logger.warning(f"Socket disconnected (SID: {sid}, User: '{user_name}'), but SID was not found in any active room tracker or the room tracker was missing.")
        # Cannot send leave message or update count if we don't know the room


@socketio.on('message')
def handle_message(data):
    """Handles incoming chat messages from clients."""
    sid = request.sid

    # Basic validation of incoming data format
    if not isinstance(data, dict) or 'data' not in data:
        app.logger.warning(f"Invalid message format received from SID {sid}. Data: {data}")
        socketio.emit('error', {'message': 'Invalid message format.'}, room=sid)
        return

    # Extract necessary info
    message_text = str(data.get('data', '')) # Get message text, default to empty string
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Message Rcvd] SID={sid}, User='{user_name}'(ID:{user_id}), Room='{room_code}', Msg='{message_text[:50]}...'")

    # Validate session and room membership consistency
    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Message REJECTED from SID {sid}: Missing essential session data (room, name, or id).")
        socketio.emit('error', {'message': 'Cannot send message: Your session is invalid. Please refresh.'}, room=sid)
        return

    # Cross-check: Does the room tracker confirm this user belongs here?
    if room_code not in rooms or user_id not in rooms[room_code].get("users", set()):
         app.logger.error(f"Message REJECTED from SID {sid} (User: {user_name}): Session/tracker mismatch for room '{room_code}'. Possible stale session or tracker issue.")
         socketio.emit('error', {'message': 'Room connection mismatch. Please refresh the page.'}, room=sid)
         # Optional: Force disconnect if this happens consistently? disconnect(sid)
         return

    # --- Process the message ---
    message_content = message_text.strip()
    if not message_content:
        app.logger.debug(f"Empty message REJECTED from {user_name} (SID: {sid}) in room '{room_code}'.")
        # Optionally, send a quiet feedback to the user?
        # socketio.emit('info', {'message': 'Cannot send empty messages.'}, room=sid)
        return

    MAX_MSG_LENGTH = 2000 # Define a max length
    if len(message_content) > MAX_MSG_LENGTH:
        app.logger.warning(f"Message REJECTED from {user_name} (SID: {sid}) in room '{room_code}': Too long ({len(message_content)} chars).")
        socketio.emit('error', {'message': f'Message rejected: Exceeds maximum length of {MAX_MSG_LENGTH} characters.'}, room=sid)
        return

    # Save message to Database *FIRST*
    if not save_message(room_code, user_id, user_name, message_content):
        app.logger.error(f"Failed to SAVE message to DB for room '{room_code}' from {user_name} (SID: {sid}). Message: '{message_content[:50]}...'")
        # Inform the sending user about the failure
        socketio.emit('error', {'message': 'Failed to save message to the server. Please try sending again.'}, room=sid)
        return # Stop processing if DB save failed

    # Prepare message data for broadcasting
    timestamp_now_iso = datetime.datetime.now(pytz.utc).isoformat()
    content_to_broadcast = {
        "name": user_name,
        "message": message_content, # Send the sanitized, stripped message
        "timestamp": timestamp_now_iso,
        "user_id": user_id,
        "isSystem": False # It's a regular user message
        }

    # Broadcast the message to everyone in the room (including the sender)
    socketio.emit('message', content_to_broadcast, to=room_code)
    app.logger.debug(f"Message from '{user_name}' (SID: {sid}) in room '{room_code}' saved and BROADCASTED.")


@socketio.on('leave')
def handle_leave():
    """Handles leave event from client."""
    sid = request.sid
    info = sid_to_info.get(sid)
    if info:
        user_id = info['user_id']
        user_name = info['user_name']
        disconnected_room = info['room_code']
        if disconnected_room and disconnected_room in rooms:
            room_info = rooms[disconnected_room]
            if user_id in room_info["users"]:
                room_info["users"].remove(user_id)
                room_info["members"] = max(0, room_info["members"] - 1)
                leave_message_content = f"{user_name} has left the room."
                leave_ts = datetime.datetime.now(pytz.utc).isoformat()
                leave_message = {
                    "name": "System",
                    "message": leave_message_content,
                    "timestamp": leave_ts,
                    "user_id": None,
                    "isSystem": True
                }
                socketio.emit('message', leave_message, to=disconnected_room)
                emit_user_count(disconnected_room)
                save_message(disconnected_room, None, "System", leave_message_content)
                if room_info["members"] <= 0 and disconnected_room != "PUBLIC":
                    del rooms[disconnected_room]
                    app.logger.info(f"Private room tracker '{disconnected_room}' deleted as it became empty.")
                elif room_info["members"] <= 0 and disconnected_room == "PUBLIC":
                    room_info["members"] = 0
                    app.logger.info(f"Public room '{disconnected_room}' is empty, tracker retained with 0 members.")


# --- General Error Handlers ---
@socketio.on_error_default
def default_error_handler(e):
    """Log unhandled SocketIO errors."""
    sid = request.sid if request else 'Unknown SID'
    app.logger.error(f"Unhandled SocketIO Error: {e} (SID: {sid})", exc_info=True)
    # Attempt to notify the specific client if possible
    if request and sid:
        try:
            socketio.emit('error', {'message': 'An unexpected server error occurred with your connection.'}, room=sid)
        except Exception as emit_err:
            app.logger.error(f"Failed to emit generic error message to SID {sid} after internal error: {emit_err}")


@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 Not Found errors."""
    app.logger.warning(f"404 Not Found: Request for '{request.url}' from {request.remote_addr}. Referrer: {request.referrer}")
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Handle 500 Internal Server errors."""
    # Extract original exception if available (useful for specific handling)
    original_exception = getattr(e, "original_exception", e)
    app.logger.error(f"500 Internal Server Error handling request for {request.url} from {request.remote_addr}: {original_exception}", exc_info=True)
    # Ensure DB connection associated with this request context is closed
    close_db(original_exception)
    return render_template('500.html'), 500

@app.errorhandler(ConnectionError) # Catch our specific DB connection error
def handle_db_connection_error(e):
    """Handle database connection errors gracefully during requests."""
    app.logger.critical(f"Database Connection Error during request {request.url} from {request.remote_addr}: {e}", exc_info=app.debug)
    close_db(e) # Attempt cleanup
    flash("The service is temporarily unavailable due to a database connection issue. Please try again later or contact support.", "error")
    # Try redirecting to home if it's not a static asset or home itself
    if request.endpoint and request.endpoint not in ['static', 'home', 'logout']:
         return redirect(url_for('home'))
    # Fallback to showing a specific error page/message
    return render_template('500.html', error_message="Database connection failed. The application cannot currently function."), 503 # Service Unavailable

@app.errorhandler(Exception) # Generic handler for other exceptions
def handle_exception(e):
    """Handle other uncaught exceptions during request processing."""
    app.logger.error(f"Unhandled Exception handling request for {request.url} from {request.remote_addr}: {e}", exc_info=True)

    # Special handling for specific types if needed
    if isinstance(e, (pymysql.MySQLError)):
        # Logged already, potentially show generic DB error to user
        flash("An unexpected database problem occurred. Please try again later.", "error")
    # Handle other specific exceptions if necessary...
    # elif isinstance(e, SomeOtherError):
    #     flash("Specific message for SomeOtherError", "warning")

    # Ensure DB connection tied to this failing request is closed
    close_db(e)

    # Render generic 500 page
    return render_template('500.html'), 500


# --- Run the App ---
if __name__ == "__main__":
    port = int(os.getenv('PORT', 5001))
    host = os.getenv('HOST', '0.0.0.0') # Listen on all available interfaces
    debug_mode = app.config['DEBUG']

    print(f"--- Starting Flask-SocketIO Chat Application ---")
    print(f" Configuration:")
    print(f"   - Host: {host}")
    print(f"   - Port: {port}")
    print(f"   - Debug Mode: {'ON' if debug_mode else 'OFF'}")
    print(f"   - Async Mode: {ASYNC_MODE or 'Werkzeug (Default - Development Only!)'}")
    print(f"   - Database Host: {DB_CONFIG['host']}")
    print(f"   - Database Name: {DB_CONFIG['database']}")
    print(f"-----------------------------------------------")

    try:
        # use_reloader should ideally be False in production, even if debug is technically True
        # for certain Werkzeug features. Let debug control it simply here.
        socketio.run(app, host=host, port=port, debug=debug_mode, use_reloader=debug_mode)
    except Exception as start_error:
         print(f"##### APPLICATION FAILED TO START #####: {start_error}")
         print(f"\nCRITICAL ERROR: Failed to start application: {start_error}\n", file=sys.stderr)
         sys.exit(1) # Exit with error code

# --- END OF FILE app.py ---