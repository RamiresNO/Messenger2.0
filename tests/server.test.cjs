const assert = require("node:assert/strict");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createMessengerServer } = require("../dist/server");
const { MessengerService } = require("../dist/services/MessengerService");
const { JsonRepository } = require("../dist/storage/JsonRepository");

async function withServer(run) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "study-hall-http-"));
  const messenger = await MessengerService.open(
    new JsonRepository(path.join(directory, "messages.json")),
  );
  const server = createMessengerServer(
    messenger,
    path.join(process.cwd(), "public"),
  );
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Test server did not bind to a TCP port.");
  }

  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    await rm(directory, { recursive: true, force: true });
  }
}

async function postJson(baseUrl, route, body) {
  return fetch(`${baseUrl}${route}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

test("serves the web UI and its static assets", async () => {
  await withServer(async (baseUrl) => {
    const page = await fetch(baseUrl);
    assert.equal(page.status, 200);
    assert.match(await page.text(), /Коло — простір для розмов/);
    assert.equal(page.headers.get("x-content-type-options"), "nosniff");

    const script = await fetch(`${baseUrl}/app.js`);
    assert.equal(script.status, 200);
    assert.match(script.headers.get("content-type"), /javascript/);
  });
});

test("supports account creation, group chat, sending, search, and access checks", async () => {
  await withServer(async (baseUrl) => {
    const aliceResponse = await postJson(baseUrl, "/api/users", {
      username: "alice",
    });
    const bobResponse = await postJson(baseUrl, "/api/users", {
      username: "bob",
    });
    const outsiderResponse = await postJson(baseUrl, "/api/users", {
      username: "outsider",
    });
    assert.equal(aliceResponse.status, 201);
    assert.equal(bobResponse.status, 201);
    assert.equal(outsiderResponse.status, 201);
    const { user: alice } = await aliceResponse.json();
    const { user: bob } = await bobResponse.json();
    const { user: outsider } = await outsiderResponse.json();

    const conversationResponse = await postJson(
      baseUrl,
      "/api/conversations",
      {
        title: "Підготовка до іспиту",
        participantIds: [alice.id, bob.id],
      },
    );
    assert.equal(conversationResponse.status, 201);
    const { conversation } = await conversationResponse.json();

    const messageResponse = await postJson(
      baseUrl,
      `/api/conversations/${conversation.id}/messages`,
      { senderId: alice.id, text: "Повторімо біологію" },
    );
    assert.equal(messageResponse.status, 201);

    const search = await fetch(
      `${baseUrl}/api/search?userId=${alice.id}&q=${encodeURIComponent("БІОЛОГІЮ")}`,
    );
    assert.equal(search.status, 200);
    assert.equal((await search.json()).messages.length, 1);

    const denied = await fetch(
      `${baseUrl}/api/conversations/${conversation.id}/messages?userId=${outsider.id}`,
    );
    assert.equal(denied.status, 404);

    const invalidMessage = await postJson(
      baseUrl,
      `/api/conversations/${conversation.id}/messages`,
      { senderId: outsider.id, text: "Не маю доступу" },
    );
    assert.equal(invalidMessage.status, 400);

    const malformed = await fetch(`${baseUrl}/api/users`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{",
    });
    assert.equal(malformed.status, 400);
  });
});
