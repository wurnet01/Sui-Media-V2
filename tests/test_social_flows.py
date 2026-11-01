import io
import uuid

from app import User, Post, Story, app, db


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
