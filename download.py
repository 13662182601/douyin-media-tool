#!/usr/bin/env python3
"""独立命令行工具；仅处理用户有权使用的公开分享媒体。"""
import argparse
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

MAX_VIDEO = 200 * 1024 * 1024
MAX_DURATION = 1800
LINE_SECONDS = 90
VALIDATE_SECONDS = 180
MAX_LINES = 3
SHARE_HOSTS = {'v.douyin.com', 'www.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com'}
CDN_SUFFIXES = ('douyinvod.com', 'bytecdn.cn', 'ibytedtos.com', 'byteimg.com')
UA = 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
DNS_SLOTS = threading.BoundedSemaphore(2)


class Failure(Exception):
    pass


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise Failure('处理超时。')
    return value


def check_url(url, kind):
    try:
        if not isinstance(url, str):
            raise ValueError()
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        if p.scheme != 'https' or p.port not in (None, 443) or p.username is not None or p.password is not None:
            raise ValueError()
        if not host or host.endswith('.') or '\\' in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError()
        host.encode('ascii')
        if kind == 'share':
            allowed = host in SHARE_HOSTS
        elif kind == 'detail':
            allowed = host == 'api.amemv.com' and p.path == '/aweme/v1/aweme/detail/'
        elif kind == 'cdn':
            allowed = any(host == suffix or host.endswith('.' + suffix) for suffix in CDN_SUFFIXES)
        else:
            allowed = False
        if not allowed:
            raise ValueError()
    except (ValueError, UnicodeError, TypeError):
        raise Failure('链接目标不在允许范围内。') from None
    return p, host


def public_addresses(host, deadline):
    if not DNS_SLOTS.acquire(False):
        raise Failure('DNS 查询繁忙。')
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)
        finally:
            DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=min(8, remaining(deadline)))
    except queue.Empty:
        raise Failure('域名解析超时。') from None
    if isinstance(addresses, Exception) or not addresses:
        raise Failure('域名解析失败。')
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        mapped = getattr(ip, 'ipv4_mapped', None)
        if (not ip.is_global or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified or (mapped and not mapped.is_global)):
            raise Failure('已阻止非公网目标地址。')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, addresses, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        deadline = time.monotonic() + self.timeout
        last = None
        for family, socktype, proto, _, address in self.addresses:
            raw = socket.socket(family, socktype, proto)
            try:
                raw.settimeout(remaining(deadline))
                raw.connect(address)
                raw.settimeout(remaining(deadline))
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except (OSError, Failure) as exc:
                raw.close()
                last = exc
        raise last or Failure('连接失败。')


def request(url, kind, deadline):
    for _ in range(6):
        p, host = check_url(url, kind)
        addresses = public_addresses(host, deadline)
        conn = PinnedHTTPS(host, addresses, min(12, remaining(deadline)))
        try:
            path = p.path or '/'
            if p.query:
                path += '?' + p.query
            conn.request('GET', path, headers={'User-Agent': UA, 'Accept-Encoding': 'identity'})
            if conn.sock:
                conn.sock.settimeout(min(12, remaining(deadline)))
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader('Location')
                conn.close()
                if not target:
                    raise Failure('跳转缺少目标。')
                url = urljoin(url, target)
                continue
            if response.status != 200:
                raise Failure('源站返回 HTTP %s。' % response.status)
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise Failure('不支持压缩响应。')
            return response, conn, url
        except Exception:
            conn.close()
            raise
    raise Failure('重定向次数过多。')


def read_limited(response, conn, limit, deadline, output=None):
    try:
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) > limit):
            raise Failure('Content-Length 无效或超过大小限制。')
        total, chunks = 0, []
        while True:
            timeout = min(10, remaining(deadline))
            if conn.sock:
                conn.sock.settimeout(timeout)
            data = response.read1(min(65536, limit - total + 1))
            if not data:
                break
            total += len(data)
            if total > limit:
                raise Failure('源文件超过大小限制。')
            if output is None:
                chunks.append(data)
            else:
                output.write(data)
        if not total or (length is not None and total != int(length)):
            raise Failure('源文件为空或 Content-Length 与实际长度不一致。')
        return b''.join(chunks) if output is None else total
    finally:
        conn.close()


def share_url(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        raise Failure('分享文本必须为 1–8192 个字符。')
    matches = re.findall(r'https://[^\s<>"\u201c\u201d]+', text)
    if not matches:
        raise Failure('请输入包含 HTTPS 抖音分享链接的文本。')
    url = matches[0].rstrip('，。；！）)]}')
    check_url(url, 'share')
    return url


def extract_id(text, deadline):
    url = share_url(text)
    for _ in range(2):
        p, _ = check_url(url, 'share')
        match = re.search(r'/(?:video|share/video)/(\d{10,25})(?:/|$)', p.path)
        if match:
            return match[1]
        response, conn, final = request(url, 'share', deadline)
        conn.close()
        if final == url:
            break
        url = final
    raise Failure('无法从分享链接识别视频编号。')


def ffmpeg_binary():
    configured = os.environ.get('MEDIA_FFMPEG')
    if configured:
        found = shutil.which(configured)
        if found:
            return found
        raise Failure('MEDIA_FFMPEG 指向的程序不可执行。')
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        found = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(found).is_file() and os.access(found, os.X_OK):
            return found
    except (ImportError, RuntimeError):
        pass
    raise Failure('未找到 FFmpeg；请安装或设置 MEDIA_FFMPEG。')


def run_ffmpeg(command, deadline):
    info = bytearray()
    errors = []
    finished = threading.Event()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0) as process:
        def read_diagnostics():
            try:
                while True:
                    chunk = process.stderr.read(min(8192, 256 * 1024 - len(info) + 1))
                    if not chunk:
                        break
                    if len(info) + len(chunk) > 256 * 1024:
                        raise Failure('媒体诊断输出超过限制。')
                    info.extend(chunk)
            except (OSError, ValueError, Failure) as exc:
                errors.append(exc)
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            finally:
                finished.set()

        reader = threading.Thread(target=read_diagnostics, daemon=True)
        reader.start()
        try:
            try:
                code = process.wait(timeout=remaining(deadline))
                if not finished.wait(remaining(deadline)):
                    raise Failure('媒体校验超时。')
            except subprocess.TimeoutExpired:
                raise Failure('媒体校验超时。') from None
            if errors:
                raise errors[0]
            if code:
                raise Failure('FFmpeg 完整媒体处理失败。')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return info.decode('utf-8', 'replace')


def validate_media(binary, path, audio_only=False):
    command = [binary, '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info', '-xerror',
               '-err_detect', 'explode', '-protocol_whitelist', 'file,pipe', '-f', 'mov', '-i', str(path)]
    command += ['-map', '0:a:0'] if audio_only else ['-map', '0:v:0', '-map', '0:a?']
    command += ['-f', 'null', '-']
    info = run_ffmpeg(command, time.monotonic() + VALIDATE_SECONDS)
    # 只读取输入描述，避免将输出流编码误认为原始音频编码。
    source = re.split(r'^Output #', info, maxsplit=1, flags=re.MULTILINE)[0]
    duration = re.search(r'^  Duration: (\d+):(\d+):(\d+(?:\.\d+)?),', source, re.MULTILINE)
    actual = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]) if duration else 0
    codecs = re.findall(r'^  Stream #0:\d+(?:\[[^\]\r\n]*\])?(?:\([^\r\n)]*\))?: Audio: ([^,\s]+)',
                        source, re.MULTILINE)
    if not 0 < actual <= MAX_DURATION or (audio_only and not codecs):
        raise Failure('媒体时长无效、超过 1800 秒或缺少音轨。')
    return {'duration': actual, 'hasAudio': bool(codecs), 'audioCodec': codecs[0] if codecs else None}


def metadata(text):
    deadline = time.monotonic() + 30
    video_id = extract_id(text, deadline)
    endpoint = 'https://api.amemv.com/aweme/v1/aweme/detail/?aweme_id=' + video_id + '&aid=6383&device_platform=web&version_code=190500&version_name=19.5.0'
    response, conn, _ = request(endpoint, 'detail', deadline)
    try:
        detail = json.loads(read_limited(response, conn, 2 * 1024 * 1024, deadline))['aweme_detail']
        video = detail['video']
        duration = float(video['duration']) / 1000
        urls = video['play_addr']['url_list']
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION or not isinstance(urls, list):
            raise ValueError()
        allowed = []
        for url in urls[:20]:
            try:
                check_url(url, 'cdn')
            except Failure:
                continue
            if url not in allowed:
                allowed.append(url)
        if not allowed:
            raise ValueError()
        return video_id, str(detail.get('desc') or '抖音视频')[:500], allowed[:MAX_LINES]
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise Failure('源站未提供可用的公开播放地址或时长超过 1800 秒。') from None


def publish(part, directory, stem, suffix):
    # 同一文件系统硬链接原子创建；目标已存在时绝不覆盖，包括符号链接。
    for index in range(10000):
        target = directory / (stem + (f'-{index}' if index else '') + suffix)
        try:
            os.link(part, target)
        except FileExistsError:
            continue
        return target
    raise Failure('同名文件过多，无法安全发布。')


def download(text, output_dir, audio=False):
    share_url(text)
    binary = ffmpeg_binary()
    video_id, title, urls = metadata(text)
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.douyin-', dir=directory) as work:
        part = Path(work) / 'video.part'
        info = None
        for url in urls:
            try:
                deadline = time.monotonic() + LINE_SECONDS
                response, conn, _ = request(url, 'cdn', deadline)
                with part.open('wb') as output:
                    read_limited(response, conn, MAX_VIDEO, deadline, output)
                    output.flush()
                    os.fsync(output.fileno())
                info = validate_media(binary, part)
                break
            except (Failure, OSError, http.client.HTTPException, subprocess.TimeoutExpired):
                part.unlink(missing_ok=True)
        if info is None:
            raise Failure('所有允许的播放线路均下载或校验失败。')
        audio_part = None
        if audio:
            if not info['hasAudio'] or info['audioCodec'] != 'aac':
                raise Failure('提取 m4a 需要原视频具有 AAC 音轨。')
            audio_part = Path(work) / 'audio.part'
            command = [binary, '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'error', '-xerror',
                       '-n', '-protocol_whitelist', 'file,pipe', '-f', 'mov', '-i', str(part),
                       '-map', '0:a:0', '-vn', '-c:a', 'copy', '-f', 'ipod', str(audio_part)]
            run_ffmpeg(command, time.monotonic() + VALIDATE_SECONDS)
            if not audio_part.is_file() or not 0 < audio_part.stat().st_size <= MAX_VIDEO:
                raise Failure('音轨为空或超过 200 MiB。')
            audio_info = validate_media(binary, audio_part, audio_only=True)
            if abs(info['duration'] - audio_info['duration']) > 0.5:
                raise Failure('音轨时长与视频不一致，未发布结果。')
        video_path = publish(part, directory, video_id, '.mp4')
        try:
            audio_path = publish(audio_part, directory, video_path.stem, '.m4a') if audio_part else None
        except Exception:
            # 仅撤销本任务刚发布的链接，不涉及任何预先存在文件。
            video_path.unlink()
            raise
        return {'ok': True, 'videoId': video_id, 'title': title,
                'sourceUrl': 'https://www.douyin.com/video/' + video_id,
                'videoPath': str(video_path), 'audioPath': str(audio_path) if audio_path else None,
                'duration': info['duration'], 'hasAudio': info['hasAudio'],
                'audioExtracted': audio_path is not None, 'bytes': video_path.stat().st_size}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('命令行参数错误：' + message)


def main(argv=None):
    parser = Parser(description='下载有权使用的公开抖音视频；这是独立 CLI，不是工作台。')
    parser.add_argument('text', help='包含 HTTPS 链接的完整分享文本')
    parser.add_argument('--output-dir', required=True, help='输出目录')
    parser.add_argument('--audio', action='store_true', help='另存完整 AAC 音轨为 m4a，不修改视频')
    try:
        args = parser.parse_args(argv)
        result = download(args.text, args.output_dir, args.audio)
    except Failure as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired):
        print(json.dumps({'ok': False, 'error': '处理失败：网络、文件或媒体处理异常。'}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({'ok': False, 'error': '用户取消。'}, ensure_ascii=False))
        return 130
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
