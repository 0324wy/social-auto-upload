import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.tk_uploader import main_chrome as tk


def locator(count=0):
    value = MagicMock()
    value.count = AsyncMock(return_value=count)
    value.is_visible = AsyncMock(return_value=True)
    value.is_enabled = AsyncMock(return_value=True)
    value.get_attribute = AsyncMock(return_value=None)
    value.click = AsyncMock()
    value.wait_for = AsyncMock()
    value.set_input_files = AsyncMock()
    value.evaluate = AsyncMock(return_value=[])
    value.evaluate_all = AsyncMock(return_value=[])
    value.first = value
    value.filter.return_value = value
    value.nth.return_value = value
    return value


def browser_fixture():
    page = MagicMock()
    page.url = tk.UPLOAD_URL
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value = locator()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock()
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.start = AsyncMock(return_value=playwright)
    playwright.stop = AsyncMock()
    manager.__aexit__ = AsyncMock(return_value=False)
    return page, context, browser, playwright, manager


class TikTokPureTests(unittest.TestCase):
    def test_legacy_constructor_and_proxy_precedence(self):
        app = tk.TiktokVideo('Title', '/tmp/v.mp4', [], 0, '/tmp/c.json', '/tmp/c.jpg')
        self.assertEqual(app.thumbnail_path, '/tmp/c.jpg')
        with patch.dict(tk.os.environ, {'SAU_TIKTOK_PROXY': 'http://env:1'}), patch.object(tk, 'LOCAL_CHROME_PATH', ''), patch.object(tk, 'TIKTOK_PROXY', 'http://config:2'):
            self.assertEqual(tk._launch_options(False)['proxy']['server'], 'http://env:1')
            self.assertEqual(tk._launch_options(False, 'http://explicit:3')['proxy']['server'], 'http://explicit:3')
            self.assertNotIn('proxy', tk._launch_options(False, ''))
            self.assertEqual(tk._launch_options(False)['channel'], 'chrome')

    def test_create_receipt_uses_response_post_identity_not_an_arbitrary_id(self):
        result = tk._create_evidence({'status_code': 0, 'data': {'item_id': '123456789', 'share_url': 'https://www.tiktok.com/@creator/video/123456789?x=1'}})
        self.assertEqual(result['post_id'], '123456789')
        self.assertEqual(result['url'], 'https://www.tiktok.com/@creator/video/123456789')
        self.assertEqual(tk._create_evidence({'status_code': 0, 'data': {'id': '999', 'upload_id': '888'}}), {})
        self.assertEqual(tk._create_evidence({'data': {'item_id': '123'}}), {})
        self.assertEqual(tk._create_evidence({'status_code': 0, 'data': {'video_id': '123456789'}}), {})
        self.assertEqual(tk._create_evidence({'status_code': 0, 'item_id': '123', 'share_url': 'https://www.tiktok.com/@creator/video/456'}), {})
        self.assertIsNone(tk._post_identity('https://evil.example/@creator/video/123'))
        self.assertIsNone(tk._post_identity('https://www.tiktok.com/tiktokstudio/content'))

    def test_cover_samples_allow_small_redraws_but_reject_changed_artwork(self):
        original = bytes([80, 100, 120] * (32 * 32))
        close = bytes([81, 101, 121] * (32 * 32))
        changed = bytes([180, 100, 120] * (32 * 32))
        self.assertTrue(tk._same_image_sample(original, close))
        self.assertFalse(tk._same_image_sample(original, changed))

    def test_actual_project_post_response_uses_item_id_not_project_id(self):
        payload = {'project_id': '7690695779241776148', 'project_status': 1,
                   'single_post_resp_list': [{'batch_index': 0, 'item_id': '7690695822652247316', 'status_code': 0, 'status_msg': ''}],
                   'status_code': 0, 'status_msg': ''}
        self.assertEqual(tk._create_evidence(payload), {'post_id': '7690695822652247316', 'url': None, 'accepted': True})
        self.assertTrue(tk.CREATE_PATH.search('/tiktok/web/project/post/v1/'))
        self.assertIsNone(tk.CREATE_PATH.search('/tiktok/web/project/get_image_urls/v1/'))
        self.assertEqual(tk._create_evidence({'project_id': '7690695779241776148', 'status_code': 0}), {})

    def test_multiple_conflicting_or_unconfirmed_project_items_are_not_identity_evidence(self):
        first = {'item_id': '111', 'status_code': 0}
        second = {'item_id': '222', 'status_code': 0}
        for payload in (
            {'status_code': 0, 'single_post_resp_list': [first, second]},
            {'status_code': 0, 'single_post_resp_list': [first, dict(first)]},
            {'status_code': 0, 'item_id': '222', 'single_post_resp_list': [first]},
            {'status_code': 0, 'single_post_resp_list': [{'item_id': '111'}]},
            {'status_code': 0, 'single_post_resp_list': []},
        ):
            self.assertEqual(tk._create_evidence(payload), {})
        self.assertTrue(tk._create_evidence({'status_code': 0, 'single_post_resp_list': [{'item_id': '111', 'status_code': 8, 'status_msg': 'Rejected'}]})['rejected'])

    def test_explicit_server_rejection_is_not_success(self):
        self.assertTrue(tk._create_evidence({'status_code': 1001, 'status_msg': 'Rejected'})['rejected'])


class TikTokAsyncTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        return tk.TiktokVideo('Title', '/tmp/v.mp4', [], 0, '/tmp/c.json')

    async def test_cookie_check_errors_fail_closed_and_close_both_resources(self):
        page, context, browser, _, manager = browser_fixture()
        page.goto.side_effect = RuntimeError('navigation failed')
        with patch.object(tk, 'async_playwright', return_value=manager), patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)):
            self.assertFalse(await tk.cookie_auth('/tmp/cookie.json', proxy='http://proxy:9'))
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_interactive_login_timeout_does_not_save_cookie(self):
        page, context, browser, _, manager = browser_fixture()
        with patch.object(tk, 'async_playwright', return_value=manager), patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)), patch.object(tk, 'LOGIN_TIMEOUT_SECONDS', 0):
            result = await tk.get_tiktok_cookie('/tmp/must-not-be-written.json', headless=True)
        self.assertFalse(result['success'])
        context.storage_state.assert_not_awaited()
        browser.close.assert_awaited_once()
        self.assertFalse(manager.__aenter__.return_value.chromium.launch.call_args.kwargs['headless'])

    async def test_interactive_login_requires_positive_studio_evidence_and_returns_identity(self):
        page, context, browser, _, manager = browser_fixture()
        with tempfile.TemporaryDirectory() as temp, patch.object(tk, 'async_playwright', return_value=manager), patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)), patch.object(tk, '_authenticated', AsyncMock(return_value=True)), patch.object(tk, '_account_identity', AsyncMock(return_value={'handle': 'creator'})):
            result = await tk.get_tiktok_cookie(Path(temp) / 'session.json')
        self.assertTrue(result['success'])
        self.assertEqual(result['account_identity']['handle'], 'creator')
        context.storage_state.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_failed_login_setup_never_returns_true(self):
        with patch.object(tk, 'get_tiktok_cookie', AsyncMock(return_value={'success': False})), patch.object(tk, 'cookie_auth', AsyncMock(return_value=False)):
            self.assertFalse(await tk.tiktok_setup('/tmp/does-not-exist-tiktok-session.json', handle=True))

    async def test_upload_wait_has_finite_timeout(self):
        with patch.object(tk, 'UPLOAD_TIMEOUT_SECONDS', 0):
            with self.assertRaisesRegex(TimeoutError, '30 minutes'):
                await self.app().detect_upload_status(MagicMock())

    async def test_cover_failure_stops_before_submit_and_resources_close(self):
        page, context, browser, playwright, _ = browser_fixture()
        app = self.app()
        app.thumbnail_path = '/tmp/requested.jpg'
        app.validate = MagicMock()
        app.choose_base_locator = AsyncMock()
        app._dismiss_feature_guide = AsyncMock()
        app._dismiss_editor_overlays = AsyncMock()
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = locator(1)
        app.add_title_tags = AsyncMock()
        app.detect_upload_status = AsyncMock()
        app.upload_thumbnails = AsyncMock(side_effect=RuntimeError('cover failed'))
        app.click_publish = AsyncMock()
        with patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)):
            with self.assertRaisesRegex(RuntimeError, 'cover failed'):
                await app.upload(playwright)
        app.click_publish.assert_not_awaited()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_unchanged_saved_cover_prevents_publication(self):
        app = self.app()
        app.thumbnail_path = '/tmp/cover.jpg'
        root = MagicMock()
        card = locator(1)
        panel = locator(1)
        panel.locator.return_value = locator(1)
        panel.get_by_role.return_value = locator(1)
        root.locator.side_effect = [card, locator(), panel]
        root.get_by_text.return_value = locator(1)
        app.locator_base = root
        app._cover_snapshot = AsyncMock(return_value=['old-cover'])
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        chooser = MagicMock()
        chooser.set_files = AsyncMock()
        future = asyncio.get_running_loop().create_future()
        future.set_result(chooser)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(value=future))
        manager.__aexit__ = AsyncMock(return_value=False)
        page.expect_file_chooser.return_value = manager
        with self.assertRaisesRegex(RuntimeError, 'did not update'):
            await app.upload_thumbnails(page)
        self.assertFalse(app.cover_verified)
        self.assertEqual(page.wait_for_timeout.await_count, 60)

    def submit_fixture(self):
        app = self.app()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.locator.return_value = locator()
        app.locator_base = MagicMock()
        app.locator_base.get_by_role.return_value = locator()
        button = locator(1)
        app._post_button = MagicMock(return_value=button)
        return app, page, button

    async def test_uncertain_click_never_reclicks_and_does_not_guess_latest_row(self):
        app, page, button = self.submit_fixture()
        button.click.side_effect = TimeoutError('transport lost')
        result = await app.click_publish(page)
        self.assertEqual(result['status'], 'needs_verification')
        self.assertIsNone(result['post_id'])
        button.click.assert_awaited_once()
        page.locator.assert_not_called()
        page.remove_listener.assert_called_once()

    async def test_publish_timeout_is_bounded_and_does_not_read_first_row(self):
        app, page, button = self.submit_fixture()
        with patch.object(tk, 'PUBLISH_TIMEOUT_SECONDS', 0):
            result = await app.click_publish(page)
        self.assertEqual(result['status'], 'needs_verification')
        button.click.assert_awaited_once()
        page.locator.assert_not_called()

    async def test_actual_create_response_supplies_the_receipt(self):
        app, page, button = self.submit_fixture()
        response = SimpleNamespace(url='https://www.tiktok.com/api/v1/web/project/post/', request=SimpleNamespace(method='POST'), json=AsyncMock(return_value={'status_code': 0, 'data': {'item_id': '123456789', 'share_url': 'https://www.tiktok.com/@creator/video/123456789'}}))
        async def click(**kwargs):
            page.on.call_args.args[1](response)
            await asyncio.sleep(0)
        button.click.side_effect = click
        result = await app.click_publish(page)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['post_id'], '123456789')
        self.assertEqual(result['url'], 'https://www.tiktok.com/@creator/video/123456789')
        button.click.assert_awaited_once()
        page.locator.assert_not_called()

    async def test_saved_cover_uses_native_image_pixels_before_css_screenshot(self):
        app = self.app()
        card, image = locator(1), locator(1)
        card.locator.return_value = image
        expected = [80, 100, 120] * 1024
        image.evaluate.side_effect = [True, expected]
        self.assertEqual(await app._saved_cover_sample(card), expected)
        self.assertIn('image.naturalWidth, image.naturalHeight', image.evaluate.await_args.args[0])
        image.screenshot.assert_not_called()

    async def test_saved_cover_screenshot_fallback_is_only_for_tainted_canvas(self):
        app = self.app()
        card, image = locator(1), locator(1)
        card.locator.return_value = image
        image.evaluate.side_effect = [True, {'tainted': True}]
        image.screenshot = AsyncMock(return_value=b'screenshot')
        with patch.object(tk, '_image_sample', AsyncMock(return_value=[80] * 3072)) as sample:
            self.assertEqual(await app._saved_cover_sample(card), [80] * 3072)
        image.screenshot.assert_awaited_once_with(type='png', timeout=5000)
        sample.assert_awaited_once_with(image, b'screenshot')

    async def test_empty_native_cover_is_not_rescued_by_ui_screenshot(self):
        app = self.app()
        card, image = locator(1), locator(1)
        card.locator.return_value = image
        image.evaluate.side_effect = [True, None]
        self.assertIsNone(await app._saved_cover_sample(card))
        image.screenshot.assert_not_called()

    async def test_cover_replaced_after_save_blocks_submit(self):
        app, page, button = self.submit_fixture()
        app.thumbnail_path = '/tmp/custom.jpg'
        app.cover_verified = True
        app._verified_cover_sample = bytes([80, 100, 120] * (32 * 32))
        app.locator_base.locator.return_value = locator(1)
        app._saved_cover_sample = AsyncMock(return_value=bytes([180, 100, 120] * (32 * 32)))
        with self.assertRaisesRegex(RuntimeError, 'changed before publication'):
            await app.click_publish(page)
        button.click.assert_not_awaited()

    def fullscreen_cover_fixture(self):
        app = self.app()
        area, editor, upload = locator(1), locator(1), locator(1)
        save, cancel = locator(1), locator(1)
        area.locator.return_value = upload
        preview = locator(1)
        preview.locator.return_value = editor
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = preview
        editor.locator.side_effect = [save, cancel]
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        return app, page, area, upload, save

    async def test_final_profile_canvas_uses_native_pixels_not_letterboxed_stage(self):
        app = self.app()
        editor = MagicMock()
        profile = locator(1)
        expected = [80, 100, 120] * 1024
        profile.evaluate.return_value = expected
        editor.locator.return_value = profile
        result = await app._editor_cover_sample(editor, expected)
        self.assertEqual(result, expected)
        editor.locator.assert_called_once_with('canvas.CoverEditorProfilePreview__canvas:visible')
        script = profile.evaluate.await_args.args[0]
        self.assertIn('canvas.width', script)
        self.assertIn('sampleNative(canvas, canvas.width, canvas.height)', script)
        self.assertIn('context.drawImage(source, 0, 0)', script)
        self.assertNotIn('imageSmoothingQuality', script)
        self.assertNotIn('getBoundingClientRect', script)
        profile.screenshot.assert_not_called()

    async def test_stage_and_upload_thumbnail_cannot_replace_missing_final_profile(self):
        app = self.app()
        editor = MagicMock()
        editor.locator.return_value = locator()
        self.assertIsNone(await app._editor_cover_sample(editor, [80] * 3072))
        editor.locator.assert_called_once_with('canvas.CoverEditorProfilePreview__canvas:visible')

    async def test_ambiguous_final_profile_canvas_fails_closed(self):
        app = self.app()
        editor = MagicMock()
        editor.locator.return_value = locator(2)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            await app._editor_cover_sample(editor, [80] * 3072)

    async def test_unpainted_unreadable_or_wrong_profile_artwork_is_not_accepted(self):
        app = self.app()
        editor = MagicMock()
        profile = locator(1)
        editor.locator.return_value = profile
        for actual in (None, [200] * 3072):
            profile.evaluate.return_value = actual
            self.assertIsNone(await app._editor_cover_sample(editor, [80] * 3072))

    async def test_fullscreen_editor_waits_for_matching_stable_artwork_before_save(self):
        app, page, area, upload, save = self.fullscreen_cover_fixture()
        expected = bytes([80, 100, 120] * 1024)
        app._select_cover_file = AsyncMock()
        app._editor_cover_sample = AsyncMock(side_effect=[None, expected, expected])
        with tempfile.TemporaryDirectory() as temp:
            cover = Path(temp) / 'cover.jpg'
            cover.write_bytes(b'local-cover')
            app.thumbnail_path = str(cover)
            with patch.object(tk, '_image_sample', AsyncMock(return_value=expected)):
                await app._upload_fullscreen_cover(page, area)
        app._select_cover_file.assert_awaited_once_with(upload)
        self.assertEqual(app._editor_cover_sample.await_count, 3)
        save.click.assert_awaited_once_with(timeout=15000)
        save.wait_for.assert_awaited_once_with(state='hidden', timeout=30000)
        area.wait_for.assert_not_awaited()
        self.assertEqual(area.locator.call_args.args[0], 'input[type="file"][accept*="image/"]')

    async def test_fullscreen_editor_cannot_save_a_nonmatching_or_unloaded_image(self):
        app, page, area, upload, save = self.fullscreen_cover_fixture()
        app._select_cover_file = AsyncMock()
        app._editor_cover_sample = AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as temp:
            cover = Path(temp) / 'cover.jpg'
            cover.write_bytes(b'local-cover')
            app.thumbnail_path = str(cover)
            with patch.object(tk, '_image_sample', AsyncMock(return_value=bytes([80] * 3072))):
                with self.assertRaisesRegex(RuntimeError, 'refusing to Save'):
                    await app._upload_fullscreen_cover(page, area)
        save.click.assert_not_awaited()
        self.assertEqual(page.wait_for_timeout.await_count, 60)

    async def test_fullscreen_editor_rejects_ambiguous_input_before_file_selection(self):
        app, page, area, upload, save = self.fullscreen_cover_fixture()
        upload.count.return_value = 2
        app._select_cover_file = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
            await app._upload_fullscreen_cover(page, area)
        app._select_cover_file.assert_not_awaited()
        save.click.assert_not_awaited()

    async def test_cover_input_records_filename_before_the_site_clears_it(self):
        app = self.app()
        app.thumbnail_path = '/tmp/custom.jpg'
        upload = locator(1)
        handle = MagicMock()
        handle.evaluate = AsyncMock(side_effect=['custom.jpg', None])
        handle.dispose = AsyncMock()
        upload.evaluate_handle = AsyncMock(return_value=handle)
        await app._select_cover_file(upload)
        upload.set_input_files.assert_awaited_once_with('/tmp/custom.jpg')
        self.assertIn('capture: true', upload.evaluate_handle.await_args.args[0])
        self.assertEqual(handle.evaluate.await_args_list[-1].args[0], 'state => state.cleanup()')
        handle.dispose.assert_awaited_once()

    async def test_wrong_selected_cover_file_is_not_accepted(self):
        app = self.app()
        app.thumbnail_path = '/tmp/custom.jpg'
        upload = locator(1)
        handle = MagicMock()
        handle.evaluate = AsyncMock(side_effect=['another.jpg', None])
        handle.dispose = AsyncMock()
        upload.evaluate_handle = AsyncMock(return_value=handle)
        with self.assertRaisesRegex(RuntimeError, 'did not accept'):
            await app._select_cover_file(upload)
        handle.dispose.assert_awaited_once()

    async def test_new_editor_branch_still_requires_saved_main_card_pixel_match(self):
        app = self.app()
        card, area, panel, entry = locator(1), locator(1), locator(), locator(1)
        root = MagicMock()
        root.locator.side_effect = [card, area, panel]
        root.get_by_text.return_value = entry
        app.locator_base = root
        app._cover_snapshot = AsyncMock(side_effect=[['old'], ['new'], ['new']])
        expected = bytes([80, 100, 120] * 1024)
        app._saved_cover_sample = AsyncMock(return_value=expected)
        app._upload_fullscreen_cover = AsyncMock()
        app._upload_legacy_cover = AsyncMock()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        with tempfile.TemporaryDirectory() as temp:
            cover = Path(temp) / 'cover.jpg'
            cover.write_bytes(b'local-cover')
            app.thumbnail_path = str(cover)
            with patch.object(tk, '_image_sample', AsyncMock(return_value=expected)):
                await app.upload_thumbnails(page)
        entry.click.assert_awaited_once_with(timeout=10000)
        app._upload_fullscreen_cover.assert_awaited_once_with(page, area)
        app._upload_legacy_cover.assert_not_awaited()
        app._saved_cover_sample.assert_awaited_once_with(card)
        self.assertTrue(app.cover_verified)
        self.assertEqual(app._verified_cover_sample, expected)

    async def test_known_feature_guide_is_completed_without_forced_click(self):
        app = self.app()
        page = MagicMock()
        guide, overlay, button = locator(1), locator(1), locator(1)
        guide.get_by_role.return_value = button
        page.locator.side_effect = [guide, overlay]
        await app._dismiss_feature_guide(page)
        guide.get_by_role.assert_called_once_with("button", name="Got it", exact=True)
        button.click.assert_awaited_once_with(timeout=5000)
        guide.wait_for.assert_awaited_once_with(state="hidden", timeout=5000)
        overlay.wait_for.assert_awaited_once_with(state="hidden", timeout=5000)

    async def test_phone_preview_guide_uses_observed_title_and_preserves_caption(self):
        app = self.app()
        page = MagicMock()
        tooltips, guide, overlay, button = locator(1), locator(), locator(1), locator(1)
        observed_text = (
            'Preview your video on your phone\n'
            'Now you can view your video as it will appear on TikTok.\nGot it'
        )
        def filter_observed_tooltip(*, has_text):
            # Evaluate the actual production matcher, so dropping the new title fails.
            guide.count.return_value = int(bool(has_text.search(observed_text)))
            return guide
        tooltips.filter.side_effect = filter_observed_tooltip
        guide.get_by_role.return_value = button
        page.locator.side_effect = lambda selector: (
            tooltips if selector == '.react-joyride__tooltip:visible' else overlay
        )
        editor = locator(1)
        caption = '每天3分钟，搞定听力\n#英语学习 #听力'
        editor.inner_text = AsyncMock(return_value=caption)
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = editor
        page.keyboard.press = AsyncMock()
        await app._dismiss_editor_overlays(page)
        guide.get_by_role.assert_called_once_with('button', name='Got it', exact=True)
        button.click.assert_awaited_once_with(timeout=5000)
        guide.wait_for.assert_awaited_once_with(state='hidden', timeout=5000)
        overlay.wait_for.assert_awaited_once_with(state='hidden', timeout=5000)
        editor.evaluate.assert_awaited_once_with('el => el.blur()')
        page.keyboard.press.assert_awaited_once_with('Escape')
        editor.fill.assert_not_called()
        self.assertEqual(editor.inner_text.await_count, 2)
        page.evaluate.assert_not_called()

    async def test_partial_or_unknown_guide_titles_are_not_automatically_acknowledged(self):
        for unknown_text in (
            'Preview your video\nChoose who can see this post.\nGot it',
            'New editing features\nAccept the revised terms.\nGot it',
            'Publish your video on your phone\nGot it',
        ):
            with self.subTest(tooltip=unknown_text):
                app = self.app()
                page = MagicMock()
                tooltips, guide, overlay, button = locator(1), locator(), locator(1), locator(1)
                def filter_unknown_tooltip(*, has_text):
                    guide.count.return_value = int(bool(has_text.search(unknown_text)))
                    return guide
                tooltips.filter.side_effect = filter_unknown_tooltip
                guide.get_by_role.return_value = button
                page.locator.side_effect = lambda selector: (
                    tooltips if selector == '.react-joyride__tooltip:visible' else overlay
                )
                with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
                    await app._dismiss_feature_guide(page)
                button.click.assert_not_awaited()
                guide.get_by_role.assert_not_called()
                page.evaluate.assert_not_called()

    async def test_backdrop_can_precede_known_tooltip_without_bypassing_it(self):
        app = self.app()
        page = MagicMock()
        guide, overlay, button = locator(), locator(1), locator(1)
        guide.get_by_role.return_value = button
        page.locator.side_effect = [guide, overlay, overlay]
        async def animate_tooltip(*, state, timeout):
            if state == 'visible':
                button.click.assert_not_awaited()
                guide.count.return_value = 1
        guide.wait_for.side_effect = animate_tooltip
        await app._dismiss_feature_guide(page)
        self.assertEqual(guide.wait_for.await_args_list[0].kwargs, {'state': 'visible', 'timeout': 5000})
        button.click.assert_awaited_once_with(timeout=5000)
        self.assertEqual(guide.wait_for.await_args_list[1].kwargs, {'state': 'hidden', 'timeout': 5000})
        overlay.wait_for.assert_awaited_once_with(state='hidden', timeout=5000)
        page.evaluate.assert_not_called()

    async def test_unknown_overlay_is_not_bypassed(self):
        app = self.app()
        page = MagicMock()
        page.locator.side_effect = [locator(), locator(1)]
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            await app._dismiss_feature_guide(page)
        page.get_by_role.assert_not_called()
        page.evaluate.assert_not_called()

    def caption_fixture(self, *, persist_filename=False, reinsert_once=False):
        app = self.app()
        app.tags = ['英语学习', '听力']
        expected = app.title + '\n#英语学习 #听力'
        page, editor = MagicMock(), locator(1)
        page.wait_for_timeout = AsyncMock()
        state = {'text': 'output', 'selected': False, 'insertions': 0}
        editor.inner_text = AsyncMock(side_effect=lambda: state['text'])
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = editor
        async def press(key):
            if key == 'ControlOrMeta+A':
                state['selected'] = True
            elif key == 'Backspace' and state['selected']:
                state['text'] = 'output' if persist_filename else ''
                state['selected'] = False
        async def insert_text(value):
            self.assertEqual(state['text'], '', 'Insertion must wait for confirmed empty editor')
            state['insertions'] += 1
            state['text'] = ('output' if reinsert_once and state['insertions'] == 1 else '') + value
        page.keyboard.press = AsyncMock(side_effect=press)
        page.keyboard.insert_text = AsyncMock(side_effect=insert_text)
        app._dismiss_editor_overlays = AsyncMock()
        return app, page, editor, state, expected

    async def test_caption_keyboard_clear_removes_filename_before_insertion(self):
        app, page, editor, state, expected = self.caption_fixture()
        await app.add_title_tags(page)
        self.assertEqual(state['text'], expected)
        self.assertEqual([call.args[0] for call in page.keyboard.press.await_args_list], ['ControlOrMeta+A', 'Backspace'])
        page.keyboard.insert_text.assert_awaited_once_with(expected)
        editor.click.assert_awaited_once_with(timeout=10000)
        editor.fill.assert_not_called()
        editor.evaluate.assert_not_called()
        app._dismiss_editor_overlays.assert_awaited_once_with(page)

    async def test_caption_replacement_retries_if_filename_is_reinserted_once(self):
        app, page, editor, state, expected = self.caption_fixture(reinsert_once=True)
        await app.add_title_tags(page)
        self.assertEqual(state['text'], expected)
        self.assertEqual(page.keyboard.insert_text.await_count, 2)
        self.assertEqual(editor.click.await_count, 2)
        app._dismiss_editor_overlays.assert_awaited_once_with(page)

    async def test_caption_that_cannot_be_cleared_stops_after_three_attempts(self):
        app, page, editor, state, _ = self.caption_fixture(persist_filename=True)
        with self.assertRaisesRegex(RuntimeError, 'after 3 keyboard replacements'):
            await app.add_title_tags(page)
        self.assertEqual(editor.click.await_count, 3)
        self.assertEqual(page.keyboard.press.await_count, 6)
        page.keyboard.insert_text.assert_not_awaited()
        app._dismiss_editor_overlays.assert_not_awaited()
        self.assertEqual(state['text'], 'output')

    async def test_caption_is_verified_again_after_overlay_dismissal(self):
        app, page, _, state, expected = self.caption_fixture()
        async def mutate_caption(_page):
            state['text'] = 'output' + expected
        app._dismiss_editor_overlays.side_effect = mutate_caption
        with self.assertRaisesRegex(RuntimeError, 'caption changed after dismissing'):
            await app.add_title_tags(page)
        page.keyboard.insert_text.assert_awaited_once()

    async def test_autocomplete_is_dismissed_without_changing_caption(self):
        app = self.app()
        app._dismiss_feature_guide = AsyncMock()
        app.locator_base = MagicMock()
        editor = locator(1)
        text = '每天3分钟，搞定听力\n#听力 #英语学习'
        editor.inner_text = AsyncMock(return_value=text)
        app.locator_base.locator.return_value = editor
        page = MagicMock()
        page.keyboard.press = AsyncMock()
        await app._dismiss_editor_overlays(page)
        editor.evaluate.assert_awaited_once_with('el => el.blur()')
        page.keyboard.press.assert_awaited_once_with('Escape')
        editor.fill.assert_not_called()
        self.assertEqual(editor.inner_text.await_count, 2)

    async def test_dismissal_that_changes_caption_stops_the_upload(self):
        app = self.app()
        app._dismiss_feature_guide = AsyncMock()
        app.locator_base = MagicMock()
        editor = locator(1)
        editor.inner_text = AsyncMock(side_effect=['Title #英语学习', 'Title'])
        app.locator_base.locator.return_value = editor
        page = MagicMock()
        page.keyboard.press = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'caption changed'):
            await app._dismiss_editor_overlays(page)

    async def test_observed_audience_button_is_distinct_from_caption_combobox(self):
        app = self.app()
        root = MagicMock()
        old, named, heading, audience = locator(), locator(), locator(1), locator(1)
        audience.inner_text = AsyncMock(return_value='Everyone')
        root.locator.side_effect = [old, audience]
        root.get_by_role.return_value = named
        root.get_by_text.return_value = heading
        app.locator_base = root
        await app.set_visibility(MagicMock())
        root.get_by_text.assert_called_once_with('Who can see this post', exact=True)
        self.assertEqual(root.locator.call_args.args[0], 'button[role="combobox"]:visible')
        audience.click.assert_not_awaited()

    async def test_ambiguous_audience_buttons_stop_before_selection(self):
        app = self.app()
        root = MagicMock()
        root.locator.side_effect = [locator(), locator(2)]
        root.get_by_role.return_value = locator()
        root.get_by_text.return_value = locator(1)
        app.locator_base = root
        with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
            await app.set_visibility(MagicMock())

    async def test_known_continue_dialog_clicks_only_once_without_forcing(self):
        app = self.app()
        app.locator_base = MagicMock()
        dialog, post_now, cancel = locator(1), locator(1), locator(1)
        dialog.inner_text = AsyncMock(return_value="Continue to post?\nThe copyright check is incomplete. Posting your video now will stop the check.\nWe're still checking your video for potential issues. Do you want to continue posting before the check is complete?\nCancel\nPost now")
        dialog.get_by_role.side_effect = lambda role, name, exact: post_now if name == 'Post now' else cancel
        app.locator_base.get_by_role.return_value = dialog
        self.assertTrue(await app._continue_post_dialog(False))
        self.assertFalse(await app._continue_post_dialog(True))
        post_now.click.assert_awaited_once_with(timeout=10000)
        cancel.click.assert_not_awaited()

    async def test_unknown_continue_warning_never_confirms(self):
        app = self.app()
        app.locator_base = MagicMock()
        dialog = locator(1)
        dialog.inner_text = AsyncMock(return_value='Continue to post? Copyright violation found. Cancel Post now')
        app.locator_base.get_by_role.return_value = dialog
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            await app._continue_post_dialog(False)
        dialog.get_by_role.assert_not_called()

    async def test_continue_dialog_completes_same_transaction_without_second_main_post(self):
        app, page, main_post = self.submit_fixture()
        app._continue_post_dialog = AsyncMock(side_effect=[True, False, False])
        with patch.object(tk, 'monotonic', side_effect=[0, 1, 2, 301]):
            result = await app.click_publish(page)
        self.assertEqual(result['status'], 'needs_verification')
        main_post.click.assert_awaited_once_with(timeout=15000)
        self.assertEqual([call.args[0] for call in app._continue_post_dialog.await_args_list], [False, True])

    async def test_actual_endpoint_response_maps_exact_known_item_link(self):
        app, page, main_post = self.submit_fixture()
        app._continue_post_dialog = AsyncMock(return_value=False)
        known_link = locator(1)
        known_link.evaluate.return_value = 'https://www.tiktok.com/@foxlingo/video/7690695822652247316'
        page.locator.return_value = known_link
        response = SimpleNamespace(url='https://www.tiktok.com/tiktok/web/project/post/v1/', request=SimpleNamespace(method='POST'), json=AsyncMock(return_value={
            'project_id': '7690695779241776148', 'project_status': 1, 'status_code': 0,
            'single_post_resp_list': [{'batch_index': 0, 'item_id': '7690695822652247316', 'status_code': 0, 'status_msg': ''}],
        }))
        async def click(**kwargs):
            page.on.call_args.args[1](response)
            await asyncio.sleep(0)
        main_post.click.side_effect = click
        result = await app.click_publish(page)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['post_id'], '7690695822652247316')
        page.locator.assert_called_once_with('a[href*="/video/7690695822652247316"]')
        main_post.click.assert_awaited_once()

    async def test_cookie_refresh_failure_preserves_success_receipt(self):
        page, context, browser, playwright, _ = browser_fixture()
        app = self.app()
        app.validate = MagicMock()
        app.choose_base_locator = AsyncMock()
        app._dismiss_feature_guide = AsyncMock()
        app._dismiss_editor_overlays = AsyncMock()
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = locator(1)
        app.add_title_tags = AsyncMock()
        app.detect_upload_status = AsyncMock()
        app.set_visibility = AsyncMock()
        receipt = {'status': 'success', 'post_id': '123'}
        app.click_publish = AsyncMock(return_value=receipt)
        context.storage_state.side_effect = OSError('read-only cookie')
        with patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)):
            result = await app.upload(playwright)
        self.assertIs(result, receipt)
        browser.close.assert_awaited_once()


    async def test_cookie_refresh_timeout_preserves_confirmed_receipt(self):
        page, context, browser, playwright, _ = browser_fixture()
        app = self.app()
        app.validate = MagicMock()
        app.choose_base_locator = AsyncMock()
        app._dismiss_feature_guide = AsyncMock()
        app._dismiss_editor_overlays = AsyncMock()
        app.locator_base = MagicMock()
        app.locator_base.locator.return_value = locator(1)
        app.add_title_tags = AsyncMock()
        app.detect_upload_status = AsyncMock()
        app.set_visibility = AsyncMock()
        receipt = {'status': 'success', 'post_id': '123'}
        app.click_publish = AsyncMock(return_value=receipt)
        cancelled = asyncio.Event()
        async def stalled_storage_state(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        context.storage_state.side_effect = stalled_storage_state
        with patch.object(tk, 'set_init_script', AsyncMock(side_effect=lambda c: c)), patch.object(tk, 'SESSION_SAVE_TIMEOUT_SECONDS', .01):
            result = await asyncio.wait_for(app.upload(playwright), timeout=1)
        self.assertIs(result, receipt)
        self.assertTrue(cancelled.is_set())
        app.click_publish.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_each_resource_close_timeout_still_attempts_the_next_resource(self):
        context, browser = MagicMock(), MagicMock()
        cancelled = []
        async def stalled_close(name):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(name)
        async def close_context():
            await stalled_close('context')
        async def close_browser():
            await stalled_close('browser')
        context.close = AsyncMock(side_effect=close_context)
        browser.close = AsyncMock(side_effect=close_browser)
        with patch.object(tk, 'RESOURCE_CLOSE_TIMEOUT_SECONDS', .01):
            await asyncio.wait_for(tk._close_resources(context, browser), timeout=1)
        self.assertEqual(cancelled, ['context', 'browser'])
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_main_stop_timeout_preserves_each_existing_receipt(self):
        for status in ('success', 'needs_verification'):
            with self.subTest(status=status):
                _, _, _, playwright, manager = browser_fixture()
                app = self.app()
                receipt = {'status': status, 'post_id': '123'}
                app.upload = AsyncMock(return_value=receipt)
                cancelled = asyncio.Event()
                async def stalled_stop():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                playwright.stop.side_effect = stalled_stop
                with patch.object(tk, 'async_playwright', return_value=manager), patch.object(tk, 'PLAYWRIGHT_STOP_TIMEOUT_SECONDS', .01):
                    result = await asyncio.wait_for(app.main(), timeout=1)
                self.assertIs(result, receipt)
                self.assertTrue(cancelled.is_set())
                manager.start.assert_awaited_once()
                playwright.stop.assert_awaited_once()
                app.upload.assert_awaited_once_with(playwright)
                manager.__aexit__.assert_not_awaited()

    async def test_main_stop_failure_does_not_replace_pre_submission_error(self):
        _, _, _, playwright, manager = browser_fixture()
        app = self.app()
        original = tk.TikTokPreSubmissionError('cover mismatch')
        app.upload = AsyncMock(side_effect=original)
        playwright.stop.side_effect = RuntimeError('driver already disconnected')
        with patch.object(tk, 'async_playwright', return_value=manager):
            with self.assertRaises(tk.TikTokPreSubmissionError) as caught:
                await app.main()
        self.assertIs(caught.exception, original)
        self.assertFalse(caught.exception.submission_started)
        playwright.stop.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
