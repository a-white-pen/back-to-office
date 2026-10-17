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
document.addEventListener('click', event => {
  const button = event.target.closest('[data-quote]'); if (!button) return;
  const parents = new Set();
  document.querySelectorAll('mark.quote-hit').forEach(mark => { parents.add(mark.parentNode); mark.replaceWith(...mark.childNodes); });
  parents.forEach(parent => parent.normalize());
  const quote = normalizeQuote(button.dataset.quote); if (!quote) return;
  for (const root of [document.getElementById('listing-title'),document.getElementById('listing-jd')]) {
    if (!root) continue;
    const content = quoteText(root), start = content.text.indexOf(quote);
    if (start < 0) continue;
    const portions = new Map();
    for (const offset of content.offsets.slice(start,start+quote.length)) {
      if (!offset) continue;
      const known = portions.get(offset.node);
      portions.set(offset.node,{start:known ? Math.min(known.start,offset.start) : offset.start,
        end:known ? Math.max(known.end,offset.end) : offset.end});
    }
    for (const [node,portion] of portions) {
      const range = document.createRange(); range.setStart(node,portion.start); range.setEnd(node,portion.end);
      const mark = document.createElement('mark'); mark.className='quote-hit'; range.surroundContents(mark);
    }
    document.querySelector('mark.quote-hit')?.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
    reviewMessage('Quote highlighted in the listing.'); return;
  }
  reviewMessage('That quote is not visible in this presentation.');
});
document.addEventListener('toggle', async event => {
  const details=event.target;
  if (!details.matches?.('[data-diff-first]') || !details.open || details.dataset.loaded) return;
  const body=details.querySelector('[data-diff-body]'); body.textContent='Loading diff…';
  try {
    const response=await fetch('/review/diff?first='+encodeURIComponent(details.dataset.diffFirst)+'&second='+encodeURIComponent(details.dataset.diffSecond));
    if (!response.ok) throw Error();
    body.innerHTML=(await response.json()).html; details.dataset.loaded='true';
  } catch { body.textContent='The wording diff is temporarily unavailable.'; }
},true);
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
  updateReviewNavigation();
}
function updateReviewNavigation() {
  const root=document.querySelector('[data-review]'), data=root?.querySelector('[data-review-queue]'); if (!data) return;
  const ids=JSON.parse(data.textContent), index=ids.indexOf(Number(root.dataset.selected));
  for (const [name,position] of [['previous',index-1],['next',index+1]]) {
    const control=root.querySelector('[data-'+name+']'); if (!control) continue;
    const valid=index>=0 && position>=0 && position<ids.length;
    control.setAttribute('aria-disabled',String(!valid));
    if (valid) { const url=new URL(location.href); url.searchParams.set('id',ids[position]);url.searchParams.delete('advertiser_scope');url.searchParams.delete('advertiser_days'); control.href=url; }
    else control.href='#';
  }
  const position=root.querySelector('[data-review-position]');
  if (position) position.textContent=index>=0?(index+1)+' of '+ids.length+' on page':'Outside this queue page';
  const back=root.querySelector('[data-back-queue]');
  if (back && ids.length) { const url=new URL(location.href);url.searchParams.set('id',ids[0]);if(index!==0){url.searchParams.delete('advertiser_scope');url.searchParams.delete('advertiser_days');}back.href=url; }
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
async function refreshReview(url,focus=false) {
  if (filterActive && filterPending?.url.href===url.href) return;
  url=currentSearchFields(url,document.querySelector('[data-review-filters]'));
  latestFilterURL=new URL(url);filterPending={url,focus}; selectionVersion++;
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
      const latest=currentSearchFields(new URL(nextRequest.url),old.querySelector('[data-review-filters]'));
      if (latest.href!==nextRequest.url.href) {
        clearTimeout(searchTimer);filterPending={url:latest,focus:document.activeElement?.name||'q'};
      }
      if (filterPending) continue;
      const next=page.querySelector('[data-review]');
      if (!next) { location.href=response.url; return; }
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
      pinReview(nextRequest.url);
      if (nextRequest.focus) { const updated=next.querySelector('input[name="'+CSS.escape(focusName)+'"]');updated?.focus();updated?.setSelectionRange(start,end); }
    } catch { failureMessage='Could not refresh. Press Enter or Refresh to retry.'; }
  }
  filterActive=false;latestFilterURL=null; reviewBusy(false);if (failureMessage) reviewMessage(failureMessage);
}
function showReviewView(mode,url=new URL(location.href)) {
  const root=document.querySelector('[data-review]');if(!root)return;
  root.querySelector('[data-view-page]')?.toggleAttribute('hidden',mode!=='page');
  root.querySelector('[data-view-table]')?.toggleAttribute('hidden',mode!=='table');
  root.querySelectorAll('.view-switch a').forEach(link=>{
    const active=new URL(link.href).searchParams.get('view')===mode;
    link.classList.toggle('selected',active);
    if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
  root.querySelector('input[name=view]').value=mode;url.searchParams.set('view',mode);pinReview(url);
}
function openReviewPage(url) {
  const root=document.querySelector('[data-review]');
  const data=root?.querySelector('[data-review-queue]');
  if(!data)return;
  const id=url.searchParams.get('id')||root.dataset.selected||JSON.parse(data.textContent)[0];
  if(!id){showReviewView('page',url);return;}
  url.searchParams.set('id',id);url.searchParams.set('view','page');
  if(String(id)===root.dataset.selected){showReviewView('page',url);return;}
  selectReview(url);
}
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
      for (const [selector,key] of [['[data-review-history]','history'],['[data-review-listing]','listing'],['[data-review-role]','role']]) root.querySelector(selector).innerHTML=data[key];
      root.dataset.selected=String(data.id);showReviewView('page',target);
      root.querySelector('.listing-panel').scrollTop=0;
      root.querySelector('.left-panel').scrollTop=0;
      root.querySelector('.role-content').scrollTop=0;
      reviewMessage('');
    } catch { reviewMessage('The selected listing is unavailable. Refresh to retry.'); }
  }
  selectionActive=false;
}
document.addEventListener('click', event => {
  const anchor=event.target.closest('a');
  if (anchor?.getAttribute('aria-disabled')==='true') { event.preventDefault();return; }
  const review=document.querySelector('[data-review]'); if (!review) return;
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
    if(mode==='table')showReviewView('table');else openReviewPage(new URL(anchor.href));return;
  }
  if(anchor?.matches('[data-review-open]')){
    event.preventDefault();if(!filterActive)openReviewPage(new URL(anchor.href));return;
  }
  if (anchor?.closest('[data-review-filters]') && anchor.matches('[role=button]')) {
    event.preventDefault(); if (!filterActive) refreshReview(new URL(anchor.href));return;
  }
  if (anchor?.matches('[data-review-refresh]')) { event.preventDefault();if (!filterActive) refreshReview(new URL(location.href));return; }
  if (anchor?.matches('[data-review-select],[data-review-context],[data-next],[data-previous]') && review.querySelector('[data-review-listing]')) {
    event.preventDefault();if (!filterActive) selectReview(new URL(anchor.href));return;
  }
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
  if (event.target.matches('[data-review-filters] select[name=posted_by]')) { refreshReview(filterURL(event.target.form));return; }
  if (!event.target.matches('[data-review-seen]')) return;
  if (event.target.value) { event.target.form.querySelector('input[name=seen]').value=event.target.value;const url=filterURL(event.target.form);url.searchParams.set('seen',event.target.value);url.searchParams.delete('id');refreshReview(url); }
});
document.addEventListener('click', event => {
  const button=event.target.closest('[data-advanced-toggle]'); if (!button) return;
  const panel=button.form.querySelector('#advanced-search'); panel.open=!panel.open;
  button.setAttribute('aria-expanded',String(panel.open));button.querySelector('.chevron').textContent=panel.open?'▴':'▾';
});
document.addEventListener('keydown', event => {
  if (event.target.matches('input,select,textarea') || event.target.isContentEditable || event.ctrlKey || event.metaKey || event.altKey) return;
  if (!document.querySelector('[data-review]')) return;
  if(event.key==='Escape'){document.querySelectorAll('[data-popover][open]').forEach(pop=>pop.open=false);return;}
  if (event.key===' ') { const link=event.target.closest('a[role=button]');if (link) { event.preventDefault();link.click();return; } }
  if (event.key==='/') { event.preventDefault();document.querySelector('input[name=q]')?.focus();return; }
  if (event.key.toLowerCase()==='v') { event.preventDefault();const link=document.querySelector('.view-switch a:not(.selected)');if(link) link.click();return; }
  const name=['ArrowRight','d','D'].includes(event.key)?'next':['ArrowLeft','a','A'].includes(event.key)?'previous':null;
  const control=name && document.querySelector('[data-'+name+']');
  if (control && control.getAttribute('aria-disabled')!=='true') { event.preventDefault();control.click(); }
});
pinReview();
