from __future__ import annotations

import argparse
import queue
import socket
import sys
import threading
import time
import uuid

import requests
import sounddevice as sd


def _list_devices() -> int:
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        if int(dev.get('max_input_channels', 0) or 0) <= 0:
            continue
        print(f'[{idx}] {dev.get("name", "Unknown")} | inputs={dev.get("max_input_channels", 0)} | samplerate={dev.get("default_samplerate", "?")}')
    return 0


def _device_arg(value: str | None):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return s


def main() -> int:
    parser = argparse.ArgumentParser(description='Send live microphone audio to TDeck transcription ingest.')
    parser.add_argument('--server', required=False, default='http://127.0.0.1:5000', help='Base URL of the TDeck server.')
    parser.add_argument('--token', required=False, default='', help='Shared transcription ingest token from TDeck config.')
    parser.add_argument('--device', required=False, default=None, help='Input device index or exact device name.')
    parser.add_argument('--source-name', required=False, default=socket.gethostname(), help='Friendly source name shown in TDeck.')
    parser.add_argument('--sample-rate', type=int, default=16000, help='Capture sample rate. Default: 16000.')
    parser.add_argument('--chunk-ms', type=int, default=200, help='Chunk size in milliseconds. Default: 200.')
    parser.add_argument('--channels', type=int, default=1, help='Capture channels. Default: 1.')
    parser.add_argument('--list-devices', action='store_true', help='List available input devices and exit.')
    args = parser.parse_args()

    if args.list_devices:
        return _list_devices()

    server = str(args.server or '').rstrip('/')
    token = str(args.token or '').strip()
    if not server:
        print('error: --server is required', file=sys.stderr)
        return 2
    if not token:
        print('error: --token is required', file=sys.stderr)
        return 2

    source_id = str(uuid.uuid4())
    post_url = f'{server}/api/transcription/audio'
    q: queue.Queue[bytes] = queue.Queue(maxsize=32)
    stop_event = threading.Event()
    session = requests.Session()

    blocksize = max(1, int((args.sample_rate * max(40, args.chunk_ms)) / 1000))

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f'audio status: {status}', file=sys.stderr)
        try:
            q.put_nowait(bytes(indata))
        except queue.Full:
            try:
                q.get_nowait()
            except Exception:
                pass
            try:
                q.put_nowait(bytes(indata))
            except Exception:
                pass

    def upload_loop():
        backoff = 1.0
        while not stop_event.is_set():
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                res = session.post(
                    post_url,
                    data=chunk,
                    timeout=10,
                    headers={
                        'Content-Type': 'application/octet-stream',
                        'X-TDeck-Transcription-Token': token,
                        'X-Source-Id': source_id,
                        'X-Source-Name': str(args.source_name or '').strip() or socket.gethostname(),
                        'X-Audio-Sample-Rate': str(int(args.sample_rate)),
                        'X-Audio-Channels': str(int(args.channels)),
                        'X-Audio-Sample-Width': '2',
                    },
                )
                if res.ok:
                    backoff = 1.0
                    continue

                print(f'upload failed: HTTP {res.status_code} {res.text[:200]}', file=sys.stderr)
            except Exception as e:
                print(f'upload failed: {e}', file=sys.stderr)

            time.sleep(backoff)
            backoff = min(backoff * 1.8, 8.0)

    worker = threading.Thread(target=upload_loop, daemon=True)
    worker.start()

    try:
        with sd.RawInputStream(
            samplerate=int(args.sample_rate),
            blocksize=blocksize,
            dtype='int16',
            channels=int(args.channels),
            device=_device_arg(args.device),
            callback=audio_callback,
        ):
            print(f'Streaming microphone audio to {post_url}')
            print(f'Source name: {args.source_name}')
            if args.device is not None:
                print(f'Input device: {args.device}')
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping sender...')
    finally:
        stop_event.set()
        worker.join(timeout=2)
        try:
            session.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
