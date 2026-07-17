from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, status

from app.core.config import Settings


class OperationalControls:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._rooms_by_ip: dict[str, set[str]] = defaultdict(set)
        self._sockets: dict[tuple[str, str], tuple[str, int]] = {}
        self._counters: dict[tuple[str, str], int] = defaultdict(int)

    def check_http_rate(self, action: str, client_ip: str) -> None:
        limit = (
            self.settings.room_create_rate_limit
            if action == "create"
            else self.settings.room_join_rate_limit
        )
        self._check_rate(
            f"http_{action}",
            client_ip,
            limit,
            self.settings.room_rate_window_seconds,
            f"{action}_rate_limited",
        )

    def check_room_capacity(self, client_ip: str, room_code: str | None = None) -> None:
        rooms = self._rooms_by_ip[client_ip]
        if room_code in rooms:
            return
        if len(rooms) >= self.settings.max_active_rooms_per_ip:
            self.increment("rate_limit_rejections", "room_capacity")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "room_limit_exceeded"},
            )

    def register_room(self, client_ip: str, room_code: str) -> None:
        self._rooms_by_ip[client_ip].add(room_code)

    def remove_room(self, room_code: str) -> None:
        for client_ip in list(self._rooms_by_ip):
            self._rooms_by_ip[client_ip].discard(room_code)
            if not self._rooms_by_ip[client_ip]:
                self._rooms_by_ip.pop(client_ip, None)
        for principal in [key for key in self._sockets if key[0] == room_code]:
            self._sockets.pop(principal, None)

    def prune_stale_rooms(self, active_room_codes: set[str]) -> int:
        stale_room_codes = {
            room_code
            for rooms in self._rooms_by_ip.values()
            for room_code in rooms
            if room_code not in active_room_codes
        }
        for room_code in stale_room_codes:
            self.remove_room(room_code)
        if stale_room_codes:
            self.add("stale_room_accounting", "pruned", len(stale_room_codes))
        return len(stale_room_codes)

    def prune_expired_buckets(self) -> None:
        now = self._clock()
        for key, bucket in list(self._events.items()):
            window_seconds = self._window_seconds_for(key[0])
            while bucket and now - bucket[0] >= window_seconds:
                bucket.popleft()
            if not bucket:
                self._events.pop(key, None)

    def admit_socket(
        self,
        client_ip: str,
        room_code: str,
        player_id: str,
        socket_identity: int,
    ) -> bool:
        principal = (room_code, player_id)
        previous = self._sockets.get(principal)
        if previous is None:
            active_for_ip = sum(1 for ip, _ in self._sockets.values() if ip == client_ip)
            if active_for_ip >= self.settings.max_sockets_per_ip:
                self.increment("rate_limit_rejections", "socket_ip")
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"code": "socket_limit_exceeded"},
                )
        self._sockets[principal] = (client_ip, socket_identity)
        self.increment("socket_connections", "total")
        if previous is not None:
            self.increment("reconnects", "total")
        return previous is not None

    def release_socket(self, room_code: str, player_id: str, socket_identity: int) -> None:
        principal = (room_code, player_id)
        current = self._sockets.get(principal)
        if current is not None and current[1] == socket_identity:
            self._sockets.pop(principal, None)

    def check_event_rate(self, room_code: str, player_id: str) -> None:
        self._check_rate(
            "ws_event",
            f"{room_code}:{player_id}",
            self.settings.ws_event_rate_limit,
            self.settings.ws_event_rate_window_seconds,
            "event_rate_limited",
        )

    def increment(self, metric: str, category: str = "total") -> None:
        self._counters[(metric, category)] += 1

    def add(self, metric: str, category: str, value: int) -> None:
        self._counters[(metric, category)] += value

    @property
    def active_socket_count(self) -> int:
        return len(self._sockets)

    def render_metrics(
        self,
        active_rooms: int,
        *,
        connected_players: int = 0,
        disconnected_retained_players: int = 0,
    ) -> str:
        lines = [
            f"marki_rooms_active {active_rooms}",
            f"marki_players_connected {connected_players}",
            f"marki_players_disconnected_retained {disconnected_retained_players}",
            f"marki_websocket_connections_active {self.active_socket_count}",
        ]
        for (metric, category), value in sorted(self._counters.items()):
            lines.append(f'marki_{metric}_total{{category="{category}"}} {value}')
        return "\n".join(lines) + "\n"

    def _check_rate(
        self,
        action: str,
        identity: str,
        limit: int,
        window_seconds: float,
        error_code: str,
    ) -> None:
        now = self._clock()
        bucket = self._events[(action, identity)]
        while bucket and now - bucket[0] >= window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            self.increment("rate_limit_rejections", action)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": error_code},
            )
        bucket.append(now)

    def _window_seconds_for(self, action: str) -> float:
        if action == "ws_event":
            return self.settings.ws_event_rate_window_seconds
        return self.settings.room_rate_window_seconds
