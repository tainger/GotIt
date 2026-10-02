import io
import json
import socket
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gotit.jobs import Store
from gotit.network import PAGE_DOMAINS, SafeHTTP, UserError, checked_url, public_address
from gotit.parser import DouyinParser, extract_link, parse_page
from gotit.server import create_app
from gotit.browser import select_video

VID = '7358413207083486505'
MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 100


def fixture_page(**changes):
    item = {'aweme_id': VID, 'desc': '测试 / 视频', 'author': {'nickname': '测试作者'},
            'video': {'play_addr': {'url_list': ['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=test']}}}
    item.update(changes)
    return '<script>window._ROUTER_DATA = ' + json.dumps({'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': [item]}}}}) + ';</script>'


class Response(io.BytesIO):
    def __init__(self, data=MP4, length=None, mime='video/mp4'):
        super().__init__(data)
        self.headers = {'Content-Type': mime, 'Content-Length': str(len(data)) if length is None else length}

    def getheader(self, name):
        return self.headers.get(name)


class FakeNetwork:
    def __init__(self, data=MP4, length=None, mime='video/mp4'):
        self.data, self.length, self.mime = data, length, mime

    @contextmanager
    def open(self, *args):
        yield Response(self.data, self.length, self.mime), 'https://example.douyinvod.com/video'


class FakeParser:
    def resolve(self, text):
        extract_link(text)
        return dict(parse_page(fixture_page(), VID), source=f'https://www.douyin.com/video/{VID}')


def test_extract_full_share_text():
    assert extract_link('复制打开抖音 https://v.douyin.com/abcd/。 看看') == 'https://v.douyin.com/abcd/'
    assert extract_link(f'https://www.douyin.com/video/{VID}?x=1').endswith('?x=1')


def test_note_share_reports_actual_type():
    class NoteNetwork:
        def page(self, url):
            return '', 'https://www.iesdouyin.com/share/note/7691108508590492603/?from_ssr=1'
    parser = DouyinParser(NoteNetwork())
    for source in [
        '8.28 复制打开抖音，看看【1on0的图文作品】 https://v.douyin.com/UtzzvPR4_po/ C@u.FH 10/01 icN:/ :0pm',
        'https://www.douyin.com/note/7691108508590492603',
        'https://www.iesdouyin.com/share/note/7691108508590492603/',
    ]:
        with pytest.raises(UserError, match='图文作品（图片合集）'):
            parser.resolve(source)


def test_browser_selects_only_matching_video():
    target = f'https://v3-dy-o.zjcdn.com/video/file/?__vid={VID}'
    clips = [{'src': 'https://lf-douyin-pc-web.douyinstatic.com/intro.mp4'},
             {'src': 'https://v3-dy-o.zjcdn.com/video/file/?__vid=123'},
             {'src': 'blob:https://www.douyin.com/abc'}, {'src': target}]
    assert select_video(clips, VID) == target
    assert select_video(clips[:-1], VID) is None
    assert select_video([{'src': f'https://evil.com/a?__vid={VID}'}], VID) is None


def test_empty_share_page_uses_browser_fallback():
    class EmptyPage:
        def page(self, url): return 'window._ROUTER_DATA = {};', url
    class Browser:
        def resolve(self, vid):
            assert vid == VID
            return {'video_id': vid, 'title': 'browser result'}
    assert DouyinParser(EmptyPage(), Browser()).resolve(f'https://www.douyin.com/video/{VID}')['title'] == 'browser result'


def test_note_does_not_launch_browser():
    class NotePage:
        def page(self, url): return fixture_page(images=[{}]), url
    class Browser:
        def resolve(self, vid): pytest.fail('image post must not invoke video browser fallback')
    with pytest.raises(UserError, match='图文作品'):
        DouyinParser(NotePage(), Browser()).resolve(f'https://www.douyin.com/video/{VID}')


@pytest.mark.parametrize('url', ['http://v.douyin.com/a/', 'https://douyin.com.evil.com/a/',
    'https://127.0.0.1/', 'https://user@www.douyin.com/', 'https://www.douyin.com:8080/',
    'https://www.douyin.com\\@evil.com/', 'file:///etc/passwd'])
def test_reject_unsafe_url(url):
    with pytest.raises(UserError):
        checked_url(url, PAGE_DOMAINS)


def test_dns_private_and_mixed_rejected():
    for addresses in [('127.0.0.1',), ('8.8.8.8', '169.254.169.254'), ('::1',)]:
        with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]):
            with pytest.raises(UserError):
                public_address('www.douyin.com')


def test_redirect_revalidated():
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def close(self): pass
        def getresponse(self): return self
        status = 302
        def getheader(self, name): return 'https://127.0.0.1/private'
    with patch('gotit.network.public_address', return_value='8.8.8.8'), patch('gotit.network.PinnedHTTPS', Connection):
        with pytest.raises(UserError):
            with SafeHTTP().open('https://v.douyin.com/abc/', PAGE_DOMAINS):
                pytest.fail('unsafe redirect followed')


def test_page_parse_and_missing_data():
    result = parse_page(fixture_page(), VID)
    assert '/play/' in result['media_url']
    assert result['title'] == '测试 / 视频'
    for html in ['<html>验证</html>', fixture_page(images=[{}]), fixture_page(aweme_id='123'), 'window._ROUTER_DATA = {};']:
        with pytest.raises(UserError): parse_page(html, VID)


@pytest.mark.parametrize('data,length,mime,limit', [
    (b'<html>error</html>', None, 'text/html', 1000),
    (MP4, '999', 'video/mp4', 1000),
    (MP4, None, 'video/mp4', 10),
    (MP4, '', 'video/mp4', 10),
    (b'', None, 'video/mp4', 1000),
])
def test_invalid_download_cleanup(tmp_path, data, length, mime, limit):
    store = Store(tmp_path, FakeParser(), FakeNetwork(data, length, mime), max_bytes=limit)
    rid = store.add_result(FakeParser().resolve(f'https://www.douyin.com/video/{VID}'))['id']
    store.start(rid)
    store.close()
    assert store.list_jobs()[0]['status'] == 'failed'
    assert not list(tmp_path.glob('*.part'))
    assert not list(tmp_path.glob('*.mp4'))


def test_complete_api_flow_and_duplicate(tmp_path):
    app = create_app(tmp_path, FakeParser(), FakeNetwork())
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.post('/api/resolve', json={'text': 'x'}).status_code == 403
        token = client.get('/api/config').json()['token']
        headers = {'X-Gotit-Token': token}
        assert client.post('/api/resolve', json={'text': 'bad'}, headers=headers).status_code == 400
        assert client.post('/api/resolve', content=b'x' * 33000, headers=headers).status_code == 413
        response = client.post('/api/resolve', json={'text': f'https://www.douyin.com/video/{VID}'}, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert 'media_url' not in result and 'source' not in result
        job = client.post('/api/download', json={'result_id': result['id']}, headers=headers).json()
        for _ in range(100):
            state = client.get('/api/tasks').json()[0]
            if state['status'] in ('done', 'failed'): break
            time.sleep(.01)
        assert state['status'] == 'done', state
        assert client.get('/api/files/' + job['id']).content == MP4
        again = client.post('/api/download', json={'result_id': result['id']}, headers=headers).json()
        assert again['id'] == job['id']
        assert client.get('/api/config', headers={'Host': 'evil.example'}).status_code == 400
        assert client.get('/api/config', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    with TestClient(create_app(tmp_path, FakeParser(), FakeNetwork())) as client:
        assert client.get('/api/tasks').json()[0]['status'] == 'done'


def test_expired_result_and_restart_cleanup(tmp_path):
    store = Store(tmp_path, FakeParser(), FakeNetwork())
    rid = store.add_result(FakeParser().resolve(f'https://www.douyin.com/video/{VID}'))['id']
    store.results[rid]['created'] -= 1900
    with pytest.raises(UserError): store.start(rid)
    store.close()
    (tmp_path / 'old.part').write_bytes(b'partial')
    (tmp_path / 'history.json').write_text(json.dumps([{'id': 'a', 'status': 'downloading'}]))
    restarted = Store(tmp_path, FakeParser(), FakeNetwork())
    assert restarted.list_jobs()[0]['status'] == 'failed'
    assert not (tmp_path / 'old.part').exists()
    restarted.close()
