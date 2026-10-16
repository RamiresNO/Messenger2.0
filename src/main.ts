import { resolve } from "node:path";
import { MessengerService } from "./services/MessengerService";
import { JsonRepository } from "./storage/JsonRepository";
import { createMessengerServer } from "./server";

async function main(): Promise<void> {
  const repository = new JsonRepository(
    process.env.DATA_FILE_PATH ??
      resolve(process.cwd(), "data", "messages.json"),
  );
  const messenger = await MessengerService.open(repository);
  const publicDirectory = resolve(__dirname, "..", "public");
  const port = Number(process.env.PORT ?? 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("PORT must be a valid port number.");
  }

  const server = createMessengerServer(messenger, publicDirectory);
  const host = process.env.HOST ?? "127.0.0.1";
  server.listen(port, host, () => {
    console.log(`Study Hall Messenger is running at http://${host}:${port}`);
  });

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      server.close((error) => {
        if (error) {
          console.error("Could not stop the messenger server cleanly:", error);
          process.exitCode = 1;
        }
      });
    });
  }
}

main().catch((error: unknown) => {
  console.error(
    "Messenger could not start:",
    error instanceof Error ? error.message : error,
  );
  process.exitCode = 1;
});
