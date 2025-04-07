# --- START OF FILE app.py ---

import os
import random
import string
import pymysql
import datetime
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, session,
    flash, url_for, g, jsonify, abort # Added abort
)
from flask_socketio import (
    SocketIO, join_room, leave_room, send, disconnect
)
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
import logging
import sys # For sys.exit
from functools import wraps # Added for decorator

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
    app.logger.info("Starting database initialization check...")
    try:
        # Connect without specifying database initially to create it if needed
        conn = pymysql.connect(host=DB_CONFIG['host'], user=DB_CONFIG['user'], password=DB_CONFIG['password'], port=DB_CONFIG['port'], charset=DB_CONFIG['charset'])
        cursor = conn.cursor()
        db_name = DB_CONFIG['database']
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cursor.execute(f"USE `{db_name}`;")
        app.logger.info(f"Ensured database '{db_name}' exists.")

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
        app.logger.info("Checked/Created 'users' table.")

        # Ensure is_admin column exists if table already existed (idempotent)
        try:
            cursor.execute("SELECT is_admin FROM users LIMIT 1;")
            app.logger.debug("'is_admin' column already exists in users table.")
        except pymysql.err.OperationalError as e:
             if "Unknown column 'is_admin'" in str(e):
                 app.logger.info("Adding missing 'is_admin' column to existing 'users' table.")
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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'Timestamp when the message was recorded (UTC recommended)',
            INDEX room_time_idx (room_code, timestamp) COMMENT 'Index for efficient history fetching per room'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Stores chat messages for all rooms';
        """)
        app.logger.info("Checked/Created 'messages' table.")

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
                app.logger.info("Added foreign key constraint messages.user_id -> users.id (ON DELETE SET NULL).")
            else:
                 app.logger.debug("Foreign key constraint 'fk_user_id' already exists on messages table.")

        except pymysql.MySQLError as fk_error:
             app.logger.warning(f"Could not add/verify foreign key constraint (may already exist or DB issue): {fk_error}")

        conn.commit() # Commit schema changes
        app.logger.info("Database schema initialization complete.")

    except pymysql.MySQLError as e:
        app.logger.critical(f"FATAL: Database schema could not be initialized: {e}", exc_info=True)
        raise RuntimeError(f"FATAL: Database schema could not be initialized: {e}") from e
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn.open: conn.close()


def ensure_admin_user():
    """Creates or updates the specified admin user."""
    # !!! SECURITY WARNING: Hardcoding credentials here is highly insecure !!!
    # !!! Use environment variables or a setup script for production !!!
    admin_email = "smpk@smpk.com"
    admin_pass = "407432" # The password requested
    admin_name = "SMPK Admin" # A display name for the admin

    app.logger.info(f"Ensuring admin user '{admin_email}' exists and is configured...")
    try:
        # Need app context to use get_db() safely outside request
        with app.app_context():
            conn = get_db()
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT id, password, is_admin FROM users WHERE email = %s", (admin_email,))
                admin_user = cursor.fetchone()

                hashed_pass = generate_password_hash(admin_pass)

                if not admin_user:
                    # Create the admin user
                    cursor.execute(
                        "INSERT INTO users (name, email, password, is_admin) VALUES (%s, %s, %s, TRUE)",
                        (admin_name, admin_email, hashed_pass)
                    )
                    app.logger.info(f"Created admin user '{admin_email}' with the specified password.")
                else:
                    # Update existing user to ensure password and admin status are correct
                    needs_update = False
                    if not check_password_hash(admin_user['password'], admin_pass):
                        needs_update = True
                        update_sql = "UPDATE users SET password = %s, is_admin = TRUE WHERE id = %s"
                        update_params = (hashed_pass, admin_user['id'])
                        app.logger.info(f"Updating password for admin user '{admin_email}'.")
                    elif not admin_user['is_admin']:
                        needs_update = True
                        update_sql = "UPDATE users SET is_admin = TRUE WHERE id = %s"
                        update_params = (admin_user['id'],)
                        app.logger.info(f"Setting admin status for user '{admin_email}'.")

                    if needs_update:
                        cursor.execute(update_sql, update_params)
                    else:
                        app.logger.info(f"Admin user '{admin_email}' already exists and is correctly configured.")

            except pymysql.MySQLError as e:
                app.logger.error(f"Database error while ensuring admin user '{admin_email}': {e}", exc_info=True)
            finally:
                 if cursor: cursor.close()
                 # close_db() will handle closing the connection via teardown_appcontext

    except Exception as e:
         # Catch errors related to app context or get_db
         app.logger.error(f"Failed to run ensure_admin_user within app context: {e}", exc_info=True)


# Initialize DB and Admin User on startup
try:
    initialize_database()
    ensure_admin_user() # Create/Update the hardcoded admin user
except RuntimeError as e:
     print(f"Critical Error during startup: {e}", file=sys.stderr)
     sys.exit(1) # Exit if DB initialization fails
except ConnectionError as e:
     print(f"Critical Error during startup - DB Connection Failed: {e}", file=sys.stderr)
     sys.exit(1)


# In-memory room tracker: {'ROOMCODE': {'members': count, 'sids': {sid1, sid2,...}}}
rooms = {}

# --- Utility Functions ---
def generate_room_code(length=4):
    """Generates a unique uppercase letter room code."""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=length))
        if code not in rooms and code != "PUBLIC":
            return code

def is_valid_room(room_code):
    """Checks if a room exists either in memory or has history in DB."""
    if room_code in rooms:
        return True
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM messages WHERE room_code = %s LIMIT 1", (room_code,))
        exists_in_db = cursor.fetchone() is not None
        cursor.close()
        if exists_in_db:
            if room_code not in rooms:
                rooms[room_code] = {"members": 0, "sids": set()}
                app.logger.info(f"Re-activated room tracker for '{room_code}' based on DB history check.")
            return True
    except pymysql.MySQLError as e:
         app.logger.error(f"DB error checking room existence for {room_code}: {e}")
         return False
    except ConnectionError as e:
         app.logger.error(f"DB Connection error checking room existence for {room_code}: {e}")
         return False
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
            return redirect(url_for('dashboard')) # Redirect non-admins away
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

            # Insert new user (is_admin defaults to FALSE in DB schema)
            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                           (name, email, hashed_password))

            user_id = cursor.lastrowid
            session['user_id'] = user_id
            session['name'] = name
            session['email'] = email
            session['is_admin'] = False # New users are not admins by default
            session.permanent = True
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

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template('index.html', email=email), 400

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Fetch is_admin status
            cursor.execute("SELECT id, name, email, password, is_admin FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user['password'], password):
                session.permanent = True
                session['user_id'] = user['id']
                session['name'] = user['name']
                session['email'] = user['email']
                session['is_admin'] = user['is_admin'] # Store admin status
                app.logger.info(f"User '{user['name']}' (ID: {user['id']}, Admin: {user['is_admin']}) logged in successfully.")

                next_url = request.args.get('next')
                if next_url and next_url.startswith('/') and not next_url.startswith('//') and ':' not in next_url:
                    app.logger.debug(f"Redirecting logged-in user to requested 'next' URL: {next_url}")
                    return redirect(next_url)
                else:
                    if next_url: app.logger.warning(f"Ignoring potentially unsafe 'next' URL: {next_url}")
                    return redirect(url_for('dashboard'))
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
    # Clear user-specific session data
    session.pop('user_id', None)
    session.pop('name', None)
    session.pop('email', None)
    session.pop('room', None)
    session.pop('is_admin', None) # Clear admin status
    session.pop('reset_otp', None)
    session.pop('reset_email', None)
    session.pop('reset_otp_expires', None)

    flash(f"You have been successfully logged out. Goodbye, {user_name}!", "info")
    app.logger.info(f"User '{user_name}' logged out.")
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required # Use the decorator
def dashboard():
    if 'room' in session:
        app.logger.debug(f"Clearing room '{session.get('room')}' from session on dashboard access for user '{session.get('name')}'")
        session.pop('room', None)

    # Pass admin status to template to conditionally show admin link
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

        if not all([app.config.get('MAIL_USERNAME'), app.config.get('MAIL_PASSWORD'), app.config.get('MAIL_DEFAULT_SENDER')]):
             flash("Password reset emails are currently disabled due to server configuration. Please contact support.", "error")
             app.logger.error("Password reset requested but mail is not configured.")
             return render_template('forgetPassword.html', email=email), 503

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            if user:
                otp = ''.join(random.choices(string.digits, k=6))
                expiry_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)

                session['reset_otp'] = generate_password_hash(otp)
                session['reset_email'] = email
                session['reset_otp_expires'] = expiry_time.isoformat()
                session.permanent = True

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
                    session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                    return render_template('forgetPassword.html', email=email), 500
            else:
                app.logger.warning(f"Password reset requested for non-existent or incorrect email: {email}")
                flash("If an account with that email exists, an OTP has been sent. Please check your inbox (and spam folder).", "info")
                return redirect(url_for('reset_password'))
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

    if not all([reset_email, otp_hash, otp_expires_iso]):
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
         session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
         return redirect(url_for('forget_password'))

    if request.method == 'POST':
        otp_entered = request.form.get('otp')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        errors = []
        if not otp_entered: errors.append("OTP is required.")
        if not new_password: errors.append("New password is required.")
        if not confirm_password: errors.append("Confirm password is required.")
        if new_password != confirm_password: errors.append("The new passwords do not match.")
        elif len(new_password) < 6: errors.append("Password must be at least 6 characters long.")
        if not otp_entered or not check_password_hash(otp_hash, otp_entered):
             errors.append("The OTP entered is invalid or has expired.")

        if errors:
            for error in errors: flash(error, "error")
            return render_template('resetPassword.html'), 400

        new_hashed_password = generate_password_hash(new_password)
        conn = get_db()
        cursor = conn.cursor()
        try:
            rows_affected = cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_hashed_password, reset_email))

            if rows_affected == 1:
                app.logger.info(f"Password successfully reset for email: {reset_email}")
                session.pop('reset_otp', None); session.pop('reset_email', None); session.pop('reset_otp_expires', None)
                flash("Your password has been reset successfully. Please log in with your new password.", "success")
                return redirect(url_for('login'))
            else:
                 app.logger.error(f"Password reset failed for {reset_email}: user not found in DB during update, despite valid OTP session.")
                 flash("An unexpected error occurred while updating your password. Please try again.", "error")
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
        if not room_code_input:
            flash("Please enter a room code to join.", "error")
            return redirect(url_for('dashboard'))
        if len(room_code_input) != 4 or not room_code_input.isalpha():
            flash("Invalid room code format (must be exactly 4 letters).", "error")
            return redirect(url_for('dashboard'))
        if room_code_input == "PUBLIC":
             flash("Cannot join 'PUBLIC' room this way. Use the Public Chat link.", "warning")
             return redirect(url_for('dashboard'))

        # Use is_valid_room which checks DB too
        if not is_valid_room(room_code_input):
             flash(f"Room code '{room_code_input}' does not exist or is invalid.", "error")
             return redirect(url_for('dashboard'))

        target_room_code = room_code_input

    elif create_room_action:
        new_code = generate_room_code()
        rooms[new_code] = {"members": 0, "sids": set()}
        target_room_code = new_code
        app.logger.info(f"User '{name}' (ID: {user_id}) created new private room '{target_room_code}'.")
        # Save a "Room Created" system message? Maybe not needed.
        # save_message(target_room_code, None, "System", f"Room '{target_room_code}' created by {name}.")
    else:
        flash("Invalid action specified.", "error")
        return redirect(url_for('dashboard'))

    session['room'] = target_room_code
    app.logger.info(f"User '{name}' (ID: {user_id}) attempting to enter room '{target_room_code}'.")

    if target_room_code == "PUBLIC":
         return redirect(url_for('public_chat'))
    else:
         return redirect(url_for('room'))

@app.route('/room') # Route for PRIVATE chat rooms
@login_required
def room():
    room_code = session.get('room')
    user_name = session.get('name')

    if not room_code or not user_name:
        flash("No active room selected or user session invalid. Please join or create one from the dashboard.", "warning")
        return redirect(url_for('dashboard'))

    if room_code == "PUBLIC":
         flash("Please use the dedicated Public Chat link from the dashboard.", "warning")
         session.pop('room', None)
         return redirect(url_for('dashboard'))

    # Use is_valid_room to check if room exists in memory or DB
    if not is_valid_room(room_code):
         flash(f"The room '{room_code}' is no longer active or valid.", "error")
         session.pop('room', None)
         return redirect(url_for('dashboard'))

    app.logger.debug(f"Rendering private chat room '{room_code}' for user '{user_name}'.")
    return render_template('chatroom.html', room_code=room_code, username=user_name)

@app.route('/public_chat') # Route specifically for the PUBLIC chat room
@login_required
def public_chat():
    user_name = session.get('name')
    if not user_name:
         flash("User information missing. Please log in again.", "error")
         return redirect(url_for('login'))

    session['room'] = "PUBLIC"

    # Ensure PUBLIC room tracker exists in memory
    if "PUBLIC" not in rooms:
        rooms["PUBLIC"] = {"members": 0, "sids": set()}
        app.logger.info("Initialized PUBLIC room tracker in memory.")

    app.logger.info(f"User '{user_name}' entering public chat (room 'PUBLIC').")
    return render_template('public_chat.html', username=user_name) # Pass username


# --- Admin Routes ---
@app.route('/admin')
@admin_required # Use the decorator
def admin_dashboard():
    """Renders the main admin dashboard page."""
    # Could add stats here later (e.g., user count, active rooms)
    return render_template('admin/admin_dashboard.html', username=session.get('name'))

@app.route('/admin/users')
@admin_required
def admin_users():
    """Displays a list of all registered users."""
    users = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Fetch all users, excluding the current admin viewing the page
        current_user_id = session.get('user_id')
        cursor.execute("SELECT id, name, email, created_at FROM users WHERE id != %s ORDER BY created_at DESC", (current_user_id,))
        users = cursor.fetchall()
        cursor.close()
    except pymysql.MySQLError as e:
        app.logger.error(f"Admin area: Error fetching users list: {e}")
        flash("Could not retrieve user list due to a database error.", "error")
    except ConnectionError as e:
         app.logger.error(f"Admin area: DB Connection error fetching users: {e}")
         flash("Could not connect to the database to retrieve users.", "error")

    return render_template('admin/admin_users.html', users=users, username=session.get('name'))

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """Deletes a user account."""
    current_user_id = session.get('user_id')
    if user_id == current_user_id:
        flash("Admins cannot delete their own account.", "error")
        return redirect(url_for('admin_users'))

    try:
        conn = get_db()
        cursor = conn.cursor()
        # Check if user exists and if they are an admin before deleting
        cursor.execute("SELECT name, is_admin FROM users WHERE id = %s", (user_id,))
        user_to_delete = cursor.fetchone()

        if not user_to_delete:
            flash("User not found.", "error")
            return redirect(url_for('admin_users'))

        # Prevent deleting other admins (policy decision)
        if user_to_delete['is_admin']:
             flash("Cannot delete another administrator account.", "error")
             app.logger.warning(f"Admin {session.get('name')} ({current_user_id}) attempted to delete admin {user_to_delete['name']} ({user_id}). Operation blocked.")
             return redirect(url_for('admin_users'))

        # Proceed with deletion
        # Note: Messages from this user will have user_id set to NULL due to FK constraint
        rows_affected = cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        cursor.close()

        if rows_affected > 0:
            flash(f"User '{user_to_delete['name']}' (ID: {user_id}) has been deleted successfully.", "success")
            app.logger.info(f"Admin {session.get('name')} ({current_user_id}) deleted user {user_to_delete['name']} ({user_id}).")
        else:
            # Should not happen if user was found, but handle defensively
            flash(f"Could not delete user (ID: {user_id}). They may have already been deleted.", "warning")
            app.logger.warning(f"Admin {session.get('name')} ({current_user_id}) attempted delete for user ID {user_id}, but 0 rows were affected.")

    except pymysql.MySQLError as e:
        app.logger.error(f"Admin area: Error deleting user ID {user_id}: {e}")
        flash("Could not delete user due to a database error.", "error")
    except ConnectionError as e:
         app.logger.error(f"Admin area: DB Connection error deleting user ID {user_id}: {e}")
         flash("Could not connect to the database to delete user.", "error")

    return redirect(url_for('admin_users'))

# Potential future admin actions (Update User)
# @app.route('/admin/user/<int:user_id>/edit', methods=['GET', 'POST'])
# @admin_required
# def admin_edit_user(user_id):
#     # Fetch user data for GET
#     # Handle password update (optional), username change, is_admin toggle
#     pass


# --- Database Helper Functions for Chat ---
def get_message_history(room_code, limit=50):
    """Fetches the last 'limit' messages for a given room_code from the database, ordered oldest to newest."""
    app.logger.debug(f"Fetching history for room_code: '{room_code}', Limit: {limit}")
    messages = []
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Fetch in reverse chronological order (latest first) then reverse in Python
        query = """
            SELECT user_id, user_name, content, timestamp
            FROM messages
            WHERE room_code = %s
            ORDER BY timestamp DESC
            LIMIT %s
        """
        cursor.execute(query, (room_code, limit))
        db_messages = list(cursor.fetchall())
        db_messages.reverse() # Order from oldest to newest for display
        app.logger.debug(f"Fetched {len(db_messages)} messages from DB for room '{room_code}'")

        # Format for sending to client (ensure UTC ISO format)
        for msg in db_messages:
             ts_aware = msg['timestamp']
             # Ensure the timestamp is timezone-aware UTC
             if ts_aware.tzinfo is None:
                 ts_aware = ts_aware.replace(tzinfo=datetime.timezone.utc)
             else:
                 ts_aware = ts_aware.astimezone(datetime.timezone.utc)

             messages.append({
                 "name": msg['user_name'],
                 "message": msg['content'],
                 "timestamp": ts_aware.isoformat(), # Use ISO format for JS
                 "user_id": msg['user_id']
                 # Note: System messages are not currently saved to DB
             })
        app.logger.debug(f"Formatted {len(messages)} history messages for room '{room_code}'")

    except pymysql.MySQLError as e:
        app.logger.error(f"Error fetching/formatting history for room '{room_code}': {e}")
    except ConnectionError as e:
         app.logger.error(f"DB Connection error fetching history for room '{room_code}': {e}")
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
    return messages # Return formatted list

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
        success = True
        app.logger.debug(f"Message saved to DB for room '{room_code}'")
    except pymysql.MySQLError as e:
        app.logger.error(f"Error saving message for room '{room_code}' by user {user_name}: {e}")
    except ConnectionError as e:
         app.logger.error(f"DB Connection error saving message for room '{room_code}': {e}")
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
    return success


# --- SocketIO Event Handlers ---

@socketio.on('connect')
def handle_connect():
    """Handles new client connections and sends message history."""
    sid = request.sid
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Connect] SID={sid} attempting connection. Session: User='{user_name}'(ID:{user_id}), Room='{room_code}'")

    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Socket connection REJECTED for SID {sid}: Missing room/user/id in session.")
        socketio.emit('error', {'message': 'Invalid session. Please refresh or log in again.'}, room=sid)
        disconnect(sid)
        return False

    # Ensure room tracker exists / is valid (using is_valid_room checks DB & initializes)
    if room_code not in rooms:
        if not is_valid_room(room_code):
             app.logger.error(f"Room '{room_code}' invalid during connect for SID {sid}. Disconnecting.")
             socketio.emit('error', {'message': f'Room {room_code} is invalid or no longer exists.'}, room=sid)
             disconnect(sid); return False
        # else: tracker initialized by is_valid_room call above

    join_room(room_code)
    rooms[room_code]["members"] += 1
    rooms[room_code]["sids"].add(sid)
    app.logger.info(f"User '{user_name}' (SID: {sid}) successfully CONNECTED to room '{room_code}'. Active members: {rooms[room_code]['members']}.")

    # Send message history
    try:
        history_messages = get_message_history(room_code) # Already formatted correctly
        app.logger.debug(f"Sending {len(history_messages)} history messages to {user_name} (SID: {sid}) in room '{room_code}'")
        socketio.emit('message_history', {'messages': history_messages}, room=sid)
    except Exception as e:
        app.logger.error(f"Error retrieving/sending history for room '{room_code}' to {user_name} (SID: {sid}): {e}", exc_info=True)
        socketio.emit('error', {'message': 'Error loading message history.'}, room=sid)

    # Notify others about the join (exclude self)
    join_message_content = f"{user_name} has joined the room."
    join_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    join_message = {
        "name": "System", "message": join_message_content,
        "timestamp": join_ts, "user_id": None, "isSystem": True
        }
    socketio.emit('message', join_message, to=room_code, skip_sid=sid)
    app.logger.debug(f"Sent join notification for {user_name} to room '{room_code}' (skipped SID {sid})")
    return True


@socketio.on('disconnect')
def handle_disconnect():
    """Handles client disconnections."""
    sid = request.sid
    disconnected_room = None
    user_name = "Unknown User" # Default

    # Find the room the disconnecting SID was in
    for room_code, room_data in list(rooms.items()): # Iterate over copy if modifying dict
        if sid in room_data.get("sids", set()):
            disconnected_room = room_code
            user_name = session.get('name', user_name) # Try to get name from session
            break

    app.logger.debug(f"Disconnect attempt: SID={sid}. Found in room: '{disconnected_room}'. User from session (if available): '{user_name}'")

    if disconnected_room and disconnected_room in rooms:
        leave_room(disconnected_room) # Tell SocketIO user left room

        # Update our tracker
        if sid in rooms[disconnected_room]["sids"]:
             rooms[disconnected_room]["sids"].remove(sid)
        rooms[disconnected_room]["members"] = max(0, rooms[disconnected_room]["members"] - 1)

        current_members = rooms[disconnected_room]["members"]
        app.logger.info(f"User '{user_name}' (SID: {sid}) DISCONNECTED from room '{disconnected_room}'. Remaining members: {current_members}.")

        # Notify remaining users
        leave_message_content = f"{user_name} has left the room."
        leave_ts = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
        leave_message = {
            "name": "System", "message": leave_message_content,
            "timestamp": leave_ts, "user_id": None, "isSystem": True
            }
        socketio.emit('message', leave_message, to=disconnected_room)

        # Clean up empty private room tracker (keep PUBLIC)
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
    if not isinstance(data, dict) or 'data' not in data:
        app.logger.warning(f"Invalid message format received from SID {sid}. Data: {data}")
        socketio.emit('error', {'message': 'Invalid message format.'}, room=sid); return

    message_text = str(data.get('data', ''))
    room_code = session.get('room')
    user_name = session.get('name')
    user_id = session.get('user_id')

    app.logger.debug(f"[Message] SID={sid}, User={user_name}({user_id}), Room='{room_code}', Rcvd='{message_text[:50]}...'")

    if not all([room_code, user_name, user_id]):
        app.logger.warning(f"Message REJECTED from SID {sid}: Missing session data. Data: {message_text[:50]}...")
        socketio.emit('error', {'message': 'Cannot send message: Invalid session.'}, room=sid); return

    # Verify SID is actually in the room claimed by session (consistency check)
    if room_code not in rooms or sid not in rooms[room_code].get("sids", set()):
         app.logger.error(f"Message REJECTED from SID {sid}: Session/tracker mismatch for room '{room_code}'.")
         socketio.emit('error', {'message': 'Room connection mismatch. Please refresh.'}, room=sid)
         return

    message_content = message_text.strip()
    if not message_content:
        app.logger.debug(f"Empty message REJECTED from {user_name} (SID: {sid}) in room '{room_code}'.")
        return

    MAX_MSG_LENGTH = 2000
    if len(message_content) > MAX_MSG_LENGTH:
        app.logger.warning(f"Message REJECTED from {user_name} (SID: {sid}): Too long ({len(message_content)} chars).")
        socketio.emit('error', {'message': f'Message is too long (max {MAX_MSG_LENGTH} characters).'}, room=sid); return

    # Save the message to the database FIRST
    if not save_message(room_code, user_id, user_name, message_content):
        app.logger.error(f"Failed to save message to DB for room '{room_code}' from {user_name} (SID: {sid}).")
        socketio.emit('error', {'message': 'Failed to save message to server. Please try again.'}, room=sid); return

    # Broadcast the message to the room
    timestamp_now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    content_to_broadcast = {
        "name": user_name,
        "message": message_content, # Send the original (stripped) message
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
    # Optionally emit a generic error back to the client
    if request and sid:
        try: socketio.emit('error', {'message': 'An internal server error occurred via SocketIO.'}, room=sid)
        except Exception as emit_err: app.logger.error(f"Error emitting error message to SID {sid}: {emit_err}")


@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 Not Found errors."""
    app.logger.warning(f"404 Not Found: {request.url} (Referrer: {request.referrer})")
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Handle 500 Internal Server errors."""
    app.logger.error(f"500 Internal Server Error handling request for {request.url}: {e}", exc_info=True)
    close_db(e)
    return render_template('500.html'), 500

@app.errorhandler(ConnectionError)
def handle_db_connection_error(e):
    """Handle database connection errors gracefully during requests."""
    app.logger.critical(f"Database Connection Error during request {request.url}: {e}", exc_info=app.debug)
    close_db(e) # Ensure context cleanup
    flash("The service is temporarily unavailable due to a database connection issue. Please try again later.", "error")
    if request.endpoint and request.endpoint not in ['static', 'home']:
         return redirect(url_for('home'))
    return render_template('500.html', error_message="Database connection error."), 503


@app.errorhandler(Exception)
def handle_exception(e):
    """Handle other uncaught exceptions."""
    # Log detailed error
    app.logger.error(f"Unhandled Exception handling request for {request.url}: {e}", exc_info=True)

    # Specific handling based on error type if needed
    if isinstance(e, (pymysql.MySQLError)):
        flash("An unexpected database error occurred.", "error")
    # Add other specific error types if necessary

    # Ensure DB connection is closed
    close_db(e)

    # Render generic 500 page
    return render_template('500.html'), 500


# --- Run the App ---
if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    host = os.getenv('HOST', '0.0.0.0')
    debug_mode = app.config['DEBUG']

    app.logger.info(f"Starting Flask-SocketIO application...")
    app.logger.info(f" ---> Host: {host}")
    app.logger.info(f" ---> Port: {port}")
    app.logger.info(f" ---> Debug Mode: {debug_mode}")
    app.logger.info(f" ---> Async Mode: {ASYNC_MODE or 'Werkzeug (Default - Dev Only!)'}")

    try:
        socketio.run(app, host=host, port=port, debug=debug_mode, use_reloader=debug_mode)
    except Exception as start_error:
         app.logger.critical(f"Failed to start application: {start_error}", exc_info=True)
         sys.exit(1)

# --- END OF FILE app.py ---