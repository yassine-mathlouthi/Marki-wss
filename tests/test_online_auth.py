import unittest
import asyncio
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.routers.rooms import get_rooms_router
from app.routers.websocket import get_websocket_router
from app.services.connection_manager import ConnectionManager
from app.services.game_service import GameService
from app.services.in_memory import InMemoryRoomStore
from app.services.room_service import RoomService
from app.core.config import Settings


class OnlineAuthenticationTest(unittest.TestCase):
    def test_rest_leave_retry_is_success(self) -> None:
        host = self._create_room()
        headers = {
            "Authorization": f'Bearer {host["sessionToken"]}',
            "Idempotency-Key": "completed-leave-operation",
        }
        payload = {"playerId": host["playerId"]}

        first = self.client.post(f'/rooms/{host["roomCode"]}/leave', json=payload, headers=headers)
        replay = self.client.post(f'/rooms/{host["roomCode"]}/leave', json=payload, headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["replayed"])
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])

    def setUp(self) -> None:
        self.connection_manager = ConnectionManager()
        settings = Settings()
        settings.disconnect_grace_seconds = 0.01
        self.game_service = GameService(settings)
        self.room_service = RoomService(InMemoryRoomStore(), self.game_service)
        app = FastAPI()
        app.include_router(get_rooms_router(self.room_service, self.connection_manager))
        app.include_router(
            get_websocket_router(
                self.room_service,
                self.connection_manager,
                self.game_service,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def _create_room(self) -> dict:
        response = self.client.post(
            "/rooms/create",
            headers={"Idempotency-Key": "create-test-key-0001"},
            json={"hostName": "Host", "maxPlayers": 4},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_session_token_is_returned_but_never_exposed_in_room(self) -> None:
        created = self._create_room()

        self.assertTrue(created["sessionToken"])
        self.assertNotIn("sessionToken", created["room"]["players"][0])
        self.assertNotIn("sessionTokenHash", created["room"]["players"][0])

    def test_room_fetch_requires_a_valid_session_token(self) -> None:
        created = self._create_room()
        room_url = f'/rooms/{created["roomCode"]}'

        missing = self.client.get(room_url)
        invalid = self.client.get(
            room_url,
            headers={"Authorization": "Bearer invalid"},
        )
        valid = self.client.get(
            room_url,
            headers={"Authorization": f'Bearer {created["sessionToken"]}'},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(valid.status_code, 200)

    def test_player_cannot_leave_as_another_player(self) -> None:
        host = self._create_room()
        joined_response = self.client.post(
            "/rooms/join",
            headers={"Idempotency-Key": "join-test-key-000001"},
            json={"roomCode": host["roomCode"], "playerName": "Guest"},
        )
        self.assertEqual(joined_response.status_code, 200)
        guest = joined_response.json()

        response = self.client.post(
            f'/rooms/{host["roomCode"]}/leave',
            headers={"Authorization": f'Bearer {host["sessionToken"]}', "Idempotency-Key": "unauthorized-leave-operation"},
            json={"playerId": guest["playerId"]},
        )

        self.assertEqual(response.status_code, 401)
        room = self.room_service.get_room(host["roomCode"])
        self.assertTrue(
            any(player.player_id == guest["playerId"] for player in room.players)
        )

    def test_websocket_rejects_impersonation_before_sending_snapshot(self) -> None:
        host = self._create_room()
        joined_response = self.client.post(
            "/rooms/join",
            headers={"Idempotency-Key": "join-test-key-000002"},
            json={"roomCode": host["roomCode"], "playerName": "Guest"},
        )
        guest = joined_response.json()

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{guest["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            with self.assertRaises(WebSocketDisconnect) as raised:
                websocket.receive_json()

        self.assertEqual(raised.exception.code, 1008)

    def test_create_retry_returns_same_credentials(self) -> None:
        payload = {"hostName": "Host", "maxPlayers": 4}
        headers = {"Idempotency-Key": "create-retry-key-01"}

        first = self.client.post("/rooms/create", headers=headers, json=payload)
        second = self.client.post("/rooms/create", headers=headers, json=payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["roomCode"], second.json()["roomCode"])
        self.assertEqual(first.json()["playerId"], second.json()["playerId"])
        self.assertEqual(first.json()["sessionToken"], second.json()["sessionToken"])

    def test_join_retry_does_not_add_duplicate_player(self) -> None:
        host = self._create_room()
        payload = {"roomCode": host["roomCode"], "playerName": "Guest"}
        headers = {"Idempotency-Key": "join-retry-key-0001"}

        first = self.client.post("/rooms/join", headers=headers, json=payload)
        second = self.client.post("/rooms/join", headers=headers, json=payload)
        room = self.room_service.get_room(host["roomCode"])

        self.assertEqual(first.json()["playerId"], second.json()["playerId"])
        self.assertEqual(first.json()["sessionToken"], second.json()["sessionToken"])
        self.assertEqual(len(room.players), 2)

    def test_idempotency_key_reuse_with_new_payload_is_rejected(self) -> None:
        headers = {"Idempotency-Key": "reused-entry-key-001"}
        first = self.client.post(
            "/rooms/create",
            headers=headers,
            json={"hostName": "Host", "maxPlayers": 4},
        )
        conflict = self.client.post(
            "/rooms/create",
            headers=headers,
            json={"hostName": "Other", "maxPlayers": 4},
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"], "idempotency_key_reused")

    def test_websocket_accepts_the_matching_player_token(self) -> None:
        host = self._create_room()

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{host["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            event = websocket.receive_json()

        self.assertEqual(event["type"], "room_snapshot")
        self.assertEqual(event["playerId"], host["playerId"])

    def test_malformed_json_returns_stable_error_and_socket_stays_usable(self) -> None:
        host = self._create_room()

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{host["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            websocket.receive_json()
            websocket.receive_json()
            websocket.send_text("{not-json")
            error = websocket.receive_json()

            self.assertEqual(error["type"], "error")
            self.assertEqual(error["payload"]["code"], "invalid_json")
            self.assertTrue(error["payload"]["correlationId"])

            websocket.send_json({"type": "ping", "commandId": "ping-after-error"})
            pong = websocket.receive_json()
            self.assertEqual(pong["type"], "pong")

    def test_protocol_errors_echo_command_correlation_id(self) -> None:
        host = self._create_room()

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{host["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            websocket.receive_json()
            websocket.receive_json()
            websocket.send_json(
                {"type": "unknown", "commandId": "command-correlation-1"}
            )
            error = websocket.receive_json()

        self.assertEqual(error["payload"]["code"], "unsupported_event")
        self.assertEqual(
            error["payload"]["correlationId"], "command-correlation-1"
        )

    def test_old_socket_cannot_disconnect_its_replacement(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        old_socket = FakeSocket()
        new_socket = FakeSocket()
        asyncio.run(
            self.connection_manager.connect(
                "ABC123", "p1", old_socket, accept=False
            )
        )
        asyncio.run(
            self.connection_manager.connect(
                "ABC123", "p1", new_socket, accept=False
            )
        )

        removed = self.connection_manager.disconnect("ABC123", "p1", old_socket)

        self.assertTrue(old_socket.closed)
        self.assertFalse(removed)
        self.assertTrue(
            self.connection_manager.disconnect("ABC123", "p1", new_socket)
        )

    def test_unexpected_handler_failure_still_marks_player_disconnected(self) -> None:
        host = self._create_room()

        with patch(
            "app.routers.websocket._handle_event",
            side_effect=RuntimeError("unexpected"),
        ):
            with self.client.websocket_connect(
                f'/ws/{host["roomCode"]}/{host["playerId"]}'
            ) as websocket:
                websocket.send_json(
                    {
                        "type": "authenticate",
                        "sessionToken": host["sessionToken"],
                    }
                )
                websocket.receive_json()
                websocket.receive_json()
                websocket.send_json(
                    {"type": "ping", "commandId": "failing-command"}
                )
                with self.assertRaises(WebSocketDisconnect):
                    websocket.receive_json()

        room = self.room_service.get_room(host["roomCode"])
        player = self.room_service.get_player(room, host["playerId"])
        self.assertFalse(player.is_connected)

    def test_lobby_settings_reject_malformed_and_excessive_values(self) -> None:
        host = self._create_room()
        invalid_payloads = [
            {"cardsPerPlayer": 1_000_000},
            {"maxPlayers": 9},
            {"cardsPerPlayer": "14"},
            {"regionId": ""},
            {"language": "e"},
            {"unexpected": True},
        ]

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{host["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            websocket.receive_json()
            websocket.receive_json()
            for payload in invalid_payloads:
                websocket.send_json(
                    {
                        "type": "update_lobby_settings",
                        "commandId": f"invalid-settings-{len(payload)}-{payload}",
                        "roomCode": host["roomCode"],
                        "playerId": host["playerId"],
                        "payload": payload,
                    }
                )
                event = websocket.receive_json()
                self.assertEqual(event["type"], "command_result")
                self.assertEqual(event["payload"]["status"], "rejected")
                self.assertEqual(event["payload"]["code"], "invalid_payload")

        room = self.room_service.get_room(host["roomCode"])
        self.assertEqual(room.settings.region_id, "tunisia")
        self.assertEqual(room.settings.cards_per_player, 11)
        self.assertEqual(room.settings.language, "en")
        self.assertEqual(room.max_players, 4)

    def test_lobby_settings_accept_valid_constrained_values(self) -> None:
        host = self._create_room()

        with self.client.websocket_connect(
            f'/ws/{host["roomCode"]}/{host["playerId"]}'
        ) as websocket:
            websocket.send_json(
                {"type": "authenticate", "sessionToken": host["sessionToken"]}
            )
            websocket.receive_json()
            websocket.receive_json()
            websocket.send_json(
                {
                    "type": "update_lobby_settings",
                    "commandId": "valid-settings-update",
                    "roomCode": host["roomCode"],
                    "playerId": host["playerId"],
                    "payload": {
                        "regionId": "world",
                        "cardsPerPlayer": 14,
                        "language": "fr",
                        "wrongAnswerBehavior": "returnToHand",
                        "maxPlayers": 8,
                    },
                }
            )
            event = websocket.receive_json()

        self.assertEqual(event["type"], "room_snapshot")
        snapshot = event["payload"]["snapshot"]
        self.assertEqual(snapshot["settings"]["regionId"], "world")
        self.assertEqual(snapshot["settings"]["cardsPerPlayer"], 14)
        self.assertEqual(snapshot["settings"]["language"], "fr")
        self.assertEqual(snapshot["maxPlayers"], 8)


if __name__ == "__main__":
    unittest.main()
