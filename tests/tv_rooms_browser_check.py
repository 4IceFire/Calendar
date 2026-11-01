"""Real-auth, hardware-blocked room editor and read-only diagnostic checks."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from tv_browser_check import capture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--project', default='all')
    args = parser.parse_args()
    ready = json.loads(Path(args.ready_file).read_text()); base = ready['url']
    assert ready['fixture'] == 'tdeck-tv-isolated' and urlparse(base).hostname == '127.0.0.1'
    output = Path(args.evidence_dir); output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        for name, engine, device in [('chromium', 'chromium', None), ('firefox', 'firefox', None), ('webkit', 'webkit', None), ('mobile-chromium', 'chromium', 'Pixel 7'), ('mobile-webkit', 'webkit', 'iPhone 13')]:
            if args.project not in ('all', name): continue
            paths = {'chromium': 'chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell', 'firefox': 'firefox-1538/firefox/firefox', 'webkit': 'webkit-2336/pw_run.sh'}
            browser = getattr(p, engine).launch(headless=True, executable_path=str(Path.home() / '.cache/ms-playwright' / paths[engine]))
            context = browser.new_context(**(p.devices[device] if device else {'viewport': {'width': 1920, 'height': 1100}}))
            page = context.new_page(); errors = []; console_errors = []; failed_responses = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
            page.on('response', lambda response: failed_responses.append({'path': urlparse(response.url).path, 'status': response.status}) if response.status >= 400 else None)
            assert context.request.get(base + '/__tv_fixture').json()['fixture'] == 'tdeck-tv-isolated'
            assert context.request.post(base + '/__tv_fixture/reset').ok
            page.goto(base + '/login?next=/config/tvs'); page.locator('[name=username]').fill('admin-tv'); page.locator('[name=password]').fill('fixture-password'); page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url(lambda url: '/login' not in url)
            before = context.request.get(base + '/__tv_fixture').json()['commands']
            page.get_by_role('link', name='Room pictures and TV positions', exact=True).click()
            expect(page.locator('#tv-editor-room')).to_be_visible()
            page.select_option('#tv-editor-room', 'ground')
            # Local fixture image, rendered and uploaded as a real multipart body.
            png = page.evaluate("""() => {const c=document.createElement('canvas');c.width=960;c.height=540;const x=c.getContext('2d');x.fillStyle='#334557';x.fillRect(0,0,960,540);x.strokeStyle='#b7c9d8';x.lineWidth=12;x.strokeRect(35,35,890,470);x.fillStyle='#91a3b4';x.fillRect(370,35,200,110);x.font='32px sans-serif';x.fillText('ROOM / ISOLATED FIXTURE',230,300);return c.toDataURL('image/png').split(',')[1];}""")
            import base64
            page.locator('#tv-room-file').set_input_files({'name': 'room.png', 'mimeType': 'image/png', 'buffer': base64.b64decode(png)})
            assert context.request.get(base + '/__tv_fixture').json()['commands'] == before
            page.locator('#tv-room-upload').click()
            expect(page.locator('#tv-editor-status')).to_contain_text('Uploaded')
            page.locator('#tv-room-remove-image').click()
            expect(page.locator('#tv-editor-canvas img')).to_have_count(0)
            page.locator('#tv-room-images button').last.click()
            expect(page.locator('#tv-editor-canvas img')).to_be_visible()
            page.locator('[data-place-tv=foyer]').click()
            page.locator('#tv-position-x').fill('25'); page.locator('#tv-position-y').fill('65'); page.locator('#tv-position-apply').click()
            point = page.locator('[data-position-tv=foyer]')
            expect(point).to_be_visible(); point.focus(); point.press('ArrowRight')
            expect(page.locator('#tv-position-x')).to_have_value('26')
            page.locator('#tv-position-remove').click(); expect(point).to_have_count(0)
            page.locator('#tv-position-apply').click(); expect(point).to_be_visible()
            # Place with touch/click as well as keyboard and numeric controls.
            page.locator('[data-place-tv=hall]').click()
            picture = page.locator('#tv-editor-canvas .tv-room-picture'); box = picture.bounding_box()
            if device: page.touchscreen.tap(box['x'] + box['width'] * .72, box['y'] + box['height'] * .35)
            else: page.mouse.click(box['x'] + box['width'] * .72, box['y'] + box['height'] * .35)
            if not device:
                hall = page.locator('[data-position-tv=hall]').bounding_box()
                page.mouse.move(hall['x'] + hall['width'] / 2, hall['y'] + hall['height'] / 2); page.mouse.down()
                page.mouse.move(hall['x'] + hall['width'] / 2 + 20, hall['y'] + hall['height'] / 2 - 10, steps=4); page.mouse.up()
            page.locator('#tv-room-save').click()
            expect(page.locator('#tv-editor-status')).to_contain_text('Saved. No device commands sent.')
            saved = context.request.get(base + '/api/config/tv-room-layouts').json()['layouts']['ground']
            assert saved['positions']['foyer'] == {'x': .26, 'y': .65}
            assert 'hall' in saved['positions'] and saved['background'].endswith('.png')
            page.reload(); page.select_option('#tv-editor-room', 'ground'); expect(page.locator('[data-position-tv=foyer]')).to_be_visible()
            page.locator('[data-position-tv=foyer]').click(); expect(page.locator('#tv-position-x')).to_have_value('26')
            capture(page, output / (name + '-editor.png'))
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert context.request.get(base + '/__tv_fixture').json()['commands'] == before
            page.goto(base + '/tv-controls'); page.locator('[data-room=ground]').click(); page.locator('[data-tv=hall]').click()
            expect(page.locator('#tv-media')).not_to_be_visible(); expect(page.locator('#tv-media-help')).to_contain_text('No media mapping')
            page.route('**/api/tvs/hall/volume', lambda route: route.fulfill(status=503, content_type='application/json', body='{"ok":false,"error":"fixture unavailable"}'))
            page.locator('#tv-volume-up').click()
            expect(page.locator('#tv-command')).to_contain_text('do not automatically retry')
            with page.expect_response(lambda r: '/api/tv-pages/state' in r.url): pass
            expect(page.locator('#tv-command')).to_contain_text('do not automatically retry')
            page.unroute('**/api/tvs/hall/volume')
            assert context.request.get(base + '/__tv_fixture').json()['commands'] == before
            page.goto(base + '/tv-diagnostics'); expect(page.locator('#tv-overview')).to_have_attribute('aria-busy', 'false')
            image = page.locator('.tv-room-section').first.locator('.tv-room-picture > img')
            expect(image).to_be_visible()
            page.wait_for_function("document.querySelector('.tv-room-picture > img').complete && document.querySelector('.tv-room-picture > img').naturalWidth > 0")
            page.evaluate("window.__tvRoomImage = document.querySelector('.tv-room-picture > img')")
            photo_requests = []
            page.on('request', lambda request: photo_requests.append(request.url) if '/tv-room-images/' in request.url else None)
            for _ in range(3):
                with page.expect_response(lambda response: '/api/tv-pages/state' in response.url): pass
            assert page.evaluate("window.__tvRoomImage === document.querySelector('.tv-room-picture > img')"), 'polling must retain the decoded room image'
            assert not photo_requests, ('polling re-requested the background', photo_requests)
            expect(page.locator('.tv-positioned[data-tv-id=foyer]')).to_be_visible()
            expect(page.locator('.tv-positioned[data-tv-id=foyer] .tv-map-power')).to_contain_text('On')
            expect(page.locator('.tv-positioned[data-tv-id=foyer] .tv-map-volume')).to_have_text('Volume 25')
            assert page.locator('#tv-overview button, #tv-overview input, #tv-overview select').count() == 0
            page.locator('#tv-overview details').first.locator('summary').click()
            with page.expect_response(lambda r: '/api/tv-pages/state' in r.url): pass
            expect(page.locator('#tv-overview details').first).to_have_attribute('open', '')
            capture(page, output / (name + '-diagnostics.png'))
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert context.request.post(base + '/__tv_fixture/state', data={'power_feedback': 'off'}).ok
            expect(page.locator('[data-tv-id=foyer]')).to_have_class('tv-map-tv tv-power-off tv-positioned')
            assert context.request.post(base + '/__tv_fixture/state', data={'power_feedback': 'stale'}).ok
            expect(page.locator('[data-tv-id=foyer]')).to_have_class('tv-map-tv tv-power-unknown tv-positioned')
            expect(page.locator('[data-tv-id=foyer] .tv-map-power')).to_contain_text('Stale')
            assert context.request.post(base + '/__tv_fixture/state', data={'power_feedback': 'unknown'}).ok
            expect(page.locator('[data-tv-id=foyer] .tv-map-power')).to_contain_text('unknown')
            capture(page, output / (name + '-neutral-power.png'))
            assert context.request.post(base + '/__tv_fixture/state', data={'power_feedback': 'on'}).ok
            expect(page.locator('[data-tv-id=foyer] .tv-map-power')).to_contain_text('On')
            page.route('**/api/tv-pages/state*', lambda route: route.fulfill(status=503, content_type='application/json', body='{"ok":false,"error":"fixture unavailable"}'))
            expect(page.locator('#tv-message')).to_contain_text('Could not refresh')
            expect(page.locator('[data-tv-id=foyer]')).to_have_class('tv-map-tv tv-power-unknown tv-positioned')
            page.unroute('**/api/tv-pages/state*')
            assert context.request.get(base + '/__tv_fixture').json()['commands'] == before
            if name == 'chromium':
                page.set_viewport_size({'width': 320, 'height': 850}); capture(page, output / '320-diagnostics.png')
                boxes = page.locator('.tv-positioned').evaluate_all('(items) => items.map(n => {const r=n.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};})')
                assert not (boxes[0]['left'] < boxes[1]['right'] and boxes[0]['right'] > boxes[1]['left'] and boxes[0]['top'] < boxes[1]['bottom'] and boxes[0]['bottom'] > boxes[1]['top']), 'mobile positioned TVs overlap'
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.goto(base + '/config/tv-rooms'); page.select_option('#tv-editor-room', 'ground'); capture(page, output / '320-editor.png')
                page.locator('[data-place-tv=foyer]').click(); page.locator('#tv-position-x').fill('0'); page.locator('#tv-position-y').fill('0'); page.locator('#tv-position-apply').click()
                canvas_box = page.locator('#tv-editor-canvas .tv-room-picture').bounding_box(); tv_box = page.locator('[data-position-tv=foyer]').bounding_box()
                capture(page, output / '320-editor-edge.png')
                assert tv_box['x'] >= canvas_box['x'] and tv_box['y'] >= canvas_box['y'], ('edge positions must remain inside the editor canvas', canvas_box, tv_box)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not errors, errors
            assert all(response['status'] == 503 and response['path'] in ('/api/tvs/hall/volume', '/api/tv-pages/state') for response in failed_responses), failed_responses
            unexpected_console = [message for message in console_errors if not message.startswith('Failed to load resource: the server responded with a status of 503')]
            assert not unexpected_console, unexpected_console
            results.append({'project': name, 'passed': True, 'saved': saved, 'page_errors': errors, 'no_device_commands': True, 'expected_failure_responses': failed_responses, 'console_errors': console_errors, 'unexpected_console_errors': unexpected_console})
            (output / 'results.json').write_text(json.dumps(results, indent=2))
            context.close(); browser.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__': main()
