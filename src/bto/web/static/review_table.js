/* Handle Review table controls, queue Undo and the first-seen calendar.
   Keep browsing membership in this session and read/star/save state on the server. */
let reviewTableKept=new Set(new URL(location.href).searchParams.get('table_keep')?.split(',').filter(Boolean)||[]);
let reviewQueueStamp=new Date(), reviewTableFilterSignature=null;
const reviewMonths=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function tableFilterSignature(url) {
  url=new URL(url);for(const [key,value] of Object.entries({markets:'sg,hk,th',buckets:'default',seen:'last_night',show:'unread'}))if(url.searchParams.get(key)===value)url.searchParams.delete(key);
  const values=[...url.searchParams].filter(([key])=>!['page','size','sort','direction','view','id','table_keep','advertiser_scope','advertiser_days'].includes(key)&&url.searchParams.get(key)!=='');
  return JSON.stringify(values.sort((a,b)=>a[0].localeCompare(b[0])));
}
function prepareReviewTable(url) {
  const signature=tableFilterSignature(url);
  if(reviewTableFilterSignature!==null&&signature!==reviewTableFilterSignature) {
    reviewTableKept.clear();url.searchParams.delete('table_keep');
  }
  reviewTableFilterSignature=signature;
  if(url.searchParams.get('view')==='table'&&reviewTableKept.size)url.searchParams.set('table_keep',[...reviewTableKept].join(','));
  else url.searchParams.delete('table_keep');
  return url;
}
function rememberTableRead(id,on) {
  const show=reviewNavigation().show||'unread';
  if(show!=='all'&&on!==(show==='read'))reviewTableKept.add(String(id));
  else reviewTableKept.delete(String(id));
  document.querySelector('[data-row-id="'+CSS.escape(String(id))+'"]')?.classList.toggle('row-read',on);
  updateReviewTable();
}
function updateReviewTable() {
  const root=document.querySelector('[data-review]');if(!root)return;
  renderReviewQuotes();
  const stamp=root.querySelector('[data-queue-snapshot-at]');
  if(stamp) {
    const day=reviewQueueStamp.getDate()+' '+reviewMonths[reviewQueueStamp.getMonth()];
    const time=new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'}).format(reviewQueueStamp);
    stamp.textContent='Snapshot '+day+' · '+time;
  }
  const stale=root.querySelector('[data-table-stale]'), table=new URL(location.href).searchParams.get('view')==='table';
  if(stale){stale.hidden=!table||!reviewTableKept.size;stale.textContent=reviewTableKept.size+' marked '+(reviewNavigation().show==='read'?'unread':'read')+' since';}
  root.querySelector('[data-rebuild-queue]')?.classList.toggle('has-stale',table&&reviewTableKept.size>0);
}
document.addEventListener('click',event=>{
  const rebuild=event.target.closest('[data-rebuild-queue]');
  if(rebuild){if(filterActive||reviewStateBusy)return;reviewTableKept.clear();reviewQueueStamp=new Date();const url=new URL(location.href);url.searchParams.delete('table_keep');url.searchParams.set('page','1');if(url.searchParams.get('view')==='table')url.searchParams.delete('id');refreshReview(url);return;}
  const link=event.target.closest('[data-table-page],[data-table-size],[data-table-sort]')||event.target.closest('.review-table th')?.querySelector('[data-table-sort]');if(!link)return;
  event.preventDefault();if(filterActive||link.getAttribute('aria-disabled')==='true')return;
  const url=new URL(link.href);if(link.dataset.tableSize){try{localStorage.setItem('bto-table-size',link.dataset.tableSize);}catch{}}
  refreshReview(url);
});
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.target.matches('input,select,textarea')||event.target.isContentEditable||event.ctrlKey||event.metaKey||event.altKey)return;
  if(!document.querySelector('[data-view-table]:not([hidden])'))return;
  const name=event.key==='ArrowLeft'?'Previous page':event.key==='ArrowRight'?'Next page':null;
  const link=name&&document.querySelector('[data-table-page][aria-label="'+name+'"]');
  if(link&&link.getAttribute('aria-disabled')!=='true'){event.preventDefault();link.click();}
});
document.addEventListener('change',event=>{
  if(!event.target.matches('[data-saved-filter]'))return;
  const url=new URL(location.href);url.searchParams.delete('id');url.searchParams.delete('archive');
  for(const field of document.querySelectorAll('[data-saved-filter]'))url.searchParams.set(field.name,field.value);
  location.href=url;
});
document.addEventListener('click',event=>{
  const priority=event.target.closest('[data-saved-priority]');if(!priority)return;
  event.preventDefault();const url=new URL(location.href);url.searchParams.delete('id');url.searchParams.delete('archive');
  if(url.searchParams.get('priority')==='1')url.searchParams.delete('priority');else url.searchParams.set('priority','1');
  location.href=url;
});
document.addEventListener('click',event=>{
  const chip=event.target.closest('[data-saved-job-star]');if(!chip)return;
  event.preventDefault();event.stopPropagation();changeReviewState('star',chip.dataset.jobId,chip.dataset.active==='true');
});
document.addEventListener('wheel',event=>{
  const rail=event.target.closest('[data-hscroll]');if(!rail||rail.scrollWidth<=rail.clientWidth||event.deltaX)return;
  event.preventDefault();rail.scrollLeft+=event.deltaY;
},{passive:false});
prepareReviewTable(new URL(location.href));updateReviewTable();
if(document.querySelector('[data-view-table]:not([hidden])')&&!new URL(location.href).searchParams.has('size')){
  let size;try{size=localStorage.getItem('bto-table-size');}catch{}
  if(['25','50','75','100'].includes(size)&&size!=='25'){const url=new URL(location.href);url.searchParams.set('size',size);refreshReview(url);}
}

let lastQueueUndo=null, queueToastTimer;
// A toast replaces the one before it; only the latest can be undone, with Undo or Z.
function queueToast(message,undo=false) {
  document.querySelector('[data-queue-toast]')?.remove();clearTimeout(queueToastTimer);if(!undo)lastQueueUndo=null;
  const toast=document.createElement('div');toast.className='review-toast';toast.dataset.queueToast='';toast.setAttribute('role','status');
  const text=document.createElement('span');text.textContent=message;toast.append(text);
  if(undo){const button=document.createElement('button'),key=document.createElement('span');key.className='toast-key';key.textContent='Z';button.append('Undo ',key);button.dataset.undoQueue='';toast.append(button);}
  document.body.append(toast);queueToastTimer=setTimeout(()=>toast.remove(),6500);
}
async function queueReviewJob(button,withStar=false) {
  if(reviewStateBusy||selectionActive||filterActive)return;
  const id=button.dataset.jobId, active=button.dataset.active==='true';
  if(!active){if(await changeReviewState('save',id,false)){lastQueueUndo={id,saved:true};queueToast('Preparation removed',true);}return;}
  const root=document.querySelector('[data-review]'), nav=reviewNavigation();
  const previous={id,read:root.querySelector('.read-chip')?.dataset.active==='false',star:root.querySelector('.review-actions [data-review-state-action=star]')?.dataset.active==='false'};
  const company=root.querySelector('.advertiser-name b')?.textContent.trim()||'';
  if(!await changeReviewState('queue',id,true,false,{star:withStar}))return;
  lastQueueUndo=previous;reviewReadStack.push(Number(id));reviewTableDirty=true;
  const url=new URL(location.href);url.searchParams.set('id',nav.next_id||id);url.searchParams.set('view','page');
  await selectReview(url);queueToast('Queued to apply'+(company?' · '+company:''),true);
}
document.addEventListener('click',async event=>{
  if(!event.target.closest('[data-undo-queue]')||!lastQueueUndo||reviewStateBusy)return;
  const previous=lastQueueUndo;
  // Undoing a removal saves the job again; undoing a queue also restores its read and star state.
  if(previous.saved){if(!await changeReviewState('save',previous.id,true))return;}
  else if(!await changeReviewState('queue',previous.id,false,false,{restore:{read:previous.read,star:previous.star}}))return;
  lastQueueUndo=null;document.querySelector('[data-queue-toast]')?.remove();clearTimeout(queueToastTimer);
  if(previous.saved)return;
  reviewReadStack=reviewReadStack.filter(id=>String(id)!==String(previous.id));
  const url=new URL(location.href);url.searchParams.set('id',previous.id);url.searchParams.set('view','page');await selectReview(url);
});
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.target.matches('input,select,textarea')||event.target.isContentEditable||event.key.toLowerCase()!=='z')return;
  const undo=document.querySelector('[data-queue-toast] [data-undo-queue]');if(undo){event.preventDefault();undo.click();}
});
// A message kept across a reload, such as To Apply's star message.
try{const words=sessionStorage.getItem('bto-toast');if(words){sessionStorage.removeItem('bto-toast');queueToast(words);}}catch{}

function colourReviewQuotes(){
  pinnedReviewQuote=null;hoveredReviewQuote=null;renderReviewQuotes();
}
window.addEventListener('review:selected',colourReviewQuotes);colourReviewQuotes();

document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.target.matches('input,select,textarea')||event.target.isContentEditable||document.querySelector('[data-review]')||!document.querySelector('[data-state-owner]'))return;
  const key=event.key.toLowerCase();
  const direction=['d','arrowright'].includes(key)?'next':['a','arrowleft'].includes(key)?'previous':null;
  const link=direction&&document.querySelector('[data-saved-'+direction+']');if(link){event.preventDefault();link.click();}
});

function renderSeenCalendar(pop,offset=0) {
  const data=JSON.parse(pop.dataset.seenCalendar), counts=JSON.parse(pop.dataset.nightCounts||'{}');
  const newJobs=key=>counts[key]?' · '+counts[key].toLocaleString('en-US')+' new job'+(counts[key]===1?'':'s'):'';
  const date=s=>new Date(s+'T00:00:00Z'), month=d=>d.getUTCFullYear()*12+d.getUTCMonth();
  const today=date(data.today), earliest=date(data.earliest||data.today);
  const selected=data.selected[0];
  const initial=selected?date(selected):today;
  let index=pop.seenMonth??month(initial);index=Math.max(month(earliest),Math.min(month(today),index+offset));pop.seenMonth=index;
  const first=new Date(Date.UTC(Math.floor(index/12),index%12,1));
  pop.querySelector('[data-seen-month-title]').textContent=new Intl.DateTimeFormat('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}).format(first);
  pop.querySelector('[data-seen-month="-1"]').disabled=index<=month(earliest);
  pop.querySelector('[data-seen-month="1"]').disabled=index>=month(today);
  const grid=pop.querySelector('[data-seen-days]');grid.replaceChildren();
  for(const day of ['M','T','W','T','F','S','S']){const el=document.createElement('span');el.className='seen-weekday';el.textContent=day;grid.append(el);}
  for(let i=0;i<(first.getUTCDay()+6)%7;i++)grid.append(document.createElement('span'));
  const end=new Date(Date.UTC(Math.floor(index/12),index%12+1,0)).getUTCDate(), nights=new Set(data.nights||[]);
  for(let day=1;day<=end;day++){
    const key=dateKey(new Date(Date.UTC(Math.floor(index/12),index%12,day))),button=document.createElement('button');
    button.type='button';button.textContent=day;button.dataset.seenDate=key;
    // A day missing from the published nights collected nothing; fetch times and counts are not stored.
    const missed=nights.size>0&&key>data.earliest&&key<data.today&&key!==data.latest&&!nights.has(key);
    button.disabled=missed||!data.latest||key>data.latest||key<data.earliest;
    const label=new Intl.DateTimeFormat('en-GB',{weekday:'short',day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}).format(date(key));
    const night=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][date(key).getUTCDay()]+' '+day+' '+reviewMonths[index%12];
    const picked=!missed&&data.selected.includes(key);
    button.setAttribute('aria-label',label);button.setAttribute('aria-pressed',String(picked));
    button.classList.toggle('selected',picked);button.classList.toggle('today',key===data.today);button.classList.toggle('missed',missed);
    button.classList.toggle('in-range',!missed&&data.ranges.some(([a,b])=>a<=key&&key<b));
    button.title=key===data.today&&button.disabled?"Today. Tonight's fetch has not run yet.":missed?'Nothing was collected on '+night+'.':!data.latest||key>data.latest?'':key<data.earliest?'Before the first fetch':'Fetched '+night+newJobs(key);
    grid.append(button);
  }
}
function dateKey(date){return date.toISOString().slice(0,10);}
document.addEventListener('toggle',event=>{const pop=event.target;if(pop.matches?.('[data-seen-calendar]')&&pop.open){delete pop.seenMonth;renderSeenCalendar(pop);}},true);
document.addEventListener('click',event=>{
  const pick=event.target.closest('[data-seen-pick],[data-seen-date]'), month=event.target.closest('[data-seen-month]');
  if(month){event.preventDefault();renderSeenCalendar(month.closest('[data-seen-calendar]'),Number(month.dataset.seenMonth));return;}
  if(!pick)return;event.preventDefault();if(filterActive||pick.disabled||pick.matches('[data-seen-date].selected'))return;
  pick.closest('details').open=false;
  const url=new URL(pick.href||location.href);if(pick.dataset.seenDate)url.searchParams.set('seen',pick.dataset.seenDate);
  url.searchParams.set('page','1');url.searchParams.delete('id');refreshReview(url);
});

document.addEventListener('keydown',event=>{
  if(event.target.closest('[data-seen-calendar][open]')&&event.key!=='Escape')event.stopPropagation();
},true);
