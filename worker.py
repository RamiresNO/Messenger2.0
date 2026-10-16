import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from js import Headers, Object, Response as JSResponse
from pyodide.ffi import to_js
from workers import WorkerEntrypoint


MAX_REQUEST_BYTES = 16 * 1024
MAX_USERNAME_LENGTH = 30
MAX_TITLE_LENGTH = 80
MAX_MESSAGE_LENGTH = 4000
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class MessengerError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _python_value(value):
    converter = getattr(value, "to_py", None)
    return converter() if callable(converter) else value


def _rows(result):
    return _python_value(result.results)


def _user(row):
    row = _python_value(row)
    return {"id": row["id"], "username": row["username"], "createdAt": row["created_at"]}


def _message(row):
    row = _python_value(row)
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "senderId": row["sender_id"],
        "text": row["text"],
        "createdAt": row["created_at"],
    }


def _json_response(payload, status=200):
    headers = Headers.new()
    headers.set("Content-Type", "application/json; charset=utf-8")
    headers.set("Cache-Control", "no-store")
    init = to_js({"status": status, "headers": headers}, dict_converter=Object.fromEntries)
    return JSResponse.new(json.dumps(payload, ensure_ascii=False), init)


def _secure_response(response):
    headers = Headers.new(response.headers)
    headers.set("X-Content-Type-Options", "nosniff")
    headers.set("X-Frame-Options", "DENY")
    headers.set("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    init = to_js(
        {"status": response.status, "headers": headers},
        dict_converter=Object.fromEntries,
    )
    return JSResponse.new(response.body, init)


async def _read_json(request):
    content_length = request.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_REQUEST_BYTES:
        raise MessengerError("Запит завеликий.")
    text = await request.text()
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise MessengerError("Запит завеликий.")
    if not text:
        raise json.JSONDecodeError("Empty request body", "", 0)
    return json.loads(text)


def _require_string(body, key):
    if not isinstance(body, dict) or not isinstance(body.get(key), str):
        raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
    return body[key]


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _get_user(db, user_id):
    row = await db.prepare(
        "SELECT id, username, created_at FROM users WHERE id = ?"
    ).bind(user_id).first()
    return _user(row) if row else None


async def _handle_api(request, env, path, query):
    db = env.DB

    if path == "/api/users" and request.method == "GET":
        result = await db.prepare(
            "SELECT id, username, created_at FROM users ORDER BY username COLLATE NOCASE"
        ).all()
        return _json_response({"users": [_user(row) for row in _rows(result)]})

    if path == "/api/users" and request.method == "POST":
        body = await _read_json(request)
        username = _require_string(body, "username").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", username):
            raise MessengerError(
                "Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення."
            )
        if len(username) > MAX_USERNAME_LENGTH:
            raise MessengerError(f"Ім’я не може бути довшим за {MAX_USERNAME_LENGTH} символів.")
        user_id = str(uuid.uuid4())
        created_at = _timestamp()
        row = await db.prepare(
            """INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)
               ON CONFLICT(username) DO NOTHING
               RETURNING id, username, created_at"""
        ).bind(user_id, username, created_at).first()
        if not row:
            raise MessengerError("Це ім’я вже зайняте.", 409)
        return _json_response({"user": _user(row)}, 201)

    if path == "/api/conversations" and request.method == "GET":
        user_id = query.get("userId", [""])[0]
        if not user_id or not await _get_user(db, user_id):
            raise MessengerError("Користувача не знайдено.", 404)
        result = await db.prepare(
            """SELECT c.id, c.title, c.created_at, cp.user_id
               FROM conversations AS c
               JOIN conversation_participants AS cp ON cp.conversation_id = c.id
               WHERE c.id IN (
                 SELECT conversation_id FROM conversation_participants WHERE user_id = ?
               )
               ORDER BY c.title COLLATE NOCASE, cp.user_id"""
        ).bind(user_id).all()
        conversations = {}
        for row in _rows(result):
            conversation = conversations.setdefault(row["id"], {
                "id": row["id"],
                "title": row["title"],
                "participantIds": [],
                "createdAt": row["created_at"],
            })
            conversation["participantIds"].append(row["user_id"])
        return _json_response({"conversations": list(conversations.values())})

    if path == "/api/conversations" and request.method == "POST":
        body = await _read_json(request)
        if not isinstance(body, dict) or not isinstance(body.get("participantIds"), list):
            raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
        participant_ids = body["participantIds"]
        if not all(isinstance(user_id, str) for user_id in participant_ids):
            raise MessengerError("Перевірте список учасників.")
        title = _require_string(body, "title").strip()
        if not title:
            raise MessengerError("Назва розмови не може бути порожньою.")
        if len(title) > MAX_TITLE_LENGTH:
            raise MessengerError(f"Назва не може бути довшою за {MAX_TITLE_LENGTH} символів.")
        participant_ids = list(dict.fromkeys(participant_ids))
        if len(participant_ids) < 2:
            raise MessengerError("У розмові мають бути щонайменше двоє учасників.")
        placeholders = ", ".join("?" for _ in participant_ids)
        result = await db.prepare(
            f"SELECT id FROM users WHERE id IN ({placeholders})"
        ).bind(*participant_ids).all()
        if len(_rows(result)) != len(participant_ids):
            raise MessengerError("Усі учасники мають бути зареєстрованими користувачами.")

        conversation = {
            "id": str(uuid.uuid4()),
            "title": title,
            "participantIds": participant_ids,
            "createdAt": _timestamp(),
        }
        statements = [db.prepare(
            "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)"
        ).bind(conversation["id"], title, conversation["createdAt"])]
        statements.extend(
            db.prepare(
                "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)"
            ).bind(conversation["id"], user_id)
            for user_id in participant_ids
        )
        await db.batch(to_js(statements))
        return _json_response({"conversation": conversation}, 201)

    message_route = re.fullmatch(r"/api/conversations/([^/]+)/messages", path)
    if message_route:
        conversation_id = message_route.group(1)
        if request.method == "GET":
            user_id = query.get("userId", [""])[0]
            participant = await db.prepare(
                "SELECT 1 AS found FROM conversation_participants WHERE conversation_id = ? AND user_id = ?"
            ).bind(conversation_id, user_id).first() if user_id else None
            if not participant:
                raise MessengerError("Розмову не знайдено.", 404)
            result = await db.prepare(
                """SELECT id, conversation_id, sender_id, text, created_at
                   FROM messages WHERE conversation_id = ? ORDER BY created_at, id"""
            ).bind(conversation_id).all()
            return _json_response({"messages": [_message(row) for row in _rows(result)]})

        if request.method == "POST":
            body = await _read_json(request)
            sender_id = _require_string(body, "senderId")
            text = _require_string(body, "text")
            if not text.strip():
                raise MessengerError("Повідомлення не може бути порожнім.")
            if len(text) > MAX_MESSAGE_LENGTH:
                raise MessengerError(f"Повідомлення не може бути довшим за {MAX_MESSAGE_LENGTH} символів.")
            participant = await db.prepare(
                "SELECT 1 AS found FROM conversation_participants WHERE conversation_id = ? AND user_id = ?"
            ).bind(conversation_id, sender_id).first()
            if not participant:
                raise MessengerError("Повідомлення можуть надсилати лише учасники розмови.")
            message = {
                "id": str(uuid.uuid4()),
                "conversationId": conversation_id,
                "senderId": sender_id,
                "text": text,
                "createdAt": _timestamp(),
            }
            await db.prepare(
                """INSERT INTO messages (id, conversation_id, sender_id, text, created_at)
                   VALUES (?, ?, ?, ?, ?)"""
            ).bind(
                message["id"], conversation_id, sender_id, text, message["createdAt"]
            ).run()
            return _json_response({"message": message}, 201)

    if path == "/api/search" and request.method == "GET":
        user_id = query.get("userId", [""])[0]
        if not user_id or not await _get_user(db, user_id):
            raise MessengerError("Користувача не знайдено.", 404)
        search_query = query.get("q", [""])[0].strip()
        if not search_query:
            raise MessengerError("Введіть текст для пошуку.")
        result = await db.prepare(
            """SELECT m.id, m.conversation_id, m.sender_id, m.text, m.created_at
               FROM messages AS m
               JOIN conversation_participants AS cp ON cp.conversation_id = m.conversation_id
               WHERE cp.user_id = ? ORDER BY m.created_at, m.id"""
        ).bind(user_id).all()
        normalized_query = search_query.casefold()
        messages = [
            _message(row) for row in _rows(result)
            if normalized_query in row["text"].casefold()
        ]
        return _json_response({"messages": messages})

    return _json_response({"error": "Маршрут не знайдено."}, 404)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        try:
            parsed = urlsplit(str(request.url))
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                response = await _handle_api(request, self.env, path, parse_qs(parsed.query))
            else:
                response = await self.env.ASSETS.fetch(request)
            return _secure_response(response)
        except MessengerError as error:
            return _secure_response(_json_response({"error": str(error)}, error.status_code))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _secure_response(_json_response({"error": "Некоректний формат JSON."}, 400))
        except Exception:
            return _secure_response(_json_response({"error": "Сталася внутрішня помилка сервера."}, 500))