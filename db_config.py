import os
from flask_sqlalchemy import SQLAlchemy

# One database URL for local SQLite and production PostgreSQL.
db = SQLAlchemy()


def init_db(app):
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///social_platform.db')
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    db.init_app(app)
