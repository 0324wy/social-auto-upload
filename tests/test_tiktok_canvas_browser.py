"""Offline Chrome regression; run with SAU_RUN_BROWSER_TESTS=1. No account or network."""
import base64
import os
import unittest
from pathlib import Path

from patchright.async_api import async_playwright
from uploader.tk_uploader import main_chrome as tk


@unittest.skipUnless(os.getenv('SAU_RUN_BROWSER_TESTS') == '1', 'opt-in offline Chrome canvas regression')
class TikTokCanvasBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(channel='chrome', headless=True)
            self.page = await self.browser.new_page()
            await self.page.route('**/*', lambda route: route.abort())
            await self.page.set_content('''<div id="editor">
                <div class="StageCanvas__screen"><canvas width="1152" height="648" style="width:460px;height:259px"></canvas></div>
                <canvas class="CoverEditorProfilePreview__canvas" width="486" height="648" style="width:76px;height:101px"></canvas>
                <img class="ImageUpload__preview" style="width:39px;height:52px">
            </div>''')
        except BaseException:
            await self.playwright.stop()
            raise

    async def asyncTearDown(self):
        try:
            await self.browser.close()
        finally:
            await self.playwright.stop()

    async def test_native_area_average_matches_multistage_scaling_and_rejects_wrong_art(self):
        generated = await self.page.evaluate('''() => {
            const source = document.createElement('canvas');
            source.width = 1080; source.height = 1440;
            const context = source.getContext('2d');
            const pixels = context.createImageData(source.width, source.height);
            for (let y = 0; y < source.height; y++) {
                for (let x = 0; x < source.width; x++) {
                    const i = (y * source.width + x) * 4;
                    const value = (x + y) % 2 ? 230 : 30;
                    pixels.data[i] = value; pixels.data[i + 1] = value; pixels.data[i + 2] = value; pixels.data[i + 3] = 255;
                }
            }
            context.putImageData(pixels, 0, 0);
            context.fillStyle = '#d8b095';
            context.beginPath(); context.ellipse(540, 510, 250, 320, 0, 0, Math.PI * 2); context.fill();
            context.fillStyle = '#192b48';
            context.fillRect(190, 880, 700, 300);
            context.fillStyle = '#ffffff'; context.font = 'bold 74px sans-serif';
            context.fillText('LISTEN EVERY DAY', 90, 1260);
            context.fillStyle = '#ffd84b'; context.font = 'bold 62px sans-serif';
            context.fillText('A high-frequency title', 170, 1340);

            const profile = document.querySelector('canvas.CoverEditorProfilePreview__canvas');
            const profileContext = profile.getContext('2d');
            profileContext.imageSmoothingEnabled = true;
            profileContext.imageSmoothingQuality = 'high';
            profileContext.drawImage(source, 0, 0, profile.width, profile.height);
            const stage = document.querySelector('.StageCanvas__screen canvas');
            const stageContext = stage.getContext('2d');
            stageContext.fillStyle = '#000'; stageContext.fillRect(0, 0, stage.width, stage.height);
            stageContext.drawImage(profile, 333, 0);
            const png = source.toDataURL('image/png');
            document.querySelector('img.ImageUpload__preview').src = png;
            function lowSample(image) {
                const canvas = document.createElement('canvas'); canvas.width = canvas.height = 32;
                const c = canvas.getContext('2d'); c.imageSmoothingEnabled = true; c.imageSmoothingQuality = 'low';
                c.drawImage(image, 0, 0, 32, 32);
                return Array.from(c.getImageData(0, 0, 32, 32).data).filter((_v, i) => i % 4 !== 3);
            }
            return {png, lowDirect: lowSample(source), lowStaged: lowSample(profile)};
        }''')
        # This pattern exposes direct low-quality 1080->32 aliasing versus the already
        # downsampled 486->32 pixels; the unchanged 3/12 comparator must reject it.
        self.assertFalse(tk._same_image_sample(generated['lowDirect'], generated['lowStaged']))
        source_bytes = base64.b64decode(generated['png'].split(',', 1)[1])
        editor = self.page.locator('#editor')
        expected = await tk._image_sample(editor, source_bytes)
        app = tk.TiktokVideo('Test', '/tmp/unused.mp4', [], 0, '/tmp/unused.json')
        actual = await app._editor_cover_sample(editor, expected)
        self.assertIsNotNone(actual, 'Native area-averaged direct and multistage samples should satisfy the original 3/12 thresholds')
        self.assertTrue(tk._same_image_sample(actual, expected))
        # Stable repeated extraction must remain comparable without weakening thresholds.
        self.assertEqual(await app._editor_cover_sample(editor, expected), actual)
        # Keep the matching upload thumbnail in place; a different final profile must
        # still fail, proving neither that thumbnail nor the letterboxed stage substitutes.
        await editor.locator('canvas.CoverEditorProfilePreview__canvas').evaluate('''canvas => {
            const c = canvas.getContext('2d'); c.fillStyle = '#168eed'; c.fillRect(0, 0, canvas.width, canvas.height);
        }''')
        self.assertIsNone(await app._editor_cover_sample(editor, expected))

    @unittest.skipUnless(os.getenv('SAU_TIKTOK_SAMPLE_SOURCE') and os.getenv('SAU_TIKTOK_SAMPLE_PROFILE'), 'optional captured real source/profile pair')
    async def test_captured_native_profile_and_saved_image_match_the_real_source(self):
        source = Path(os.environ['SAU_TIKTOK_SAMPLE_SOURCE']).read_bytes()
        profile = Path(os.environ['SAU_TIKTOK_SAMPLE_PROFILE']).read_bytes()
        await self.page.evaluate("""async encoded => {
            const image = new Image(); image.src = 'data:image/png;base64,' + encoded; await image.decode();
            const canvas = document.querySelector('canvas.CoverEditorProfilePreview__canvas');
            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
            canvas.getContext('2d').drawImage(image, 0, 0);
            const card = document.createElement('div'); card.className = 'cover-container';
            image.className = 'cover-image';
            image.style.cssText = 'width:132px;height:176px;border-radius:18px;object-fit:cover';
            card.appendChild(image); document.body.appendChild(card);
        }""", base64.b64encode(profile).decode('ascii'))
        editor = self.page.locator('#editor')
        expected = await tk._image_sample(editor, source)
        app = tk.TiktokVideo('Test', '/tmp/unused.mp4', [], 0, '/tmp/unused.json')
        actual = await app._editor_cover_sample(editor, expected)
        self.assertIsNotNone(actual)
        differences = sorted(abs(a - b) for a, b in zip(actual, expected))
        print('Captured profile area error:', sum(differences) / len(differences), 'p95=', differences[int(len(differences) * .95)])
        self.assertTrue(tk._same_image_sample(actual, expected))
        card = self.page.locator('.cover-container')
        saved = await app._saved_cover_sample(card)
        self.assertTrue(tk._same_image_sample(saved, expected))
        # Publication guard must still catch a later replacement of the saved cover.
        app.thumbnail_path = '/tmp/requested-cover.jpg'
        app.cover_verified = True
        app._verified_cover_sample = saved
        app.locator_base = self.page.locator('body')
        await card.locator('img').evaluate("""async image => {
            const canvas = document.createElement('canvas'); canvas.width = 486; canvas.height = 648;
            const context = canvas.getContext('2d'); context.fillStyle = '#dc183f'; context.fillRect(0, 0, 486, 648);
            image.src = canvas.toDataURL(); await image.decode();
        }""")
        with self.assertRaisesRegex(RuntimeError, 'changed before publication'):
            await app._assert_custom_cover()


if __name__ == '__main__':
    unittest.main()
