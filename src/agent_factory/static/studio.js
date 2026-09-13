'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const state = {
    language: 'uk', messages: {}, mission: 'cat-coins',
    generation: 0, roster: {roles: []}, offering: null,
  };
  const query = new URLSearchParams(location.search);
  let createCommand = null;
  let creating = false;
  let recovering = false, recoveryCommand = null;
  let checkingUpdate = false;

  function installedUpdateLabel(installed) {
    byId('updates-current').textContent=installed.version?say('studio.updates.installed',{version:installed.version}):say(installed.kind==='source'?'studio.updates.source':'studio.updates.unknown');
  }

  async function loadUpdateInfo() {
    try{installedUpdateLabel((await get('/api/studio/updates')).installed);}
    catch{byId('updates-current').textContent=say('studio.updates.unknown');}
  }

  async function checkUpdate() {
    if(checkingUpdate)return;
    checkingUpdate=true;byId('updates-check').disabled=true;
    byId('updates-download').hidden=true;byId('updates-details').hidden=true;byId('updates-instructions').hidden=true;
    byId('updates-status').textContent=say('studio.updates.checking');
    try {
      const result=await get('/api/studio/updates/check');
      installedUpdateLabel(result.installed);
      byId('updates-status').textContent=say('studio.updates.'+result.status,{version:result.latest?.version||''});
      if(result.latest){
        byId('updates-details').hidden=false;
        byId('updates-details').textContent=say('studio.updates.details',{size:(result.latest.size_bytes/1048576).toFixed(1),time:dateLabel(result.checked_at)});
        byId('updates-download').href=result.latest.download_url;
        byId('updates-download').hidden=result.status==='ahead';
        byId('updates-instructions').hidden=result.status==='ahead';
        byId('updates-checksum').textContent='SHA-256: '+result.latest.sha256;
      }
    }catch{byId('updates-status').textContent=say('studio.updates.unavailable');}
    finally{checkingUpdate=false;byId('updates-check').disabled=false;}
  }
  let pendingLoads = 0;
  let libraryOffset = 0, selectedGame = null, libraryOwner = null;
  function dateLabel(value) {
    const raw=value.includes('T')?value:value.replace(' ','T')+'Z';
    const date=new Date(raw);return Number.isNaN(date.getTime())?value:date.toLocaleString(state.language==='uk'?'uk-UA':'en-GB');
  }

  function selectGame(key) {
    byId('mission').value=key;state.mission=key;
    if(libraryOwner)try{sessionStorage.setItem('studio-selection:'+libraryOwner,key);}catch{}
    const url=new URL(location.href);url.searchParams.set('mission',key);
    history.replaceState(null,'',url);
    for(const link of document.querySelectorAll('[data-lang]')) {
      const target=new URL(url);target.searchParams.set('lang',link.dataset.lang);link.href=target.href;
    }
  }

  function renderProgress(game) {
    selectGame(game.mission_key);
    selectedGame=game;byId('planning-progress').hidden=false;
    byId('planning-title').textContent=game.title;
    byId('planning-status').textContent=game.summary;
    byId('planning-model').textContent=say('studio.progress.model',{model:game.model,provider:game.provider});
    byId('planning-cost').textContent=say(game.local_only?'studio.progress.local':'studio.progress.cost_unknown');
    byId('planning-updated').textContent=say('studio.progress.updated',{time:dateLabel(game.updated_at)});
    byId('planning-meter').max=game.total_steps;byId('planning-meter').value=game.completed_steps;
    byId('planning-count').textContent=say('studio.progress.count',{done:game.completed_steps,total:game.total_steps});
    replace(byId('planning-steps'),game.steps.map(step=>{
      const row=element('li');row.className=step.state;
      const label=step.state==='done'?say('studio.progress.done'):step.state==='failed'?say('studio.progress.invalid'):step.state==='running'?say('studio.progress.attempt',{attempt:step.active_attempt,max:game.max_attempts}):say('studio.progress.pending');
      row.append(element('strong',step.name),element('span',` — ${label}`));
      if(step.model!==game.model)row.append(element('small',` · ${step.model}`));return row;
    }));
    byId('planning-explanation').textContent=game.state==='failed'?say('studio.progress.failed',{role:game.failure_role||game.last_detail||game.summary}):game.state==='interrupted'?say('studio.progress.interrupted'):game.state==='cancelled'?say('studio.progress.stopped'):game.state==='stopping'?say('studio.progress.stopping'):game.background?say('studio.progress.running'):game.last_detail;
    byId('planning-stop').hidden=!game.can_stop;
    byId('planning-recovery').hidden=!game.can_recover;
    byId('planning-recover').disabled=recovering;
    byId('recovery-reason').textContent=game.recovery_reason||'';
    byId('recovery-scope').textContent=say('studio.recovery.scope',{kept:game.recovery_kept_steps||0,model:game.model});
    byId('planning-idea').textContent=game.idea;
    byId('planning-failure').hidden=!game.failure_details?.length;
    replace(byId('planning-errors'),(game.failure_details||[]).map(error=>element('li',error)));
    const opened=new Set(Array.from(byId('planning-artifacts').querySelectorAll('details[open]')).map(node=>node.dataset.artifact));
    replace(byId('planning-artifacts'),game.artifacts.map(artifact=>{
      const details=element('details');details.dataset.artifact=String(artifact.id);details.open=opened.has(String(artifact.id));
      details.append(element('summary',artifact.title),resultContent(artifact.content));return details;
    }));
    if(!game.artifacts.length)byId('planning-artifacts').append(element('p',say('studio.progress.none')));
  }

  function resultContent(value) {
    if(value===null||typeof value!=='object')return element('p',String(value??''),'preserve-lines');
    const container=element(Array.isArray(value)?'ol':'div');
    for(const [key,content] of Object.entries(value)) {
      const item=element(Array.isArray(value)?'li':'section');
      if(!Array.isArray(value))item.append(element('strong',key.replaceAll('_',' ')));
      item.append(resultContent(content));container.append(item);
    }
    return container;
  }

  async function loadLibrary(generation) {
    try {
      const data=await get(`/api/studio/games?offset=${libraryOffset}&limit=6`);
      if(generation!==state.generation)return;
      libraryOwner=data.owner||null;
      if(!query.get('mission')&&!selectedGame&&byId('mission').value==='cat-coins'&&data.items.length){
        let previous=null;try{previous=sessionStorage.getItem('studio-selection:'+libraryOwner);}catch{}
        selectGame(previous||data.items[0].mission_key);
      }
      const focusedGame=document.activeElement?.dataset.game;
      replace(byId('game-library'),data.items.map(game=>{
        const button=element('button');button.type='button';button.dataset.game=game.mission_key;
        button.setAttribute('aria-pressed',String(game.mission_key===byId('mission').value));
        button.append(element('strong',game.title),element('span',`${game.summary} · ${game.completed_steps}/${game.total_steps}`),element('small',dateLabel(game.created_at)));
        button.onclick=()=>{selectGame(game.mission_key);load();};return button;
      }));
      if(focusedGame)Array.from(byId('game-library').querySelectorAll('button')).find(button=>button.dataset.game===focusedGame)?.focus({preventScroll:true});
      if(!data.items.length)byId('game-library').append(element('p',say('studio.library.empty')));
      byId('games-count').textContent=data.total?`${libraryOffset+1}–${Math.min(libraryOffset+6,data.total)} / ${data.total}`:'';
      byId('games-previous').hidden=libraryOffset===0;byId('games-more').hidden=libraryOffset+6>=data.total;
      if(byId('mission').value&&byId('mission').value!=='cat-coins') {
        const game=await get(`/api/studio/progress/${encodeURIComponent(byId('mission').value)}`);
        if(generation!==state.generation)return;
        renderProgress(game);
      }
      byId('library-error').textContent='';
    } catch(error) {
      if(generation!==state.generation)return;
      byId('library-error').textContent=say('studio.error',{message:error.message});
      byId('planning-status').textContent=say('studio.error',{message:error.message});
      byId('planning-stop').hidden=true;
      byId('planning-recovery').hidden=true;
    }
  }

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
        if (attribute && key) node.setAttribute(attribute.trim(), say(key.trim()));
      }
    }
    for (const node of document.querySelectorAll('[data-lang]')) {
      node.setAttribute('aria-current', String(node.dataset.lang === state.language));
    }
  }

  function withLanguage(url) {
    return `${url}${url.includes('?') ? '&' : '?'}lang=${encodeURIComponent(state.language)}`;
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

  async function readJson(response) {
    try { return await response.json(); } catch { return {}; }
  }

  async function get(url) {
    const response = await fetch(withLanguage(url), {cache: 'no-store'});
    const payload = await readJson(response);
    if(response.status===401||response.status===403) {
      selectedGame=null;libraryOwner=null;byId('game-library').replaceChildren();byId('planning-progress').hidden=true;
      byId('game-title').value='';byId('game-idea').value='';
      throw new Error(say('studio.progress.access'));
    }
    if (!response.ok || payload.error) {
      const error = new Error(payload.error ? payload.error.message : `HTTP ${response.status}`);
      error.code = payload.error ? payload.error.code : 'http_error';
      throw error;
    }
    return payload;
  }

  // A missing plan is a normal state for a game that has not been planned yet,
  // so it must not blank the rest of the screen.
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
      const detail = payload.detail === 'local_studio_not_ready' ? say('studio.create.notready') : typeof payload.detail==='string' && payload.detail.startsWith('recovery_') ? say('studio.recovery.'+payload.detail) : payload.detail;
      throw new Error(payload.error ? payload.error.message : (typeof detail === 'string' ? detail : `HTTP ${response.status}`));
    }
    return payload;
  }

  async function loadMessages() {
    const chosen = query.get('lang');
    const response = await fetch(
      `/api/i18n${chosen ? `?lang=${encodeURIComponent(chosen)}` : ''}`,
      {cache: 'no-store'},
    );
    if (!response.ok) return;
    const payload = await response.json();
    state.language = payload.language;
    state.messages = payload.messages;
    applyTranslations();
  }

  function showPlan(plan) {
    const stages = plan.stages || [];
    byId('plan-empty').hidden = stages.length > 0;
    byId('next').textContent = plan.next || '';
    replace(byId('stages'), stages.map(stage => {
      const section = element('div', undefined, 'stage');
      const head = element('div', undefined, 'stage-head');
      head.append(element('h3', stage.title));
      if (stage.boundary === 'playable') {
        const play = element('a', say('studio.play'));
        play.href = `/games?version=${encodeURIComponent(stage.playable_version)}`;
        head.append(play);
      }
      section.append(head);
      section.append(element('p', stage.boundary_note || '', 'boundary'));
      const tasks = element('ul', undefined, 'tasks');
      for (const item of stage.items) {
        const entry = element('li');
        entry.append(element('span', item.state_label, `state state-${item.state}`));
        entry.append(element('span', item.title));
        if (item.role) entry.append(element('span', `· ${item.role}`));
        if (item.note) entry.append(element('p', item.note, 'note'));
        tasks.append(entry);
      }
      section.append(tasks);
      return section;
    }));
  }

  function showQuestions(decisions, tools, money) {
    const items = [];
    for (const question of decisions.questions || []) {
      const entry = element('li');
      entry.append(element('strong', question.subject));
      entry.append(element('span', `${question.ask} (${question.options.join(' / ')})`, 'options'));
      items.push(entry);
    }
    for (const choice of tools.open || []) {
      const entry = element('li');
      entry.append(element('strong', `${choice.tool}: ${choice.reason}`));
      entry.append(element(
        'span', choice.ways_out.map(way => way.label).join(' / '), 'options'));
      items.push(entry);
    }
    if (money.question) {
      const entry = element('li');
      entry.append(element('strong', money.question.subject));
      entry.append(element('span', money.question.ask, 'options'));
      items.push(entry);
    }
    replace(byId('questions'), items.length
      ? items
      : [element('li', say('studio.questions.none'), 'clear')]);
  }

  function amount(value, unit) {
    return value === null || value === undefined ? '—' : `${Number(value).toFixed(2)} ${unit}`;
  }

  function showMoney(money) {
    const unit = money.unit || 'USD';
    const rows = [
      [say('studio.money.spent'), amount(money.spent, unit)],
      [say('studio.money.reserved'), amount(money.reserved, unit)],
      [say('studio.money.limit'), amount(money.limit, unit)],
    ];
    const nodes = [];
    for (const [term, value] of rows) {
      nodes.push(element('dt', term));
      nodes.push(element('dd', value));
    }
    replace(byId('money'), nodes);
    // A forecast without measured tasks behind it says so, and the screen
    // repeats that sentence rather than showing a number it does not have.
    byId('forecast').textContent = money.forecast
      ? `${say('studio.money.forecast')}: ${money.forecast.known
          ? amount(money.forecast.amount, unit) : money.forecast.basis}`
      : '';
    replace(byId('by-role'), (money.by_role || []).map(row => {
      const line = document.createElement('tr');
      line.append(element('td', row.role));
      line.append(element('td', amount(row.spent, unit)));
      line.append(element('td', amount(row.reserved, unit)));
      return line;
    }));
  }

  function money2(value, unit) {
    return `${Number(value).toFixed(2)} ${unit}`;
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
    if (!actor()) {
      result.textContent = say('studio.loop.need-name');
      byId('actor').focus();
      return;
    }
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

  function button(label, handler) {
    const node = element('button', label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function showTeam(roster) {
    state.roster = roster || {roles: []};
    byId('acceptance').textContent = state.roster.acceptance || '';
    const offering = state.offering;
    replace(byId('team'), (state.roster.roles || []).map(role => {
      const entry = element('li', undefined, role.enabled ? '' : 'off');
      entry.append(element('strong', role.title));
      entry.append(element('span', role.duty));
      if (role.enabled) {
        if (role.model) entry.append(element('span', `· ${role.provider} ${role.model}`));
        if (!role.core) {
          entry.append(button(
            say('studio.team.disable'), () => changeRole(role.role, 'disable')));
        }
        return entry;
      }
      entry.append(element('span', `— ${say('studio.team.off')}`));
      if (offering && offering.role === role.role) {
        entry.append(element('p', consequenceLines(offering), 'note'));
        if (offering.consequence) {
          entry.append(button(
            say('studio.team.confirm'), () => changeRole(role.role, 'enable')));
        }
      } else {
        entry.append(button(say('studio.team.enable'), () => offerRole(role.role)));
      }
      return entry;
    }));
  }

  function showLoop(cycles) {
    const label = cycles.state === 'paused'
      ? say('studio.loop.paused') : say('studio.loop.running');
    byId('cycle-state').textContent =
      `${label} — ${say('studio.loop.cycle', {number: cycles.cycle})}`;
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
      if (!machine.builds) entry.append(element('span', ` — ${say('studio.machines.nobuild')}`));
      return entry;
    }));
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
    replace(byId('run-mandate'), rows.flatMap(([term, value]) => [
      element('dt', term), element('dd', value),
    ]));
    byId('run-grant').hidden = Boolean(mandate);
    byId('run-revoke').hidden = !mandate;
    byId('run-ceiling').hidden = Boolean(mandate);
    document.querySelector('label[for="run-ceiling"]').hidden = Boolean(mandate);
    const steps = run.steps || [];
    replace(byId('run-steps'), steps.map(step => {
      const entry = element('li');
      entry.append(element('strong', step.summary));
      if (step.detail) entry.append(element('span', ` — ${step.detail}`));
      return entry;
    }));
    byId('run-empty').hidden = steps.length > 0;
  }

  async function load() {
    state.mission = byId('mission').value.trim() || state.mission;
    // Two loads can overlap - switching game while the first is still in
    // flight. Only the newest one may paint, or a slow answer about the old
    // game quietly replaces what is on the screen.
    const generation = ++state.generation;
    pendingLoads += 1;
    try {
      await loadLibrary(generation);
      if(generation!==state.generation)return;
      state.mission=byId('mission').value.trim()||state.mission;
      const [first, local, plan, decisions, tools, money, roster, cycles, machines, run] =
        await Promise.all([
          get('/api/studio/first-run'),
          get('/api/studio/local-readiness'),
          maybe(`/api/studio/plan/${encodeURIComponent(state.mission)}`, {stages: []}),
          maybe(`/api/studio/decisions?mission=${encodeURIComponent(state.mission)}`, {questions: []}),
          maybe(`/api/studio/paid-tools/${encodeURIComponent(state.mission)}`, {open: []}),
          maybe(`/api/studio/cost/${encodeURIComponent(state.mission)}`, {}),
          maybe(`/api/studio/roster/${encodeURIComponent(state.mission)}`, {roles: []}),
          maybe(`/api/studio/cycles/${encodeURIComponent(state.mission)}`, {cycle: 1, state: 'running'}),
          maybe('/api/studio/machines', {machines: []}),
          maybe(`/api/studio/supervisor/${encodeURIComponent(state.mission)}`,
                {mandate: null, steps: [], summary: ''}),
        ]);
      if (generation !== state.generation) return;
      byId('start-blocked').hidden = Boolean(first.can_start);
      byId('create-game').disabled = creating || !local.can_start;
      byId('create-game').title = local.summary;
      byId('create-readiness').textContent = local.model?say('studio.create.selected',{model:local.model}):local.summary;
      showPlan(plan);
      showQuestions(decisions, tools, money);
      showMoney(money);
      showTeam(roster);
      showLoop(cycles);
      showMachines(machines);
      showRun(run);
      byId('summary').textContent = selectedGame ? `${selectedGame.title} · ${selectedGame.summary}` : first.can_start
        ? (plan.next || say('studio.plan.empty'))
        : first.summary;
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

  function actor() {
    return byId('actor').value.trim();
  }

  async function act(action) {
    const result = byId('loop-result');
    if (!actor()) {
      result.textContent = say('studio.loop.need-name');
      byId('actor').focus();
      return;
    }
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
    const who = byId('run-actor').value.trim();
    if (!who) {
      result.textContent = say('studio.loop.need-name');
      byId('run-actor').focus();
      return;
    }
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
          actor: who, steps: ['plan'],
          ceiling: Number.isFinite(ceiling) && ceiling >= 0 ? ceiling : 0,
        });
      } else {
        await post(`/api/studio/supervisor/${mission}/revoke`, {actor: who});
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

  function start() {
    byId('updates-check').onclick=checkUpdate;
    if (query.get('mission')) selectGame(query.get('mission'));
    byId('games-previous').onclick=()=>{libraryOffset=Math.max(0,libraryOffset-6);load();};
    byId('games-more').onclick=()=>{libraryOffset+=6;load();};
    byId('planning-recover').onclick=async()=>{
      const game=selectedGame;if(!game?.can_recover||recovering)return;
      recovering=true;byId('planning-recover').disabled=true;
      const binding=game.mission_key+game.checkpoint;
      if(recoveryCommand?.binding!==binding)recoveryCommand={binding,id:crypto.randomUUID()};
      byId('recovery-result').textContent=say('studio.recovery.checking');
      try{
        await post(`/api/studio/games/${game.mission_id}/recover`,{command_id:recoveryCommand.id,checkpoint:game.checkpoint});
        recoveryCommand=null;byId('recovery-result').textContent=say('studio.recovery.accepted');await load();
      }catch(error){byId('recovery-result').textContent=error.message;}
      finally{recovering=false;byId('planning-recover').disabled=false;}
    };
    byId('planning-stop').onclick=async()=>{
      const game=selectedGame;if(!game)return;
      byId('planning-stop').disabled=true;
      try{await post(`/api/studio/games/${game.mission_id}/stop`,{});await load();}
      catch(error){byId('planning-status').textContent=say('studio.error',{message:error.message});}
      finally{byId('planning-stop').disabled=false;}
    };
    byId('create-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (creating || byId('create-game').disabled) return;
      creating = true;
      byId('create-game').disabled = true;
      byId('game-title').disabled=true;byId('game-idea').disabled=true;
      createCommand = createCommand || crypto.randomUUID();
      try {
        const answer = await post('/api/studio/create', {
          command_id: createCommand, title: byId('game-title').value.trim(),
          idea: byId('game-idea').value.trim(),
        });
        selectGame(answer.mission_key);libraryOffset=0;
        byId('game-title').value='';byId('game-idea').value='';
        byId('create-result').textContent = say('studio.create.queued');
        createCommand = null;
        await load();
        byId('planning-progress').scrollIntoView({block:'start',behavior:'smooth'});
      } catch (error) {
        byId('create-result').textContent = say('studio.error', {message: error.message});
      } finally {
        creating = false;
        byId('game-title').disabled=false;byId('game-idea').disabled=false;
        await load();
      }
    });
    for (const id of ['game-title', 'game-idea']) {
      byId(id).addEventListener('input', () => { if (!creating) createCommand = null; });
    }
    byId('refresh').addEventListener('click', () => load());
    byId('mission').addEventListener('change', () => {
      state.offering = null;
      selectGame(byId('mission').value.trim());
      load();
    });
    byId('pause').addEventListener('click', () => act('pause'));
    byId('resume').addEventListener('click', () => act('resume'));
    byId('send').addEventListener('click', () => act('send'));
    byId('run-grant').addEventListener('click', () => delegate('grant'));
    byId('run-revoke').addEventListener('click', () => delegate('revoke'));
    loadMessages().then(()=>{loadUpdateInfo();return load();}).catch(error => {
      byId('summary').textContent = say('studio.error', {message: error.message});
    });
    setInterval(() => { if (!document.hidden && pendingLoads === 0) load(); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
