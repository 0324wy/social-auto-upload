import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import uploader.xiaohongshu_uploader.main as xhs_main


def make_async_locator(name, *, count=1, visible=True):
    locator = MagicMock(name=name)
    locator.first = locator
    locator.count = AsyncMock(return_value=count)
    locator.is_visible = AsyncMock(return_value=visible)
    locator.wait_for = AsyncMock()
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.hover = AsyncMock()
    locator.click = AsyncMock()
    locator.set_input_files = AsyncMock()
    locator.element_handle = AsyncMock(return_value=object())
    locator.get_attribute = AsyncMock(return_value=None)
    return locator


class FakeLocator:
    def __init__(self, name, count=0, src=None, children=None):
        self.name = name
        self._count = count
        self._src = src
        self._children = children or {}

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self._children.get(selector, FakeLocator(selector))

    def get_by_text(self, text, exact=False):
        return self._children.get(f"text:{text}", FakeLocator(text))

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    async def count(self):
        return self._count

    async def wait_for(self, **kwargs):
        return None

    async def get_attribute(self, name):
        if name == "src":
            return self._src
        return None

    async def fill(self, value):
        return None

    async def click(self):
        return None


class RecordingKeyboard:
    def __init__(self):
        self.actions = []

    async def press(self, key):
        self.actions.append(("press", key))

    async def type(self, text, delay=None):
        self.actions.append(("type", text, delay))


class RecordingLocator(FakeLocator):
    def __init__(self, name):
        super().__init__(name, count=1)
        self.actions = []

    async def fill(self, value):
        self.actions.append(("fill", value))

    async def click(self):
        self.actions.append(("click",))

    async def wait_for(self, **kwargs):
        self.actions.append(("wait_for", kwargs))


class RecordingPage:
    def __init__(self):
        self.keyboard = RecordingKeyboard()
        self.locators = {
            'input[placeholder*="填写标题"]': RecordingLocator("title"),
            'p[data-placeholder*="输入正文描述"]': RecordingLocator("desc"),
            '#creator-editor-topic-container': RecordingLocator("topic-container"),
            '#creator-editor-topic-container .item': RecordingLocator("topic-item"),
        }

    def locator(self, selector):
        return self.locators[selector]


class XiaohongshuUploaderTests(unittest.TestCase):
    def test_creator_urls_keep_xiaohongshu_domain_by_default(self):
        with patch.dict(os.environ, {"SAU_XHS_CREATOR_BASE_URL": ""}):
            self.assertEqual(
                xhs_main._build_xhs_creator_url("/login"),
                "https://creator.xiaohongshu.com/login",
            )

    def test_creator_urls_use_configured_rednote_domain(self):
        with patch.dict(
            os.environ,
            {"SAU_XHS_CREATOR_BASE_URL": "https://creator.rednote.com/"},
        ):
            self.assertEqual(
                xhs_main._build_xhs_creator_url("/login"),
                "https://creator.rednote.com/login",
            )
            self.assertEqual(
                xhs_main._build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                ),
                "https://creator.rednote.com/publish/publish?from=homepage&target=video",
            )

    def test_find_xhs_qrcode_locator_prefers_scan_sibling_inside_login_box(self):
        qrcode_locator = FakeLocator("qrcode", count=1, src="data:image/png;base64,abc")
        scan_text_locator = FakeLocator(
            "scan-text",
            count=1,
            children={
                "xpath=..//following-sibling::div//img": qrcode_locator,
            },
        )
        login_box_locator = FakeLocator(
            "login-box",
            count=1,
            children={
                "div:has-text('扫一扫')": scan_text_locator,
                "text:APP扫一扫登录": scan_text_locator,
            },
        )
        page = FakeLocator(
            "page",
            children={
                "div[class*='login-box']": login_box_locator,
                ".login-box-container": login_box_locator,
            },
        )

        locator = asyncio.run(xhs_main._find_xhs_qrcode_locator(page))
        self.assertIs(locator, qrcode_locator)

    def test_setup_returns_detail_when_cookie_invalid_without_handle(self):
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            result = asyncio.run(
                xhs_main.xiaohongshu_setup(
                    "missing.json",
                    handle=False,
                    return_detail=True,
                )
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "cookie_invalid")

    def test_setup_uses_login_flow_when_handle_is_true(self):
        login_result = {
            "success": True,
            "status": "success",
            "message": "ok",
            "account_file": "account.json",
            "qrcode": {"image_path": "qrcode.png"},
            "current_url": "https://creator.xiaohongshu.com/",
        }
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            with patch(
                "uploader.xiaohongshu_uploader.main.xiaohongshu_cookie_gen",
                new=AsyncMock(return_value=login_result),
            ) as mock_login:
                result = asyncio.run(
                    xhs_main.xiaohongshu_setup(
                        "account.json",
                        handle=True,
                        return_detail=True,
                    )
                )
        self.assertTrue(result["success"])
        mock_login.assert_awaited_once()

    def test_video_validate_upload_args_normalizes_video_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            thumbnail_path = Path(tmp_dir) / "demo.png"
            cookie_path = Path(tmp_dir) / "account.json"
            video_path.write_bytes(b"video")
            thumbnail_path.write_bytes(b"image")
            cookie_path.write_text("{}")

            app = xhs_main.XiaoHongShuVideo(
                title="demo",
                file_path=str(video_path),
                tags=["xhs"],
                publish_date=0,
                account_file=str(cookie_path),
                thumbnail_path=str(thumbnail_path),
            )

            with patch(
                "uploader.xiaohongshu_uploader.main.cookie_auth",
                new=AsyncMock(return_value=True),
            ):
                asyncio.run(app.validate_upload_args())

        self.assertTrue(app.file_path.endswith("demo.mp4"))
        self.assertTrue(app.thumbnail_path.endswith("demo.png"))

    def test_wait_for_cover_entry_prefers_current_edit_control(self):
        edit_entry = make_async_locator("edit-entry")
        missing = make_async_locator("missing", count=0, visible=False)
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            edit_entry
            if selector == "div.cover-plugin-preview div.cover-edit-entry"
            else missing
        )

        result = asyncio.run(xhs_main._wait_for_cover_entry(page))

        self.assertIs(result, edit_entry)

    def test_wait_for_cover_entry_supports_empty_cover_control(self):
        upload_entry = make_async_locator("upload-entry")
        missing = make_async_locator("missing", count=0, visible=False)
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            upload_entry
            if selector == "div.cover-plugin-preview div.upload-cover"
            else missing
        )

        result = asyncio.run(xhs_main._wait_for_cover_entry(page))

        self.assertIs(result, upload_entry)

    def test_wait_for_cover_entry_dismisses_pk_cover_guide(self):
        edit_entry = make_async_locator("edit-entry")
        guide = make_async_locator("guide")
        missing = make_async_locator("missing", count=0, visible=False)
        page = MagicMock()
        page.get_by_text.return_value = guide
        page.wait_for_timeout = AsyncMock()
        page.locator.side_effect = lambda selector: (
            edit_entry
            if selector == "div.cover-plugin-preview div.cover-edit-entry"
            else missing
        )

        result = asyncio.run(xhs_main._wait_for_cover_entry(page))

        self.assertIs(result, edit_entry)
        guide.click.assert_awaited_once_with(force=True)

    def test_wait_for_cover_entry_hovers_ai_cover_surface(self):
        edit_entry = make_async_locator("edit-entry")
        edit_entry.is_visible = AsyncMock(side_effect=[False, True])
        surface = make_async_locator("cover-surface")
        missing = make_async_locator("missing", count=0, visible=False)
        guide = make_async_locator("guide", count=0, visible=False)
        page = MagicMock()
        page.get_by_text.return_value = guide
        page.wait_for_timeout = AsyncMock()

        def locate(selector):
            if selector == "div.cover-plugin-preview div.cover-edit-entry":
                return edit_entry
            if selector == "div.cover-plugin-preview div.default--ai-cover-layout":
                return surface
            return missing

        page.locator.side_effect = locate

        result = asyncio.run(xhs_main._wait_for_cover_entry(page))

        self.assertIs(result, edit_entry)
        surface.hover.assert_awaited_once()

    def test_set_thumbnail_uses_current_direct_upload_control(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        entry = make_async_locator("entry")
        modal = make_async_locator("modal")
        file_input = make_async_locator("file-input")
        preview = make_async_locator("preview")
        uploaded_thumbnail = make_async_locator("uploaded-thumbnail")
        inactive_mask = make_async_locator("inactive-mask")
        uploaded_thumbnail.locator.return_value = inactive_mask
        complete = make_async_locator("complete")
        current_surface = make_async_locator("current-surface")
        current_surface.get_attribute.return_value = "background-image: url(old-cover)"
        evaluating = make_async_locator("evaluating")

        def locate_in_modal(selector):
            if selector == xhs_main.COVER_FILE_INPUT_SELECTOR:
                return file_input
            if selector == xhs_main.COVER_UPLOADED_THUMBNAIL_SELECTOR:
                return uploaded_thumbnail
            return preview

        modal.locator.side_effect = locate_in_modal
        modal.get_by_role.return_value = complete

        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            current_surface
            if selector == xhs_main.COVER_CURRENT_SURFACE_SELECTOR
            else modal
        )
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.get_by_text.return_value = evaluating

        with patch.object(
            xhs_main,
            "_wait_for_cover_entry",
            new=AsyncMock(return_value=entry),
        ):
            asyncio.run(app.set_thumbnail(page, "cover.jpg"))

        page.locator.assert_any_call(xhs_main.COVER_CURRENT_SURFACE_SELECTOR)
        page.locator.assert_any_call(xhs_main.COVER_MODAL_SELECTOR)
        modal.get_by_text.assert_not_called()
        modal.locator.assert_any_call(xhs_main.COVER_FILE_INPUT_SELECTOR)
        modal.locator.assert_any_call(xhs_main.COVER_PREVIEW_SELECTOR)
        modal.locator.assert_any_call(xhs_main.COVER_UPLOADED_THUMBNAIL_SELECTOR)
        modal.get_by_role.assert_called_once_with(
            "button", name="完成", exact=True
        )
        file_input.set_input_files.assert_awaited_once_with("cover.jpg")
        uploaded_thumbnail.click.assert_awaited_once_with(force=True)
        inactive_mask.wait_for.assert_awaited_once_with(
            state="hidden", timeout=xhs_main.COVER_IMAGE_TIMEOUT_MS
        )
        self.assertEqual(page.wait_for_function.await_count, 3)
        evaluating.wait_for.assert_awaited_once_with(
            state="hidden", timeout=xhs_main.COVER_IMAGE_TIMEOUT_MS
        )
        complete.click.assert_awaited_once_with(force=True)
        self.assertEqual(
            modal.wait_for.await_args_list,
            [
                unittest.mock.call(
                    state="visible", timeout=xhs_main.COVER_MODAL_TIMEOUT_MS
                ),
                unittest.mock.call(
                    state="hidden", timeout=xhs_main.COVER_MODAL_TIMEOUT_MS
                ),
            ],
        )

    def test_set_thumbnail_keeps_legacy_upload_tab_and_confirm_button(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        entry = make_async_locator("entry")
        modal = make_async_locator("modal")
        upload_tab = make_async_locator("upload-tab")
        file_input = make_async_locator("file-input")
        file_input.wait_for = AsyncMock(
            side_effect=[TimeoutError("current editor input missing"), None]
        )
        preview = make_async_locator("preview")
        uploaded_thumbnail = make_async_locator(
            "missing-uploaded-thumbnail", count=0
        )
        missing_complete = make_async_locator("missing-complete", count=0)
        confirm = make_async_locator("confirm")
        modal.get_by_text.return_value = upload_tab

        def locate_in_modal(selector):
            if selector == xhs_main.COVER_FILE_INPUT_SELECTOR:
                return file_input
            if selector == xhs_main.COVER_UPLOADED_THUMBNAIL_SELECTOR:
                return uploaded_thumbnail
            return preview

        modal.locator.side_effect = locate_in_modal
        modal.get_by_role.side_effect = lambda _role, *, name, exact: (
            missing_complete if name == "完成" else confirm
        )

        page = MagicMock()
        missing_surface = make_async_locator("missing-surface", count=0)
        page.locator.side_effect = lambda selector: (
            missing_surface
            if selector == xhs_main.COVER_CURRENT_SURFACE_SELECTOR
            else modal
        )
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with patch.object(
            xhs_main,
            "_wait_for_cover_entry",
            new=AsyncMock(return_value=entry),
        ):
            asyncio.run(app.set_thumbnail(page, "cover.jpg"))

        modal.get_by_text.assert_called_once_with("上传封面", exact=True)
        upload_tab.click.assert_awaited_once_with(force=True)
        confirm.click.assert_awaited_once_with(force=True)

    def test_set_thumbnail_stops_when_uploaded_cover_is_not_activated(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        entry = make_async_locator("entry")
        modal = make_async_locator("modal")
        file_input = make_async_locator("file-input")
        preview = make_async_locator("preview")
        uploaded_thumbnail = make_async_locator("uploaded-thumbnail")
        inactive_mask = make_async_locator("inactive-mask")
        inactive_mask.wait_for.side_effect = TimeoutError("still inactive")
        uploaded_thumbnail.locator.return_value = inactive_mask
        complete = make_async_locator("complete")
        current_surface = make_async_locator("current-surface")
        current_surface.get_attribute.return_value = "background-image: url(old-cover)"

        def locate_in_modal(selector):
            if selector == xhs_main.COVER_FILE_INPUT_SELECTOR:
                return file_input
            if selector == xhs_main.COVER_UPLOADED_THUMBNAIL_SELECTOR:
                return uploaded_thumbnail
            return preview

        modal.locator.side_effect = locate_in_modal
        modal.get_by_role.return_value = complete
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            current_surface
            if selector == xhs_main.COVER_CURRENT_SURFACE_SELECTOR
            else modal
        )
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.keyboard.press = AsyncMock()

        with (
            patch.object(
                xhs_main,
                "_wait_for_cover_entry",
                new=AsyncMock(return_value=entry),
            ),
            self.assertRaisesRegex(RuntimeError, "自定义封面设置失败"),
        ):
            asyncio.run(app.set_thumbnail(page, "cover.jpg"))

        uploaded_thumbnail.click.assert_awaited_once_with(force=True)
        complete.click.assert_not_awaited()

    def test_set_thumbnail_failure_stops_publish(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        page = MagicMock()
        page.keyboard.press = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        with (
            patch.object(
                xhs_main,
                "_wait_for_cover_entry",
                new=AsyncMock(side_effect=TimeoutError("missing")),
            ),
            self.assertRaisesRegex(RuntimeError, "自定义封面设置失败"),
        ):
            asyncio.run(app.set_thumbnail(page, "cover.jpg"))

        page.keyboard.press.assert_awaited_once_with("Escape")

    def test_thumbnail_error_prevents_clicking_publish(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
            thumbnail_path="cover.jpg",
        )
        upload_file = make_async_locator("upload-file")
        upload_input = MagicMock()
        preview = MagicMock()
        preview.inner_text = AsyncMock(return_value="上传成功")
        upload_input.query_selector = AsyncMock(return_value=preview)

        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.locator.return_value = upload_file
        page.wait_for_selector = AsyncMock(return_value=upload_input)
        app.fill_meta = AsyncMock()
        app.set_thumbnail = AsyncMock(side_effect=RuntimeError("cover failed"))

        with self.assertRaisesRegex(RuntimeError, "cover failed"):
            asyncio.run(app.upload_video_content(page))

        self.assertNotIn(
            unittest.mock.call('button:has-text("发布")'),
            page.locator.call_args_list,
        )

    def test_note_uploader_exists_and_validates_required_fields(self):
        note_cls = getattr(xhs_main, "XiaoHongShuNote")
        app = note_cls(
            image_paths=[],
            note="",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )

        with patch.object(app, "validate_base_args", new=AsyncMock(return_value=None)):
            with self.assertRaises(ValueError):
                asyncio.run(app.validate_upload_args())

    def test_video_fill_meta_uses_desc_then_first_tag(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
            desc="描述内容",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['input[placeholder*="填写标题"]'].actions,
            [("fill", "标题内容")],
        )
        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertIn(("type", "描述内容", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)
        self.assertEqual(
            page.locators['#creator-editor-topic-container .item'].actions,
            [("wait_for", {"state": "visible", "timeout": 4000}), ("click",)],
        )

    def test_video_fill_meta_can_fill_first_tag_without_desc(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertNotIn(("type", "", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)

    def test_missing_repost_source_skips_declaration_controls(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        page = MagicMock()

        asyncio.run(app.check_original_declaration(page))

        page.get_by_text.assert_not_called()

    def test_context_close_failure_still_closes_browser(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        app.validate_upload_args = AsyncMock()
        app.upload_video_content = AsyncMock(side_effect=RuntimeError("upload failed"))

        page = MagicMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock(side_effect=OSError("close failed"))
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        with (
            patch.object(xhs_main, "set_init_script", AsyncMock(return_value=context)),
            self.assertRaisesRegex(RuntimeError, "upload failed"),
        ):
            asyncio.run(app.upload(playwright))

        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    def test_upload_timeout_closes_browser_resources(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        app.validate_upload_args = AsyncMock()

        locator = MagicMock()
        locator.first = locator
        locator.set_input_files = AsyncMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.locator.return_value = locator

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock(side_effect=OSError("close failed"))
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        with (
            patch.object(xhs_main, "set_init_script", AsyncMock(return_value=context)),
            patch.object(xhs_main, "monotonic", side_effect=[0, 901]),
            self.assertRaisesRegex(TimeoutError, "15 分钟"),
        ):
            asyncio.run(app.upload(playwright))

        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    def test_note_title_defaults_do_not_override_explicit_title(self):
        app = xhs_main.XiaoHongShuNote(
            image_paths=["a.png"],
            note="正文",
            tags=[],
            publish_date=0,
            account_file="account.json",
            title="显式标题",
            desc="图文正文",
        )

        self.assertEqual(app.title, "显式标题")
        self.assertEqual(app.desc, "图文正文")


if __name__ == "__main__":
    unittest.main()
