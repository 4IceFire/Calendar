"""Actual-browser Timers contract; only a marked loopback fake fixture."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--browser', default='chromium')
    args = parser.parse_args()
    url = json.loads(Path(args.ready_file).read_text())['url']
    assert urlparse(url).hostname == '127.0.0.1'
    artifacts = Path(args.artifacts); artifacts.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        names = ['chromium', 'firefox', 'webkit', 'mobile-chromium', 'mobile-webkit'] if args.browser == 'all' else [args.browser]
        for name in names:
            print('Checking ' + name, flush=True)
            engine = name.removeprefix('mobile-')
            import os
            executable = os.environ.get('TDECK_BROWSER_' + engine.upper())
            browser = getattr(pw, engine).launch(**({'executable_path': executable} if executable else {}))
            context = browser.new_context(**(pw.devices['Pixel 7' if engine == 'chromium' else 'iPhone 13'] if name.startswith('mobile-') else {'viewport': {'width': 1920, 'height': 1080}}))
            context.route('**/*', lambda route: route.continue_() if urlparse(route.request.url).hostname == '127.0.0.1' else route.abort())
            page = context.new_page()
            assert context.request.get(url + '/__timers_fixture__/health').json()['fixture'] == 'tdeck-timers-ui'
            assert context.request.post(url + '/__timers_fixture__/reset').json()['ok']
            page.goto(url + '/timers')
            expect(page.locator('#timers-presets-body tr')).to_have_count(2)
            expect(page.get_by_label('Update Timer', exact=True)).to_be_checked()
            expect(page.locator('#timers-stage-delay-send')).to_be_disabled()
            page.route('**/api/timers/mutate', lambda route: route.fulfill(status=500, content_type='application/json', body='{"ok":false,"error":"Synthetic selection save failed"}'))
            page.locator('#timers-stage-delay-preset').select_option('2')
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Synthetic selection save failed')
            expect(page.locator('#timers-stage-delay-send')).to_be_disabled()
            page.unroute('**/api/timers/mutate')
            page.locator('#timers-stage-delay-preset').select_option('1')
            expect(page.locator('#timers-stage-delay-send')).to_be_enabled()
            page.locator('#timers-stage-delay-preset').select_option('2')
            expect(page.locator('#timers-stage-delay-send')).to_be_enabled()
            page.reload()
            expect(page.locator('#timers-stage-delay-preset')).to_have_value('2')
            expect(page.locator('#timers-stage-preset')).to_have_value('1')
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['calls'] == [] and health['presses'] == []
            page.locator('#timers-stage-delay-minutes').fill('2')
            expect(page.locator('#timers-stage-delay-preview')).to_contain_text('12:01AM')
            page.screenshot(path=str(artifacts / (name + '-before.png')), full_page=True)
            page.evaluate("""() => {
                window.__timerHints = [];
                const original = _timersStageSetDelayHint;
                _timersStageSetDelayHint = function(message, kind) {
                    window.__timerHints.push({message, kind, minutes: document.getElementById('timers-stage-delay-minutes').value});
                    return original(message, kind);
                };
            }""")
            with page.expect_response(lambda response: response.url.endswith('/api/propresenter/stage/stream_delay')) as received:
                page.locator('#timers-stage-delay-send').click()
            reply = received.value.json()
            assert reply['saved'] and reply['applied'] and reply['sent'], reply
            try:
                expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Preset saved; applied; message sent', timeout=15000)
            except AssertionError:
                debug = {'hints': page.evaluate('window.__timerHints'), 'health': context.request.get(url + '/__timers_fixture__/health').json()}
                (artifacts / (name + '-failure.json')).write_text(json.dumps(debug, indent=2))
                print(json.dumps(debug), flush=True)
                raise
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['presets'][1]['time'] == '00:01'
            assert health['calls'][:3] == [['set', 1, '00:01'], ['reset', 1], ['start', 1]]
            assert health['presses'] == ['location/1/0/1/press']
            page.locator('#timers-stage-delay-send').click()
            expect(page.locator('#timers-presets-body tr').nth(1).locator('input[type=time]')).to_have_value('00:03')
            page.get_by_label('Update Timer', exact=True).uncheck()
            page.locator('#timers-stage-delay-send').click()
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Message sent; preset unchanged')
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['presets'][1]['time'] == '00:03' and len(health['presses']) == 2
            page.get_by_label('Update Timer', exact=True).check()
            assert context.request.post(url + '/__timers_fixture__/failure?step=reset').json()['ok']
            page.locator('#timers-stage-delay-send').click()
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Preset saved; apply incomplete; message sent')
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['presets'][1]['time'] == '00:05' and len(health['presses']) == 3
            page.screenshot(path=str(artifacts / (name + '-partial.png')), full_page=True)
            assert context.request.post(url + '/__timers_fixture__/edit').json()['ok']
            before = len(health['calls'])
            page.locator('#timers-stage-delay-send').click()
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Timers changed')
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert len(health['calls']) == before
            page.reload()
            expect(page.locator('#timers-stage-delay-preset')).to_have_value('2')
            expect(page.locator('#timers-presets-body tr').nth(1).locator('input[type=time]')).to_have_value('10:00')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(artifacts / (name + '-reload.png')), full_page=True)
            page.route('**/api/propresenter/stage/stream_delay', lambda route: route.abort('failed'))
            page.locator('#timers-stage-delay-send').click()
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('outcome unknown')
            page.unroute('**/api/propresenter/stage/stream_delay')
            assert len(context.request.get(url + '/__timers_fixture__/health').json()['calls']) == before
            # Actual handlers: queue a move response, then click Delay while the
            # old positional selector is still shown. The post-flush B must win.
            assert context.request.post(url + '/__timers_fixture__/reset').json()['ok']
            page.reload()
            expect(page.locator('#timers-presets-body tr')).to_have_count(2)
            page.locator('#timers-stage-delay-preset').select_option('2')
            expect(page.locator('#timers-stage-delay-send')).to_be_enabled()
            page.locator('#timers-stage-delay-minutes').fill('0')
            page.evaluate("""() => {
                const original = window.fetch;
                window.__moveHeld = false;
                window.fetch = async (url, options) => {
                    const response = await original(url, options);
                    if (String(url).endsWith('/api/timers/mutate') && JSON.parse(options.body).action === 'move_preset') {
                        window.__moveHeld = true;
                        await new Promise(resolve => {window.__releaseMove = resolve;});
                    }
                    return response;
                };
            }""")
            if name.startswith('mobile-'):
                page.locator('#timers-presets-body tr').nth(1).locator('[data-action=edit-toggle]').click()
            page.locator('#timers-presets-body tr').nth(1).locator('[data-action=up]').click()
            page.wait_for_function('window.__moveHeld')
            page.locator('#timers-stage-delay-send').click()
            page.evaluate('window.__releaseMove()')
            expect(page.locator('#timers-stage-delay-hint')).to_contain_text('Preset saved; applied; message sent')
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['presets'][0]['name'] == 'Delay' and health['presets'][0]['time'] == '23:59'
            assert health['calls'][0] == ['set', 1, '23:59'] and health['presses'] == ['location/1/0/1/press']
            expect(page.locator('#timers-stage-delay-preset')).to_have_value('1')

            # Preserve a pre-click draft, hold the device response and prove all
            # controls plus keyboard/structural fallback guards freeze execution.
            page.evaluate("""() => {
                const input = document.querySelector('input[data-role=preset-name]');
                input.value = 'Pending draft';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                const original = window.fetch;
                window.__delayHeld = false;
                window.fetch = async (url, options) => {
                    const response = await original(url, options);
                    if (String(url).endsWith('/api/propresenter/stage/stream_delay')) {
                        window.__delayHeld = true;
                        await new Promise(resolve => {window.__releaseDelay = resolve;});
                    }
                    return response;
                };
            }""")
            page.locator('#timers-stage-delay-minutes').fill('2')
            page.locator('#timers-stage-delay-send').click()
            page.wait_for_function('window.__delayHeld')
            enabled = page.evaluate("Array.from(document.querySelectorAll('#timers-page input, #timers-page select, #timers-page textarea, #timers-page button')).filter(el => !el.disabled).map(el => ({id: el.id, role: el.dataset.role, text: el.textContent}))")
            assert not enabled, enabled
            assert page.evaluate("""() => {
                const root = document.getElementById('timers-page');
                return ['click', 'input', 'change', 'keydown', 'beforeinput'].every(type =>
                    !root.dispatchEvent(new Event(type, {bubbles: true, cancelable: true})));
            }""")
            page.screenshot(path=str(artifacts / (name + '-execution-locked.png')), full_page=True)
            page.evaluate('window.__releaseDelay()')
            expect(page.locator('#timers-stage-delay-send')).to_be_enabled()
            expect(page.locator('#timers-presets-body tr').nth(0).locator('input[data-role=preset-name]')).to_have_value('Pending draft')
            expect(page.locator('#timers-presets-body tr').nth(0).locator('input[type=time]')).to_have_value('00:01')
            page.wait_for_timeout(800)  # past the detached row's former debounce
            health = context.request.get(url + '/__timers_fixture__/health').json()
            assert health['presets'][0]['name'] == 'Pending draft' and health['presets'][0]['time'] == '00:01'
            results.append({'browser': name, 'passed': True, 'calls': health['calls'], 'presses': health['presses']})
            (artifacts / 'results.json').write_text(json.dumps(results, indent=2))
            print(name + ' passed', flush=True)
            context.close(); browser.close()
    (artifacts / 'results.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
