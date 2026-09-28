# -*- coding: utf-8 -*-
"""TikTok Studio uploads with bounded waits and submission-specific receipts."""
from __future__ import annotations

import asyncio
import base64
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlparse

from patchright.async_api import Playwright, async_playwright

from conf import BASE_DIR, LOCAL_CHROME_PATH, LOCAL_CHROME_HEADLESS
try:
    from conf import TIKTOK_PROXY
except ImportError:
    TIKTOK_PROXY = None
from utils.base_social_media import set_init_script
from utils.log import tiktok_logger

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
UPLOAD_TIMEOUT_SECONDS = 30 * 60
PUBLISH_TIMEOUT_SECONDS = 5 * 60
LOGIN_TIMEOUT_SECONDS = 10 * 60
SESSION_SAVE_TIMEOUT_SECONDS = 15
RESOURCE_CLOSE_TIMEOUT_SECONDS = 10
PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 10
VISIBILITY_LABELS = {"public": "Everyone", "friends": "Friends", "private": "Only you"}
CREATE_PATH = re.compile(r"(?:/(?:api/)?v\d+/(?:web/)?(?:project/post|item/create|post/create)/?|/tiktok/web/project/post/v1/?)$")


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _launch_options(headless=True, proxy=None):
    options = {"headless": headless, "args": ["--lang=en-US"]}
    if LOCAL_CHROME_PATH:
        options["executable_path"] = LOCAL_CHROME_PATH
    else:
        options["channel"] = "chrome"
    proxy = proxy if proxy is not None else os.getenv("SAU_TIKTOK_PROXY", TIKTOK_PROXY or "")
    if proxy:
        options["proxy"] = {"server": proxy}
    return options


async def _close_resources(context, browser):
    for name, resource in (("context", context), ("browser", browser)):
        if resource is not None:
            try:
                await asyncio.wait_for(resource.close(), timeout=RESOURCE_CLOSE_TIMEOUT_SECONDS)
            except Exception as exc:
                tiktok_logger.warning(f"Could not close TikTok {name}: {type(exc).__name__}: {exc}")


def _account_path(account_file):
    path = Path(account_file).expanduser()
    # Preserve legacy relative-path examples; CLI supplies cookies/tiktok_<account>.json.
    return path if path.is_absolute() else Path(BASE_DIR) / "tk_uploader" / path


async def _studio_root(page):
    frame = page.locator('iframe[data-tt="Upload_index_iframe"]')
    return page.frame_locator('iframe[data-tt="Upload_index_iframe"]') if await frame.count() else page.locator("body")


async def _authenticated(page):
    if "tiktok.com" != (urlparse(page.url).hostname or "").removeprefix("www.") or "/tiktokstudio" not in urlparse(page.url).path:
        return False
    root = await _studio_root(page)
    # A real authenticated upload control is positive evidence; absence of a login error is not.
    upload = root.locator('input[type="file"][accept*="video"], button:has-text("Select video"), button[aria-label="Select file"]')
    return bool(await upload.count())


async def _account_identity(page):
    # Read only public account identifiers; never copy cookie/session fields to receipts.
    try:
        result = await page.evaluate("""() => {
            const scope = window.__UNIVERSAL_DATA_FOR_REHYDRATION__?.__DEFAULT_SCOPE__;
            const context = scope?.['webapp.app-context'];
            const user = context?.user || window.SIGI_STATE?.AppContext?.appContext?.user;
            if (!user) return {};
            return {handle: user.uniqueId || user.unique_id || '', nickname: user.nickname || '',
                    user_id: String(user.uid || user.id || '')};
        }""")
        return {key: str(value) for key, value in result.items() if value} if isinstance(result, dict) else {}
    except Exception:
        return {}


async def cookie_auth(account_file, *, headless=True, proxy=None):
    browser = context = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**_launch_options(headless, proxy))
                context = await browser.new_context(storage_state=str(_account_path(account_file)), locale="en-US")
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                for _ in range(30):
                    if await _authenticated(page):
                        return True
                    if "/login" in page.url:
                        return False
                    await page.wait_for_timeout(500)
                return False
            finally:
                await _close_resources(context, browser)
    except Exception:
        return False


async def get_tiktok_cookie(account_file, *, headless=False, proxy=None):
    """Always show login; save storage state only after authenticated Studio appears."""
    path = _account_path(account_file)
    browser = context = None
    current_url = ""
    success = False
    account = {}
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**_launch_options(False, proxy))
                context = await browser.new_context(locale="en-US")
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                tiktok_logger.info("请在可见浏览器完成 TikTok 登录；进入 Studio 上传页后才会保存登录态。")
                deadline = monotonic() + LOGIN_TIMEOUT_SECONDS
                while monotonic() < deadline:
                    current_url = page.url
                    if await _authenticated(page):
                        account = await _account_identity(page)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        await context.storage_state(path=str(path))
                        success = True
                        break
                    # Some login flows land on the feed. Re-enter Studio after a visible account menu appears.
                    if "/tiktokstudio" not in urlparse(page.url).path and await page.locator('[data-e2e="profile-icon"]').count():
                        await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(1000)
            finally:
                await _close_resources(context, browser)
    except Exception as exc:
        return {"success": False, "status": "login_failed", "message": str(exc), "account_file": str(path), "current_url": current_url}
    return {"success": success, "status": "logged_in" if success else "timeout", "message": "登录成功" if success else "登录超时，未保存登录态", "account_file": str(path), "current_url": current_url, "account_identity": account}


async def tiktok_setup(account_file, handle=False, return_detail=False, headless=False, proxy=None):
    path = _account_path(account_file)
    valid = path.is_file() and await cookie_auth(path, headless=headless, proxy=proxy)
    if valid:
        result = {"success": True, "status": "cookie_valid", "message": "登录态有效", "account_file": str(path)}
    elif handle:
        result = await get_tiktok_cookie(path, headless=headless, proxy=proxy)
    else:
        result = {"success": False, "status": "cookie_invalid", "message": "登录态不存在或失效", "account_file": str(path)}
    return result if return_detail else result["success"]


def _post_identity(url):
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"www.tiktok.com", "tiktok.com"} or parsed.scheme != "https":
            return None
        match = re.fullmatch(r"/(?:@[^/]+/video|share/video)/(\d+)", parsed.path.rstrip("/"))
        return (match.group(1), f"https://www.tiktok.com{parsed.path}") if match else None
    except (TypeError, ValueError):
        return None


def _create_evidence(payload):
    """Parse only a response to this submission's create endpoint; never list results."""
    if not isinstance(payload, dict):
        return {}
    status = payload.get("status_code", payload.get("statusCode", payload.get("status")))
    if status is not None and str(status) not in {"0", "success", "ok"}:
        return {"rejected": True, "error": str(payload.get("status_msg", payload.get("statusMsg", "TikTok rejected submission")))}
    if status is None:
        return {}
    nodes = [payload]
    if "single_post_resp_list" in payload:
        posts = payload["single_post_resp_list"]
        # A project can contain several posts. This uploader submitted one video;
        # accept only its unambiguous item response, never the project_id.
        if not isinstance(posts, list) or len(posts) != 1 or not isinstance(posts[0], dict):
            return {}
        post = posts[0]
        if "status_code" not in post:
            return {}
        if str(post["status_code"]) != "0":
            return {"rejected": True, "error": str(post.get("status_msg") or "TikTok rejected the single post")}
        if not re.fullmatch(r"\d+", str(post.get("item_id", ""))):
            return {}
        nodes.append(post)
    for key in ("data", "item", "itemInfo", "aweme", "post"):
        if isinstance(payload.get(key), dict):
            nodes.append(payload[key])
    for node in list(nodes):
        for key in ("item", "itemInfo", "aweme", "post"):
            if isinstance(node.get(key), dict):
                nodes.append(node[key])
    ids = set()
    urls = set()
    for node in nodes:
        for key in ("item_id", "itemId", "aweme_id", "post_id", "postId"):
            value = str(node.get(key, ""))
            if re.fullmatch(r"\d+", value):
                ids.add(value)
        for key in ("share_url", "shareUrl", "url"):
            identity = _post_identity(node.get(key))
            if identity:
                ids.add(identity[0])
                urls.add(identity[1])
    if len(ids) != 1:
        return {}
    return {"post_id": ids.pop(), "url": next(iter(urls)) if len(urls) == 1 else None, "accepted": True}


# Both source files and the already-rendered profile use identical area integration.
# Browser drawImage downscaling (even quality="high") is not an area-average guarantee.
_NATIVE_AREA_SAMPLE_JS = r"""(source, width, height) => {
    if (width < 32 || height < 32) return null;
    const native = document.createElement('canvas');
    native.width = width; native.height = height;
    const context = native.getContext('2d', {willReadFrequently: true});
    context.drawImage(source, 0, 0); // Native size: no browser resampling.
    const rgba = context.getImageData(0, 0, width, height).data;
    const result = [], scaleX = width / 32, scaleY = height / 32;
    let paintedArea = 0;
    for (let row = 0; row < 32; row++) {
        const top = row * scaleY, bottom = (row + 1) * scaleY;
        for (let column = 0; column < 32; column++) {
            const left = column * scaleX, right = (column + 1) * scaleX;
            let red = 0, green = 0, blue = 0, covered = 0;
            for (let y = Math.floor(top); y < Math.ceil(bottom); y++) {
                const vertical = Math.min(bottom, y + 1) - Math.max(top, y);
                for (let x = Math.floor(left); x < Math.ceil(right); x++) {
                    const horizontal = Math.min(right, x + 1) - Math.max(left, x);
                    const index = (y * width + x) * 4;
                    const weight = horizontal * vertical * rgba[index + 3] / 255;
                    covered += weight;
                    red += rgba[index] * weight;
                    green += rgba[index + 1] * weight;
                    blue += rgba[index + 2] * weight;
                }
            }
            if (!covered) return null;
            paintedArea += covered;
            result.push(Math.round(red / covered), Math.round(green / covered), Math.round(blue / covered));
        }
    }
    return paintedArea >= width * height * .975 ? result : null;
}"""


async def _image_sample(locator, image_bytes):
    # Decode at native dimensions; average every source pixel intersecting each sample cell.
    encoded = base64.b64encode(image_bytes).decode("ascii")
    sample = await locator.evaluate("""async (_element, encoded) => {
        const image = new Image();
        image.src = 'data:image/' + (encoded.startsWith('/9j/') ? 'jpeg' : 'png') + ';base64,' + encoded;
        await image.decode();
        const sampleNative = """ + _NATIVE_AREA_SAMPLE_JS + """;
        return sampleNative(image, image.naturalWidth, image.naturalHeight);
    }""", encoded)
    if sample is None:
        raise RuntimeError("Cover sample is empty or insufficiently painted")
    return sample


def _same_image_sample(first, second):
    if len(first) != 32 * 32 * 3 or len(second) != len(first):
        return False
    differences = sorted(abs(left - right) for left, right in zip(first, second))
    return sum(differences) / len(differences) <= 3 and differences[int(len(differences) * .95)] <= 12


class TikTokPreSubmissionError(RuntimeError):
    submission_started = False


class TiktokVideo:
    def __init__(self, title, file_path, tags, publish_date, account_file, thumbnail_path=None, *, headless=None, proxy=None, visibility="public", debug=False):
        self.title = title
        self.file_path = str(file_path)
        self.tags = tags
        self.publish_date = publish_date
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.account_file = str(_account_path(account_file))
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = LOCAL_CHROME_HEADLESS if headless is None else headless
        self.proxy = proxy
        self.visibility = visibility
        self.debug = debug
        self.locator_base = None
        self.cover_verified = False
        self._verified_cover_sample = None
        self.attempted_at = None
        self._submission_started = False

    def _receipt(self, status, *, post_id=None, url=None, remote_status=None, error=None):
        return {"schema_version": 1, "platform": "tiktok", "status": status, "post_id": post_id, "url": url,
                "remote_status": remote_status, "visibility": self.visibility, "cover_verified": self.cover_verified,
                "content_kind": "video", "attempted_at": self.attempted_at or _utc_now(), "finished_at": _utc_now(), "error": error}

    def validate(self):
        if not Path(self.file_path).is_file():
            raise ValueError(f"Video not found: {self.file_path}")
        if self.thumbnail_path and not Path(self.thumbnail_path).is_file():
            raise ValueError(f"Requested cover not found: {self.thumbnail_path}")
        caption = self.title + "\n" + " ".join(f"#{tag}" for tag in self.tags)
        if not self.title.strip() or len(caption) > 2200:
            raise ValueError("TikTok caption must be nonempty and at most 2200 characters including hashtags")
        if self.visibility not in VISIBILITY_LABELS:
            raise ValueError("Unsupported TikTok visibility")

    async def choose_base_locator(self, page):
        self.locator_base = await _studio_root(page)

    def _post_button(self):
        return self.locator_base.get_by_role("button", name="Post", exact=True)

    async def _dismiss_feature_guide(self, page):
        guide = page.locator('.react-joyride__tooltip:visible').filter(
            has_text=re.compile(r"New editing features added|Preview your video on your phone")
        )
        if not await guide.count() and await page.locator('.react-joyride__overlay:visible').count():
            # The backdrop appears before the animated tooltip's content.
            try:
                await guide.wait_for(state="visible", timeout=5000)
            except Exception as exc:
                raise RuntimeError("Unrecognized TikTok guide blocks editor controls") from exc
            if not await guide.count():
                raise RuntimeError("Unrecognized TikTok guide blocks editor controls")
        if await guide.count():
            if await guide.count() != 1:
                raise RuntimeError("TikTok feature guide is ambiguous")
            await guide.get_by_role("button", name="Got it", exact=True).click(timeout=5000)
            await guide.wait_for(state="hidden", timeout=5000)
            await page.locator('.react-joyride__overlay:visible').wait_for(state="hidden", timeout=5000)
        elif await page.locator('.react-joyride__overlay:visible').count():
            raise RuntimeError("Unrecognized TikTok guide blocks editor controls")

    async def _dismiss_editor_overlays(self, page):
        await self._dismiss_feature_guide(page)
        editor = self.locator_base.locator('div.public-DraftEditor-content, [contenteditable="true"][role="textbox"], [contenteditable="true"][role="combobox"]').first
        if not await editor.count():
            return
        before = await editor.inner_text()
        # Blurring and Escape dismiss hashtag suggestions without choosing or deleting text.
        await editor.evaluate("el => el.blur()")
        await page.keyboard.press("Escape")
        if await editor.inner_text() != before:
            raise RuntimeError("TikTok caption changed while dismissing autocomplete")

    async def add_title_tags(self, page):
        editor = self.locator_base.locator('div.public-DraftEditor-content, [contenteditable="true"][role="textbox"]').first
        await editor.wait_for(state="visible", timeout=30000)
        text = self.title + "\n" + " ".join(f"#{tag}" for tag in self.tags)
        for _attempt in range(3):
            # DraftJS can retain the auto-inserted filename when fill() replaces DOM text.
            # Real keyboard deletion updates its editor state before the new insertion.
            await editor.click(timeout=10000)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(150)
            if (await editor.inner_text()).strip():
                continue
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(200)
            if (await editor.inner_text()).strip() != text.strip():
                continue
            await self._dismiss_editor_overlays(page)
            if (await editor.inner_text()).strip() != text.strip():
                raise RuntimeError("TikTok caption changed after dismissing autocomplete")
            return
        raise RuntimeError("TikTok caption did not retain the requested text after 3 keyboard replacements")

    async def detect_upload_status(self, page):
        deadline = monotonic() + UPLOAD_TIMEOUT_SECONDS
        while monotonic() < deadline:
            failure = self.locator_base.get_by_text(re.compile(r"^(Upload failed|Couldn't upload|Video upload failed)", re.I))
            if await failure.count() and await failure.first.is_visible():
                raise RuntimeError("TikTok video upload failed before submission")
            button = self._post_button()
            if await button.count() == 1 and await button.is_visible() and await button.is_enabled() and await button.get_attribute("aria-disabled") != "true":
                return
            await page.wait_for_timeout(1000)
        raise TimeoutError("TikTok upload did not become ready within 30 minutes")

    async def _cover_snapshot(self, container):
        return await container.evaluate("""root => {
            const values = [];
            for (const img of root.querySelectorAll('img')) {
                if (img.getClientRects().length && img.complete && img.naturalWidth > 100 && img.naturalHeight > 100) values.push(img.currentSrc || img.src);
            }
            for (const canvas of root.querySelectorAll('canvas')) {
                if (canvas.getClientRects().length && canvas.width > 100 && canvas.height > 100) {
                    try { values.push(canvas.toDataURL()); } catch (_) {}
                }
            }
            return values;
        }""")

    async def _select_cover_file(self, upload):
        selection = await upload.evaluate_handle("""el => {
            const view = el.ownerDocument.defaultView;
            const state = {name: ''};
            const record = event => {
                if (event.target !== el) return;
                state.name = el.files && el.files[0] ? el.files[0].name : '';
                state.cleanup();
            };
            state.cleanup = () => view.removeEventListener('change', record, true);
            view.addEventListener('change', record, {capture: true});
            return state;
        }""")
        try:
            await upload.set_input_files(self.thumbnail_path)
            name = await selection.evaluate("state => state.name")
        finally:
            try:
                await selection.evaluate("state => state.cleanup()")
            finally:
                await selection.dispose()
        if name != Path(self.thumbnail_path).name:
            raise RuntimeError("TikTok cover input did not accept the requested image file")

    async def _editor_cover_sample(self, editor, expected):
        # The profile canvas is the actual 3:4 result (486x648 in current Studio),
        # despite its small CSS size. StageCanvas includes black sidebars; the upload
        # thumbnail proves only file selection. Neither is final-cover evidence.
        canvas = editor.locator('canvas.CoverEditorProfilePreview__canvas:visible')
        count = await canvas.count()
        if count == 0:
            return None
        if count != 1:
            raise RuntimeError("TikTok final profile cover canvas is ambiguous")
        sample = await canvas.evaluate("""canvas => {
            if (canvas.width < 240 || canvas.height < 320 || Math.abs(canvas.width / canvas.height - 3 / 4) > .01) return null;
            try {
                const sampleNative = """ + _NATIVE_AREA_SAMPLE_JS + """;
                return sampleNative(canvas, canvas.width, canvas.height);
            } catch (_) { return null; }
        }""")
        return sample if sample is not None and _same_image_sample(sample, expected) else None

    async def _upload_fullscreen_cover(self, page, area):
        if await area.count() != 1:
            raise RuntimeError("TikTok full-screen cover upload area is ambiguous")
        # Upload cover area disappears after file selection. Anchor to the final preview,
        # which remains in the editor while that input is replaced by its small thumbnail.
        preview = self.locator_base.locator('canvas.CoverEditorProfilePreview__canvas:visible')
        await preview.wait_for(state="visible", timeout=15000)
        if await preview.count() != 1:
            raise RuntimeError("TikTok final profile cover preview is not unique")
        editor = preview.locator("xpath=ancestor::*[.//button[contains(concat(' ', normalize-space(@class), ' '), ' header-button ') and normalize-space()='Save'] and .//button[contains(concat(' ', normalize-space(@class), ' '), ' header-button ') and normalize-space()='Cancel']][1]")
        if await editor.count() != 1:
            raise RuntimeError("TikTok full-screen cover editor is not uniquely identified")
        save = editor.locator('button.header-button:visible').filter(has_text=re.compile(r'^Save$'))
        cancel = editor.locator('button.header-button:visible').filter(has_text=re.compile(r'^Cancel$'))
        upload = area.locator('input[type="file"][accept*="image/"]')
        if await save.count() != 1 or await cancel.count() != 1 or await upload.count() != 1:
            raise RuntimeError("TikTok full-screen cover editor controls are missing or ambiguous")
        expected = await _image_sample(editor, Path(self.thumbnail_path).read_bytes())
        await self._select_cover_file(upload)
        previous = None
        for _ in range(60):
            sample = await self._editor_cover_sample(editor, expected)
            if sample is not None and previous is not None and _same_image_sample(sample, previous) and await save.is_enabled():
                await save.click(timeout=15000)
                await save.wait_for(state="hidden", timeout=30000)
                await preview.wait_for(state="hidden", timeout=30000)
                return
            previous = sample
            await page.wait_for_timeout(500)
        raise RuntimeError("TikTok full-screen editor did not show the requested custom cover; refusing to Save")

    async def _upload_legacy_cover(self, page, panel):
        await panel.wait_for(state="visible", timeout=15000)
        await self.locator_base.get_by_text("Upload cover", exact=True).click()
        area = panel.locator('.upload-image-upload-area')
        async with page.expect_file_chooser(timeout=15000) as choice:
            await area.click()
        chooser = await choice.value
        await chooser.set_files(self.thumbnail_path)
        confirm = panel.get_by_role("button", name="Confirm", exact=True)
        await confirm.wait_for(state="visible", timeout=30000)
        # A disabled processing button cannot be bypassed with a forced click.
        await confirm.click(timeout=30000)
        await panel.wait_for(state="hidden", timeout=30000)

    async def upload_thumbnails(self, page):
        card = self.locator_base.locator('.cover-container:visible')
        if await card.count() != 1:
            raise RuntimeError("Requested TikTok cover: main cover preview is missing or ambiguous")
        before = set(await self._cover_snapshot(card))
        entry = self.locator_base.get_by_text("Edit cover", exact=True)
        if await entry.count() == 1 and await entry.is_visible():
            await entry.click(timeout=10000)
        elif await entry.count() == 0:
            # Legacy editor exposes its entry solely as the cover card.
            await card.click(timeout=10000)
        else:
            raise RuntimeError("TikTok Edit cover entry is ambiguous")
        area = self.locator_base.locator('label.ImageUpload__uploadArea[role="button"][aria-label="Upload cover image"]:visible')
        panel = self.locator_base.locator('div.cover-edit-panel:not(.hide-panel):visible')
        for _ in range(30):
            if await area.count():
                await self._upload_fullscreen_cover(page, area)
                break
            if await panel.count() == 1:
                await self._upload_legacy_cover(page, panel)
                break
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError("No supported TikTok cover editor appeared")
        await card.wait_for(state="visible", timeout=15000)
        previous = set()
        for _ in range(60):
            current = set(await self._cover_snapshot(card)) - before
            if current and current.intersection(previous):
                # Screenshot the actual saved image to compare pixels without a cross-origin canvas read.
                sample = await self._saved_cover_sample(card)
                expected = await _image_sample(card, Path(self.thumbnail_path).read_bytes())
                if sample and _same_image_sample(sample, expected):
                    self._verified_cover_sample = sample
                    self.cover_verified = True
                    return
            previous = current
            await page.wait_for_timeout(500)
        raise RuntimeError("Requested TikTok cover did not update and load in the saved preview")

    async def _saved_cover_sample(self, card):
        image = card.locator('img:visible')
        if await image.count() != 1:
            return None
        if not await image.evaluate("img => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0"):
            return None
        sample = await image.evaluate("""image => {
            try {
                const sampleNative = """ + _NATIVE_AREA_SAMPLE_JS + """;
                return sampleNative(image, image.naturalWidth, image.naturalHeight);
            } catch (error) {
                if (error.name === 'SecurityError') return {tainted: true};
                throw error;
            }
        }""")
        if isinstance(sample, list):
            return sample
        if isinstance(sample, dict) and sample.get("tainted"):
            # Only inaccessible cross-origin pixels need a CSS-rendered screenshot fallback.
            return await _image_sample(image, await image.screenshot(type="png", timeout=5000))
        return None

    async def _assert_custom_cover(self):
        if not self.thumbnail_path:
            return
        if not self.cover_verified or self._verified_cover_sample is None:
            raise RuntimeError("Requested TikTok cover has not been verified")
        card = self.locator_base.locator('.cover-container:visible')
        if await card.count() != 1:
            raise RuntimeError("Saved TikTok cover is missing or ambiguous")
        current = await self._saved_cover_sample(card)
        if current is None or not _same_image_sample(current, self._verified_cover_sample):
            raise RuntimeError("Saved TikTok cover changed before publication")

    async def set_visibility(self, page):
        control = self.locator_base.locator('[data-e2e="privacy-selector"]:visible, [data-e2e="video-visibility"]:visible, .privacy-selector:visible')
        if await control.count() != 1:
            # New Studio builds expose the same control as a named combobox.
            control = self.locator_base.get_by_role("combobox", name=re.compile("Who can (?:watch|view|see)(?: this post)?", re.I))
        if await control.count() != 1:
            # Observed Studio DOM: caption is a DIV combobox; audience is a BUTTON
            # combobox named by its current value, beneath "Who can see this post".
            heading = self.locator_base.get_by_text("Who can see this post", exact=True)
            if await heading.count() == 1 and await heading.is_visible():
                control = self.locator_base.locator('button[role="combobox"]:visible').filter(has_text=re.compile(r"^\s*(?:Everyone|Friends|Only you)\s*$"))
        if await control.count() != 1:
            raise RuntimeError("Cannot verify TikTok audience control; refusing to submit")
        label = VISIBILITY_LABELS[self.visibility]
        if label != (await control.inner_text()).strip():
            await control.click()
            option = self.locator_base.get_by_role("option", name=label, exact=True)
            if await option.count() != 1:
                raise RuntimeError("Requested TikTok audience option is missing or ambiguous")
            await option.click()
        if label != (await control.inner_text()).strip():
            raise RuntimeError("TikTok audience did not retain requested visibility")

    async def _continue_post_dialog(self, already_confirmed):
        dialog = self.locator_base.get_by_role("dialog").filter(has_text="Continue to post?")
        count = await dialog.count()
        if count == 0:
            return False
        if count != 1:
            raise RuntimeError("Ambiguous TikTok continuation dialog; verify this submission manually")
        text = await dialog.inner_text()
        required = (
            "Continue to post?",
            "The copyright check is incomplete.",
            "Do you want to continue posting before the check is complete?",
        )
        if not all(phrase in text for phrase in required):
            raise RuntimeError("Unrecognized TikTok continuation warning; no confirmation clicked")
        post_now = dialog.get_by_role("button", name="Post now", exact=True)
        cancel = dialog.get_by_role("button", name="Cancel", exact=True)
        if await post_now.count() != 1 or await cancel.count() != 1:
            raise RuntimeError("TikTok continuation controls are missing or ambiguous")
        if already_confirmed:
            return False
        # This completes the current submission, rather than clicking the main Post again.
        await post_now.click(timeout=10000)
        return True

    async def click_publish(self, page):
        button = self._post_button()
        if await button.count() != 1 or not await button.is_enabled():
            raise RuntimeError("TikTok Post button is not uniquely available")
        await self._assert_custom_cover()
        evidence = []
        tasks = set()
        submitted = False
        continuation_confirmed = False

        async def capture(response):
            try:
                request = response.request
                parsed = urlparse(response.url)
                if not submitted or request.method != "POST" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"} or not CREATE_PATH.search(parsed.path):
                    return
                body = await response.json()
                proof = _create_evidence(body)
                if proof:
                    evidence.append(proof)
            except Exception:
                return

        def on_response(response):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            # From this point a transport or click error may follow a successful server submission.
            submitted = True
            self._submission_started = True
            try:
                await button.click(timeout=15000)
            except Exception as exc:
                return self._receipt("needs_verification", error=f"Post click outcome uncertain: {exc}")
            deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
            while monotonic() < deadline:
                rejected = next((item for item in evidence if item.get("rejected")), None)
                accepted = [item for item in evidence if item.get("accepted")]
                identities = {item["post_id"] for item in accepted}
                if rejected and not accepted:
                    return self._receipt("failed", remote_status="rejected", error=rejected["error"])
                if len(identities) > 1:
                    return self._receipt("needs_verification", error="Conflicting submission IDs; no latest-post inference attempted")
                if len(identities) == 1:
                    post_id = next(iter(identities))
                    url = next((item.get("url") for item in accepted if item.get("url")), None)
                    if not url:
                        # Match the known transaction ID only, never the first/latest table row.
                        links = page.locator(f'a[href*="/video/{post_id}"]')
                        for index in range(await links.count()):
                            identity = _post_identity(await links.nth(index).evaluate("el => el.href"))
                            if identity and identity[0] == post_id:
                                url = identity[1]
                                break
                    if url:
                        return self._receipt("success", post_id=post_id, url=url, remote_status="scheduled" if self.publish_date else "submitted")
                # Some builds show a transaction-specific result dialog with the permalink.
                dialog = self.locator_base.get_by_role("dialog").filter(has_text=re.compile(r"(?:Your video has been (?:uploaded|posted)|Video (?:uploaded|posted))", re.I))
                if await dialog.count() == 1 and await dialog.is_visible():
                    links = dialog.locator('a[href*="/video/"]')
                    found = {_post_identity(href) for href in await links.evaluate_all("els => els.map(el => el.href)")}
                    found.discard(None)
                    if len(found) == 1:
                        post_id, url = found.pop()
                        if not identities or identities == {post_id}:
                            return self._receipt("success", post_id=post_id, url=url, remote_status="scheduled" if self.publish_date else "submitted")
                if await self._continue_post_dialog(continuation_confirmed):
                    continuation_confirmed = True
                await page.wait_for_timeout(500)
            known = next((item for item in evidence if item.get("accepted")), {})
            return self._receipt("needs_verification", post_id=known.get("post_id"), url=known.get("url"), remote_status="unknown", error="No transaction-specific confirmation within 5 minutes; do not resubmit without reconciliation")
        except Exception as exc:
            return self._receipt("needs_verification", error=f"Submission confirmation interrupted: {exc}")
        finally:
            page.remove_listener("response", on_response)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def upload(self, playwright: Playwright):
        self.attempted_at = _utc_now()
        browser = context = None
        receipt = None
        try:
            self.validate()
            options = _launch_options(self.headless, self.proxy)
            if self.local_executable_path:
                options.pop("channel", None)
                options["executable_path"] = self.local_executable_path
            browser = await playwright.chromium.launch(**options)
            context = await browser.new_context(storage_state=self.account_file, locale="en-US")
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            await self.choose_base_locator(page)
            file_input = self.locator_base.locator('input[type="file"][accept*="video"]').first
            await file_input.wait_for(state="attached", timeout=60000)
            await file_input.set_input_files(self.file_path)
            await self._dismiss_feature_guide(page)
            await self.add_title_tags(page)
            await self.detect_upload_status(page)
            await self._dismiss_editor_overlays(page)
            if self.thumbnail_path:
                await self.upload_thumbnails(page)
            await self.set_visibility(page)
            if self.publish_date:
                await self.set_schedule_time(page, self.publish_date)
            receipt = await self.click_publish(page)
            try:
                await asyncio.wait_for(context.storage_state(path=self.account_file), timeout=SESSION_SAVE_TIMEOUT_SECONDS)
            except Exception as exc:
                # Cookie refresh is not grounds to lose a confirmed post receipt.
                tiktok_logger.warning(f"Could not refresh TikTok session after submission: {exc}")
            return receipt
        except Exception as exc:
            if self._submission_started:
                return receipt or self._receipt("needs_verification", error=f"Submission outcome uncertain: {exc}")
            raise TikTokPreSubmissionError(str(exc)) from exc
        finally:
            await _close_resources(context, browser)

    async def set_schedule_time(self, page, publish_date):
        schedule_input_element = self.locator_base.get_by_label('Schedule')
        await schedule_input_element.wait_for(state='visible')  # 确保按钮可见

        await schedule_input_element.click(force=True)
        if await self.locator_base.locator('div.TUXButton-content >> text=Allow').count():
            await self.locator_base.locator('div.TUXButton-content >> text=Allow').click()

        scheduled_picker = self.locator_base.locator('div.scheduled-picker')
        await scheduled_picker.locator('div.TUXInputBox').nth(1).click()

        calendar_month = await self.locator_base.locator(
            'div.calendar-wrapper span.month-title').inner_text()

        n_calendar_month = datetime.strptime(calendar_month, '%B').month

        schedule_month = publish_date.month

        if n_calendar_month != schedule_month:
            if n_calendar_month < schedule_month:
                arrow = self.locator_base.locator('div.calendar-wrapper span.arrow').nth(-1)
            else:
                arrow = self.locator_base.locator('div.calendar-wrapper span.arrow').nth(0)
            await arrow.click()

        # day set
        valid_days_locator = self.locator_base.locator(
            'div.calendar-wrapper span.day.valid')
        valid_days = await valid_days_locator.count()
        for i in range(valid_days):
            day_element = valid_days_locator.nth(i)
            text = await day_element.inner_text()
            if text.strip() == str(publish_date.day):
                await day_element.click()
                break
        # time set
        await scheduled_picker.locator('div.TUXInputBox').nth(0).click()

        hour_str = publish_date.strftime("%H")
        correct_minute = (publish_date.minute // 5) * 5
        minute_str = f"{correct_minute:02d}"

        hour_selector = f"span.tiktok-timepicker-left:has-text('{hour_str}')"
        minute_selector = f"span.tiktok-timepicker-right:has-text('{minute_str}')"

        # pick hour first
        await page.wait_for_timeout(1000)  # 等待500毫秒
        await self.locator_base.locator(hour_selector).click()
        # click time button again
        await page.wait_for_timeout(1000)  # 等待500毫秒
        # pick minutes after
        await self.locator_base.locator(minute_selector).click()

        # click title to remove the focus.
        # await self.locator_base.locator("h1:has-text('Upload video')").click()

    async def main(self):
        receipt = None
        playwright = None
        try:
            # Context-manager exit has no timeout and can hang after Chrome exits.
            playwright = await async_playwright().start()
            receipt = await self.upload(playwright)
            return receipt
        except Exception as exc:
            if receipt is not None:
                return receipt
            if self._submission_started:
                return self._receipt("needs_verification", error=f"Browser shutdown interrupted confirmation: {exc}")
            if isinstance(exc, TikTokPreSubmissionError):
                raise
            raise TikTokPreSubmissionError(str(exc)) from exc
        finally:
            if playwright is not None:
                try:
                    await asyncio.wait_for(playwright.stop(), timeout=PLAYWRIGHT_STOP_TIMEOUT_SECONDS)
                except Exception as exc:
                    # Receipt/error classification is settled before best-effort shutdown.
                    tiktok_logger.warning(f"Could not stop TikTok Playwright: {type(exc).__name__}: {exc}")
