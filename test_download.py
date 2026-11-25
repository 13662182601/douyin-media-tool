import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import download as d

TEXT = '示例 https://www.douyin.com/video/1234567890123456789/'
URLS = ['https://a.douyinvod.com/one', 'https://b.douyinvod.com/two']
INFO = {'duration': 3.0, 'hasAudio': True, 'audioCodec': 'aac'}


class Response:
    def __init__(self, body=b'media', length='5', status=200, location=None):
        self.body = io.BytesIO(body)
        self.headers = {'Content-Length': length, 'Location': location}
        self.status = status

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.body.read(size)


def connection():
    return mock.Mock(sock=None)


class SecurityTests(unittest.TestCase):
    def test_url_whitelist(self):
        for url in [None, 42, {}, 'http://v.douyin.com/x', 'https://v.douyin.com.evil.test/x',
                    'https://user@v.douyin.com/x', 'https://v.douyin.com:444/x',
                    'https://v.douyin.com./x', 'https://v.douyin.com/\\evil',
                    'https://v.douyin.com/\nfoo', 'https://127.0.0.1/x']:
            with self.subTest(url=url), self.assertRaises(d.Failure):
                d.check_url(url, 'share')
        d.check_url(URLS[0], 'cdn')
        with self.assertRaises(d.Failure):
            d.check_url('https://notdouyinvod.com/x', 'cdn')
        with self.assertRaises(d.Failure):
            d.check_url('https://api.amemv.com/other', 'detail')

    def test_dns_rejects_mixed_public_private(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                     for ip in ['8.8.8.8', '127.0.0.1']]
        with mock.patch.object(socket, 'getaddrinfo', return_value=addresses):
            with self.assertRaises(d.Failure):
                d.public_addresses('v.douyin.com', time.monotonic() + 3)

    def test_pinned_connection_uses_verified_address_and_tls_host(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        raw, context = mock.Mock(), mock.Mock()
        with mock.patch.object(d.ssl, 'create_default_context', return_value=context):
            conn = d.PinnedHTTPS('a.douyinvod.com', addresses, 5)
        with mock.patch.object(d.socket, 'socket', return_value=raw):
            conn.connect()
        raw.connect.assert_called_once_with(('8.8.8.8', 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='a.douyinvod.com')

    def test_redirect_rechecks_whitelist(self):
        conn = connection()
        conn.getresponse.return_value = Response(status=302, location='https://127.0.0.1/private')
        with mock.patch.object(d, 'public_addresses', return_value=[]), mock.patch.object(d, 'PinnedHTTPS', return_value=conn):
            with self.assertRaises(d.Failure):
                d.request(URLS[0], 'cdn', time.monotonic() + 3)
        self.assertEqual(conn.request.call_count, 1)
        conn.close.assert_called()

    def test_share_text_and_direct_id(self):
        self.assertEqual(d.extract_id(TEXT, time.monotonic() + 2), '1234567890123456789')
        for text in ['', 'x' * 8193, 'https://evil.test/x']:
            with self.assertRaises(d.Failure):
                d.share_url(text)


class TransferTests(unittest.TestCase):
    def test_truncated_and_overlong_length(self):
        for body, length in [(b'abc', '5'), (b'abcdef', '3'), (b'', '0'), (b'x', '-1'), (b'abc', '999')]:
            conn = connection()
            with self.subTest(body=body, length=length), self.assertRaises(d.Failure):
                d.read_limited(Response(body, length), conn, 20, time.monotonic() + 2)
            conn.close.assert_called_once()

    def test_unannounced_limit_and_success(self):
        with self.assertRaises(d.Failure):
            d.read_limited(Response(b'abcdef', None), connection(), 5, time.monotonic() + 2)
        self.assertEqual(d.read_limited(Response(), connection(), 20, time.monotonic() + 2), b'media')

    def test_metadata_uses_play_addr_and_filters(self):
        payload = {'aweme_detail': {'desc': '测试', 'video': {'duration': 3000,
                   'download_addr': {'url_list': ['https://evil.test/not-used']},
                   'play_addr': {'url_list': ['https://evil.test/x'] + URLS}}}}
        body = json.dumps(payload).encode()
        with mock.patch.object(d, 'request', return_value=(Response(body, str(len(body))), connection(), '')):
            video_id, title, urls = d.metadata(TEXT)
        self.assertEqual(urls, URLS)
        self.assertEqual(title, '测试')


class DownloadTests(unittest.TestCase):
    def patches(self, request=None, info=INFO):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(d, 'ffmpeg_binary', return_value='ffmpeg'))
        stack.enter_context(mock.patch.object(d, 'metadata', return_value=('1234567890123456789', '测试', URLS)))
        stack.enter_context(mock.patch.object(d, 'request', side_effect=request or (lambda *args: (Response(), connection(), args[0]))))
        stack.enter_context(mock.patch.object(d, 'validate_media', return_value=info))
        return stack

    def test_timeout_fallback_fresh_deadline_and_atomic_visibility(self):
        deadlines = []
        with tempfile.TemporaryDirectory() as temp:
            def request(url, kind, deadline):
                deadlines.append(deadline)
                self.assertFalse(list(Path(temp).glob('*.mp4')))
                if len(deadlines) == 1:
                    time.sleep(0.002)
                    raise d.Failure('超时')
                return Response(), connection(), url
            with self.patches(request):
                result = d.download(TEXT, temp)
            self.assertGreater(deadlines[1], deadlines[0])
            self.assertEqual(Path(result['videoPath']).read_bytes(), b'media')
            self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_truncated_first_line_falls_back(self):
        attempts = iter([Response(b'ab', '9'), Response()])
        with tempfile.TemporaryDirectory() as temp, self.patches(lambda *args: (next(attempts), connection(), '')):
            result = d.download(TEXT, temp)
            self.assertTrue(result['ok'])

    def test_failed_decode_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temp, self.patches():
            with mock.patch.object(d, 'validate_media', side_effect=d.Failure('损坏')):
                with self.assertRaises(d.Failure):
                    d.download(TEXT, temp)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_silent_video_and_audio_request_failure(self):
        silent = {'duration': 3, 'hasAudio': False, 'audioCodec': None}
        with tempfile.TemporaryDirectory() as temp, self.patches(info=silent):
            result = d.download(TEXT, temp)
            self.assertFalse(result['hasAudio'])
            self.assertIsNone(result['audioPath'])
            with self.assertRaises(d.Failure):
                d.download(TEXT, temp, True)
            self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_audio_failure_does_not_publish_video(self):
        with tempfile.TemporaryDirectory() as temp, self.patches(), mock.patch.object(d, 'run_ffmpeg', side_effect=d.Failure('提取失败')):
            with self.assertRaises(d.Failure):
                d.download(TEXT, temp, True)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_audio_success_and_full_duration_validation(self):
        def extract(command, deadline):
            Path(command[-1]).write_bytes(b'audio')
            return ''
        with tempfile.TemporaryDirectory() as temp, self.patches(), mock.patch.object(d, 'run_ffmpeg', side_effect=extract):
            result = d.download(TEXT, temp, True)
            self.assertTrue(result['audioExtracted'])
            self.assertEqual(Path(result['audioPath']).read_bytes(), b'audio')
        with tempfile.TemporaryDirectory() as temp, self.patches(), mock.patch.object(d, 'run_ffmpeg', side_effect=extract):
            with mock.patch.object(d, 'validate_media', side_effect=[INFO, dict(INFO, duration=1)]):
                with self.assertRaises(d.Failure):
                    d.download(TEXT, temp, True)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_concurrent_publish_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / 'source.part'
            source.write_bytes(b'complete')
            existing = directory / 'video.mp4'
            existing.write_bytes(b'original')
            with ThreadPoolExecutor(max_workers=8) as executor:
                targets = list(executor.map(lambda _: d.publish(source, directory, 'video', '.mp4'), range(20)))
            self.assertEqual(len(set(targets)), 20)
            self.assertEqual(existing.read_bytes(), b'original')
            self.assertTrue(all(p.read_bytes() == b'complete' for p in targets))

    def test_validation_maps_full_video_audio_and_rejects_duration(self):
        info = '  Duration: 00:00:03.00, start: 0.0\n  Stream #0:0: Video: h264\n  Stream #0:1[0x2](und): Audio: aac\nOutput #0\n  Stream #0:0: Audio: pcm_s16le'
        with mock.patch.object(d, 'run_ffmpeg', return_value=info) as run:
            self.assertEqual(d.validate_media('ffmpeg', Path('x.part')), INFO)
        command = run.call_args[0][0]
        self.assertIn('-xerror', command)
        self.assertIn('0:v:0', command)
        self.assertIn('0:a?', command)
        self.assertNotIn('-t', command)
        with mock.patch.object(d, 'run_ffmpeg', return_value='  Duration: 00:31:00.00, start: 0.0'):
            with self.assertRaises(d.Failure):
                d.validate_media('ffmpeg', Path('x.part'))


    def test_validation_ignores_metadata_duration_and_audio(self):
        metadata = ('    title : Duration: 00:00:01.00, Audio: aac\n'
                    '    comment : Stream #0:1: Audio: aac\n'
                    '      Duration: 00:00:01.00,\n'
                    '      Stream #0:1: Audio: aac\n')
        for actual in ['', '  Duration: 00:31:00.00, start: 0.0\n']:
            with self.subTest(actual=actual), mock.patch.object(d, 'run_ffmpeg', return_value=metadata + actual):
                with self.assertRaises(d.Failure):
                    d.validate_media('ffmpeg', Path('x.part'))
        info = metadata + '  Duration: 00:00:03.00, start: 0.0\n'
        with mock.patch.object(d, 'run_ffmpeg', return_value=info):
            self.assertEqual(d.validate_media('ffmpeg', Path('x.part')),
                             {'duration': 3.0, 'hasAudio': False, 'audioCodec': None})
            with self.assertRaises(d.Failure):
                d.validate_media('ffmpeg', Path('x.part'), audio_only=True)
        info += ('    title : Output #0 fake\n'
                 '  Stream #0:1(eng): Audio: aac, 48000 Hz\n'
                 'Output #0, null\n  Stream #0:0: Audio: pcm_s16le\n')
        with mock.patch.object(d, 'run_ffmpeg', return_value=info):
            self.assertEqual(d.validate_media('ffmpeg', Path('x.part'), audio_only=True), INFO)


class FFmpegProcessTests(unittest.TestCase):
    def run_script(self, script, seconds=5):
        return d.run_ffmpeg([sys.executable, '-c', script], time.monotonic() + seconds)

    def test_bounded_diagnostics_success_and_nonzero_exit(self):
        self.assertEqual(self.run_script('import sys; sys.stderr.write("x" * 262144)'), 'x' * 262144)
        with self.assertRaisesRegex(d.Failure, '完整媒体处理失败'):
            self.run_script('raise SystemExit(1)')

    def test_timeout_terminates_silent_process(self):
        spawn = subprocess.Popen
        for script in ['import time; time.sleep(60)',
                       'import os, time; os.close(2); time.sleep(60)']:
            processes = []

            def capture(*args, **kwargs):
                process = spawn(*args, **kwargs)
                processes.append(process)
                return process

            with self.subTest(script=script), mock.patch.object(d.subprocess, 'Popen', side_effect=capture):
                with self.assertRaisesRegex(d.Failure, '超时'):
                    self.run_script(script, seconds=0.2)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())

    def test_excess_diagnostics_terminate_infinite_writer(self):
        with self.assertRaisesRegex(d.Failure, '诊断输出超过限制'):
            self.run_script('import os\nwhile True: os.write(2, b"x" * 8192)')

    def test_excess_diagnostics_from_exited_process_are_rejected(self):
        with self.assertRaisesRegex(d.Failure, '诊断输出超过限制'):
            self.run_script('import os; os.write(2, b"x" * 262145)')


class CliTests(unittest.TestCase):
    def test_success_stdout_json(self):
        output = io.StringIO()
        with mock.patch.object(d, 'download', return_value={'ok': True}) as download, contextlib.redirect_stdout(output):
            self.assertEqual(d.main([TEXT, '--output-dir', 'out', '--audio']), 0)
        download.assert_called_once_with(TEXT, 'out', True)
        self.assertEqual(json.loads(output.getvalue()), {'ok': True})

    def test_failure_message_preserved_and_untrusted_exceptions_redacted(self):
        secret = '/private/user/secret.mp4 原始响应内容'
        errors = [OSError(secret), ValueError(secret), d.http.client.HTTPException(secret),
                  subprocess.TimeoutExpired(['ffmpeg', secret], 1, stderr=secret)]
        for error in errors:
            output = io.StringIO()
            with self.subTest(error=type(error).__name__), mock.patch.object(d, 'download', side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(d.main([TEXT, '--output-dir', 'out']), 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {'ok': False, 'error': '处理失败：网络、文件或媒体处理异常。'})
        output = io.StringIO()
        with mock.patch.object(d, 'download', side_effect=d.Failure('安全提示')), contextlib.redirect_stdout(output):
            self.assertEqual(d.main([TEXT, '--output-dir', 'out']), 1)
        self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'error': '安全提示'})

    def test_failure_and_bad_arguments_stdout_json(self):
        for args in [[], [TEXT, '--output-dir', 'out']]:
            output = io.StringIO()
            with mock.patch.object(d, 'download', side_effect=d.Failure('测试失败')), contextlib.redirect_stdout(output):
                self.assertEqual(d.main(args), 1)
            self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
