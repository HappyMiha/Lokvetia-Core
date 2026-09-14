'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const state = {sections: [], pending: null, language: 'uk', messages: {}};
  const query = new URLSearchParams(location.search);

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
    document.title = `${say('settings.title')} · ${say('app.name')}`;
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

  function withLanguage(url) {
    return `${url}${url.includes('?') ? '&' : '?'}lang=${encodeURIComponent(state.language)}`;
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function actor() {
    return byId('actor').value.trim();
  }

  async function readJson(response) {
    try { return await response.json(); } catch { return {}; }
  }

  async function post(url, body) {
    const response = await fetch(withLanguage(url), {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Agent-Factory-Confirm': 'true'},
      body: JSON.stringify(Object.assign({confirmed: true}, body)),
    });
    const payload = await readJson(response);
    if (!response.ok || payload.error) {
      const message = payload.error ? payload.error.message : `Помилка ${response.status}`;
      const error = new Error(message);
      error.code = payload.error ? payload.error.code : 'http_error';
      throw error;
    }
    return payload;
  }

  function control(field) {
    // Every generated control carries its own accessible name; the visible
    // heading above it is not programmatically associated with it.
    if (field.kind === 'boolean') {
      const input = element('input');
      input.type = 'checkbox';
      input.checked = field.value === 'true';
      input.dataset.read = 'checkbox';
      input.setAttribute('aria-label', field.label);
      return input;
    }
    if (field.kind === 'choice') {
      const select = element('select');
      select.setAttribute('aria-label', field.label);
      for (const option of field.choices) {
        const node = element('option', option);
        node.value = option;
        if (option === field.value) node.selected = true;
        select.append(node);
      }
      select.dataset.read = 'value';
      return select;
    }
    const input = element('input');
    input.type = field.kind === 'integer' || field.kind === 'decimal' ? 'number' : 'text';
    if (field.minimum !== null && field.minimum !== undefined) input.min = field.minimum;
    if (field.maximum !== null && field.maximum !== undefined) input.max = field.maximum;
    if (field.kind === 'decimal') input.step = 'any';
    input.value = field.value;
    input.dataset.read = 'value';
    input.setAttribute('aria-label', field.label);
    if (field.kind === 'list') input.placeholder = say('settings.reason.placeholder');
    return input;
  }

  function readControl(node) {
    return node.dataset.read === 'checkbox' ? String(node.checked) : node.value.trim();
  }

  function badges(field) {
    const wrap = element('span');
    wrap.append(element('span',
      say(field.origin === 'override' ? 'settings.badge.changed' : 'settings.badge.default'),
      field.origin === 'override' ? 'badge badge-changed' : 'badge badge-default'));
    if (field.risk === 'sensitive') {
      wrap.append(' ', element('span', say('settings.badge.sensitive'), 'badge badge-sensitive'));
    }
    if (!field.reconfigurable) {
      wrap.append(' ', element('span', say('settings.badge.locked'), 'badge badge-locked'));
    }
    return wrap;
  }

  function originLine(field) {
    const parts = [
      say('settings.origin.default', {value: field.default || '—'}),
      say('settings.origin.source', {source: field.default_source}),
    ];
    if (field.unit) parts.push(say('settings.origin.unit', {unit: field.unit}));
    if (field.origin === 'override' && field.changed_by) {
      parts.push(say('settings.origin.changed_by', {actor: field.changed_by})
        + (field.changed_at ? ` · ${field.changed_at}` : ''));
      if (field.changed_reason) {
        parts.push(say('settings.origin.reason', {reason: field.changed_reason}));
      }
    }
    return element('p', parts.join(' · '), 'origin');
  }

  function renderField(field) {
    const node = element('div', undefined, 'field');
    const head = element('div', undefined, 'field-head');
    head.append(element('strong', field.label), badges(field));
    node.append(head, element('p', field.help));
    if (field.risk === 'sensitive' && field.consequence) {
      node.append(element(
        'p', say('settings.consequence', {consequence: field.consequence}), 'origin',
      ));
    }
    node.append(originLine(field));

    const status = element('p', '', 'field-ok');
    if (!field.reconfigurable) {
      const shown = element('p', say('settings.current', {value: field.value}));
      node.append(shown);
      return node;
    }

    const controls = element('div', undefined, 'controls');
    const input = control(field);
    const reason = element('input');
    reason.type = 'text';
    reason.className = 'reason';
    reason.maxLength = 300;
    reason.placeholder = say('settings.reason.placeholder');
    reason.setAttribute('aria-label', say('settings.reason.label', {label: field.label}));
    const save = element('button', say('settings.save'));
    save.type = 'button';
    save.setAttribute('aria-label', say('settings.save.label', {label: field.label}));
    const reset = element('button', say('settings.reset'));
    reset.type = 'button';
    reset.setAttribute('aria-label', say('settings.reset.label', {label: field.label}));
    reset.disabled = field.origin !== 'override';
    controls.append(input, reason, save, reset);
    node.append(controls, status);

    const report = (text, ok) => {
      status.textContent = text;
      status.className = ok ? 'field-ok' : 'field-error';
    };

    save.addEventListener('click', async () => {
      if (!actor()) { report(say('settings.need_actor'), false); byId('actor').focus(); return; }
      const value = readControl(input);
      const body = {value, actor: actor(), reason: reason.value.trim(), acknowledged_consequence: false};
      try {
        await post(`/api/settings/values/${encodeURIComponent(field.key)}`, body);
        await load(say('settings.saved', {label: field.label}));
      } catch (error) {
        if (error.code === 'confirmation_required') {
          askConsequence(field, () => post(
            `/api/settings/values/${encodeURIComponent(field.key)}`,
            Object.assign({}, body, {acknowledged_consequence: true, reason: state.pending.reason}),
          ));
          return;
        }
        report(error.message, false);
      }
    });

    reset.addEventListener('click', async () => {
      if (!actor()) { report(say('settings.need_actor'), false); byId('actor').focus(); return; }
      const body = {actor: actor(), reason: reason.value.trim(), acknowledged_consequence: false};
      try {
        await post(`/api/settings/values/${encodeURIComponent(field.key)}/reset`, body);
        await load(say('settings.restored', {label: field.label}));
      } catch (error) {
        if (error.code === 'confirmation_required') {
          askConsequence(field, () => post(
            `/api/settings/values/${encodeURIComponent(field.key)}/reset`,
            Object.assign({}, body, {acknowledged_consequence: true, reason: state.pending.reason}),
          ));
          return;
        }
        report(error.message, false);
      }
    });
    return node;
  }

  function askConsequence(field, apply) {
    const dialog = byId('confirm');
    state.pending = {apply, reason: ''};
    byId('confirm-setting').textContent = field.label;
    byId('confirm-consequence').textContent = field.consequence;
    byId('confirm-reason').value = '';
    byId('confirm-ack').checked = false;
    dialog.showModal();
  }

  function findingList(findings) {
    const list = element('ul', undefined, 'findings');
    for (const finding of findings) {
      const item = element('li', undefined, `level-${finding.level}`);
      item.append(element('span', finding.summary));
      if (finding.detail) item.append(element('span', finding.detail, 'detail'));
      list.append(item);
    }
    return list;
  }

  // Eight sections and forty settings in one column is a wall, not a screen.
  // Each section is folded shut and says in one line what is inside, so a person
  // opens the one they came for instead of scrolling past the seven they did not.
  function renderSection(section) {
    const node = element('details', undefined, 'section-card');
    const head = element('summary', undefined, 'section-head');
    const title = element('h2', section.title);
    const count = element('span', say('settings.count', {
      count: section.fields.length,
      changed: section.changed_count || 0,
    }), 'origin');
    head.append(title, count);
    node.append(head);
    const verify = element('button', say('settings.verify'));
    verify.type = 'button';
    verify.setAttribute('aria-label', say('settings.verify.label', {title: section.title}));
    node.append(element('p', section.summary), verify);
    if (section.changed_count) {
      node.append(element(
        'p', say('settings.changed_count', {count: section.changed_count}), 'origin',
      ));
    }
    const findings = findingList(section.findings);
    node.append(findings);
    verify.addEventListener('click', async () => {
      verify.disabled = true;
      try {
        const payload = await post(`/api/settings/sections/${encodeURIComponent(section.section)}/verify`, {});
        findings.replaceWith(findingList(payload.findings));
      } finally {
        verify.disabled = false;
      }
    });
    for (const field of section.fields) node.append(renderField(field));
    return node;
  }

  function renderHistory(changes) {
    const body = byId('history-rows');
    body.replaceChildren();
    byId('history-empty').hidden = changes.length > 0;
    for (const change of changes) {
      const row = element('tr');
      row.append(
        element('td', change.created_at),
        element('td', change.key),
        element('td', `${change.previous_value} → ${change.new_value}`),
        element('td', change.actor),
        element('td', change.reason || '—'),
      );
      body.append(row);
    }
  }

  async function load(message) {
    const response = await fetch(withLanguage('/api/settings/sections'), {cache: 'no-store'});
    if (!response.ok) {
      byId('summary').textContent = say('settings.read_failed', {status: response.status});
      return;
    }
    const payload = await response.json();
    state.sections = payload.sections;
    const host = byId('sections');
    host.replaceChildren();
    for (const section of payload.sections) host.append(renderSection(section));
    const changed = payload.changed_total;
    byId('summary').textContent = (message ? `${message} ` : '')
      + (changed
        ? say('settings.summary.changed', {count: changed})
        : say('settings.summary.clean'));
    await loadHistory();
  }

  async function loadHistory() {
    const response = await fetch(withLanguage('/api/settings/changes?limit=50'), {cache: 'no-store'});
    if (!response.ok) return;
    const payload = await response.json();
    renderHistory(payload.changes || []);
  }

  byId('confirm').addEventListener('close', async () => {
    const dialog = byId('confirm');
    const pending = state.pending;
    state.pending = null;
    if (dialog.returnValue !== 'confirm' || !pending) return;
    if (!byId('confirm-ack').checked) {
      byId('summary').textContent = say('settings.not_applied');
      return;
    }
    pending.reason = byId('confirm-reason').value.trim();
    state.pending = pending;
    try {
      await pending.apply();
      await load(say('settings.applied'));
    } catch (error) {
      byId('summary').textContent = error.message;
    } finally {
      state.pending = null;
    }
  });

  byId('who-form').addEventListener('submit', event => event.preventDefault());
  byId('refresh-history').addEventListener('click', loadHistory);
  loadMessages().then(load);
})();
