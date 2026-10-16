import { readFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { extname, join } from "node:path";
import { MessengerError } from "./services/MessengerError";
import { MessengerService } from "./services/MessengerService";

const MAX_REQUEST_BYTES = 16 * 1024;
const STATIC_FILES = new Map([
  ["/", "index.html"],
  ["/styles.css", "styles.css"],
  ["/app.js", "app.js"],
]);

function sendJson(
  response: ServerResponse,
  statusCode: number,
  payload: unknown,
): void {
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(payload));
}

function sendError(response: ServerResponse, error: unknown): void {
  if (error instanceof MessengerError) {
    sendJson(response, error.statusCode, { error: error.message });
    return;
  }
  if (error instanceof SyntaxError) {
    sendJson(response, 400, { error: "Некоректний формат JSON." });
    return;
  }
  console.error("Messenger request failed:", error);
  sendJson(response, 500, { error: "Сталася внутрішня помилка сервера." });
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_REQUEST_BYTES) {
      throw new MessengerError("Запит завеликий.");
    }
    chunks.push(buffer);
  }

  if (size === 0) {
    throw new SyntaxError("Request body is empty.");
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
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

async function serveStatic(
  response: ServerResponse,
  publicDirectory: string,
  fileName: string,
): Promise<void> {
  const filePath = join(publicDirectory, fileName);
  const contents = await readFile(filePath);
  const contentType =
    extname(fileName) === ".css"
      ? "text/css; charset=utf-8"
      : extname(fileName) === ".js"
        ? "text/javascript; charset=utf-8"
        : "text/html; charset=utf-8";

  response.writeHead(200, {
    "Content-Type": contentType,
    "Cache-Control": "no-cache",
  });
  response.end(contents);
}

export function createMessengerServer(
  messenger: MessengerService,
  publicDirectory: string,
): Server {
  return createServer((request, response) => {
    response.setHeader("X-Content-Type-Options", "nosniff");
    response.setHeader("X-Frame-Options", "DENY");
    response.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    );

    void handleRequest(request, response, messenger, publicDirectory).catch(
      (error: unknown) => sendError(response, error),
    );
  });
}

async function handleRequest(
  request: IncomingMessage,
  response: ServerResponse,
  messenger: MessengerService,
  publicDirectory: string,
): Promise<void> {
  const requestUrl = new URL(request.url ?? "/", "http://localhost");
  const staticFile = STATIC_FILES.get(requestUrl.pathname);
  if (request.method === "GET" && staticFile) {
    await serveStatic(response, publicDirectory, staticFile);
    return;
  }

  if (requestUrl.pathname === "/api/users" && request.method === "GET") {
    sendJson(response, 200, { users: messenger.listUsers() });
    return;
  }

  if (requestUrl.pathname === "/api/users" && request.method === "POST") {
    const body = await readJson(request);
    if (!isRecord(body)) {
      throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
    }
    const user = await messenger.createUser(requireString(body, "username"));
    sendJson(response, 201, { user });
    return;
  }

  if (
    requestUrl.pathname === "/api/conversations" &&
    request.method === "GET"
  ) {
    const userId = requestUrl.searchParams.get("userId");
    if (!userId || !messenger.getUser(userId)) {
      throw new MessengerError("Користувача не знайдено.", 404);
    }
    sendJson(response, 200, {
      conversations: messenger.listConversationsForUser(userId),
    });
    return;
  }

  if (
    requestUrl.pathname === "/api/conversations" &&
    request.method === "POST"
  ) {
    const body = await readJson(request);
    if (!isRecord(body) || !Array.isArray(body.participantIds)) {
      throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
    }
    if (!body.participantIds.every((id) => typeof id === "string")) {
      throw new MessengerError("Перевірте список учасників.");
    }
    const conversation = await messenger.createConversation(
      requireString(body, "title"),
      body.participantIds,
    );
    sendJson(response, 201, { conversation });
    return;
  }

  const messagesRoute = requestUrl.pathname.match(
    /^\/api\/conversations\/([^/]+)\/messages$/,
  );
  if (messagesRoute) {
    const conversationId = messagesRoute[1];
    if (request.method === "GET") {
      const userId = requestUrl.searchParams.get("userId");
      const conversation = messenger.getConversation(conversationId);
      if (
        !userId ||
        !conversation ||
        !conversation.participantIds.includes(userId)
      ) {
        throw new MessengerError("Розмову не знайдено.", 404);
      }
      sendJson(response, 200, {
        messages: messenger.getMessages(conversationId),
      });
      return;
    }

    if (request.method === "POST") {
      const body = await readJson(request);
      if (!isRecord(body)) {
        throw new MessengerError("Перевірте заповнені поля та спробуйте ще раз.");
      }
      const message = await messenger.sendMessage(
        conversationId,
        requireString(body, "senderId"),
        requireString(body, "text"),
      );
      sendJson(response, 201, { message });
      return;
    }
  }

  if (requestUrl.pathname === "/api/search" && request.method === "GET") {
    const userId = requestUrl.searchParams.get("userId");
    if (!userId || !messenger.getUser(userId)) {
      throw new MessengerError("Користувача не знайдено.", 404);
    }
    const messages = messenger.searchMessages(
      userId,
      requestUrl.searchParams.get("q") ?? "",
    );
    sendJson(response, 200, { messages });
    return;
  }

  if (requestUrl.pathname.startsWith("/api/")) {
    sendJson(response, 404, { error: "Маршрут не знайдено." });
    return;
  }

  sendJson(response, 404, { error: "Сторінку не знайдено." });
}
