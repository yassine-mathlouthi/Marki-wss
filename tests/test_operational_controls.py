import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.core.config import Settings
from app.models.game import LobbySettings
from app.services.in_memory import InMemoryRoomStore
from app.services.operational_controls import OperationalControls
from app.services.room_service import RoomService


class OperationalControlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.settings = Settings()
        self.settings.room_create_rate_limit = 2
        self.settings.room_join_rate_limit = 2
        self.settings.room_rate_window_seconds = 10
        self.settings.max_active_rooms_per_ip = 1
        self.settings.max_sockets_per_ip = 1
        self.settings.ws_event_rate_limit = 2
        self.settings.ws_event_rate_window_seconds = 10
        self.controls = OperationalControls(
            self.settings, clock=lambda: self.now
        )

    def test_http_and_event_rates_are_bounded_and_recover(self) -> None:
        self.controls.check_http_rate("create", "client")
        self.controls.check_http_rate("create", "client")
        with self.assertRaises(HTTPException) as raised:
            self.controls.check_http_rate("create", "client")
        self.assertEqual(raised.exception.status_code, 429)

        self.controls.check_event_rate("ABC123", "p1")
        self.controls.check_event_rate("ABC123", "p1")
        with self.assertRaises(HTTPException):
            self.controls.check_event_rate("ABC123", "p1")

        self.now = 11
        self.controls.check_http_rate("create", "client")
        self.controls.check_event_rate("ABC123", "p1")

    def test_room_and_socket_capacity_are_released(self) -> None:
        self.controls.register_room("client", "ROOM01")
        with self.assertRaises(HTTPException):
            self.controls.check_room_capacity("client", "ROOM02")
        self.controls.remove_room("ROOM01")
        self.controls.check_room_capacity("client", "ROOM02")

        self.controls.admit_socket("client", "ROOM02", "p1", 1)
        with self.assertRaises(HTTPException):
            self.controls.admit_socket("client", "ROOM03", "p2", 2)
        self.controls.release_socket("ROOM02", "p1", 1)
        self.controls.admit_socket("client", "ROOM03", "p2", 2)

    def test_reconnect_replaces_principal_without_consuming_another_socket(self) -> None:
        self.assertFalse(
            self.controls.admit_socket("client", "ROOM01", "p1", 1)
        )
        self.assertTrue(
            self.controls.admit_socket("client", "ROOM01", "p1", 2)
        )
        self.controls.release_socket("ROOM01", "p1", 1)
        self.assertEqual(self.controls.active_socket_count, 1)

    def test_abandoned_rooms_expire_but_connected_rooms_remain(self) -> None:
        store = InMemoryRoomStore()
        service = RoomService(store)
        old_room, _, _ = service.create_room("Old", 4, LobbySettings())
        active_room, active_player, _ = service.create_room(
            "Active", 4, LobbySettings()
        )
        old_room.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        active_room.updated_at = old_room.updated_at
        active_player.is_connected = True

        expired = service.expire_abandoned_rooms(
            now=datetime.now(timezone.utc), ttl_seconds=60
        )

        self.assertEqual(expired, [old_room.room_code])
        self.assertEqual(service.active_room_count(), 1)

    def test_metrics_have_bounded_non_sensitive_labels(self) -> None:
        self.controls.increment("socket_errors", "invalid_json")
        output = self.controls.render_metrics(active_rooms=3)

        self.assertIn("marki_rooms_active 3", output)
        self.assertIn("marki_socket_errors_total", output)
        self.assertNotIn("client", output)
        self.assertNotIn("ROOM", output)


if __name__ == "__main__":
    unittest.main()
