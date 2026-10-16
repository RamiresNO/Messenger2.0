import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            response_body = error.read()
            error.close()
            return error.code, json.loads(response_body)

    def test_ui_and_static_assets_are_served(self):
        with urlopen(self.base_url) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Коло — простір для розмов", page)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with urlopen(self.base_url + "/app.js") as response:
            self.assertIn("javascript", response.headers["Content-Type"])

    def test_accounts_group_chat_search_and_access_control(self):
        _, alice_response = self.request("/api/users", {"username": "alice"})
        _, bob_response = self.request("/api/users", {"username": "bob"})
        _, outsider_response = self.request("/api/users", {"username": "outsider"})
        alice, bob, outsider = (
            alice_response["user"], bob_response["user"], outsider_response["user"]
        )
        status, conversation_response = self.request("/api/conversations", {
            "title": "Підготовка до іспиту",
            "participantIds": [alice["id"], bob["id"]],
        })
        self.assertEqual(status, 201)
        conversation = conversation_response["conversation"]
        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages",
            {"senderId": alice["id"], "text": "Повторімо біологію"},
        )
        self.assertEqual(status, 201)
        search_query = urlencode({"userId": alice["id"], "q": "БІОЛОГІЮ"})
        status, result = self.request(f"/api/search?{search_query}")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["messages"]), 1)

        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages?userId={outsider['id']}"
        )
        self.assertEqual(status, 404)
        status, _ = self.request(
            f"/api/conversations/{conversation['id']}/messages",
            {"senderId": outsider["id"], "text": "Не маю доступу"},
        )
        self.assertEqual(status, 400)

    def test_validation_and_persistence(self):
        status, _ = self.request("/api/users", {"username": "bad name"})
        self.assertEqual(status, 400)
        _, alice_response = self.request("/api/users", {"username": "alice"})
        _, bob_response = self.request("/api/users", {"username": "bob"})
        alice, bob = alice_response["user"], bob_response["user"]
        status, _ = self.request("/api/conversations", {
            "title": "Solo", "participantIds": [alice["id"]],
        })
        self.assertEqual(status, 400)
        status, result = self.request("/api/conversations", {
            "title": "Study group", "participantIds": [alice["id"], bob["id"]],
        })
        self.assertEqual(status, 201)
        conversation = result["conversation"]
        self.request(f"/api/conversations/{conversation['id']}/messages", {
            "senderId": alice["id"], "text": "Saved message",
        })

        reloaded = Messenger(Path(self.directory.name) / "messages.json")
        self.assertEqual(reloaded.get_messages(conversation["id"])[0]["text"], "Saved message")

    def test_malformed_json_returns_bad_request(self):
        request = Request(
            self.base_url + "/api/users", data=b"{", method="POST",
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