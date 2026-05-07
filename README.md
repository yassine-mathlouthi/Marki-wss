# MARKI Game Server

Scalable FastAPI backend for the MARKI Flutter mobile football card and trivia game. This MVP uses in-memory room storage for simple deployment, while the code is structured so Redis can be added later for multi-instance scaling.

## Features

- REST room lifecycle endpoints
- Public WebSocket endpoint for real-time gameplay
- Room code creation and case-insensitive join flow
- Host reassignment when the host leaves
- Player readiness, game start, score updates, and turn rotation
- CORS configuration through environment variables
- Clean service-based structure ready for Redis migration

## Project Structure

```text
marki-game-server/
  app/
    main.py
    core/
      config.py
    models/
      room.py
      player.py
      events.py
    services/
      room_service.py
      connection_manager.py
      game_service.py
    routers/
      rooms.py
      websocket.py
  requirements.txt
  render.yaml
  README.md
```

## Requirements

- Python 3.11+

## Run Locally

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Set environment variables if needed:

```bash
export CORS_ALLOW_ORIGINS="http://localhost:3000,http://localhost:5173"
export APP_NAME="MARKI Game Server"
export ENVIRONMENT="development"
```

4. Start the server:

```bash
uvicorn app.main:app --reload
```

5. Open the API:

```text
http://127.0.0.1:8000
```

## Deploy On Render

1. Push this project to a Git repository.
2. In Render, create a new Web Service.
3. Point Render to the repository and project root.
4. Render will use the included `render.yaml`:

```yaml
services:
  - type: web
    name: marki-game-server
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

5. Add environment variables in Render if needed:

```text
CORS_ALLOW_ORIGINS=https://your-flutter-web-preview.example.com
APP_NAME=MARKI Game Server
ENVIRONMENT=production
```

## REST API

### Health Check

`GET /`

Response:

```json
{
  "status": "ok",
  "service": "MARKI Game Server"
}
```

### Create Room

`POST /rooms/create`

Request:

```json
{
  "hostName": "Yassine",
  "maxPlayers": 4
}
```

Response:

```json
{
  "roomCode": "ABCD12",
  "playerId": "uuid",
  "room": {
    "roomCode": "ABCD12",
    "hostPlayerId": "uuid",
    "players": [],
    "status": "waiting",
    "maxPlayers": 4,
    "currentTurnPlayerId": "uuid",
    "scores": {
      "uuid": 0
    },
    "createdAt": "2026-05-07T10:00:00Z",
    "updatedAt": "2026-05-07T10:00:00Z"
  }
}
```

Example `curl`:

```bash
curl -X POST http://127.0.0.1:8000/rooms/create \
  -H "Content-Type: application/json" \
  -d '{"hostName":"Yassine","maxPlayers":4}'
```

### Join Room

`POST /rooms/join`

Request:

```json
{
  "roomCode": "ABCD12",
  "playerName": "Ali"
}
```

Example `curl`:

```bash
curl -X POST http://127.0.0.1:8000/rooms/join \
  -H "Content-Type: application/json" \
  -d '{"roomCode":"ABCD12","playerName":"Ali"}'
```

### Get Room State

`GET /rooms/{room_code}`

Example:

```bash
curl http://127.0.0.1:8000/rooms/ABCD12
```

### Leave Room

`POST /rooms/{room_code}/leave`

Request:

```json
{
  "playerId": "uuid"
}
```

Example:

```bash
curl -X POST http://127.0.0.1:8000/rooms/ABCD12/leave \
  -H "Content-Type: application/json" \
  -d '{"playerId":"uuid"}'
```

## WebSocket

URL example:

```text
wss://your-render-app.onrender.com/ws/ABCD12/player-id
```

Local example:

```text
ws://127.0.0.1:8000/ws/ABCD12/player-id
```

### Event Format

All events use this structure:

```json
{
  "type": "event_name",
  "roomCode": "ABCD12",
  "playerId": "uuid",
  "payload": {},
  "timestamp": "2026-05-07T10:00:00Z"
}
```

### Client Event Examples

Ready:

```json
{
  "type": "ready",
  "roomCode": "ABCD12",
  "playerId": "uuid",
  "payload": {
    "ready": true
  }
}
```

Start game:

```json
{
  "type": "start_game",
  "roomCode": "ABCD12",
  "playerId": "host-player-id",
  "payload": {}
}
```

Submit answer:

```json
{
  "type": "submit_answer",
  "roomCode": "ABCD12",
  "playerId": "uuid",
  "payload": {
    "answer": "Ronaldo",
    "cardId": "optional-card-id"
  }
}
```

Update score:

```json
{
  "type": "update_score",
  "roomCode": "ABCD12",
  "playerId": "host-player-id",
  "payload": {
    "targetPlayerId": "uuid",
    "points": 1
  }
}
```

Next turn:

```json
{
  "type": "next_turn",
  "roomCode": "ABCD12",
  "playerId": "host-player-id",
  "payload": {}
}
```

Ping:

```json
{
  "type": "ping",
  "roomCode": "ABCD12",
  "playerId": "uuid",
  "payload": {}
}
```

### Server Broadcast Examples

- `player_joined`
- `player_left`
- `player_connected`
- `player_disconnected`
- `player_ready_updated`
- `game_started`
- `answer_submitted`
- `score_updated`
- `turn_changed`
- `pong`

## Validation and Security Notes

- Room codes are normalized to uppercase and limited to 6 characters.
- Player names are limited to 32 characters.
- `maxPlayers` is limited to the range `2..8`.
- REST errors return safe messages through FastAPI `HTTPException`.
- Malformed WebSocket data and unsupported event types are handled without crashing the server.
- WebSocket connections are rejected when the room or player is invalid.

## Redis Migration Notes

This MVP stores rooms and active connections in memory, which means it is not enough for production scaling across multiple backend instances.

To scale later with Redis:

- Move room state storage from memory to Redis.
- Store ephemeral connection metadata in Redis if needed.
- Publish room events through Redis pub/sub so all app instances can broadcast consistently.
- Add room TTL and cleanup for abandoned rooms.

Without Redis, each Render instance would maintain its own isolated memory, so players connected to different instances would not share the same room state.
