import io
import uuid

from app import User, Post, Story, Comment, Message, app, db

# Route-flow tests focus on application behaviour; CSRF is exercised separately in production configuration.
app.config['WTF_CSRF_ENABLED'] = False


def _create_user(username_prefix='pytestuser'):
    username = f"{username_prefix}{uuid.uuid4().hex[:8]}"
    with app.app_context():
        user = User(username=username, email=f"{username}@example.com")
        user.set_password('Pass1234!')
        db.session.add(user)
        db.session.commit()
    return username, 'Pass1234!'


def test_login_and_home_page_load():
    username, password = _create_user()
    client = app.test_client()
    response = client.post('/login', data={'username': username, 'password': password}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['Location'] == '/'

    home = client.get('/')
    assert home.status_code == 200
    assert b'HDM' in home.data


def test_post_like_comment_and_story_flow():
    username, password = _create_user()
    client = app.test_client()
    client.post('/login', data={'username': username, 'password': password}, follow_redirects=False)

    post_response = client.post('/create_post', data={'content': 'This is a pytest post'}, follow_redirects=False)
    assert post_response.status_code == 302

    with app.app_context():
        post = Post.query.order_by(Post.id.desc()).first()
        assert post is not None
        assert post.content == 'This is a pytest post'

        like_response = client.post(f'/like/{post.id}', data={'reaction_type': 'like'}, headers={'X-Requested-With': 'XMLHttpRequest'})
        like_json = like_response.get_json()
        assert like_response.status_code == 200
        assert like_json['status'] == 'ok'
        assert like_json['liked'] is True

        comment_response = client.post(f'/comment/{post.id}', data={'content': 'Looks great!'}, headers={'X-Requested-With': 'XMLHttpRequest'})
        comment_json = comment_response.get_json()
        assert comment_response.status_code == 200
        assert comment_json['status'] == 'ok'
        assert comment_json['content'] == 'Looks great!'

    png = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe5\x27\x98;\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    story_response = client.post(
        '/create_story',
        data={'caption': 'Pytest story', 'story_media': (io.BytesIO(png), 'story.png', 'image/png')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    assert story_response.status_code == 302

    with app.app_context():
        story = Story.query.order_by(Story.id.desc()).first()
        assert story is not None
        assert story.caption == 'Pytest story'
        assert story.media_type == 'image'


def test_state_changing_routes_require_csrf_token():
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = app.test_client().post('/login', data={'username': 'nobody', 'password': 'wrong'})
        assert response.status_code == 400
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_health_endpoint_checks_database():
    response = app.test_client().get('/healthz')
    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok', 'database': 'ok'}


def test_replies_are_limited_to_root_comments_on_the_same_post():
    username, password = _create_user('replyuser')
    client = app.test_client()
    client.post('/login', data={'username': username, 'password': password})

    client.post('/create_post', data={'content': 'Reply validation post'})
    with app.app_context():
        post = Post.query.filter_by(content='Reply validation post').order_by(Post.id.desc()).first()
        assert post is not None
        root = client.post(f'/comment/{post.id}', data={'content': 'Root comment'}, headers={'X-Requested-With': 'XMLHttpRequest'}).get_json()
        reply = client.post(f'/comment/{post.id}', data={'content': 'First reply', 'parent_id': root['comment_id']}, headers={'X-Requested-With': 'XMLHttpRequest'}).get_json()
        assert root['status'] == 'ok'
        assert reply['status'] == 'ok'
        nested = client.post(f'/comment/{post.id}', data={'content': 'Nested reply', 'parent_id': reply['comment_id']}, headers={'X-Requested-With': 'XMLHttpRequest'})
        assert nested.status_code == 404


def test_direct_message_persists_and_marks_as_read():
    sender_name, password = _create_user('sender')
    recipient_name, _ = _create_user('recipient')
    with app.app_context():
        sender_id = User.query.filter_by(username=sender_name).first().id
        recipient_id = User.query.filter_by(username=recipient_name).first().id

    sender = app.test_client()
    sender.post('/login', data={'username': sender_name, 'password': password})
    sent = sender.post(f'/send_message/{recipient_id}', data={'content': 'Hello from HDM'}, follow_redirects=False)
    assert sent.status_code == 302
    assert f'/messages?user={recipient_id}' in sent.headers['Location']

    with app.app_context():
        message = Message.query.filter_by(sender_id=sender_id, recipient_id=recipient_id, content='Hello from HDM').first()
        assert message is not None
        assert message.is_read is False

    recipient = app.test_client()
    recipient.post('/login', data={'username': recipient_name, 'password': password})
    conversation = recipient.get(f'/messages?user={sender_id}')
    assert conversation.status_code == 200
    assert b'Hello from HDM' in conversation.data

    with app.app_context():
        message = Message.query.filter_by(sender_id=sender_id, recipient_id=recipient_id, content='Hello from HDM').first()
        assert message.is_read is True
