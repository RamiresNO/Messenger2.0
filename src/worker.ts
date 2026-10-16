import { MessengerError } from "./services/MessengerError";

interface D1Result<T> {
  results: T[];
}

interface D1PreparedStatement {
  bind(...values: (string | number | null)[]): D1PreparedStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<D1Result<T>>;
  run(): Promise<unknown>;
}

interface D1Database {
  prepare(query: string): D1PreparedStatement;
  batch<T = unknown>(statements: D1PreparedStatement[]): Promise<T[]>;
}

interface WorkerEnv {
  DB: D1Database;
  ASSETS: { fetch(request: Request): Promise<Response> };
}

interface User {
  id: string;
  username: string;
  createdAt: string;
}

interface Conversation {
  id: string;
  title: string;
  participantIds: string[];
  createdAt: string;
}

interface Message {
  id: string;
  conversationId: string;
  senderId: string;
  text: string;
  createdAt: string;
}

interface UserRow {
  id: string;
  username: string;
  created_at: string;
}

interface ConversationRow {
  id: string;
  title: string;
  created_at: string;
}

interface ParticipantRow extends ConversationRow {
  user_id: string;
}

interface MessageRow {
  id: string;
  conversation_id: string;
  sender_id: string;
  text: string;
  created_at: string;
}

const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_USERNAME_LENGTH = 30;
const MAX_TITLE_LENGTH = 80;
const MAX_MESSAGE_LENGTH = 4000;

function jsonResponse(payload: unknown, status = 200): Response {
  return Response.json(payload, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function secureResponse(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set(
    "Content-Security-Policy",
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  );
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function errorResponse(error: unknown): Response {
  if (error instanceof MessengerError) {
    return jsonResponse({ error: error.message }, error.statusCode);
  }
  if (error instanceof SyntaxError) {
    return jsonResponse({ error: "Некоректний формат JSON." }, 400);
  }
  console.error("Messenger Worker request failed:", error);
  return jsonResponse({ error: "Сталася внутрішня помилка сервера." }, 500);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(
  value: Record<string, unknown>,
  key: string,
): string {
  const result = value[key];
  if (typeof result !== "string") {
    throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
  }
  return result;
}

async function readJson(request: Request): Promise<unknown> {
  const contentLength = Number(request.headers.get("Content-Length") ?? 0);
  if (contentLength > MAX_REQUEST_BYTES) {
    throw new MessengerError("Запит завеликий.");
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > MAX_REQUEST_BYTES) {
    throw new MessengerError("Запит завеликий.");
  }
  if (!text) {
    throw new SyntaxError("Request body is empty.");
  }
  return JSON.parse(text) as unknown;
}

function toUser(row: UserRow): User {
  return { id: row.id, username: row.username, createdAt: row.created_at };
}

function toMessage(row: MessageRow): Message {
  return {
    id: row.id,
    conversationId: row.conversation_id,
    senderId: row.sender_id,
    text: row.text,
    createdAt: row.created_at,
  };
}

async function getUser(db: D1Database, userId: string): Promise<User | null> {
  const row = await db
    .prepare(
      "SELECT id, username, created_at FROM users WHERE id = ?",
    )
    .bind(userId)
    .first<UserRow>();
  return row ? toUser(row) : null;
}

async function handleApi(request: Request, env: WorkerEnv): Promise<Response> {
  const url = new URL(request.url);

  if (url.pathname === "/api/users" && request.method === "GET") {
    const result = await env.DB.prepare(
      "SELECT id, username, created_at FROM users ORDER BY username COLLATE NOCASE",
    ).all<UserRow>();
    return jsonResponse({ users: result.results.map(toUser) });
  }

  if (url.pathname === "/api/users" && request.method === "POST") {
    const body = await readJson(request);
    if (!isRecord(body)) {
      throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
    }
    const username = requireString(body, "username").trim();
    if (!/^[a-zA-Z0-9_.-]+$/.test(username)) {
      throw new MessengerError(
        "Ім’я може містити лише латинські літери, цифри, крапки, дефіси та підкреслення.",
      );
    }
    if (username.length > MAX_USERNAME_LENGTH) {
      throw new MessengerError(
        `Ім’я не може бути довшим за ${MAX_USERNAME_LENGTH} символів.`,
      );
    }
    const user: User = {
      id: crypto.randomUUID(),
      username,
      createdAt: new Date().toISOString(),
    };
    const insertedUser = await env.DB.prepare(
      `INSERT INTO users (id, username, created_at) VALUES (?, ?, ?)
       ON CONFLICT(username) DO NOTHING
       RETURNING id, username, created_at`,
    )
      .bind(user.id, user.username, user.createdAt)
      .first<UserRow>();
    if (!insertedUser) {
      throw new MessengerError("Це ім’я вже зайняте.", 409);
    }
    return jsonResponse({ user: toUser(insertedUser) }, 201);
  }

  if (url.pathname === "/api/conversations" && request.method === "GET") {
    const userId = url.searchParams.get("userId");
    if (!userId || !(await getUser(env.DB, userId))) {
      throw new MessengerError("Користувача не знайдено.", 404);
    }
    const result = await env.DB.prepare(
      `SELECT c.id, c.title, c.created_at, cp.user_id
       FROM conversations AS c
       JOIN conversation_participants AS cp ON cp.conversation_id = c.id
       WHERE c.id IN (
         SELECT conversation_id FROM conversation_participants WHERE user_id = ?
       )
       ORDER BY c.title COLLATE NOCASE, cp.user_id`,
    )
      .bind(userId)
      .all<ParticipantRow>();
    return jsonResponse({
      conversations: groupConversations(result.results),
    });
  }

  if (url.pathname === "/api/conversations" && request.method === "POST") {
    const body = await readJson(request);
    if (!isRecord(body) || !Array.isArray(body.participantIds)) {
      throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
    }
    if (!body.participantIds.every((id) => typeof id === "string")) {
      throw new MessengerError("Перевірте список учасників.");
    }

    const title = requireString(body, "title").trim();
    if (!title) {
      throw new MessengerError("Назва розмови не може бути порожньою.");
    }
    if (title.length > MAX_TITLE_LENGTH) {
      throw new MessengerError(
        `Назва не може бути довшою за ${MAX_TITLE_LENGTH} символів.`,
      );
    }
    const participantIds = [...new Set(body.participantIds)];
    if (participantIds.length < 2) {
      throw new MessengerError("У розмові мають бути щонайменше двоє учасників.");
    }
    const placeholders = participantIds.map(() => "?").join(", ");
    const foundUsers = await env.DB.prepare(
      `SELECT id FROM users WHERE id IN (${placeholders})`,
    )
      .bind(...participantIds)
      .all<{ id: string }>();
    if (foundUsers.results.length !== participantIds.length) {
      throw new MessengerError(
        "Усі учасники мають бути зареєстрованими користувачами.",
      );
    }

    const conversation: Conversation = {
      id: crypto.randomUUID(),
      title,
      participantIds,
      createdAt: new Date().toISOString(),
    };
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
      ).bind(conversation.id, conversation.title, conversation.createdAt),
      ...participantIds.map((userId) =>
        env.DB
          .prepare(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
          )
          .bind(conversation.id, userId),
      ),
    ]);
    return jsonResponse({ conversation }, 201);
  }

  const messageRoute = url.pathname.match(
    /^\/api\/conversations\/([^/]+)\/messages$/,
  );
  if (messageRoute) {
    const conversationId = messageRoute[1];
    if (request.method === "GET") {
      const userId = url.searchParams.get("userId");
      const participant = userId
        ? await env.DB.prepare(
            "SELECT 1 AS found FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
          )
            .bind(conversationId, userId)
            .first<{ found: number }>()
        : null;
      if (!participant) {
        throw new MessengerError("Розмову не знайдено.", 404);
      }
      const result = await env.DB.prepare(
        `SELECT id, conversation_id, sender_id, text, created_at
         FROM messages WHERE conversation_id = ?
         ORDER BY created_at, id`,
      )
        .bind(conversationId)
        .all<MessageRow>();
      return jsonResponse({ messages: result.results.map(toMessage) });
    }

    if (request.method === "POST") {
      const body = await readJson(request);
      if (!isRecord(body)) {
        throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
      }
      const senderId = requireString(body, "senderId");
      const text = requireString(body, "text");
      if (!text.trim()) {
        throw new MessengerError("Повідомлення не може бути порожнім.");
      }
      if (text.length > MAX_MESSAGE_LENGTH) {
        throw new MessengerError(
          `Повідомлення не може бути довшим за ${MAX_MESSAGE_LENGTH} символів.`,
        );
      }
      const participant = await env.DB.prepare(
        "SELECT 1 AS found FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
      )
        .bind(conversationId, senderId)
        .first<{ found: number }>();
      if (!participant) {
        throw new MessengerError(
          "Повідомлення можуть надсилати лише учасники розмови.",
        );
      }

      const message: Message = {
        id: crypto.randomUUID(),
        conversationId,
        senderId,
        text,
        createdAt: new Date().toISOString(),
      };
      await env.DB.prepare(
        `INSERT INTO messages (id, conversation_id, sender_id, text, created_at)
         VALUES (?, ?, ?, ?, ?)`,
      )
        .bind(
          message.id,
          message.conversationId,
          message.senderId,
          message.text,
          message.createdAt,
        )
        .run();
      return jsonResponse({ message }, 201);
    }
  }

  if (url.pathname === "/api/search" && request.method === "GET") {
    const userId = url.searchParams.get("userId");
    if (!userId || !(await getUser(env.DB, userId))) {
      throw new MessengerError("Користувача не знайдено.", 404);
    }
    const query = (url.searchParams.get("q") ?? "").trim();
    if (!query) {
      throw new MessengerError("Введіть текст для пошуку.");
    }
    const result = await env.DB.prepare(
      `SELECT m.id, m.conversation_id, m.sender_id, m.text, m.created_at
       FROM messages AS m
       JOIN conversation_participants AS cp
         ON cp.conversation_id = m.conversation_id
       WHERE cp.user_id = ?
       ORDER BY m.created_at, m.id`,
    )
      .bind(userId)
      .all<MessageRow>();
    const normalizedQuery = query.toLocaleLowerCase();
    const messages = result.results
      .filter((row) => row.text.toLocaleLowerCase().includes(normalizedQuery))
      .map(toMessage);
    return jsonResponse({ messages });
  }

  return jsonResponse({ error: "Маршрут не знайдено." }, 404);
}

function groupConversations(rows: ParticipantRow[]): Conversation[] {
  const conversations = new Map<string, Conversation>();
  for (const row of rows) {
    let conversation = conversations.get(row.id);
    if (!conversation) {
      conversation = {
        id: row.id,
        title: row.title,
        participantIds: [],
        createdAt: row.created_at,
      };
      conversations.set(row.id, conversation);
    }
    conversation.participantIds.push(row.user_id);
  }
  return [...conversations.values()];
}

export default {
  async fetch(request: Request, env: WorkerEnv): Promise<Response> {
    try {
      const url = new URL(request.url);
      const response = url.pathname.startsWith("/api/")
        ? await handleApi(request, env)
        : await env.ASSETS.fetch(request);
      return secureResponse(response);
    } catch (error) {
      return secureResponse(errorResponse(error));
    }
  },
};
