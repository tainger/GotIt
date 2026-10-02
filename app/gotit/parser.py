"""A replaceable public share-page adapter. No login or paid service."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .network import MEDIA_DOMAINS, PAGE_DOMAINS, UserError, checked_url


def extract_link(text):
    links = re.findall(r'https?://[^\s<>\"\u4e00-\u9fff]+', text)
    for candidate in links:
        candidate = candidate.rstrip('.,;!?)]}，。；！？”）')
        try:
            parsed = checked_url(candidate, PAGE_DOMAINS)
        except UserError:
            continue
        if parsed.hostname == 'v.douyin.com' or video_id(candidate) or is_note(candidate):
            return candidate
    raise UserError('没有找到抖音视频链接。请粘贴完整分享文案或单条视频链接。')


def video_id(url):
    match = re.search(r'/(?:share/)?video/(\d{10,25})(?:/|$)', urlsplit(url).path)
    return match.group(1) if match else None


def is_note(url):
    return bool(re.fullmatch(r'/(?:share/)?note/\d{10,25}/?', urlsplit(url).path))


NOTE_MESSAGE = '这条链接是图文作品（图片合集），不是视频。当前版本暂不支持图文下载，请换一条视频分享链接。'


def parse_page(html, expected_id):
    match = re.search(r'(?:window\.)?_ROUTER_DATA\s*=\s*', html)
    if not match:
        raise UserError('分享页没有提供视频数据，可能需要平台验证或链接已失效。请先在抖音中确认视频可打开，再重试。')
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        loaders = data.get('loaderData', {})
        items = []
        for loader in loaders.values():
            if isinstance(loader, dict):
                items.extend((loader.get('videoInfoRes') or {}).get('item_list') or [])
        item = next((i for i in items if str(i.get('aweme_id')) == expected_id), None)
        if not item:
            raise UserError('分享页未返回该视频的数据：可能是平台验证、访问限制或视频已失效。请在抖音中确认链接可打开后重试。')
        if item.get('images'):
            raise UserError(NOTE_MESSAGE)
        video = item.get('video') or {}
        urls = (video.get('play_addr') or {}).get('url_list') or []
        media_url = next((u for u in urls if isinstance(u, str) and u.startswith(('http://', 'https://'))), None)
        if not media_url:
            raise UserError('来源没有提供可下载的视频地址。')
        parsed = urlsplit(media_url)
        # Use the source's playback endpoint, without changing signed query strings.
        media_url = urlunsplit(('https', parsed.netloc, parsed.path.replace('/playwm/', '/play/'), parsed.query, ''))
        checked_url(media_url, MEDIA_DOMAINS)
        return {'video_id': expected_id, 'title': str(item.get('desc') or '抖音视频')[:500],
                'author': str((item.get('author') or {}).get('nickname') or '')[:100],
                'media_url': media_url, 'platform': '抖音',
                'watermark_note': '使用来源播放文件；不保证消除画面内嵌水印。'}
    except (ValueError, TypeError, AttributeError, KeyError):
        raise UserError('平台页面格式发生变化，当前解析器暂时无法读取。') from None


class DouyinParser:
    def __init__(self, network, browser=None):
        self.network = network
        self.browser = browser

    def resolve(self, text):
        source = extract_link(text)
        if is_note(source):
            raise UserError(NOTE_MESSAGE)
        vid = video_id(source)
        html = None
        if not vid:
            html, final_url = self.network.page(source)
            if is_note(final_url):
                raise UserError(NOTE_MESSAGE)
            vid = video_id(final_url)
        if not vid:
            raise UserError('分享链接没有指向单条视频，暂不支持主页、直播和合集。')
        canonical = f'https://www.iesdouyin.com/share/video/{vid}/'
        if html and '_ROUTER_DATA' in html:
            try:
                result = parse_page(html, vid)
                return dict(result, source=canonical)
            except UserError:
                pass
        html, _ = self.network.page(canonical)
        try:
            return dict(parse_page(html, vid), source=canonical)
        except UserError as exc:
            if not self.browser or str(exc) == NOTE_MESSAGE:
                raise
            return self.browser.resolve(vid)
