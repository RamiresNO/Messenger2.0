import json
import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, unquote, urlsplit

from auth import (
    PASSWORD_BYTES,
    PASSWORD_ITERATIONS,
    decode_password_hash,
    encode_password_hash,
    hash_session_token,
    new_password_salt,
    new_session_token,
    username_from_email,
    validate_password,
)
from js import Headers, Object, Response as JSResponse, Uint8Array, console, crypto, fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint


MAX_REQUEST_BYTES = 16 * 1024
MAX_USERNAME_LENGTH = 30
MAX_TITLE_LENGTH = 80
MAX_MESSAGE_LENGTH = 4000
SESSION_TTL = timedelta(days=30)
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


def _json_response(payload, status=200, response_headers=None):
    headers = Headers.new()
    headers.set("Content-Type", "application/json; charset=utf-8")
    headers.set("Cache-Control", "no-store")
    for name, value in (response_headers or {}).items():
        headers.set(name, value)
    init = to_js({"status": status, "headers": headers}, dict_converter=Object.fromEntries)
    return JSResponse.new(json.dumps(payload, ensure_ascii=False), init)


def _redirect_response(location, cookie=None):
    headers = Headers.new()
    headers.set("Location", location)
    headers.set("Cache-Control", "no-store")
    if cookie:
        headers.set("Set-Cookie", cookie)
    init = to_js({"status": 302, "headers": headers}, dict_converter=Object.fromEntries)
    return JSResponse.new(None, init)


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


def _cookie(request, name):
    cookies = SimpleCookie()
    cookies.load(request.headers.get("Cookie", ""))
    value = cookies.get(name)
    return value.value if value else ""


def _session_cookie(token, secure=True, max_age=int(SESSION_TTL.total_seconds())):
    parts = [f"onegram_session={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _oauth_state_cookie(state):
    return f"onegram_oauth_state={state}; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=600"


async def _derive_password(password, salt, iterations=PASSWORD_ITERATIONS):
    password_value = password.encode("utf-8")
    password_bytes = Uint8Array.new(len(password_value))
    salt_bytes = Uint8Array.new(len(salt))
    for index, value in enumerate(password_value):
        password_bytes[index] = value
    for index, value in enumerate(salt):
        salt_bytes[index] = value
    usages = to_js(["deriveBits"])
    key = await crypto.subtle.importKey(
        "raw", password_bytes, "PBKDF2", False, usages
    )
    parameters = to_js(
        {"name": "PBKDF2", "salt": salt_bytes, "iterations": iterations, "hash": "SHA-256"},
        dict_converter=Object.fromEntries,
    )
    buffer = await crypto.subtle.deriveBits(parameters, key, PASSWORD_BYTES * 8)
    digest = Uint8Array.new(buffer)
    return bytes(int(digest[index]) for index in range(PASSWORD_BYTES))


async def _hash_password(password):
    try:
        validate_password(password)
    except ValueError as error:
        raise MessengerError(str(error)) from error
    salt = new_password_salt()
    digest = await _derive_password(password, salt)
    return encode_password_hash(salt, digest)


async def _verify_password(password, encoded):
    parsed = decode_password_hash(encoded)
    if parsed is None or not isinstance(password, str):
        return False
    salt, expected = parsed
    actual = await _derive_password(password, salt)
    return hmac.compare_digest(actual, expected)


async def _create_session(db, user_id):
    token = new_session_token()
    created = datetime.now(timezone.utc)
    await db.prepare(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)"
    ).bind(
        hash_session_token(token),
        user_id,
        created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        (created + SESSION_TTL).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    ).run()
    return token


async def _resolve_session(db, request):
    token = _cookie(request, "onegram_session")
    if not token:
        return None
    row = await db.prepare(
        """SELECT u.id, u.username, u.created_at
           FROM sessions AS s JOIN users AS u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ?"""
    ).bind(hash_session_token(token), _timestamp()).first()
    return _user(row) if row else None


def _required_user(user):
    if not user:
        raise MessengerError("Увійдіть, щоб продовжити.", 401)
    return user


async def _google_request(url, method="GET", form=None):
    options = None
    if form is not None:
        options = to_js({
            "method": "POST",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "body": urlencode(form),
        }, dict_converter=Object.fromEntries)
    response = await js_fetch(url, options) if options else await js_fetch(url)
    if not response.ok:
        raise ValueError("Google OAuth request failed")
    return _python_value(await response.json())


async def _google_identity(request, env, query):
    client_id = getattr(env, "GOOGLE_CLIENT_ID", "")
    client_secret = getattr(env, "GOOGLE_CLIENT_SECRET", "")
    redirect_uri = getattr(env, "GOOGLE_REDIRECT_URI", "")
    if not client_id or not client_secret:
        return _redirect_response("/?authError=google_not_configured")
    state = query.get("state", [""])[0]
    code = query.get("code", [""])[0]
    saved_state = _cookie(request, "onegram_oauth_state")
    if not state or not saved_state or not hmac.compare_digest(state, saved_state) or not code:
        return _redirect_response("/?authError=google")

    origin = str(request.url).split("/", 3)[:3]
    inferred_redirect = "/".join(origin) + "/api/auth/google/callback"
    redirect_uri = str(redirect_uri or inferred_redirect)
    try:
        token_data = await _google_request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            form={
                "code": code,
                "client_id": str(client_id),
                "client_secret": str(client_secret),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("Google did not return an ID token")
        claims = await _google_request(
            "https://oauth2.googleapis.com/tokeninfo?" + urlencode({"id_token": id_token})
        )
        verified = str(claims.get("email_verified", "")).lower() == "true"
        if (
            claims.get("aud") != client_id
            or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
            or int(claims.get("exp", 0)) <= int(datetime.now(timezone.utc).timestamp())
            or not verified
            or not claims.get("sub")
            or not claims.get("email")
        ):
            raise ValueError("Invalid Google identity token")
    except Exception:
        return _redirect_response("/?authError=google")

    user = _python_value(await env.DB.prepare(
        "SELECT id, username, created_at FROM users WHERE google_sub = ?"
    ).bind(claims["sub"]).first())
    if user:
        user = _user(user)
    else:
        base_username = username_from_email(claims["email"])
        username = base_username
        for attempt in range(8):
            exists = await env.DB.prepare(
                "SELECT 1 AS found FROM users WHERE username = ? COLLATE NOCASE"
            ).bind(username).first()
            if not exists:
                break
            username = f"{base_username[:23]}-{uuid.uuid4().hex[:6]}"
        user_id = str(uuid.uuid4())
        created_at = _timestamp()
        row = await env.DB.prepare(
            """INSERT INTO users (id, username, created_at, google_sub)
               VALUES (?, ?, ?, ?) RETURNING id, username, created_at"""
        ).bind(user_id, username, created_at, claims["sub"]).first()
        user = _user(row)

    session_token = await _create_session(env.DB, user["id"])
    return _redirect_response("/", _session_cookie(session_token))


async def _handle_auth(request, env, path, query):
    if path == "/api/auth/session" and request.method == "GET":
        user = await _resolve_session(env.DB, request)
        return _json_response({"user": user})

    if path == "/api/auth/google/config" and request.method == "GET":
        return _json_response({
            "enabled": bool(getattr(env, "GOOGLE_CLIENT_ID", "") and getattr(env, "GOOGLE_CLIENT_SECRET", ""))
        })

    if path == "/api/auth/google" and request.method == "GET":
        client_id = getattr(env, "GOOGLE_CLIENT_ID", "")
        client_secret = getattr(env, "GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return _json_response({"error": "Вхід через Google ще не налаштований."}, 503)
        parsed = urlsplit(str(request.url))
        redirect_uri = getattr(env, "GOOGLE_REDIRECT_URI", "") or (
            f"{parsed.scheme}://{parsed.netloc}/api/auth/google/callback"
        )
        state = uuid.uuid4().hex + uuid.uuid4().hex
        location = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        })
        return _redirect_response(location, _oauth_state_cookie(state))

    if path == "/api/auth/google/callback" and request.method == "GET":
        return await _google_identity(request, env, query)

    if path == "/api/auth/register" and request.method == "POST":
        body = await _read_json(request)
        username = _require_string(body, "username").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", username):
            raise MessengerError(
                "Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення."
            )
        if len(username) > MAX_USERNAME_LENGTH:
            raise MessengerError(f"Ім’я не може бути довшим за {MAX_USERNAME_LENGTH} символів.")
        password_hash = await _hash_password(_require_string(body, "password"))
        user_id = str(uuid.uuid4())
        created_at = _timestamp()
        row = await env.DB.prepare(
            """INSERT INTO users (id, username, created_at, password_hash)
               VALUES (?, ?, ?, ?) ON CONFLICT(username) DO NOTHING
               RETURNING id, username, created_at"""
        ).bind(user_id, username.strip(), created_at, password_hash).first()
        if not row:
            raise MessengerError("Це ім’я вже зайняте.", 409)
        user = _user(row)
        token = await _create_session(env.DB, user["id"])
        return _json_response(
            {"user": user},
            201,
            {"Set-Cookie": _session_cookie(token)},
        )

    if path == "/api/auth/login" and request.method == "POST":
        body = await _read_json(request)
        username = _require_string(body, "username").strip()
        password = _require_string(body, "password")
        row = _python_value(await env.DB.prepare(
            "SELECT id, username, created_at, password_hash FROM users WHERE username = ? COLLATE NOCASE"
        ).bind(username).first())
        if not row or not row.get("password_hash") or not await _verify_password(password, row["password_hash"]):
            raise MessengerError("Неправильний нік або пароль.", 401)
        user = _user(row)
        token = await _create_session(env.DB, user["id"])
        return _json_response({"user": user}, 200, {"Set-Cookie": _session_cookie(token)})

    if path == "/api/auth/logout" and request.method == "POST":
        token = _cookie(request, "onegram_session")
        if token:
            await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(
                hash_session_token(token)
            ).run()
        return _json_response(
            {"ok": True},
            200,
            {"Set-Cookie": "onegram_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"},
        )

    return _json_response({"error": "Маршрут не знайдено."}, 404)


async def _get_user(db, user_id):
    row = await db.prepare(
        "SELECT id, username, created_at FROM users WHERE id = ?"
    ).bind(user_id).first()
    return _user(row) if row else None


async def _handle_api(request, env, path, query):
    if path.startswith("/api/auth/"):
        return await _handle_auth(request, env, path, query)

    db = env.DB
    current_user = _required_user(await _resolve_session(db, request))

    if path == "/api/users" and request.method == "GET":
        result = await db.prepare(
            "SELECT id, username, created_at FROM users ORDER BY username COLLATE NOCASE"
        ).all()
        return _json_response({"users": [_user(row) for row in _rows(result)]})

    if path == "/api/users" and request.method == "POST":
        return _json_response({"error": "Маршрут не знайдено."}, 404)

    if path == "/api/conversations" and request.method == "GET":
        user_id = query.get("userId", [""])[0]
        if user_id != current_user["id"]:
            raise MessengerError("Немає доступу до цього профілю.", 403)
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
        if current_user["id"] not in participant_ids:
            raise MessengerError("Поточний користувач має бути учасником розмови.", 403)
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
            if user_id != current_user["id"]:
                raise MessengerError("Немає доступу до цього профілю.", 403)
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
            if sender_id != current_user["id"]:
                raise MessengerError("Немає доступу до цього профілю.", 403)
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
        if user_id != current_user["id"]:
            raise MessengerError("Немає доступу до цього профілю.", 403)
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
        except Exception as error:
            console.error("Onegram Worker request failed", path, type(error).__name__)
            return _secure_response(_json_response({"error": "Сталася внутрішня помилка сервера."}, 500))