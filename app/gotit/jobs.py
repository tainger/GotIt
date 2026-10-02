import copy
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .network import MEDIA_DOMAINS, UserError


class Store:
    def __init__(self, root, parser, network, max_bytes=200 * 1024 * 1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.parser, self.network, self.max_bytes = parser, network, max_bytes
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.results, self.jobs = {}, {}
        self.history = self.root / 'history.json'
        if self.history.exists():
            try:
                for job in json.loads(self.history.read_text('utf-8')):
                    if job.get('status') in ('queued', 'downloading'):
                        job.update(status='failed', error='上次运行已中断，请重新解析并下载。')
                    self.jobs[job['id']] = job
            except (ValueError, KeyError, TypeError):
                pass
        for partial in self.root.glob('*.part'):
            partial.unlink(missing_ok=True)

    def save(self):
        temp = self.history.with_suffix('.tmp')
        temp.write_text(json.dumps(list(self.jobs.values())[-100:], ensure_ascii=False), 'utf-8')
        temp.replace(self.history)

    def add_result(self, result):
        with self.lock:
            now = time.time()
            self.results = {k: v for k, v in self.results.items() if now - v['created'] < 1800}
            if len(self.results) >= 100:
                del self.results[next(iter(self.results))]
            rid = uuid.uuid4().hex
            self.results[rid] = dict(result, created=now)
            return dict(id=rid, **{k: v for k, v in result.items() if k not in ('media_url', 'source')})

    def list_jobs(self):
        with self.lock:
            return copy.deepcopy(list(self.jobs.values())[-30:][::-1])

    def start(self, rid):
        with self.lock:
            result = self.results.get(rid)
            if not result or time.time() - result['created'] > 1800:
                raise UserError('解析结果已过期，请重新解析。')
            for job in self.jobs.values():
                if job['video_id'] == result['video_id'] and (job['status'] in ('queued', 'downloading') or
                        (job['status'] == 'done' and (self.root / job['filename']).is_file())):
                    return dict(job)
            if sum(j['status'] in ('queued', 'downloading') for j in self.jobs.values()) >= 3:
                raise UserError('已有三个任务等待处理，请下载完成后再试。')
            jid = uuid.uuid4().hex
            name = re.sub(r'[^\w\u4e00-\u9fff -]', '_', result['title'])[:60].strip() or '视频'
            job = dict(id=jid, video_id=result['video_id'], title=result['title'], status='queued',
                       downloaded=0, total=None, error=None, created=time.time(),
                       filename=f'{name}_{result["video_id"]}_{jid[:8]}.mp4')
            self.jobs[jid] = job
            try:
                self.save()
            except OSError:
                del self.jobs[jid]
                raise UserError('无法写入任务记录，请检查下载目录权限和磁盘空间。') from None
            self.pool.submit(self.run, jid, dict(result))
            return dict(job)

    def update(self, jid, **values):
        with self.lock:
            self.jobs[jid].update(values)

    def run(self, jid, result):
        part = self.root / (jid + '.part')
        try:
            self.update(jid, status='downloading')
            # One fresh resolution, since signed download links may have expired.
            for attempt in range(2):
                try:
                    self.transfer(jid, result['media_url'], part)
                    break
                except UserError as exc:
                    if attempt or not any(code in str(exc) for code in ('HTTP 403', 'HTTP 410')):
                        raise
                    result = self.parser.resolve(result['source'])
                    self.update(jid, downloaded=0, total=None)
            with self.lock:
                part.replace(self.root / self.jobs[jid]['filename'])
                self.jobs[jid]['status'] = 'done'
        except Exception as exc:
            message = str(exc) if isinstance(exc, UserError) else '下载中断或文件无法写入，请检查网络和磁盘空间后重试。'
            self.update(jid, status='failed', error=message)
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                self.update(jid, status='failed', error='临时文件无法清理，请检查下载目录权限。')
            with self.lock:
                try:
                    self.save()
                except OSError:
                    self.jobs[jid]['error'] = '任务记录无法写入磁盘，重启后可能无法显示本次记录。'

    def transfer(self, jid, url, part):
        deadline = time.monotonic() + 300
        with self.network.open(url, MEDIA_DOMAINS, deadline) as (response, _):
            length = response.getheader('Content-Length')
            total = int(length) if length and length.isdigit() else None
            if total is not None and total > self.max_bytes:
                raise UserError('视频超过 200MB 上限，已停止下载。')
            kind = (response.getheader('Content-Type') or '').lower()
            if 'text/' in kind or 'json' in kind:
                raise UserError('来源返回了网页而不是视频，请重新解析。')
            self.update(jid, total=total)
            count = 0
            with part.open('wb') as output:
                while True:
                    if time.monotonic() > deadline:
                        raise UserError('下载超过五分钟，已停止，请稍后重试。')
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    if count == 0 and (len(chunk) < 12 or chunk[4:8] != b'ftyp'):
                        raise UserError('来源文件不是支持的 MP4 视频，已停止保存。')
                    count += len(chunk)
                    if count > self.max_bytes:
                        raise UserError('视频超过 200MB 上限，已停止下载。')
                    output.write(chunk)
                    self.update(jid, downloaded=count)
            if count == 0 or (total is not None and count != total):
                raise UserError('视频下载不完整，请重试。')

    def file(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job['status'] != 'done':
                raise UserError('文件尚未下载完成。')
            path = self.root / job['filename']
            if path.parent != self.root or not path.is_file():
                raise UserError('文件已被移走或删除，请重新下载。')
            return path

    def close(self):
        self.pool.shutdown(wait=True)
