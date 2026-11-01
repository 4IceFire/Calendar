"""Compact TV remote regressions against marked synthetic fixtures only."""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from tv_browser_check import capture
from browser_error_contract import cancelled_poll_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--project', default='all')
    parser.add_argument('--navigation-only', action='store_true')
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
            context = browser.new_context(**(p.devices[device] if device else {'viewport': {'width': 1440, 'height': 1000}}))
            errors = []; fits = []; watches = []
            def watch(page):
                lifecycle = []; methods = {}; watches.append(lifecycle)
                page.add_init_script("""(() => {
                  function event(name, message) {console.debug('__COMPACT_LIFECYCLE__'+JSON.stringify({event:name,url:location.href,message:message||''}));}
                  event('document-start');
                  ['beforeunload','error','unhandledrejection'].forEach(name=>window.addEventListener(name,e=>event(name,e.message||'')));
                })();""")
                page.on('console', lambda message: lifecycle.append({**json.loads(message.text[len('__COMPACT_LIFECYCLE__'):]), 'at': time.monotonic()}) if message.text.startswith('__COMPACT_LIFECYCLE__') else None)
                page.on('request', lambda request: methods.update({request.url: request.method}))
                page.on('pageerror', lambda error: errors.append({'message': str(error), 'stack': getattr(error, 'stack', ''), 'url': page.url, 'at': time.monotonic(), 'lifecycle': lifecycle, 'request_methods': methods}))
                return page
            page = watch(context.new_page()); page.bring_to_front()
            assert context.request.get(base + '/__tv_fixture').json()['fixture'] == 'tdeck-tv-isolated'
            reset = context.request.post(base + '/__tv_fixture/reset'); assert reset.ok and reset.json()['ok']
            page.goto(base + '/login'); page.locator('[name=username]').fill('operator-tv'); page.locator('[name=password]').fill('fixture-password'); page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url('**/tv-controls')
            page.locator('[data-room=ground]').click(); page.locator('[data-tv=foyer]').click()
            if args.navigation_only:
                page.locator('#tv-back').click()
                expect(page.locator('[data-tv=foyer]')).to_be_visible()
                assert page.url.endswith('/tv-controls')
                page.locator('[data-tv=foyer]').click()
            for width, height in [(390, 700), (320, 640)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.evaluate("document.documentElement.style.setProperty('scroll-behavior','auto','important');scrollTo(0,0)")
                capture(page, output / (name + '-' + str(width) + '-remote.png'))
                geometry = page.evaluate("({height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth,viewportHeight:innerHeight,viewportWidth:innerWidth})")
                fits.append(geometry)
                assert geometry['height'] <= height + 1 and geometry['width'] <= width, ('primary remote must fit one mobile viewport', geometry)
                observed = page.locator('#tv-observed').bounding_box(); controls = page.locator('#tv-remote-controls').bounding_box()
                assert observed['y'] + observed['height'] <= controls['y'], 'TV status must precede all remote controls'
            if not device: page.set_viewport_size({'width': 1440, 'height': 1000})
            page.goto(base + '/tv-diagnostics')
            expect(page.locator('a[data-tv-id=foyer]')).to_have_attribute('href', '/tv-controls?tv=foyer&from=diagnostics')
            page.locator('a[data-tv-id=foyer]').click(); page.wait_for_url('**/tv-controls?tv=foyer&from=diagnostics')
            expect(page.locator('#tv-remote-controls')).to_be_enabled()
            page.locator('#tv-back').click(); page.wait_for_url('**/tv-diagnostics')
            expect(page.locator('a[data-tv-id=foyer]')).to_be_visible()
            assert context.request.get(base + '/__tv_fixture').json()['commands']['foyer'] == []
            page.locator('a[data-tv-id=foyer]').click(); page.wait_for_url('**/tv-controls?tv=foyer&from=diagnostics')
            expect(page.locator('#tv-name')).to_have_text('Ground Foyer (simulated)')
            expect(page.locator('#tv-remote-controls')).to_be_enabled()
            if args.navigation_only:
                page.reload(); expect(page.locator('#tv-remote-controls')).to_be_enabled()
                page.locator('#tv-back').click(); page.wait_for_url('**/tv-diagnostics')
                expect(page.locator('a[data-tv-id=foyer]')).to_be_visible()
                assert all(not values for values in context.request.get(base + '/__tv_fixture').json()['commands'].values())
                cancelled = [error for error in errors if cancelled_poll_error(error, engine=engine, base=base, lifecycle=error['lifecycle'], request_methods=error['request_methods'])]
                unexpected = [error for error in errors if error not in cancelled]
                window_errors = [event for lifecycle in watches for event in lifecycle if event['event'] in ('error', 'unhandledrejection')]
                assert not unexpected and not window_errors, (unexpected, window_errors)
                results.append({'project': name, 'passed': True, 'navigation_only': True, 'no_device_commands': True, 'page_errors': unexpected, 'window_errors': window_errors, 'expected_cancelled_poll_errors': cancelled})
                (output / 'results.json').write_text(json.dumps(results, indent=2))
                context.close(); browser.close(); continue
            slider = page.locator('#tv-volume-level')
            expect(slider).to_have_attribute('type', 'range')
            expect(slider).to_have_value('25')
            rail = slider.evaluate("n => getComputedStyle(n).backgroundImage")
            assert rail != 'none', 'slider needs a visible, contrasting rail behind its thumb'
            assert context.request.get(base + '/__tv_fixture').json()['commands']['foyer'] == []
            # Input previews locally; a background report must not move a held slider.
            slider.evaluate("n => {n.value='31';n.dispatchEvent(new Event('input',{bubbles:true}));}")
            expect(page.locator('#tv-volume-value')).to_contain_text('31 requested')
            assert context.request.post(base + '/__tv_fixture/state', data={'volume': 43}).ok
            expect(page.locator('#tv-observed')).to_contain_text('43')
            expect(slider).to_have_value('31')
            assert context.request.get(base + '/__tv_fixture').json()['commands']['foyer'] == []
            with page.expect_response(lambda r: r.url.endswith('/api/tvs/foyer/volume') and r.request.method == 'POST') as changed:
                slider.dispatch_event('change')
            assert changed.value.request.post_data_json == {'level': 31}
            assert changed.value.request.headers.get('x-csrf-token')
            expect(page.locator('#tv-command')).to_contain_text('confirmed')
            expect(page.locator('#tv-volume-value')).to_have_text('31')
            # Keyboard commit uses the same bounded path, not a separate form.
            slider.focus()
            with page.expect_response(lambda r: r.url.endswith('/api/tvs/foyer/volume') and r.request.method == 'POST') as changed:
                slider.press('ArrowRight')
            assert changed.value.request.post_data_json == {'level': 32}
            expect(page.locator('#tv-observed')).to_contain_text('32')
            expect(slider).to_have_value('32')
            expect(slider).to_be_enabled()
            before_drag = len(context.request.get(base + '/__tv_fixture').json()['commands']['foyer'])
            box = slider.bounding_box()
            with page.expect_response(lambda r: r.url.endswith('/api/tvs/foyer/volume') and r.request.method == 'POST') as moved:
                if device:
                    page.touchscreen.tap(box['x'] + box['width'] * .8, box['y'] + box['height'] / 2)
                else:
                    page.mouse.move(box['x'] + box['width'] * .32, box['y'] + box['height'] / 2); page.mouse.down()
                    page.mouse.move(box['x'] + box['width'] * .8, box['y'] + box['height'] / 2, steps=8); page.mouse.up()
            pointer_level = moved.value.request.post_data_json['level']
            expect(page.locator('#tv-volume-value')).to_have_text(str(pointer_level))
            assert len(context.request.get(base + '/__tv_fixture').json()['commands']['foyer']) == before_drag + 1
            slider.dispatch_event('change')
            with page.expect_response(lambda r: '/api/tv-pages/state' in r.url): pass
            assert len(context.request.get(base + '/__tv_fixture').json()['commands']['foyer']) == before_drag + 1, 'release/change must not replay a committed gesture'
            assert context.request.post(base + '/__tv_fixture/state', data={'ignore': True}).ok
            slider.evaluate("n => {n.value='52';n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));}")
            expect(page.locator('#tv-command')).to_contain_text('pending')
            expect(page.locator('#tv-observed')).to_contain_text(str(pointer_level))
            expect(page.locator('#tv-volume-value')).to_have_text('52 requested')
            assert context.request.post(base + '/__tv_fixture/state', data={'ignore': False, 'external': True}).ok
            expect(slider).to_be_disabled()
            expect(page.locator('#tv-volume-value')).to_contain_text('Unavailable')
            expect(page.locator('#tv-observed')).to_contain_text('External audio')
            assert context.request.post(base + '/__tv_fixture/state', data={'power_feedback': 'stale'}).ok
            expect(page.locator('#tv-observed .tv-reading strong').first).to_have_text('on · stale')
            capture(page, output / (name + '-slider.png'))
            admin_context = browser.new_context(viewport={'width': 390, 'height': 700})
            admin = watch(admin_context.new_page()); admin.bring_to_front()
            admin.goto(base + '/login?next=/config/tvs'); admin.locator('[name=username]').fill('admin-tv'); admin.locator('[name=password]').fill('fixture-password'); admin.get_by_role('button', name='Sign in', exact=True).click()
            try:
                admin.wait_for_url('**/config/tvs', timeout=10000)
            except Exception:
                print({'login_url': admin.url, 'login_feedback': admin.locator('main').inner_text()})
                raise
            admin.goto(base + '/tv-controls')
            expect(admin.locator('.tv-setup-links a')).to_have_count(2)
            for link in admin.locator('.tv-setup-links a').all():
                expect(link).to_have_class('btn btn-outline-secondary')
                assert link.bounding_box()['height'] >= 43.99, (name, link.text_content(), link.bounding_box(), link.evaluate("n=>({minHeight:getComputedStyle(n).minHeight,transform:getComputedStyle(n).transform})"))
                assert link.evaluate("n => getComputedStyle(n).textDecorationLine") == 'none'
            capture(admin, output / (name + '-navigation-buttons.png'))
            admin.goto(base + '/config/tv-rooms')
            try:
                expect(admin.locator('#tv-room-editor')).to_have_attribute('aria-busy', 'false')
            except Exception:
                print({'editor_errors': errors, 'editor_state': admin.evaluate("({ready:document.readyState,hidden:document.hidden,initializer:typeof initializeTvRooms,sharedView:typeof window.TvRoomView})")})
                raise
            expect(admin.locator('.tv-page-tools a')).to_have_count(2)
            for link in admin.locator('.tv-page-tools a').all():
                expect(link).to_have_class('btn btn-outline-secondary')
                assert link.bounding_box()['height'] >= 43.99, (name, link.text_content(), link.bounding_box(), link.evaluate("n=>({minHeight:getComputedStyle(n).minHeight,transform:getComputedStyle(n).transform})"))
            assert not admin.evaluate('document.documentElement.scrollWidth > innerWidth')
            capture(admin, output / (name + '-editor-buttons.png')); admin_context.close()
            cancelled = [error for error in errors if cancelled_poll_error(error, engine=engine, base=base, lifecycle=error['lifecycle'], request_methods=error['request_methods'])]
            unexpected = [error for error in errors if error not in cancelled]
            window_errors = [event for lifecycle in watches for event in lifecycle if event['event'] in ('error', 'unhandledrejection')]
            assert not unexpected, unexpected
            assert not window_errors, window_errors
            results.append({'project': name, 'passed': True, 'fits': fits, 'page_errors': unexpected, 'window_errors': window_errors, 'expected_cancelled_poll_errors': cancelled})
            (output / 'results.json').write_text(json.dumps(results, indent=2))
            context.close(); browser.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__': main()
