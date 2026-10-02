"""HTTPS with public-IP pinning, bounded redirects, and no inherited proxy."""
import http.client
import ipaddress
import socket
import ssl
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

UA = 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36'
PAGE_DOMAINS = ('douyin.com', 'iesdouyin.com')
MEDIA_DOMAINS = PAGE_DOMAINS + ('douyinvod.com', 'bytevod.com', 'ibytedtos.com', 'snssdk.com', 'amemv.com', 'bytedance.com', 'zjcdn.com')


class UserError(Exception):
    pass


def checked_url(url, domains):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.port not in (None, 443)
                or parsed.username or parsed.password or '\\' in url
                or any(ord(c) < 33 for c in url)
                or not any(host == d or host.endswith('.' + d) for d in domains)):
            raise ValueError()
        return parsed
    except ValueError:
        raise UserError('链接地址不受支持，请粘贴抖音的 HTTPS 分享链接。') from None


def public_address(host):
    entries = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [entry[4][0] for entry in entries]
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise UserError('目标地址不是可访问的公网地址。')
    return addresses[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class SafeHTTP:
    @contextmanager
    def open(self, url, domains, deadline=None):
        deadline = deadline or time.monotonic() + 40
        for _ in range(6):
            parsed = checked_url(url, domains)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UserError('请求超时，请稍后重试。')
            address = public_address(parsed.hostname)
            conn = PinnedHTTPS(parsed.hostname, address, min(15, remaining))
            try:
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                conn.request('GET', path, headers={'User-Agent': UA, 'Accept-Encoding': 'identity'})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise UserError('来源返回了无效的跳转地址。')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise UserError(f'来源暂时无法访问（HTTP {response.status}），请稍后重试。')
                yield response, url
                return
            finally:
                conn.close()
        raise UserError('分享链接跳转次数过多。')

    def page(self, url):
        with self.open(url, PAGE_DOMAINS) as (response, final_url):
            content = response.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise UserError('分享页面过大，已停止解析。')
            return content.decode('utf-8', errors='replace'), final_url
