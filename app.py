import json
import os
import re
import tempfile
import threading
import uuid
import hmac
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request as UrlRequest, urlopen

from auth import hash_password, hash_session_token, new_session_token, username_from_email, verify_password


MAX_REQUEST_BYTES = 16 * 1024
MAX_USERNAME_LENGTH = 30
MAX_TITLE_LENGTH = 80
MAX_MESSAGE_LENGTH = 4000
SESSION_TTL = timedelta(days=30)
PUBLIC_DIRECTORY = Path(__file__).parent / "public"
STATIC_FILES = {
    "/": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}


class MessengerError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Messenger:
    def __init__(self, file_path):
        self.file_path = Path(file_path)
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        try:
            with self.file_path.open(encoding="utf-8") as data_file:
                data = json.load(data_file)
        except FileNotFoundError:
            return {"users": [], "conversations": [], "messages": [], "sessions": []}
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Could not parse messenger data at {self.file_path}.") from error

        if not self._valid_data(data):
            raise RuntimeError(f"Messenger data at {self.file_path} has an invalid format.")
        data.setdefault("sessions", [])
        return data

    @staticmethod
    def _valid_data(data):
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), list) for key in ("users", "conversations", "messages")
        ):
            return False
        if "sessions" in data and not isinstance(data["sessions"], list):
            return False

        def valid_timestamp(value):
            if not isinstance(value, str):
                return False
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return True
            except ValueError:
                return False

        users, conversations, messages = data["users"], data["conversations"], data["messages"]
        if not all(
            isinstance(user, dict)
            and isinstance(user.get("id"), str) and user["id"]
            and isinstance(user.get("username"), str) and user["username"]
            and valid_timestamp(user.get("createdAt"))
            for user in users
        ):
            return False
        user_ids = [user["id"] for user in users]
        if len(set(user_ids)) != len(user_ids) or len(
            {user["username"].casefold() for user in users}
        ) != len(users):
            return False

        if not all(
            isinstance(conversation, dict)
            and isinstance(conversation.get("id"), str) and conversation["id"]
            and isinstance(conversation.get("title"), str) and conversation["title"].strip()
            and isinstance(conversation.get("participantIds"), list)
            and all(isinstance(item, str) and item for item in conversation["participantIds"])
            and len(conversation["participantIds"]) >= 2
            and len(set(conversation["participantIds"])) == len(conversation["participantIds"])
            and all(item in user_ids for item in conversation["participantIds"])
            and valid_timestamp(conversation.get("createdAt"))
            for conversation in conversations
        ):
            return False
        conversation_ids = [item["id"] for item in conversations]
        if len(set(conversation_ids)) != len(conversation_ids):
            return False

        if not all(
            isinstance(message, dict)
            and isinstance(message.get("id"), str) and message["id"]
            and isinstance(message.get("conversationId"), str)
            and isinstance(message.get("senderId"), str)
            and isinstance(message.get("text"), str) and message["text"].strip()
            and valid_timestamp(message.get("createdAt"))
            and any(
                conversation["id"] == message["conversationId"]
                and message["senderId"] in conversation["participantIds"]
                for conversation in conversations
            )
            for message in messages
        ):
            return False
        message_ids = [item["id"] for item in messages]
        return len(set(message_ids)) == len(message_ids)

    def _commit(self, data):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.file_path.parent,
                prefix=f"{self.file_path.name}.", suffix=".tmp", delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(data, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
            os.replace(temporary_path, self.file_path)
        except OSError:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            raise
        self.data = data

    def list_users(self):
        with self.lock:
            return sorted(self.data["users"], key=lambda user: user["username"].casefold())

    def get_user(self, user_id):
        return next((user for user in self.data["users"] if user["id"] == user_id), None)

    def create_user(self, username, password_hash=None, google_sub=None):
        normalized = username.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", normalized):
            raise MessengerError("Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення.")
        if len(normalized) > MAX_USERNAME_LENGTH:
            raise MessengerError(f"Ім’я не може бути довшим за {MAX_USERNAME_LENGTH} символів.")
        with self.lock:
            if any(user["username"].casefold() == normalized.casefold() for user in self.data["users"]):
                raise MessengerError("Це ім’я вже зайняте.", 409)
            if google_sub and self.get_user_by_google_sub(google_sub):
                raise MessengerError("Цей Google-акаунт уже зареєстрований.", 409)
            user = {
                "id": str(uuid.uuid4()),
                "username": normalized,
                "createdAt": timestamp(),
                "passwordHash": password_hash,
                "googleSub": google_sub,
            }
            self._commit({**self.data, "users": [*self.data["users"], user]})
            return user

    def find_user_by_username(self, username):
        normalized = username.strip().casefold()
        return next(
            (user for user in self.data["users"] if user["username"].casefold() == normalized),
            None,
        )

    def get_user_by_google_sub(self, google_sub):
        return next(
            (user for user in self.data["users"] if user.get("googleSub") == google_sub),
            None,
        )

    @staticmethod
    def public_user(user):
        return {key: user[key] for key in ("id", "username", "createdAt")}

    def login(self, username, password):
        user = self.find_user_by_username(username)
        if not user or not user.get("passwordHash") or not verify_password(password, user["passwordHash"]):
            raise MessengerError("Неправильний нік або пароль.", 401)
        return user

    def create_session(self, user_id):
        if not self.get_user(user_id):
            raise MessengerError("Користувача не знайдено.", 404)
        token = new_session_token()
        created_at = datetime.now(timezone.utc)
        session = {
            "tokenHash": hash_session_token(token),
            "userId": user_id,
            "createdAt": timestamp(created_at),
            "expiresAt": timestamp(created_at + SESSION_TTL),
        }
        with self.lock:
            next_sessions = [
                item for item in self.data.get("sessions", [])
                if item["expiresAt"] > timestamp(created_at)
            ]
            self._commit({**self.data, "sessions": [*next_sessions, session]})
        return token

    def resolve_session(self, token):
        if not token:
            return None
        now = timestamp()
        token_hash = hash_session_token(token)
        with self.lock:
            session = next(
                (item for item in self.data.get("sessions", []) if item["tokenHash"] == token_hash),
                None,
            )
            if not session or session["expiresAt"] <= now:
                return None
            return self.get_user(session["userId"])

    def delete_session(self, token):
        if not token:
            return
        token_hash = hash_session_token(token)
        with self.lock:
            sessions = [
                item for item in self.data.get("sessions", [])
                if item["tokenHash"] != token_hash
            ]
            self._commit({**self.data, "sessions": sessions})

    def list_conversations(self, user_id):
        with self.lock:
            return sorted(
                (item for item in self.data["conversations"] if user_id in item["participantIds"]),
                key=lambda item: item["title"].casefold(),
            )

    def get_conversation(self, conversation_id):
        return next((item for item in self.data["conversations"] if item["id"] == conversation_id), None)

    def create_conversation(self, title, participant_ids):
        normalized_title = title.strip()
        if not normalized_title:
            raise MessengerError("Назва розмови не може бути порожньою.")
        if len(normalized_title) > MAX_TITLE_LENGTH:
            raise MessengerError(f"Назва не може бути довшою за {MAX_TITLE_LENGTH} символів.")
        unique_ids = list(dict.fromkeys(participant_ids))
        if len(unique_ids) < 2:
            raise MessengerError("У розмові мають бути щонайменше двоє учасників.")
        with self.lock:
            if any(not self.get_user(user_id) for user_id in unique_ids):
                raise MessengerError("Усі учасники мають бути зареєстрованими користувачами.")
            conversation = {
                "id": str(uuid.uuid4()),
                "title": normalized_title,
                "participantIds": unique_ids,
                "createdAt": timestamp(),
            }
            self._commit({**self.data, "conversations": [*self.data["conversations"], conversation]})
            return conversation

    def get_messages(self, conversation_id):
        with self.lock:
            return sorted(
                (item for item in self.data["messages"] if item["conversationId"] == conversation_id),
                key=lambda item: item["createdAt"],
            )

    def send_message(self, conversation_id, sender_id, text):
        with self.lock:
            conversation = self.get_conversation(conversation_id)
            if not conversation:
                raise MessengerError("Розмову не знайдено.", 404)
            if sender_id not in conversation["participantIds"]:
                raise MessengerError("Повідомлення можуть надсилати лише учасники розмови.")
            if not text.strip():
                raise MessengerError("Повідомлення не може бути порожнім.")
            if len(text) > MAX_MESSAGE_LENGTH:
                raise MessengerError(f"Повідомлення не може бути довшим за {MAX_MESSAGE_LENGTH} символів.")
            message = {
                "id": str(uuid.uuid4()),
                "conversationId": conversation_id,
                "senderId": sender_id,
                "text": text,
                "createdAt": timestamp(),
            }
            self._commit({**self.data, "messages": [*self.data["messages"], message]})
            return message

    def search_messages(self, user_id, query):
        normalized = query.strip().casefold()
        if not normalized:
            raise MessengerError("Введіть текст для пошуку.")
        visible_ids = {item["id"] for item in self.list_conversations(user_id)}
        with self.lock:
            return sorted(
                (
                    message for message in self.data["messages"]
                    if message["conversationId"] in visible_ids
                    and normalized in message["text"].casefold()
                ),
                key=lambda item: item["createdAt"],
            )


class MessengerHandler(BaseHTTPRequestHandler):
    server_version = "OnegramMessenger/1.0"
    messenger = None
    public_directory = PUBLIC_DIRECTORY

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _json(self, status, payload, headers=None):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location, set_cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for cookie in set_cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _cookie_header(self, name, value, max_age, http_only=True):
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0]
        attributes = [f"{name}={value}", "Path=/", "SameSite=Lax", f"Max-Age={max_age}"]
        if http_only:
            attributes.append("HttpOnly")
        if forwarded_proto == "https":
            attributes.append("Secure")
        return "; ".join(attributes)

    def _session_token(self):
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        cookie = cookies.get("onegram_session")
        return cookie.value if cookie else ""

    def _current_user(self):
        user = self.messenger.resolve_session(self._session_token())
        if not user:
            raise MessengerError("Увійдіть, щоб продовжити.", 401)
        return user

    def _google_settings(self):
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "")
        if not redirect_uri:
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0]
            redirect_uri = f"{forwarded_proto}://{self.headers.get('Host', 'localhost:3000')}/api/auth/google/callback"
        return client_id, client_secret, redirect_uri

    def _start_google_login(self):
        client_id, _, redirect_uri = self._google_settings()
        if not client_id or not os.environ.get("GOOGLE_CLIENT_SECRET"):
            raise MessengerError("Вхід через Google ще не налаштований.", 503)
        state = uuid.uuid4().hex + uuid.uuid4().hex
        location = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        })
        self._redirect(
            location,
            (self._cookie_header("onegram_oauth_state", state, 600),),
        )

    def _finish_google_login(self, query):
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        state_cookie = cookies.get("onegram_oauth_state")
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        clear_state = self._cookie_header("onegram_oauth_state", "", 0)
        if not state_cookie or not state or not hmac.compare_digest(state_cookie.value, state) or not code:
            self._redirect("/?authError=google", (clear_state,))
            return

        client_id, client_secret, redirect_uri = self._google_settings()
        try:
            token_request = UrlRequest(
                "https://oauth2.googleapis.com/token",
                data=urlencode({
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                }).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(token_request, timeout=10) as response:
                token_data = json.loads(response.read())
            id_token = token_data.get("id_token")
            if not id_token:
                raise ValueError("Google did not return an ID token")
            verify_url = "https://oauth2.googleapis.com/tokeninfo?" + urlencode({"id_token": id_token})
            with urlopen(verify_url, timeout=10) as response:
                claims = json.loads(response.read())
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
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            self._redirect("/?authError=google", (clear_state,))
            return

        user = self.messenger.get_user_by_google_sub(claims["sub"])
        if not user:
            username = username_from_email(claims["email"])
            try:
                user = self.messenger.create_user(username, google_sub=claims["sub"])
            except MessengerError as error:
                if error.status_code != 409:
                    raise
                suffix = uuid.uuid4().hex[:6]
                user = self.messenger.create_user(f"{username[:23]}-{suffix}", google_sub=claims["sub"])
        token = self.messenger.create_session(user["id"])
        self._redirect(
            "/",
            (
                self._cookie_header("onegram_session", token, int(SESSION_TTL.total_seconds())),
                clear_state,
            ),
        )

    def _body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise MessengerError("Некоректний формат запиту.") from error
        if size < 0 or size > MAX_REQUEST_BYTES:
            raise MessengerError("Запит завеликий.")
        if size == 0:
            raise json.JSONDecodeError("Empty request body", "", 0)
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise json.JSONDecodeError("Invalid JSON", "", 0) from error

    @staticmethod
    def _string(body, key):
        value = body.get(key) if isinstance(body, dict) else None
        if not isinstance(value, str):
            raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
        return value

    def _static(self, filename):
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }
        contents = (self.public_directory / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types[Path(filename).suffix])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(contents)))
        self.end_headers()
        self.wfile.write(contents)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        try:
            if path in STATIC_FILES:
                self._static(STATIC_FILES[path])
            elif path == "/api/auth/session":
                user = self.messenger.resolve_session(self._session_token())
                self._json(200, {"user": self.messenger.public_user(user) if user else None})
            elif path == "/api/auth/google/config":
                client_id, client_secret, _ = self._google_settings()
                self._json(200, {"enabled": bool(client_id and client_secret)})
            elif path == "/api/auth/google":
                self._start_google_login()
            elif path == "/api/auth/google/callback":
                self._finish_google_login(query)
            elif path == "/api/users":
                self._current_user()
                self._json(200, {"users": [self.messenger.public_user(user) for user in self.messenger.list_users()]})
            elif path == "/api/conversations":
                current_user = self._current_user()
                user_id = query.get("userId", [""])[0]
                if user_id != current_user["id"]:
                    raise MessengerError("Немає доступу до цього профілю.", 403)
                self._json(200, {"conversations": self.messenger.list_conversations(user_id)})
            elif path == "/api/search":
                current_user = self._current_user()
                user_id = query.get("userId", [""])[0]
                if user_id != current_user["id"]:
                    raise MessengerError("Немає доступу до цього профілю.", 403)
                self._json(200, {"messages": self.messenger.search_messages(user_id, query.get("q", [""])[0])})
            else:
                match = re.fullmatch(r"/api/conversations/([^/]+)/messages", path)
                if match:
                    current_user = self._current_user()
                    conversation_id = match.group(1)
                    user_id = query.get("userId", [""])[0]
                    conversation = self.messenger.get_conversation(conversation_id)
                    if user_id != current_user["id"] or not conversation or user_id not in conversation["participantIds"]:
                        raise MessengerError("Розмову не знайдено.", 404)
                    self._json(200, {"messages": self.messenger.get_messages(conversation_id)})
                elif path.startswith("/api/"):
                    self._json(404, {"error": "Маршрут не знайдено."})
                else:
                    self._json(404, {"error": "Сторінку не знайдено."})
        except MessengerError as error:
            self._json(error.status_code, {"error": str(error)})
        except (OSError, RuntimeError):
            self._json(500, {"error": "Сталася внутрішня помилка сервера."})

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        message_route = re.fullmatch(r"/api/conversations/([^/]+)/messages", path)
        if path == "/api/auth/logout":
            token = self._session_token()
            self.messenger.delete_session(token)
            self._json(
                200,
                {"ok": True},
                {"Set-Cookie": self._cookie_header("onegram_session", "", 0)},
            )
            return
        if path not in ("/api/auth/register", "/api/auth/login", "/api/conversations") and not message_route:
            self._json(404, {"error": "Маршрут не знайдено."})
            return
        try:
            body = self._body()
            if path == "/api/auth/register":
                username = self._string(body, "username")
                password = self._string(body, "password")
                try:
                    password_hash = hash_password(password)
                except ValueError as error:
                    raise MessengerError(str(error)) from error
                user = self.messenger.create_user(username, password_hash=password_hash)
                token = self.messenger.create_session(user["id"])
                self._json(
                    201,
                    {"user": self.messenger.public_user(user)},
                    {"Set-Cookie": self._cookie_header("onegram_session", token, int(SESSION_TTL.total_seconds()))},
                )
            elif path == "/api/auth/login":
                user = self.messenger.login(
                    self._string(body, "username"),
                    self._string(body, "password"),
                )
                token = self.messenger.create_session(user["id"])
                self._json(
                    200,
                    {"user": self.messenger.public_user(user)},
                    {"Set-Cookie": self._cookie_header("onegram_session", token, int(SESSION_TTL.total_seconds()))},
                )
            elif path == "/api/conversations":
                current_user = self._current_user()
                if not isinstance(body, dict) or not isinstance(body.get("participantIds"), list):
                    raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
                if not all(isinstance(item, str) for item in body["participantIds"]):
                    raise MessengerError("Перевірте список учасників.")
                if current_user["id"] not in body["participantIds"]:
                    raise MessengerError("Поточний користувач має бути учасником розмови.", 403)
                conversation = self.messenger.create_conversation(
                    self._string(body, "title"), body["participantIds"]
                )
                self._json(201, {"conversation": conversation})
            else:
                current_user = self._current_user()
                if not isinstance(body, dict):
                    raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
                sender_id = self._string(body, "senderId")
                if sender_id != current_user["id"]:
                    raise MessengerError("Немає доступу до цього профілю.", 403)
                message = self.messenger.send_message(
                    message_route.group(1), sender_id, self._string(body, "text")
                )
                self._json(201, {"message": message})
        except json.JSONDecodeError:
            self._json(400, {"error": "Некоректний формат JSON."})
        except MessengerError as error:
            self._json(error.status_code, {"error": str(error)})
        except (OSError, RuntimeError):
            self._json(500, {"error": "Сталася внутрішня помилка сервера."})

    def log_message(self, _format, *_args):
        return


def main():
    data_path = Path(os.environ.get("DATA_FILE_PATH", Path(__file__).parent / "data" / "messages.json"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "3000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be a valid port number.")

    handler = type("ConfiguredMessengerHandler", (MessengerHandler,), {
        "messenger": Messenger(data_path),
        "public_directory": PUBLIC_DIRECTORY,
    })
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"onegram працює за адресою http://{host}:{port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nСервер зупинено.")


if __name__ == "__main__":
    main()