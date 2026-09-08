# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
import re
from datetime import datetime
from pathlib import Path
from time import monotonic

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
VIDEO_UPLOAD_TIMEOUT_SECONDS = 15 * 60
PUBLISH_TIMEOUT_SECONDS = 5 * 60
COVER_ENTRY_TIMEOUT_MS = 120_000
COVER_MODAL_TIMEOUT_MS = 30_000
COVER_IMAGE_TIMEOUT_MS = 30_000
COVER_ENTRY_SELECTORS = (
    "div.cover-plugin-preview div.cover-edit-entry",
    "div.cover-plugin-preview div.upload-cover",
    "div.cover-plugin-preview div.default.pointer",
)
COVER_SURFACE_SELECTORS = (
    "div.cover-plugin-preview div.default--ai-cover-layout",
    "div.cover-plugin-preview div.default",
)
COVER_MODAL_SELECTOR = "div.d-modal.cover-modal:visible"
COVER_FILE_INPUT_SELECTOR = (
    'input[type="file"][aria-label="上传封面图片"], '
    '#upload-cover-containner input[type="file"], '
    'div.upload-wrapper input[type="file"][accept*="image"]'
)
COVER_PREVIEW_SELECTOR = (
    'button.uploaded-thumbnail img[alt="已上传封面"], '
    "img.uploaded-thumbnail-img, "
    "#upload-cover-containner img.cropper"
)
COVER_UPLOADED_THUMBNAIL_SELECTOR = "button.uploaded-thumbnail"
COVER_UPLOADED_INACTIVE_MASK_SELECTOR = ".uploaded-thumbnail-inactive-mask"
COVER_CURRENT_SURFACE_SELECTOR = (
    "div.cover-plugin-preview div.default--ai-cover-layout"
)
COVER_EVALUATING_TEXT = "封面效果评估中"
GROUP_CHAT_POPOVER_SELECTOR = "div.d-popover.d-dropdown:visible"
GROUP_CHAT_OPTION_NAME_SELECTOR = ".item.custom-option .name"
QUOTE_NOTE_MODAL_SELECTOR = "div.d-modal.select-note-modal:visible"
QUOTE_NOTE_CARD_TITLE_SELECTOR = ".select-note-modal__note-grid .note-card__title"
QUOTE_NOTE_SELECTED_SELECTOR = ".quote-note-container__selected-text"


def _normalize_display_text(value: object) -> str:
    """Normalize rendered whitespace while preserving exact displayed wording."""
    return re.sub(r"\s+", " ", str(value)).strip()


async def _has_visible_exact_text(container, text: str) -> bool:
    matches = container.get_by_text(text, exact=True)
    for index in range(await matches.count()):
        try:
            if await matches.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _close_browser_resources(context, browser) -> None:
    """Best-effort cleanup that never masks the original upload error."""
    if context is not None:
        try:
            await context.close()
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"关闭浏览器上下文失败: {exc}"))
    if browser is not None:
        try:
            await browser.close()
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"关闭浏览器失败: {exc}"))


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


async def _wait_for_cover_entry(page: Page):
    """Wait for either the current edit entry or an older empty-cover entry."""
    deadline = monotonic() + COVER_ENTRY_TIMEOUT_MS / 1000
    while monotonic() < deadline:
        # Xiaohongshu can place a one-time PK-cover tour over this section.
        # Dismiss it before trying to hover or click the underlying controls.
        guide_confirm = page.get_by_text("我知道了", exact=True).first
        try:
            if await guide_confirm.count() and await guide_confirm.is_visible():
                await guide_confirm.click(force=True)
                await page.wait_for_timeout(500)
                xiaohongshu_logger.info(_msg("🖼️", "已关闭 PK 封面功能引导"))
        except Exception:
            pass

        for selector in COVER_ENTRY_SELECTORS:
            candidate = page.locator(selector).first
            if not await candidate.count():
                continue
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue

        # With the current AI-cover layout, "编辑封面" is only exposed while
        # hovering the generated first-frame tile.
        for selector in COVER_SURFACE_SELECTORS:
            surface = page.locator(selector).first
            if not await surface.count():
                continue
            try:
                if not await surface.is_visible():
                    continue
                await surface.hover()
                await page.wait_for_timeout(250)
                edit_entry = page.locator(COVER_ENTRY_SELECTORS[0]).first
                if await edit_entry.count() and await edit_entry.is_visible():
                    return edit_entry
            except Exception:
                continue
        await page.wait_for_timeout(500)
    raise TimeoutError("等待小红书封面入口超时")


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                _build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                )
            )
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium")
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(_build_xhs_creator_url("/login"))
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title[:20])

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            xiaohongshu_logger.warning(
                _msg("🏷️", f"标签数量 {len(self.tags)} 超过小红书上限 {max_tags}，只取前 {max_tags} 个: {self.tags[:max_tags]}")
            )
            self.tags = self.tags[:max_tags]

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 标签是可选增强项：等不到候选框就跳过该标签继续，不让整条发布因此失败。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                first_item = page.locator('#creator-editor-topic-container .item').first
                await first_item.wait_for(state="visible", timeout=4000)
                await first_item.click()
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("🏷️", f"话题『{tag}』未出现候选，跳过该标签继续发布: {exc}")
                )
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                continue

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        容错：任一步失败记 warning 跳过、继续发布，不中断。
        """
        source = (getattr(self, "repost_source", "") or "").strip()
        if not source:
            return
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败，跳过继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        group_chat: str | None = None,
        quote_note: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""
        self.group_chat = (group_chat or "").strip()
        self.quote_note = (quote_note or "").strip()

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        try:
            # The current page renders "编辑封面" once the first frame exists and
            # "设置封面" before that. Wait for either state instead of assuming
            # that the title field means cover extraction has finished.
            cover_entry = await _wait_for_cover_entry(page)
            current_surface = page.locator(COVER_CURRENT_SURFACE_SELECTOR).first
            previous_surface_style = None
            if await current_surface.count():
                previous_surface_style = await current_surface.get_attribute("style")
            await cover_entry.scroll_into_view_if_needed(timeout=5000)
            await cover_entry.click(force=True)

            # Keep every ambiguous locator inside the active cover modal. The
            # page also contains unrelated AI-cover upload buttons.
            modal = page.locator(COVER_MODAL_SELECTOR).first
            await modal.wait_for(state="visible", timeout=COVER_MODAL_TIMEOUT_MS)

            file_input = modal.locator(COVER_FILE_INPUT_SELECTOR).first
            try:
                await file_input.wait_for(state="attached", timeout=10000)
            except Exception:
                # Legacy editor: the image input is only attached after
                # switching from frame extraction to the upload tab.
                upload_tab = modal.get_by_text("上传封面", exact=True).first
                await upload_tab.wait_for(state="visible", timeout=10000)
                await upload_tab.click(force=True)
                file_input = modal.locator(COVER_FILE_INPUT_SELECTOR).first
                await file_input.wait_for(state="attached", timeout=10000)
            await file_input.set_input_files(thumbnail_path)

            preview = modal.locator(COVER_PREVIEW_SELECTOR).first
            await preview.wait_for(state="visible", timeout=COVER_IMAGE_TIMEOUT_MS)
            await page.wait_for_function(
                "img => img.complete && img.naturalWidth > 0",
                arg=await preview.element_handle(),
                timeout=COVER_IMAGE_TIMEOUT_MS,
            )

            # In the current editor, uploading only adds an inactive thumbnail.
            # It must be clicked before "完成" or Xiaohongshu keeps the video frame.
            uploaded_thumbnail = modal.locator(
                COVER_UPLOADED_THUMBNAIL_SELECTOR
            ).first
            uses_current_editor = bool(await uploaded_thumbnail.count())
            if uses_current_editor:
                await uploaded_thumbnail.wait_for(
                    state="visible", timeout=COVER_IMAGE_TIMEOUT_MS
                )
                await page.wait_for_function(
                    "button => !button.disabled",
                    arg=await uploaded_thumbnail.element_handle(),
                    timeout=COVER_IMAGE_TIMEOUT_MS,
                )
                await uploaded_thumbnail.click(force=True)
                inactive_mask = uploaded_thumbnail.locator(
                    COVER_UPLOADED_INACTIVE_MASK_SELECTOR
                ).first
                await inactive_mask.wait_for(
                    state="hidden", timeout=COVER_IMAGE_TIMEOUT_MS
                )

            complete = modal.get_by_role("button", name="完成", exact=True).first
            if not await complete.count():
                complete = modal.get_by_role(
                    "button", name="确定", exact=True
                ).first
            await complete.wait_for(state="visible", timeout=10000)
            await complete.click(force=True)

            await modal.wait_for(state="hidden", timeout=COVER_MODAL_TIMEOUT_MS)
            if uses_current_editor:
                if not previous_surface_style:
                    raise RuntimeError("无法读取设置前的小红书封面状态")
                await page.wait_for_function(
                    """([selector, previousStyle]) => {
                        const surface = document.querySelector(selector);
                        return surface && surface.getAttribute('style') !== previousStyle;
                    }""",
                    arg=[COVER_CURRENT_SURFACE_SELECTOR, previous_surface_style],
                    timeout=COVER_IMAGE_TIMEOUT_MS,
                )
                evaluating = page.get_by_text(
                    COVER_EVALUATING_TEXT, exact=True
                ).first
                if await evaluating.count():
                    await evaluating.wait_for(
                        state="hidden", timeout=COVER_IMAGE_TIMEOUT_MS
                    )
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            xiaohongshu_logger.error(
                _msg("🖼️", f"自定义封面设置失败，已停止发布：{exc}")
            )
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass
            raise RuntimeError("小红书自定义封面设置失败，已停止发布") from exc

    def _group_chat_popover(self, page: Page):
        return page.locator(GROUP_CHAT_POPOVER_SELECTOR).filter(
            has=page.get_by_text("我创建的群聊", exact=True)
        ).last

    async def _open_group_chat_popover(self, page: Page):
        """Open the group selector, including when another group is selected."""
        popover = self._group_chat_popover(page)
        placeholder = page.get_by_text("选择群聊", exact=True).first
        try:
            if await placeholder.count() and await placeholder.is_visible():
                await placeholder.click(force=True)
                await popover.wait_for(state="visible", timeout=5000)
                return popover
        except Exception:
            pass

        # Once a group is selected, Xiaohongshu replaces the placeholder with
        # its name. Probe visible selects and identify the right one by the
        # distinctive dropdown heading instead of relying on generated classes.
        selects = page.locator("#publish-container .d-select:visible")
        for index in range(await selects.count()):
            candidate = selects.nth(index)
            try:
                await candidate.click(force=True)
                await popover.wait_for(state="visible", timeout=800)
                return popover
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        raise RuntimeError("没有找到『选择群聊』控件")

    async def _has_selected_group_chat(self, page: Page, target: str) -> bool:
        selects = page.locator("#publish-container .d-select:visible")
        return await _has_visible_exact_text(selects, target)

    async def apply_group_chat(self, page: Page) -> bool:
        """Select one uniquely named group chat; warn and continue on failure."""
        target = self.group_chat
        if not target:
            return True
        try:
            if await self._has_selected_group_chat(page, target):
                xiaohongshu_logger.info(_msg("👥", f"群聊已经是『{target}』"))
                return True

            popover = await self._open_group_chat_popover(page)
            names = popover.locator(GROUP_CHAT_OPTION_NAME_SELECTOR)
            await names.first.wait_for(state="visible", timeout=5000)
            rendered_names = await names.all_inner_texts()
            matching_indexes = [
                index
                for index, name in enumerate(rendered_names)
                if _normalize_display_text(name) == target
            ]
            if len(matching_indexes) != 1:
                reason = "未找到" if not matching_indexes else "找到多个同名群聊"
                xiaohongshu_logger.warning(
                    _msg("⚠️", f"{reason}『{target}』，跳过群聊关联并继续发布")
                )
                await page.keyboard.press("Escape")
                return False

            option = names.nth(matching_indexes[0]).locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' custom-option ')][1]"
            )
            await option.click(force=True)
            await popover.wait_for(state="hidden", timeout=5000)
            if not await self._has_selected_group_chat(page, target):
                raise RuntimeError("选择后未能验证页面中的群聊名称")
            xiaohongshu_logger.success(_msg("👥", f"已关联群聊：{target}"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(
                _msg("⚠️", f"关联群聊『{target}』失败，跳过并继续发布: {exc}")
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _has_quoted_note(self, page: Page, target: str) -> bool:
        selected = page.locator(QUOTE_NOTE_SELECTED_SELECTOR)
        for rendered_text in await selected.all_inner_texts():
            titles = re.findall(r"《(.*?)》", rendered_text, flags=re.DOTALL)
            if any(_normalize_display_text(title) == target for title in titles):
                return True
        return False

    async def apply_quote_note(self, page: Page) -> bool:
        """Quote one uniquely titled own note; warn and continue on failure."""
        target = self.quote_note
        if not target:
            return True
        try:
            if await self._has_quoted_note(page, target):
                xiaohongshu_logger.info(_msg("🔗", f"已经引用笔记『{target}』"))
                return True

            trigger = page.get_by_text("引用笔记", exact=True).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                await trigger.click(force=True)
            except Exception:
                if not await _js_click_by_text(page, "引用笔记"):
                    raise RuntimeError("没有找到『引用笔记』控件")

            modal = page.locator(QUOTE_NOTE_MODAL_SELECTOR).last
            await modal.wait_for(state="visible", timeout=5000)
            own_notes_tab = modal.get_by_role(
                "button", name="我的笔记", exact=True
            ).first
            if await own_notes_tab.count():
                await own_notes_tab.click(force=True)
                await page.wait_for_timeout(300)

            titles = modal.locator(QUOTE_NOTE_CARD_TITLE_SELECTOR)
            await titles.first.wait_for(state="visible", timeout=8000)
            rendered_titles = await titles.all_inner_texts()
            matching_indexes = [
                index
                for index, title in enumerate(rendered_titles)
                if _normalize_display_text(title) == target
            ]
            if len(matching_indexes) != 1:
                reason = "未找到" if not matching_indexes else "找到多篇同名笔记"
                xiaohongshu_logger.warning(
                    _msg("⚠️", f"{reason}『{target}』，跳过引用并继续发布")
                )
                await page.keyboard.press("Escape")
                return False

            card = titles.nth(matching_indexes[0]).locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' note-card ')][1]"
            )
            await card.click(force=True)
            confirm = modal.get_by_role(
                "button", name="确认引用", exact=True
            ).first
            await confirm.wait_for(state="visible", timeout=5000)
            await confirm.click(force=True)
            await modal.wait_for(state="hidden", timeout=5000)
            if not await self._has_quoted_note(page, target):
                raise RuntimeError("确认后未能验证页面中的引用笔记标题")
            xiaohongshu_logger.success(_msg("🔗", f"已引用笔记：{target}"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(
                _msg("⚠️", f"引用笔记『{target}』失败，跳过并继续发布: {exc}")
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def apply_content_associations(self, page: Page) -> None:
        """Apply optional associations independently and never block publish."""
        for label, apply_association in (
            ("群聊", self.apply_group_chat),
            ("引用笔记", self.apply_quote_note),
        ):
            try:
                await apply_association(page)
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("⚠️", f"设置{label}时发生异常，跳过并继续发布: {exc}")
                )

    async def upload_video_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        upload_deadline = monotonic() + VIDEO_UPLOAD_TIMEOUT_SECONDS
        while monotonic() < upload_deadline:
            try:
                upload_input = await page.wait_for_selector('input.upload-input', timeout=3000)
                preview_new = await upload_input.query_selector(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]')
                if preview_new:
                    # 获取整个预览区域的文本，更鲁棒地判断上传状态
                    all_text = await preview_new.inner_text()
                    upload_success = any(keyword in all_text for keyword in ['上传成功', '分辨率', '重新上传', '编辑封面', '已上传', '已选择', '100%'])
                    
                    if not upload_success:
                        # 检查是否有特定的状态码或百分比
                        stage_elements = await preview_new.query_selector_all('div.stage')
                        for stage in stage_elements:
                            text_content = await page.evaluate('(element) => element.textContent', stage)
                            if '上传成功' in text_content or '分辨率' in text_content:
                                upload_success = True
                                break
                    
                    if upload_success:
                        xiaohongshu_logger.success(_msg("🥳", "视频已经传完啦"))
                        break
                    
                    if self.debug:
                        normalized_text = all_text.strip().replace("\n", " ")
                        xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {normalized_text}"))
                    xiaohongshu_logger.debug(_msg("🧍", "还没看到上传成功标识，小人继续等一会"))
                else:
                    # 尝试检查标题输入框是否已经出现，如果是，说明已经进入编辑状态
                    title_container = page.locator('input[placeholder*="填写标题"]')
                    if await title_container.count() > 0 and await title_container.is_visible():
                        xiaohongshu_logger.success(_msg("🥳", "虽然没看到预览区，但标题框出来了，小人继续"))
                        break
                    xiaohongshu_logger.debug(_msg("🧍", "还没拿到预览区域，小人继续等一会"))
            except Exception as e:
                xiaohongshu_logger.debug(_msg("😵", f"上传状态还没稳定下来，小人继续观察: {e}"))
            await asyncio.sleep(2)
        else:
            raise TimeoutError("等待小红书视频上传完成超时（15 分钟）")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        await self.apply_content_associations(page)

        # await self.set_location(page, "青岛市")

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        publish_deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
        while monotonic() < publish_deadline:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待小红书视频发布结果超时（5 分钟）")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        browser = None
        context = None

        try:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
            context = await browser.new_context(
                permissions=["geolocation"],
                storage_state=self.account_file,
            )
            context = await set_init_script(context)
            page = await context.new_page()
            await self.upload_video_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await _close_browser_resources(context, browser)

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        upload_deadline = monotonic() + VIDEO_UPLOAD_TIMEOUT_SECONDS
        while monotonic() < upload_deadline:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)
        else:
            raise TimeoutError("等待小红书图文上传完成超时（15 分钟）")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        publish_deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
        while monotonic() < publish_deadline:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待小红书图文发布结果超时（5 分钟）")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        browser = None
        context = None

        try:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
            context = await browser.new_context(
                permissions=["geolocation"],
                storage_state=self.account_file,
            )
            context = await set_init_script(context)
            page = await context.new_page()
            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await _close_browser_resources(context, browser)

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()
