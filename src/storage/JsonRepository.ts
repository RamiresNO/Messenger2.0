import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import {
  emptyMessengerData,
  type Conversation,
  type Message,
  type MessengerData,
  type User,
} from "../models";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}

function isUser(value: unknown): value is User {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    typeof value.username === "string" &&
    value.username.length > 0 &&
    isTimestamp(value.createdAt)
  );
}

function isConversation(value: unknown): value is Conversation {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    typeof value.title === "string" &&
    value.title.trim().length > 0 &&
    Array.isArray(value.participantIds) &&
    value.participantIds.every(
      (participantId) =>
        typeof participantId === "string" && participantId.length > 0,
    ) &&
    isTimestamp(value.createdAt)
  );
}

function isMessage(value: unknown): value is Message {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    typeof value.conversationId === "string" &&
    value.conversationId.length > 0 &&
    typeof value.senderId === "string" &&
    value.senderId.length > 0 &&
    typeof value.text === "string" &&
    value.text.trim().length > 0 &&
    isTimestamp(value.createdAt)
  );
}

function isMessengerData(value: unknown): value is MessengerData {
  if (!isRecord(value)) {
    return false;
  }

  const { users, conversations, messages } = value;
  if (
    !Array.isArray(users) ||
    !Array.isArray(conversations) ||
    !Array.isArray(messages) ||
    !users.every(isUser) ||
    !conversations.every(isConversation) ||
    !messages.every(isMessage)
  ) {
    return false;
  }

  const userIds = new Set(users.map((user) => user.id));
  const conversationIds = new Set(
    conversations.map((conversation) => conversation.id),
  );
  if (
    userIds.size !== users.length ||
    conversationIds.size !== conversations.length ||
    new Set(
      users.map((user) => user.username.toLocaleLowerCase()),
    ).size !== users.length
  ) {
    return false;
  }

  return (
    conversations.every(
      (conversation) =>
        conversation.participantIds.length >= 2 &&
        new Set(conversation.participantIds).size ===
          conversation.participantIds.length &&
        conversation.participantIds.every((id) => userIds.has(id)),
    ) &&
    messages.every((message) => {
      const conversation = conversations.find(
        (candidate) => candidate.id === message.conversationId,
      );
      return (
        conversation !== undefined &&
        conversation.participantIds.includes(message.senderId)
      );
    }) &&
    new Set(messages.map((message) => message.id)).size === messages.length
  );
}

export class JsonRepository {
  constructor(private readonly filePath: string) {}

  async load(): Promise<MessengerData> {
    let contents: string;
    try {
      contents = await readFile(this.filePath, "utf8");
    } catch (error) {
      if (isRecord(error) && error.code === "ENOENT") {
        return emptyMessengerData();
      }
      throw error;
    }

    let data: unknown;
    try {
      data = JSON.parse(contents);
    } catch (error) {
      throw new Error(`Could not parse messenger data at ${this.filePath}.`, {
        cause: error,
      });
    }

    if (!isMessengerData(data)) {
      throw new Error(`Messenger data at ${this.filePath} has an invalid format.`);
    }
    return data;
  }

  async save(data: MessengerData): Promise<void> {
    await mkdir(dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${randomUUID()}.tmp`;

    try {
      await writeFile(temporaryPath, JSON.stringify(data, null, 2), {
        encoding: "utf8",
        flag: "wx",
      });
      await rename(temporaryPath, this.filePath);
    } catch (error) {
      try {
        await unlink(temporaryPath);
      } catch (cleanupError) {
        if (!(isRecord(cleanupError) && cleanupError.code === "ENOENT")) {
          throw new AggregateError(
            [error, cleanupError],
            `Could not save messenger data or clean up ${temporaryPath}.`,
          );
        }
      }
      throw error;
    }
  }
}
