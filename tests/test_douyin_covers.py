import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from uploader.douyin_uploader import main as douyin_main
from uploader.douyin_uploader.main import (
    COVER_DIALOG_SELECTOR,
    COVER_UPLOAD_SELECTOR,
    COVER_PREVIEW_SELECTOR,
    DouyinCustomCoverError,
    DouYinVideo,
)


def locator(count=0):
    result = MagicMock()
    result.first = result
    result.count = AsyncMock(return_value=count)
    result.is_visible = AsyncMock(return_value=count > 0)
    result.is_enabled = AsyncMock(return_value=True)
    result.get_attribute = AsyncMock(return_value=None)
    result.click = AsyncMock()
    result.wait_for = AsyncMock()
    result.evaluate = AsyncMock()
    result.set_input_files = AsyncMock()
    result.filter.return_value = result
    result.locator.return_value = result
    result.get_by_role.return_value = result
    return result


class DouyinCoverTests(unittest.IsolatedAsyncioTestCase):
    def video(self, landscape=None, portrait=None):
        return DouYinVideo(
            "标题", "/tmp/demo.mp4", [], 0, "/tmp/cookie.json",
            thumbnail_landscape_path=landscape,
            thumbnail_portrait_path=portrait,
        )

    def page(self):
        page = MagicMock()
        page.locator.return_value = locator()
        page.get_by_text.return_value = locator()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock()
        page.keyboard.press = AsyncMock()
        return page

    async def test_both_requested_covers_are_uploaded_rechecked_then_saved(self):
        video = self.video("/tmp/landscape.jpg", "/tmp/portrait.jpg")
        page = self.page()
        cover = object()
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "original-frame", "loaded": True})
        video._wait_for_saved_covers = AsyncMock(return_value={})
        video._open_cover_editor = AsyncMock(return_value=cover)
        video._upload_custom_cover = AsyncMock(side_effect=[{"portrait-preview"}, {"landscape-preview"}])
        video._select_cover_tab = AsyncMock()
        video._wait_for_cover_preview = AsyncMock()
        video._save_custom_covers = AsyncMock()

        await video.set_thumbnail(page)

        self.assertEqual(video._upload_custom_cover.await_args_list, [
            call(page, cover, "设置竖封面", "portrait", "/tmp/portrait.jpg"),
            call(page, cover, "设置横封面", "landscape", "/tmp/landscape.jpg"),
        ])
        self.assertEqual(video._select_cover_tab.await_args_list, [
            call(page, cover, "设置竖封面"), call(page, cover, "设置横封面"),
        ])
        self.assertEqual(video._wait_for_cover_preview.await_args_list, [
            call(page, cover, "portrait", expected={"portrait-preview"}),
            call(page, cover, "landscape", expected={"landscape-preview"}),
        ])
        video._save_custom_covers.assert_awaited_once_with(page, cover)
        self.assertTrue(video._custom_covers_verified)

    async def test_single_landscape_cover_keeps_existing_contract(self):
        video = self.video(landscape="/tmp/landscape.jpg")
        page = self.page()
        cover = object()
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "original-frame", "loaded": True})
        video._wait_for_saved_covers = AsyncMock(return_value={})
        video._open_cover_editor = AsyncMock(return_value=cover)
        video._upload_custom_cover = AsyncMock(return_value={"landscape-preview"})
        video._select_cover_tab = AsyncMock()
        video._wait_for_cover_preview = AsyncMock()
        video._save_custom_covers = AsyncMock()
        await video.set_thumbnail(page)
        video._upload_custom_cover.assert_awaited_once_with(
            page, cover, "设置横封面", "landscape", "/tmp/landscape.jpg"
        )
        self.assertTrue(video._custom_covers_verified)

    async def test_no_requested_cover_does_not_open_or_verify_editor(self):
        video = self.video()
        page = self.page()
        await video.set_thumbnail(page)
        await video._assert_custom_covers_ready(page)
        page.evaluate.assert_not_awaited()
        page.locator.assert_not_called()
        page.get_by_text.assert_not_called()

    async def test_no_custom_cover_keeps_recommended_cover_fallback(self):
        video = self.video()
        page = self.page()
        warning = locator(1)
        confirmation = locator()
        page.get_by_text.side_effect = lambda text: warning if text == "请设置封面后再发布" else confirmation
        recommended = locator(1)
        page.locator.return_value = recommended
        with patch.object(douyin_main.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await video.handle_auto_video_cover(page))
        recommended.click.assert_awaited_once()

    async def test_custom_cover_never_falls_back_to_recommended_cover(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        video._custom_covers_verified = True
        page.get_by_text.return_value = locator(1)
        with self.assertRaisesRegex(DouyinCustomCoverError, "推荐封面"):
            await video.handle_auto_video_cover(page)
        self.assertEqual(page.locator.call_args_list, [call(COVER_DIALOG_SELECTOR)])

    async def test_real_upload_slot_is_scoped_after_tab_selection(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        cover = locator(1)
        slot = locator(1)
        upload = locator(1)
        # The real site clears files during onChange, so reading the input afterwards is empty.
        upload.evaluate.return_value = ""
        selection = MagicMock()
        selection.evaluate = AsyncMock(return_value="portrait.jpg")
        selection.dispose = AsyncMock()
        upload.evaluate_handle = AsyncMock(return_value=selection)
        cover.locator.return_value = slot
        slot.locator.return_value = upload
        video._select_cover_tab = AsyncMock()
        video._loaded_cover_previews = AsyncMock(return_value={"old-preview"})
        video._wait_for_cover_preview = AsyncMock(return_value={"new-preview"})
        previews = await video._upload_custom_cover(page, cover, "设置竖封面", "portrait", "/tmp/portrait.jpg")
        self.assertEqual(previews, {"new-preview"})
        video._select_cover_tab.assert_awaited_once_with(page, cover, "设置竖封面")
        cover.locator.assert_called_once_with(COVER_UPLOAD_SELECTOR)
        slot.locator.assert_called_once_with("input.semi-upload-hidden-input")
        upload.set_input_files.assert_awaited_once_with("/tmp/portrait.jpg")
        upload.evaluate.assert_not_awaited()
        selection.evaluate.assert_any_await("state => state.name")
        selection.evaluate.assert_any_await("state => state.cleanup()")
        selection.dispose.assert_awaited_once()
        video._wait_for_cover_preview.assert_awaited_once_with(page, cover, "portrait", baseline={"old-preview"})

    async def test_capture_listener_is_cleaned_up_when_file_selection_fails(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        cover, slot, upload = locator(1), locator(1), locator(1)
        cover.locator.return_value = slot
        slot.locator.return_value = upload
        selection = MagicMock()
        selection.evaluate = AsyncMock()
        selection.dispose = AsyncMock()
        upload.evaluate_handle = AsyncMock(return_value=selection)
        upload.set_input_files.side_effect = OSError("selection failed")
        video._select_cover_tab = AsyncMock()
        video._loaded_cover_previews = AsyncMock(return_value=set())
        with self.assertRaisesRegex(OSError, "selection failed"):
            await video._upload_custom_cover(page, cover, "设置竖封面", "portrait", "/tmp/portrait.jpg")
        selection.evaluate.assert_awaited_once_with("state => state.cleanup()")
        selection.dispose.assert_awaited_once()

    async def test_missing_or_ambiguous_real_slot_never_uses_ai_reference_input(self):
        for count in [0, 2]:
            with self.subTest(count=count):
                video = self.video(portrait="/tmp/portrait.jpg")
                page = self.page()
                cover = locator(1)
                slot = locator(count)
                cover.locator.return_value = slot
                video._select_cover_tab = AsyncMock()
                with self.assertRaisesRegex(DouyinCustomCoverError, "唯一"):
                    await video._upload_custom_cover(page, cover, "设置竖封面", "portrait", "/tmp/portrait.jpg")
                cover.locator.assert_called_once_with(COVER_UPLOAD_SELECTOR)
                slot.locator.assert_not_called()

    async def test_tab_click_without_active_evidence_fails(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        cover = locator(1)
        tab = locator(1)
        tab.evaluate.return_value = False
        cover.get_by_text.return_value = tab
        with patch.object(douyin_main, "_native_click", AsyncMock(return_value=True)):
            with self.assertRaisesRegex(DouyinCustomCoverError, "已激活"):
                await video._select_cover_tab(page, cover, "设置竖封面")
        self.assertEqual(tab.click.await_count, 3)

    async def test_only_unique_main_lower_canvas_supplies_preview_evidence(self):
        video = self.video()
        cover, canvas = locator(1), locator(1)
        cover.locator.return_value = canvas
        canvas.evaluate.return_value = "canvas:596x341:12345"
        self.assertEqual(await video._loaded_cover_previews(cover, "portrait"), {"canvas:596x341:12345"})
        cover.locator.assert_called_once_with(COVER_PREVIEW_SELECTOR)
        cover.evaluate.assert_not_awaited()

    async def test_gallery_or_phone_previews_cannot_replace_missing_main_canvas(self):
        video = self.video()
        cover = locator(1)
        cover.locator.return_value = locator()
        cover.evaluate.return_value = ["gallery-image", "phone-preview"]
        self.assertEqual(await video._loaded_cover_previews(cover, "portrait"), set())
        cover.evaluate.assert_not_awaited()

    async def test_ambiguous_main_canvases_fail_closed(self):
        video = self.video()
        cover = locator(1)
        cover.locator.return_value = locator(2)
        with self.assertRaisesRegex(DouyinCustomCoverError, "画布不唯一"):
            await video._loaded_cover_previews(cover, "portrait")

    async def test_empty_or_unreadable_main_canvas_is_not_preview_evidence(self):
        video = self.video()
        cover, canvas = locator(1), locator(1)
        cover.locator.return_value = canvas
        canvas.evaluate.return_value = ""
        self.assertEqual(await video._loaded_cover_previews(cover, "portrait"), set())

    def test_canvas_samples_tolerate_redraw_but_reject_different_artwork(self):
        def signature(values, size="596x341"):
            return "canvas-rgb32:" + size + ":" + base64.b64encode(bytes(values)).decode("ascii")
        original = [80, 130, 180] * (32 * 32)
        redraw = [81, 129, 182] * (32 * 32)
        replacement = [130, 110, 80] * (32 * 32)
        self.assertTrue(douyin_main._same_cover_preview(signature(original), signature(redraw)))
        self.assertFalse(douyin_main._same_cover_preview(signature(original), signature(replacement)))
        self.assertFalse(douyin_main._same_cover_preview(signature(original), signature(original, "597x341")))
        self.assertFalse(douyin_main._same_cover_preview(signature(original), "canvas-rgb32:596x341:invalid"))

    async def test_preview_requires_two_consecutive_stable_samples(self):
        video = self.video()
        page = self.page()
        cover = locator(1)
        cover.get_by_text.return_value = locator()
        video._loaded_cover_previews = AsyncMock(side_effect=[{"drawing-in-progress"}, {"finished-artwork"}, {"finished-artwork"}])
        video._cover_finish_enabled = AsyncMock(return_value=True)
        result = await video._wait_for_cover_preview(page, cover, "portrait", baseline={"original-frame"})
        self.assertEqual(result, {"finished-artwork"})
        self.assertEqual(page.wait_for_timeout.await_count, 2)

    async def test_unchanged_preview_is_not_accepted_as_custom_upload(self):
        video = self.video()
        page = self.page()
        cover = locator(1)
        cover.get_by_text.return_value = locator()
        video._loaded_cover_previews = AsyncMock(return_value={"existing-video-frame"})
        video._cover_finish_enabled = AsyncMock(return_value=True)
        with self.assertRaisesRegex(DouyinCustomCoverError, "预览"):
            await video._wait_for_cover_preview(page, cover, "portrait", baseline={"existing-video-frame"})
        self.assertEqual(page.wait_for_timeout.await_count, 60)

    async def test_switching_back_must_retain_each_requested_preview(self):
        video = self.video()
        page = self.page()
        cover = locator(1)
        cover.get_by_text.return_value = locator()
        video._loaded_cover_previews = AsyncMock(return_value={"other-tab-preview"})
        video._cover_finish_enabled = AsyncMock(return_value=True)
        with self.assertRaises(DouyinCustomCoverError):
            await video._wait_for_cover_preview(page, cover, "portrait", expected={"requested-preview"})

    async def test_loaded_preview_still_requires_enabled_finish_button(self):
        video = self.video()
        page = self.page()
        cover = locator(1)
        cover.get_by_text.return_value = locator()
        video._loaded_cover_previews = AsyncMock(return_value={"new-preview"})
        video._cover_finish_enabled = AsyncMock(return_value=False)
        with self.assertRaisesRegex(DouyinCustomCoverError, "完成按钮"):
            await video._wait_for_cover_preview(page, cover, "portrait", baseline=set())

    async def test_cannot_escape_editor_and_claim_cover_was_saved(self):
        video = self.video()
        page = self.page()
        cover = locator(1)
        cover.wait_for.side_effect = TimeoutError("still open")
        finish = locator(1)
        cover.get_by_role.return_value = finish
        video._cover_finish_enabled = AsyncMock(return_value=True)
        with patch.object(douyin_main, "_native_click", AsyncMock(return_value=True)):
            with self.assertRaisesRegex(DouyinCustomCoverError, "保存关闭"):
                await video._save_custom_covers(page, cover)
        page.keyboard.press.assert_not_awaited()

    async def test_second_cover_failure_prevents_save_and_verification(self):
        video = self.video("/tmp/landscape.jpg", "/tmp/portrait.jpg")
        page = self.page()
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "original-frame", "loaded": True})
        video._wait_for_saved_covers = AsyncMock(return_value={})
        video._open_cover_editor = AsyncMock(return_value=object())
        video._upload_custom_cover = AsyncMock(side_effect=[{"portrait"}, OSError("image upload failed")])
        video._save_custom_covers = AsyncMock()
        with self.assertRaisesRegex(DouyinCustomCoverError, "image upload failed"):
            await video.set_thumbnail(page)
        self.assertFalse(video._custom_covers_verified)
        video._save_custom_covers.assert_not_awaited()

    async def test_save_failure_stops_upload_before_publish_or_cookie_write(self):
        video = self.video("/tmp/landscape.jpg", "/tmp/portrait.jpg")
        video.validate_upload_args = AsyncMock()
        video.fill_title_and_description = AsyncMock()
        video.apply_self_declaration = AsyncMock()
        video.apply_collection = AsyncMock()
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "original-frame", "loaded": True})
        video._wait_for_saved_covers = AsyncMock(return_value={})
        video._open_cover_editor = AsyncMock(return_value=object())
        video._upload_custom_cover = AsyncMock(return_value={"custom-preview"})
        video._select_cover_tab = AsyncMock()
        video._wait_for_cover_preview = AsyncMock()
        video._save_custom_covers = AsyncMock(side_effect=DouyinCustomCoverError("封面保存失败"))
        page = self.page()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.locator.return_value = locator(1)
        publish_button = locator(1)
        page.get_by_role.return_value = publish_button
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        context.storage_state = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        with (
            patch.object(douyin_main, "set_init_script", AsyncMock(return_value=context)),
            patch.object(douyin_main.asyncio, "sleep", AsyncMock()),
            self.assertRaisesRegex(DouyinCustomCoverError, "封面保存失败"),
        ):
            await video.upload(playwright)
        publish_button.click.assert_not_awaited()
        context.storage_state.assert_not_awaited()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_saved_cover_read_is_scoped_to_main_form_orientation(self):
        video = self.video()
        page = self.page()
        card, image = locator(1), locator(1)
        page.locator.return_value = card
        card.locator.return_value = image
        image.evaluate.return_value = {"src": "saved-portrait", "loaded": True, "width": 1080, "height": 1440}
        result = await video._saved_cover_snapshot(page, "portrait")
        self.assertEqual(result["src"], "saved-portrait")
        page.locator.assert_called_once_with('[class^="coverControl-"]')
        card.filter.assert_called_once_with(has_text="竖封面3:4")
        card.locator.assert_called_once_with("img")

    async def test_saved_cover_waits_for_both_changed_and_loaded_form_images(self):
        video = self.video()
        page = self.page()
        video._saved_cover_snapshot = AsyncMock(side_effect=[
            {"src": "new-portrait", "loaded": True}, {"src": "new-landscape", "loaded": False},
            {"src": "new-portrait", "loaded": True}, {"src": "new-landscape", "loaded": True},
        ])
        result = await video._wait_for_saved_covers(page, {"portrait": "old-frame", "landscape": "old-frame"})
        self.assertEqual(result, {"portrait": "new-portrait", "landscape": "new-landscape"})
        page.wait_for_timeout.assert_awaited_once_with(500)

    async def test_editor_close_with_unchanged_form_images_is_not_saved(self):
        video = self.video()
        page = self.page()
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "original-frame", "loaded": True})
        with self.assertRaisesRegex(DouyinCustomCoverError, "60 秒"):
            await video._wait_for_saved_covers(page, {"portrait": "original-frame", "landscape": "original-frame"})
        self.assertEqual(page.wait_for_timeout.await_count, 120)

    async def test_saved_cover_must_still_match_at_publish_time(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        video._custom_covers_verified = True
        video._verified_cover_sources = {"portrait": "saved-portrait"}
        video._saved_cover_snapshot = AsyncMock(return_value={"src": "unrequested-image", "loaded": True})
        with self.assertRaisesRegex(DouyinCustomCoverError, "发生变化"):
            await video._assert_custom_covers_ready(page)

    async def test_unverified_custom_cover_state_blocks_publish(self):
        video = self.video(portrait="/tmp/portrait.jpg")
        page = self.page()
        with self.assertRaisesRegex(DouyinCustomCoverError, "尚未验证"):
            await video._assert_custom_covers_ready(page)
        page.get_by_role.assert_not_called()


if __name__ == "__main__":
    unittest.main()
