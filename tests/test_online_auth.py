import unittest

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
    def setUp(self) -> None:
        self.connection_manager = ConnectionManager()
        self.room_service = RoomService(InMemoryRoomStore())
        app = FastAPI()
        app.include_router(get_rooms_router(self.room_service, self.connection_manager))
        app.include_router(
            get_websocket_router(
                self.room_service,
                self.connection_manager,
                GameService(Settings()),
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def _create_room(self) -> dict:
        response = self.client.post(
            "/rooms/create",
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
            json={"roomCode": host["roomCode"], "playerName": "Guest"},
        )
        self.assertEqual(joined_response.status_code, 200)
        guest = joined_response.json()

        response = self.client.post(
            f'/rooms/{host["roomCode"]}/leave',
            headers={"Authorization": f'Bearer {host["sessionToken"]}'},
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
                        "roomCode": host["roomCode"],
                        "playerId": host["playerId"],
                        "payload": payload,
                    }
                )
                event = websocket.receive_json()
                self.assertEqual(event["type"], "error")

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
