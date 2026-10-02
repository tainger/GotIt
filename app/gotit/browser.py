"""Load a public video in a fresh browser context; never read user profiles."""
import socket
import time
from urllib.parse import parse_qs, urlsplit

from .network import MEDIA_DOMAINS, UserError, checked_url, public_address

BROWSER_DOMAINS = MEDIA_DOMAINS + (
    'douyinstatic.com', 'douyinpic.com', 'bytecdn.cn', 'bytecdn.com',
    'byteimg.com', 'bytescm.com', 'bytegoofy.com', 'bytetos.com',
    'zijieapi.com', 'bytedapm.com', 'volccdn.com', 'volces.com',
)


def select_video(videos, expected_id):
    """Ignore ads, preload clips and recommendations: require the target ID."""
    for video in videos:
        url = video.get('src') or ''
        try:
            parsed = checked_url(url, MEDIA_DOMAINS)
        except UserError:
            continue
        if parse_qs(parsed.query).get('__vid') == [expected_id]:
            return url
    return None


class BrowserResolver:
    def resolve(self, vid):
        try:
            from playwright.sync_api import sync_playwright, Error, TimeoutError
        except ImportError:
            raise UserError('浏览器解析组件未安装，请关闭程序并重新运行 start.command。') from None

        checked_url(f'https://www.douyin.com/video/{vid}', ('douyin.com',))
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel='chrome', headless=True, timeout=30000)
            except Error:
                raise UserError('浏览器解析需要本机安装 Google Chrome。安装后重新解析即可，不需要登录。') from None
            try:
                context = browser.new_context(service_workers='block', accept_downloads=False)
                verified_hosts = set()

                def guard(route):
                    try:
                        target = checked_url(route.request.url, BROWSER_DOMAINS)
                        if target.hostname not in verified_hosts:
                            public_address(target.hostname)
                            verified_hosts.add(target.hostname)
                        route.continue_()
                    except (UserError, OSError, socket.gaierror, Error):
                        try:
                            route.abort()
                        except Error:
                            pass

                context.route('**/*', guard)
                page = context.new_page()
                page.goto(f'https://www.douyin.com/video/{vid}', wait_until='domcontentloaded', timeout=45000)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    videos = page.locator('video').evaluate_all('(els) => els.map(e => ({src:e.currentSrc || e.src}))')
                    url = select_video(videos, vid)
                    if url:
                        title = page.title().removesuffix(' - 抖音').strip()[:500] or '抖音视频'
                        return dict(video_id=vid, title=title, author='', media_url=url,
                                    platform='抖音', source=f'https://www.douyin.com/video/{vid}',
                                    watermark_note='使用来源播放文件；不保证消除画面内嵌水印。')
                    page.wait_for_timeout(500)
                raise UserError('浏览器未获得这条视频的播放地址。来源可能要求登录或验证，暂时无法自动下载。')
            except TimeoutError:
                raise UserError('浏览器加载视频超时，请检查网络后重试。') from None
            except Error:
                raise UserError('浏览器未能加载公开视频，请稍后重试。') from None
            finally:
                browser.close()
