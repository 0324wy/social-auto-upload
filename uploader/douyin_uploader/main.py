# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import base64
import inspect
import os
import sys
from pathlib import Path
from time import monotonic

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import douyin_logger

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
VIDEO_UPLOAD_TIMEOUT_SECONDS = 15 * 60
PUBLISH_TIMEOUT_SECONDS = 5 * 60
COVER_DIALOG_SELECTOR = "div.dy-creator-content-modal:visible"
COVER_UPLOAD_SELECTOR = ".semi-upload:visible:has(.semi-upload-drag-area-main-text)"
COVER_PREVIEW_SELECTOR = '[class*="cloudArea-"]:visible canvas.lower-canvas[class*="cloudImage-"]:visible'


class DouyinCustomCoverError(RuntimeError):
    """A requested custom cover was not verified; publishing must stop."""


def _same_cover_preview(first: str, second: str) -> bool:
    """Compare 32x32 RGB samples, allowing minor JPEG/canvas redraw differences."""
    if first == second:
        return True
    try:
        first_kind, first_size, first_data = first.split(":", 2)
        second_kind, second_size, second_data = second.split(":", 2)
        if first_kind != "canvas-rgb32" or second_kind != first_kind or first_size != second_size:
            return False
        left = base64.b64decode(first_data, validate=True)
        right = base64.b64decode(second_data, validate=True)
        if len(left) != 32 * 32 * 3 or len(right) != len(left):
            return False
        differences = [abs(a - b) for a, b in zip(left, right)]
        return sum(differences) / len(differences) <= 3 and sorted(differences)[int(len(differences) * 0.95)] <= 12
    except (ValueError, TypeError):
        return False


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _read_verify_code(code_file: str) -> str:
    if os.path.exists(code_file):
        with open(code_file, encoding="utf-8") as file_obj:
            return file_obj.read().strip()

    if not sys.stdin or not sys.stdin.isatty():
        return ""

    try:
        return (await asyncio.to_thread(input, "请输入抖音短信验证码（直接回车可稍后重试）: ")).strip()
    except (EOFError, OSError):
        return ""


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _native_click(page, locator) -> bool:
    """对元素做"真人级"点击：真实鼠标点中心 + 派发完整 pointer/mouse 事件序列。
    抖音身份验证组件（uc_verification_component）只认这整套事件，不认单纯 click。
    返回是否点击成功。"""
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if not box:
        try:
            await locator.click(timeout=8000)
            return True
        except Exception:
            return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await asyncio.sleep(0.15)
        await page.mouse.click(x, y)
        await asyncio.sleep(0.2)
        await page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return;
                const opts = {bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,view:window,pointerId:1,pointerType:'mouse',isPrimary:true,button:0,buttons:1};
                for (const t of ['pointerover','pointerenter','pointerdown','mousedown','pointerup','mouseup','click']) {
                    const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
                    try { el.dispatchEvent(new C(t, opts)); } catch(e){ try{ el.dispatchEvent(new MouseEvent(t,opts)); }catch(_){} }
                }
            }""",
            {"x": x, "y": y},
        )
        return True
    except Exception:
        return False


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


async def _close_browser_resources(context, browser) -> None:
    """Best-effort cleanup that never masks the original upload error."""
    if context is not None:
        try:
            await context.close()
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"关闭浏览器上下文失败: {exc}"))
    if browser is not None:
        try:
            await browser.close()
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"关闭浏览器失败: {exc}"))


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    use_headless = os.environ.get("DOUYIN_COOKIE_AUTH_HEADLESS", "true").lower() in ("1", "true", "yes")
    launch_kwargs = {"headless": use_headless, "channel": "chromium", "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
    for _attempt in range(3):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(storage_state=account_file)
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2500)  # 等页面稳定，避免瞬时跳转误判
                has_login = await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count()
                if "content/upload" in page.url and not has_login:
                    return True
            except Exception:
                pass
            finally:
                await browser.close()
    return False


async def douyin_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS, cdp_url: str | None = None):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await douyin_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless, cdp_url=cdp_url)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    # 等 SPA 加载完成（不只等"扫码登录"文字，否则抖音慢加载时 30s 就超时）。
    # 给 domcontentloaded 后足够时间让客户端 JS 注入登录卡。
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    # attached 状态：DOM 里出现即可，不要求 visible/渲染完整，避免 race
    await scan_login_tab.wait_for(state="attached", timeout=60000)

    # 新版抖音创作者中心 (single_tab + animate_qrcode_container) 不再用 aria-label="二维码"。
    # 按优先级兜底多个 selector，至少一个能命中即可。
    qrcode_selectors = [
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
    ]
    last_err: Exception | None = None
    for sel in qrcode_selectors:
        qrcode_img = page.locator(sel).first
        try:
            await qrcode_img.wait_for(state="attached", timeout=10000)
        except Exception as e:
            last_err = e
            continue
        src = await qrcode_img.get_attribute("src")
        if src:
            return src
        last_err = RuntimeError(f"selector {sel} 命中但 src 为空")

    raise RuntimeError(f"未获取到抖音登录二维码地址 (last_err={last_err})")


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    # 提取二维码 src 仅为了保存/终端显示；定位不到时不致命——有头浏览器里二维码可见，直接扫码即可
    try:
        qrcode_src = await _extract_douyin_qrcode_src(page)
    except Exception as exc:
        douyin_logger.warning(_msg("😵", f"没定位到二维码元素（{str(exc)[:50]}）——请直接在弹出的浏览器里扫码，小人继续等登录跳转"))
        return {"image_path": "", "image_data_url": ""}
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    # 登录后会跳到 creator-micro 下任意页（home/content 等）；登录页是 creator.douyin.com/ 根路径
    if "creator.douyin.com/creator-micro" not in page.url:
        return False

    login_markers = [
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
        page.get_by_role("img", name="二维码").first,
    ]

    for marker in login_markers:
        if not await marker.count():
            continue
        try:
            if await marker.is_visible():
                return False
        except Exception:
            continue

    return True


async def _wait_for_douyin_login(page: Page, account_file: str, qrcode_info: dict, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 100) -> dict:
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    original_url = page.url
    saw_2fa = False
    for _ in range(max_checks):
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        # URL 变化 + sessionid 未到位 → 二验流程，继续等
        if page.url != original_url and not await _is_douyin_login_completed(page):
            sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]')
            if await sms_input.count() > 0:
                if not saw_2fa:
                    douyin_logger.warning(_msg("⚠️", f"检测到抖音短信/安全二次验证，请在弹出的浏览器中手动输入。等待 sessionid ({_}/{max_checks})"))
                    saw_2fa = True
            await asyncio.sleep(poll_interval)
            continue

        expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired_box.count() and await expired_box.is_visible():
            douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await expired_box.click()
            await asyncio.sleep(1)
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)

async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 2,
    max_checks: int = 60,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
):
    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            should_close_context = False
        else:
            browser = await playwright.chromium.launch(headless=headless, channel="chromium")
            context = await browser.new_context()
            should_close_context = True
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/")
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                # 登录已通过"发布视频"确认成功、storage_state 刚从已登录浏览器抓下来，
                # 不再用 flaky 的浏览器重检（那正是导致成功被误判为失败的老 bug）。
                # 只轻量确认文件里有 sessionid。
                try:
                    import json as _json
                    _d = _json.load(open(account_file))
                    _has_sess = any(c.get("name") == "sessionid" and c.get("value") for c in _d.get("cookies", []))
                    if not _has_sess:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "抖音扫码流程结束，但 cookie 中无 sessionid",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                except Exception as _e:
                    douyin_logger.warning(_msg("⚠️", f"cookie 文件校验异常（忽略，按成功处理）: {_e}"))
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if should_close_context:
                await context.close()
            await browser.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill(title[:30])

        description_editor = page.locator('div.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=120000)
        await description_editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")

        # 先填正文描述，再填 #话题（此前 description 参数未被写入，导致抖音只有标签没有正文）
        if description and description.strip():
            await page.keyboard.type(description.strip())

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False

    async def set_self_declaration(self, page: Page, declaration: str) -> bool:
        """抖音「自主声明」：打开声明弹窗 → 单选声明类型 → 确定。

        真实弹窗（用户 F12 实测）：header「请选择声明类型（单选）」，选项为
        label.semi-radio 内 span.semi-radio-addon 文本，「内容由AI生成」与
        「内容为转载信息」「内容为个人观点或见解」等并列；底部 footer 的
        semi-button-primary =「确定」。

        入口/弹窗异步渲染；且填完话题后残留的 mention-wrapper/semi-portal 浮层会盖住入口，
        必须先清浮层再点。失败返回 False。

        Args:
            declaration: 声明类型文本（调用方显式传入）
        """
        try:
            # 清掉会遮挡入口的浮层（话题下拉/引导层），并让输入框失焦
            await self._clear_blocking_overlays(page)

            # 入口：点开声明弹窗（多个候选文案，native 仅作兜底）
            entry = None
            for etext in ["请选择自主声明", "请选择声明类型", "添加自主声明", "自主声明", "作品声明"]:
                cand = page.get_by_text(etext).first
                if await cand.count():
                    entry = cand
                    break
            if entry is not None:
                try:
                    await entry.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await entry.click(timeout=6000)
                except Exception:
                    await _native_click(page, entry)
                await page.wait_for_timeout(1200)

            # 弹窗：header「请选择声明类型（单选）」
            dialog = page.locator(".semi-modal-content").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                dialog = page.locator(".semi-modal-body").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                douyin_logger.warning(_msg("🧾", "自主声明弹窗未打开，跳过声明继续发布"))
                return False
            await dialog.first.wait_for(state="visible", timeout=6000)

            # 选项：label.semi-radio 内 span.semi-radio-addon 精确匹配
            option = dialog.locator("label.semi-radio").filter(
                has=page.locator(f'.semi-radio-addon:text-is("{declaration}")')
            ).first
            if await option.count() == 0:
                option = dialog.locator("label.semi-radio").filter(has_text=declaration).first
            if await option.count():
                try:
                    await option.click(timeout=6000)
                except Exception:
                    await _native_click(page, option)
            else:
                await dialog.get_by_text(declaration, exact=True).first.click(timeout=6000, force=True)
            await page.wait_for_timeout(400)

            # 确定：footer 的 primary 按钮
            confirm_btn = dialog.locator("button.semi-button-primary").filter(has_text="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = dialog.get_by_role("button", name="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = page.get_by_role("button", name="确定").first
            try:
                await confirm_btn.click(timeout=6000)
            except Exception:
                await _native_click(page, confirm_btn)
            try:
                await dialog.first.wait_for(state="hidden", timeout=6000)
            except Exception:
                pass
            douyin_logger.success(_msg("🧾", f"自主声明已选择「{declaration}」"))
            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🧾", f"自主声明设置失败，跳过该步骤继续发布：{exc}"))
            return False

    async def select_bgm(self, page: Page, bgm_name: str) -> bool:
        """为图文发布选择 BGM：可选增强功能，搜索无结果或异常均跳过不中断发布。"""
        try:
            # 点击「选择音乐」按钮
            music_entry = page.locator('text="选择音乐"').nth(1)
            if not await music_entry.count():
                music_entry = page.locator('text="选择音乐"').first
            await music_entry.wait_for(state="visible", timeout=10000)
            await music_entry.click()

            # 等待侧边栏出现并搜索
            sidesheet = page.locator(".semi-sidesheet-content").first
            await sidesheet.wait_for(state="visible", timeout=8000)
            search_input = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
            await search_input.wait_for(state="visible", timeout=5000)
            await search_input.fill(bgm_name)
            await search_input.press("Enter")

            # 等待搜索结果
            await asyncio.sleep(2)
            first_card = sidesheet.locator(".card-container-tmocjc").first
            try:
                await first_card.wait_for(state="visible", timeout=8000)
            except Exception:
                douyin_logger.warning(_msg("🎵", f"音乐「{bgm_name}」搜索结果为空，小人跳过"))
                await self._close_music_sidesheet(page)
                return False

            # 打印找到的音乐名称
            try:
                song_name_el = first_card.locator(".song-name-oRge4d").first
                if await song_name_el.count():
                    song_name = await song_name_el.inner_text()
                    douyin_logger.info(_msg("🎵", f"小人找到了: {song_name}"))
            except Exception:
                pass

            # JS 点击「使用」（按钮 visibility:hidden，普通 click 无效）
            apply_btn = first_card.locator(".apply-btn-LUPP0D").first
            await apply_btn.evaluate("el => el.click()")
            douyin_logger.info(_msg("🥳", f"BGM「{bgm_name}」已应用"))

            # 等待侧边栏关闭，超时则手动关闭
            try:
                await sidesheet.wait_for(state="hidden", timeout=5000)
            except Exception:
                await self._close_music_sidesheet(page)

            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🎵", f"添加 BGM 时出错，跳过该步骤继续发布：{exc}"))
            try:
                await self._close_music_sidesheet(page)
            except Exception:
                pass
            return False

    async def _close_music_sidesheet(self, page: Page) -> None:
        try:
            close_btn = page.locator(".semi-sidesheet-close").first
            if await close_btn.count() and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        collection_name: str | None = None,
        declaration: str | None = None,
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
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self._custom_covers_verified = False
        self._verified_cover_sources = {}
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""
        self.collection_name = collection_name
        self.declaration = declaration.strip() if declaration and declaration.strip() else None

    async def apply_self_declaration(self, page: Page) -> None:
        if not self.declaration:
            return
        if not await self.set_self_declaration(page, self.declaration):
            raise RuntimeError(f"自主声明「{self.declaration}」设置失败，拒绝继续发布")

    async def _clear_blocking_overlays(self, page: Page) -> None:
        """清除会拦截点击的浮层：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)
        及其所在 semi-portal、其它非模态 semi-portal(tooltip/popover)、shepherd 引导层，
        并让当前输入框失焦。合集/声明下拉自身的 portal 是"点开后"才创建，故此处清理不误伤。

        根因见 recorder.log 2026-08-10 05:31：apply_collection 点合集下拉时，
        publish-mention-wrapper / semi-portal 拦截 pointer events → click 超时 → 归集被跳过。
        """
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
                    document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove());
                    document.querySelectorAll('[class*="mention-wrapper"]').forEach(e=>{ const p=e.closest('.semi-portal'); (p||e).remove(); });
                    // 关闭残留的非模态 Semi 浮层 portal（保留模态框，如声明弹窗）
                    document.querySelectorAll('.semi-portal').forEach(e=>{ if(!e.querySelector('.semi-modal, .semi-modal-content')) e.remove(); });
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(400)

    async def apply_collection(self, page: Page) -> None:
        """在发布表单页"添加合集"区选择目标合集（Semi Design select，字节组件库）。

        结构与快手（Ant Design）不同：合集名是纯文本 span.option-title-*，无 label 属性，
        用文本精确匹配。触发器用专属 class .select-collection-* 定位（页面唯一，第一级
        "合集/系列"类型下拉与此无关，不会误选）。找不到匹配合集时按 Escape 收起下拉，
        保持未选状态直接发布（界面允许留空，不阻断主发布流程）。
        """
        if not self.collection_name:
            return
        try:
            # 关键修复：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)及 semi-portal
            # 浮层盖在"添加合集"下拉上，普通 click 全点在遮罩上→超时→归集被跳过。先清浮层再点。
            await self._clear_blocking_overlays(page)

            trigger = page.locator('[class*="select-collection-"]').first
            if await trigger.count() == 0:
                douyin_logger.warning(_msg("😵", "未找到\"添加合集\"下拉框，跳过归集"))
                return
            selection = trigger.locator(".semi-select-selection")
            try:
                await selection.click(timeout=5000)
            except Exception:
                await self._clear_blocking_overlays(page)
                await _native_click(page, selection)
            await page.wait_for_timeout(800)

            option = page.locator(".semi-select-option.collection-option").filter(
                has=page.locator(f'[class*="option-title-"]:text-is("{self.collection_name}")')
            )
            if await option.count() == 0:
                douyin_logger.warning(
                    _msg("😵", f"合集下拉框未找到「{self.collection_name}」，跳过归集，保持未选状态")
                )
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return

            try:
                await option.first.click(timeout=5000)
            except Exception:
                await _native_click(page, option.first)
            await page.wait_for_timeout(500)
            douyin_logger.success(_msg("🥳", f"已选择合集：{self.collection_name}"))
        except Exception as exc:
            douyin_logger.warning(_msg("😵", f"选择合集失败，跳过归集继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _submit_sms_verify_code(self, page: Page, sms_input, code: str, code_file: str) -> bool:
        douyin_logger.info(_msg("✍️", f"已获取验证码，准备填入: {code}"))
        await sms_input.click()
        await sms_input.fill(code)
        douyin_logger.info(_msg("✅", "验证码已填入输入框"))
        await page.wait_for_timeout(500)

        verify_btn = page.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")').first
        if await verify_btn.count() and await verify_btn.is_visible():
            try:
                await verify_btn.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(force)"))
            except Exception:
                await page.eval_on_selector('div.uc-ui-verify_sms-verify_button', 'el => el.click()')
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(JS)"))
        else:
            verify_by_text = page.get_by_text("验证", exact=True).first
            if await verify_by_text.count():
                await verify_by_text.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(text)"))
            else:
                douyin_logger.warning(_msg("⚠️", "未找到验证按钮，尝试按Enter"))
                await page.keyboard.press("Enter")

        if os.path.exists(code_file):
            os.remove(code_file)
            douyin_logger.info(_msg("🧹", "验证码文件已清理"))

        await page.wait_for_timeout(3000)
        douyin_logger.info(_msg("🔄", "验证码处理完成，继续发布流程"))
        return True

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    async def handle_upload_error(self, page):
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def handle_auto_video_cover(self, page):
        if self.thumbnail_landscape_path or self.thumbnail_portrait_path:
            await self._assert_custom_covers_ready(page)
            return False
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def _assert_custom_covers_ready(self, page: Page) -> None:
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return
        if not self._custom_covers_verified:
            raise DouyinCustomCoverError("自定义封面尚未验证保存成功，拒绝继续发布")
        if await page.locator(COVER_DIALOG_SELECTOR).count():
            raise DouyinCustomCoverError("自定义封面编辑器仍然打开，拒绝继续发布")
        warning = page.get_by_text("请设置封面后再发布").first
        if await warning.count() and await warning.is_visible():
            raise DouyinCustomCoverError("平台提示自定义封面未设置成功，拒绝替换为推荐封面")
        for orientation, expected_src in self._verified_cover_sources.items():
            snapshot = await self._saved_cover_snapshot(page, orientation)
            if snapshot.get("src") != expected_src or not snapshot.get("loaded"):
                raise DouyinCustomCoverError(f"已保存的自定义{orientation}封面预览发生变化或尚未加载，拒绝继续发布")

    async def _saved_cover_snapshot(self, page: Page, orientation: str) -> dict:
        label = "竖封面3:4" if orientation == "portrait" else "横封面4:3"
        card = page.locator('[class^="coverControl-"]').filter(has_text=label)
        if await card.count() != 1:
            raise DouyinCustomCoverError(f"未找到唯一的已保存封面预览「{label}」")
        image = card.locator("img")
        if await image.count() != 1:
            raise DouyinCustomCoverError(f"已保存封面预览「{label}」没有唯一图片")
        return await image.evaluate(
            """img => ({src: img.currentSrc || img.src, loaded: img.complete && img.naturalWidth > 0 && img.naturalHeight > 0,
                        width: img.naturalWidth, height: img.naturalHeight})"""
        )

    async def _wait_for_saved_covers(self, page: Page, baselines: dict[str, str]) -> dict[str, str]:
        # Closing the editor starts an asynchronous cover job. Only the main form's
        # requested cards, not the phone mockup or AI recommendations, prove it has completed.
        for _ in range(120):
            saved = {}
            for orientation, previous_src in baselines.items():
                snapshot = await self._saved_cover_snapshot(page, orientation)
                src = snapshot.get("src")
                if src and src != previous_src and snapshot.get("loaded"):
                    saved[orientation] = src
            if len(saved) == len(baselines):
                return saved
            await page.wait_for_timeout(500)
        missing = ", ".join(orientation for orientation in baselines if orientation not in saved)
        raise DouyinCustomCoverError(f"封面保存后 60 秒内未能确认主表单图片更新并加载（{missing}），拒绝继续发布")

    async def _open_cover_editor(self, page: Page):
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )
        cover_area = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await cover_area.count():
            cover_area = page.locator('[class*="cover"]').first
        cover = page.locator(COVER_DIALOG_SELECTOR).first
        try:
            await cover_area.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        for _ in range(5):
            trigger = cover_area
            try:
                await cover_area.hover(force=True)
            except Exception:
                pass
            for label in ["编辑封面", "选择封面", "设置封面"]:
                candidate = page.get_by_text(label, exact=True).first
                if await candidate.count() and await candidate.is_visible():
                    trigger = candidate
                    break
            await _native_click(page, trigger)
            try:
                await cover.wait_for(state="visible", timeout=5000)
                return cover
            except Exception:
                continue
        raise DouyinCustomCoverError("无法打开自定义封面编辑器，拒绝继续发布")

    async def _select_cover_tab(self, page: Page, cover, label: str) -> None:
        tab = cover.get_by_text(label, exact=True).first
        if not await tab.count() or not await tab.is_visible():
            raise DouyinCustomCoverError(f"未找到自定义封面标签「{label}」")
        for _ in range(3):
            try:
                await tab.click(timeout=3000)
            except Exception:
                await _native_click(page, tab)
            await page.wait_for_timeout(500)
            selected = await tab.evaluate(
                r"""el => {
                    for (let node = el, depth = 0; node && depth < 4; node = node.parentElement, depth++) {
                        if (node.getAttribute('aria-selected') === 'true' || node.getAttribute('data-state') === 'active') return true;
                        const cls = String(node.className || '');
                        if (/(^|[-_\s])(?:active|selected)(?:[-_\s]|$)|(?:Active|Selected)(?:[-_\s]|$)/.test(cls)) return true;
                        if (node.getAttribute('role') === 'tab') return false;
                    }
                    return false;
                }"""
            )
            if selected:
                return
            await _native_click(page, tab)
        raise DouyinCustomCoverError(f"无法确认自定义封面标签「{label}」已激活")

    async def _loaded_cover_previews(self, cover, orientation: str) -> set[str]:
        # Verified creator DOM: the editable image is the lower Fabric canvas in cloudArea.
        # AI/gallery images, the upper interaction canvas and the small phone previews are not evidence.
        canvas = cover.locator(COVER_PREVIEW_SELECTOR)
        count = await canvas.count()
        if count == 0:
            return set()
        if count != 1:
            raise DouyinCustomCoverError(f"自定义{orientation}封面的主编辑画布不唯一")
        signature = await canvas.evaluate(
            """canvas => {
                if (!canvas.width || !canvas.height) return '';
                try {
                    const sample = document.createElement('canvas');
                    sample.width = sample.height = 32;
                    const context = sample.getContext('2d');
                    context.drawImage(canvas, 0, 0, 32, 32);
                    const pixels = context.getImageData(0, 0, 32, 32).data;
                    const minimum = [255, 255, 255], maximum = [0, 0, 0];
                    let rgb = '', painted = 0;
                    for (let i = 0; i < pixels.length; i += 4) {
                        if (pixels[i + 3] > 0) painted++;
                        for (let channel = 0; channel < 3; channel++) {
                            const value = pixels[i + channel];
                            minimum[channel] = Math.min(minimum[channel], value);
                            maximum[channel] = Math.max(maximum[channel], value);
                            rgb += String.fromCharCode(value);
                        }
                    }
                    // A transparent or uniformly blank loading canvas is not artwork evidence.
                    if (painted < 32 || Math.max(...maximum.map((value, channel) => value - minimum[channel])) < 16) return '';
                    return `canvas-rgb32:${canvas.width}x${canvas.height}:${btoa(rgb)}`;
                } catch (_) { return ''; }
            }"""
        )
        return {signature} if signature else set()

    async def _cover_finish_enabled(self, cover) -> bool:
        button = cover.get_by_role("button", name="完成", exact=True).first
        if not await button.count() or not await button.is_visible() or not await button.is_enabled():
            return False
        return (
            await button.get_attribute("aria-disabled") != "true"
            and "semi-button-disabled" not in (await button.get_attribute("class") or "")
        )

    async def _wait_for_cover_preview(self, page, cover, orientation, *, baseline=None, expected=None) -> set[str]:
        previous_matches = set()
        for _ in range(60):
            for message in ["上传失败", "图片上传失败", "图片处理失败"]:
                failure = cover.get_by_text(message, exact=True).first
                if await failure.count() and await failure.is_visible():
                    raise DouyinCustomCoverError(f"自定义{orientation}封面处理失败：{message}")
            previews = await self._loaded_cover_previews(cover, orientation)
            if expected is not None:
                matches = {preview for preview in previews if any(_same_cover_preview(preview, wanted) for wanted in expected)}
            else:
                matches = {preview for preview in previews if not any(_same_cover_preview(preview, previous) for previous in baseline or set())}
            if matches and await self._cover_finish_enabled(cover):
                stable = {preview for preview in matches if any(_same_cover_preview(preview, previous) for previous in previous_matches)}
                if stable:
                    return stable
                previous_matches = matches
            else:
                previous_matches = set()
            await page.wait_for_timeout(500)
        raise DouyinCustomCoverError(f"未能验证自定义{orientation}封面的已加载预览或完成按钮状态")

    async def _upload_custom_cover(self, page, cover, label, orientation, path) -> set[str]:
        await self._select_cover_tab(page, cover, label)
        # Only the visible real cover slot has main-text. Never use the AI reference slot or an arbitrary last input.
        slot = cover.locator(COVER_UPLOAD_SELECTOR)
        if await slot.count() != 1:
            raise DouyinCustomCoverError(f"「{label}」未找到唯一可见的真实封面上传槽")
        upload = slot.locator("input.semi-upload-hidden-input").first
        await upload.wait_for(state="attached", timeout=5000)
        baseline = await self._loaded_cover_previews(cover, orientation)
        # The site's onChange handler clears input.files. Record the selected name before
        # React's handler runs; the JSHandle also survives replacement of the input node.
        selection = await upload.evaluate_handle(
            """el => {
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
            }"""
        )
        try:
            await upload.set_input_files(path)
            selected_file = await selection.evaluate("state => state.name")
        finally:
            try:
                await selection.evaluate("state => state.cleanup()")
            finally:
                await selection.dispose()
        if selected_file != Path(path).name:
            raise DouyinCustomCoverError(f"「{label}」未接受指定的封面文件")
        previews = await self._wait_for_cover_preview(page, cover, orientation, baseline=baseline)
        douyin_logger.info(_msg("🖼️", f"「{label}」指定文件已上传，预览已加载"))
        return previews

    async def _save_custom_covers(self, page, cover) -> None:
        for _ in range(4):
            if not await self._cover_finish_enabled(cover):
                raise DouyinCustomCoverError("自定义封面完成按钮不可用，拒绝继续发布")
            finish = cover.get_by_role("button", name="完成", exact=True).first
            try:
                await finish.click(timeout=4000)
            except Exception:
                await _native_click(page, finish)
            try:
                await cover.wait_for(state="hidden", timeout=3000)
                return
            except Exception:
                pass
            # Only explicit cover completion confirmations are accepted; Escape would cancel, not save.
            confirmation = page.locator(".semi-modal-content:visible").filter(has_text="封面")
            for name in ["确定", "确认", "仍然完成", "仍要完成"]:
                button = confirmation.get_by_role("button", name=name, exact=True).first
                if await button.count() and await button.is_visible():
                    await _native_click(page, button)
                    break
            try:
                await cover.wait_for(state="hidden", timeout=3000)
                return
            except Exception:
                await _native_click(page, finish)
                try:
                    await cover.wait_for(state="hidden", timeout=3000)
                    return
                except Exception:
                    pass
        raise DouyinCustomCoverError("自定义封面点击完成后未能确认保存关闭，拒绝继续发布")

    async def set_thumbnail(self, page: Page):
        self._custom_covers_verified = False
        self._verified_cover_sources = {}
        requested = [
            ("设置竖封面", "portrait", self.thumbnail_portrait_path),
            ("设置横封面", "landscape", self.thumbnail_landscape_path),
        ]
        requested = [item for item in requested if item[2]]
        if not requested:
            return
        try:
            baselines = {}
            for _, orientation, _ in requested:
                snapshot = await self._saved_cover_snapshot(page, orientation)
                baselines[orientation] = snapshot.get("src", "")
            cover = await self._open_cover_editor(page)
            verified = []
            for label, orientation, path in requested:
                previews = await self._upload_custom_cover(page, cover, label, orientation, path)
                verified.append((label, orientation, previews))
            # Revisit both tabs before saving: the second upload must not have replaced the first tab's cover.
            for label, orientation, previews in verified:
                await self._select_cover_tab(page, cover, label)
                await self._wait_for_cover_preview(page, cover, orientation, expected=previews)
            await self._save_custom_covers(page, cover)
            self._verified_cover_sources = await self._wait_for_saved_covers(page, baselines)
            self._custom_covers_verified = True
            await self._assert_custom_covers_ready(page)
        except Exception as exc:
            self._custom_covers_verified = False
            self._verified_cover_sources = {}
            if isinstance(exc, DouyinCustomCoverError):
                raise
            raise DouyinCustomCoverError(f"自定义封面设置失败，拒绝继续发布：{exc}") from exc
        douyin_logger.info(_msg("🥳", "所有指定封面均已验证预览并通过完成按钮保存；发布前检查通过"))


    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        browser = None
        context = None
        try:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium", args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                storage_state=f"{self.account_file}",
                permissions=["geolocation"],
            )
            context = await set_init_script(context)

            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
            douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
            douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            # ── 进入页面后可能弹身份验证（短信验证码）或被踢到登录页 ──
            await page.wait_for_timeout(2000)

            # 确认已经在上传页（非登录页），再找上传 input
            # 用更精确的选择器避免匹配到登录表单的 input
            upload_input = page.locator("input.upload-btn-input, div[class^='container'] input[accept]").first
            if not await upload_input.count():
                # 兜底：排除登录页的 input
                upload_input = page.locator("div[class^='container'] input[type='file'], div[class^='container'] input.upload-input").first
            if not await upload_input.count():
                # 最终兜底
                upload_input = page.locator("div[class^='container'] input").first
            await upload_input.wait_for(state="attached", timeout=60000)
            await upload_input.set_input_files(self.file_path)

            upload_deadline = monotonic() + VIDEO_UPLOAD_TIMEOUT_SECONDS
            while monotonic() < upload_deadline:
                try:
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
                        timeout=3000,
                    )
                    douyin_logger.info(_msg("🥳", "已经进入 version_1 发布页面"))
                    break
                except Exception:
                    try:
                        await page.wait_for_url(
                            "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                            timeout=3000,
                        )
                        douyin_logger.info(_msg("🥳", "已经进入 version_2 发布页面"))
                        break
                    except Exception:
                        douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                        await asyncio.sleep(0.5)
            else:
                raise TimeoutError("等待抖音进入视频发布页面超时（15 分钟）")

            await asyncio.sleep(1)
            douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
            await self.fill_title_and_description(page, self.title, self.desc, self.tags)
            douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

            while monotonic() < upload_deadline:
                try:
                    number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                    if number > 0:
                        douyin_logger.success(_msg("🥳", "视频已经传完啦"))
                        break
                    douyin_logger.info(_msg("🏃", "小人正在努力上传视频"))
                    await asyncio.sleep(2)
                    if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                        douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                        await self.handle_upload_error(page)
                except Exception:
                    douyin_logger.debug(_msg("🧍", "小人还在等视频上传完成"))
                    await asyncio.sleep(2)
            else:
                raise TimeoutError("等待抖音视频上传完成超时（15 分钟）")

            if self.productLink and self.productTitle:
                douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
                await self.set_product_link(page, self.productLink, self.productTitle)
                douyin_logger.info(_msg("🥳", "商品链接设置完成"))

            # 只有调用方明确传入声明时才操作声明区域。
            await self.apply_self_declaration(page)

            # 先归集：此时尚未打开封面弹窗，避免 dy-creator-content-portal 封面浮层拦截合集下拉
            # （实测：封面弹窗在 headless 下常滞留"检测中"未关闭，会盖住"添加合集"下拉）
            await self.apply_collection(page)

            # 再设封面（放最后，关掉弹窗，避免残留浮层挡住发布按钮）
            await self.set_thumbnail(page)

            third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
            if await page.locator(third_part_element).count():
                if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                    await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

            if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
                await self.set_schedule_time_douyin(page, self.publish_date)

            sms_prompt_logged = False
            publish_deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
            while monotonic() < publish_deadline:
                try:
                    # 移除会拦截发布按钮点击的新手引导/话题下拉浮层
                    await page.evaluate(
                        "() => { document.querySelectorAll('.shepherd-element, .shepherd-modal-overlay-container, [class*=\"mention-wrapper\"]').forEach(e => e.remove()); }"
                    )
                    # 检测并处理短信验证码弹窗
                    sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]').first
                    if await sms_input.count() and await sms_input.is_visible():
                        douyin_logger.warning(_msg("📱", "检测到短信验证码弹窗"))
                        # 点击「获取验证码」按钮（仅首次）
                        get_code_btn = page.get_by_text("获取验证码").first
                        if await get_code_btn.count() and await get_code_btn.is_visible():
                            await get_code_btn.click()
                            douyin_logger.info(_msg("📤", "已点击「获取验证码」，请查看手机短信"))
                        code_file = os.path.join(BASE_DIR, "verify_code.txt")
                        code = await _read_verify_code(code_file)
                        if code:
                            sms_prompt_logged = False
                            await self._submit_sms_verify_code(page, sms_input, code, code_file)
                        elif not sms_prompt_logged:
                            douyin_logger.warning(_msg("⏳", f"等待验证码输入；可在交互终端直接输入，或写入文件: {code_file}"))
                            sms_prompt_logged = True

                    # ── 正常发布流程 ──
                    await self._assert_custom_covers_ready(page)
                    publish_button = page.get_by_role("button", name="发布", exact=True)
                    if await publish_button.count():
                        await publish_button.click(force=True)
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/manage**",
                        timeout=3000,
                    )
                    douyin_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                    break
                except DouyinCustomCoverError:
                    raise
                except Exception:
                    await self.handle_auto_video_cover(page)
                    douyin_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                    if self.debug:
                        await page.screenshot(full_page=True)
                    await asyncio.sleep(0.5)
            else:
                raise TimeoutError("等待抖音视频发布结果超时（5 分钟）")

            await context.storage_state(path=self.account_file)
            douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
            await asyncio.sleep(2)
        finally:
            await _close_browser_resources(context, browser)

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        bgm: str = "",
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
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.bgm = bgm or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if len(self.title) > 20:
            raise ValueError(f"标题不能超过20字符，当前: {len(self.title)}字符")

        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        note_len = len(self.note) if self.note else 0
        if note_len > 1000:
            raise ValueError(f"正文不能超过1000字符，当前: {note_len}字符")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        upload_deadline = monotonic() + VIDEO_UPLOAD_TIMEOUT_SECONDS
        while monotonic() < upload_deadline:
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待抖音图文上传完成超时（15 分钟）")

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        title_len = len(self.title) if self.title else 0
        tags_text = " ".join(f"#{t}" for t in self.tags) if self.tags else ""
        desc_and_tags_len = len(self.note or "") + (len(tags_text) + 2 if self.tags else 0)
        douyin_logger.info(_msg("📝", f"标题总字数: {title_len}，描述+话题总字数: {desc_and_tags_len}"))
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.bgm:
            await self.select_bgm(page, self.bgm)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        publish_deadline = monotonic() + PUBLISH_TIMEOUT_SECONDS
        while monotonic() < publish_deadline:
            try:
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(
                    "**/creator-micro/content/manage?enter_from=publish**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待抖音图文发布结果超时（5 分钟）")

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        browser = None
        context = None
        try:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium", args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                storage_state=f"{self.account_file}",
                permissions=["geolocation"],
            )
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
            await asyncio.sleep(2)
        finally:
            await _close_browser_resources(context, browser)

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
