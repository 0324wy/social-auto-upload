# -*- coding: utf-8 -*-
"""YouTube Studio uploads with explicit, upload-specific publication receipts."""
import asyncio
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from patchright.async_api import Page, Playwright, async_playwright

from conf import DEBUG_MODE
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.log import youtube_logger

try:
    from conf import YT_PROXY
except ImportError:
    YT_PROXY = None

STUDIO_URL = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"
VISIBILITY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
UPLOAD_DIALOG = "ytcp-uploads-dialog"
THUMBNAIL_UPLOADER = "ytcp-thumbnail-uploader"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _browser_options(*, headless: bool, proxy=None) -> dict:
    options = {"headless": headless, "channel": "chrome"}
    configured = proxy if proxy is not None else os.environ.get("SAU_YOUTUBE_PROXY") or YT_PROXY
    if configured:
        if isinstance(configured, dict):
            options["proxy"] = dict(configured)
        else:
            parsed = urlsplit(configured)
            if parsed.username or parsed.password:
                from urllib.parse import unquote
                host = parsed.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                server = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
                options["proxy"] = {"server": server, "username": unquote(parsed.username or ""), "password": unquote(parsed.password or "")}
            else:
                options["proxy"] = {"server": str(configured)}
    return options


async def _new_context(browser, *, headless: bool, account_file=None):
    options = {"locale": "en-US"}
    if account_file is not None:
        options["storage_state"] = str(account_file)
    if headless:
        probe = await browser.new_page()
        try:
            native_ua = await probe.evaluate("navigator.userAgent")
            if not isinstance(native_ua, str) or not native_ua:
                raise RuntimeError("无法读取当前 Chrome 浏览器版本")
            options["user_agent"] = native_ua.replace("HeadlessChrome/", "Chrome/")
        finally:
            await probe.close()
    context = await browser.new_context(**options)
    try:
        return await set_init_script(context)
    except Exception:
        await context.close()
        raise


async def _close_resources(context, browser) -> None:
    for resource in (context, browser):
        if resource is not None:
            try:
                await resource.close()
            except Exception:
                youtube_logger.warning("YouTube 浏览器资源关闭失败")


def _build_login_result(success, status, message, account_file, current_url=""):
    return {"success": success, "status": status, "message": message,
            "account_file": str(account_file), "current_url": current_url}


async def _skip_unsupported_browser_notice(page: Page) -> None:
    for text in ["SKIP TO YOUTUBE STUDIO", "Skip to YouTube Studio", "跳过并前往 YouTube 工作室"]:
        link = page.get_by_text(text, exact=True).first
        if await link.count() and await link.is_visible():
            await link.click(timeout=5000)
            await page.wait_for_timeout(1000)
            return


async def _is_authenticated_studio(page: Page) -> bool:
    await _skip_unsupported_browser_notice(page)
    parsed = urlsplit(page.url)
    if parsed.hostname != "studio.youtube.com" or not parsed.path.startswith("/channel/"):
        return False
    # A channel URL alone can survive while a challenge or sign-in screen is displayed.
    navigation = page.locator("ytcp-navigation-drawer, ytcp-channel-dashboard, ytcp-entity-page").first
    return bool(await navigation.count() and await navigation.is_visible())


async def cookie_auth(account_file, *, headless: bool = True, proxy=None) -> bool:
    if not Path(account_file).is_file():
        return False
    browser = context = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**_browser_options(headless=headless, proxy=proxy))
                context = await _new_context(browser, headless=headless, account_file=account_file)
                page = await context.new_page()
                await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
                for _ in range(15):
                    if await _is_authenticated_studio(page):
                        return True
                    if urlsplit(page.url).hostname == "accounts.google.com":
                        return False
                    await page.wait_for_timeout(1000)
                return False
            finally:
                await _close_resources(context, browser)
    except Exception:
        return False


async def youtube_cookie_gen(account_file, headless: bool = False, *, proxy=None):
    browser = context = None
    async with async_playwright() as playwright:
        try:
            # Interactive Google login always needs a visible window.
            browser = await playwright.chromium.launch(**_browser_options(headless=False, proxy=proxy))
            context = await _new_context(browser, headless=False)
            page = await context.new_page()
            await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
            youtube_logger.info(_msg("🔐", "请在浏览器中登录 Google / YouTube；进入频道工作室后保存登录态"))
            for _ in range(600):
                if await _is_authenticated_studio(page):
                    await page.wait_for_timeout(1500)
                    if await _is_authenticated_studio(page):
                        Path(account_file).parent.mkdir(parents=True, exist_ok=True)
                        await context.storage_state(path=str(account_file))
                        return _build_login_result(True, "logged_in", "登录成功", account_file, page.url)
                await page.wait_for_timeout(1000)
            return _build_login_result(False, "timeout", "等待 YouTube 登录超时，未保存登录态", account_file, page.url)
        finally:
            await _close_resources(context, browser)


async def youtube_setup(account_file, handle: bool = False, return_detail: bool = False,
                        headless: bool = False, proxy=None):
    if await cookie_auth(account_file, headless=headless, proxy=proxy):
        result = _build_login_result(True, "cookie_valid", "登录态有效", account_file)
    elif handle:
        result = await youtube_cookie_gen(account_file, headless=headless, proxy=proxy)
    else:
        result = _build_login_result(False, "cookie_invalid", "登录态不存在或已失效", account_file)
    return result if return_detail else result["success"]


def classify_content_kind(width, height, duration, rotation=0) -> str:
    """Eligibility for new uploads; no video is cropped or transcoded.

    https://support.google.com/youtube/answer/15424877
    """
    try:
        width, height, duration = float(width), float(height), float(duration)
        if not all(math.isfinite(value) and value > 0 for value in (width, height, duration)):
            return "unknown"
        if int(rotation) % 180:
            width, height = height, width
        return "shorts" if height >= width and duration <= 180 else "video"
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def probe_content_kind(file_path) -> str:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of", "json", str(file_path),
        ], check=True, capture_output=True, text=True, timeout=20)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        rotation = next((item["rotation"] for item in stream.get("side_data_list", []) if "rotation" in item),
                        stream.get("tags", {}).get("rotate", 0))
        return classify_content_kind(stream.get("width"), stream.get("height"),
                                     data.get("format", {}).get("duration") or stream.get("duration"), rotation)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return "unknown"


def _video_id_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/shorts/", "/embed/")):
                candidate = parsed.path.split("/")[2]
            else:
                return None
        elif host == "studio.youtube.com" and parsed.path.startswith("/video/"):
            candidate = parsed.path.split("/")[2]
        else:
            return None
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
    except (TypeError, ValueError, IndexError):
        return None


async def _dismiss_autocomplete(page: Page):
    await page.evaluate("() => { const el = document.activeElement; if (el && el.blur) el.blur(); }")
    dropdown = page.locator("tp-yt-iron-dropdown:visible").first
    if await dropdown.count() and await dropdown.is_visible():
        await page.keyboard.press("Escape")


async def _fill_editable(page: Page, selector: str, text: str):
    box = page.locator(selector).first
    await box.wait_for(state="visible", timeout=30000)
    await box.fill(text)
    if (await box.inner_text()).strip() != text.strip():
        raise RuntimeError("YouTube 标题或简介未按请求写入")
    await _dismiss_autocomplete(page)


async def _click_if_present(page: Page, selector: str, timeout: int = 4000) -> bool:
    try:
        element = page.locator(selector).first
        await element.wait_for(state="visible", timeout=timeout)
        await element.click()
        return True
    except Exception:
        return False


async def _is_checked(element) -> bool:
    return bool(await element.evaluate("el => el.checked === true || el.hasAttribute('checked') || el.getAttribute('aria-checked') === 'true'"))


async def _select_radio(page, selector: str, description: str) -> None:
    radio = page.locator(selector).first
    await radio.wait_for(state="visible", timeout=15000)
    if not await _is_checked(radio):
        await radio.click()
    if not await _is_checked(radio):
        raise RuntimeError(f"YouTube {description}未按请求选中，停止发布")


async def _wait_upload_complete(page: Page, max_polls: int = 360) -> bool:
    # Absence of a progress widget is not proof of completion.
    for _ in range(max_polls):
        progress = page.locator(f"{UPLOAD_DIALOG} .progress-label, {UPLOAD_DIALOG} ytcp-video-upload-progress").first
        if await progress.count():
            text = (await progress.inner_text()).strip()
            if re.search(r"upload failed|processing abandoned|上传失败|处理失败|无法处理", text, re.I):
                raise RuntimeError("YouTube 视频上传或处理失败")
            # Footers can show a processing label while bytes are still uploading.
            percentages = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)]
            uploading = bool(re.search(r"\buploading\b|正在上传|上传中", text, re.I))
            if (percentages and min(percentages) < 100) or (uploading and not percentages):
                await page.wait_for_timeout(5000)
                continue
            if re.search(r"upload complete|uploaded|processing|checks complete|finished|上传完成|已上传|正在处理|处理完毕|检查完成", text, re.I):
                return True
        await page.wait_for_timeout(5000)
    raise TimeoutError("等待 YouTube 上传完成超时（30 分钟），停止发布")


async def _scoped_video_ids(scope) -> set[str]:
    links = await scope.locator("a[href]").evaluate_all("links => links.map(link => link.href)")
    return {video_id for link in links if (video_id := _video_id_from_url(link))}


class YouTubeVideo(BaseVideoUploader):
    def __init__(self, title, file_path, tags, account_file, *,
                 description="", thumbnail_path=None, playlist=None,
                 visibility="public", debug=DEBUG_MODE, headless=False, proxy=None):
        if visibility not in VISIBILITY:
            raise ValueError(f"不支持的 YouTube 可见性: {visibility}")
        self.title = title
        self.file_path = str(file_path)
        self.tags = tags or []
        self.account_file = str(account_file)
        self.description = description or ""
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.playlist = playlist
        self.visibility = visibility
        self.debug = debug
        self.headless = headless
        self.proxy = proxy
        self.cover_verified = False
        self._thumbnail_prepared = False
        self.content_kind = "unknown"
        self.shorts_eligible = None
        self.observed_visibility = None

    def validate_upload_args(self):
        self.file_path = str(self.validate_video_file(self.file_path))
        if not Path(self.account_file).is_file():
            raise FileNotFoundError("YouTube 登录态文件不存在，请先登录")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 100:
            raise ValueError("YouTube 标题必须是 1–100 个字符")
        if len(self.description) > 5000:
            raise ValueError("YouTube 简介超过 5000 个字符")
        if len(",".join(self.tags)) > 500:
            raise ValueError("YouTube 标签总长度超过 500 个字符")
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))
        eligible_kind = probe_content_kind(self.file_path)
        self.shorts_eligible = True if eligible_kind == "shorts" else False if eligible_kind == "video" else None
        # Eligibility is not a claim that Studio has already classified an upload as a Short.
        self.content_kind = "video" if eligible_kind == "video" else "unknown"

    async def _thumbnail_state(self, tile) -> dict:
        return await tile.evaluate(
            """root => {
                const tile = root.closest('ytcp-thumbnail-editor') || root;
                const selected = tile.hasAttribute('selected') || tile.hasAttribute('checked') ||
                    tile.getAttribute('aria-selected') === 'true' || tile.getAttribute('aria-checked') === 'true' ||
                    !!tile.querySelector('[selected], [aria-selected="true"], [aria-checked="true"]');
                const images = Array.from(root.querySelectorAll('img')).filter(img => img.getClientRects().length && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
                return {selected, sources: images.map(img => img.currentSrc || img.src)};
            }"""
        )

    async def set_thumbnail(self, page: Page) -> None:
        self.cover_verified = False
        self._thumbnail_prepared = False
        if not self.thumbnail_path:
            return
        tile = page.locator(f"{UPLOAD_DIALOG} {THUMBNAIL_UPLOADER}").first
        await tile.wait_for(state="visible", timeout=30000)
        upload = tile.locator("input[type='file']").first
        await upload.wait_for(state="attached", timeout=10000)
        original = set((await self._thumbnail_state(tile)).get("sources", []))
        await upload.set_input_files(self.thumbnail_path)
        for _ in range(60):
            state = await self._thumbnail_state(tile)
            if state.get("selected") and set(state.get("sources", [])).difference(original):
                self._thumbnail_prepared = True
                return
            await page.wait_for_timeout(500)
        raise RuntimeError("指定的 YouTube 封面未确认加载并选中，停止发布")

    async def _set_playlist(self, page: Page) -> None:
        if not self.playlist:
            return
        if not await _click_if_present(page, "ytcp-video-metadata-playlists ytcp-dropdown-trigger, #basics ytcp-text-dropdown-trigger", 8000):
            raise RuntimeError("无法打开 YouTube 播放列表设置")
        checkbox = page.locator("tp-yt-paper-checkbox").filter(has=page.get_by_text(self.playlist, exact=True)).first
        if not await checkbox.count():
            if not await _click_if_present(page, "ytcp-button:has-text('New playlist'), ytcp-button:has-text('创建播放列表')", 4000):
                raise RuntimeError("指定的 YouTube 播放列表不存在且无法创建")
            title = page.locator("ytcp-playlist-metadata-editor #textbox, #create-playlist-form #textbox").first
            await title.wait_for(state="visible", timeout=8000)
            await title.fill(self.playlist)
            if not await _click_if_present(page, "ytcp-button#create-button, tp-yt-paper-dialog ytcp-button:has-text('Create'), tp-yt-paper-dialog ytcp-button:has-text('创建')", 4000):
                raise RuntimeError("创建 YouTube 播放列表失败")
            await checkbox.wait_for(state="visible", timeout=10000)
        if not await _is_checked(checkbox):
            await checkbox.click()
        if not await _is_checked(checkbox):
            raise RuntimeError("指定的 YouTube 播放列表未选中")
        if not await _click_if_present(page, "ytcp-playlist-dialog #save-button, ytcp-button:has-text('Done'), ytcp-button:has-text('完成')", 5000):
            raise RuntimeError("YouTube 播放列表未保存")

    async def _fill_details(self, page: Page) -> None:
        await _fill_editable(page, f"{UPLOAD_DIALOG} #title-textarea #textbox", self.title)
        if self.description.strip():
            await _fill_editable(page, f"{UPLOAD_DIALOG} #description-textarea #textbox", self.description)
        await self.set_thumbnail(page)
        await self._set_playlist(page)
        await _select_radio(page, "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']", "非儿童向受众")
        if self.tags:
            if not await _click_if_present(page, f"{UPLOAD_DIALOG} #toggle-button", 6000):
                raise RuntimeError("无法打开 YouTube 标签设置")
            tag_input = page.locator(f"{UPLOAD_DIALOG} #tags-container #text-input, {UPLOAD_DIALOG} ytcp-form-input-container#tags-container input").first
            await tag_input.fill(",".join(self.tags))
            await tag_input.press("Enter")
            text = await page.locator(f"{UPLOAD_DIALOG} #tags-container").inner_text()
            if any(tag not in text for tag in self.tags):
                raise RuntimeError("YouTube 标签未完整保存")

    async def _set_visibility(self, page: Page) -> None:
        selector = f"{UPLOAD_DIALOG} tp-yt-paper-radio-button[name='{VISIBILITY[self.visibility]}']"
        for _ in range(5):
            radio = page.locator(selector).first
            if await radio.count() and await radio.is_visible():
                await _select_radio(page, selector, f"可见性 {self.visibility}")
                return
            if not await _click_if_present(page, f"{UPLOAD_DIALOG} #next-button", 10000):
                raise RuntimeError("YouTube 上传向导无法进入下一步")
            await page.wait_for_timeout(1000)
        raise RuntimeError("YouTube 上传向导未到达可见性设置")

    async def _current_upload_id(self, page: Page) -> str:
        dialog = page.locator(UPLOAD_DIALOG).first
        for _ in range(30):
            ids = await _scoped_video_ids(dialog)
            if len(ids) == 1:
                return next(iter(ids))
            if len(ids) > 1:
                raise RuntimeError("本次 YouTube 上传向导返回了多个作品 ID，停止发布")
            await page.wait_for_timeout(1000)
        raise RuntimeError("未取得本次 YouTube 上传的明确作品 ID，停止发布")

    async def _confirm_publication(self, page: Page, post_id: str, max_polls: int = 60) -> str | None:
        # Only a completion dialog containing THIS upload's ID can confirm this attempt.
        # Never inspect a channel's newest row or an unrelated link on the page.
        # These web-component hosts can have no layout box while their paper dialog
        # is visible. Scope visibility, text and links to the actual rendered dialog.
        confirmations = page.locator("ytcp-video-share-dialog tp-yt-paper-dialog[role='dialog']:visible")
        for _ in range(max_polls):
            if await confirmations.count() == 1:
                confirmation = confirmations.first
                ids = await _scoped_video_ids(confirmation)
                text = await confirmation.inner_text()
                published = bool(re.search(r"video published|published successfully|视频已发布|已发布|发布成功", text, re.I))
                saved = bool(re.search(r"video saved|saved successfully|视频已保存|已保存", text, re.I))
                if ids == {post_id} and (published or (self.visibility != "public" and saved)):
                    return "published" if self.visibility == "public" else self.visibility
            # Studio acknowledges a public submission before SD processing finishes.
            # This popup has no video URL, so bind it to the still-open upload dialog's
            # unique ID. The title is intentionally not used: a batch may reuse titles.
            processing = page.locator("ytcp-uploads-still-processing-dialog tp-yt-paper-dialog[role='dialog']:visible")
            if self.visibility == "public" and await processing.count() == 1:
                popup = processing.first
                heading = popup.locator("#uploads-still-processing-dialog-title").first
                text = " ".join((await popup.inner_text()).lower().split())
                explicit_pending = (
                    await heading.count() == 1
                    and (await heading.inner_text()).strip().lower() == "video processing"
                    and "needs to finish processing before your video is public on youtube" in text
                )
                upload_dialogs = page.locator(f"{UPLOAD_DIALOG} tp-yt-paper-dialog[role='dialog']:visible")
                if explicit_pending and await upload_dialogs.count() == 1:
                    ids = await _scoped_video_ids(upload_dialogs.first)
                    if ids == {post_id}:
                        return "accepted_pending_processing"
            await page.wait_for_timeout(5000)
        return None

    async def _verify_saved_details(self, page: Page, post_id: str, *, allow_pending: bool = False) -> bool:
        """Read the exact uploaded video's persisted settings, never a latest-video list."""
        details = await page.context.new_page()
        try:
            await details.goto(f"{STUDIO_URL}/video/{post_id}/edit", wait_until="domcontentloaded", timeout=60000)
            await _skip_unsupported_browser_notice(details)
            if _video_id_from_url(details.url) != post_id:
                return False
            visibility = details.locator("ytcp-video-metadata-visibility").first
            await visibility.wait_for(state="visible", timeout=30000)
            labels = {"public": {"public", "公开"}, "unlisted": {"unlisted", "不公开"}, "private": {"private", "私享", "私密"}, "pending": {"pending", "处理中", "待处理"}}
            lines = {line.strip().lower() for line in (await visibility.inner_text()).splitlines() if line.strip()}
            observed = {value for value, names in labels.items() if names.intersection(lines)}
            self.observed_visibility = next(iter(observed)) if len(observed) == 1 else None
            accepted = {self.visibility}
            if allow_pending and self.visibility == "public":
                accepted.add("pending")
            if self.observed_visibility not in accepted:
                return False
            if self.thumbnail_path:
                tile = details.locator(THUMBNAIL_UPLOADER).first
                await tile.wait_for(state="visible", timeout=20000)
                self.cover_verified = False
                for _ in range(30):
                    state = await self._thumbnail_state(tile)
                    if state.get("selected") and state.get("sources"):
                        self.cover_verified = True
                        break
                    await details.wait_for_timeout(500)
                if not self.cover_verified:
                    return False
            # A Shorts link tied to this ID is actual Studio evidence, unlike local dimensions alone.
            links = await details.locator("ytcp-video-info a[href], ytcp-video-metadata-editor a[href]").evaluate_all("links => links.map(link => link.href)")
            if any(_video_id_from_url(link) == post_id and urlsplit(link).path.startswith("/shorts/") for link in links):
                self.content_kind = "shorts"
            return True
        except Exception:
            return False
        finally:
            try:
                await details.close()
            except Exception:
                pass

    def _receipt(self, status, attempted_at, post_id=None, remote_status=None, error=None, *, observed_visibility=None) -> dict:
        return {
            "schema_version": 1, "platform": "youtube", "status": status,
            "post_id": post_id, "url": f"https://www.youtube.com/watch?v={post_id}" if post_id else None,
            "remote_status": remote_status, "visibility": self.visibility,
            "requested_visibility": self.visibility,
            "observed_visibility": observed_visibility if observed_visibility is not None else self.observed_visibility,
            "cover_verified": self.cover_verified, "content_kind": self.content_kind, "shorts_eligible": self.shorts_eligible,
            "attempted_at": attempted_at, "finished_at": _utc_now(), "error": error,
        }

    async def upload(self, playwright: Playwright) -> dict:
        attempted_at = _utc_now()
        browser = context = None
        submitted = False
        post_id = None
        try:
            self.validate_upload_args()
            browser = await playwright.chromium.launch(**_browser_options(headless=self.headless, proxy=self.proxy))
            context = await _new_context(browser, headless=self.headless, account_file=self.account_file)
            page = await context.new_page()
            page.set_default_timeout(30000)
            await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(15):
                if await _is_authenticated_studio(page):
                    break
                await page.wait_for_timeout(1000)
            else:
                raise RuntimeError("YouTube 登录态无效或尚未进入频道工作室，请重新登录")
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state="attached", timeout=60000)
            await file_input.set_input_files(self.file_path)
            await page.locator(f"{UPLOAD_DIALOG} #title-textarea").wait_for(state="visible", timeout=120000)
            await self._fill_details(page)
            await self._set_visibility(page)
            await _wait_upload_complete(page)
            post_id = await self._current_upload_id(page)
            if self.thumbnail_path and not self._thumbnail_prepared:
                raise RuntimeError("指定的 YouTube 封面尚未验证，停止发布")
            await _select_radio(page, f"{UPLOAD_DIALOG} tp-yt-paper-radio-button[name='{VISIBILITY[self.visibility]}']", f"可见性 {self.visibility}")
            publish = page.locator(f"{UPLOAD_DIALOG} #done-button").first
            await publish.wait_for(state="visible", timeout=15000)
            enabled = await publish.evaluate("el => !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true' && !el.querySelector('button:disabled')")
            if not enabled:
                raise RuntimeError("YouTube 发布按钮不可用，停止发布")
            # A click timeout can occur after the server received the request. Never retry it.
            submitted = True
            await publish.click(timeout=15000)
            remote_status = await self._confirm_publication(page, post_id)
            if not remote_status:
                return self._receipt("needs_verification", attempted_at, post_id, "unknown", "提交后未确认本次作品发布结果；请核查该 ID，勿重复上传")
            if remote_status == "accepted_pending_processing":
                # Acceptance is submission success only after the exact saved video's
                # Pending/Public state and requested custom cover are independently read.
                verified = await self._verify_saved_details(page, post_id, allow_pending=True)
                if not verified:
                    return self._receipt("needs_verification", attempted_at, post_id, "settings_unverified", "YouTube 已接收请求，但该 ID 保存后的状态或封面尚未确认；勿重复上传")
                if self.observed_visibility == "public":
                    remote_status = "published"
            elif not await self._verify_saved_details(page, post_id):
                return self._receipt("needs_verification", attempted_at, post_id, "settings_unverified", "本次作品已提交，但保存后的可见性或封面未获确认；勿重复上传")
            try:
                await context.storage_state(path=self.account_file)
            except Exception:
                youtube_logger.warning("YouTube 登录态刷新失败；已确认的发布结果保留")
            return self._receipt("success", attempted_at, post_id, remote_status)
        except Exception as exc:
            if submitted:
                return self._receipt("needs_verification", attempted_at, post_id, "unknown", str(exc))
            exc.submission_started = False
            raise
        finally:
            await _close_resources(context, browser)

    async def main(self) -> dict:
        receipt = None
        try:
            async with async_playwright() as playwright:
                receipt = await self.upload(playwright)
                return receipt
        except Exception as exc:
            if receipt is not None:
                youtube_logger.warning("YouTube Playwright 退出失败；保留已经取得的发布回执")
                return receipt
            # upload() converts every exception after submission into a receipt.
            exc.submission_started = False
            raise
