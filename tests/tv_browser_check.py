"""Actual Playwright TV interactions against a marked owned fixture only.

Run with the browser-tooling Python, --ready-file and --evidence-dir outside repo.
"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from browser_error_contract import cancelled_poll_error


def capture(page, path):
    # Full-page captures otherwise place the sticky navbar at its old scroll
    # position inside the stitched image, obscuring mobile content artificially.
    page.wait_for_load_state('load')
    page.evaluate("document.documentElement.style.setProperty('scroll-behavior', 'auto', 'important'); document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0)")
    page.screenshot(path=str(path), full_page=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--project', default='all')
    parser.add_argument('--trace-lifecycle', action='store_true')
    args = parser.parse_args()
    ready = json.loads(Path(args.ready_file).read_text())
    base = ready['url']
    assert urlparse(base).hostname == '127.0.0.1' and ready['fixture'] == 'tdeck-tv-isolated'
    output = Path(args.evidence_dir); output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        projects = [('chromium', 'chromium', None), ('firefox', 'firefox', None), ('webkit', 'webkit', None),
                    ('mobile-chromium', 'chromium', 'Pixel 7'), ('mobile-webkit', 'webkit', 'iPhone 13')]
        for name, engine, device in projects:
            if args.project not in ('all', name):
                continue
            launcher = getattr(p, engine)
            kwargs = {'headless': True}
            if engine == 'chromium':
                kwargs['executable_path'] = str(Path.home() / '.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell')
            if engine == 'webkit':
                kwargs['executable_path'] = str(Path.home() / '.cache/ms-playwright/webkit-2336/pw_run.sh')
            if engine == 'firefox':
                kwargs['executable_path'] = str(Path.home() / '.cache/ms-playwright/firefox-1538/firefox/firefox')
            browser = launcher.launch(**kwargs)
            context = browser.new_context(**(p.devices[device] if device else {'viewport': {'width': 1440, 'height': 1000}}))
            page = context.new_page()
            lifecycle = []
            request_methods = {}
            page.add_init_script("""(() => {
              console.debug('__TV_LIFECYCLE__' + JSON.stringify({event:'document-start', url:location.href}));
              ['beforeunload','pagehide','error','unhandledrejection'].forEach(event => window.addEventListener(event, e => console.debug('__TV_LIFECYCLE__' + JSON.stringify({event, url:location.href, message:e.message || ''}))));
            })();""")
            def lifecycle_message(message):
                if message.text.startswith('__TV_LIFECYCLE__'):
                    lifecycle.append({**json.loads(message.text[len('__TV_LIFECYCLE__'):]), 'at': time.monotonic()})
            page.on('console', lifecycle_message)
            page.on('request', lambda request: request_methods.update({request.url: request.method}))
            trace_records = []
            if args.trace_lifecycle:
                page.add_init_script("""(() => {
                  function trace(event, details) { console.debug('__TV_TRACE__' + JSON.stringify({event, details, at:performance.now(), url:location.pathname, visibility:document.visibilityState, ready:document.readyState})); }
                  const nativeAbort = AbortController.prototype.abort;
                  AbortController.prototype.abort = function() { trace('abort-called', new Error().stack); return nativeAbort.apply(this, arguments); };
                  ['beforeunload','pagehide','pageshow','visibilitychange','error','unhandledrejection'].forEach(event => window.addEventListener(event, e => trace(event, e.message || '')));
                })();""")
            errors = []
            console_errors = []
            failed_responses = []
            phase = ['startup']
            navigation_events = []
            page.on('requestfailed', lambda request: navigation_events.append({'event': 'requestfailed', 'url': request.url, 'failure': request.failure, 'phase': phase[0]}))
            page.on('framenavigated', lambda frame: navigation_events.append({'event': 'navigation', 'url': frame.url, 'phase': phase[0]}) if frame == page.main_frame else None)
            page.on('pageerror', lambda error: errors.append({'message': str(error), 'stack': getattr(error, 'stack', ''), 'url': page.url, 'at': time.monotonic(), 'phase': phase[0], 'events': list(navigation_events), 'trace': list(trace_records[-30:])}))
            page.on('console', lambda message: trace_records.append(json.loads(message.text[len('__TV_TRACE__'):])) if message.text.startswith('__TV_TRACE__') else None)
            page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
            page.on('response', lambda response: failed_responses.append({'path': urlparse(response.url).path, 'status': response.status}) if response.status >= 400 else None)
            if name == 'chromium':
                def delayed_security_script(route):
                    time.sleep(.25)
                    route.continue_()
                page.route('**/static/api_client.js*', delayed_security_script)
            marker = context.request.get(base + '/__tv_fixture').json()
            assert marker['fixture'] == 'tdeck-tv-isolated'
            reset = context.request.post(base + '/__tv_fixture/reset')
            assert reset.ok and reset.json()['ok']
            page.goto(base + '/login')
            page.locator('[name=username]').fill('operator-tv')
            page.locator('[name=password]').fill('fixture-password')
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url('**/tv-controls')
            phase[0] = 'controls'
            expect(page.locator('#tv-room option[value=ground]')).to_have_count(1)
            assert context.request.get(base + '/__tv_fixture').json()['commands']['foyer'] == []
            page.select_option('#tv-room', 'ground')
            page.select_option('#tv-choice', 'foyer')
            expect(page.locator('#tv-remote')).to_be_visible()
            expect(page.locator('#tv-choice option[value=private]')).to_have_count(0)
            assert context.request.get(base + '/__tv_fixture').json()['commands']['foyer'] == []
            with page.expect_response(lambda response: response.url.endswith('/api/tvs/foyer/power') and response.request.method == 'POST') as power:
                page.locator('#tv-power-on').click()
            assert power.value.request.post_data_json == {'state': 'on'}
            assert power.value.request.headers.get('x-csrf-token')
            expect(page.locator('#tv-power-off')).to_be_enabled()
            with page.expect_response(lambda response: response.url.endswith('/api/tvs/foyer/power') and response.request.method == 'POST') as power:
                page.locator('#tv-power-off').click()
            assert power.value.request.post_data_json == {'state': 'off'}
            expect(page.locator('#tv-command')).to_contain_text('power off')
            expect(page.locator('#tv-command')).to_contain_text('pending')
            assert context.request.post(base + '/__tv_fixture/state', data={'volume': 25, 'source': 'HDMI1'}).ok
            expect(page.locator('#tv-volume-set')).to_be_enabled()
            page.locator('#tv-volume-level').fill('31')
            command_start = time.monotonic()
            page.locator('#tv-volume-set').click()
            expect(page.locator('#tv-command')).to_contain_text('confirmed')
            command_render_ms = round((time.monotonic() - command_start) * 1000)
            observed = context.request.get(base + '/api/tv-pages/state').json()['tvs'][0]['observation']
            report_ms = round((observed['fields']['volume']['sampledAt'] - observed['command']['requestedAt']) * 1000)
            expect(page.locator('#tv-observed')).to_contain_text('31')
            page.select_option('#tv-source', 'HDMI2')
            page.get_by_role('button', name='Change input', exact=True).click()
            expect(page.locator('#tv-observed')).to_contain_text('HDMI2')
            assert context.request.post(base + '/__tv_fixture/state', data={'ignore': True}).ok
            page.locator('#tv-volume-level').fill('32')
            page.locator('#tv-volume-set').click()
            expect(page.locator('#tv-command')).to_contain_text('pending')
            expect(page.locator('#tv-observed')).to_contain_text('31')
            assert context.request.post(base + '/__tv_fixture/state', data={'ignore': False}).ok
            assert context.request.post(base + '/__tv_fixture/state', data={'volume': 43, 'source': 'HDMI1'}).ok
            start = time.monotonic()
            expect(page.locator('#tv-observed')).to_contain_text('43')
            feedback_ms = round((time.monotonic() - start) * 1000)
            assert context.request.post(base + '/__tv_fixture/state', data={'fail': True}).ok
            page.locator('#tv-volume-up').click()
            expect(page.locator('#tv-failures')).to_contain_text('Command failure: volume_up')
            assert context.request.post(base + '/__tv_fixture/state', data={'fail': False}).ok
            expect(page.locator('#tv-failures')).to_contain_text('Command failure: volume_up')
            expect(page.locator('#tv-volume-up')).to_be_enabled()
            expect(page.locator('#tv-failures')).not_to_contain_text('Active:')
            capture(page, output / (name + '-controls.png'))
            overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not overflow, name + ' controls overflow'
            page.locator('#tv-media').click()
            phase[0] = 'media'
            expect(page.locator('#media-grid')).to_have_attribute('aria-busy', 'false')
            expect(page.get_by_role('button', name='Display Welcome fixture')).to_be_visible()
            capture(page, output / (name + '-media.png'))
            page.get_by_role('button', name='Display Welcome fixture').click()
            phase[0] = 'display-return'
            page.wait_for_url('**/tv-controls', timeout=15000)
            phase[0] = 'diagnostics-navigation'
            page.goto(base + '/tv-diagnostics')
            expect(page.locator('#tv-overview')).to_have_attribute('aria-busy', 'false')
            expect(page.locator('#tv-overview')).to_contain_text('Command failure: volume_up')
            expect(page.locator('#tv-overview')).not_to_contain_text('Private')
            assert page.locator('#tv-overview button').count() == 0
            assert context.request.post(base + '/__tv_fixture/state', data={'external': True}).ok
            expect(page.locator('#tv-overview')).to_contain_text('External audio — volume unavailable/unverified')
            capture(page, output / (name + '-diagnostics.png'))
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            if args.trace_lifecycle:
                (output / (name + '-trace.json')).write_text(json.dumps(trace_records, indent=2))
            # WebKit reports native policy diagnostics for handled GET polls
            # cancelled by document navigation, including the existing auth
            # heartbeat. Keep those records, but never excuse a real window
            # exception/rejection, a command, or an in-page policy failure.
            window_errors = [event for event in lifecycle if event['event'] in ('error', 'unhandledrejection')]
            expected_cancelled = [error for error in errors if cancelled_poll_error(error, engine=engine, base=base, lifecycle=lifecycle, request_methods=request_methods)]
            unexpected_errors = [error for error in errors if error not in expected_cancelled]
            assert not window_errors, window_errors
            assert not unexpected_errors, unexpected_errors
            expected_messages = {text for error in expected_cancelled for text in (error['message'], error['stack'].splitlines()[0])}
            unexpected_console = [message for message in console_errors if 'Failed to load resource' not in message and message not in expected_messages]
            assert not unexpected_console, unexpected_console
            assert all(response == {'path': '/api/tvs/foyer/volume', 'status': 502} for response in failed_responses), failed_responses
            commands = context.request.get(base + '/__tv_fixture').json()['commands']
            assert not commands['hall'] and not commands['private']
            if name == 'chromium':
                page.set_viewport_size({'width': 2540, 'height': 1200})
                capture(page, output / 'wide-diagnostics.png')
                page.set_viewport_size({'width': 320, 'height': 800})
                page.goto(base + '/tv-controls')
                page.select_option('#tv-room', 'ground'); page.select_option('#tv-choice', 'foyer')
                expect(page.locator('#tv-remote')).to_be_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                capture(page, output / 'narrow-320-controls.png')
                admin_context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                admin = admin_context.new_page()
                admin.goto(base + '/login'); admin.locator('[name=username]').fill('admin-tv'); admin.locator('[name=password]').fill('fixture-password')
                admin.get_by_role('button', name='Sign in', exact=True).click()
                admin.wait_for_url(lambda url: '/login' not in url)
                admin.goto(base + '/admin/permissions?tab=groups#role-2')
                form = admin.locator('[data-role-form][data-role-id="2"]')
                expect(form).to_be_visible()
                form.locator('[data-permission-tab=tvs]').click()
                hall = form.locator('input[value="tvdevice:ground:hall"]')
                with admin.expect_response(lambda response: response.url.endswith('/api/admin/groups/2') and response.request.method == 'POST') as saved:
                    hall.check()
                assert saved.value.ok
                assert len(context.request.get(base + '/api/tv-pages/state').json()['tvs']) == 2
                capture(admin, output / 'admin-room-permissions.png')
                with admin.expect_response(lambda response: response.url.endswith('/api/admin/groups/2') and response.request.method == 'POST') as saved:
                    hall.uncheck()
                assert saved.value.ok
                assert len(context.request.get(base + '/api/tv-pages/state').json()['tvs']) == 1
                assert admin_context.request.post(base + '/__tv_fixture/state', data={'external': False}).ok
                before = admin_context.request.get(base + '/__tv_fixture').json()['commands']
                admin.goto(base + '/config/tvs')
                expect(admin.locator('#tv-map-foyer-output')).to_have_value('1')
                admin.locator('#tv-map-foyer-output').fill('2'); admin.locator('#tv-map-foyer-source').fill('HDMI2')
                admin.locator('#tv-mapping-save').click()
                expect(admin.locator('#tv-mapping-status')).to_contain_text('Saved. No device commands sent.')
                assert admin_context.request.get(base + '/__tv_fixture').json()['commands'] == before
                admin.reload(); expect(admin.locator('#tv-map-foyer-output')).to_have_value('2')
                capture(admin, output / 'admin-media-mapping.png')
                admin.locator('#tv-map-foyer-output').fill('1'); admin.locator('#tv-map-foyer-source').fill('HDMI1'); admin.locator('#tv-mapping-save').click()
                expect(admin.locator('#tv-mapping-status')).to_contain_text('Saved. No device commands sent.')
                admin_context.close()
            results.append({'project': name, 'passed': True, 'external_report_to_render_ms': feedback_ms,
                            'command_to_render_ms': command_render_ms, 'simulated_request_to_report_ms': report_ms,
                            'page_errors': unexpected_errors, 'window_errors': window_errors, 'expected_cancelled_poll_errors': expected_cancelled,
                            'console_errors': console_errors, 'unexpected_console_errors': unexpected_console,
                            'expected_failure_responses': failed_responses})
            (output / 'results.json').write_text(json.dumps(results, indent=2))
            context.close(); browser.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
