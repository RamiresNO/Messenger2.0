const assert = require("node:assert/strict");
const { mkdtemp, readFile, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { MessengerService } = require("../dist/services/MessengerService");
const { JsonRepository } = require("../dist/storage/JsonRepository");

async function withService(run) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "study-hall-"));
  try {
    const filePath = path.join(directory, "data", "messages.json");
    const repository = new JsonRepository(filePath);
    const service = await MessengerService.open(repository);
    await run({ directory, filePath, repository, service });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("persists users, group conversations, and ordered messages across reloads", async () => {
  await withService(async ({ repository, service }) => {
    const alice = await service.createUser("alice");
    const bob = await service.createUser("bob");
    const chris = await service.createUser("chris");
    const conversation = await service.createConversation("Exam prep", [
      alice.id,
      bob.id,
      chris.id,
    ]);

    await service.sendMessage(conversation.id, alice.id, "Bring your notes");
    await service.sendMessage(conversation.id, bob.id, "I will bring mine");

    const reloaded = await MessengerService.open(repository);
    assert.deepEqual(
      reloaded.getMessages(conversation.id).map((message) => message.text),
      ["Bring your notes", "I will bring mine"],
    );
    assert.equal(reloaded.getParticipants(conversation).length, 3);
  });
});

test("validates users, conversations, and messages", async () => {
  await withService(async ({ service }) => {
    const alice = await service.createUser("alice");
    const bob = await service.createUser("bob");

    await assert.rejects(service.createUser("ALICE"), /вже зайняте/);
    await assert.rejects(service.createUser("not a username"), /латинські літери/);
    await assert.rejects(
      service.createConversation("Solo", [alice.id]),
      /щонайменше двоє учасників/,
    );

    const conversation = await service.createConversation("Study group", [
      alice.id,
      bob.id,
    ]);
    await assert.rejects(
      service.sendMessage(conversation.id, alice.id, "  "),
      /не може бути порожнім/,
    );
    await assert.rejects(
      service.sendMessage(conversation.id, "not-a-participant", "Hello"),
      /лише учасники розмови/,
    );
    await assert.rejects(
      service.sendMessage(conversation.id, alice.id, "x".repeat(4001)),
      /довшим за 4000/,
    );
  });
});

test("searches case-insensitively only in the signed-in user's conversations", async () => {
  await withService(async ({ service }) => {
    const alice = await service.createUser("alice");
    const bob = await service.createUser("bob");
    const chris = await service.createUser("chris");
    const shared = await service.createConversation("Shared", [alice.id, bob.id]);
    const privateToChris = await service.createConversation("Chris and Bob", [
      chris.id,
      bob.id,
    ]);

    await service.sendMessage(shared.id, bob.id, "Bring the BIOLOGY book");
    await service.sendMessage(privateToChris.id, chris.id, "Biology notes");

    assert.equal(service.searchMessages(alice.id, "biology").length, 1);
    assert.equal(service.searchMessages(chris.id, "BIOLOGY").length, 1);
  });
});

test("persists valid JSON and rejects malformed persisted data", async () => {
  await withService(async ({ filePath }) => {
    const repository = new JsonRepository(filePath);
    await repository.save({ users: [], conversations: [], messages: [] });
    const data = await repository.load();
    assert.deepEqual(data, { users: [], conversations: [], messages: [] });
    assert.match(await readFile(filePath, "utf8"), /"conversations": \[\]/);

    await writeFile(filePath, '{"users": []}', "utf8");
    await assert.rejects(repository.load(), /invalid format/);
  });
});
