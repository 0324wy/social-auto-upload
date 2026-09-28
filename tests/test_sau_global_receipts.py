import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sau_cli


def receipt(platform='tiktok', status='success'):
    return {'schema_version': 1, 'platform': platform, 'status': status, 'post_id': '123456789',
            'url': 'https://www.tiktok.com/@creator/video/123456789', 'remote_status': 'submitted',
            'visibility': 'public', 'cover_verified': True, 'content_kind': 'video',
            'attempted_at': '2026-09-28T20:00:00+00:00', 'finished_at': '2026-09-28T20:01:00+00:00'}


class GlobalCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'video.mp4'
        self.video.write_bytes(b'video')
        self.cover = self.root / 'cover.jpg'
        self.cover.write_bytes(b'cover')
        self.result = self.root / 'result.json'

    def tearDown(self):
        self.temp.cleanup()

    def args(self, platform='tiktok'):
        return sau_cli.build_parser().parse_args([platform, 'upload-video', '--account', 'creator', '--file', str(self.video), '--title', 'Title', '--thumbnail', str(self.cover), '--result-file', str(self.result), '--visibility', 'public', '--proxy', 'http://localhost:1'])

    async def test_alias_and_standard_cookie_path(self):
        args = self.args('tk')
        with patch.object(sau_cli, 'resolve_runtime_home', return_value=self.root):
            self.assertEqual(sau_cli.resolve_account_file('tiktok', args.account), self.root / 'cookies/tiktok_creator.json')
        upload = AsyncMock(return_value=receipt())
        with patch.object(sau_cli, 'upload_tiktok_video', upload):
            self.assertEqual(await sau_cli.dispatch(args), 0)
        self.assertEqual(json.loads(self.result.read_text())['platform'], 'tiktok')
        request = upload.await_args.args[0]
        self.assertEqual(request.proxy, 'http://localhost:1')
        self.assertEqual(request.thumbnail_file, self.cover)

    async def test_checkpoint_is_on_disk_before_uploader_runs(self):
        async def upload(request):
            checkpoint = json.loads(self.result.read_text())
            self.assertEqual(checkpoint['status'], 'needs_verification')
            self.assertIsNone(checkpoint['post_id'])
            return receipt()
        with patch.object(sau_cli, 'upload_tiktok_video', upload):
            self.assertEqual(await sau_cli.dispatch(self.args()), 0)
        self.assertEqual(list(self.root.glob('result.json.*.tmp')), [])

    async def test_pre_submit_failure_writes_failed_receipt(self):
        with patch.object(sau_cli, 'upload_tiktok_video', AsyncMock(side_effect=sau_cli.PreSubmissionError('invalid cover'))):
            self.assertEqual(await sau_cli.dispatch(self.args()), 1)
        data = json.loads(self.result.read_text())
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['remote_status'], 'not_submitted')

    async def test_uncertain_result_is_saved_with_nonzero_exit(self):
        wanted = receipt(status='needs_verification')
        with patch.object(sau_cli, 'upload_tiktok_video', AsyncMock(return_value=wanted)):
            self.assertEqual(await sau_cli.dispatch(self.args()), 2)
        self.assertEqual(json.loads(self.result.read_text()), wanted)

    async def test_existing_uncertain_or_success_receipt_prevents_reupload(self):
        for status in ('needs_verification', 'success'):
            self.result.write_text(json.dumps(receipt(status=status)))
            upload = AsyncMock()
            with patch.object(sau_cli, 'upload_tiktok_video', upload):
                with self.assertRaisesRegex(RuntimeError, 'reconcile'):
                    await sau_cli.dispatch(self.args())
            upload.assert_not_awaited()
            self.assertEqual(json.loads(self.result.read_text())['status'], status)

    async def test_invalid_success_is_not_accepted(self):
        invalid = receipt()
        invalid['post_id'] = None
        with patch.object(sau_cli, 'upload_tiktok_video', AsyncMock(return_value=invalid)):
            self.assertEqual(await sau_cli.dispatch(self.args()), 2)
        self.assertEqual(json.loads(self.result.read_text())['status'], 'needs_verification')

    async def test_interruption_keeps_durable_unknown_checkpoint(self):
        with patch.object(sau_cli, 'upload_tiktok_video', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await sau_cli.dispatch(self.args())
        self.assertEqual(json.loads(self.result.read_text())['status'], 'needs_verification')

    async def test_youtube_receipt_contract_and_legacy_platform_parser(self):
        result = receipt('youtube')
        result['post_id'] = 'abcdefghijk'
        result['url'] = 'https://www.youtube.com/watch?v=abcdefghijk'
        with patch.object(sau_cli, 'upload_youtube_video', AsyncMock(return_value=result)):
            self.assertEqual(await sau_cli.dispatch(self.args('youtube')), 0)
        self.assertEqual(json.loads(self.result.read_text()), result)
        args = sau_cli.build_parser().parse_args(['douyin', 'check', '--account', 'main'])
        self.assertEqual(args.platform, 'douyin')

    async def test_youtube_pending_processing_success_keeps_observed_pending_and_exit_zero(self):
        result = receipt('youtube')
        result.update(post_id='abcdefghijk', url='https://www.youtube.com/watch?v=abcdefghijk',
                      remote_status='accepted_pending_processing', requested_visibility='public',
                      observed_visibility='pending')
        with patch.object(sau_cli, 'upload_youtube_video', AsyncMock(return_value=result)):
            self.assertEqual(await sau_cli.dispatch(self.args('youtube')), 0)
        saved = json.loads(self.result.read_text())
        self.assertEqual(saved, result)
        self.assertEqual(saved['visibility'], 'public')
        self.assertEqual(saved['observed_visibility'], 'pending')
        self.assertNotEqual(saved['remote_status'], 'published')

    async def test_youtube_pending_processing_without_verified_cover_is_uncertain(self):
        result = receipt('youtube')
        result.update(post_id='abcdefghijk', url='https://www.youtube.com/watch?v=abcdefghijk',
                      remote_status='accepted_pending_processing', requested_visibility='public',
                      observed_visibility='pending', cover_verified=False)
        with patch.object(sau_cli, 'upload_youtube_video', AsyncMock(return_value=result)):
            self.assertEqual(await sau_cli.dispatch(self.args('youtube')), 2)
        saved = json.loads(self.result.read_text())
        self.assertEqual(saved['status'], 'needs_verification')
        self.assertEqual(saved['post_id'], 'abcdefghijk')
        self.assertEqual(saved['requested_visibility'], 'public')
        self.assertEqual(saved['observed_visibility'], 'pending')
        self.assertFalse(saved['cover_verified'])
        self.assertIn('cover verification', saved['error'])

    async def test_login_and_check_failure_remain_nonzero(self):
        for platform in ('youtube', 'tiktok', 'tk'):
            canonical = 'tiktok' if platform == 'tk' else platform
            login = sau_cli.build_parser().parse_args([platform, 'login', '--account', 'main'])
            self.assertFalse(login.headless)
            with patch.object(sau_cli, f'login_{canonical}_account', AsyncMock(return_value={'success': False})):
                self.assertEqual(await sau_cli.dispatch(login), 1)
            check = sau_cli.build_parser().parse_args([platform, 'check', '--account', 'main'])
            with patch.object(sau_cli, f'check_{canonical}_account', AsyncMock(return_value=False)):
                self.assertEqual(await sau_cli.dispatch(check), 1)


if __name__ == '__main__':
    unittest.main()
