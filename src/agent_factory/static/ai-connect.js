const $ = id => document.getElementById(id);
const descriptions = {
  codex: 'Ваш ChatGPT або наявний вхід у Codex. Ключ API зазвичай не потрібний.',
  gemini: 'Ваш Google-акаунт або наявний вхід у Gemini. Вхід відкриється лише за потреби.',
  ollama: 'AI на вашому комп’ютері. Без акаунта й ключів; модель підготуємо автоматично.'
};
const icons = {codex:'✳',gemini:'✦',ollama:'⌘'};
let providers = [], active = null, timer = null, generation = 0, starting = false;
const ongoing = job => ['queued','running','login'].includes(job?.status);
function text(tag, value, className='') { const node=document.createElement(tag);node.textContent=value;node.className=className;return node; }
async function api(path, options={}) {
  const result=await fetch(path,{cache:'no-store',...options});
  if (!result.ok) throw new Error(result.status===401?'Увійдіть у Lokvetia, щоб підключити AI.':result.status===409?'Інше підключення вже виконується. Оновлюємо стан…':result.status===403?'Підключення доступне власнику локальної студії.':'Не вдалося зв’язатися зі студією. Спробуйте ще раз.');
  return result.json();
}
function cards() {
  $('providers').replaceChildren();$('providers').setAttribute('aria-busy','false');
  for (const item of providers) {
    const card=text('article','',`card${active?.provider===item.id?' selected':''}`);
    const top=text('div','','card-top');
    const connected=!!item.connection;
    top.append(text('span',icons[item.id],`icon ${item.id}`),text('span',connected?'Підключено':item.installed?'Знайдено на ПК':'Налаштуємо автоматично',`badge${connected?' connected':''}`));
    const needsKey=item.id==='gemini'&&item.job?.step==='key';
    const button=text('button',needsKey?'Ввести ключ':connected?'Перевірити знову':'Підключити');button.type='button';button.dataset.provider=item.id;button.disabled=starting||ongoing(active);
    button.onclick=()=>{if(needsKey){show(item.job);$('gemini-key').focus();}else start(item.id);};
    card.append(top,text('h2',item.title),text('p',descriptions[item.id]),button);
    if(connected){const remove=text('button','Відключити','quiet disconnect');remove.type='button';remove.disabled=ongoing(active)||starting;remove.onclick=async()=>{try{await api(`/api/ai-setup/connections/${item.id}`,{method:'DELETE',headers:{'X-Agent-Factory-Confirm':'true'}});active=null;$('journey').hidden=true;await refresh();}catch(error){$('notice').textContent=error.message;}};card.append(remove);}
    $('providers').append(card);
  }
  $('ready-banner').hidden=!providers.some(item=>item.connection)||ongoing(active);
}
function show(job) {
  active=job;cards();$('journey').hidden=false;
  $('journey-title').textContent=providers.find(item=>item.id===job.provider)?.title||'Підключення AI';
  $('progress').textContent=job.message;
  const ready=job.status==='ready';const running=ongoing(job);
  $('journey-icon').textContent=ready?'✓':running?'◌':'↻';
  $('activity').hidden=!running;$('cancel').hidden=!running;$('retry').hidden=running||ready;$('done').hidden=!ready;
  $('key-form').hidden=job.provider!=='gemini'||!['key','auth_failed'].includes(job.step);
  if($('key-form').hidden)$('gemini-key').value='';
  if(!$('key-form').hidden)$('retry').hidden=true;
  $('login-link').hidden=!job.login_url;$('login-note').hidden=job.status!=='login';
  $('login-link').removeAttribute('href');
  $('install-link').hidden=job.step!=='missing';
  $('install-link').href={codex:'https://learn.chatgpt.com/docs/cli',gemini:'https://geminicli.com/docs/get-started/installation/',ollama:'https://ollama.com/download/windows'}[job.provider]||'#';
  if (job.login_url) {
    try { const url=new URL(job.login_url); const allowed=job.provider==='codex'?['auth.openai.com','auth0.openai.com','chatgpt.com']:['accounts.google.com'];
      if (url.protocol==='https:'&&allowed.includes(url.hostname)&&!url.username&&!url.password) $('login-link').href=url.href;
      else $('login-link').hidden=true;
    } catch {$('login-link').hidden=true;}
  }
  $('notice').textContent=ready?'AI підключений. Можна продовжувати.':running?'Можна залишити цю сторінку відкритою — ми покажемо результат.':'';
}
async function refresh() {
  const token=++generation;
  try {
    const result=await api('/api/ai-setup');if(token!==generation)return;
    $('remote').hidden=result.local;$('providers').hidden=!result.local;
    if(!result.local){$('notice').textContent='';return;}
    providers=result.providers;cards();
    const running=providers.map(item=>item.job).find(ongoing);
    if(running){show(running);schedule();}
    else if(active){const latest=providers.find(item=>item.id===active.provider)?.job;if(latest)show(latest);}
    else $('notice').textContent='Для початку достатньо одного AI. Решту можна додати пізніше.';
  } catch(error){if(token===generation)$('notice').textContent=error.message;}
}
async function start(provider, secret=null) {
  if(starting||ongoing(active))return;
  starting=true;cards();$('notice').textContent='Готуємо підключення…';
  try {
    const body={provider,command_id:crypto.randomUUID()};if(secret!==null)body.secret=secret;
    secret=null;$('gemini-key').value='';
    const pending=api('/api/ai-setup',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Factory-Confirm':'true'},body:JSON.stringify(body)});
    delete body.secret;
    const job=await pending;
    show(job);$('journey').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'nearest'});schedule();
  } catch(error){await refresh();if(!ongoing(active))$('notice').textContent=error.message;}
  finally{starting=false;cards();}
}
function schedule(){clearTimeout(timer);if(ongoing(active))timer=setTimeout(poll,1200);}
async function poll(){
  const id=active?.id;if(!id)return;
  try {const job=await api(`/api/ai-setup/${encodeURIComponent(id)}`);if(active?.id!==id)return;show(job);if(!ongoing(job))await refresh();}
  catch(error){$('progress').textContent=error.message+' Відновлюємо з’єднання…';}
  schedule();
}
$('cancel').onclick=async()=>{if(!active)return;try{show(await api(`/api/ai-setup/${encodeURIComponent(active.id)}`,{method:'DELETE',headers:{'X-Agent-Factory-Confirm':'true'}}));clearTimeout(timer);}catch(error){$('progress').textContent=error.message;}};
$('retry').onclick=()=>active&&start(active.provider);
$('key-form').onsubmit=event=>{event.preventDefault();const value=$('gemini-key').value;$('gemini-key').value='';start('gemini',value);};
window.addEventListener('pagehide',()=>{clearTimeout(timer);$('gemini-key').value='';});
window.addEventListener('pageshow',event=>{if(event.persisted)refresh();});
refresh();
