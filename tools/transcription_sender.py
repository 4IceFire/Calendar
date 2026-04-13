from __future__ import annotations

import argparse
import sys
import time

from transcription_sender_lib import SenderService, list_input_devices, normalize_sender_config


def _list_devices() -> int:
    for dev in list_input_devices():
        print(f'[{dev["id"]}] {dev["name"]} | inputs={dev["max_input_channels"]} | samplerate={dev["default_samplerate"]}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Send live microphone audio to TDeck transcription ingest.')
    parser.add_argument('--server', required=False, default='http://127.0.0.1:5000', help='Base URL of the TDeck server.')
    parser.add_argument('--token', required=False, default='', help='Shared transcription ingest token from TDeck config.')
    parser.add_argument('--device', required=False, default=None, help='Input device index or exact device name.')
    parser.add_argument('--source-name', required=False, default='', help='Friendly source name shown in TDeck.')
    parser.add_argument('--sample-rate', type=int, default=16000, help='Capture sample rate. Default: 16000.')
    parser.add_argument('--chunk-ms', type=int, default=200, help='Chunk size in milliseconds. Default: 200.')
    parser.add_argument('--channels', type=int, default=1, help='Capture channels. Default: 1.')
    parser.add_argument('--list-devices', action='store_true', help='List available input devices and exit.')
    args = parser.parse_args()

    if args.list_devices:
        return _list_devices()

    cfg = normalize_sender_config({
        'server': args.server,
        'token': args.token,
        'device': args.device or '',
        'source_name': args.source_name,
        'sample_rate': args.sample_rate,
        'chunk_ms': args.chunk_ms,
        'channels': args.channels,
    })

    if not str(cfg.get('server') or '').strip():
        print('error: --server is required', file=sys.stderr)
        return 2
    if not str(cfg.get('token') or '').strip():
        print('error: --token is required', file=sys.stderr)
        return 2

    service = SenderService()
    ok, err = service.start(cfg)
    if not ok:
        print(f'error: {err or "unable to start sender"}', file=sys.stderr)
        return 2

    print(f'Streaming microphone audio to {cfg["server"]}/api/transcription/audio')
    print(f'Source name: {cfg["source_name"]}')
    if cfg.get('device'):
        print(f'Input device: {cfg["device"]}')

    try:
        while True:
            st = service.status_snapshot()
            if st.get('status') == 'error':
                print(f'upload failed: {st.get("last_error")}', file=sys.stderr)
                time.sleep(2)
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping sender...')
    finally:
        service.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
