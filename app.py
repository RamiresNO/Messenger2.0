import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


MAX_REQUEST_BYTES = 16 * 1024
MAX_USERNAME_LENGTH = 30
MAX_TITLE_LENGTH = 80
MAX_MESSAGE_LENGTH = 4000
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


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
            return {"users": [], "conversations": [], "messages": []}
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Could not parse messenger data at {self.file_path}.") from error

        if not self._valid_data(data):
            raise RuntimeError(f"Messenger data at {self.file_path} has an invalid format.")
        return data

    @staticmethod
    def _valid_data(data):
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), list) for key in ("users", "conversations", "messages")
        ):
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

    def create_user(self, username):
        normalized = username.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", normalized):
            raise MessengerError("Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення.")
        if len(normalized) > MAX_USERNAME_LENGTH:
            raise MessengerError(f"Ім’я не може бути довшим за {MAX_USERNAME_LENGTH} символів.")
        with self.lock:
            if any(user["username"].casefold() == normalized.casefold() for user in self.data["users"]):
                raise MessengerError("Це ім’я вже зайняте.", 409)
            user = {"id": str(uuid.uuid4()), "username": normalized, "createdAt": timestamp()}
            self._commit({**self.data, "users": [*self.data["users"], user]})
            return user

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
    server_version = "KoloMessenger/1.0"
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

    def _json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
            elif path == "/api/users":
                self._json(200, {"users": self.messenger.list_users()})
            elif path == "/api/conversations":
                user_id = query.get("userId", [""])[0]
                if not user_id or not self.messenger.get_user(user_id):
                    raise MessengerError("Користувача не знайдено.", 404)
                self._json(200, {"conversations": self.messenger.list_conversations(user_id)})
            elif path == "/api/search":
                user_id = query.get("userId", [""])[0]
                if not user_id or not self.messenger.get_user(user_id):
                    raise MessengerError("Користувача не знайдено.", 404)
                self._json(200, {"messages": self.messenger.search_messages(user_id, query.get("q", [""])[0])})
            else:
                match = re.fullmatch(r"/api/conversations/([^/]+)/messages", path)
                if match:
                    conversation_id = match.group(1)
                    user_id = query.get("userId", [""])[0]
                    conversation = self.messenger.get_conversation(conversation_id)
                    if not conversation or user_id not in conversation["participantIds"]:
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
        if path not in ("/api/users", "/api/conversations") and not message_route:
            self._json(404, {"error": "Маршрут не знайдено."})
            return
        try:
            body = self._body()
            if path == "/api/users":
                user = self.messenger.create_user(self._string(body, "username"))
                self._json(201, {"user": user})
            elif path == "/api/conversations":
                if not isinstance(body, dict) or not isinstance(body.get("participantIds"), list):
                    raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
                if not all(isinstance(item, str) for item in body["participantIds"]):
                    raise MessengerError("Перевірте список учасників.")
                conversation = self.messenger.create_conversation(
                    self._string(body, "title"), body["participantIds"]
                )
                self._json(201, {"conversation": conversation})
            else:
                if not isinstance(body, dict):
                    raise MessengerError("Перевірте заповнені поля та спробуйте ще раз.")
                message = self.messenger.send_message(
                    message_route.group(1), self._string(body, "senderId"), self._string(body, "text")
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
        print(f"Коло працює за адресою http://{host}:{port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nСервер зупинено.")


if __name__ == "__main__":
    main()