import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.youtube_uploader import main as youtube

POST_ID = "abcdefghijk"
POST_URL = f"https://www.youtube.com/watch?v={POST_ID}"


def locator(count=1):
    item = MagicMock()
    item.first = item
    item.count = AsyncMock(return_value=count)
    item.is_visible = AsyncMock(return_value=bool(count))
    item.wait_for = AsyncMock()
    item.click = AsyncMock()
    item.fill = AsyncMock()
    item.press = AsyncMock()
    item.inner_text = AsyncMock(return_value="")
    item.evaluate = AsyncMock(return_value=True)
    item.evaluate_all = AsyncMock(return_value=[])
    item.set_input_files = AsyncMock()
    item.get_attribute = AsyncMock(return_value=None)
    item.locator.return_value = item
    item.filter.return_value = item
    return item


def browser_fixture():
    page = MagicMock()
    page.url = "https://studio.youtube.com/channel/UC_example"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.close = AsyncMock()
    page.evaluate = AsyncMock()
    page.locator.return_value = locator()
    page.get_by_text.return_value = locator(0)
    page.keyboard.press = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock()
    context.close = AsyncMock()
    page.context = context
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    probe = MagicMock()
    probe.evaluate = AsyncMock(return_value="Mozilla/5.0 HeadlessChrome/153.0.8010.53 Safari/537.36")
    probe.close = AsyncMock()
    browser.new_page = AsyncMock(return_value=probe)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=False)
    return page, context, browser, playwright, manager


class YouTubeMetadataTests(unittest.TestCase):
    def test_shorts_eligibility_limits_and_rotation(self):
        for width, height, seconds, rotation, expected in [
            (1920, 1080, 60, 0, "video"),
            (1080, 1920, 180, 0, "shorts"),
            (1080, 1080, 180, 0, "shorts"),
            (1080, 1920, 180.01, 0, "video"),
            (1920, 1080, 120, 90, "shorts"),
            (0, 1920, 30, 0, "unknown"),
            (float("nan"), 1920, 30, 0, "unknown"),
            (1080, 1920, None, 0, "unknown"),
        ]:
            with self.subTest(values=(width, height, seconds, rotation)):
                self.assertEqual(youtube.classify_content_kind(width, height, seconds, rotation), expected)

    def test_video_id_requires_recognized_host_and_exact_id_shape(self):
        self.assertEqual(youtube._video_id_from_url(POST_URL), POST_ID)
        self.assertEqual(youtube._video_id_from_url(f"https://youtu.be/{POST_ID}?feature=share"), POST_ID)
        self.assertEqual(youtube._video_id_from_url(f"https://www.youtube.com/shorts/{POST_ID}"), POST_ID)
        self.assertEqual(youtube._video_id_from_url(f"https://studio.youtube.com/video/{POST_ID}/edit"), POST_ID)
        self.assertIsNone(youtube._video_id_from_url(f"https://youtube.com.example.com/watch?v={POST_ID}"))
        self.assertIsNone(youtube._video_id_from_url("https://www.youtube.com/watch?v=too-short"))
        self.assertIsNone(youtube._video_id_from_url("https://studio.youtube.com/channel/UC_example"))

    def test_browser_options_use_same_proxy_and_honor_headless(self):
        with patch.dict(youtube.os.environ, {"SAU_YOUTUBE_PROXY": "http://127.0.0.1:1234"}), patch.object(youtube, "YT_PROXY", "http://old:1"):
            self.assertEqual(youtube._browser_options(headless=True), {
                "headless": True, "channel": "chrome", "proxy": {"server": "http://127.0.0.1:1234"},
            })
            self.assertEqual(youtube._browser_options(headless=False, proxy=""), {"headless": False, "channel": "chrome"})
            self.assertEqual(youtube._browser_options(headless=False, proxy="http://user:p%40ss@127.0.0.1:2345")["proxy"],
                             {"server": "http://127.0.0.1:2345", "username": "user", "password": "p@ss"})

    def test_missing_probe_returns_unknown_without_transcoding(self):
        with patch.object(youtube.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(youtube.probe_content_kind("video.mp4"), "unknown")

    def test_eligible_media_is_not_reported_as_verified_shorts(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            account = Path(directory) / "account.json"
            video.write_bytes(b"video")
            account.write_text("{}")
            app = youtube.YouTubeVideo("title", video, [], account)
            with patch.object(youtube, "probe_content_kind", return_value="shorts"):
                app.validate_upload_args()
            self.assertTrue(app.shorts_eligible)
            self.assertEqual(app.content_kind, "unknown")

    def test_invalid_visibility_is_not_silently_changed_to_public(self):
        with self.assertRaises(ValueError):
            youtube.YouTubeVideo("title", "video.mp4", [], "account.json", visibility="typo")


class YouTubeLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_context_uses_actual_browser_version_and_preserves_locale_storage(self):
        _, context, browser, *_ = browser_fixture()
        with patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)):
            self.assertIs(await youtube._new_context(browser, headless=True, account_file="account.json"), context)
        browser.new_context.assert_awaited_once_with(locale="en-US", storage_state="account.json", user_agent="Mozilla/5.0 Chrome/153.0.8010.53 Safari/537.36")
        browser.new_page.return_value.close.assert_awaited_once()

    async def test_headed_context_keeps_native_user_agent(self):
        _, _, browser, *_ = browser_fixture()
        with patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)):
            await youtube._new_context(browser, headless=False)
        browser.new_context.assert_awaited_once_with(locale="en-US")
        browser.new_page.assert_not_awaited()

    async def test_failed_user_agent_probe_closes_probe_without_creating_context(self):
        _, _, browser, *_ = browser_fixture()
        browser.new_page.return_value.evaluate.side_effect = RuntimeError("probe failed")
        with self.assertRaises(RuntimeError):
            await youtube._new_context(browser, headless=True)
        browser.new_page.return_value.close.assert_awaited_once()
        browser.new_context.assert_not_awaited()

    async def test_channel_url_without_authenticated_navigation_is_not_valid(self):
        page, *_ = browser_fixture()
        page.locator.return_value = locator(0)
        self.assertFalse(await youtube._is_authenticated_studio(page))

    async def test_official_unsupported_browser_skip_does_not_itself_prove_login(self):
        page, *_ = browser_fixture()
        page.url = "https://www.youtube.com/supported_browsers"
        skip = locator()
        page.get_by_text.side_effect = lambda text, **kwargs: skip if text == "SKIP TO YOUTUBE STUDIO" else locator(0)
        self.assertFalse(await youtube._is_authenticated_studio(page))
        skip.click.assert_awaited_once()

    async def test_cookie_check_failure_returns_false_and_closes_resources(self):
        page, context, browser, playwright, manager = browser_fixture()
        page.goto.side_effect = TimeoutError("network timeout")
        with patch.object(youtube.Path, "is_file", return_value=True), patch.object(youtube, "async_playwright", return_value=manager), patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)):
            self.assertFalse(await youtube.cookie_auth("account.json", headless=True, proxy="http://127.0.0.1:1234"))
        playwright.chromium.launch.assert_awaited_once_with(headless=True, channel="chrome", proxy={"server": "http://127.0.0.1:1234"})
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        context.storage_state.assert_not_awaited()

    async def test_interactive_login_uses_visible_chrome_and_only_saves_after_auth(self):
        page, context, browser, playwright, manager = browser_fixture()
        with tempfile.TemporaryDirectory() as directory:
            account = str(Path(directory) / "account.json")
            with patch.object(youtube, "async_playwright", return_value=manager), patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)), patch.object(youtube, "_is_authenticated_studio", AsyncMock(return_value=True)):
                result = await youtube.youtube_cookie_gen(account, headless=True, proxy="http://127.0.0.1:1234")
            self.assertTrue(result["success"])
            context.storage_state.assert_awaited_once_with(path=account)
        playwright.chromium.launch.assert_awaited_once_with(headless=False, channel="chrome", proxy={"server": "http://127.0.0.1:1234"})
        browser.close.assert_awaited_once()


class YouTubeFlowTests(unittest.IsolatedAsyncioTestCase):
    def app(self, thumbnail=None):
        return youtube.YouTubeVideo("same batch title", "video.mp4", [], "account.json", thumbnail_path=thumbnail, headless=True)

    async def test_upload_progress_mixed_with_processing_does_not_finish_early(self):
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "Uploading 76% · HD processing will begin soon"
        with self.assertRaises(TimeoutError):
            await youtube._wait_upload_complete(page, max_polls=1)

    async def test_percentage_without_uploading_word_still_blocks_processing_shortcut(self):
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "76% · Processing will begin soon"
        with self.assertRaises(TimeoutError):
            await youtube._wait_upload_complete(page, max_polls=1)

    async def test_missing_progress_is_not_upload_success(self):
        page, *_ = browser_fixture()
        page.locator.return_value = locator(0)
        with self.assertRaises(TimeoutError):
            await youtube._wait_upload_complete(page, max_polls=1)

    async def test_explicit_upload_failure_stops(self):
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "Upload failed. Processing abandoned."
        with self.assertRaises(RuntimeError):
            await youtube._wait_upload_complete(page, max_polls=1)

    async def test_processing_after_upload_is_valid_completion_evidence(self):
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "Upload complete. Processing HD version."
        self.assertTrue(await youtube._wait_upload_complete(page, max_polls=1))

    async def test_requested_thumbnail_requires_loaded_selected_custom_tile(self):
        app = self.app("cover.jpg")
        page, *_ = browser_fixture()
        app._thumbnail_state = AsyncMock(side_effect=[
            {"selected": False, "sources": ["old-frame"]},
            {"selected": False, "sources": ["custom-image"]},
            {"selected": True, "sources": ["custom-image"]},
        ])
        await app.set_thumbnail(page)
        self.assertTrue(app._thumbnail_prepared)
        self.assertFalse(app.cover_verified)  # Persisted cover still needs confirmation after publish.
        page.locator.assert_called_once_with("ytcp-uploads-dialog ytcp-thumbnail-uploader")
        page.locator.return_value.set_input_files.assert_awaited_once_with("cover.jpg")

    async def test_requested_thumbnail_does_not_accept_old_preview(self):
        app = self.app("cover.jpg")
        page, *_ = browser_fixture()
        app._thumbnail_state = AsyncMock(return_value={"selected": True, "sources": ["old-frame"]})
        with self.assertRaisesRegex(RuntimeError, "封面"):
            await app.set_thumbnail(page)
        self.assertFalse(app._thumbnail_prepared)

    async def test_visibility_must_actually_be_checked(self):
        page, *_ = browser_fixture()
        page.locator.return_value.evaluate.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未按请求选中"):
            await youtube._select_radio(page, "radio-selector", "public")

    async def test_current_upload_id_is_scoped_and_ambiguous_ids_stop(self):
        app = self.app()
        page, *_ = browser_fixture()
        with patch.object(youtube, "_scoped_video_ids", AsyncMock(return_value={POST_ID, "zzzzzzzzzzz"})):
            with self.assertRaisesRegex(RuntimeError, "多个作品 ID"):
                await app._current_upload_id(page)
        page.locator.assert_called_once_with("ytcp-uploads-dialog")

    async def test_completion_dialog_for_other_id_does_not_confirm_current_upload(self):
        app = self.app()
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "Video published"
        with patch.object(youtube, "_scoped_video_ids", AsyncMock(return_value={"zzzzzzzzzzz"})):
            self.assertIsNone(await app._confirm_publication(page, POST_ID, max_polls=1))

    async def test_matching_completion_dialog_confirms_this_upload(self):
        app = self.app()
        page, *_ = browser_fixture()
        page.locator.return_value.inner_text.return_value = "Video published"
        with patch.object(youtube, "_scoped_video_ids", AsyncMock(return_value={POST_ID})):
            self.assertEqual(await app._confirm_publication(page, POST_ID, max_polls=1), "published")

    def processing_page(self, *, ids=None, popup_count=1, heading="Video processing", text=None):
        page, *_ = browser_fixture()
        popup = locator(popup_count)
        popup.inner_text.return_value = text or "Video processing\nThe standard definition (SD) version of your video needs to finish processing before your video is public on YouTube\nSame title\nClose"
        header = locator()
        header.inner_text.return_value = heading
        popup.locator.return_value = header
        uploads = locator()
        uploads.evaluate_all.return_value = [f"https://youtu.be/{item}" for item in (ids if ids is not None else [POST_ID])]
        host = locator()
        host.is_visible.return_value = False
        controls = {
            "ytcp-video-share-dialog tp-yt-paper-dialog[role='dialog']:visible": locator(0),
            "ytcp-uploads-still-processing-dialog tp-yt-paper-dialog[role='dialog']:visible": popup,
            "ytcp-uploads-dialog tp-yt-paper-dialog[role='dialog']:visible": uploads,
            "ytcp-video-share-dialog": host,
            "ytcp-uploads-still-processing-dialog": host,
            "ytcp-uploads-dialog": host,
        }
        page.locator.side_effect = lambda selector: controls.get(selector, locator(0))
        return page

    async def test_exact_id_processing_acknowledgement_returns_immediately(self):
        app = self.app()
        page = self.processing_page()
        self.assertEqual(await app._confirm_publication(page, POST_ID, max_polls=1), "accepted_pending_processing")
        page.wait_for_timeout.assert_not_awaited()

    async def test_processing_and_upload_hosts_without_boxes_use_visible_inner_dialogs(self):
        page = self.processing_page()
        self.assertFalse(await page.locator("ytcp-uploads-still-processing-dialog").is_visible())
        self.assertFalse(await page.locator("ytcp-uploads-dialog").is_visible())
        self.assertEqual(await self.app()._confirm_publication(page, POST_ID, max_polls=1), "accepted_pending_processing")
        self.assertIn(unittest.mock.call("ytcp-uploads-dialog tp-yt-paper-dialog[role='dialog']:visible"), page.locator.call_args_list)

    async def test_share_host_without_box_uses_unique_visible_inner_dialog(self):
        for count, expected in [(1, "published"), (2, None)]:
            with self.subTest(count=count):
                page, *_ = browser_fixture()
                host = locator()
                host.is_visible.return_value = False
                inner = locator(count)
                inner.inner_text.return_value = "Video published"
                inner.evaluate_all.return_value = [POST_URL]
                selectors = {"ytcp-video-share-dialog": host, "ytcp-video-share-dialog tp-yt-paper-dialog[role='dialog']:visible": inner}
                page.locator.side_effect = lambda selector: selectors.get(selector, locator(0))
                self.assertFalse(await page.locator("ytcp-video-share-dialog").is_visible())
                self.assertEqual(await self.app()._confirm_publication(page, POST_ID, max_polls=1), expected)

    async def test_processing_popup_requires_unique_same_upload_id(self):
        for ids in [[], ["zzzzzzzzzzz"], [POST_ID, "zzzzzzzzzzz"]]:
            with self.subTest(ids=ids):
                self.assertIsNone(await self.app()._confirm_publication(self.processing_page(ids=ids), POST_ID, max_polls=1))

    async def test_processing_acknowledgement_requires_unique_real_component_and_explicit_message(self):
        for options in [
            {"popup_count": 0}, {"popup_count": 2},
            {"heading": "Other dialog"},
            {"text": "Video processing\nSomething unrelated about being public"},
        ]:
            with self.subTest(options=options):
                self.assertIsNone(await self.app()._confirm_publication(self.processing_page(**options), POST_ID, max_polls=1))

    async def test_public_processing_message_does_not_confirm_private_request(self):
        app = self.app()
        app.visibility = "private"
        self.assertIsNone(await app._confirm_publication(self.processing_page(), POST_ID, max_polls=1))

    async def test_pending_processing_success_requires_saved_details_and_preserves_pending_state(self):
        app = self.app("cover.jpg")
        app._thumbnail_prepared = True
        result, page, context, browser = await self.run_upload(app, confirmed="accepted_pending_processing", observed_visibility="pending")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["remote_status"], "accepted_pending_processing")
        self.assertEqual(result["requested_visibility"], "public")
        self.assertEqual(result["observed_visibility"], "pending")
        self.assertEqual(result["post_id"], POST_ID)
        self.assertTrue(result["cover_verified"])
        app._verify_saved_details.assert_awaited_once_with(page, POST_ID, allow_pending=True)
        page.locator.return_value.click.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_processing_acknowledgement_without_saved_settings_is_not_success(self):
        result, page, _, _ = await self.run_upload(self.app(), confirmed="accepted_pending_processing", persisted=False)
        self.assertEqual(result["status"], "needs_verification")
        self.assertEqual(result["remote_status"], "settings_unverified")
        page.locator.return_value.click.assert_awaited_once()

    async def test_pending_dialog_followed_by_persisted_public_is_published(self):
        result, _, _, _ = await self.run_upload(self.app(), confirmed="accepted_pending_processing", observed_visibility="public")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["remote_status"], "published")
        self.assertEqual(result["observed_visibility"], "public")

    async def test_saved_pending_requires_explicit_allow_pending_and_persisted_custom_cover(self):
        for allow_pending, selected, expected in [(False, True, False), (True, False, False), (True, True, True)]:
            with self.subTest(allow_pending=allow_pending, selected=selected):
                app = self.app("cover.jpg")
                page, *_ = browser_fixture()
                details = browser_fixture()[0]
                details.url = f"https://studio.youtube.com/video/{POST_ID}/edit"
                details.locator.return_value.inner_text.return_value = "Visibility\nPending"
                page.context.new_page.return_value = details
                app._thumbnail_state = AsyncMock(return_value={"selected": selected, "sources": ["persisted-custom-cover"]})
                self.assertEqual(await app._verify_saved_details(page, POST_ID, allow_pending=allow_pending), expected)
                self.assertEqual(app.observed_visibility, "pending")
                self.assertEqual(app.cover_verified, expected)

    async def test_saved_details_requires_requested_visibility_for_exact_id(self):
        app = self.app()
        page, *_ = browser_fixture()
        details = browser_fixture()[0]
        details.url = f"https://studio.youtube.com/video/{POST_ID}/edit"
        details.locator.return_value.inner_text.return_value = "Visibility\nPrivate"
        page.context.new_page.return_value = details
        self.assertFalse(await app._verify_saved_details(page, POST_ID))
        details.close.assert_awaited_once()

    async def test_saved_details_verifies_custom_cover_and_actual_shorts_link(self):
        app = self.app("cover.jpg")
        page, *_ = browser_fixture()
        details = browser_fixture()[0]
        details.url = f"https://studio.youtube.com/video/{POST_ID}/edit"
        details.locator.return_value.inner_text.return_value = "Visibility\nPublic"
        details.locator.return_value.evaluate_all.return_value = [f"https://www.youtube.com/shorts/{POST_ID}"]
        page.context.new_page.return_value = details
        app._thumbnail_state = AsyncMock(return_value={"selected": True, "sources": ["persisted-custom-cover"]})
        self.assertTrue(await app._verify_saved_details(page, POST_ID))
        self.assertTrue(app.cover_verified)
        self.assertEqual(app.content_kind, "shorts")

    async def run_upload(self, app, *, upload_error=None, click_error=None, confirmed="published", persisted=True, observed_visibility=None):
        page, context, browser, playwright, _ = browser_fixture()
        app.validate_upload_args = MagicMock()
        app._fill_details = AsyncMock()
        app._set_visibility = AsyncMock()
        app._current_upload_id = AsyncMock(return_value=POST_ID)
        app._confirm_publication = AsyncMock(return_value=confirmed)
        async def verify_saved(*args, **kwargs):
            if persisted:
                app.observed_visibility = observed_visibility or app.visibility
                app.cover_verified = bool(app.thumbnail_path)
            return persisted
        app._verify_saved_details = AsyncMock(side_effect=verify_saved)
        page.locator.return_value.click.side_effect = click_error
        with patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)), patch.object(youtube, "_is_authenticated_studio", AsyncMock(return_value=True)), patch.object(youtube, "_select_radio", AsyncMock()), patch.object(youtube, "_wait_upload_complete", AsyncMock(side_effect=upload_error)):
            result = await app.upload(playwright)
        return result, page, context, browser

    async def test_upload_timeout_stops_before_publish_and_closes_everything(self):
        app = self.app()
        page, context, browser, playwright, _ = browser_fixture()
        app.validate_upload_args = MagicMock()
        app._fill_details = AsyncMock()
        app._set_visibility = AsyncMock()
        with patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)), patch.object(youtube, "_is_authenticated_studio", AsyncMock(return_value=True)), patch.object(youtube, "_wait_upload_complete", AsyncMock(side_effect=TimeoutError("upload timeout"))):
            with self.assertRaises(TimeoutError) as raised:
                await app.upload(playwright)
            self.assertFalse(raised.exception.submission_started)
        page.locator.return_value.click.assert_not_awaited()
        context.storage_state.assert_not_awaited()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_thumbnail_failure_prevents_submission(self):
        app = self.app("cover.jpg")
        page, context, browser, playwright, _ = browser_fixture()
        app.validate_upload_args = MagicMock()
        app._fill_details = AsyncMock(side_effect=RuntimeError("cover failed"))
        with patch.object(youtube, "set_init_script", AsyncMock(side_effect=lambda value: value)), patch.object(youtube, "_is_authenticated_studio", AsyncMock(return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "cover failed"):
                await app.upload(playwright)
        page.locator.return_value.click.assert_not_awaited()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_click_timeout_is_needs_verification_and_is_never_retried(self):
        result, page, context, browser = await self.run_upload(self.app(), click_error=TimeoutError("click outcome unknown"))
        self.assertEqual(result["status"], "needs_verification")
        self.assertEqual(result["post_id"], POST_ID)
        self.assertEqual(result["url"], POST_URL)
        self.assertTrue(result["attempted_at"] and result["finished_at"])
        page.locator.return_value.click.assert_awaited_once()
        context.storage_state.assert_not_awaited()
        browser.close.assert_awaited_once()

    async def test_unconfirmed_publish_or_persisted_settings_never_return_success(self):
        for confirmed, persisted in [(None, True), ("published", False)]:
            with self.subTest(confirmed=confirmed, persisted=persisted):
                result, page, _, _ = await self.run_upload(self.app(), confirmed=confirmed, persisted=persisted)
                self.assertEqual(result["status"], "needs_verification")
                page.locator.return_value.click.assert_awaited_once()

    async def test_success_receipt_has_exact_current_id_and_resources_close(self):
        result, page, context, browser = await self.run_upload(self.app())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["post_id"], POST_ID)
        self.assertEqual(result["url"], POST_URL)
        self.assertEqual(result["visibility"], "public")
        self.assertEqual(result["requested_visibility"], "public")
        self.assertEqual(result["observed_visibility"], "public")
        context.storage_state.assert_awaited_once_with(path="account.json")
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_validation_error_is_explicitly_before_submission(self):
        app = self.app()
        app.validate_upload_args = MagicMock(side_effect=ValueError("invalid arguments"))
        playwright = browser_fixture()[3]
        with self.assertRaises(ValueError) as raised:
            await app.upload(playwright)
        self.assertFalse(raised.exception.submission_started)
        playwright.chromium.launch.assert_not_awaited()

    async def test_main_preserves_receipt_after_playwright_cleanup_error(self):
        app = self.app()
        *_, manager = browser_fixture()
        manager.__aexit__.side_effect = RuntimeError("cleanup failed")
        expected = {"status": "success", "post_id": POST_ID}
        app.upload = AsyncMock(return_value=expected)
        with patch.object(youtube, "async_playwright", return_value=manager):
            self.assertEqual(await app.main(), expected)

    async def test_main_returns_receipt(self):
        app = self.app()
        *_, manager = browser_fixture()
        expected = {"status": "needs_verification"}
        app.upload = AsyncMock(return_value=expected)
        with patch.object(youtube, "async_playwright", return_value=manager):
            self.assertEqual(await app.main(), expected)


if __name__ == "__main__":
    unittest.main()
