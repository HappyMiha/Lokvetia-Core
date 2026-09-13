# Planning you can return to

`/studio` lists the authenticated owner's saved games, newest first, with pagination. Selecting a game updates the URL; returning in the same tab restores the owner's last selection, otherwise it opens the most recent game. Only the opaque selected key is kept in tab session storage, under the owner identity; project text and AI credentials are not stored there. Every read still enforces server-side ownership. Opening, refreshing or switching pages never submits planning. A successful Start creates the mission before enqueuing work, so it remains discoverable even if the response is lost.

The create form states the actual qualified Ollama model before Start. The current one-click planning path is local-only with a zero paid-API limit; connecting Codex or Gemini does not select them for this path. The connection page also names the model behind its local-AI card.

The planning panel reports the configured model, local/paid boundary, the latest persisted event, worker state, five named planning steps, completed validated artifacts and attempts. The progress bar counts accepted role artifacts, not elapsed time or token generation. Token counts are explicitly unavailable. A failed or merely finished worker is never labeled a ready plan: the proposal must have reached its review phase. Partial validated results and bounded validation errors remain readable after failure.

Browser reloads retain the game through server storage and its URL. After a server restart the library is still present. If no live worker or durable terminal evidence confirms a run, it is labeled unconfirmed/interrupted; reading the page never resumes it automatically.

**Stop planning** requires the authenticated mission owner, local write/control scopes and the existing same-origin confirmation boundary. It first revokes the durable studio mandate, then cancels the queued future or signals the current CLI. The planning invoker passes cancellation to the existing runtime/process supervisor and checks revocation before any later model request. The UI distinguishes stopping from stopped; previous evidence is retained. A replay of a cancelled queued creation cannot restore its mandate or requeue it.

Existing role qualification, model bindings, proposal validation, spending permissions and game execution gates remain in force. This change exposes and controls planning; it does not promise that a model's output will pass validation or that a complete executable game has been built.

## Українською

На сторінці **Студія** оберіть гру в блоці **Мої ігри**. Нижче видно модель, стан і перевірені етапи. Повернення на сторінку не починає планування заново. Якщо планування зупинилось із помилкою, відкрийте **Чому зупинилося** та **Збережені результати**. Якщо робота триває, доступна кнопка **Зупинити планування**. Зупинка не видаляє гру або попередні результати.
