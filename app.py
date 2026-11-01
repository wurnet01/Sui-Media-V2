from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from db_config import db, init_db
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import re
import uuid
import ipaddress
import socket
from urllib.parse import urlsplit
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
try:
    import requests
    from bs4 import BeautifulSoup
    LINK_PREVIEW_ENABLED = os.environ.get('ENABLE_LINK_PREVIEWS') == '1'
except ImportError:
    LINK_PREVIEW_ENABLED = False

load_dotenv()

app = Flask(__name__)
secret_key = os.environ.get('SECRET_KEY')
if not secret_key and (os.environ.get('APP_ENV') == 'production' or os.environ.get('RENDER')):
    raise RuntimeError('SECRET_KEY must be configured in production.')
if not secret_key:
    if os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('PYTEST_CURRENT_TEST'):
        secret_key = 'local-development-only-change-this-key'
    else:
        # Keep local imports convenient, but deployment documentation requires a real secret.
        secret_key = 'unsafe-development-key-set-SECRET_KEY-before-deploying'
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'auto').lower() != 'false' and os.environ.get('FLASK_DEBUG') != '1',
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    PREFERRED_URL_SCHEME='https' if os.environ.get('COOKIE_SECURE', 'auto').lower() != 'false' else 'http',
    WTF_CSRF_ENABLED=True,
    WTF_CSRF_TIME_LIMIT=3600,
)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
    strategy='fixed-window',
    default_limits=[],
)
app.config['RATELIMIT_HEADERS_ENABLED'] = True
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Initialize SocketIO
allowed_origins = [origin.strip() for origin in os.environ.get('SOCKETIO_ALLOWED_ORIGINS', '').split(',') if origin.strip()]
socketio = SocketIO(app, cors_allowed_origins=allowed_origins or None, manage_session=True)
ALLOWED_EXTENSIONS = {
    'image': {'png', 'jpg', 'jpeg', 'gif', 'webp'},
    'video': {'mp4', 'webm', 'mov'}
}

app.config['MAX_UPLOAD_SIZE_BYTES'] = app.config['MAX_CONTENT_LENGTH']

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'; media-src 'self'"
    return response


def sanitize_text(value):
    if value is None:
        return ''
    value = str(value).strip()
    value = value.replace('\x00', '')
    return value


def validate_username(value):
    value = sanitize_text(value)
    if len(value) < 3 or len(value) > 25:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        return None
    return value


def validate_email(value):
    value = sanitize_text(value)
    return bool(re.fullmatch(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', value)) and len(value) <= 254


def validate_password(value):
    value = str(value or '').strip()
    return len(value) >= 8 and len(value) <= 128

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize database
init_db(app)


def ensure_schema():
    with app.app_context():
        db.create_all()
        inspector = db.inspect(db.engine)
        tables = set(inspector.get_table_names())

        if 'story' not in tables:
            db.session.execute(db.text('CREATE TABLE story (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, caption TEXT, media_type VARCHAR(20), media_file VARCHAR(255), created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, FOREIGN KEY(user_id) REFERENCES user(id))'))
        if 'story_view' not in tables:
            db.session.execute(db.text('CREATE TABLE story_view (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, viewer_id INTEGER NOT NULL, viewed_at DATETIME, FOREIGN KEY(story_id) REFERENCES story(id), FOREIGN KEY(viewer_id) REFERENCES user(id))'))
        if 'story_reply' not in tables:
            db.session.execute(db.text('CREATE TABLE story_reply (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, user_id INTEGER NOT NULL, content TEXT NOT NULL, created_at DATETIME, FOREIGN KEY(story_id) REFERENCES story(id), FOREIGN KEY(user_id) REFERENCES user(id))'))
        if 'saved_post' not in tables:
            db.session.execute(db.text('CREATE TABLE saved_post (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, post_id INTEGER NOT NULL, timestamp DATETIME, FOREIGN KEY(user_id) REFERENCES user(id), FOREIGN KEY(post_id) REFERENCES post(id))'))
        if 'report' not in tables:
            db.session.execute(db.text('CREATE TABLE report (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL, reported_user_id INTEGER NOT NULL, reason VARCHAR(200) NOT NULL, created_at DATETIME, FOREIGN KEY(reporter_id) REFERENCES user(id), FOREIGN KEY(reported_user_id) REFERENCES user(id))'))
        if 'story_reaction' not in tables:
            db.session.execute(db.text('CREATE TABLE story_reaction (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, story_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reaction_type VARCHAR(20) NOT NULL, created_at DATETIME, FOREIGN KEY(story_id) REFERENCES story(id), FOREIGN KEY(user_id) REFERENCES user(id))'))

        for table, col, dtype in [
            ('like', 'reaction_type', 'VARCHAR(20)'),
            ('comment', 'parent_id', 'INTEGER'),
            ('story_reply', 'user_id', 'INTEGER'),
            ('story_view', 'viewer_id', 'INTEGER')
        ]:
            cols = [info['name'] for info in inspector.get_columns(table)] if table in tables else []
            if col not in cols:
                db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col} {dtype}'))
        db.session.commit()

# Initialize Flask-Migrate
migrate = Migrate(app, db)

# Initialize Flask-Admin
admin = Admin(app, name='Database Admin')

class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    show_online_status = db.Column(db.Boolean, default=True)
    theme = db.Column(db.String(20), default='light')  # 'light', 'dark', or 'dynamic'
    last_seen = db.Column(db.DateTime)
    is_online = db.Column(db.Boolean, default=False)

class Education(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    institution = db.Column(db.String(200), nullable=False)
    degree = db.Column(db.String(100), nullable=True)
    field_of_study = db.Column(db.String(100), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    is_current = db.Column(db.Boolean, default=False)
    type = db.Column(db.String(20))  # 'school', 'university', etc.

class BlockedUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    blocked_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# Friends association table
friends = db.Table('friends',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('friend_id', db.Integer, db.ForeignKey('user.id'), primary_key=True)
)

class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reported_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reason = db.Column(db.String(200), nullable=False, default='spam')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SavedPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Story(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    caption = db.Column(db.Text, nullable=True)
    media_type = db.Column(db.String(20), nullable=True)
    media_file = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, default=lambda: datetime.utcnow(), nullable=False)
    user = db.relationship('User', backref='stories', lazy=True)

    @property
    def is_expired(self):
        return datetime.utcnow() > self.expires_at

class StoryView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    viewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow)

class StoryReply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class StoryReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reaction_type = db.Column(db.String(20), nullable=False, default='like')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# User Model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    profile_picture = db.Column(db.String(200), default='default-avatar.svg')
    bio = db.Column(db.Text, default='')
    cover_photo = db.Column(db.String(200), default='default-cover.svg')
    date_of_birth = db.Column(db.Date, nullable=True)
    place_of_birth = db.Column(db.String(100), nullable=True)
    marital_status = db.Column(db.String(20), nullable=True)  # Single, Married, etc.
    current_city = db.Column(db.String(100), nullable=True)
    current_job = db.Column(db.String(200), nullable=True)
    job_title = db.Column(db.String(100), nullable=True)
    company = db.Column(db.String(100), nullable=True)
    education = db.relationship('Education', backref='user', lazy=True)
    posts = db.relationship('Post', backref='author', lazy=True)
    saved_posts = db.relationship('SavedPost', backref='user', lazy=True)
    messages_sent = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy=True)
    messages_received = db.relationship('Message', foreign_keys='Message.recipient_id', backref='recipient', lazy=True)
    notifications = db.relationship('Notification', backref='user', lazy=True)
    comments = db.relationship('Comment', backref='author', lazy=True)
    settings = db.relationship('UserSettings', backref='user', lazy=True, uselist=False)
    blocked_users = db.relationship('BlockedUser', 
        foreign_keys='BlockedUser.user_id',
        backref='blocker', lazy='dynamic')
    blocked_by = db.relationship('BlockedUser',
        foreign_keys='BlockedUser.blocked_user_id',
        backref='blocked', lazy='dynamic')
    friends = db.relationship(
        'User', secondary=friends,
        primaryjoin=(friends.c.user_id == id),
        secondaryjoin=(friends.c.friend_id == id),
        backref=db.backref('friend_of', lazy='dynamic'),
        lazy='dynamic'
    )
    friend_requests_sent = db.relationship('FriendRequest',
        foreign_keys='FriendRequest.sender_id',
        backref='sender', lazy='dynamic')
    friend_requests_received = db.relationship('FriendRequest',
        foreign_keys='FriendRequest.recipient_id',
        backref='recipient', lazy='dynamic')
    
    @property
    def get_profile_picture(self):
        if not self.profile_picture:
            return 'default-avatar.svg'
        if '/' in self.profile_picture:
            return self.profile_picture
        if not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], self.profile_picture)):
            return 'default.jpg'
        return self.profile_picture

    def get_settings(self):
        if not self.settings:
            self.settings = UserSettings(user_id=self.id)
            db.session.add(self.settings)
            db.session.commit()
        return self.settings

    @property
    def is_online(self):
        settings = self.get_settings()
        if not settings.show_online_status:
            return False
        return settings.is_online

    def set_online_status(self, status):
        settings = self.get_settings()
        settings.is_online = status
        settings.last_seen = datetime.utcnow()
        db.session.commit()

    def update_last_seen(self):
        settings = self.get_settings()
        settings.last_seen = datetime.utcnow()
        settings.is_online = True
        db.session.commit()

    def set_offline(self):
        settings = self.get_settings()
        settings.is_online = False
        db.session.commit()

    def block_user(self, user):
        if not self.has_blocked(user):
            block = BlockedUser(user_id=self.id, blocked_user_id=user.id)
            db.session.add(block)
            # Remove from friends if they are friends
            if self.is_friend(user):
                self.remove_friend(user)
            db.session.commit()

    def is_friend(self, user):
        return self.friends.filter_by(id=user.id).first() is not None

    def add_friend(self, user):
        if not self.is_friend(user):
            self.friends.append(user)
            user.friends.append(self)
            db.session.commit()

    def remove_friend(self, user):
        if self.is_friend(user):
            self.friends.remove(user)
            user.friends.remove(self)
            db.session.commit()

    def unblock_user(self, user):
        block = BlockedUser.query.filter_by(user_id=self.id, blocked_user_id=user.id).first()
        if block:
            db.session.delete(block)
            db.session.commit()

    def has_blocked(self, user):
        return BlockedUser.query.filter_by(user_id=self.id, blocked_user_id=user.id).first() is not None

    def is_blocked_by(self, user):
        return BlockedUser.query.filter_by(user_id=user.id, blocked_user_id=self.id).first() is not None
        block = BlockedUser.query.filter_by(
            user_id=self.id,
            blocked_user_id=user.id
        ).first()
        if block:
            db.session.delete(block)
            db.session.commit()

    def has_blocked(self, user):
        return BlockedUser.query.filter_by(
            user_id=self.id,
            blocked_user_id=user.id
        ).first() is not None

    def is_blocked_by(self, user):
        return BlockedUser.query.filter_by(
            user_id=user.id,
            blocked_user_id=self.id
        ).first() is not None
    
    @property
    def get_cover_photo(self):
        if not self.cover_photo or not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], self.cover_photo)):
            return 'default-cover.svg'
        return self.cover_photo
    
    def send_friend_request(self, user):
        if not self.has_sent_friend_request(user):
            request = FriendRequest(sender=self, recipient=user)
            db.session.add(request)
            notification = Notification(
                user=user,
                content=f"{self.username} sent you a friend request",
                notification_type="friend_request"
            )
            db.session.add(notification)
            db.session.commit()
    
    def accept_friend_request(self, user):
        request = FriendRequest.query.filter_by(sender_id=user.id, recipient_id=self.id).first()
        if request:
            self.friends.append(user)
            user.friends.append(self)
            db.session.delete(request)
            notification = Notification(
                user=user,
                content=f"{self.username} accepted your friend request",
                notification_type="friend_accept"
            )
            db.session.add(notification)
            db.session.commit()
    
    def has_sent_friend_request(self, user):
        return FriendRequest.query.filter_by(
            sender_id=self.id,
            recipient_id=user.id
        ).first() is not None
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Post Model
class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=True)  # Explicitly nullable
    media_type = db.Column(db.String(10), nullable=True)  # 'image' or 'video'
    media_file = db.Column(db.String(200), nullable=True)
    feeling = db.Column(db.String(50), nullable=True)  # Store the selected emoji feeling
    link = db.Column(db.String(2048), nullable=True)  # Store URLs
    link_title = db.Column(db.String(200), nullable=True)  # Store link preview title
    link_image = db.Column(db.String(2048), nullable=True)  # Store link preview image URL
    link_description = db.Column(db.Text, nullable=True)  # Store link preview description
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    likes = db.relationship('Like', backref='post', lazy=True)
    comments = db.relationship('Comment', backref='post', lazy=True)
    
    @property
    def media_url(self):
        if self.media_file and os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], self.media_file)):
            return url_for('uploaded_file', filename=self.media_file)
        return None

# Like Model
class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    reaction_type = db.Column(db.String(20), default='like')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    settings = current_user.get_settings()
    if request.method == 'POST':
        show_online = request.form.get('show_online_status') == 'on'
        theme = request.form.get('theme', 'dark')
        if theme not in {'light', 'dark', 'dynamic'}:
            flash('Choose a valid theme.', 'error')
            return redirect(url_for('settings'))
        settings.show_online_status = show_online
        settings.theme = theme
        db.session.commit()
        flash('Settings updated successfully!', 'success')
        return redirect(url_for('settings'))
    
    return render_template('settings.html', settings=settings)

@app.route('/block/<int:user_id>', methods=['POST'])
@login_required
def block_user(user_id):
    user = User.query.get_or_404(user_id)
    if user == current_user or user.has_blocked(current_user) or current_user.has_blocked(user):
        flash('You cannot block yourself!', 'error')
        return redirect(url_for('profile', username=user.username))
    
    current_user.block_user(user)
    FriendRequest.query.filter(
        db.or_(
            db.and_(FriendRequest.sender_id == current_user.id, FriendRequest.recipient_id == user.id),
            db.and_(FriendRequest.sender_id == user.id, FriendRequest.recipient_id == current_user.id)
        )
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f'You have blocked {user.username}', 'success')
    return redirect(url_for('profile', username=user.username))

@app.route('/unblock/<int:user_id>', methods=['POST'])
@login_required
def unblock_user(user_id):
    user = User.query.get_or_404(user_id)
    current_user.unblock_user(user)
    flash(f'You have unblocked {user.username}', 'success')
    return redirect(url_for('profile', username=user.username))

@app.route('/report/<int:user_id>', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def report_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot report yourself.', 'error')
        return redirect(url_for('profile', username=user.username))
    reason = sanitize_text(request.form.get('reason', 'spam')) or 'spam'
    if not Report.query.filter_by(reporter_id=current_user.id, reported_user_id=user.id).first():
        db.session.add(Report(reporter_id=current_user.id, reported_user_id=user.id, reason=reason))
        db.session.add(Notification(user_id=user.id, content=f'{current_user.username} reported your profile for: {reason}', notification_type='report'))
        db.session.commit()
    flash(f'{user.username} has been reported.', 'success')
    return redirect(request.referrer or url_for('profile', username=user.username))

@app.before_request
def before_request():
    if current_user.is_authenticated:
        current_user.update_last_seen()
        
@app.route('/toggle_online_status', methods=['POST'])
@login_required
def toggle_online_status():
    settings = current_user.get_settings()
    settings.show_online_status = not settings.show_online_status
    db.session.commit()
    status = 'visible' if settings.show_online_status else 'hidden'
    flash(f'Your online status is now {status}', 'success')
    return redirect(url_for('settings'))
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# Comment Model
class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
    replies = db.relationship('Comment', backref=db.backref('parent', remote_side='Comment.id'), lazy='dynamic')

# Message Model
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text)
    media_type = db.Column(db.String(10))  # 'image' or 'video'
    media_file = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)
    
    @property
    def media_url(self):
        if self.media_file and os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], self.media_file)):
            return url_for('uploaded_file', filename=self.media_file)
        return None

ensure_schema()

class SecureModelView(ModelView):
    def is_accessible(self):
        admin_username = os.environ.get('ADMIN_USERNAME')
        return current_user.is_authenticated and current_user.username== admin_username

# Add models to admin interface after all models are defined

admin.add_view(SecureModelView(User, db.session))
admin.add_view(SecureModelView(Post, db.session))
admin.add_view(SecureModelView(Like, db.session))
admin.add_view(SecureModelView(Comment, db.session))
admin.add_view(SecureModelView(Message, db.session))
admin.add_view(SecureModelView(FriendRequest, db.session))
admin.add_view(SecureModelView(Notification, db.session))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        active_feed = request.args.get('feed', 'for-you')
        if active_feed not in {'for-you', 'following', 'recent'}:
            active_feed = 'for-you'
        posts_query = Post.query.order_by(Post.timestamp.desc())
        if active_feed == 'following':
            followed_ids = [friend.id for friend in current_user.friends.all()]
            posts_query = posts_query.filter(Post.user_id.in_(followed_ids + [current_user.id]))
        posts = posts_query.all()
        active_story_rows = Story.query.filter(
            Story.expires_at > datetime.utcnow()
        ).order_by(Story.created_at.desc()).all()

        # Recommendations must be genuine accounts and must not include the
        # current user, existing friends, or either side of a block.
        friend_ids = [friend.id for friend in current_user.friends.all()]
        blocked_ids = [
            row.blocked_user_id
            for row in BlockedUser.query.filter_by(user_id=current_user.id).all()
        ]
        blocked_by_ids = [
            row.user_id
            for row in BlockedUser.query.filter_by(blocked_user_id=current_user.id).all()
        ]
        excluded_ids = set(friend_ids + blocked_ids + blocked_by_ids + [current_user.id])
        suggestions_query = User.query
        if excluded_ids:
            suggestions_query = suggestions_query.filter(~User.id.in_(excluded_ids))
        suggest_users = suggestions_query.order_by(User.id.desc()).limit(6).all()
        posts = [post for post in posts if post.user_id not in blocked_ids and post.user_id not in blocked_by_ids]
        active_story_rows = [story for story in active_story_rows if story.user_id not in blocked_ids and story.user_id not in blocked_by_ids]

        return render_template('index.html', posts=posts, stories=active_story_rows, suggest_users=suggest_users, active_feed=active_feed)
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit('5 per hour')
def register():
    if request.method == 'POST':
        username = validate_username(request.form.get('username', ''))
        email = sanitize_text(request.form.get('email', '')).lower()
        password = request.form.get('password', '')

        if not username:
            flash('Username must be 3-25 characters and contain only letters, numbers, periods, underscores, or dashes.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'error')
            return redirect(url_for('register'))

        if not validate_email(email):
            flash('Please provide a valid email address.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered', 'error')
            return redirect(url_for('register'))

        if not validate_password(password):
            flash('Password must be between 8 and 128 characters.', 'error')
            return redirect(url_for('register'))

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash('Registration successful. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login():
    if request.method == 'POST':
        username = validate_username(request.form.get('username', ''))
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first() if username else None
        if user and user.check_password(password):
            login_user(user)
            user.set_online_status(True)
            return redirect(url_for('index'))
        flash('Invalid username or password', 'error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    current_user.set_online_status(False)
    logout_user()
    return redirect(url_for('login'))

def allowed_file(filename, file_type='image'):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS.get(file_type, set())

def extract_url_from_content(content):
    # Regular expression to find URLs in text
    url_pattern = r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)'
    urls = re.findall(url_pattern, content)
    return urls[0] if urls else None

def get_link_preview(url):
    if not LINK_PREVIEW_ENABLED:
        return {
            'title': '',
            'description': '',
            'image': ''
        }
    
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return {'title': '', 'description': '', 'image': ''}
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not resolved or any(not ipaddress.ip_address(item[4][0]).is_global for item in resolved):
            return {'title': '', 'description': '', 'image': ''}
        # Never follow redirects: a public preview URL must not redirect to a private host.
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=False)
        if response.status_code != 200:
            return {'title': '', 'description': '', 'image': ''}
        soup = BeautifulSoup(response.text, 'html.parser')

        # Get title
        title = soup.title.string if soup.title else ''

        # Get description from meta tags
        description = ''
        meta_desc = soup.find('meta', {'name': ['description', 'og:description'], 'content': True})
        if meta_desc:
            description = meta_desc.get('content')

        # Get preview image from meta tags
        image = ''
        meta_img = soup.find('meta', {'property': 'og:image', 'content': True})
        if meta_img:
            image = meta_img.get('content')

        return {
            'title': title[:200] if title else '',
            'description': description[:500] if description else '',
            'image': image[:2048] if image else ''
        }
    except Exception as e:
        print(f"Error getting link preview: {str(e)}")
        return {
            'title': '',
            'description': '',
            'image': ''
        }

def has_valid_media_signature(file, file_type):
    """Reject files whose bytes do not match their claimed image/video extension."""
    try:
        position = file.stream.tell()
        header = file.stream.read(16)
        file.stream.seek(position)
    except (AttributeError, OSError):
        return False
    if file_type == 'image':
        return (
            header.startswith(b'\x89PNG\r\n\x1a\n') or
            header.startswith(b'\xff\xd8\xff') or
            header.startswith((b'GIF87a', b'GIF89a')) or
            (header.startswith(b'RIFF') and header[8:12] == b'WEBP')
        )
    if file_type == 'video':
        return (len(header) >= 12 and header[4:8] == b'ftyp') or header.startswith(b'\x1a\x45\xdf\xa3')
    return False


def save_media(file, file_type='image'):
    if not file or not file.filename:
        return None
    if not allowed_file(file.filename, file_type) or not has_valid_media_signature(file, file_type):
        return None
    if file.content_length and file.content_length > app.config['MAX_CONTENT_LENGTH']:
        return None
    filename = secure_filename(f"{uuid.uuid4()}_{file.filename}")
    media_folder = os.path.join(app.config['UPLOAD_FOLDER'], file_type)
    os.makedirs(media_folder, exist_ok=True)
    file_path = os.path.join(media_folder, filename)
    file.save(file_path)
    return f"{file_type}/{filename}"

def get_file_type(filename):
    if not filename or '.' not in filename:
        return None
    ext = filename.rsplit('.', 1)[1].lower()
    if ext in ALLOWED_EXTENSIONS['image']:
        return 'image'
    elif ext in ALLOWED_EXTENSIONS['video']:
        return 'video'
    return None

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    if filename in {'default-avatar.svg', 'default-cover.svg'}:
        return send_from_directory(os.path.join(app.root_path, 'static', 'images'), filename)
    # Split the path into media type and actual filename
    if '/' in filename:
        media_type, actual_filename = filename.split('/', 1)
        if media_type not in {'image', 'video'}:
            return jsonify({'error': 'Not found'}), 404
        return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], media_type), actual_filename)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/create_post', methods=['POST'])
@login_required
@limiter.limit('30 per hour')
def create_post():
    content = request.form.get('content', '')
    media = request.files.get('media')
    feeling = request.form.get('feeling', '')

    content = sanitize_text(content)[:10000] if content else None
    feeling = sanitize_text(feeling)[:50] if feeling else None

    media_type = None
    media_filename = None
    if media and media.filename:
        media_type = get_file_type(media.filename)
        if media_type:
            media_filename = save_media(media, media_type)

    link = None
    link_title = None
    link_description = None
    link_image = None

    if content:
        url = extract_url_from_content(content)
        if url:
            link = url
            preview = get_link_preview(url)
            link_title = preview['title']
            link_description = preview['description']
            link_image = preview['image']

    if content or media_filename or feeling or link:
        post = Post(
            content=content,
            media_type=media_type,
            media_file=media_filename,
            feeling=feeling,
            link=link,
            link_title=link_title,
            link_description=link_description,
            link_image=link_image,
            author=current_user
        )
        db.session.add(post)
        db.session.commit()
        flash('Post created successfully!', 'success')
    return redirect(url_for('index'))

@app.route('/create_story', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def create_story():
    caption = sanitize_text(request.form.get('caption', ''))[:2000]
    media = request.files.get('story_media')
    if not media or not media.filename:
        flash('Add a photo or video to publish a story.', 'error')
        return redirect(request.referrer or url_for('index'))

    media_type = get_file_type(media.filename)
    if not media_type:
        flash('Story media must be an image or video.', 'error')
        return redirect(request.referrer or url_for('index'))

    media_filename = save_media(media, media_type)
    if not media_filename:
        flash('Story upload failed.', 'error')
        return redirect(request.referrer or url_for('index'))

    story = Story(
        user_id=current_user.id,
        caption=caption or None,
        media_type=media_type,
        media_file=media_filename,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.session.add(story)
    db.session.commit()
    flash('Your story was published.', 'success')
    return redirect(request.referrer or url_for('index'))

@app.route('/story/<int:story_id>/view', methods=['POST'])
@login_required
def view_story(story_id):
    story = Story.query.get_or_404(story_id)
    if story.is_expired:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'expired'}), 410
        flash('This story has expired.', 'error')
        return redirect(request.referrer or url_for('index'))
    if story.user_id != current_user.id:
        existing_view = StoryView.query.filter_by(story_id=story.id, viewer_id=current_user.id).first()
        if not existing_view:
            db.session.add(StoryView(story_id=story.id, viewer_id=current_user.id))
            db.session.commit()
    return jsonify({
        'status': 'ok',
        'story_id': story.id,
        'caption': story.caption or '',
        'media_url': url_for('uploaded_file', filename=story.media_file) if story.media_file else '',
        'username': story.user.username,
        'media_type': story.media_type,
        'avatar_url': url_for('uploaded_file', filename=story.user.get_profile_picture),
        'views': StoryView.query.filter_by(story_id=story.id).count(),
    })

@app.route('/story/<int:story_id>/react', methods=['POST'])
@login_required
def react_to_story(story_id):
    story = Story.query.get_or_404(story_id)
    if story.is_expired:
        return jsonify({'status': 'error', 'message': 'This story has expired.'}), 410
    reaction_type = (request.form.get('reaction_type') or 'like').strip().lower()
    if reaction_type not in {'like', 'love', 'wow', 'fire'}:
        reaction_type = 'like'
    existing = StoryReaction.query.filter_by(story_id=story.id, user_id=current_user.id).first()
    if existing:
        existing.reaction_type = reaction_type
    else:
        db.session.add(StoryReaction(story_id=story.id, user_id=current_user.id, reaction_type=reaction_type))
    db.session.commit()
    return jsonify({'status': 'ok', 'reaction_type': reaction_type})

@app.route('/story/<int:story_id>/reply', methods=['POST'])
@login_required
def reply_to_story(story_id):
    story = Story.query.get_or_404(story_id)
    if story.is_expired:
        return jsonify({'status': 'error', 'message': 'This story has expired.'}), 410
    content = sanitize_text(request.form.get('content', ''))[:2000]
    if not content:
        return jsonify({'status': 'error', 'message': 'Reply cannot be empty'}), 400
    reply = StoryReply(story_id=story.id, user_id=current_user.id, content=content)
    db.session.add(reply)
    if story.user_id != current_user.id:
        db.session.add(Notification(user_id=story.user_id, content=f'{current_user.username} replied to your story.', notification_type='story_reply'))
    db.session.commit()
    return jsonify({'status': 'ok', 'username': current_user.username, 'content': content})

@app.route('/like/<int:post_id>', methods=['POST'])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user.has_blocked(post.author) or current_user.is_blocked_by(post.author):
        return jsonify({'status': 'error', 'message': 'This post is unavailable.'}), 403
    reaction_type = (request.form.get('reaction_type') or 'like').strip().lower()
    if reaction_type not in {'like', 'love', 'wow', 'fire'}:
        reaction_type = 'like'

    existing_like = Like.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    liked = False
    if existing_like:
        if existing_like.reaction_type == reaction_type:
            db.session.delete(existing_like)
        else:
            existing_like.reaction_type = reaction_type
            liked = True
    else:
        db.session.add(Like(user_id=current_user.id, post_id=post_id, reaction_type=reaction_type))
        liked = True

    if liked and post.author.id != current_user.id:
        db.session.add(Notification(user_id=post.author.id, content=f'{current_user.username} reacted to your post.', notification_type='like'))
    db.session.commit()
    count = Like.query.filter_by(post_id=post_id).count()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'liked': liked, 'count': count, 'reaction_type': reaction_type})
    return redirect(request.referrer or url_for('index'))

@app.route('/comment/<int:post_id>', methods=['POST'])
@login_required
@limiter.limit('60 per hour')
def add_comment(post_id):
    content = sanitize_text(request.form.get('content', ''))
    if not content:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'Comment cannot be empty'})
        flash('Comment cannot be empty', 'error')
        return redirect(request.referrer or url_for('index'))

    post = Post.query.get_or_404(post_id)
    if current_user.has_blocked(post.author) or current_user.is_blocked_by(post.author):
        return jsonify({'status': 'error', 'message': 'This post is unavailable.'}), 403
    parent_id_raw = request.form.get('parent_id', '').strip()
    parent = None
    if parent_id_raw:
        if not parent_id_raw.isdigit():
            return jsonify({'status': 'error', 'message': 'Invalid reply target'}), 400
        parent = Comment.query.filter_by(id=int(parent_id_raw), post_id=post.id).first()
        if parent is None or parent.parent_id is not None:
            return jsonify({'status': 'error', 'message': 'The comment you are replying to does not exist'}), 404

    comment = Comment(
        content=content[:5000],
        user_id=current_user.id,
        post_id=post.id,
        parent_id=parent.id if parent else None
    )
    db.session.add(comment)

    # Persist an actual notification for the post owner or parent-comment author.
    notification_target = parent.author if parent else post.author
    if notification_target.id != current_user.id:
        db.session.add(Notification(
            user_id=notification_target.id,
            content=(
                f'{current_user.username} replied to your comment.'
                if parent else f'{current_user.username} commented on your post.'
            ),
            notification_type='comment_reply' if parent else 'comment'
        ))
    db.session.commit()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'status': 'ok',
            'comment_id': comment.id,
            'username': current_user.username,
            'content': comment.content,
            'avatar': current_user.get_profile_picture,
            'parent_id': comment.parent_id,
            'timestamp': comment.timestamp.strftime('%b %d')
        })
    return redirect(request.referrer or url_for('index'))

@app.route('/save_post/<int:post_id>', methods=['POST'])
@login_required
def save_post(post_id):
    Post.query.get_or_404(post_id)
    existing = SavedPost.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    if existing:
        db.session.delete(existing)
        saved = False
    else:
        db.session.add(SavedPost(user_id=current_user.id, post_id=post_id))
        saved = True
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'saved': saved})
    return redirect(request.referrer or url_for('index'))

@app.route('/share/<int:post_id>', methods=['POST'])
@login_required
def share_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user.has_blocked(post.author) or current_user.is_blocked_by(post.author):
        return jsonify({'status': 'error', 'message': 'This post is unavailable.'}), 403
    share_text = f"Shared from {post.author.username}: {post.content or 'Check this post on HDM'}"
    shared = Post(
        content=share_text,
        author=current_user,
        feeling='Shared',
        media_type=post.media_type,
        media_file=post.media_file,
        link=post.link,
        link_title=post.link_title,
        link_description=post.link_description,
        link_image=post.link_image,
        timestamp=datetime.utcnow()
    )
    db.session.add(shared)
    if post.author.id != current_user.id:
        db.session.add(Notification(user_id=post.author.id, content=f'{current_user.username} shared your post.', notification_type='share'))
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'post_id': shared.id})
    flash('Post shared successfully!', 'success')
    return redirect(request.referrer or url_for('index'))

@app.route('/saved_posts')
@login_required
def saved_posts():
    saved = SavedPost.query.filter_by(user_id=current_user.id).order_by(SavedPost.timestamp.desc()).all()
    posts = [entry.post for entry in saved if entry.post]
    return render_template('saved_posts.html', posts=posts)

@app.route('/delete_post/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.author != current_user:
        flash('You cannot delete someone else\'s post!', 'error')
        return redirect(url_for('index'))
        
    # Delete associated likes and comments first
    Like.query.filter_by(post_id=post.id).delete()
    Comment.query.filter_by(post_id=post.id).delete()
    
    # Shared posts may still reference this media file; only remove an unreferenced asset.
    if post.media_file:
        other_posts = Post.query.filter(Post.media_file == post.media_file, Post.id != post.id).count()
        other_stories = Story.query.filter_by(media_file=post.media_file).count()
        other_messages = Message.query.filter_by(media_file=post.media_file).count()
        if not (other_posts or other_stories or other_messages):
            media_path = os.path.join(app.config['UPLOAD_FOLDER'], post.media_type or '', os.path.basename(post.media_file))
            if os.path.exists(media_path):
                os.remove(media_path)
    
    db.session.delete(post)
    db.session.commit()
    flash('Post deleted successfully!', 'success')
    return redirect(request.referrer or url_for('index'))

@app.route('/delete_comment/<int:comment_id>', methods=['POST'])
@login_required
def delete_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)
    if comment.author != current_user:
        flash('You cannot delete someone else\'s comment!', 'error')
        return redirect(url_for('index'))
    
    Comment.query.filter_by(parent_id=comment.id).delete(synchronize_session=False)
    db.session.delete(comment)
    db.session.commit()
    flash('Comment deleted successfully!', 'success')
    return redirect(request.referrer or url_for('index'))

@app.route('/profile/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.timestamp.desc()).all()
    is_friend = current_user.is_friend(user)
    has_sent_request = current_user.has_sent_friend_request(user)
    received_request = FriendRequest.query.filter_by(sender_id=user.id, recipient_id=current_user.id).first() is not None
    friend_count = user.friends.count()
    recent_friends = user.friends.limit(6).all()
    
    return render_template('profile.html', 
                         user=user, 
                         posts=posts, 
                         is_friend=is_friend,
                         has_sent_request=has_sent_request,
                         received_request=received_request,
                         friend_count=friend_count,
                         recent_friends=recent_friends)

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        bio = request.form['bio']
        profile_pic = request.files.get('profile_picture')
        cover_photo = request.files.get('cover_photo')
        
        if profile_pic and profile_pic.filename:
            if not allowed_file(profile_pic.filename, 'image'):
                flash('Invalid file type. Allowed types are: ' + ', '.join(ALLOWED_EXTENSIONS['image']), 'error')
            else:
                try:
                    filename = save_media(profile_pic, 'image')
                    if filename:
                        # Delete old profile picture if it's not the default
                        if current_user.profile_picture != 'default.jpg':
                            old_file = os.path.join(app.config['UPLOAD_FOLDER'], current_user.profile_picture)
                            if os.path.exists(old_file):
                                os.remove(old_file)
                        current_user.profile_picture = filename
                        flash('Profile picture updated successfully!', 'success')
                except Exception as e:
                    flash('Error uploading profile picture. Please try again.', 'error')
                    print(f"Error uploading file: {str(e)}")
        
        if cover_photo and cover_photo.filename:
            if not allowed_file(cover_photo.filename, 'image'):
                flash('Invalid file type for cover photo. Allowed types are: ' + ', '.join(ALLOWED_EXTENSIONS['image']), 'error')
            else:
                try:
                    filename = save_media(cover_photo, 'image')
                    if filename:
                        if current_user.cover_photo != 'default_cover.jpg':
                            old_file = os.path.join(app.config['UPLOAD_FOLDER'], current_user.cover_photo)
                            if os.path.exists(old_file):
                                os.remove(old_file)
                        current_user.cover_photo = filename
                        flash('Cover photo updated successfully!', 'success')
                except Exception as e:
                    flash('Error uploading cover photo. Please try again.', 'error')
                    print(f"Error uploading file: {str(e)}")
        
        current_user.bio = bio
        db.session.commit()
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile', username=current_user.username))
    
    return render_template('edit_profile.html')

@app.route('/friends/<username>')
@login_required
def friends_list(username):
    user = User.query.filter_by(username=username).first_or_404()
    friends = user.friends.all()
    # For each friend, check if the current user is already friends with them
    for friend in friends:
        friend.is_friend_with_current = current_user.is_friend(friend)
        friend.has_pending_request = current_user.has_sent_friend_request(friend)
    return render_template('friends_list.html', 
                         user=user, 
                         friends=friends,
                         current_user=current_user)

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '')
    if query:
        users = User.query.filter(
            (User.username.ilike(f'%{query}%')) |
            (User.email.ilike(f'%{query}%')) |
            (User.bio.ilike(f'%{query}%'))
        ).all()
    else:
        users = []
    return render_template('search.html', users=users, query=query)

@app.route('/messages')
@login_required
def messages():
    messages_received = Message.query.filter_by(recipient_id=current_user.id).order_by(Message.timestamp.desc()).all()
    messages_sent = Message.query.filter_by(sender_id=current_user.id).order_by(Message.timestamp.desc()).all()
    selected_id = request.args.get('user', type=int)
    if selected_id:
        selected_user = User.query.get_or_404(selected_id)
        if selected_user.id == current_user.id or current_user.has_blocked(selected_user) or current_user.is_blocked_by(selected_user):
            flash('This conversation is unavailable.', 'error')
            return redirect(url_for('messages'))
        Message.query.filter_by(sender_id=selected_user.id, recipient_id=current_user.id, is_read=False).update({'is_read': True}, synchronize_session='fetch')
        db.session.commit()
    return render_template('messages.html',
                         messages_received=messages_received,
                         messages_sent=messages_sent,
                         user_query=User.query)

@app.route('/notifications')
@login_required
def notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
    
    # Mark all notifications as read
    for notification in notifications:
        if not notification.is_read:
            notification.is_read = True
    db.session.commit()
    
    return render_template('notifications.html', notifications=notifications)

@app.route('/notifications/mark_read/<int:notification_id>', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        notification.is_read = True
        db.session.commit()
    return redirect(url_for('notifications'))

@app.route('/notifications/delete/<int:notification_id>', methods=['POST'])
@login_required
def delete_notification(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    if notification.user_id == current_user.id:
        db.session.delete(notification)
        db.session.commit()
    return redirect(url_for('notifications'))

@app.route('/send_message/<int:recipient_id>', methods=['POST'])
@login_required
@limiter.limit('120 per hour')
def send_message(recipient_id):
    recipient = User.query.get_or_404(recipient_id)
    if recipient.id == current_user.id or current_user.has_blocked(recipient) or current_user.is_blocked_by(recipient):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'Messaging is unavailable for this account.'}), 403
        flash('Messaging is unavailable for this account.', 'error')
        return redirect(url_for('messages', user=recipient_id))
    content = sanitize_text(request.form.get('content', ''))[:5000]
    media = request.files.get('media')
    
    if content or (media and media.filename):
        media_type = None
        media_filename = None
        
        if media and media.filename:
            media_type = get_file_type(media.filename)
            if not media_type:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'status': 'error', 'message': 'Unsupported attachment type.'}), 400
                flash('Unsupported attachment type.', 'error')
                return redirect(url_for('messages', user=recipient_id))
            media_filename = save_media(media, media_type)
            if not media_filename:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'status': 'error', 'message': 'Attachment upload failed.'}), 400
                flash('Attachment upload failed.', 'error')
                return redirect(url_for('messages', user=recipient_id))
        
        message = Message(
            sender_id=current_user.id,
            recipient_id=recipient_id,
            content=content,
            media_type=media_type,
            media_file=media_filename
        )
        db.session.add(message)

        # Create notification for the recipient
        notification = Notification(
            user_id=recipient_id,
            content=f"New message from {current_user.username}",
            notification_type="message"
        )
        db.session.add(notification)
        
        db.session.commit()

        # Send real-time notification via WebSocket
        socketio.emit('new_message', {
            'type': 'message',
            'recipient_id': recipient_id,
            'message': {
                'sender_username': current_user.username,
                'content': content,
                'media_type': media_type,
                'media_url': message.media_url if media_filename else None,
                'timestamp': message.timestamp.strftime('%Y-%m-%d %H:%M')
            }
        },to=f"user_{recipient_id}")

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'status': 'success',
                'message': 'Message sent successfully'
            })
        else:
            flash('Message sent successfully!', 'success')
            return redirect(url_for('messages', user=recipient_id))
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'status': 'error',
            'message': 'Message content required'
        })
    else:
        flash('Message content required', 'error')
        return redirect(url_for('messages', user=recipient_id))

@app.route('/friend_requests')
@login_required
def friend_requests():
    received_requests = FriendRequest.query.filter_by(recipient_id=current_user.id).all()
    sent_requests = FriendRequest.query.filter_by(sender_id=current_user.id).all()
    return render_template('friend_requests.html', received_requests=received_requests, sent_requests=sent_requests)

@app.route('/send_friend_request/<int:user_id>', methods=['POST'])
@login_required
@limiter.limit('30 per hour')
def send_friend_request(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id or current_user.has_blocked(user) or current_user.is_blocked_by(user):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot send a request to this account.'}), 400
        flash('You cannot send a friend request to yourself.', 'error')
        return redirect(url_for('profile', username=user.username))

    if current_user.has_sent_friend_request(user) or current_user.is_friend(user):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'ok', 'message': 'Request already sent', 'already_present': True})
        flash('Friend request already sent or you are already friends.', 'info')
        return redirect(request.referrer or url_for('profile', username=user.username))

    current_user.send_friend_request(user)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'message': 'Friend request sent', 'sent': True})
    flash('Friend request sent!')
    return redirect(url_for('profile', username=user.username))

@app.route('/accept_friend_request/<int:user_id>', methods=['POST'])
@login_required
def accept_friend_request(user_id):
    user = User.query.get_or_404(user_id)
    current_user.accept_friend_request(user)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'message': 'Friend request accepted'})
    flash('Friend request accepted!')
    return redirect(url_for('friend_requests'))

@app.route('/decline_friend_request/<int:request_id>', methods=['POST'])
@login_required
def decline_friend_request(request_id):
    friend_request = FriendRequest.query.get_or_404(request_id)
    if friend_request.recipient_id == current_user.id:
        db.session.delete(friend_request)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'ok', 'message': 'Friend request declined'})
        flash('Friend request declined')
    elif request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'error', 'message': 'This request is not yours to manage'}), 403
    return redirect(url_for('friend_requests'))

# Database initialization
def init_database():
    """Create missing tables without destroying existing user data.

    Schema changes must be delivered through Flask-Migrate migrations.
    This helper intentionally never drops or recreates existing tables.
    """
    with app.app_context():
        db.create_all()
        print('Database schema checked; existing data preserved.')

@app.route('/healthz')
def healthz():
    """Lightweight platform health check; does not expose application data."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'ok', 'database': 'ok'}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception('Health check database probe failed')
        return jsonify({'status': 'error'}), 503

@app.route('/notifications/unread')
@login_required
def check_unread_notifications():
    unread_count = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    ).count()
    return jsonify({'unread_count': unread_count})

@socketio.on('connect')
def handle_connect():
    if not current_user.is_authenticated:
        return False 
    join_room(f"user_{current_user.id}")
    current_user.update_last_seen()


@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        current_user.set_offline()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(
        app,
        host='0.0.0.0',
        port=int(os.environ.get('PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG') == '1',
        allow_unsafe_werkzeug=os.environ.get('FLASK_DEBUG') == '1'
    )




