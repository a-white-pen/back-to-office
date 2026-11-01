/* Handle Review navigation, state changes and quotes, plus saved-job search panels. */
document.querySelector('[data-password-toggle]')?.addEventListener('click', event => {
  const input = document.getElementById('password');
  input.type = input.type === 'password' ? 'text' : 'password';
  event.currentTarget.textContent = input.type === 'password' ? 'Show' : 'Hide';
});
document.querySelectorAll('[data-application-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-application-tab]').forEach(tab => tab.setAttribute('aria-selected', String(tab === button)));
  document.querySelector('[data-application-heading]').textContent = button.textContent;
  document.querySelector('[data-application-shell]').dataset.applicationView = button.dataset.applicationTab;
}));

function foldQuote(text) {
  return text.normalize('NFKC').replace(/[‘’]/g,"'").replace(/[“”]/g,'"')
    .replace(/[‐‑‒–—−]/g,'-').toLowerCase().replace(/ß/g,'ss').replace(/ς/g,'σ');
}
function normalizeQuote(text) {
  return foldQuote(text).replace(/^\s*(?:[•·▪*]\s*|-\s+|\d+[.)]\s+)/,'').replace(/\s+/g,' ').trim();
}
// Keep normalized text linked to its original DOM ranges for quote highlights.
function quoteText(root) {
  let text = ''; const offsets = [];
  const segmenter = new Intl.Segmenter(undefined, {granularity:'grapheme'});
  function append(value, position) {
    for (const character of value) {
      if (/\s/.test(character)) {
        if (!text.endsWith(' ')) { text += ' '; offsets.push(position); }
      } else {
        for (let part=0; part<character.length; part++) { text += character[part]; offsets.push(position); }
      }
    }
  }
  function visit(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      for (const part of segmenter.segment(node.textContent)) {
        append(foldQuote(part.segment), {node,start:part.index,end:part.index+part.segment.length});
      }
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const block = /^(P|DIV|LI|UL|OL|H[1-6]|TR|TD|TH|BLOCKQUOTE|BR)$/.test(node.tagName);
    if (block) append(' ',null);
    for (const child of node.childNodes) visit(child);
    if (block) append(' ',null);
  }
  visit(root); return {text,offsets};
}
let pinnedReviewQuote=null, hoveredReviewQuote=null;
function renderReviewQuotes() {
  const parents=new Set();
  document.querySelectorAll('mark.quote-tone,mark.quote-hit').forEach(mark=>{
    parents.add(mark.parentNode);mark.replaceWith(...mark.childNodes);
  });
  parents.forEach(parent=>parent.normalize());
  // Group tints colour the JD only; the hovered or clicked ask may also be found in the title.
  const jd=document.getElementById('listing-jd'),title=document.getElementById('listing-title');
  const tones=jd?[quoteText(jd)]:[], everywhere=title?[...tones,quoteText(title)]:tones, paints=new Map();
  const active=(hoveredReviewQuote?.isConnected&&hoveredReviewQuote)||(pinnedReviewQuote?.isConnected&&pinnedReviewQuote);
  let activeFound=false;
  function paint(button,priority,contents=tones) {
    const quote=normalizeQuote(button.dataset.quote);if(!quote)return false;
    for(const content of contents) {
      const start=content.text.indexOf(quote);if(start<0)continue;
      for(const offset of content.offsets.slice(start,start+quote.length)) {
        if(!offset)continue;
        let values=paints.get(offset.node);
        if(!values){values=new Uint8Array(offset.node.length);paints.set(offset.node,values);}
        for(let i=offset.start;i<offset.end;i++)values[i]=Math.max(values[i],priority);
      }
      return true;
    }
    return false;
  }
  // Resolve overlaps before adding marks, so a phrase never has nested highlight tints.
  for(const [kind,priority] of [['other',1],['for',2],['against',3]])
    document.querySelectorAll('.requirement-group.'+kind+' [data-quote]').forEach(button=>paint(button,priority));
  if(active)activeFound=paint(active,4,everywhere);
  const classes=['','quote-tone quote-other','quote-tone quote-for','quote-tone quote-against','quote-hit'];
  for(const [node,values] of paints) {
    // Work backwards: wrapping a later segment leaves earlier text offsets intact.
    for(let end=values.length;end>0;) {
      const priority=values[end-1];let start=end-1;
      while(start>0&&values[start-1]===priority)start--;
      if(priority){const range=document.createRange();range.setStart(node,start);range.setEnd(node,end);
        const mark=document.createElement('mark');mark.className=classes[priority];range.surroundContents(mark);}
      end=start;
    }
  }
  return activeFound;
}
function highlightReviewQuote(button,scroll=true) {
  if(!button)return;
  const found=renderReviewQuotes();
  const mark=found?document.querySelector('mark.quote-hit'):null,pane=mark?.closest('.listing-panel');
  // Leave space above the phrase when scrolling to it.
  if(scroll&&pane)pane.scrollTo({top:pane.scrollTop+mark.getBoundingClientRect().top-pane.getBoundingClientRect().top-120,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
  else if(scroll&&!found)reviewMessage('That quote is not visible in this presentation.');
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-quote]');if(button){pinnedReviewQuote=button;hoveredReviewQuote=null;highlightReviewQuote(button);}});
document.addEventListener('mouseover',event=>{const button=event.target.closest('[data-quote]');if(button&&!button.contains(event.relatedTarget)){if(pinnedReviewQuote!==button)pinnedReviewQuote=null;hoveredReviewQuote=button;highlightReviewQuote(button,false);}});
document.addEventListener('mouseout',event=>{const button=event.target.closest('[data-quote]');if(button&&!button.contains(event.relatedTarget)){hoveredReviewQuote=null;renderReviewQuotes();}});
// A fact's definition opens leftwards when it would run past the listing pane.
for(const type of ['mouseover','focusin'])document.addEventListener(type,event=>{
  const tip=event.target.closest?.('.fact-cells [data-tooltip]'),pane=tip?.closest('.listing-panel');if(!pane)return;
  tip.classList.toggle('tip-end',tip.parentElement.getBoundingClientRect().left+300>pane.getBoundingClientRect().right);
});
async function loadWordingDiff(panel) {
  if (panel.dataset.loaded) return;
  const body=panel.querySelector('[data-diff-body]'); body.textContent='Loading diff…';
  try {
    const response=await fetch('/review/diff?first='+encodeURIComponent(panel.dataset.diffFirst)+'&second='+encodeURIComponent(panel.dataset.diffSecond));
    if (!response.ok) throw Error();
    body.innerHTML=(await response.json()).html; panel.dataset.loaded='true';
  } catch { body.textContent='The wording diff is temporarily unavailable.'; }
}
// Reveal a panel and load its wording diff only when first opened.
document.addEventListener('click', event => {
  const toggle=event.target.closest('[data-reveal]'); if (!toggle) return;
  const panel=toggle.closest('[data-reveal-root]')?.querySelector(':scope>[data-reveal-panel]'); if (!panel) return;
  panel.hidden=!panel.hidden; toggle.setAttribute('aria-expanded',String(!panel.hidden));
  toggle.textContent=panel.hidden?toggle.dataset.closed:toggle.dataset.open;
  if (!panel.hidden && panel.matches('[data-diff-first]')) loadWordingDiff(panel);
});
document.addEventListener('click', event => {
  const tab=event.target.closest('[data-copy-tab]'); if (!tab) return;
  const section=tab.closest('.context-block');
  section.querySelectorAll('[data-copy-tab]').forEach(item => item.setAttribute('aria-selected',String(item===tab)));
  section.querySelectorAll('[data-copy-panel]').forEach(panel => panel.hidden=panel.dataset.copyPanel!==tab.dataset.copyTab);
});

document.addEventListener('toggle',event=>{
  if (!event.target.matches?.('[data-popover]') || !event.target.open) return;
  document.querySelectorAll('[data-popover][open]').forEach(pop=>{if(pop!==event.target)pop.open=false;});
},true);
document.addEventListener('click',event=>{
  if (!event.target.closest('[data-popover]')) document.querySelectorAll('[data-popover][open]').forEach(pop=>pop.open=false);
});
let filterPending=null, filterActive=false, latestFilterURL=null, searchTimer, selectionVersion=0;
let selectionPending=null, selectionActive=false;
function reviewMessage(message) {
  const status=document.querySelector('[data-review-loading]'); if (status) status.textContent=message;
}
function reviewBusy(value,message='Updating filters…') {
  document.querySelector('[data-review]')?.setAttribute('aria-busy',String(value));
  reviewMessage(value?message:'');
}
function pinReview(url=new URL(location.href)) {
  const root=document.querySelector('[data-review]'); if (!root) return;
  if (root.dataset.selected && url.searchParams.get('view')!=='table') url.searchParams.set('id',root.dataset.selected);
  history.replaceState(null,'',url);
  const refresh=root.querySelector('[data-review-refresh]'); if (refresh) refresh.href=url.href;
  root.querySelectorAll('.view-switch a').forEach(link=>{
    const target=new URL(url);target.searchParams.set('view',new URL(link.href).searchParams.get('view'));link.href=target;
  });
  root.querySelectorAll('[data-review-show],[data-filter-clear],[data-review-filters] a[role=button]').forEach(link=>{
    const target=new URL(link.href);target.searchParams.set('view',url.searchParams.get('view')||'page');link.href=target;
  });
  updateReviewNavigation();
}
let reviewReadStack=[], reviewStateBusy=false, reviewTableDirty=false;
function reviewNavigation() {
  const data=document.querySelector('[data-review-navigation]');
  return data?JSON.parse(data.textContent):{};
}
function updateReviewNavigation() {
  const root=document.querySelector('[data-review]');if(!root)return;
  const nav=reviewNavigation(), selected=root.dataset.selected;
  for(const [name,id] of [['previous',nav.previous_id],['next',nav.next_id]]) {
    const controls=root.querySelectorAll('[data-'+name+']');if(!controls.length)continue;
    const previous=name==='previous'&&nav.show==='unread'&&reviewReadStack.length;
    const enabled=!!id||!!previous||(name==='next'&&!!selected&&root.dataset.admin==='true'&&root.dataset.stateAvailable==='true');
    controls.forEach(control=>{control.setAttribute('aria-disabled',String(!enabled));delete control.dataset.reviewEdge;});
    const url=new URL(location.href);url.searchParams.set('view','page');
    if(id)url.searchParams.set('id',id);else url.searchParams.delete('id');
    url.searchParams.delete('advertiser_scope');url.searchParams.delete('advertiser_days');controls.forEach(control=>control.href=url);
  }
  const outside=root.querySelector('[data-outside-queue]');
  if(outside){outside.hidden=!selected||!!nav.position;outside.querySelector('[data-outside-text]').textContent=nav.show==='unread'&&root.querySelector('.read-chip')?.dataset.active==='false'?'Read · this listing has left the Unread queue.':'Viewing a job outside the current queue.';}
  root.querySelectorAll('[data-show-count]').forEach(node=>node.textContent=nav.counts?.[node.dataset.showCount]??0);
  const toGo=root.querySelector('[data-to-go]');if(toGo)toGo.textContent=(nav.to_go??0)+' to go';
  const advertiser=root.querySelector('[data-advertiser-progress]');
  if(advertiser) {
    advertiser.hidden=!nav.advertiser||new URL(location.href).searchParams.get('view')==='table';
    if(nav.advertiser){advertiser.querySelector('[data-advertiser-name]').textContent=nav.advertiser.name;advertiser.querySelector('[data-advertiser-count]').textContent=nav.advertiser.position+' of '+nav.advertiser.total;}
  }
  const back=root.querySelector('[data-back-queue]');if(back){const url=new URL(location.href);url.searchParams.delete('id');url.searchParams.set('page','1');url.searchParams.set('view','page');back.href=url;}
}
function acceptReviewFacets(facets) {
  const root=document.querySelector('[data-review]');if(!root||!facets)return;
  for(const [attribute,key] of [['data-market-count','market_counts'],['data-bucket-count','bucket_counts'],['data-board-count','board_counts']]) {
    root.querySelectorAll('['+attribute+']').forEach(node=>node.textContent=facets[key]?.[node.getAttribute(attribute)]??0);
  }
  root.querySelectorAll('[data-out-of-scope-count]').forEach(node=>node.textContent=facets.out_of_scope_count??0);
}
function acceptReviewNavigation(nav) {
  const root=document.querySelector('[data-review]');if(!root||!nav)return;
  root.querySelector('[data-review-navigation]').textContent=JSON.stringify(nav);
  root.querySelector('[data-review-queue]').textContent=JSON.stringify(nav.ids);
  root.dataset.queuePage=nav.page;root.dataset.queueSize=nav.size||50;root.dataset.queueOffset=(nav.page-1)*(nav.size||50);root.dataset.queueTotal=nav.total;
  const count=root.querySelector('[data-job-count]');if(count)count.textContent=nav.total+' jobs';
  const global=document.querySelector('[data-review-nav-count]');if(global)global.textContent=nav.counts?.unread??nav.total;
  updateReviewNavigation();
}
const saveTitle=on=>on?'In To Apply · press W or Enter to undo':'Adds this job to To Apply · ⇧ also stars it';
// Apply an admin change on the server before updating the visible controls.
async function changeReviewState(action,id,active,refresh=true,extra={}) {
  const owner=document.querySelector('[data-review],[data-state-owner]');
  if(!owner||owner.dataset.admin!=='true'||owner.dataset.stateAvailable!=='true'||reviewStateBusy||filterActive||selectionActive)return false;
  reviewStateBusy=true;
  try {
    const response=await fetch('/review/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':owner.dataset.csrf},body:JSON.stringify({action,id:Number(id),active,...extra})});
    const data=await response.json();if(!response.ok)throw Error(data.error||'Could not save Review state.');
    const fields={read:'reviewed_at',star:'starred_at',save:'saved_to_apply_at'};
    owner.querySelectorAll('[data-review-state-action][data-job-id="'+CSS.escape(String(id))+'"]').forEach(button=>{
      const kind=button.dataset.reviewStateAction,on=!!data.review_state[fields[kind]];
      button.dataset.active=String(!on);button.dataset.stateValue=data.review_state[fields[kind]]||'';
      if(button.matches('.read-dot')){button.classList.toggle('is-read',on);button.setAttribute('aria-label',on?'Mark unread':'Mark read');button.title=on?'Mark unread':'Mark read';}
      else if(kind==='star'){button.setAttribute('aria-label',on?'Unstar job':'Star job');const label=button.querySelector('[data-star-label]');if(label)label.textContent=on?'★':'☆';else button.textContent=on?'★':'☆';}
      else if(kind==='save'){button.classList.toggle('is-saved',on);button.title=saveTitle(on);const label=button.querySelector('[data-save-label]');if(label)label.textContent=on?'✓ Applying':"I'll apply";}
    });
    document.querySelectorAll('[data-saved-count]').forEach(node=>node.textContent=data.saved_count);
    if(action==='read'){reviewTableDirty=true;if(new URL(location.href).searchParams.get('view')==='table'&&typeof rememberTableRead==='function')rememberTableRead(id,!!data.review_state.reviewed_at);}
    // A star change on a job in To Apply says what happens to its people search.
    const starWords=action==='star'&&['queued','searching'].includes(data.people_search)?(active?'Starred · people search started':data.people_search==='queued'?'Unstarred · the people search still runs. Remove the job from To Apply to stop it.':'Unstarred · the people search still runs.'):'';
    // To Apply reloads to show the change, so its message waits for the new page.
    if(!owner.matches('[data-review]')){if(starWords)try{sessionStorage.setItem('bto-toast',starWords);}catch{}location.reload();return true;}
    if(starWords&&typeof queueToast==='function')queueToast(starWords);
    if(refresh&&new URL(location.href).searchParams.get('view')!=='table') {
      const url=new URL(location.href);url.searchParams.set('id',owner.dataset.selected);url.searchParams.set('view','page');await selectReview(url);
    } else if(refresh&&action==='read') {
      const url=new URL(location.href);url.searchParams.delete('id');await refreshReview(url);
    }
    return true;
  } catch(error) {reviewMessage(error.message||'Could not save Review state. Please retry.');return false;}
  finally {reviewStateBusy=false;}
}
async function navigateReview(direction) {
  if(filterActive||selectionActive||reviewStateBusy)return;
  const root=document.querySelector('[data-review]'),nav=reviewNavigation(),current=Number(root.dataset.selected);
  let target=direction==='next'?nav.next_id:nav.previous_id;
  if(direction==='previous'&&nav.show==='unread'&&reviewReadStack.length)target=reviewReadStack.pop();
  if(direction==='next'&&root.dataset.admin==='true'&&root.dataset.stateAvailable==='true'&&current) {
    if(!await changeReviewState('read',current,true,false))return;
    reviewReadStack.push(current);
  }
  const url=new URL(location.href);url.searchParams.set('view','page');url.searchParams.set('id',target||current);
  url.searchParams.delete('advertiser_scope');url.searchParams.delete('advertiser_days');
  await selectReview(url);
  if(!target&&direction==='next'&&typeof queueToast==='function')queueToast('That was the last job in this queue.');
}
function filterURL(form) {
  if(filterActive && latestFilterURL) return currentSearchFields(new URL(latestFilterURL),form);
  const url=new URL('/review',location.origin);
  for (const [name,value] of new FormData(form)) if (value!=='') url.searchParams.append(name,value);
  return url;
}
function currentSearchFields(url,form) {
  let changed=false;
  for (const name of ['q','title','exclude','company','description','posted_by']) {
    const field=form?.elements.namedItem(name); if (!field) continue;
    if ((url.searchParams.get(name)||'')!==field.value) changed=true;
    if (field.value) url.searchParams.set(name,field.value); else url.searchParams.delete(name);
  }
  if (changed) url.searchParams.delete('id');
  return url;
}
// Serialize filter requests and discard results replaced by newer input.
async function refreshReview(url,focus=false,edge=null) {
  if(!edge)reviewReadStack=[];
  if (filterActive && filterPending?.url.href===url.href) return;
  url=currentSearchFields(url,document.querySelector('[data-review-filters]'));
  if(typeof prepareReviewTable==='function')url=prepareReviewTable(url);
  latestFilterURL=new URL(url);filterPending={url,focus,edge}; selectionVersion++;
  if (filterActive) return;
  filterActive=true; let failureMessage='';
  while (filterPending) {
    const nextRequest=filterPending; filterPending=null;
    const old=document.querySelector('[data-review]');
    const pops=[...old.querySelectorAll('[data-popover][open]')].map(node=>node.dataset.popover);
    const expanded=old.querySelector('#advanced-search')?.open;
    reviewBusy(true);
    try {
      const response=await fetch(nextRequest.url);
      const page=new DOMParser().parseFromString(await response.text(),'text/html');
      const next=page.querySelector('[data-review]');
      if (!next) { location.href=response.url; return; }
      const latest=currentSearchFields(new URL(nextRequest.url),old.querySelector('[data-review-filters]'));
      if (latest.href!==nextRequest.url.href) {
        clearTimeout(searchTimer);filterPending={url:latest,focus:document.activeElement?.name||'q'};
      }
      if (filterPending) continue;
      const focusName=typeof nextRequest.focus==='string'?nextRequest.focus:'q';
      const input=old.querySelector('input[name="'+CSS.escape(focusName)+'"]'), start=input?.selectionStart, end=input?.selectionEnd;
      const advance=next.querySelector('#advanced-search'); if (advance) advance.open=expanded;
      const toggle=next.querySelector('[data-advanced-toggle]');
      toggle?.setAttribute('aria-expanded',String(!!expanded));
      if (toggle) toggle.querySelector('.chevron').textContent=expanded?'▴':'▾';
      next.querySelectorAll('[data-popover]').forEach(node=>node.open=pops.includes(node.dataset.popover));
      clearTimeout(searchTimer);old.replaceWith(next);failureMessage='';
      const count=page.querySelector('[data-review-nav-count]');
      const nav=document.querySelector('[data-review-nav-count]'); if (nav && count) nav.textContent=count.textContent;
      pinReview(nextRequest.url);reviewTableDirty=false;if(typeof updateReviewTable==='function')updateReviewTable();
      if (nextRequest.focus) { const updated=next.querySelector('input[name="'+CSS.escape(focusName)+'"]');updated?.focus();updated?.setSelectionRange(start,end); }
    } catch { failureMessage='Could not refresh. Press Enter or Refresh to retry.'; }
  }
  filterActive=false;latestFilterURL=null; reviewBusy(false);if (failureMessage) reviewMessage(failureMessage);
}
function showReviewView(mode,url=new URL(location.href)) {
  const root=document.querySelector('[data-review]');if(!root)return;
  root.querySelector('[data-view-page]')?.toggleAttribute('hidden',mode!=='page');
  root.querySelector('[data-view-table]')?.toggleAttribute('hidden',mode!=='table');
  root.querySelector('[data-page-counter]')?.toggleAttribute('hidden',mode!=='page');
  root.querySelector('[data-review-pagination]')?.toggleAttribute('hidden',mode!=='table');
  root.querySelectorAll('.view-switch a').forEach(link=>{
    const active=new URL(link.href).searchParams.get('view')===mode;
    link.classList.toggle('selected',active);
    if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
  root.querySelector('[data-to-go]')?.toggleAttribute('hidden',mode!=='page');
  root.querySelector('input[name=view]').value=mode;url.searchParams.set('view',mode);pinReview(url);if(typeof updateReviewTable==='function')updateReviewTable();
}
function openReviewPage(url) {
  const root=document.querySelector('[data-review]');
  if(typeof reviewTableKept!=='undefined')reviewTableKept.clear();url.searchParams.delete('table_keep');
  const data=root?.querySelector('[data-review-queue]');
  if(!data)return;
  const id=url.searchParams.get('id')||root.dataset.selected||reviewNavigation().first_unread_id||JSON.parse(data.textContent)[0];
  if(!id){showReviewView('page',url);return;}
  url.searchParams.set('id',id);url.searchParams.set('view','page');
  if(String(id)===root.dataset.selected){showReviewView('page',url);return;}
  selectReview(url);
}
// Load the latest selected job without letting an earlier response overwrite it.
async function selectReview(url) {
  selectionPending=url;
  if (selectionActive) return;
  selectionActive=true;
  while (selectionPending) {
    const target=selectionPending; selectionPending=null; const version=++selectionVersion;
    const endpoint=new URL(target); endpoint.pathname='/review/listing';
    reviewMessage('Loading listing…');
    try {
      const response=await fetch(endpoint);
      if (response.redirected) { location.href=response.url; return; }
      if (!response.ok) throw Error();
      const data=await response.json();
      if (selectionPending || version!==selectionVersion || filterActive) continue;
      const root=document.querySelector('[data-review]');
      if(data.navigation&&Number(root.dataset.queuePage)!==data.navigation.page){target.searchParams.set('page',data.navigation.page);await refreshReview(target,false,'navigation');continue;}
      for (const [selector,key] of [['[data-review-history]','history'],['[data-review-listing]','listing'],['[data-review-role]','role']]) root.querySelector(selector).innerHTML=data[key];root.querySelector('[data-review-listing]').className='listing-card';hoveredReviewQuote=null;window.dispatchEvent(new CustomEvent('review:selected',{detail:data}));
      root.dataset.stateAvailable=String(data.review_state_available);root.dataset.selected=String(data.id);if(data.navigation){target.searchParams.set('page',data.navigation.page);acceptReviewNavigation(data.navigation);}acceptReviewFacets(data.facets);showReviewView('page',target);
      root.querySelectorAll('.review-actions [data-review-state-action]').forEach(button=>{const field={star:'starred_at',save:'saved_to_apply_at'}[button.dataset.reviewStateAction];if(!field)return;const on=!!data.review_state?.[field];if(field==='saved_to_apply_at'){button.classList.toggle('is-saved',on);button.title=saveTitle(on);}button.dataset.jobId=data.id;button.dataset.active=String(!on);button.disabled=root.dataset.admin!=='true'||root.dataset.stateAvailable!=='true';if(field==='starred_at')button.setAttribute('aria-label',on?'Unstar job':'Star job');const label=button.querySelector('[data-star-label],[data-save-label]');if(label)label.textContent=field==='starred_at'?(on?'★':'☆'):(on?'✓ Applying':"I'll apply");});
      root.querySelector('.listing-panel').scrollTop=0;
      root.querySelector('.left-panel').scrollTop=0;
      root.querySelector('.role-content').scrollTop=0;
      reviewMessage('');
    } catch { reviewMessage('The selected listing is unavailable. Refresh to retry.'); }
  }
  selectionActive=false;
}
document.addEventListener('click', event => {
  const stateButton=event.target.closest('[data-review-state-action]');
  if(stateButton){event.preventDefault();if(!stateButton.disabled)if(stateButton.dataset.reviewStateAction==='save')queueReviewJob(stateButton,event.shiftKey);else changeReviewState(stateButton.dataset.reviewStateAction,stateButton.dataset.jobId,stateButton.dataset.active==='true');return;}
  const anchor=event.target.closest('a');
  if (anchor?.getAttribute('aria-disabled')==='true') { event.preventDefault();return; }
  const review=document.querySelector('[data-review]'); if (!review) return;
  if(anchor?.matches('[data-review-relax]')){event.preventDefault();const url=new URL(anchor.href);for(const name of ['q','title','exclude','company','description','posted_by']){const field=review.querySelector('[data-review-filters]').elements.namedItem(name);if(field)field.value=url.searchParams.get(name)||'';}refreshReview(url);return;}
  if(anchor?.matches('[data-filter-clear]')){
    event.preventDefault();if(filterActive)return;
    const field=review.querySelector('[data-review-filters]').elements.namedItem(anchor.dataset.filterClear);
    if(field)field.value=anchor.dataset.filterValue;
    if(anchor.dataset.filterClear==='advertiser'){
      for(const name of ['scope','context_days','all']){
        const extra=review.querySelector('[data-review-filters]').elements.namedItem(name);if(extra)extra.value='';
      }
    }
    refreshReview(new URL(anchor.href));return;
  }
  if(anchor?.closest('.view-switch')){
    event.preventDefault();if(filterActive)return;
    const mode=new URL(anchor.href).searchParams.get('view');
    if(mode!==(new URL(location.href).searchParams.get('view')==='table'?'table':'page')&&typeof reviewQueueStamp!=='undefined')reviewQueueStamp=new Date();
    if(mode==='table'){const url=new URL(anchor.href);url.searchParams.delete('id');url.searchParams.set('page','1');try{const size=localStorage.getItem('bto-table-size');if(['25','50','75','100'].includes(size))url.searchParams.set('size',size);}catch{}refreshReview(url);}else openReviewPage(new URL(anchor.href));return;
  }
  if(anchor?.matches('[data-review-open]')){
    event.preventDefault();if(!filterActive)openReviewPage(new URL(anchor.href));return;
  }
  if (anchor?.closest('[data-review-filters]') && anchor.matches('[role=button]')) {
    event.preventDefault(); if (!filterActive) refreshReview(new URL(anchor.href));return;
  }
  if (anchor?.matches('[data-review-refresh]')) { event.preventDefault();if (!filterActive) refreshReview(new URL(location.href));return; }
  if(anchor?.matches('[data-review-show]')){event.preventDefault();refreshReview(new URL(anchor.href));return;}
  if(anchor?.matches('[data-next],[data-previous]')){event.preventDefault();navigateReview(anchor.matches('[data-next]')?'next':'previous');return;}
  if(anchor?.matches('[data-back-queue]')){event.preventDefault();refreshReview(new URL(anchor.href));return;}
  if(anchor?.matches('[data-review-select],[data-review-context]')){event.preventDefault();if(!filterActive)selectReview(new URL(anchor.href));return;}
  const row=event.target.closest('[data-review-row]');
  if (row && !event.target.closest('a,button,input,select') && !filterActive) openReviewPage(new URL(row.dataset.reviewRow,location.origin));
});
document.addEventListener('submit', event => {
  if (!event.target.matches('[data-review-filters]')) return;
  event.preventDefault();clearTimeout(searchTimer); refreshReview(filterURL(event.target));
});
document.addEventListener('input', event => {
  if (!event.target.matches('[data-review-filters] input:not([type=hidden])')) return;
  clearTimeout(searchTimer);
  if (!event.isComposing) searchTimer=setTimeout(()=>refreshReview(filterURL(event.target.form),event.target.name),350);
});
document.addEventListener('change', event => {
  if (!event.target.matches('[data-review-seen]')) return;
  if (event.target.value) { event.target.form.querySelector('input[name=seen]').value=event.target.value;const url=filterURL(event.target.form);url.searchParams.set('seen',event.target.value);url.searchParams.delete('id');refreshReview(url); }
});
document.addEventListener('click', event => {
  const posted=event.target.closest('[data-posted-by]');
  if (posted) { if (!posted.matches('.selected')) { posted.form.elements.namedItem('posted_by').value=posted.dataset.postedBy;refreshReview(filterURL(posted.form)); } return; }
  const button=event.target.closest('[data-advanced-toggle]'); if (!button) return;
  const panel=button.form.querySelector('#advanced-search'); panel.open=!panel.open;
  button.setAttribute('aria-expanded',String(panel.open));button.querySelector('.chevron').textContent=panel.open?'▴':'▾';
});
document.addEventListener('keydown', event => {
  if(event.key==='Escape'&&document.querySelector('[data-review]')){document.querySelectorAll('[data-popover][open]').forEach(pop=>pop.open=false);if(event.target.matches('input,select,textarea'))event.target.blur();return;}
  if (event.target.matches('input,select,textarea') || event.target.isContentEditable || event.ctrlKey || event.metaKey || event.altKey) return;
  if (!document.querySelector('[data-review]')) return;
  if (event.key===' ') { const link=event.target.closest('a[role=button]');if (link) { event.preventDefault();link.click();return; } }
  if (event.key==='/') { event.preventDefault();document.querySelector('input[name=q]')?.focus();return; }
  if (event.key.toLowerCase()==='v') { event.preventDefault();const link=document.querySelector('.view-switch a:not(.selected)');if(link) link.click();return; }
  if(!document.querySelector('[data-view-page]:not([hidden])'))return;
  if(event.key==='Enter'){if(hoveredReviewQuote?.isConnected){event.preventDefault();hoveredReviewQuote.click();return;}if(event.target.closest('a,button,summary'))return;const button=document.querySelector('.review-actions [data-review-state-action=save]');if(button&&!button.disabled){event.preventDefault();queueReviewJob(button,event.shiftKey);}return;}
  if(['u','s','w'].includes(event.key.toLowerCase())){const action={u:'read',s:'star',w:'save'}[event.key.toLowerCase()];const button=document.querySelector('[data-view-page]:not([hidden]) [data-review-state-action="'+action+'"]');if(button&&!button.disabled){event.preventDefault();if(action==='save')queueReviewJob(button,event.shiftKey);else button.click();}return;}
  const name=['d','D'].includes(event.key)?'next':['a','A'].includes(event.key)?'previous':null;
  const control=name && document.querySelector('[data-'+name+']');
  if (control && control.getAttribute('aria-disabled')!=='true') { event.preventDefault();control.click(); }
});
// 1, 2 and 3 open Review, To apply and Applied from any page.
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.target.matches('input,select,textarea')||event.target.isContentEditable||!['1','2','3'].includes(event.key))return;
  const tab=document.querySelectorAll('.navbar .nav-tab')[Number(event.key)-1];
  if(tab&&!tab.matches('.active')){event.preventDefault();tab.click();}
});
pinReview();
if(document.querySelector('[data-review]')&&performance.getEntriesByType('navigation')[0]?.type==='reload') {
  const url=new URL(location.href);url.searchParams.delete('id');url.searchParams.set('page','1');refreshReview(url);
}

/* To Apply: Links and People update by themselves while a search waits or runs. */
let researchTimer=null, researchTurn=0;
function researchMessage(message) {
  document.querySelector('[data-research-message]')?.remove();
  const body=document.querySelector('.application-body');if(!body)return;
  const notice=document.createElement('p');notice.className='notice application-message';notice.setAttribute('role','status');notice.dataset.researchMessage='';notice.textContent=message;
  body.append(notice);setTimeout(()=>notice.remove(),8000);
}
function showResearchPanel(open,remember=true) {
  const panel=document.querySelector('[data-research-panel]');if(!panel)return;
  panel.hidden=!open;
  document.querySelectorAll('[data-research-toggle]').forEach(button=>{button.setAttribute('aria-expanded',String(open));button.textContent=open?'Less ▴':'More ▾';});
  if(remember)try{localStorage.setItem('bto-apply-strip',open?'1':'0');}catch{}
}
function elementFromHTML(html) {
  const template=document.createElement('template');template.innerHTML=html.trim();return template.content.firstElementChild;
}
// Briefly highlight a completed search and people added since the previous result.
// Match names because each search creates new person IDs.
function markSearchFinished(section,earlierNames) {
  section.classList.add('just-finished');
  const tag=document.createElement('span');tag.className='new-tag';tag.textContent='New';
  section.querySelector('.research-heading .toolbar-spacer')?.after(tag);
  const action=section.querySelector('.research-heading>.research-action');if(action)action.hidden=true;
  if(earlierNames.size)section.querySelectorAll('.person-row[data-person-name]').forEach(row=>{
    if(earlierNames.has(row.dataset.personName.toLowerCase()))return;
    const label=document.createElement('span');label.className='new-person';label.textContent='new';
    row.classList.add('just-finished');row.querySelector('.person-cell>b')?.append(' ',label);
  });
  const tinted=[section,...section.querySelectorAll('.person-row.just-finished')];
  // Reading the layout draws the tint first, so removing it fades instead of vanishing.
  section.getBoundingClientRect();
  tinted.forEach(node=>{node.classList.add('settling');node.classList.remove('just-finished');});
  setTimeout(()=>{tinted.forEach(node=>node.classList.remove('settling'));section.querySelectorAll('.new-tag,.new-person').forEach(node=>node.remove());if(action)action.hidden=false;},4200);
}
function acceptResearch(id,data) {
  const job='[data-job-id="'+CSS.escape(String(id))+'"]';
  const line=document.querySelector('[data-research-line]'+job);
  if(line&&data.line)line.replaceWith(elementFromHTML(data.line));
  const panel=document.querySelector('[data-research-panel]'+job);if(!panel)return;
  const strip=document.querySelector('[data-research-strip]');
  if(strip&&data.strip&&strip.researchHTML!==data.strip){const next=elementFromHTML(data.strip);next.researchHTML=data.strip;strip.replaceWith(next);showResearchPanel(!panel.hidden,false);}
  for(const [part,key] of [['[data-links-section]','links'],['[data-people-section]','people']]) {
    const old=panel.querySelector(part);if(!old||!data[key]||old.researchHTML===data[key])continue;
    const earlier=new Set([...old.querySelectorAll('.person-row[data-person-name]')].map(row=>row.dataset.personName.toLowerCase()));
    const next=elementFromHTML(data[key]);next.researchHTML=data[key];old.replaceWith(next);
    if(old.dataset.running==='true'&&next.dataset.running!=='true'&&!['failed',''].includes(next.dataset.state))markSearchFinished(next,earlier);
  }
}
function waitingOrRunningJobIds() {
  const ids=new Set(), panel=document.querySelector('[data-research-panel]');
  if(panel?.querySelector('[data-running="true"]'))ids.add(panel.dataset.jobId);
  document.querySelectorAll('[data-research-line][data-running="true"]').forEach(line=>ids.add(line.dataset.jobId));
  return [...ids];
}
function scheduleResearchPoll() {
  if(researchTimer)clearTimeout(researchTimer);
  researchTimer=waitingOrRunningJobIds().length?setTimeout(pollResearch,3000):null;
}
// Poll the selected job and one other active job in turn, one request at a time.
async function pollResearch() {
  const selected=document.querySelector('[data-research-panel]')?.dataset.jobId, busy=waitingOrRunningJobIds();
  const others=busy.filter(id=>id!==selected), ids=busy.includes(selected)?[selected]:[];
  if(others.length)ids.push(others[researchTurn++%others.length]);
  for(const id of ids) {
    try {
      const response=await fetch('/to-apply/research?id='+encodeURIComponent(id));
      if(response.redirected){location.href=response.url;return;}
      if(response.ok)acceptResearch(id,await response.json());
    } catch {}
  }
  scheduleResearchPoll();
}
async function runResearch(button) {
  const owner=document.querySelector('[data-state-owner]');
  if(!owner||owner.dataset.admin!=='true'||button.disabled)return;
  button.disabled=true;let message='Could not start the search. Try again.';
  try {
    const response=await fetch('/to-apply/research/run',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':owner.dataset.csrf},body:JSON.stringify({id:Number(button.dataset.jobId),kind:button.dataset.researchRun})});
    let data={};try{data=await response.json();}catch{}
    if(data.line)acceptResearch(button.dataset.jobId,data);
    message=response.ok?'':data.error||message;
  } catch {}
  button.disabled=false;if(message)researchMessage(message);
  scheduleResearchPoll();
}
// Run all asks for every search that never ran or failed; the page reloads to show them queued.
async function runAll(button) {
  const owner=document.querySelector('[data-state-owner]');
  if(!owner||owner.dataset.admin!=='true'||button.disabled)return;
  button.disabled=true;let message='Could not start Run all. Try again.';
  try {
    const response=await fetch('/to-apply/run-all',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':owner.dataset.csrf},body:'{}'});
    if(response.ok){location.reload();return;}
    let data={};try{data=await response.json();}catch{}
    message=data.error||message;
  } catch {}
  button.disabled=false;researchMessage(message);
}
// While Run all runs, its bar is asked for again every few seconds and replaced in place.
let runAllTimer=null;
function scheduleRunAllPoll() {
  if(runAllTimer)clearTimeout(runAllTimer);
  runAllTimer=document.querySelector('[data-run-bar][data-running="true"]')?setTimeout(pollRunAll,4000):null;
}
async function pollRunAll() {
  try {
    const response=await fetch('/to-apply/run-all');
    const data=await response.json();
    const bar=document.querySelector('[data-run-bar]');
    if(bar&&data.bar){bar.replaceWith(elementFromHTML(data.bar));paintRunProgress();}
  } catch {}
  scheduleRunAllPoll();
}
// The page's security policy blocks style attributes, so the progress width is set here.
function paintRunProgress() {
  document.querySelectorAll('[data-run-pct]').forEach(fill=>{fill.style.width=Math.max(0,Math.min(100,Number(fill.dataset.runPct)||0))+'%';});
}
async function lookUpPerson(button) {
  const owner=document.querySelector('[data-state-owner]');if(!owner||owner.dataset.admin!=='true')return;
  const id=button.dataset.lookUp, cells='[data-person-email="'+CSS.escape(id)+'"]';
  document.querySelectorAll(cells).forEach(cell=>{
    const word=document.createElement('span');word.className='looking-up';word.textContent='looking up…';
    const bar=document.createElement('span');bar.className='skeleton person';cell.replaceChildren(word,bar);
  });
  let message='Look up failed. Try again.';
  try {
    const response=await fetch('/to-apply/research/look-up',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':owner.dataset.csrf},body:JSON.stringify({person_id:Number(id)})});
    let data={};try{data=await response.json();}catch{}
    if(response.ok&&data.row) {
      document.querySelectorAll('.person-row[data-person-id="'+CSS.escape(id)+'"]').forEach(row=>row.replaceWith(elementFromHTML(data.row)));
      document.querySelectorAll('.post-person '+cells).forEach(cell=>cell.replaceWith(elementFromHTML(data.email_cell)));
      return;
    }
    message=data.error||message;
  } catch {}
  document.querySelectorAll(cells).forEach(cell=>{
    const line=document.createElement('div');line.className='look-up';
    const word=document.createElement('span');word.className='look-up-failed';word.textContent='failed';
    const retry=document.createElement('button');retry.className='research-action';retry.dataset.lookUp=id;retry.textContent='Retry';
    const reason=document.createElement('p');reason.className='research-reason';reason.textContent=message;
    line.append(word,retry);cell.replaceChildren(line,reason);
  });
}
document.addEventListener('click',event=>{
  const run=event.target.closest('[data-research-run]');
  if(run){event.preventDefault();runResearch(run);return;}
  const all=event.target.closest('[data-run-all]');
  if(all){event.preventDefault();runAll(all);return;}
  const look=event.target.closest('[data-look-up]');
  if(look){event.preventDefault();lookUpPerson(look);return;}
  const copy=event.target.closest('[data-copy-email]');
  if(copy){navigator.clipboard?.writeText(copy.dataset.copyEmail).catch(()=>{});copy.textContent='Copied';clearTimeout(copy.copiedTimer);copy.copiedTimer=setTimeout(()=>{copy.textContent='Copy';},1200);return;}
  if(event.target.closest('[data-research-toggle]')){showResearchPanel(!!document.querySelector('[data-research-panel]')?.hidden);return;}
  if(event.target.closest('[data-research-open]')){showResearchPanel(true);document.querySelector('[data-people-section]')?.scrollIntoView({block:'nearest'});return;}
  const posts=event.target.closest('[data-post-toggle]');
  if(posts){const list=posts.closest('.post').querySelector('[data-post-people="'+posts.dataset.postToggle+'"]');list.hidden=!list.hidden;posts.setAttribute('aria-expanded',String(!list.hidden));posts.querySelector('[data-post-arrow]').textContent=list.hidden?'▾':'▴';}
});
document.addEventListener('keydown',event=>{
  const key=event.key?.toLowerCase();
  if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.target.matches('input,select,textarea')||event.target.isContentEditable||!['o','l'].includes(key))return;
  const link=document.querySelector(key==='o'?'[data-state-owner] [data-apply-link]':'[data-state-owner] [data-job-listing-link]');if(link){event.preventDefault();link.click();}
});
if(document.querySelector('[data-research-panel]')){let open=false;try{open=localStorage.getItem('bto-apply-strip')==='1';}catch{}showResearchPanel(open,false);}
// How it works: the menu pill of the section being read is filled in.
const howNav=document.querySelector('.how-nav');
if(howNav){
  const pills=[...howNav.querySelectorAll('[data-how-section]')];
  const markSection=()=>{
    let active=pills[0];
    for(const pill of pills){const section=document.getElementById(pill.dataset.howSection);if(section&&section.getBoundingClientRect().top<=110)active=pill;}
    const page=document.scrollingElement;if(page.scrollTop+innerHeight>=page.scrollHeight-4)active=pills[pills.length-1];
    if(active.classList.contains('active'))return;
    pills.forEach(pill=>pill.classList.toggle('active',pill===active));
    howNav.scrollTo({left:Math.max(0,active.offsetLeft-24),behavior:'smooth'});
  };
  document.addEventListener('scroll',markSection,{passive:true});markSection();
}
scheduleResearchPoll();
paintRunProgress();
scheduleRunAllPoll();
