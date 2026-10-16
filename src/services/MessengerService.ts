import { randomUUID } from "node:crypto";
import type { Conversation, Message, MessengerData, User } from "../models";
import { MessengerError } from "./MessengerError";
import { JsonRepository } from "../storage/JsonRepository";

export { MessengerError } from "./MessengerError";

const MAX_USERNAME_LENGTH = 30;
const MAX_TITLE_LENGTH = 80;
const MAX_MESSAGE_LENGTH = 4000;

export class MessengerService {
  private constructor(
    private readonly repository: JsonRepository,
    private data: MessengerData,
  ) {}

  static async open(repository: JsonRepository): Promise<MessengerService> {
    return new MessengerService(repository, await repository.load());
  }

  listUsers(): User[] {
    return [...this.data.users].sort((a, b) =>
      a.username.localeCompare(b.username),
    );
  }

  findUserByUsername(username: string): User | undefined {
    const normalized = username.trim().toLocaleLowerCase();
    return this.data.users.find(
      (user) => user.username.toLocaleLowerCase() === normalized,
    );
  }

  getUser(userId: string): User | undefined {
    return this.data.users.find((user) => user.id === userId);
  }

  async createUser(username: string): Promise<User> {
    const normalized = username.trim();
    if (!/^[a-zA-Z0-9_.-]+$/.test(normalized)) {
      throw new MessengerError(
        "Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення.",
      );
    }
    if (normalized.length > MAX_USERNAME_LENGTH) {
      throw new MessengerError(`Ім’я не може бути довшим за ${MAX_USERNAME_LENGTH} символів.`);
    }
    if (this.findUserByUsername(normalized)) {
      throw new MessengerError("Це ім’я вже зайняте.", 409);
    }

    const user: User = {
      id: randomUUID(),
      username: normalized,
      createdAt: new Date().toISOString(),
    };
    await this.commit({ ...this.data, users: [...this.data.users, user] });
    return user;
  }

  listConversationsForUser(userId: string): Conversation[] {
    return this.data.conversations
      .filter((conversation) => conversation.participantIds.includes(userId))
      .sort((a, b) => a.title.localeCompare(b.title));
  }

  getConversation(conversationId: string): Conversation | undefined {
    return this.data.conversations.find(
      (conversation) => conversation.id === conversationId,
    );
  }

  getParticipants(conversation: Conversation): User[] {
    return conversation.participantIds
      .map((id) => this.data.users.find((user) => user.id === id))
      .filter((user): user is User => user !== undefined);
  }

  async createConversation(
    title: string,
    participantIds: string[],
  ): Promise<Conversation> {
    const normalizedTitle = title.trim();
    if (!normalizedTitle) {
      throw new MessengerError("Назва розмови не може бути порожньою.");
    }
    if (normalizedTitle.length > MAX_TITLE_LENGTH) {
      throw new MessengerError(`Назва не може бути довшою за ${MAX_TITLE_LENGTH} символів.`);
    }

    const uniqueParticipantIds = [...new Set(participantIds)];
    if (uniqueParticipantIds.length < 2) {
      throw new MessengerError("У розмові мають бути щонайменше двоє учасників.");
    }
    if (
      uniqueParticipantIds.some(
        (id) => !this.data.users.some((user) => user.id === id),
      )
    ) {
      throw new MessengerError("Усі учасники мають бути зареєстрованими користувачами.");
    }

    const conversation: Conversation = {
      id: randomUUID(),
      title: normalizedTitle,
      participantIds: uniqueParticipantIds,
      createdAt: new Date().toISOString(),
    };
    await this.commit({
      ...this.data,
      conversations: [...this.data.conversations, conversation],
    });
    return conversation;
  }

  getMessages(conversationId: string): Message[] {
    return this.data.messages
      .filter((message) => message.conversationId === conversationId)
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  async sendMessage(
    conversationId: string,
    senderId: string,
    text: string,
  ): Promise<Message> {
    const conversation = this.getConversation(conversationId);
    if (!conversation) {
      throw new MessengerError("Розмову не знайдено.", 404);
    }
    if (!conversation.participantIds.includes(senderId)) {
      throw new MessengerError("Повідомлення можуть надсилати лише учасники розмови.");
    }
    if (!text.trim()) {
      throw new MessengerError("Повідомлення не може бути порожнім.");
    }
    if (text.length > MAX_MESSAGE_LENGTH) {
      throw new MessengerError(`Повідомлення не може бути довшим за ${MAX_MESSAGE_LENGTH} символів.`);
    }

    const message: Message = {
      id: randomUUID(),
      conversationId,
      senderId,
      text,
      createdAt: new Date().toISOString(),
    };
    await this.commit({ ...this.data, messages: [...this.data.messages, message] });
    return message;
  }

  searchMessages(userId: string, query: string): Message[] {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) {
      throw new MessengerError("Введіть текст для пошуку.");
    }

    const visibleConversationIds = new Set(
      this.listConversationsForUser(userId).map((conversation) => conversation.id),
    );
    return this.data.messages
      .filter(
        (message) =>
          visibleConversationIds.has(message.conversationId) &&
          message.text.toLocaleLowerCase().includes(normalizedQuery),
      )
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  private async commit(nextData: MessengerData): Promise<void> {
    await this.repository.save(nextData);
    this.data = nextData;
  }
}
