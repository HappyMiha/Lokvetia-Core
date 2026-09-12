# Беклог Lokvetia Core: від ідеї до власної гри, 12+

Дата: **5 вересня 2026**. Це активний продуктовий план; [аудит поточного стану](product-audit-2026-09-05.uk.md) пояснює підстави. Машинне джерело — [game-creator-backlog.json](../examples/game-creator-backlog.json), schema v2. Усі нові задачі **запропоновані**, жодна не оголошена реалізованою.

## Наступний напрям: режим «AI-студія»

Постановка [«AI-студія»](ai-studio.uk.md) прийнята як напрям розвитку цього ж плану.
Її епіки й задачі живуть у тому самому маніфесті з префіксом `AF-ST`, milestone `m4`,
і приймаються задачею `AF-ST-010`. Вони не скасовують жодну задачу `AF-GC`: вони
прибирають ручні ворота там, де ці задачі вже перевірені.

## Мета й межі

Автор описує гру, вказує наявні хмарні/локальні AI, проходить зрозуміле підключення, отримує придатний для свого ПК план. Система встановлює затверджені компоненти, готує середовище, розробляє гру невеликими перевіреними кроками. Автор періодично натискає «Грати», дає відгук і може повернути попередню версію.

«Майже будь-яка складність» — напрям розвитку, а не критерій першого релізу. Перший доказ: **Windows, Godot, GDScript, невелика 2D гра**, кваліфікований cloud worker і незалежний reviewer. Далі — local-only/hybrid та Unity, 3D й більші проєкти. Інші ОС і рушії отримують підтримку лише після кваліфікації; це не обіцянка їх недоступності назавжди.

Для 12-річного автора зовнішній account flow має відповідати правилам конкретного провайдера. Дорослий налаштовує доступ/витрати там, де це потрібно; local/offline шлях залишається окремим результатом M2. Не обіцяємо, що батьківський дозвіл автоматично дозволяє будь-який сторонній сервіс.

Ядро лишається project-neutral: ігрові templates, engine commands та acceptance contracts живуть у game packs. Наявні policy, audit, worktree, memory та recovery механізми використовуємо повторно.

## Шлях користувача

1. **Моя ідея:** опис → короткі уточнення → керування, мета та перший playable.
2. **Мої AI:** доступні продукти → офіційний login/ключ → перевірка реальної моделі й можливостей.
3. **Мій ПК:** read-only inventory → рекомендація → точний план встановлення, диск, витрати та необхідні особисті дії.
4. **Створення:** автоматична підготовка → реальні зміни → engine checks → незалежний review → робоча версія.
5. **Грати й змінювати:** Play конкретного build → feedback → наступна версія; Pause, Stop і відновлення завжди доступні.

Технічні IDs, лізи, JSON, routing та журнал доступні в деталях. Основний екран показує результат, прогрес і одну наступну дію.

## Черговість та приймання

| Етап | Результат | Gate |
|---|---|---|
| M0 | Правдиві статуси, справні базові взаємодії, сумісні live roles, відтворюваний CI | Усі задачі M0 прийняті |
| M1 | Перша Godot-гра, Play → feedback → v2 на чистому ПК | AF-GC-026, включно з дослідженням AF-GC-040 |
| M2 | Local-only та hybrid, з урахуванням RAM/VRAM і гри | AF-GC-031 |
| M3 | Unity, assets/export, перевірені складніші зразки й оновлення | AF-GC-034, AF-GC-036–038 |

M0 не забороняє паралельну роботу над незалежними UI/setup задачами M1. Детальні dependencies нижче визначають початок задачі; release gate визначає приймання всього етапу. Для першої гри не потрібні всі можливості старого AMM беклогу.

**Найближча ітерація:** 001, 002, 003, 005, 006, 039; паралельно 004. Після 006 — 041/042. На демонстрації мають бути фактичні результати CI, відсутність хибного READY та збережений опис/чернетка. Потім 007/008/011/025 — спільний прохід idea/setup без реальних витрат. Пріоритет не замінює залежність.

**P0** — блокує достовірність, керованість або авторизацію; **P1** — потрібне для M1/M2; **P2** — розширення M3. **S/M/L** — попередні відносні оцінки до 2/5/10 інженерних днів, без календарних обіцянок. L перед початком ділиться на менші PR. assigned_role визначає відповідальну спеціальність, а не дозвіл автоматично запускати такого агента.

У задачі одна відповідальна спеціальність, незалежний reviewer і власник приймання. Робочі стани трекера: Proposed → Ready (dependencies + scope + estimate) → In progress → In review → Accepted; Blocked містить причину й наступну дію. Невдалий тест не дозволяє Accepted. Не більше однієї активної задачі на worker worktree; WIP коригуємо за виміряними ресурсами.

## Список задач

| ID | Пріоритет | Етап | Розмір | Результат | Залежить від |
|---|---|---|---|---|---|
| AF-GC-001 | P0 | M0 | M | Відновити відтворюваний CI на трьох ОС | — |
| AF-GC-002 | P0 | M0 | M | Показувати готовність лише після реальних перевірок | — |
| AF-GC-003 | P0 | M0 | S | Закриття діалогу ніколи не підтверджує дію | — |
| AF-GC-004 | P1 | M0 | S | Зберігати чернетки та фокус під час автооновлення | — |
| AF-GC-005 | P0 | M0 | M | Не втрачати зміст звичайного опису гри | — |
| AF-GC-006 | P0 | M0 | M | Прив’язати вибрану модель до фактичного запуску | — |
| AF-GC-007 | P1 | M1 | M | Додати головний екран «Мої ігри» і покроковий старт | AF-GC-003, AF-GC-004 |
| AF-GC-008 | P1 | M1 | M | Перетворити ідею на зрозумілий план першої гри | AF-GC-005, AF-GC-007 |
| AF-GC-009 | P1 | M1 | L | Підключати хмарний AI через зрозумілий майстер | AF-GC-006, AF-GC-007, AF-GC-010, AF-GC-025 |
| AF-GC-010 | P1 | M1 | M | Зберігати й відкликати доступ до AI без витоку ключів | AF-GC-039 |
| AF-GC-011 | P1 | M1 | M | Перевіряти можливості ПК до вибору моделі та рушія | AF-GC-007 |
| AF-GC-012 | P1 | M1 | M | Рекомендувати реалістичний рушій і конфігурацію AI | AF-GC-008, AF-GC-011 |
| AF-GC-013 | P1 | M1 | M | Показувати точний план встановлення з перевірених джерел | AF-GC-002, AF-GC-012 |
| AF-GC-014 | P1 | M1 | L | Виконувати та відновлювати встановлення програм | AF-GC-013 |
| AF-GC-015 | P1 | M1 | L | Запускати локальну інфраструктуру однією дією | AF-GC-014 |
| AF-GC-016 | P1 | M1 | M | Створити Godot pack для першої 2D гри | AF-GC-008, AF-GC-014 |
| AF-GC-017 | P1 | M1 | M | Перевіряти Godot-проєкт і створювати реальний build | AF-GC-016 |
| AF-GC-018 | P1 | M1 | M | Дозволяти обмежену cloud-сесію з прозорим бюджетом | AF-GC-009, AF-GC-010, AF-GC-025 |
| AF-GC-019 | P1 | M1 | L | Підключити справжню розробку до ігрового плану | AF-GC-006, AF-GC-017, AF-GC-018, AF-GC-041, AF-GC-042 |
| AF-GC-020 | P1 | M1 | M | Зберігати останню перевірену ігрову версію | AF-GC-019 |
| AF-GC-021 | P1 | M1 | M | Запускати «Грати» для конкретної робочої версії | AF-GC-007, AF-GC-020 |
| AF-GC-022 | P1 | M1 | M | Перетворювати відгук після гри на наступну версію | AF-GC-008, AF-GC-021 |
| AF-GC-023 | P1 | M1 | M | Пояснювати прогрес і надійно зупиняти роботу | AF-GC-019, AF-GC-020 |
| AF-GC-024 | P1 | M1 | M | Зробити основний шлях доступним українською та англійською | AF-GC-007, AF-GC-021, AF-GC-022, AF-GC-023 |
| AF-GC-025 | P1 | M1 | M | Визначити доступний шлях для 12+ та участь дорослого | AF-GC-007 |
| AF-GC-026 | P1 | M1 | M | Прийняти повний Godot-шлях на чистому ПК | AF-GC-001, AF-GC-002, AF-GC-003, AF-GC-004, AF-GC-005, AF-GC-006, AF-GC-009, AF-GC-015, AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-025, AF-GC-039, AF-GC-040 |
| AF-GC-027 | P1 | M2 | M | Встановлювати та перевіряти локальні моделі | AF-GC-006, AF-GC-011, AF-GC-013, AF-GC-014 |
| AF-GC-028 | P1 | M2 | L | Дати локальному AI кваліфікований інструмент розробки | AF-GC-017, AF-GC-027, AF-GC-041, AF-GC-042 |
| AF-GC-029 | P1 | M2 | M | Ділити ресурси ПК між AI, рушієм та грою | AF-GC-011, AF-GC-021, AF-GC-027 |
| AF-GC-030 | P1 | M2 | M | Маршрутизувати cloud/local за явними правилами | AF-GC-018, AF-GC-028, AF-GC-029 |
| AF-GC-031 | P1 | M2 | M | Кваліфікувати local-only та hybrid створення гри | AF-GC-026, AF-GC-028, AF-GC-029, AF-GC-030 |
| AF-GC-032 | P2 | M3 | M | Підключати Unity Hub, Editor і потрібні модулі | AF-GC-013, AF-GC-014, AF-GC-025, AF-GC-026 |
| AF-GC-033 | P2 | M3 | L | Додати Unity pack, тести та build adapter | AF-GC-017, AF-GC-019, AF-GC-032 |
| AF-GC-034 | P2 | M3 | M | Прийняти повний Unity-шлях для новачка | AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-033 |
| AF-GC-035 | P2 | M3 | M | Керувати походженням та імпортом ігрових ресурсів | AF-GC-016, AF-GC-020, AF-GC-026 |
| AF-GC-036 | P2 | M3 | L | Розширювати складність через вимірювані зразки ігор | AF-GC-031, AF-GC-034, AF-GC-035 |
| AF-GC-037 | P2 | M3 | M | Експортувати гру та ділитися нею окремою дією | AF-GC-020, AF-GC-025, AF-GC-035 |
| AF-GC-038 | P2 | M3 | M | Оновлювати застосунок і збирати зрозумілу діагностику | AF-GC-015, AF-GC-023, AF-GC-026 |
| AF-GC-039 | P0 | M0 | M | Узгодити авторизацію локального API з обіцяною політикою | — |
| AF-GC-040 | P1 | M1 | M | Перевірити зрозумілість із користувачами 12–15 років | AF-GC-009, AF-GC-015, AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-025 |
| AF-GC-041 | P0 | M0 | M | Зберігати ролі та незалежність reviewer у live mission | AF-GC-006 |
| AF-GC-042 | P0 | M0 | M | Кваліфікувати ролі planning та bootstrap для провайдерів | AF-GC-006 |
| AF-GC-043 | P0 | M2 | M | Atomically admit qualified workers with scoped attempts and shared capacity | AF-GC-039 |

## Як читати й виконувати задачі

Нижче наведено намір і конкретне приймання. У JSON додатково збережені компоненти, способи перевірки, очікувані артефакти, Definition of Done та legacy mapping. UI, API і recovery відповідного сценарію входять до його задачі; їх не відкладаємо до фінального «намалювати інтерфейс».

### AF-GC-001 — Відновити відтворюваний CI на трьох ОС

Чистий checkout має давати надійний результат перевірок незалежно від установлених AI CLI на машині.

- Тести monitor не залежать від персональних CLI та окремо перевіряють потрібні/непотрібні провайдери.
- Перевірка завершення subprocess використовує відповідний ОС механізм; Linux/macOS не викликають tasklist.exe.
- Матриця Python 3.11/3.12 Windows/Linux/macOS, wheel smoke та Docker CI зелені або мають явне обґрунтоване обмеження без прихованого skip.

**Перевірка:** Запустити на чистих CI runners; зберегти повні підсумки, skipped reasons, wheel/demo та cancellation evidence.

### AF-GC-002 — Показувати готовність лише після реальних перевірок

Повідомлення «Можна створювати гру» повинно означати готовність обраного шляху, а не лише перехід статусу в базі.

- DEVELOPMENT не починається без актуального звіту required tools/services/model/workspace для затвердженого плану.
- Відсутня програма, модель чи невдалий probe дає конкретну дію виправлення; повторний запуск перевіряє фактичний стан.
- UI розрізняє installed/authenticated/qualified/ready та simulated/live; необраний провайдер не блокує справний маршрут.

**Перевірка:** Regression: порожній workspace без рушія не може повернути environment READY; success/missing/stale/failed-probe cases.

### AF-GC-003 — Закриття діалогу ніколи не підтверджує дію

Усунути ризик успадкування попереднього підтвердження при повторному відкритті native dialog.

- Перед кожним відкриттям результат діалогу скинутий; лише явна кнопка підтверджує конкретний поточний запит.
- Confirm → новий діалог → Escape/Cancel не надсилає mutation request.
- Подвійний клік та повторне відкриття не дублюють операцію; фокус повертається до ініціатора.

**Перевірка:** Browser regression на disposable fixture з перехопленими запитами; записати нуль mutations при Escape/Cancel.

### AF-GC-004 — Зберігати чернетки та фокус під час автооновлення

Редагування моделі або провайдера не повинно зникати кожні п’ять секунд.

- Model/provider drafts, selection і focus зберігаються щонайменше три refresh cycles.
- Серверна зміна під час редагування показує конфлікт і дозволяє вибрати версію.
- Оновлення статусів продовжується без повної заміни форми; Save та Cancel мають явний результат.

**Перевірка:** Browser: ввести незбережену модель, дочекатися трьох циклів, перевірити value/focus та save/cancel.

### AF-GC-005 — Не втрачати зміст звичайного опису гри

Імпорт абзацу без Markdown має зберігати намір користувача; евристичний імпорт не називати виконаним AI-аналізом.

- Український/англійський абзац збережений як оригінальне джерело, незалежно від результату парсера.
- Для «кіт збирає монети, три життя» preview містить ці вимоги або ставить уточнення; порожній filename-epic не вважається готовим планом.
- UI чесно позначає deterministic import, AI proposal і підтверджений план; користувач редагує preview до імпорту.

**Перевірка:** Text/PDF/Markdown import cases з семантичними assertions на вимоги, executable leaves і source trace.

### AF-GC-006 — Прив’язати вибрану модель до фактичного запуску

Вибір моделі має змінювати реальний provider request та зберігати правдиву ідентичність для незалежної перевірки.

- Дві дозволені моделі одного провайдера породжують відповідні різні qualified request/argv; довільні shell аргументи заборонені.
- Непідтримувана чи невідома модель відхиляється до запуску; журнал фіксує requested і effective model без секретів.
- Зміна моделі інвалідує залежну кваліфікацію/дозвіл; reviewer independence перевіряє effective identity.

**Перевірка:** Adapter fixture порівнює requests для model-a/model-b; live canary лише на явно дозволеному профілі та бюджеті.

### AF-GC-007 — Додати головний екран «Мої ігри» і покроковий старт

Новачок починає з ідеї та бачить лише поточний крок: Ідея → AI → Підготовка → Створення → Грати.

- Порожній стан має одну основну дію «Створити гру»; task IDs, roles, leases і JSON сховані у деталях.
- Кроки, введені дані та помилки збережені після перезапуску; можна повернутися до попереднього кроку.
- Для чинного проєкту видно останню робочу версію, поточний прогрес і наступну необхідну дію.
- Пошук і пагінація дають доступ до понад 200 задач/версій без ручного API/JSON; основні фільтри мають списки допустимих значень.

**Перевірка:** Browser journeys: чистий стан, перерваний wizard, повернення до наявної гри; API contract tests.

### AF-GC-008 — Перетворити ідею на зрозумілий план першої гри

Система зберігає амбіцію великої гри, але погоджує маленький перший playable milestone та наступні кроки.

- Brief містить жанр, керування, мету/програш, платформу, стиль, перший playable і відкладені можливості.
- Уточнення конкретні й дозовані; припущення, витрати та межі результату можна виправити перед стартом.
- Редагована чернетка має виконувані задачі й game-specific критерії з source trace; живе cloud-планування стає доступним після 018, до цього UI не оголошує AI-план прийнятим.

**Перевірка:** Fixtures: platformer, top-down collector, puzzle, надмірний multiplayer запит; незалежний review brief і плану.

### AF-GC-009 — Підключати хмарний AI через зрозумілий майстер

Користувач називає наявні продукти; майстер пояснює, яке саме підключення вони дають: API, підтримуваний CLI або відсутність інтеграції.

- Версійований каталог показує офіційний login/device/API-key flow; підписка на чат не автоматично означає API доступ чи кредит.
- Перший реліз кваліфікує щонайменше один cloud coding route і незалежний review route; інші показані як непідтримувані/потребують налаштування.
- Login проходить у провайдера; після повернення connection check розрізняє auth, quota, model access, capability й мережеві помилки.

**Перевірка:** Disposable account/test keys: missing, expired, denied model, quota, offline, successful bounded canary; не обходити умови сервісу.

### AF-GC-010 — Зберігати й відкликати доступ до AI без витоку ключів

Секрет вводиться у спеціальному кроці, зберігається через ОС і не потрапляє у prompt, backlog чи support bundle.

- OS credential-store reference переживає перезапуск; значення інжектується лише в дозволений процес/запит.
- Disconnect/revoke припиняє нові виклики та пояснює, як відкликати доступ у провайдера.
- Маскування перевірене у помилках, логах, exports, crash reports та скриншотних інструкціях; секрет не повертається browser API.

**Перевірка:** Synthetic secret canary search через усі evidence/log/export sinks; revoke та restart tests на підтримуваній ОС.

### AF-GC-011 — Перевіряти можливості ПК до вибору моделі та рушія

Read-only перевірка до затвердження плану збирає лише потрібні технічні характеристики та пояснює невідомі значення.

- Звіт містить ОС/архітектуру, CPU, доступну/загальну RAM, GPU/VRAM або shared memory, вільний диск, runtimes/engines та час перевірки.
- Unknown/unsupported не перетворюється на нуль чи вигадану характеристику; збій GPU probe не блокує cloud-only шлях.
- UI показує, що залишиться локально; серійні номери, особисті файли та список сторонніх процесів не потрібні.

**Перевірка:** Windows VM/host matrix: CPU-only, integrated GPU, discrete GPU, low disk; fixtures плюс фактичний inventory report.

### AF-GC-012 — Рекомендувати реалістичний рушій і конфігурацію AI

Показати причину рекомендації та варіанти для слабкого ПК, без обіцянки, що будь-яка модель створить будь-яку гру.

- Порівняння Godot/local/cloud враховує підтримку ОС, пам’ять, диск, renderer і цільовий build; Unity до кваліфікації явно позначена наступним етапом.
- Оцінки завантаження/витрат/часу мають джерело, дату й діапазон невизначеності; невідомий VRAM дає обережну рекомендацію.
- Користувач може обрати доступну альтернативу; обґрунтоване обмеження першого milestone не змінює ідею мовчки.

**Перевірка:** Decision-table tests на профілях слабкого/середнього ПК; review поточних вимог engine/model catalog.

### AF-GC-013 — Показувати точний план встановлення з перевірених джерел

Перед автоматичним налаштуванням користувач бачить що, звідки, навіщо, куди та скільки буде встановлено.

- Каталог фіксує версію, офіційне джерело, checksum/signature, залежності, ліцензійний крок, диск і scope змін.
- Plan розрізняє вже встановлене, reusable, install, update, manual action; не оновлює сторонній софт без потреби.
- Затвердження прив’язане до digest плану; зміни джерела/прав/обсягу потребують нового рішення, а не довільного shell script.

**Перевірка:** Plan diff, підміна checksum/source, dependency conflict, no-admin та offline fixtures.

### AF-GC-014 — Виконувати та відновлювати встановлення програм

Lokvetia Core сам виконує затверджені установки; людина підключається лише там, де потрібна особиста дія.

- Завантаження/перевірка/встановлення/postcondition журналюються; повтор після обриву не дублює успішні операції.
- UI веде через потрібний UAC/login/EULA крок і відновлюється після нього; не приймає договір від імені користувача.
- Недостатній диск, зіпсований пакет, зайнятий порт та перезавантаження ОС мають retry/repair/rollback без видалення чужих файлів.

**Перевірка:** Чиста Windows VM: install Godot та потрібних runtime; kill/network loss/disk full tests у disposable VM зі звітом фактичних postconditions.

### AF-GC-015 — Запускати локальну інфраструктуру однією дією

Користувач відкриває застосунок без трьох PowerShell-вікон і ручного налаштування бази, сервера та worker.

- Пакетований launcher встановлює/піднімає лише потрібні сервіси, перевіряє readiness та відкриває wizard.
- Якщо обраний шлях потребує Temporal/Docker, setup перевіряє вимоги й запускає їх; відсутня залежність має зрозумілий guided recovery.
- Закриття/оновлення/перезапуск не губить гри, не лишає неконтрольованих процесів і не відкриває сервіс у мережу.

**Перевірка:** Fresh Windows non-admin start, reboot/resume, port conflict, service crash, double-launch; перелік підтримуваних конфігурацій.

### AF-GC-016 — Створити Godot pack для першої 2D гри

Ігрові шаблони та правила розмістити в окремому pack, зберігаючи project-neutral ядро фабрики.

- Pack фіксує підтримувану Godot версію, GDScript, структуру scenes/assets/scripts та renderer для baseline ПК.
- Два мінімальні шаблони collector/platformer мають керування, win/lose і restart без зовнішнього контенту.
- Проєкт відкривається у справжньому editor; імпорт/upgrade pack не перезаписує авторські файли без preview.

**Перевірка:** Створення двох нових projects; actual Godot import/open; pack compatibility і asset license checks.

### AF-GC-017 — Перевіряти Godot-проєкт і створювати реальний build

Позитивний текстовий verdict не замінює запуск рушія: потрібні import, script checks, runtime smoke та export.

- Qualified shell-free adapter перевіряє версію й підтримку flags; import/parse/runtime/export мають timeout, exit code та bounded log.
- Відсутня export template, syntax error, missing resource та crash дають failure; --check-only не видається за повний тест гри.
- Артефакт містить commit, engine/template version, preset, checksum; успішний headless smoke доповнений графічним запуском.

**Перевірка:** Godot CLI з офіційних docs: healthy/broken reference projects, export preset/template mismatch, graphical playtest.

### AF-GC-018 — Дозволяти обмежену cloud-сесію з прозорим бюджетом

Одна зрозуміла сесія дозволяє послідовну роботу в межах вибраного AI, даних, часу й бюджету.

- Окремий bounded CLOUD scope охоплює передстартове планування, coding та review; він не маскує remote provider як LOCAL і не потребує локальної моделі для M1.
- UI показує provider/model, дані для передачі, scope файлів, верхні caps витрат/часу/ітерацій; нові права/витрати вимагають окремого рішення.
- Перед викликом резервується бюджет з урахуванням паралелізму; quota/unknown cost/limit pause не запускають прихований paid fallback.

**Перевірка:** Concurrency budget, denied scope, expired auth, cancellation, uncertain charge reconciliation; capped real canary після налаштування.

### AF-GC-019 — Підключити справжню розробку до ігрового плану

Виконувані задачі мають змінювати гру через qualified worker, проходити перевірку й давати інтегровані результати.

- Dependency-ready задача виконується у leased worktree з потрібним контекстом; simulation явно відокремлена.
- Worker diff проходить game validators та незалежний review; bounded repair зупиняється при відсутності прогресу.
- Прийнятий diff інтегрується рівно один раз; child timeout/retry/restart не дублює commit або платний виклик без reconciliation.

**Перевірка:** Нова Godot гра: real worker → diff → import/test/build → незалежний review → accepted commit; failure/repair і replay evidence.

### AF-GC-020 — Зберігати останню перевірену ігрову версію

Нова незавершена робота не позбавляє користувача можливості пограти у попередню справну версію.

- Playable checkpoint містить commit, build digest, engine/assets/config і результати перевірок.
- Невдалий наступний build не змінює latest working pointer; promotion атомарний та replay-safe.
- Відновлення створює нову гілку/версію з preview; прийнята історія й оригінали збережені.

**Перевірка:** Build failure, interrupted promotion, restore to prior version, duplicate acceptance та source/build identity checks.

### AF-GC-021 — Запускати «Грати» для конкретної робочої версії

Велика кнопка «Грати» відкриває перевірений build і показує прості інструкції керування.

- UI показує version/build ID, короткі зміни та керування; Play запускає саме цей artifact.
- Процес має контрольований lifecycle, logs і Stop; падіння не закриває фабрику й не губить прогрес.
- Можна грати у попередню версію під час наступної розробки; якщо ресурсів недостатньо, AI призупиняється з поясненням.

**Перевірка:** Graphical Windows launch/stop/crash; latest working під час failed build; executable provenance/path checks.

### AF-GC-022 — Перетворювати відгук після гри на наступну версію

Користувач пише «зроби стрибок вищим», а система змінює перевірену гру зі збереженням попередньої.

- Feedback прив’язаний до played build; текст/дозволений screenshot і reproduction steps мають preview.
- Система показує короткий change plan та вплив на попередні вимоги; нові витрати чи scope не приймаються мовчки.
- Після розробки доступні новий build, перевірка саме запитаної поведінки та повернення до попереднього.

**Перевірка:** E2E: play v1 → вищий стрибок → build v2 → human playtest → restore v1; stale feedback/conflicting request cases.

### AF-GC-023 — Пояснювати прогрес і надійно зупиняти роботу

Користувач бачить що робиться, що вже можна спробувати та як продовжити після проблеми.

- UI показує людяний етап, останній heartbeat, blockers, spent/reserved budget і наступну дію; невідомий ETA так і позначено.
- Pause/Resume/Stop охоплюють scheduling, inference/build processes і spending; можливий час завершення поточної дії пояснений.
- Після app/worker/PC restart збережено accepted builds, uncommitted edits і decision scope; orphan не запускається повторно без перевірки.

**Перевірка:** Fault injection на worker/build/acceptance boundaries, stop під час paid call, service restart і no-progress tests.

### AF-GC-024 — Зробити основний шлях доступним українською та англійською

Перевірити реальний динамічний інтерфейс, клавіатуру та зрозумілість помилок, а не лише рядки HTML.

- Усі тексти основного creator flow локалізовані uk/en; помилки мають зрозумілу причину та дію.
- Клавіатура, focus order/return, screen-reader labels/status, contrast і target size перевірені на rendered UI.
- 320 CSS px, 200% zoom і laptop viewport не приховують керування; виправлено невалідний responsive CSS.

**Перевірка:** Browser accessibility scan і keyboard suite, manual screen-reader walkthrough, responsive screenshots; критерії WCAG 2.2 AA за застосовністю.

### AF-GC-025 — Визначити доступний шлях для 12+ та участь дорослого

Продукт має підтримувати 12-річного автора без вимоги обходити вікові чи облікові правила зовнішніх сервісів.

- Для кожного connector задокументовані актуальні офіційні вимоги та дата перевірки; eligibility перевіряється перед пропозицією login.
- Є зрозумілий дорослому setup/budget/consent flow там, де він дозволений; якщо cloud route недоступний, M1 пропонує зберегти ідею або offline template demo, а справжній local AI явно віднесено до 027–031.
- Мінімізовано особисті дані, cloud transmission і retention зрозумілі; немає публікації, покупок або передачі ключів за замовчуванням.

**Перевірка:** Review офіційних умов обраних providers і релевантних вимог до запуску продукту; UX cases 12,13–15,16+ без збору зайвих персональних даних.

### AF-GC-026 — Прийняти повний Godot-шлях на чистому ПК

M1 завершується відтворюваною реальною грою, а не кількістю реалізованих сервісів.

- Чистий Windows ПК: установка → ідея → дозволений cloud AI → setup → Godot build → Play → feedback → v2 → restore.
- Щонайменше collector і platformer пройшли сценарій; offline/no quota/low disk/crash мають перевірений recovery.
- Звіт містить host/версії/cost/time і реальне відео/логи; автоматичні тести та research gate 040 пройдені; відкритих P0 немає.

**Перевірка:** Дві чисті підтримувані конфігурації, власні дозволені тестові облікові записи з cap; independent acceptance.

### AF-GC-027 — Встановлювати та перевіряти локальні моделі

Система рекомендує сумісний локальний runtime/model, завантажує затверджену модель та перевіряє реальний inference.

- Перший локальний route через Ollama має каталог моделей з ліцензією, digest, disk/context/RAM припущеннями та датою кваліфікації.
- Download має прогрес/cancel/resume; вже наявна модель перевикористовується після перевірки версії.
- Доступність CLI не дорівнює loaded model; actual canary/OOM/offline checks визначають готовність і пропонують слабшій машині варіант.

**Перевірка:** Офіційний Ollama runtime на CPU-only і discrete GPU, обірване завантаження, OOM, bad digest, missing model.

### AF-GC-028 — Дати локальному AI кваліфікований інструмент розробки

Локальна генерація тексту стає контрольованим worker з file tools, ізоляцією, валідаторами та можливістю виправляти помилки.

- Qualified local worker редагує лише task worktree через дозволені tools; advisory completion не називається реалізованою грою.
- Role/capability/model profile сумісні з planning/development/review contracts; unsupported tasks пояснено.
- Local-only Godot change проходить actual build та незалежну перевірку; якщо іншої кваліфікованої моделі немає, статус вимагає явного human review.

**Перевірка:** Real local model на reference machine: write → build → repair; tool injection/path escape/timeout/OOM negative suite.

### AF-GC-029 — Ділити ресурси ПК між AI, рушієм та грою

Паралельні локальні завдання не повинні забирати всю пам’ять і робити неможливим playtest.

- Планувальник враховує host-wide RAM/VRAM reservations, queue і фактичний load; per-mission lease не видається за GPU scheduler.
- Playtest може звільнити ресурс через pause/unload AI; local time/context/disk caps діють навіть без грошової вартості.
- OOM, stale lease та кілька missions не спричиняють нескінченний reload; retry/backoff і diagnostics збережені.

**Перевірка:** Дві конкуруючі missions плюс Godot Play; RAM/VRAM pressure, crash/restart, queue fairness на кваліфікованому ПК.

### AF-GC-030 — Маршрутизувати cloud/local за явними правилами

Користувач вибирає local-only, cloud-only або hybrid та розуміє, куди піде кожна задача.

- HYBRID має окрему авторизацію provider/model/data/tools/cost; LOCAL ніколи не дозволяє remote fallback.
- Перед зміною маршруту перевіряються quality/capability, privacy і remaining budget; UI пояснює причину й ефективну модель.
- Недоступний cloud або local OOM дає дозволений fallback чи pause; робота та independent reviewer identity не губляться.

**Перевірка:** Local-only network-denied тест, cloud outage, budget exhaustion, model replacement та stale capability matrix.

### AF-GC-031 — Кваліфікувати local-only та hybrid створення гри

M2 має довести, що гра створюється і вдосконалюється на локальних ресурсах та у дозволеній змішаній конфігурації.

- Local-only та hybrid проходять idea→build→play→feedback на задокументованих hardware/model profiles.
- Виміряні peak memory, latency, quality, cost та OOM recovery; слабкий ПК отримує чесне обмеження/альтернативу.
- Немає недозволеного cloud трафіку, втрати checkpoint чи повторної прийнятої мутації при відновленні.

**Перевірка:** Actual local hardware matrix та fault campaign; незалежна перевірка артефактів і network/cost evidence.

### AF-GC-032 — Підключати Unity Hub, Editor і потрібні модулі

Unity має власний guided setup та кваліфіковану версію; не припускати, що всі дії Hub та активація автоматизуються.

- Каталог фіксує підтримувану Unity Editor/Hub версію та platform modules; detects existing installations і project compatibility.
- Account/license/EULA кроки передаються людині через офіційний flow; система перевіряє результат і відновлює setup.
- Insufficient license, missing module, low disk і conflicting editor versions мають actionable recovery.

**Перевірка:** Clean supported Windows VM з легітимною тестовою ліцензією; activation handoff та install/resume evidence.

### AF-GC-033 — Додати Unity pack, тести та build adapter

Unity C# pipeline використовує спільні contracts, але окремо перевіряє editor imports, compilation та реальну гру.

- Reference project має gameplay, керування, win/lose; package lock та Editor версія відтворювані.
- Batch build, EditMode/PlayMode checks та export використовують qualified vectors, logs/results і timeout; однаковий exit code не приховує compilation errors.
- Graphical Play і artifact identity інтегровані з чинними checkpoint/feedback controls; local/cloud підтримка явно вказана.

**Перевірка:** Healthy/broken Unity projects: C# compile error, missing asset/package, test failure, real build та graphical launch.

### AF-GC-034 — Прийняти повний Unity-шлях для новачка

Підтримка Unity означає пройдену послідовність створення, гри й зміни на чистій конфігурації.

- Setup→idea→AI→Unity project→test/build→Play→feedback→v2 пройдено із збереженою попередньою версією.
- Ліцензійні/мережеві/compile/crash failures показують просте recovery і не оголошують build готовим.
- Опублікована матриця Editor/platform/model/hardware та measured limits; Godot regressions пройдені.

**Перевірка:** Чиста підтримувана Unity машина, recorded human playtests і незалежний qualification report.

### AF-GC-035 — Керувати походженням та імпортом ігрових ресурсів

Зображення, моделі, звук і шрифти мають безпечний імпорт, provenance та зрозумілі обмеження використання.

- Кожен asset має source/license/attribution або позначку unknown; unknown блокує share/export, що потребує прав.
- Імпорт перевіряє тип/розмір/шкідливі archive paths; unsupported formats не запускають довільні plugins.
- Бюджети texture/poly/audio size пов’язані з target hardware; зміна asset має preview і rollback.

**Перевірка:** Licensed fixture assets, oversized/corrupt archives, missing attribution та engine reimport/performance checks.

### AF-GC-036 — Розширювати складність через вимірювані зразки ігор

Напрям «майже будь-яка складність» перетворити на рівні підтримки, перевірені реальними проєктами.

- Каталог покриває просту 2D, багаторівневу 2D та невелику 3D гру з save/load, UI, audio і asset budgets.
- Кожен рівень має відомі engine/model/hardware межі, quality/performance targets і критерії playable acceptance.
- Multiplayer/open-world/console/VR позначені окремими future investigations; система пропонує scoped prototype без неправдивої гарантії.

**Перевірка:** Reference-game benchmark: playability/performance/build regression плюс ручний gameplay review кожного рівня.

### AF-GC-037 — Експортувати гру та ділитися нею окремою дією

Готовий build можна забрати локально; публікація має окрему ціль, preview і зрозуміле рішення користувача.

- Підтримувані target presets створюють versioned package з attribution, checksum та короткою інструкцією запуску.
- Secrets, local paths і зайві особисті дані не входять в export; непідтримувана платформа пояснена до build.
- Публікація/visibility/доступ зовнішніх людей ніколи не успадковується з дозволу на розробку; canceled share не змінює external state.

**Перевірка:** Clean-machine запуск export, package inspection та simulated publication preview/denial; live publication лише за окремим запитом.

### AF-GC-038 — Оновлювати застосунок і збирати зрозумілу діагностику

Власник має безпечно оновити фабрику, відновити гру й отримати допомогу без ручного пошуку логів.

- Signed update з backup/migration/rollback перевіряє сумісність проєктів і не змінює pinned engine/model без окремого плану.
- Support bundle має preview/redaction і версії, але не ключі, prompts або файли гри без явного вибору.
- Uninstall розрізняє застосунок і користувацькі ігри; проєкти збережені за замовчуванням і відкриваються після reinstall.

**Перевірка:** Upgrade попередньої підтримуваної версії, interrupted migration, rollback, secret canary export, uninstall/reinstall на VM.

### AF-GC-039 — Узгодити авторизацію локального API з обіцяною політикою

Усі поверхні керування мають виконувати єдиний задокументований контракт доступу; захист не залежить від конкретного екрану.

- Інвентаризація read/mutation endpoints визначає потрібний auth і scope; configured token/session policy застосована послідовно.
- Browser має підтримуваний session flow; actor identity, local origin/host, expiration/revoke й необхідні human gates перевіряються на сервері.
- Негативна матриця без/неправильного/простроченого доступу проходить; sensitive reproduction details передаються через приватний security review.

**Перевірка:** Endpoint authorization matrix, intended browser flow, origin/host denial, replay та regression tests без публікації токенів.

### AF-GC-040 — Перевірити зрозумілість із користувачами 12–15 років

Зручність підтверджують спостереження за новачками, а не припущення розробника чи API-тест.

- Пілот щонайменше з 5 новачками 12–15 за належною згодою дорослих; синтетичні дані та дозволені облікові записи.
- Ціль: ≥4/5 знаходять старт і погоджують зрозумілий brief за ≤5 хв; ≥4/5 запускають build, дають feedback і знаходять Stop без підказки модератора.
- Звіт відокремлює активний час користувача від download/build/AI wait, фіксує допомогу/помилки; usability blockers усунені й повторно перевірені.

**Перевірка:** Модеровані сценарії та анонімізований протокол; це gate для пілоту, не статистичний доказ для всіх дітей.

### AF-GC-041 — Зберігати ролі та незалежність reviewer у live mission

Child authorization не повинна підміняти всі стадії одним agent/model і робити самоперевірку незалежним verdict.

- Implementation, validation, proxy review та policy stages отримують окремі сумісні scoped assignments.
- Effective producer identity виключена з незалежного review; за відсутності reviewer місія чекає рішення, не self-accepts.
- Replay/replace/resume зберігає provenance stage identity та не розширює права child.

**Перевірка:** Fixture з code-model/review-model/policy-model перевіряє різні invocation identities; same-model і mismatch denial до subprocess.

### AF-GC-042 — Кваліфікувати ролі planning та bootstrap для провайдерів

Затверджений route має бути реально сумісний із ролями planning/development, а не зупинятися на allowlist після старту.

- Відповідність role IDs/capabilities/profile перевіряється до вибору route й затвердження місії.
- Перший підтримуваний provider проходить read-only planning і потрібний bootstrap/developer contract без blanket tool permissions.
- Unsupported role показує конкретну доступну альтернативу; negative tests підтверджують, що заборонені ролі лишаються забороненими.

**Перевірка:** Matrix shipped provider profiles × autonomous role IDs; bounded canary для кваліфікованих пар, 0 subprocess для rejected pairs.

### AF-GC-043 — Atomically admit qualified workers with scoped attempts and shared capacity

One admission transaction checks the current qualification, lifecycle, trusted project owner and physical worker capacity, then creates the existing assignment, lease and first attempt.

- Share capacity across logical aliases and projects; worker-supplied capacity is not authority.
- Bind the exact task, run, stage, attempt, ready worktree and context before launch. Require the stored admission even when optional launch metadata is omitted.
- Replay one request or start without another attempt or external launch. Retain uncertain capacity after lease expiry, heartbeat loss or a lost start response.
- Release only with trusted stop evidence for the exact admission and fence; old acknowledgements cannot free newer work. Preserve the unregistered local API.

**Validation:** Independent SQLite connection races, rollback injection, synthetic runtime start/replay, stale qualification and scope denial, affected legacy tests, and independent review. This does not certify a remote host or deployment. See [worker admission](worker-admission.md).

## Що робимо зі старим беклогом

Стабільні `AF-001…057` і `AF-AMM-001…048` збережено. Їх не видаляємо, не перенумеровуємо й не імпортуємо вдруге як нову роботу. Звіт «57/57» описує попередні платформні вимоги, а не готовність продукту для дітей. Нові ID позначають нові результати або конкретні регресії; legacy references означають повторне використання/деталізацію, не автоматичне виконання залежності.

| Старі вимоги | Рішення | Новий результат |
|---|---|---|
| AF-036–043 | Використати API/UI основу; додати creator flow і browser evidence | 003–004, 007, 021–024, 040 |
| AF-009–016; AMM-007–011 | Використати intake/revisions; зберігати реальну ідею та короткий game brief | 005, 008, 022 |
| AF-004/018/019/056; AMM-005/006/029 | Зберегти LOCAL; додати окремий bounded CLOUD/HYBRID scope | 010, 018, 025, 030, 039 |
| AMM-020–022, 035–040 | Довести реальними змінами та playable checkpoints | 019–023, 041–042 |
| AMM-023–029 | Об’єднати в hardware-aware local worker/resource outcomes | 011–012, 027–031 |
| AMM-030–034 | Розділити ранній read-only inventory і затверджене виконання установки | 002, 011–015 |
| AMM-041–045 | Рознести API/UI між послідовними користувацькими сценаріями | 007–024, 030 |
| AMM-046–048 | Перевіряти помилки в кожній задачі; окремі release gates | 001, 026, 031, 034, 040 |
| AF-029–035 | Зберегти реалізовані контракти; подальший multi-tenant/cluster rollout відкласти | Не є prerequisite першої гри |

Подальше розширення marketplace, складних agent debates/quorums, clustered deployment, довготривалих soak та довільних рушіїв не ставимо перед першим playable. Наявні механізми не видаляємо. Multiplayer, console publishing, VR, billing/marketplace та необмежений unattended режим потребують окремого дослідження після кваліфікації основного шляху.

## Перевірка й імпорт

З кореня репозиторію після встановлення проєкту:

```sh
python scripts/validate-game-creator-backlog.py
python -m agent_factory backlog validate --path examples/game-creator-backlog.json
```

Маніфест — план, не автоматичний дозвіл на AI витрати, інсталяцію чи GitHub mutation. Його можна імпортувати чинними засобами Lokvetia Core після перевірки. Ця зміна комітить документи в Git; не створює десятки GitHub Issues і не запускає беклог на виконання.

## Первинні джерела для реалізації

Перевірено 2026-09-05; під час реалізації фіксувати обрану версію, а не покладатися на рухомий `stable`.

- [Godot command line](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html): окремі import/headless/script/export механізми, export presets/templates; саме тому 017 вимагає більше ніж позитивний agent verdict.
- [Unity Editor command line](https://docs.unity3d.com/6000.0/Documentation/Manual/EditorCommandLineArguments.html): аргументи editor automation; Unity adapter і license/setup кваліфікуються окремо у 032–034.
- [Ollama FAQ](https://docs.ollama.com/faq): memory/context/concurrency та model loading потрібно враховувати у локальному плануванні; 027–031 перевіряють це на реальному ПК.

Зазначені джерела обґрунтовують adapter requirements, а не доводять, що Lokvetia Core їх уже реалізує. Умови AI accounts, ліцензії та вимоги до віку перевіряються окремо в 025 перед ввімкненням connector.
