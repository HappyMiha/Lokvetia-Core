# Connect your AI

Open **Settings → AI access** in your local Lokvetia installation, or visit `/connect`. Choose one card:

| AI | What you do | What Lokvetia does |
| --- | --- | --- |
| Codex CLI | Click **Connect**. Sign in to ChatGPT if prompted. | Finds the official CLI, reuses its existing login, or opens official login; checks an actual response. |
| Gemini CLI | Click **Connect**. Complete Google login when available; enter a Gemini API key if Google refuses this login method. | Reuses CLI authentication, opens the official Google flow when needed, stores an optional key in Windows Credential Manager, then checks the response. |
| Local model | Click **Connect**. No account or key. | Finds or installs Ollama, starts it when needed, uses an installed supported Qwen model or downloads Qwen 2.5 Coder 7B, then checks a response and automatically qualifies the local planning roles. |

One connected AI is enough to leave this page. You can add others later. A browser reload restores the current connection job. **Cancel** stops setup; **Disconnect** removes Lokvetia's connection and any stored Gemini key. It does not sign you out of your official CLI, uninstall software, delete models, or stop an existing Ollama service. Execution permissions for projects remain separate.

## Українською

Відкрийте **Налаштування й деталі → Доступ до AI**. Натисніть **Підключити** біля Codex, Gemini або локальної моделі. Якщо ви вже входили в офіційний CLI на цьому ПК, майстер використає наявний вхід. Інакше відкриється сторінка провайдера. Після входу перевірка продовжиться автоматично.

Якщо Google відмовить у вході через Gemini CLI, з’явиться одне поле **Gemini API key** з посиланням на Google AI Studio. Вставте ключ і натисніть **Зберегти й підключити**. Ключ не потрібно надсилати в чат, записувати у файл конфігурації чи вставляти в командний рядок.

Коли з’явиться **Підключено**, натисніть **Перейти до студії**. Для локальної моделі акаунт і ключ не потрібні. Якщо підтримуваної моделі немає, завантаження займає приблизно 4,7 ГБ. Майстер перевіряє мінімум 8 ГБ оперативної пам’яті та 6 ГБ вільного місця перед завантаженням.

На кожному новому ПК відкрийте встановлену локальну студію й натисніть **Підключити** на картці локального AI. Майстер також автоматично реєструє цей ПК і перевіряє сім ролей короткими локальними запитами API та CLI, до чотирьох хвилин на перевірку. Готовність записується в базу саме цієї інсталяції. На ПК без окремої відеокарти фактичні перевірки все одно мають пройти в межах часу; сам факт встановлення не означає готовність. Невдала перевірка показує дію для повторення, а встановлену модель зберігає. Це перевірка формату й доступності, а не гарантія якості плану для довільної гри.

## Scope and recovery

- The automatic install path targets Windows with Winget. Codex and Gemini use official, pinned npm packages with lifecycle scripts disabled. Existing native/official Node launchers are detected without running shell wrappers. Machines without Winget or usable Node receive an installation link and can retry; admin prompts, provider consent, unavailable packages and account restrictions cannot be silently resolved.
- Setup sends only a synthetic marker prompt, with no project attached. Codex runs ephemeral/read-only with user configuration ignored. Gemini uses a deny-tools admin policy, disabled extensions/MCP access and hooks for the check. The short check uses your provider's limits. It proves connection, not qualification for every role or permission to spend on a project.
- A Gemini `UNSUPPORTED_CLIENT`/ineligible-account response asks for an API key instead of repeating the refused OAuth flow. API access and quotas depend on the Google account. The live Windows check qualified cached Codex authentication and installed Ollama; this account's Gemini OAuth was refused. The API-key path is covered by synthetic integration/browser tests, not a claimed successful live Google request.
- Only the local `operations_owner`, with local tenant and write/control scopes, can start, cancel or disconnect. Mutations require the same-origin HTTP boundary and confirmation header. A hosted server does not connect visitors to the server operator's personal AI. Use your local installation.
- A connection job has a 15-minute deadline. Logout or policy revocation cancels it; owned CLI processes have bounded output and timeouts. Browser reloads recover state. After a service crash, an unfinished job can be cancelled/retried or expires; CLI processes are not automatically reattached. An already submitted Ollama canary can finish within its 120-second timeout after cancellation, but cannot publish a ready connection.
- Optional keys are stored under an opaque, workspace/owner-bound reference in Windows Credential Manager; SQLite holds only the reference and connection state. Keys are never returned by the API or put in browser storage. Gemini receives the key only after the existing execution approval, for the approving owner. Known key values are redacted from provider output. Disconnect revokes the reference before attempting OS cleanup; an OS deletion failure may leave an inaccessible entry for manual Credential Manager cleanup.

Official authentication guidance: [Codex](https://learn.chatgpt.com/docs/auth), [Gemini CLI](https://geminicli.com/docs/get-started/authentication/), [Google AI Studio keys](https://aistudio.google.com/apikey).

No backlog acceptance or full autonomous game-creation claim is established by a connection check.
