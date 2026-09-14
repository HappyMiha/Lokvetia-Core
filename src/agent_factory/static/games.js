'use strict';
const $=id=>document.getElementById(id), labels=['Ідея','AI','Підготовка','Створення','Грати'];
const pages={games:0,missions:0,versions:0}, generations={games:0,missions:0,versions:0};
let current=null, project=null, step=0, dirty=false, timer=null, saveChain=Promise.resolve(), pendingSave=null, pendingCreate=null, readinessTimer=null, readinessGeneration=0;
const uid=()=>crypto.randomUUID();
const messages={idea_required:'Напишіть ідею, перш ніж продовжити.',model_required:'Оберіть модель або збережіть ідею та поверніться пізніше.',model_unavailable:'Модель більше не доступна в конфігурації. Перевірте налаштування AI.',newer_version:'Інша вкладка зберегла новішу версію. Ваш текст залишився на екрані; скопіюйте його й відкрийте останню версію.',source_already_submitted:'Вихідну чернетку вже передано Core. Створіть нову ідею для іншого початкового задуму.',intake_pending:'Збереження входу Core було перервано. Повторіть збереження чернетки: та сама команда відновить його.',confirmation_required:'Потрібне явне підтвердження збереження чернетки.'};
function note(message){$('notice').textContent=messages[message]||message||'';}
function element(tag,value){const node=document.createElement(tag);if(value!==undefined)node.textContent=value;return node;}
async function api(url,body,confirm=false){
 const response=await fetch(url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json',...(confirm?{'X-Agent-Factory-Confirm':'true'}:{})}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});
 const data=await response.json();
 if(response.status===401){$('title').value='';$('idea').value='';current=null;project=null;dirty=false;location.assign('/login');throw new Error('Потрібно знову увійти.');}
 if(!response.ok)throw new Error(typeof data.detail==='string'?(messages[data.detail]||data.detail):'Запит не виконано. Перевірте введені дані.');
 return data;
}
async function action(fn){try{return await fn();}catch(error){note(error.message||'Не вдалося зберегти. Ваш текст залишається на екрані.');return null;}}
function pager(name,data){const holder=$(name+'-prev').parentElement;holder.classList.toggle('idle',!data.total);$(name+'-prev').disabled=pages[name]===0;$(name+'-next').disabled=pages[name]+data.items.length>=data.total;$(name+'-page').textContent=data.total?`${pages[name]+1}–${Math.min(pages[name]+data.items.length,data.total)} із ${data.total}`:'Немає записів';}
async function list(name){
 const generation=++generations[name],query=$(name==='games'?'game-query':'mission-query').value;
 const data=await api(`/api/games/${name==='games'?'starts':'missions'}?offset=${pages[name]}&limit=20&q=${encodeURIComponent(query)}`);
 if(generation!==generations[name])return;
 const container=$(name==='games'?'game-list':'mission-list');container.replaceChildren();
 for(const item of data.items){const card=element('article');card.className='card';card.append(element('h2',item.title));
  card.append(element('p',name==='games'?`Збережено · крок «${labels[item.view_step]}»`:phase(item.phase)));
  if(name==='missions'){card.append(element('p',progressText(item)),progressBar(item),element('p',nextAction(item)));}
  card.append(element('p',workingVersion(item)));
  const button=element('button','Продовжити');button.onclick=()=>action(()=>openItem(name,name==='games'?item.id:item.mission_id));card.append(button);container.append(card);}
 if(!data.items.length){const empty=element('div');empty.className='empty';
  empty.append(element('strong',query?'За цим пошуком нічого не знайдено.':'Тут з’являться ваші ігри.'));
  if(!query)empty.append(element('span','Натисніть «Створити гру» і опишіть задум своїми словами.'));
  container.append(empty);}
 pager(name,data);
}
function phase(value){return {DRAFT:'Чернетка: потрібен план',SPECIFICATION_ANALYSIS:'Розбір специфікації',BACKLOG_GENERATION:'Складання плану',WAITING_FOR_BACKLOG_APPROVAL:'План очікує затвердження',APPROVED:'План затверджено',ENVIRONMENT_DISCOVERY:'Перевірка середовища',ENVIRONMENT_BOOTSTRAP:'Підготовка середовища',DEVELOPMENT:'Створення',VALIDATION:'Перевірка результату',INTEGRATION:'Збирання разом',FINAL_VALIDATION:'Підсумкова перевірка',COMPLETED:'Робочий процес завершено'}[value]||'Потрібна перевірка поточного стану';}
// One wording for the project state, used by the list and by the open project.
const ACTIONS={prepare_plan:'Наступна дія: підготуйте й затвердьте план у Core.',approve_plan:'Наступна дія: затвердьте план у Core.',inspect_readiness:'Наступна дія: перевірте актуальний стан середовища.',resolve_blocked_work:'Наступна дія: розберіться із задачами, що зупинилися.',review_result:'Наступна дія: перегляньте результат і підтвердьте його.'};
function nextAction(item){return item?(ACTIONS[item.next_action]||'Наступна дія: перевірте поточний стан проєкту.'):'Наступна дія: збережіть ідею, оберіть модель і підготуйте проєкт.';}
function progressText(item){const state=item&&item.progress;
 if(!state||!state.total)return 'Задач у плані ще немає.';
 const parts=[`прийнято ${state.accepted} з ${state.total}`];
 if(state.finished)parts.push(`виконано ${state.finished}`);
 if(state.in_progress)parts.push(`в роботі ${state.in_progress}`);
 if(state.blocked)parts.push(`зупинено ${state.blocked}`);
 return parts.join(' · ');}
function progressBar(item){const state=item&&item.progress,share=state&&state.total?state.accepted_share:0;
 const rail=element('div');rail.className='rail';const bar=element('div');bar.className='bar';bar.style.width=share+'%';
 rail.append(bar);rail.setAttribute('role','img');rail.setAttribute('aria-label',`Прийнято ${share}% задач плану`);return rail;}
// Playability is a separate fact from progress and is never inferred from it.
function workingVersion(item){return item&&item.working_version&&item.working_version.available?'Є перевірена ігрова версія.':'Перевіреної ігрової версії ще немає.';}
function values(){return {title:$('title').value,idea:$('idea').value,model_key:$('model').value};}
function render(fill=true){
 clearTimeout(readinessTimer);++readinessGeneration;
 $('home').hidden=true;$('editor').hidden=false;
 const locked=!current||current.source_locked;
 if(fill&&current){$('title').value=current.title;$('idea').value=current.idea;
  if(current.model_key&&!Array.from($('model').options).some(o=>o.value===current.model_key)){const option=element('option',current.model_key+' · перевірте конфігурацію');option.value=current.model_key;$('model').append(option);}
  $('model').value=current.model_key;}
 $('title').disabled=locked;$('idea').disabled=locked;$('model').disabled=locked;
 $('game-title').textContent=current?current.title:project.title;
 $('steps').replaceChildren();labels.forEach((label,index)=>{const button=element('button',`${index+1}. ${label}`);button.setAttribute('aria-current',index===step?'step':'false');button.onclick=()=>action(()=>goStep(index));$('steps').append(button);});
 document.querySelectorAll('[data-step]').forEach(node=>node.hidden=Number(node.dataset.step)!==step);
 $('previous-step').disabled=step===0;$('next-step').disabled=step===4;$('save-draft').hidden=!current;
 $('refresh-project').hidden=!project;
 $('submit-draft').hidden=!current||Boolean(current.mission_id);$('check-environment').hidden=!project;
 $('history').hidden=!current;$('technical').textContent=JSON.stringify(current||project,null,2);
 $('save-state').textContent=current?`Збережена версія ${current.revision}${dirty?' · є незбережені зміни':''}`:'Наявний проєкт Core';
 $('progress-state').textContent=project?`${phase(project.phase)}. ${progressText(project)}. ${workingVersion(project)} Остання зміна проєкту: ${project.updated_at}. Час завершення невідомий.`:'Проєкт ще не передано Core. Поверніться до підготовки.';
 $('preparation-copy').textContent=project?'Вихідна чернетка збережена. Для виконання потрібні затверджений план і актуальний звіт середовища. Перегляньте поточний прогрес на кроці «Створення».':'Збережіть вихідну ідею та обрану модель як чернетку Core. Це не затверджує план або запуск.';
 const previousPlanLink=$('plan-link');if(previousPlanLink)previousPlanLink.remove();
 if(project){const link=element('a','Відкрити чернетку плану гри');link.id='plan-link';link.href='/planning/'+encodeURIComponent(project.mission_id);$('preparation-copy').after(link);}
 $('environment-state').textContent='';
 $('next-action').textContent=nextAction(project);
 if(current&&current.error_code)note(current.error_code);
}
async function openItem(kind,id){
 clearTimeout(timer);await saveChain;if(dirty){note('Спочатку збережіть зміни або перезавантажте сторінку, щоб їх відкинути.');return;}
 if(kind==='games'){current=await api('/api/games/starts/'+encodeURIComponent(id));project=current.project;step=current.view_step;location.hash='start/'+id;}
 else{project=await api('/api/games/missions/'+encodeURIComponent(id));current=null;step=2;location.hash='mission/'+id;$('title').value=project.title;$('idea').value='Вихідна специфікація збережена в Core.';$('model').value='';}
 pendingSave=null;pages.versions=0;render();note(current?.error_code||'');
}
function persist(nextStep=step){
 if(!current)return Promise.resolve(true);
 clearTimeout(timer);
 const ident=current.id;
 saveChain=saveChain.catch(()=>false).then(async()=>{
  if(!current||current.id!==ident)return false;
  const fields=values();
  // On an ambiguous network failure, retry the same immutable request first.
  const request=pendingSave||{command_id:uid(),expected_revision:current.revision,...fields,view_step:nextStep};
  pendingSave=request;
  try{
   const result=await api('/api/games/starts/'+ident+'/save',request);
   if(!current||current.id!==ident)return false;
   const hasNewInput=JSON.stringify(values())!==JSON.stringify({title:request.title,idea:request.idea,model_key:request.model_key});
   current=result;project=result.project;pendingSave=null;step=result.view_step;dirty=hasNewInput;
   render(!hasNewInput);note(result.error_code||'Зміни збережено.');
   if(hasNewInput)timer=setTimeout(()=>action(()=>persist(step)),350);
   return !result.error_code;
  }catch(error){dirty=true;$('save-state').textContent='Збереження не підтверджено. Текст залишається на екрані; повторіть «Зберегти».';note(error.message);return false;}
 });
 return saveChain;
}
async function goStep(index){if(current){if(!await persist(index))return;}else{step=index;render(false);}if(step===2&&project)await readiness(false);}
async function readiness(refresh){
 if(!project)return;
 clearTimeout(readinessTimer);const generation=++readinessGeneration,ident=project.mission_id;
 $('environment-state').textContent='Перевірка стану середовища…';
 try{
  const report=await api(`/api/autonomous-missions/${ident}/environment${refresh?'/check':''}`,refresh?{}:undefined);
  if(!project||project.mission_id!==ident||generation!==readinessGeneration)return;
  const remaining=Date.parse(report.expires_at)-Date.now();
  const ready=report.status==='ready'&&report.mode==='live'&&remaining>0&&remaining<=300000&&Array.isArray(report.checks)&&report.checks.length&&report.checks.every(check=>check.ready===true);
  $('environment-state').textContent=ready?'Перевірки середовища чинні. Це не підтверджує готовність гри.':'Середовище ще не підтверджено. Перегляньте необхідні дії в панелі оператора.';
  if(ready)readinessTimer=setTimeout(()=>{if(generation===readinessGeneration)$('environment-state').textContent='Термін перевірки минув. Перевірте середовище знову.';},remaining);
 }catch(error){if(generation===readinessGeneration)$('environment-state').textContent='Актуальний стан недоступний. Повторіть перевірку.';throw error;}
}
async function versions(){
 if(!current)return;const ident=current.id,generation=++generations.versions;
 const data=await api(`/api/games/starts/${ident}/versions?offset=${pages.versions}&limit=20&q=${encodeURIComponent($('version-query').value)}`);
 if(!current||current.id!==ident||generation!==generations.versions)return;
 $('version-list').replaceChildren();for(const item of data.items){const button=element('button',`Версія ${item.revision} · ${item.title}`);button.onclick=()=>{$('version-preview').hidden=false;$('version-preview').textContent=`${item.title}\n\n${item.idea}\n\nЗбережено: ${item.updated_at}`;};$('version-list').append(button);}pager('versions',data);
}
$('create-game').onclick=()=>action(async()=>{const button=$('create-game');button.disabled=true;try{const result=await api('/api/games/starts',{command_id:pendingCreate||(pendingCreate=uid())});pendingCreate=null;await openItem('games',result.id);}finally{button.disabled=false;}});
for(const id of ['title','idea','model'])$(id).addEventListener(id==='model'?'change':'input',()=>{dirty=true;$('save-state').textContent='Є незбережені зміни…';clearTimeout(timer);timer=setTimeout(()=>action(()=>persist(step)),500);});
$('save-draft').onclick=()=>action(()=>persist(step));$('previous-step').onclick=()=>action(()=>goStep(Math.max(0,step-1)));$('next-step').onclick=()=>action(()=>goStep(Math.min(4,step+1)));
$('back-home').onclick=()=>action(async()=>{clearTimeout(timer);if(current&&dirty&&!await persist(step))return;await saveChain;if(dirty)return;clearTimeout(readinessTimer);++readinessGeneration;current=null;project=null;dirty=false;location.hash='';$('editor').hidden=true;$('home').hidden=false;await list('games');});
$('submit-draft').onclick=()=>action(async()=>{if(!await persist(2))return;$('confirm').returnValue='cancel';$('confirm').showModal();});
$('confirm').addEventListener('close',()=>{if($('confirm').returnValue!=='confirm'||!current)return;action(async()=>{const ident=current.id;$('submit-draft').disabled=true;try{$('title').disabled=true;$('idea').disabled=true;$('model').disabled=true;const result=await api('/api/games/starts/'+ident+'/submit',{command_id:uid(),expected_revision:current.revision,confirmed:true},true);if(!current||current.id!==ident)return;current=result;project=result.project;dirty=false;render();note('Чернетка Core збережена. Виконання не почалося.');}catch(error){if(current?.id===ident){const saved=await api('/api/games/starts/'+ident);current=saved;project=saved.project;render();}throw error;}finally{$('submit-draft').disabled=false;}});});
$('check-environment').onclick=()=>action(()=>readiness(true));
for(const [name,form] of [['games','game-search'],['missions','mission-search'],['versions','version-search']]){
 $(form).onsubmit=event=>{event.preventDefault();pages[name]=0;action(()=>name==='versions'?versions():list(name));};
 $(name+'-prev').onclick=()=>{pages[name]=Math.max(0,pages[name]-20);action(()=>name==='versions'?versions():list(name));};
 $(name+'-next').onclick=()=>{pages[name]+=20;action(()=>name==='versions'?versions():list(name));};
}
$('history').addEventListener('toggle',()=>{if($('history').open)action(versions);});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
action(async()=>{await Promise.all([list('games'),list('missions')]);try{const models=await api('/api/games/models');for(const item of models.items){const option=element('option',item.model+' · '+item.provider+' (конфігурація)');option.value=item.key;$('model').append(option);}if(!models.items.length)$('model-note').textContent='Сумісну локальну модель ще не налаштовано. Ідею можна зберегти й продовжити пізніше.';}catch(error){$('model-note').textContent='Не вдалося прочитати конфігурацію моделей. Ідея залишається доступною.';}const [kind,id]=location.hash.slice(1).split('/');if(id&&['start','mission'].includes(kind))await openItem(kind==='start'?'games':'missions',id);});

$('reload-draft').onclick=()=>action(async()=>{
 if(!current)return;clearTimeout(timer);await saveChain;
 const unsaved=dirty?values():null, result=await api('/api/games/starts/'+current.id);
 current=result;project=result.project;step=result.view_step;dirty=false;pendingSave=null;render();
 if(unsaved){$('recovery-copy').hidden=false;$('recovery-copy').textContent='Ваш незбережений текст (скопіюйте потрібне):\n'+unsaved.title+'\n\n'+unsaved.idea;}
 note('Відкрито останню збережену версію.');
});

$('refresh-project').onclick=()=>action(async()=>{
 if(!project)return;const ident=project.mission_id;
 const latest=await api('/api/games/missions/'+ident);
 if(!project||project.mission_id!==ident)return;
 project=latest;if(current)current.project=latest;render(false);note('Стан проєкту оновлено.');
});
