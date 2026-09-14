'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const state = {
    language: 'uk', messages: {}, mission: '', games: [],
    generation: 0, roster: {roles: []}, offering: null,
  };
  const query = new URLSearchParams(location.search);
  let createCommand = null;
  let creating = false;
  let pendingLoads = 0;

  // Every visible string comes from the server's catalogue, so a missing
  // translation shows up as a missing key rather than as silent Ukrainian.
  function say(key, parameters) {
    let text = state.messages[key];
    if (text === undefined) return key;
    if (parameters) {
      for (const [name, value] of Object.entries(parameters)) {
        text = text.split(`{${name}}`).join(String(value));
      }
    }
    return text;
  }

  function applyTranslations() {
    document.documentElement.lang = state.language;
    document.title = `${say('studio.title')} · ${say('app.name')}`;
    for (const node of document.querySelectorAll('[data-i18n]')) {
      node.textContent = say(node.dataset.i18n);
    }
    for (const node of document.querySelectorAll('[data-i18n-attr]')) {
      for (const pair of node.dataset.i18nAttr.split(';')) {
        const [attribute, key] = pair.split(':');
        if (attribute && key) node.setAttribute(attribute, say(key));
      }
    }
    for (const link of document.querySelectorAll('[data-lang]')) {
      link.setAttribute('aria-current', String(link.dataset.lang === state.language));
    }
    for (const link of document.querySelectorAll('.app-nav a')) {
      if (link.getAttribute('href') === location.pathname) link.setAttribute('aria-current', 'page');
    }
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function replace(node, children) {
    node.replaceChildren(...children);
  }

  // A name is asked for once and kept for this browser only, because typing it
  // again in three places is how a form becomes an obstacle. It is a
  // convenience, never a credential: the server still records whatever arrives.
  function rememberedName() {
    try { return localStorage.getItem('lokvetia.actor') || ''; } catch { return ''; }
  }
  function rememberName(value) {
    try { localStorage.setItem('lokvetia.actor', value); } catch { /* private window */ }
  }

  async function readJson(response) {
    try { return await response.json(); } catch { return {}; }
  }

  function withLanguage(url) {
    return url + (url.includes('?') ? '&' : '?') + 'lang=' + encodeURIComponent(state.language);
  }

  async function get(url) {
    const response = await fetch(withLanguage(url), {cache: 'no-store'});
    const payload = await readJson(response);
    if (!response.ok || payload.error) {
      const error = new Error(payload.error ? payload.error.message : `HTTP ${response.status}`);
      error.code = payload.error ? payload.error.code : 'http_error';
      throw error;
    }
    return payload;
  }

  async function maybe(url, fallback) {
    try { return await get(url); } catch { return fallback; }
  }

  async function post(url, body) {
    const response = await fetch(withLanguage(url), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Agent-Factory-Confirm': 'true'},
      body: JSON.stringify(Object.assign({confirmed: true}, body)),
    });
    const payload = await readJson(response);
    if (!response.ok || payload.error) {
      const detail = payload.detail === 'local_studio_not_ready'
        ? say('studio.create.notready') : payload.detail;
      throw new Error(payload.error ? payload.error.message
        : (typeof detail === 'string' ? detail : `HTTP ${response.status}`));
    }
    return payload;
  }

  async function loadMessages() {
    const chosen = query.get('lang');
    const response = await fetch(
      '/api/i18n' + (chosen ? `?lang=${encodeURIComponent(chosen)}` : ''), {cache: 'no-store'});
    if (!response.ok) return;
    const bundle = await response.json();
    state.language = bundle.language;
    state.messages = bundle.messages;
    applyTranslations();
  }

  // ------------------------------------------------------------ the games

  async function loadGames() {
    const listing = await maybe('/api/games/missions', {items: []});
    state.games = (listing.items || []).filter(item => item.mission_key);
    const picker = byId('mission');
    const wanted = query.get('game') || query.get('mission') || state.mission || (state.games[0] || {}).mission_key || '';
    replace(picker, state.games.map(game => {
      const option = element('option', game.title || game.mission_key);
      option.value = game.mission_key;
      return option;
    }));
    // A game can be named in the address before it is in anyone's listing.
    // Opening it anyway beats an empty screen that blames the person.
    if (wanted && !state.games.some(game => game.mission_key === wanted)) {
      const option = element('option', wanted);
      option.value = wanted;
      picker.append(option);
    }
    if (!picker.options.length) {
      const option = element('option', say('studio.mission.none'));
      option.value = '';
      picker.append(option);
    }
    picker.value = wanted;
    state.mission = picker.value || wanted;
    picker.parentElement.hidden = picker.options.length < 2;
  }

  function currentGame() {
    return state.games.find(game => game.mission_key === state.mission) || null;
  }

  // -------------------------------------------------------------- the page

  function showHero(plan, first, run, cycles) {
    const game = currentGame();
    byId('title').textContent =
      plan.project || (game && game.title) || state.mission || say('studio.title');
    const counts = plan.counts || {};
    const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
    const done = counts.done || 0;
    byId('progress-meter').firstElementChild.style.width =
      (total ? Math.round((done / total) * 100) : 0) + '%';
    byId('progress-text').textContent = total
      ? say('studio.progress', {done, total}) : '';
    byId('progress-meter').hidden = !total;
    // The same sentence twice is noise. When the lede already says development
    // cannot start, the warning strip below it has nothing to add.
    byId('summary').textContent = !first.can_start
      ? first.summary
      : (plan.next || say('studio.plan.empty'));
    byId('start-blocked').hidden = true;

    // One action, and it is whichever one the situation actually calls for.
    const actions = [];
    if (!first.can_start) {
      const fix = element('a', say('studio.action.setup'), 'btn btn--primary btn--big');
      fix.href = '/settings';
      actions.push(fix);
    } else if (!(run.mandate && run.mandate.live)) {
      const delegate = element('button', say('studio.action.delegate'), 'btn btn--primary btn--big');
      delegate.type = 'button';
      delegate.addEventListener('click', () => {
        byId('actor').focus();
        byId('run-grant').scrollIntoView({block: 'center'});
      });
      actions.push(delegate);
    }
    replace(byId('hero-actions'), actions);
    const label = cycles.state === 'paused'
      ? say('studio.loop.paused') : say('studio.loop.running');
    byId('cycle-state').textContent =
      `${label} — ${say('studio.loop.cycle', {number: cycles.cycle || 1})}`;
  }

  function showPlan(plan) {
    const stages = plan.stages || [];
    replace(byId('stages'), stages.map(stage => {
      const box = element('section', undefined, 'stage');
      const head = element('div', undefined, 'stage-head');
      head.append(element('h3', stage.title));
      if (stage.boundary === 'playable') {
        const play = element('a', say('studio.play'), 'btn');
        play.href = `/games?version=${encodeURIComponent(stage.playable_version)}`;
        head.append(play);
      }
      box.append(head);
      if (stage.boundary_note) box.append(element('p', stage.boundary_note, 'boundary'));
      const tasks = element('ul', undefined, 'tasks');
      for (const item of stage.items || []) {
        const row = element('li');
        row.append(element('span', item.state_label, 'chip chip--' + item.state));
        row.append(element('span', item.title));
        if (item.role) row.append(element('span', item.role, 'task-role'));
        if (item.note) row.append(element('p', item.note, 'note'));
        tasks.append(row);
      }
      box.append(tasks);
      return box;
    }));
    byId('plan-empty').hidden = stages.length > 0;
    byId('next').textContent = plan.next || '—';
  }

  function showQuestions(decisions, tools, money) {
    const items = [];
    for (const question of decisions.questions || []) {
      items.push({subject: question.subject, ask: question.ask,
                  options: question.options || []});
    }
    for (const tool of tools.open || []) {
      items.push({subject: `${tool.tool}: ${tool.reason}`, ask: '',
                  options: (tool.ways_out || []).map(way => way.label)});
    }
    if (money && money.question) {
      items.push({subject: money.question.subject, ask: money.question.ask || '',
                  options: money.question.options || []});
    }
    replace(byId('questions'), items.map(item => {
      const entry = element('li');
      entry.append(element('strong', item.subject));
      if (item.ask) entry.append(element('p', item.ask, 'small'));
      if (item.options.length) {
        entry.append(element('span', item.options.join(' · '), 'options'));
      }
      return entry;
    }));
    byId('questions-card').hidden = items.length === 0;
  }

  function showMoney(money) {
    const unit = money.unit || 'USD';
    const rows = [
      ['studio.money.spent', money.spent],
      ['studio.money.reserved', money.reserved],
      ['studio.money.limit', money.limit],
    ];
    replace(byId('money'), rows.flatMap(([key, value]) => {
      const box = element('div', undefined, 'stat');
      box.append(element('dt', say(key)));
      box.append(element('dd', value === null || value === undefined
        ? '—' : `${Number(value).toFixed(2)} ${unit}`));
      return [box];
    }));
    const meter = byId('money-meter');
    const limit = Number(money.limit) || 0;
    const share = value => limit > 0 ? Math.min(100, (Number(value) || 0) / limit * 100) + '%' : '0';
    meter.children[0].style.width = share(money.spent);
    meter.children[1].style.width = share(money.reserved);
    meter.hidden = limit <= 0;
    // A forecast without measured tasks behind it says so, and the screen
    // repeats that sentence rather than showing a number it does not have.
    const forecast = money.forecast;
    byId('forecast').textContent = forecast
      ? `${say('studio.money.forecast')}: ${forecast.known
          ? Number(forecast.amount).toFixed(2) + ' ' + unit : forecast.basis}`
      : '';
    const table = byId('by-role');
    const header = element('tr');
    for (const key of ['studio.team.title', 'studio.money.spent', 'studio.money.reserved']) {
      header.append(element('th', say(key)));
    }
    const body = (money.by_role || []).map(entry => {
      const row = element('tr');
      row.append(element('td', entry.role));
      row.append(element('td', `${Number(entry.spent).toFixed(2)} ${unit}`));
      row.append(element('td', `${Number(entry.reserved).toFixed(2)} ${unit}`));
      return row;
    });
    replace(table, [header, ...body]);
  }

  function showTeam(roster) {
    state.roster = roster || {roles: []};
    const acceptance = byId('acceptance');
    acceptance.textContent = state.roster.acceptance || '';
    acceptance.hidden = !state.roster.acceptance;
    const offering = state.offering;
    replace(byId('team'), (state.roster.roles || []).map(role => {
      const item = element('li', undefined, role.enabled ? '' : 'off');
      item.append(element('span', role.title, 'who'));
      item.append(element('span', role.duty, 'duty'));
      if (role.enabled) {
        if (role.model) item.append(element('span', `${role.provider} ${role.model}`, 'task-role'));
        if (!role.core) {
          item.append(button(say('studio.team.disable'),
                             () => changeRole(role.role, 'disable'), 'btn'));
        }
        return item;
      }
      item.append(element('span', say('studio.team.off'), 'chip chip--planned'));
      if (offering && offering.role === role.role) {
        const offer = element('p', consequenceLines(offering), 'offer');
        if (offering.consequence) {
          offer.append(document.createTextNode(' '));
          offer.append(button(say('studio.team.confirm'),
                              () => changeRole(role.role, 'enable'), 'btn btn--primary'));
        }
        item.append(offer);
      } else {
        item.append(button(say('studio.team.enable'), () => offerRole(role.role), 'btn'));
      }
      return item;
    }));
  }

  function button(label, handler, className) {
    const node = element('button', label, className);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function showRun(run) {
    const mandate = run.mandate && run.mandate.live ? run.mandate : null;
    byId('run-state').textContent = run.summary || say('studio.run.nobody');
    const rows = [];
    if (mandate) {
      rows.push([say('studio.run.may'), mandate.steps.join(', ')]);
      rows.push([say('studio.run.until'), mandate.expires_at]);
      if (mandate.ceiling !== null && mandate.ceiling !== undefined) {
        rows.push([say('studio.run.ceiling'), `${mandate.ceiling} ${mandate.unit}`]);
      }
    }
    replace(byId('run-mandate'), rows.map(([term, value]) => {
      const box = element('div', undefined, 'stat');
      box.append(element('dt', term));
      box.append(element('dd', value));
      return box;
    }));
    byId('run-grant').hidden = Boolean(mandate);
    byId('run-revoke').hidden = !mandate;
    byId('run-ceiling').parentElement.hidden = Boolean(mandate);
    const steps = run.steps || [];
    replace(byId('run-steps'), steps.map(step => {
      const entry = element('li');
      entry.append(element('strong', step.summary));
      if (step.detail) entry.append(element('span', ` — ${step.detail}`, 'detail'));
      return entry;
    }));
    byId('run-empty').hidden = steps.length > 0;
  }

  function showLoop(cycles) {
    const said = [];
    for (const cycle of cycles.history || []) {
      for (const comment of cycle.comments || []) {
        said.push(element('li', `${cycle.cycle}: ${comment.text}`));
      }
    }
    replace(byId('comments'), said);
  }

  function showMachines(machines) {
    replace(byId('machines'), (machines.machines || []).map(machine => {
      const entry = element('li');
      entry.append(element('strong', machine.signature));
      if (!machine.builds) entry.append(element('span', `— ${say('studio.machines.nobuild')}`, 'nobuild'));
      return entry;
    }));
  }

  async function load() {
    // Two loads can overlap - switching game while the first is still in
    // flight. Only the newest one may paint, or a slow answer about the old
    // game quietly replaces what is on the screen.
    const generation = ++state.generation;
    const mission = encodeURIComponent(state.mission);
    pendingLoads += 1;
    try {
      const [first, local, plan, decisions, tools, money, roster, cycles, machines, run] =
        await Promise.all([
          maybe('/api/studio/first-run', {can_start: true, summary: ''}),
          maybe('/api/studio/local-readiness', {can_start: false, summary: ''}),
          maybe(`/api/studio/plan/${mission}`, {stages: []}),
          maybe(`/api/studio/decisions?mission=${mission}`, {questions: []}),
          maybe(`/api/studio/paid-tools/${mission}`, {open: []}),
          maybe(`/api/studio/cost/${mission}`, {}),
          maybe(`/api/studio/roster/${mission}`, {roles: []}),
          maybe(`/api/studio/cycles/${mission}`, {cycle: 1, state: 'running'}),
          maybe('/api/studio/machines', {machines: []}),
          maybe(`/api/studio/supervisor/${mission}`, {mandate: null, steps: [], summary: ''}),
        ]);
      if (generation !== state.generation) return;
      byId('create-game').disabled = creating || !local.can_start;
      byId('create-game').title = local.summary;
      byId('create-readiness').textContent = local.summary;
      showHero(plan, first, run, cycles);
      showPlan(plan);
      showQuestions(decisions, tools, money);
      showMoney(money);
      showTeam(roster);
      showLoop(cycles);
      showMachines(machines);
      showRun(run);
    } catch (error) {
      if (generation === state.generation) {
        byId('create-game').disabled = true;
        byId('create-readiness').textContent = say('studio.error', {message: error.message});
        byId('summary').textContent = say('studio.error', {message: error.message});
      }
    } finally {
      pendingLoads -= 1;
    }
  }

  // ---------------------------------------------------------------- acting

  function actor() {
    return byId('actor').value.trim();
  }

  function needName(result) {
    result.textContent = say('studio.loop.need-name');
    byId('actor').focus();
  }

  // Turning a role on is two steps on purpose: the first shows what it would
  // cost and what it changes, and only the second switches anything. The
  // pending offer lives in state, so a refresh of the list does not silently
  // throw away what the person is in the middle of reading.
  async function offerRole(role) {
    state.offering = {role, consequence: null, error: ''};
    showTeam(state.roster);
    try {
      state.offering = {
        role,
        consequence: await get(
          `/api/studio/roster/${encodeURIComponent(state.mission)}/consequence/${role}`),
        error: '',
      };
    } catch (error) {
      state.offering = {role, consequence: null, error: error.message};
    }
    showTeam(state.roster);
  }

  function consequenceLines(offer) {
    if (offer.error) return say('studio.error', {message: offer.error});
    if (!offer.consequence) return say('common.loading');
    const consequence = offer.consequence;
    const lines = [
      consequence.added_cost === null
        ? say('studio.team.cost.unknown')
        : say('studio.team.cost', {
            amount: Number(consequence.added_cost).toFixed(2),
            unit: consequence.unit,
          }),
      consequence.subscription_note,
    ];
    if (consequence.acceptance_changes) lines.push(say('studio.team.acceptance.changes'));
    return lines.join(' ');
  }

  async function changeRole(role, action) {
    const result = byId('loop-result');
    if (!actor()) return needName(result);
    try {
      const suffix = action === 'disable' ? '?action=disable' : '';
      await post(
        `/api/studio/roster/${encodeURIComponent(state.mission)}/${role}${suffix}`,
        {actor: actor()});
      state.offering = null;
      await load();
    } catch (error) {
      result.textContent = say('studio.error', {message: error.message});
    }
  }

  async function act(action) {
    const result = byId('loop-result');
    if (!actor()) return needName(result);
    const mission = encodeURIComponent(state.mission);
    try {
      if (action === 'send') {
        const text = byId('comment').value.trim();
        if (!text) { byId('comment').focus(); return; }
        await post(`/api/studio/cycles/${mission}/comments`, {text, author: actor()});
        byId('comment').value = '';
      } else {
        const answer = await post(
          `/api/studio/cycles/${mission}/${action}`, {actor: actor()});
        result.textContent = answer.summary || answer.note || '';
      }
      await load();
    } catch (error) {
      result.textContent = say('studio.error', {message: error.message});
    }
  }

  // Handing the studio the keys takes two clicks on purpose: the first says what
  // it will then do in the person's name, the second does it.
  async function delegate(action) {
    const result = byId('run-result');
    if (!actor()) return needName(result);
    const mission = encodeURIComponent(state.mission);
    const grant = byId('run-grant');
    if (action === 'grant' && grant.dataset.armed !== 'yes') {
      grant.dataset.armed = 'yes';
      grant.textContent = say('studio.run.confirm');
      result.textContent = say('studio.run.consequence');
      return;
    }
    try {
      if (action === 'grant') {
        const ceiling = Number(byId('run-ceiling').value);
        await post(`/api/studio/supervisor/${mission}`, {
          actor: actor(), steps: ['plan'],
          ceiling: Number.isFinite(ceiling) && ceiling >= 0 ? ceiling : 0,
        });
      } else {
        await post(`/api/studio/supervisor/${mission}/revoke`, {actor: actor()});
      }
      result.textContent = '';
      await load();
    } catch (error) {
      result.textContent = say('studio.error', {message: error.message});
    } finally {
      grant.dataset.armed = '';
      grant.textContent = say('studio.run.grant');
    }
  }

  // Starting a game is one request, and repeating it after a failure repeats the
  // same command rather than making a second game.
  async function createGame(event) {
    event.preventDefault();
    if (creating || byId('create-game').disabled) return;
    creating = true;
    byId('create-game').disabled = true;
    createCommand = createCommand || crypto.randomUUID();
    try {
      const answer = await post('/api/studio/create', {
        command_id: createCommand,
        title: byId('game-title').value.trim(),
        idea: byId('game-idea').value.trim(),
      });
      state.mission = answer.mission_key;
      byId('create-result').textContent = say('studio.create.queued');
      createCommand = null;
      await loadGames();
    } catch (error) {
      byId('create-result').textContent = say('studio.error', {message: error.message});
    } finally {
      creating = false;
      await load();
    }
  }

  function start() {
    byId('actor').value = rememberedName();
    byId('actor').addEventListener('change', event => rememberName(event.target.value.trim()));
    byId('create-form').addEventListener('submit', createGame);
    for (const id of ['game-title', 'game-idea']) {
      byId(id).addEventListener('input', () => { if (!creating) createCommand = null; });
    }
    byId('mission').addEventListener('change', event => {
      state.mission = event.target.value;
      state.offering = null;
      load();
    });
    byId('pause').addEventListener('click', () => act('pause'));
    byId('resume').addEventListener('click', () => act('resume'));
    byId('send').addEventListener('click', () => act('send'));
    byId('run-grant').addEventListener('click', () => delegate('grant'));
    byId('run-revoke').addEventListener('click', () => delegate('revoke'));
    loadMessages().then(loadGames).then(load).catch(error => {
      byId('summary').textContent = say('studio.error', {message: error.message});
    });
    // A page left open should show what changed without being asked, but never
    // start a second read while the first is still in flight.
    setInterval(() => { if (!document.hidden && pendingLoads === 0) load(); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
