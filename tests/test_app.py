import json
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auth import hash_password
from app import Messenger, MessengerHandler, PUBLIC_DIRECTORY


class MessengerApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        messenger = Messenger(Path(self.directory.name) / "messages.json")
        handler = type("TestMessengerHandler", (MessengerHandler,), {"messenger": messenger})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, body=None, cookie=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        active_cookie = self.cookie if cookie is None else cookie
        if active_cookie:
            headers["Cookie"] = active_cookie
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
        )
        try:
            with urlopen(request) as response:
                self.last_headers = response.headers
                self._remember_cookie(response.headers.get("Set-Cookie"))
                return response.status, json.loads(response.read())
        except HTTPError as error:
            self.last_headers = error.headers
            self._remember_cookie(error.headers.get("Set-Cookie"))
            response_body = error.read()
            error.close()
            return error.code, json.loads(response_body)

    def _remember_cookie(self, header):
        if header:
            cookies = SimpleCookie()
            cookies.load(header)
            if "onegram_session" in cookies:
                self.cookie = f"onegram_session={cookies['onegram_session'].value}"

    def test_ui_and_static_assets_are_served(self):
        with urlopen(self.base_url) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("onegram — простір для розмов", page)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with urlopen(self.base_url + "/app.js") as response:
            self.assertIn("javascript", response.headers["Content-Type"])

    def test_accounts_group_chat_search_and_access_control(self):
        _, alice_response = self.request("/api/auth/register", {"username": "alice", "password": "alice-password-9"})
        alice_cookie = self.cookie
        _, bob_response = self.request("/api/auth/register", {"username": "bob", "password": "bob-password-9"}, cookie="")
        bob_cookie = self.cookie
        _, outsider_response = self.request("/api/auth/register", {"username": "outsider", "password": "outsider-password-9"}, cookie="")
        outsider_cookie = self.cookie
        alice, bob, outsider = (
            alice_response["user"], bob_response["user"], outsider_response["user"]
        )
        status, conversation_response = self.request("/api/conversations", {
            "title": "Підготовка до іспиту",
            "participantIds": [alice["id"], bob["id"]],
        }, cookie=alice_cookie)
        self.assertEqual(status, 201)
        conversation = conversation_response["conversation"]
        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages",
            {"senderId": alice["id"], "text": "Повторімо біологію"},
            cookie=alice_cookie,
        )
        self.assertEqual(status, 201)
        search_query = urlencode({"userId": alice["id"], "q": "БІОЛОГІЮ"})
        status, result = self.request(f"/api/search?{search_query}", cookie=alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["messages"]), 1)

        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages?userId={outsider['id']}"
            , cookie=outsider_cookie
        )
        self.assertEqual(status, 404)
        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages",
            {"senderId": outsider["id"], "text": "Не маю доступу"},
            cookie=outsider_cookie,
        )
        self.assertEqual(status, 400)

    def test_validation_and_persistence(self):
        status, _ = self.request("/api/auth/register", {"username": "bad name", "password": "password-123"})
        self.assertEqual(status, 400)
        _, alice_response = self.request("/api/auth/register", {"username": "alice", "password": "alice-password-9"})
        alice_cookie = self.cookie
        _, bob_response = self.request("/api/auth/register", {"username": "bob", "password": "bob-password-9"}, cookie="")
        alice, bob = alice_response["user"], bob_response["user"]
        status, _ = self.request("/api/conversations", {
            "title": "Solo", "participantIds": [alice["id"]],
        }, cookie=alice_cookie)
        self.assertEqual(status, 400)
        status, result = self.request("/api/conversations", {
            "title": "Study group", "participantIds": [alice["id"], bob["id"]],
        }, cookie=alice_cookie)
        self.assertEqual(status, 201)
        conversation = result["conversation"]
        self.request(f"/api/conversations/{conversation['id']}/messages", {
            "senderId": alice["id"], "text": "Saved message",
        }, cookie=alice_cookie)

        reloaded = Messenger(Path(self.directory.name) / "messages.json")
        self.assertEqual(reloaded.get_messages(conversation["id"])[0]["text"], "Saved message")

    def test_password_login_and_session_persist_without_plaintext(self):
        file_path = Path(self.directory.name) / "auth.json"
        messenger = Messenger(file_path)
        password = "Correct Horse Battery 9!"
        user = messenger.create_user("secure_user", hash_password(password))

        self.assertNotIn(password, file_path.read_text(encoding="utf-8"))
        self.assertEqual(messenger.login("SECURE_USER", password)["id"], user["id"])
        with self.assertRaisesRegex(Exception, "Неправильний нік або пароль"):
            messenger.login("secure_user", "wrong password")

        token = messenger.create_session(user["id"])
        reloaded = Messenger(file_path)
        self.assertEqual(reloaded.resolve_session(token)["id"], user["id"])
        reloaded.delete_session(token)
        self.assertIsNone(reloaded.resolve_session(token))

    def test_http_registration_login_logout_and_private_api(self):
        status, _ = self.request("/api/users")
        self.assertEqual(status, 401)

        status, registration = self.request("/api/auth/register", {
            "username": "session_user",
            "password": "secure-password-42",
        })
        self.assertEqual(status, 201)
        self.assertIn("HttpOnly", self.last_headers.get("Set-Cookie", ""))
        self.assertEqual(self.request("/api/auth/session")[1]["user"]["id"], registration["user"]["id"])

        status, _ = self.request("/api/auth/login", {
            "username": "session_user",
            "password": "wrong-password",
        }, cookie="")
        self.assertEqual(status, 401)
        status, login = self.request("/api/auth/login", {
            "username": "SESSION_USER",
            "password": "secure-password-42",
        }, cookie="")
        self.assertEqual(status, 200)
        self.assertEqual(login["user"]["id"], registration["user"]["id"])

        status, _ = self.request("/api/auth/logout", {}, cookie=self.cookie)
        self.assertEqual(status, 200)
        status, session = self.request("/api/auth/session", cookie="")
        self.assertEqual(status, 200)
        self.assertIsNone(session["user"])
        status, _ = self.request("/api/conversations", cookie="")
        self.assertEqual(status, 401)

    def test_malformed_json_returns_bad_request(self):
        request = Request(
            self.base_url + "/api/auth/register", data=b"{", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        response_body = error.exception.read()
        error.exception.close()
        self.assertEqual(json.loads(response_body)["error"], "Некоректний формат JSON.")


if __name__ == "__main__":
    unittest.main()