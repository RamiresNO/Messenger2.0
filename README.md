# onegram

Вебмесенджер для навчальних груп. Сервер і логіка застосунку написані на Python зі стандартної бібліотеки. Інтерфейс використовує HTML, CSS і JavaScript для роботи у браузері.

## Можливості

- Створення профілів і перемикання між ними.
- Реєстрація та вхід за ніком і паролем.
- Вхід через Google OAuth після налаштування Google credentials.
- Особисті та групові розмови.
- Історія повідомлень із часовими мітками й автоматичним оновленням.
- Пошук повідомлень у розмовах поточного профілю.
- Валідація профілів, учасників і повідомлень та перевірка доступу.
- Адаптивний інтерфейс для комп’ютера, планшета й телефона.

## Запуск

Потрібен Python 3.10 або новіший. Залежності встановлювати не потрібно.

```sh
python app.py
```

Відкрий [http://localhost:3000](http://localhost:3000). Дані зберігаються у `data/messages.json`. Адресу та шлях до файла можна змінити змінними середовища `HOST`, `PORT` і `DATA_FILE_PATH`.

## Cloudflare

Для збереження публічної адреси `messenger2-0` Cloudflare Worker також реалізований на Python у `worker.py` і використовує наявну D1-базу. TypeScript та Node.js-код застосунку не потрібні. Для Python Workers потрібні [uv](https://docs.astral.sh/uv/) і Node.js для інструментів Cloudflare.

```sh
uv sync --dev
uv run pywrangler dev
```

Перед розгортанням застосуй схему до наявної бази й опублікуй Worker:

```sh
uv run pywrangler d1 migrations apply mini-messenger-db --remote
uv run pywrangler deploy
```

Конфігурація `wrangler.jsonc` уже містить ID бази `mini-messenger-db`. Не створюй нову базу: це збереже ту саму прив’язку й адресу Worker.

### Google OAuth

Створи OAuth 2.0 Client ID типу **Web application** у Google Cloud Console. Додай `https://messenger2-0.mrniko248.workers.dev/api/auth/google/callback` до **Authorized redirect URIs**. Для локальної перевірки додай також `http://localhost:3000/api/auth/google/callback` і задай `GOOGLE_REDIRECT_URI`.

У Cloudflare Worker додай runtime variable `GOOGLE_CLIENT_ID`, secret `GOOGLE_CLIENT_SECRET` і variable `GOOGLE_REDIRECT_URI` зі значенням Cloudflare callback URL. Для локального запуску задай ті самі змінні середовища перед `python app.py`. Не записуй client secret у Git.

## Тести

```sh
python -m unittest discover -s tests -p "test_*.py"
```

Тести перевіряють JSON-збереження, реєстрацію, парольний вхід, сесії, групові розмови, валідацію, пошук і HTTP API.

**Увага:** відновлення пароля та обмеження частоти спроб входу поки не реалізовані. Не використовуй застосунок для чутливих даних.
