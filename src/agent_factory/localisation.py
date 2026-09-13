"""Two languages, one catalogue, and a test that fails when one is missing.

The product claims Ukrainian and English. A half-translated interface is worse
than an untranslated one, because it hides which half is missing, so every
message here carries both languages and a completeness test refuses a catalogue
where one is blank. Nothing falls back silently.

Language is negotiated from the request, never guessed from the machine: an
explicit choice wins, then a stored preference, then what the browser asked for,
then Ukrainian.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

LANGUAGES = ("uk", "en")
DEFAULT_LANGUAGE = "uk"
LANGUAGE_COOKIE = "lokvetia_language"
ACCEPT_LANGUAGE = re.compile(r"([A-Za-z]{2,3})(?:-[A-Za-z0-9]+)?\s*(?:;\s*q=([0-9.]+))?")


class LocalisedError(ValueError):
    """An error that carries its text in every supported language.

    A message a person has to act on must reach them in their language, so the
    error travels as a Message and the boundary that knows the request renders
    it. str() gives Ukrainian so a log line still reads.
    """

    def __init__(self, message: "Message", **parameters: Any):
        self.message = message
        # The values that make the message specific travel with it, so the
        # boundary can render the same error in either language without having
        # to know what went wrong.
        self.parameters = parameters
        super().__init__(message.text(DEFAULT_LANGUAGE, **parameters))

    def text(self, language: str = DEFAULT_LANGUAGE) -> str:
        return self.message.text(language, **self.parameters)


class MissingTranslation(ValueError):
    """Raised when a message does not carry every supported language."""


@dataclass(frozen=True)
class Message:
    """One piece of text in every language the product claims to support."""

    uk: str
    en: str

    def __post_init__(self) -> None:
        for language in LANGUAGES:
            if not str(getattr(self, language)).strip():
                raise MissingTranslation(
                    f"Message is missing its {language} text: {self!r}"
                )

    def text(self, language: str = DEFAULT_LANGUAGE, **parameters: Any) -> str:
        value = getattr(self, normalise(language))
        return value.format(**parameters) if parameters else value

    def record(self) -> dict[str, str]:
        return {"uk": self.uk, "en": self.en}


def verbatim(text: str) -> Message:
    """A name that comes from data rather than from us.

    A workflow stage, a project or a provider is named by whoever configured
    it, in one language. Repeating that name in every slot states plainly that
    it is the same name everywhere; inventing a translation for it would be a
    guess wearing the clothes of a fact.
    """
    value = str(text).strip()
    if not value:
        raise MissingTranslation("A name taken from data cannot be empty")
    return Message(value, value)


def normalise(language: str | None) -> str:
    """Reduce a tag to a supported language, or to the default."""
    candidate = str(language or "").strip().casefold().replace("_", "-").split("-")[0]
    return candidate if candidate in LANGUAGES else DEFAULT_LANGUAGE


def negotiate(
    *,
    explicit: str | None = None,
    stored: str | None = None,
    accept_language: str | None = None,
) -> str:
    """An explicit choice wins, then a stored one, then the browser's preference."""
    for candidate in (explicit, stored):
        if candidate and normalise(candidate) == str(candidate).strip().casefold()[:2]:
            return normalise(candidate)
    ranked: list[tuple[float, int, str]] = []
    for index, match in enumerate(ACCEPT_LANGUAGE.finditer(accept_language or "")):
        tag = match.group(1).casefold()
        if tag not in LANGUAGES:
            continue
        try:
            quality = float(match.group(2)) if match.group(2) else 1.0
        except ValueError:
            quality = 1.0
        ranked.append((-quality, index, tag))
    if ranked:
        return sorted(ranked)[0][2]
    return DEFAULT_LANGUAGE


CATALOGUE: Mapping[str, Message] = {
    # Shared chrome
    "app.name": Message("Lokvetia Core", "Lokvetia Core"),
    "app.workspace": Message("Локальний робочий простір", "Local workspace"),
    "nav.games": Message("Мої ігри", "My games"),
    "nav.hardware": Message("Перевірити ПК", "Check this PC"),
    "nav.credentials": Message("Доступ до AI", "AI access"),
    "nav.operations": Message("Панель оператора", "Operator console"),
    "nav.settings": Message("Налаштування", "Settings"),
    "nav.progress": Message("Хід роботи", "Work in progress"),
    "nav.login": Message("Доступ і вихід", "Sign in and out"),
    "nav.skip": Message("До вмісту", "Skip to content"),
    "nav.aria": Message("Навігація", "Navigation"),
    "language.label": Message("Мова", "Language"),
    "language.uk": Message("Українська", "Ukrainian"),
    "language.en": Message("Англійська", "English"),
    "common.unknown": Message("Невідомо", "Unknown"),
    "common.loading": Message("Завантаження…", "Loading…"),
    "common.refresh": Message("Оновити", "Refresh"),
    "common.cancel": Message("Скасувати", "Cancel"),
    "common.apply": Message("Застосувати", "Apply"),

    # Settings page
    "settings.title": Message("Налаштування", "Settings"),
    "settings.eyebrow": Message("Робочий простір", "Workspace"),
    "settings.lead": Message(
        "Усе, що впливає на роботу, зібране тут: чинне значення, звідки воно "
        "взялося, і що зміниться, якщо його змінити. Файли редагувати не потрібно.",
        "Everything that affects how this works is here: the current value, where "
        "it came from, and what changes if you change it. No file editing needed.",
    ),
    "settings.who.label": Message("Хто змінює", "Who is changing this"),
    "settings.who.placeholder": Message("Ваше імʼя", "Your name"),
    "settings.who.help": Message(
        "Кожна зміна записується на конкретну людину й лишається в незмінній "
        "історії внизу сторінки. Без імені зміна не зберігається.",
        "Every change is recorded against a named person and stays in the "
        "unchangeable history below. Without a name, nothing is saved.",
    ),
    "settings.save": Message("Зберегти", "Save"),
    "settings.reset": Message("Повернути типове", "Restore the default"),
    "settings.verify": Message("Перевірити розділ", "Check this section"),
    "settings.reason.placeholder": Message(
        "Причина (необовʼязково)", "Reason (optional)"
    ),
    "settings.reason.label": Message("Причина зміни: {label}", "Reason for: {label}"),
    "settings.save.label": Message("Зберегти: {label}", "Save: {label}"),
    "settings.reset.label": Message(
        "Повернути типове: {label}", "Restore the default: {label}"
    ),
    "settings.verify.label": Message(
        "Перевірити розділ: {title}", "Check this section: {title}"
    ),
    "settings.badge.default": Message("типове", "default"),
    "settings.badge.changed": Message("змінено", "changed"),
    "settings.badge.sensitive": Message("важливе", "important"),
    "settings.badge.locked": Message("лише перегляд", "view only"),
    "settings.origin.default": Message("Типове: {value}", "Default: {value}"),
    "settings.origin.source": Message(
        "Джерело типового: {source}", "Where the default comes from: {source}"
    ),
    "settings.origin.unit": Message("Одиниці: {unit}", "Unit: {unit}"),
    "settings.origin.changed_by": Message(
        "Змінив(ла) {actor}", "Changed by {actor}"
    ),
    "settings.origin.reason": Message("Причина: {reason}", "Reason: {reason}"),
    "settings.consequence": Message(
        "Якщо змінити: {consequence}", "If you change it: {consequence}"
    ),
    "settings.current": Message("Чинне значення: {value}", "Current value: {value}"),
    "settings.changed_count": Message(
        "Змінено від типового: {count}", "Changed from the default: {count}"
    ),
    "settings.summary.changed": Message(
        "Значень, змінених від типового: {count}.",
        "Values changed from their default: {count}.",
    ),
    "settings.summary.clean": Message(
        "Усі значення типові — нічого не перевизначено.",
        "Every value is the default — nothing is overridden.",
    ),
    "settings.saved": Message("Збережено: {label}.", "Saved: {label}."),
    "settings.restored": Message(
        "Повернуто типове: {label}.", "Restored the default: {label}."
    ),
    "settings.applied": Message(
        "Зміну застосовано з підтвердженням наслідку.",
        "The change was applied with its consequence acknowledged.",
    ),
    "settings.not_applied": Message(
        "Зміну не застосовано: наслідок не підтверджено.",
        "Not applied: the consequence was not acknowledged.",
    ),
    "settings.need_actor": Message(
        "Спершу вкажіть, хто змінює.", "First say who is making the change."
    ),
    "settings.read_failed": Message(
        "Не вдалося прочитати налаштування ({status}).",
        "Could not read the settings ({status}).",
    ),
    "settings.history.title": Message("Історія змін", "Change history"),
    "settings.history.note": Message(
        "Записи не редагуються й не видаляються — цього не дозволяє сама база.",
        "Entries are never edited or deleted — the database itself refuses.",
    ),
    "settings.history.empty": Message(
        "Змін ще не було: усі значення типові.",
        "No changes yet: every value is the default.",
    ),
    "settings.history.when": Message("Коли", "When"),
    "settings.history.setting": Message("Налаштування", "Setting"),
    "settings.history.change": Message("Було → стало", "Before → after"),
    "settings.history.who": Message("Хто", "Who"),
    "settings.history.why": Message("Причина", "Reason"),
    "settings.history.region": Message(
        "Історія змін налаштувань", "Settings change history"
    ),
    "settings.confirm.title": Message("Підтвердьте наслідок", "Acknowledge the cost"),
    "settings.confirm.reason": Message("Причина зміни", "Reason for the change"),
    "settings.confirm.ack": Message(
        "Розумію наслідок і беру його на себе",
        "I understand the consequence and accept it",
    ),

    # Work in progress page
    "work.title": Message("Хід роботи", "Work in progress"),
    "work.eyebrow": Message("Що зараз відбувається", "What is happening now"),
    "work.lead": Message(
        "Що робиться просто зараз, скільки це вже коштувало, і що саме зупинить "
        "зупинка. Якщо чогось не знаємо — так і написано.",
        "What is being done right now, what it has cost so far, and what a stop "
        "would actually stop. Where something is unknown, it says so.",
    ),
    "work.runs.label": Message("Запуск", "Run"),
    "work.runs.empty": Message(
        "Немає незавершених запусків.", "No run is in flight.",
    ),
    "work.stage": Message("Етап {index} з {total}", "Stage {index} of {total}"),
    "work.liveness.alive": Message("Працює", "Running"),
    "work.liveness.quiet": Message("Давно не звітувала", "Quiet for a while"),
    "work.liveness.stalled": Message("Не подає ознак життя", "Not reporting"),
    "work.liveness.waiting": Message("Нічого не виконується", "Nothing running"),
    "work.heartbeat": Message(
        "Останній сигнал: {age} с тому", "Last sign of life: {age}s ago",
    ),
    "work.heartbeat.never": Message(
        "Сигналу від виконавця ще не було.", "No sign of life from a worker yet.",
    ),
    "work.blockers.title": Message("Що заважає", "What is in the way"),
    "work.blockers.none": Message("Нічого не заважає.", "Nothing is in the way."),
    "work.next": Message("Що робити далі", "What to do next"),
    "work.estimate.title": Message("Скільки лишилось", "Time remaining"),
    "work.estimate.value": Message("Приблизно {minutes} хв", "About {minutes} min"),
    "work.spend.title": Message("Витрати", "Spending"),
    "work.spend.spent": Message("Витрачено", "Spent"),
    "work.spend.reserved": Message("Зарезервовано", "Reserved"),
    "work.spend.cap": Message("Межа", "Cap"),
    "work.spend.remaining": Message("Лишилось", "Left"),
    "work.spend.nocap": Message("Межу не встановлено", "No cap is set"),
    "work.stop.title": Message("Що зробить зупинка", "What a stop would do"),
    "work.stop.preview": Message(
        "Це попередній перегляд. Поки ви не натиснете «Зупинити», нічого не "
        "зупиняється.",
        "This is a preview. Nothing stops until you press Stop.",
    ),
    "work.stop.now": Message("Зупиниться одразу", "Stops immediately"),
    "work.stop.anyway": Message("Все одно доробить", "Finishes anyway"),
    "work.stop.nothing": Message("Зараз нічого не виконується.", "Nothing is running."),
    "work.stop.wait": Message(
        "Найдовше очікування: приблизно {seconds} с",
        "Longest wait: about {seconds}s",
    ),
    "work.stop.button": Message("Зупинити запуск", "Stop this run"),
    "work.stop.confirm": Message(
        "Розумію, що вже надіслані виклики доробляться і будуть оплачені",
        "I understand that calls already sent will finish and be paid for",
    ),
    "work.stop.reason": Message("Причина зупинки", "Reason for stopping"),
    "work.stop.done": Message("Запуск зупинено.", "The run was stopped."),
    "work.pause.button": Message("Призупинити", "Pause"),
    "work.resume.button": Message("Продовжити", "Resume"),
    "work.pause.unavailable": Message(
        "Призупинення доступне лише для запусків під керуванням Temporal. Цей "
        "запуск ним не керується, тож кнопка нічого не зробить.",
        "Pausing exists only for runs orchestrated by Temporal. This run is not, "
        "so the button would do nothing.",
    ),
    "work.restart.title": Message("Після перезапуску", "After a restart"),
    "work.restart.check": Message("Треба перевірити", "Needs checking"),
    "work.restart.preserved": Message("Збережено", "Kept"),
    "work.restart.empty": Message("Порожньо.", "Nothing here."),
    "work.error": Message(
        "Не вдалося прочитати стан: {message}",
        "The state could not be read: {message}",
    ),

    # AI-studio screen
    "studio.title": Message("Студія", "The studio"),
    "studio.eyebrow": Message("Ваша гра", "Your game"),
    "studio.lead": Message(
        "Що студія робить зараз, що вже можна зіграти, і що вирішуєте ви.",
        "What the studio is doing now, what you can already play, and what is "
        "yours to decide.",
    ),
    "studio.mission.label": Message("Гра", "Game"),
    "studio.mission.none": Message(
        "Тут ще немає жодної гри.", "There is no game here yet.",
    ),
    "studio.start.blocked": Message(
        "Розробка не почнеться, поки немає перевіреного джерела виконання.",
        "Development will not start until there is a checked source of execution.",
    ),
    "studio.plan.title": Message("План роботи", "The plan of work"),
    "studio.plan.next": Message("Далі", "Next"),
    "studio.plan.empty": Message("Плану ще немає.", "There is no plan yet."),
    "studio.play": Message("Грати", "Play"),
    "studio.questions.title": Message("Що вирішуєте ви", "Yours to decide"),
    "studio.questions.none": Message(
        "Зараз нічого вирішувати не треба.", "Nothing needs deciding right now.",
    ),
    "studio.money.title": Message("Гроші", "Money"),
    "studio.money.spent": Message("Витрачено", "Spent"),
    "studio.money.reserved": Message("Зарезервовано", "Reserved"),
    "studio.money.limit": Message("Ліміт", "Limit"),
    "studio.money.forecast": Message("Прогноз до кінця етапу", "Forecast for the stage"),
    "studio.team.title": Message("Хто працює", "Who is working"),
    "studio.team.add": Message("Додати роль", "Add a role"),
    "studio.team.off": Message("Вимкнена", "Off"),
    "studio.team.enable": Message("Увімкнути", "Turn on"),
    "studio.team.disable": Message("Вимкнути", "Turn off"),
    "studio.team.confirm": Message("Увімкнути попри це", "Turn it on anyway"),
    "studio.team.cost.unknown": Message(
        "Скільки це додасть — невідомо.", "How much this adds is unknown.",
    ),
    "studio.team.cost": Message(
        "Приблизно +{amount} {unit} за етап.",
        "About +{amount} {unit} per stage.",
    ),
    "studio.team.acceptance.changes": Message(
        "Після цього розробник більше не прийматиме власну роботу.",
        "After this the developer will no longer accept their own work.",
    ),
    "studio.loop.title": Message("Пауза й правки", "Pause and edits"),
    "studio.loop.pause": Message("Пауза", "Pause"),
    "studio.loop.resume": Message("Продовжити", "Continue"),
    "studio.loop.comment": Message("Що змінити", "What to change"),
    "studio.loop.send": Message("Надіслати", "Send"),
    "studio.loop.who": Message("Ваше імʼя", "Your name"),
    "studio.loop.paused": Message("На паузі", "Paused"),
    "studio.loop.running": Message("Працює", "Running"),
    "studio.loop.cycle": Message("Цикл {number}", "Cycle {number}"),
    "studio.run.title": Message("Хто веде роботу", "Who is driving the work"),
    "studio.create.title": Message("Почати нову гру", "Start a new game"),
    "studio.create.name": Message("Назва гри", "Game title"),
    "studio.create.idea": Message("Опишіть гру", "Describe your game"),
    "studio.create.scope": Message(
        "Локальна модель складе план. Автоматична розробка й збірка ще не підключені.",
        "The local model will prepare a plan. Automatic development and builds are not connected yet."),
    "studio.create.consent": Message(
        "Кнопка запускає планування локально з нульовим лімітом платних запитів.",
        "This button starts local planning with a zero budget for paid requests."),
    "studio.create.start": Message("Почати планування", "Start planning"),
    "studio.create.setup": Message("Підключення провайдерів", "Provider connections"),
    "studio.create.notready": Message("Спочатку перевірте локальний воркер і модель Ollama.",
                                       "Qualify the local worker and Ollama model first."),
    "studio.library.title": Message("Мої ігри", "My games"),
    "studio.library.help": Message("Ідеї, результати й поточне планування зберігаються тут. Вихід зі сторінки не зупиняє роботу.", "Ideas, results and planning are saved here. Leaving this page does not stop work."),
    "studio.library.empty": Message("Ще немає ігор. Почніть із назви та ідеї нижче.", "No games yet. Start with a name and idea below."),
    "studio.progress.stop": Message("Зупинити планування", "Stop planning"),
    "studio.recovery.title": Message("Продовжимо вашу гру", "Continue your game"),
    "studio.recovery.start": Message("Виправити й продовжити", "Repair and continue"),
    "studio.recovery.connect": Message("Перевірити локальний AI", "Check local AI"),
    "studio.recovery.scope": Message("Збережемо {kept} перевірених етапів. Передамо моделі {model} помилки та продовжимо з місця зупинки: не більше 2 спроб на етап, платні API — $0. Успіх не гарантований; у разі повторної помилки зупинимось і збережемо результат.", "Keep {kept} validated stages. Give {model} the errors and continue from the stopping point: at most 2 attempts per stage, paid APIs $0. Success is not guaranteed; another failure stops work and preserves the result."),
    "studio.recovery.checking": Message("Перевіряємо модель і збережений результат…", "Checking the model and saved results…"),
    "studio.recovery.accepted": Message("Продовження поставлено в чергу. Хід роботи з’явиться вище.", "Continuation queued. Progress will appear above."),
    "studio.recovery.recovery_busy": Message("Локальний воркер ще зайнятий. Дочекайтеся завершення або зупиніть активне планування й повторіть. Друга копія не запущена.", "The local worker is still busy. Wait or stop active planning, then retry. No second copy was started."),
    "studio.recovery.recovery_changed": Message("Стан гри змінився. Оновіть сторінку, перегляньте результат і повторіть.", "The game changed. Refresh, review its state and retry."),
    "studio.recovery.recovery_phase": Message("Гра вже перейшла до іншого кроку або призупинена. Оновіть сторінку й перегляньте її стан.", "The game moved to another step or is paused. Refresh and review its state."),
    "studio.recovery.recovery_model_changed": Message("Підключена модель відрізняється від моделі цієї гри. Поверніть попередню модель; автоматична заміна не виконується.", "The connected model differs from this game’s model. Restore the previous model; it is not changed automatically."),
    "studio.recovery.recovery_source_changed": Message("Ідея змінилася після цієї спроби. Старі результати не можна безпечно використати; почніть новий план з оновленої ідеї.", "The idea changed after this attempt. Old results cannot safely be reused; start a new plan from the updated idea."),
    "studio.progress.idea": Message("Початкова ідея", "Original idea"),
    "studio.progress.reason": Message("Чому зупинилося", "Why it stopped"),
    "studio.progress.access": Message("Увійдіть як власник локальної студії, щоб побачити свої ігри.", "Sign in as the local studio owner to see your games."),
    "studio.progress.results": Message("Збережені результати", "Saved results"),
    "studio.progress.none": Message("Ще немає перевірених результатів. Незавершений або помилковий план не видається за готовий.", "No validated results yet. An incomplete or invalid plan is not presented as ready."),
    "studio.progress.model": Message("Модель планування: {model} · {provider}", "Planning model: {model} · {provider}"),
    "studio.progress.local": Message("На вашому ПК · платні API-запити заборонені · ліміт $0. Лічильник локальних токенів недоступний.", "On your PC · paid API calls disabled · $0 limit. Local token counts are unavailable."),
    "studio.progress.cost_unknown": Message("Витрати й токени: немає підтверджених даних.", "Costs and tokens: no confirmed data available."),
    "studio.progress.count": Message("Перевірено етапів: {done} із {total}. Це завершені результати, не відсоток часу.", "Validated steps: {done} of {total}. These are completed results, not a time estimate."),
    "studio.progress.updated": Message("Остання подія: {time}", "Last event: {time}"),
    "studio.progress.running": Message("Робота триває у фоні. Можна перейти на іншу сторінку й повернутися. Поточна відповідь з’явиться після завершення запиту.", "Work continues in the background. You can leave and return. The current response appears after the request completes."),
    "studio.progress.failed": Message("Зупинилося на етапі «{role}». Нові запити автоматично не запускаються. Перевірені проміжні результати доступні нижче.", "Stopped at “{role}”. No new requests start automatically. Validated intermediate results are available below."),
    "studio.progress.interrupted": Message("Сервер не підтверджує активне виконання. Ця сторінка нічого не перезапускає. Попередні результати збережені.", "The server cannot confirm active execution. This page does not restart work. Previous results are saved."),
    "studio.progress.stopped": Message("Планування зупинене. Нові запити за цим дорученням заборонені; попередні результати збережені.", "Planning stopped. New requests under this mandate are disabled; previous results are saved."),
    "studio.progress.stopping": Message("Запит на зупинку прийнято. Чекаємо підтвердження завершення поточного процесу; нові запити заборонені.", "Stop requested. Waiting for the current process to exit; new requests are disabled."),
    "studio.progress.attempt": Message("Спроба {attempt} із {max}", "Attempt {attempt} of {max}"),
    "studio.progress.done": Message("Готово", "Done"),
    "studio.progress.pending": Message("Очікує", "Pending"),
    "studio.progress.invalid": Message("Не пройшло перевірку", "Validation failed"),
    "studio.create.selected": Message("Плануватиме {model} через Ollama на вашому ПК. Codex і Gemini для цього запуску не використовуються.", "{model} via Ollama on your PC will plan this game. Codex and Gemini are not used for this launch."),
    "studio.create.queued": Message("Планування поставлено в чергу. Хід роботи оновлюється автоматично.",
                                     "Planning is queued. Progress updates automatically."),
    "studio.run.nobody": Message(
        "Ніхто не доручив студії вести цю гру самостійно.",
        "Nobody has asked the studio to run this game on its own.",
    ),
    "studio.run.may": Message("Дозволено", "Allowed"),
    "studio.run.ceiling": Message("Стеля витрат", "Spending ceiling"),
    "studio.run.until": Message("Діє до", "In force until"),
    "studio.run.grant": Message("Доручити студії", "Let the studio run it"),
    "studio.run.confirm": Message("Так, доручити", "Yes, let it run"),
    "studio.run.revoke": Message("Відкликати доручення", "Take the mandate back"),
    "studio.run.consequence": Message(
        "Доручення дозволяє планування цієї гри від вашого імені. Цей контроль "
        "записує дозвіл; запуск потребує локальної черги або CLI. Ліміт витрат "
        "і пауза залишаються чинними.",
        "This mandate permits planning in your name. This control records "
        "permission; dispatch requires the local queue or CLI. Spending limits "
        "and pause remain in force.",
    ),
    "studio.run.steps": Message("Що вона вже зробила", "What it has already done"),
    "studio.run.nothing": Message(
        "Поки що вона тут нічого не робила.", "It has not done anything here yet.",
    ),
    "studio.machines.title": Message("Де це виконується", "Where this runs"),
    "studio.machines.nobuild": Message(
        "тут ігри не збираються", "games are not built here",
    ),
    "studio.loop.need-name": Message(
        "Вкажіть імʼя, щоб цю дію було на кого записати.",
        "Enter a name, so this can be recorded against someone.",
    ),
    "studio.error": Message(
        "Не вдалося прочитати стан: {message}",
        "The state could not be read: {message}",
    ),

    # Settings errors, phrased as cause and action
    "error.actor_required": Message(
        "Зміна налаштування записується на конкретну людину. Вкажіть імʼя.",
        "A setting is recorded against a named person. Enter a name.",
    ),
    "error.not_reconfigurable": Message(
        "{label} походить із {source} і змінюється там, а не тут.",
        "{label} comes from {source} and is changed there, not here.",
    ),
    "error.confirmation_required": Message(
        "{label}: {consequence} Підтвердьте наслідок, щоб застосувати зміну.",
        "{label}: {consequence} Acknowledge the consequence to apply the change.",
    ),
    "error.unknown_setting": Message(
        "Такого налаштування немає: {key}.", "No such setting: {key}."
    ),
    "error.out_of_range": Message(
        "{label}: {value} поза межами {minimum}–{maximum}. Виберіть значення в межах.",
        "{label}: {value} is outside {minimum}–{maximum}. Choose a value in range.",
    ),
}


def translate(key: str, language: str = DEFAULT_LANGUAGE, **parameters: Any) -> str:
    try:
        message = CATALOGUE[key]
    except KeyError:
        raise KeyError(f"Unknown message key: {key}") from None
    return message.text(language, **parameters)


def bundle(language: str = DEFAULT_LANGUAGE, *, prefix: str = "") -> dict[str, str]:
    """Every message a static page needs, in one language."""
    chosen = normalise(language)
    return {
        key: message.text(chosen)
        for key, message in CATALOGUE.items()
        if not prefix or key.startswith(prefix)
    }


def missing_translations(messages: Iterable[Any]) -> tuple[str, ...]:
    """Names of messages that do not carry every supported language."""
    gaps: list[str] = []
    for index, message in enumerate(messages):
        for language in LANGUAGES:
            value = getattr(message, language, "")
            if not str(value).strip():
                gaps.append(f"{index}:{language}")
    return tuple(gaps)
