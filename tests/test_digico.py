from __future__ import annotations

import socket
import threading
import time
import unittest

from digico import (
    DigicoConfig,
    DigicoMixerClient,
    decode_osc_packet,
    encode_osc_message,
)


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class _DeskSimulator:
    def __init__(self, port: int) -> None:
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.1)
        self.sock.bind(("127.0.0.1", port))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.controls: list[tuple[str, list]] = []
        self.queries: list[str] = []

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.sock.close()
        self.thread.join(timeout=1)

    def _reply(self, target, address: str, args: list) -> None:
        try:
            self.sock.sendto(encode_osc_message(address, args), target)
        except OSError:
            # The test may be shutting down while the simulator is replying.
            pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, source = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            for address, args in decode_osc_packet(packet):
                if address.endswith("/?"):
                    self.queries.append(address)
                if address == "/Console/Channels/?":
                    self._reply(source, "/Console/Input_Channels", [2])
                elif address == "/Console/Aux_Outputs/modes/?":
                    self._reply(source, "/Console/Aux_Outputs/modes", [1, 2])
                elif address.startswith("/Aux_Outputs/") and address.endswith("/Buss_Trim/name/?"):
                    number = int(address.split("/")[2])
                    self._reply(source, address[:-2], [f"Aux {number}"])
                elif address.startswith("/Input_Channels/") and address.endswith("/Channel_Input/name/?"):
                    number = int(address.split("/")[2])
                    self._reply(source, address[:-2], [f"Input {number}"])
                elif address == "/Snapshots/Current_Snapshot/?":
                    self._reply(source, "/Snapshots/Current_Snapshot", [-1])
                elif address.endswith("/send_level/?"):
                    self._reply(source, address[:-2], [-20.0])
                elif address.endswith("/send_on/?"):
                    self._reply(source, address[:-2], [1])
                elif address.endswith("/send_pan/?"):
                    self._reply(source, address[:-2], [0.5])
                elif address.endswith(("/send_level", "/send_on", "/send_pan")):
                    self.controls.append((address, list(args)))
                    self._reply(source, address, list(args))


class OscCodecTests(unittest.TestCase):
    def test_round_trip_supported_types(self):
        packet = encode_osc_message("/test", [7, 1.25, "hello", True, False, None])
        messages = decode_osc_packet(packet)
        self.assertEqual(messages[0][0], "/test")
        self.assertEqual(messages[0][1][0], 7)
        self.assertAlmostEqual(messages[0][1][1], 1.25)
        self.assertEqual(messages[0][1][2:], ["hello", True, False, None])

    def test_digico_address_only_packet_is_accepted(self):
        packet = b"/Console/Session/!\x00"
        packet += b"\x00" * ((4 - len(packet) % 4) % 4)
        self.assertEqual(decode_osc_packet(packet), [("/Console/Session/!", [])])


class DigicoClientIntegrationTests(unittest.TestCase):
    def test_discovery_state_and_control(self):
        desk_port = _free_udp_port()
        listen_port = _free_udp_port()
        desk = _DeskSimulator(desk_port)
        desk.start()
        client = DigicoMixerClient(
            DigicoConfig(
                enabled=True,
                host="127.0.0.1",
                port=desk_port,
                listen_address="127.0.0.1",
                listen_port=listen_port,
                request_interval=0.025,
                retry_interval=0.1,
                stale_after=2.0,
                auxes=(
                    {"order": 2, "icon": "vocals"},
                    {"order": 1, "icon": "drums"},
                ),
                channels=(
                    {"order": 2, "group": "Vocals", "icon": "vocals"},
                    {"order": 1, "group": "Band", "icon": "keyboard"},
                ),
            )
        )
        try:
            client.start()
            deadline = time.time() + 3
            while time.time() < deadline and not client.status()["ready"]:
                time.sleep(0.025)
            status = client.status()
            self.assertTrue(status["ready"], status)
            self.assertEqual(status["channels"], 2)
            self.assertEqual(status["auxes"], 2)

            config = client.mixer_config()
            self.assertEqual([item["channel"] for item in config["channels"]], [2, 1])
            self.assertEqual([item["group"] for item in config["channels"]], ["Band", "Vocals"])
            self.assertEqual([item["icon"] for item in config["channels"]], ["keyboard", "vocals"])
            self.assertEqual([item["channel"] for item in config["auxes"]], [2, 1])
            self.assertEqual([item["icon"] for item in config["auxes"]], ["drums", "vocals"])
            self.assertTrue(config["auxes"][0]["stereo"])
            self.assertFalse(config["auxes"][1]["stereo"])

            state = client.aux_state(2)
            deadline = time.time() + 2
            while time.time() < deadline and any(
                c["sendOn"] is None or c["level"] is None or c["pan"] is None
                for c in state["channels"]
            ):
                time.sleep(0.05)
                state = client.aux_state(2)
            self.assertTrue(all(c["level"] == -20.0 for c in state["channels"]))
            self.assertTrue(all(c["pan"] == 0.5 for c in state["channels"]))
            self.assertTrue(all(c["sendOn"] is True for c in state["channels"]))
            aux_two_queries = [address for address in desk.queries if "/Aux_Send/2/" in address]
            last_level = max(i for i, address in enumerate(aux_two_queries) if "/send_level/" in address)
            first_on = min(i for i, address in enumerate(aux_two_queries) if "/send_on/" in address)
            last_on = max(i for i, address in enumerate(aux_two_queries) if "/send_on/" in address)
            first_pan = min(i for i, address in enumerate(aux_two_queries) if "/send_pan/" in address)
            self.assertLess(last_level, first_on)
            self.assertLess(last_on, first_pan)

            # Mono AUXes only need levels. Once received, browser polling uses
            # the cache instead of continuously re-querying the desk.
            mono_state = client.aux_state(1)
            deadline = time.time() + 2
            while time.time() < deadline and any(
                c["sendOn"] is None or c["level"] is None for c in mono_state["channels"]
            ):
                time.sleep(0.05)
                mono_state = client.aux_state(1)
            mono_level_query = "/Aux_Send/1/send_level/?"
            mono_on_query = "/Aux_Send/1/send_on/?"
            mono_pan_query = "/Aux_Send/1/send_pan/?"
            level_query_count = sum(mono_level_query in address for address in desk.queries)
            on_query_count = sum(mono_on_query in address for address in desk.queries)
            self.assertEqual(level_query_count, 2)
            self.assertEqual(on_query_count, 2)
            self.assertFalse(any(mono_pan_query in address for address in desk.queries))
            for _ in range(4):
                client.aux_state(1)
            time.sleep(0.15)
            self.assertEqual(
                sum(mono_level_query in address for address in desk.queries),
                level_query_count,
            )
            self.assertEqual(
                sum(mono_on_query in address for address in desk.queries),
                on_query_count,
            )

            unknown = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                unknown.bind(("127.0.0.3", 0))
                unknown.sendto(
                    encode_osc_message("/Console/Input_Channels", [99]),
                    ("127.0.0.1", listen_port),
                )
                time.sleep(0.05)
                self.assertEqual(client.status()["channels"], 2)
                self.assertEqual(client.status()["ignoredPackets"], 1)
            finally:
                unknown.close()

            self.assertEqual(client.set_level(2, 1, -12.5), -12.5)
            self.assertEqual(client.set_pan(2, 1, 0.25), 0.25)
            self.assertFalse(client.set_send_on(2, 1, False))
            deadline = time.time() + 1
            while time.time() < deadline and len(desk.controls) < 3:
                time.sleep(0.025)
            self.assertEqual(len(desk.controls), 3)
            self.assertEqual(desk.controls[-1][0], "/Input_Channels/1/Aux_Send/2/send_on")
            self.assertEqual(desk.controls[-1][1], [0])

            # The idle heartbeat keeps the desk online after discovery even
            # when no browser is actively polling an AUX.
            time.sleep(2.1)
            self.assertTrue(client.status()["connected"], client.status())
        finally:
            client.close()
            desk.close()

    def test_configured_external_device_can_relay_osc(self):
        desk = _DeskSimulator(_free_udp_port())
        external = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        external.settimeout(2.0)
        external.bind(("127.0.0.2", 0))
        listen_port = _free_udp_port()
        desk.start()
        client = DigicoMixerClient(
            DigicoConfig(
                enabled=True,
                host="127.0.0.1",
                port=desk.port,
                listen_address="127.0.0.1",
                listen_port=listen_port,
                request_interval=0.025,
                retry_interval=0.1,
                stale_after=2.0,
                external_devices=(
                    {
                        "enabled": True,
                        "name": "Test iPad",
                        "ip": "127.0.0.2",
                        "port": external.getsockname()[1],
                        "broadcast": True,
                        "loopback": False,
                    },
                ),
            )
        )
        try:
            client.start()
            raw, _source = external.recvfrom(65535)
            self.assertTrue(decode_osc_packet(raw))

            external.sendto(
                encode_osc_message(
                    "/Input_Channels/1/Aux_Send/1/send_level",
                    [-8.0],
                ),
                ("127.0.0.1", listen_port),
            )
            deadline = time.time() + 2
            while time.time() < deadline and not desk.controls:
                time.sleep(0.025)
            self.assertTrue(desk.controls)
            self.assertEqual(
                desk.controls[-1][0],
                "/Input_Channels/1/Aux_Send/1/send_level",
            )
            self.assertGreater(client.status()["relayPackets"], 0)
        finally:
            client.close()
            desk.close()
            external.close()


if __name__ == "__main__":
    unittest.main()
