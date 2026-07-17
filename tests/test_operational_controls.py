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

    def test_final_player_leave_releases_owned_room_capacity(self) -> None:
        store = InMemoryRoomStore()
        service = RoomService(store)
        service.add_room_deleted_listener(self.controls.remove_room)

        for index in range(6):
            room, host, _ = service.create_room("Host", 4, LobbySettings())
            self.controls.check_room_capacity("client")
            self.controls.register_room("client", room.room_code)
            service.leave_room(room.room_code, host.player_id)
            self.controls.check_room_capacity("client")
            self.assertEqual(service.active_room_count(), 0, index)

    def test_joined_rooms_do_not_consume_created_room_capacity(self) -> None:
        self.controls.register_room("owner", "ROOM01")
        self.controls.check_room_capacity("guest")

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
        old_room.all_disconnected_at = datetime.now(timezone.utc) - timedelta(hours=1)
        old_room.expires_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        active_room.all_disconnected_at = old_room.all_disconnected_at
        active_room.expires_at = old_room.expires_at
        active_player.is_connected = True

        expired = service.expire_abandoned_rooms(
            now=datetime.now(timezone.utc), ttl_seconds=60
        )

        self.assertEqual(expired, [old_room.room_code])
        self.assertEqual(service.active_room_count(), 1)

    def test_room_expiry_uses_phase_specific_lifecycle_deadline(self) -> None:
        store = InMemoryRoomStore()
        service = RoomService(
            store,
            waiting_room_ttl_seconds=60,
            playing_room_ttl_seconds=600,
            finished_room_ttl_seconds=30,
        )
        room, host, _ = service.create_room("Host", 4, LobbySettings())
        disconnected_at = room.all_disconnected_at

        self.assertIsNotNone(disconnected_at)
        self.assertEqual(
            room.expires_at,
            disconnected_at + timedelta(seconds=60),
        )

        service.mark_connected(room.room_code, host.player_id, True)
        self.assertIsNone(room.all_disconnected_at)
        self.assertIsNone(room.expires_at)

        service.mark_connected(room.room_code, host.player_id, False)
        self.assertIsNotNone(room.all_disconnected_at)
        self.assertEqual(
            room.expires_at,
            room.all_disconnected_at + timedelta(seconds=60),
        )

    def test_full_lobby_evicts_expired_disconnected_player(self) -> None:
        store = InMemoryRoomStore()
        service = RoomService(store)
        room, host, _ = service.create_room("Host", 2, LobbySettings())
        room, guest, guest_token = service.join_room(room.room_code, "Guest")
        service.mark_connected(room.room_code, host.player_id, True)
        service.mark_connected(room.room_code, guest.player_id, True)
        room = service.mark_connected(room.room_code, guest.player_id, False)
        service.expire_disconnected_player(
            room.room_code,
            guest.player_id,
            guest.connection_epoch,
        )

        room, replacement, _ = service.join_room(room.room_code, "Replacement")

        self.assertNotEqual(replacement.player_id, guest.player_id)
        with self.assertRaises(HTTPException) as raised:
            service.authenticate_player(room, guest.player_id, guest_token)
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(raised.exception.detail, {"code": "player_removed"})

    def test_metrics_have_bounded_non_sensitive_labels(self) -> None:
        self.controls.increment("socket_errors", "invalid_json")
        self.controls.add("websocket_message_bytes", "room_snapshot", 123)
        output = self.controls.render_metrics(active_rooms=3)

        self.assertIn("marki_rooms_active 3", output)
        self.assertIn("marki_socket_errors_total", output)
        self.assertIn("marki_websocket_message_bytes_total", output)
        self.assertNotIn("client", output)
        self.assertNotIn("ROOM", output)

    def test_expired_rate_buckets_are_pruned(self) -> None:
        self.controls.check_http_rate("create", "client")
        self.controls.check_event_rate("ABC123", "p1")

        self.now = 11
        self.controls.prune_expired_buckets()

        output = self.controls.render_metrics(active_rooms=0)
        self.assertIn("marki_rooms_active 0", output)
        self.controls.check_http_rate("create", "client")
        self.controls.check_event_rate("ABC123", "p1")


if __name__ == "__main__":
    unittest.main()
