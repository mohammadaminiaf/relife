'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => new Intl.NumberFormat().format(Number(value || 0));
const bytes = value => { const n = Number(value || 0); if (n < 1024) return n + ' B'; const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), 4); return (n / 1024 ** i).toFixed(i > 1 ? 1 : 0) + ' ' + ['B','KiB','MiB','GiB','TiB'][i]; };
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const statusLabels = {not_downloaded:'Needs download',unchecked:'Not checked',convertible:'Ready to convert',converted:'EPUB ready',unsupported:'Unsupported format'};
const statusHelp = {not_downloaded:'Download this original before checking its format.',unchecked:'Saved locally. Compatibility has not been checked yet.',convertible:'The downloaded container passed a check with the recovered reader.',converted:'An EPUB and its matching conversion receipt are saved on this computer.',unsupported:'The last format check could not read this downloaded container. The original was kept.'};
const languages = {fa:'Persian',ar:'Arabic',en:'English',az:'Azerbaijani',id:'Indonesian',fr:'French',zh:'Chinese',ur:'Urdu'};
const url = new URL(location.href);
const state = {view:url.searchParams.get('view') === 'jobs' ? 'jobs':'library', q:url.searchParams.get('q') || '',category:url.searchParams.get('category') || '',lang:url.searchParams.get('lang') || '',filter:url.searchParams.get('state') || 'all',page:1,pageSize:50,total:0,books:[],selected:new Set(),all:false,excluded:new Set(),jobs:[],summary:null,plan:null,online:false,request:0,jobSignature:'',polling:false};

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let value;
  try { value = await response.json(); } catch (_) { throw new Error('The library service returned an unreadable response.'); }
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}
let toastTimer;
let batchReviewRequest=0;
function toast(message, error=false) { $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,error?8500:4500); }
function showDialog(id) { document.querySelectorAll('dialog[open]').forEach(d=>d.close()); $(id).showModal(); }
function activeJob() { return state.jobs.find(j=>['running','pausing'].includes(j.state)); }
function syncBatchAvailability() {
  if(!state.plan || !$('batch-dialog').open || state.startingBatch)return;
  const active=activeJob();
  $('start-batch').disabled=!!active;
  if(active){$('batch-error').textContent='A job is already running. Pause it or wait for it to finish before starting this batch.';state.batchBusyWarning=true;}
  else if(state.batchBusyWarning){$('batch-error').textContent='';state.batchBusyWarning=false;}
}
function filters() { return {q:state.q,category:state.category,lang:state.lang,state:state.filter,sort:'id'}; }
function searchParams(values) { const params = new URLSearchParams(); Object.entries(values).forEach(([k,v])=>{if(v!=='' && v!==null && v!==undefined) params.set(k,v);}); return params; }
function syncURL() { const params=searchParams({...filters(),sort:null,view:state.view==='jobs'?'jobs':null}); if(params.get('state')==='all') params.delete('state'); history.replaceState(null,'',location.pathname+(params.size?'?'+params:'')); $('export-library').href='/library/export?'+searchParams(filters()); }
function setView(view, filter) {
  state.view=view;
  if(filter!==undefined && filter!==state.filter) { state.filter=filter; state.page=1; resetAllSelection(); $('book-state').value=filter; }
  $('library-view').hidden=view!=='library'; $('jobs-view').hidden=view!=='jobs';
  $('page-title').textContent=view==='jobs'?'Every batch, accounted for.':'Recover your library.';
  $('page-description').textContent=view==='jobs'?'Your progress stays saved. Resume a batch whenever you’re ready.':'Find your books, restore the originals, and give them a new home in EPUB.';
  $('breadcrumb-view').textContent=view==='jobs'?'Recovery jobs':'Library';
  document.querySelectorAll('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view && (view==='jobs'||b.dataset.state===state.filter)));
  syncURL(); if(view==='library') loadBooks(); else renderJobs();
}
function resetAllSelection() { state.all=false; state.excluded.clear(); }
function isSelected(id) { return state.all ? !state.excluded.has(Number(id)) : state.selected.has(Number(id)); }
function selectedCount() { return state.all ? Math.max(0,state.total-state.excluded.size) : state.selected.size; }
function renderSelection() {
  const count=selectedCount();
  $('selection-count').textContent=count?`${number(count)} book${count===1?'':'s'} selected`:'Batch uses current filters';
  $('clear-selection').hidden=count===0;
  $('select-matching').hidden=state.all || !state.total;
  const selected=state.books.filter(b=>isSelected(b.id)).length;
  $('select-page').checked=state.books.length>0 && selected===state.books.length;
  $('select-page').indeterminate=selected>0 && selected<state.books.length;
  $('select-page').disabled=!state.books.length;
  document.querySelectorAll('[data-select-book]').forEach(input=>{input.checked=isSelected(input.dataset.selectBook); input.closest('tr').classList.toggle('selected',input.checked);});
}
function statusBadge(local) { return `<span class="status-badge ${escapeHTML(local.status)}" title="${escapeHTML(statusHelp[local.status]||'')}">${escapeHTML(statusLabels[local.status]||'Unknown')}</span>`; }
function renderBooks() {
  const colors=['#a9b59a','#b9a98b','#8fa99a','#a9a4bb','#b99886','#99abb4'];
  $('book-rows').innerHTML=state.books.length ? state.books.map(book=>{
    const local=book.local, id=Number(book.id), category=book.categories?.[0];
    const files=local.epub_available ? `<a class="button outlined" href="${local.epub_url}" download aria-label="Save EPUB for book ${id}">EPUB ${icon('download')}</a>` : `<button class="icon-button" data-restore-book="${id}" title="Download and convert this book" aria-label="Download and convert book ${id}">${icon('download')}</button>`;
    return `<tr class="${isSelected(id)?'selected':''}"><td class="checkbox-cell"><input type="checkbox" data-select-book="${id}" aria-label="Select book ${id}" ${isSelected(id)?'checked':''}></td><td class="book-cell"><div class="book-entry"><span class="book-glyph" style="--book-color:${colors[id%colors.length]}">${icon('book')}</span><div><button class="book-title" dir="auto" data-detail="${id}">${escapeHTML(book.title)}</button><span class="book-author" dir="auto"><span class="book-id">#${id}</span> ${escapeHTML(book.author||'Unknown author')}</span></div></div></td><td><span class="category-name" dir="auto" title="${escapeHTML(book.categories?.map(c=>c.title).join(' / ')||'')}">${escapeHTML(category?.title||'Uncategorized')}</span></td><td><span class="language-pill">${escapeHTML(languages[book.lang]||book.lang||'—')}</span></td><td>${statusBadge(local)}${local.error?`<button class="icon-button" data-detail="${id}" title="Review the last error" aria-label="Review error for book ${id}">${icon('alert')}</button>`:''}</td><td class="file-size">${bytes(book.file_size)}</td><td class="actions-cell"><div class="row-actions">${files}<button class="icon-button" data-detail="${id}" title="Book details" aria-label="Details for book ${id}">${icon('arrow')}</button></div></td></tr>`;
  }).join('') : `<tr><td class="empty-state" colspan="7"><b>No books match these filters.</b>Try another name, category or status. Your saved files are still here.<br><button class="text-button" data-reset-filters>Clear filters</button></td></tr>`;
  const from=state.total?(state.page-1)*state.pageSize+1:0, to=Math.min(state.page*state.pageSize,state.total);
  $('page-label').textContent=`${number(from)}–${number(to)} of ${number(state.total)} books`;
  $('result-summary').textContent=`${number(state.total)} ${state.total===1?'book':'books'}${state.q||state.category||state.lang||state.filter!=='all'?' matching your filters':' available to rediscover'}`;
  $('previous-page').disabled=state.page<=1; $('next-page').disabled=state.page*state.pageSize>=state.total;
  const hasFilters=!!(state.q||state.category||state.lang||state.filter!=='all');
  $('filter-context').hidden=!hasFilters;
  $('active-filter-label').textContent=[state.q?`“${state.q}”`:'',state.category?$('category').selectedOptions[0]?.textContent:'',state.lang?languages[state.lang]||state.lang:'',state.filter!=='all'?$('book-state').selectedOptions[0]?.textContent:''].filter(Boolean).join(' · ');
  renderSelection(); syncURL();
}
async function loadBooks() {
  const request=++state.request;
  $('table-card').setAttribute('aria-busy','true');
  try {
    const data=await api('/books?'+searchParams({...filters(),page:state.page,page_size:state.pageSize}));
    if(request!==state.request) return;
    state.books=data.books;state.total=data.total;
    if(!data.books.length && state.page>1 && data.total>0) {state.page=1;return loadBooks();}
    renderBooks();
  } catch(error) {
    if(request!==state.request)return;
    $('book-rows').innerHTML=`<tr><td colspan="7" class="empty-state"><b>We couldn’t load these books.</b>${escapeHTML(error.message)}<br><button class="text-button" data-reset-filters>Reset filters</button></td></tr>`;
    toast(error.message,true);
  } finally { if(request===state.request)$('table-card').setAttribute('aria-busy','false'); }
}
async function loadCategories() {
  const data=await api('/categories');
  state.categories=data.categories;
  $('category').innerHTML='<option value="">All categories</option>'+data.categories.map(c=>`<option value="${escapeHTML(c.id)}">${escapeHTML(c.title)}${c.title_fa?' · '+escapeHTML(c.title_fa):''} (${number(c.book_count)})</option>`).join('');
  $('category').value=state.category;
  if($('category-dialog').open)renderCategorySearch();
}
function categoryKey(text) {return String(text).normalize('NFKD').replace(/[\u0300-\u036f\u064b-\u065f\u0670]/g,'').replace(/[كيى]/g,c=>c==='ك'?'ک':'ی').toLowerCase();}
function renderCategorySearch() {
  const words=categoryKey($('category-search').value).split(/\s+/).filter(Boolean);
  const categories=state.categories||[];
  const matches=categories.filter(c=>words.length?words.every(word=>categoryKey(c.title+' '+(c.title_fa||'')+' '+(c.title_ar||'')).includes(word)):c.parent_id==='-1');
  $('category-result-count').textContent=number(matches.length)+(words.length?' matching categories':' main categories')+(matches.length>50?' · showing the first 50':'');
  $('category-results').innerHTML=matches.slice(0,50).map(c=>`<button class="category-result" data-pick-category="${escapeHTML(c.id)}"><span><b>${escapeHTML(c.title)}</b><bdi>${escapeHTML(c.title_fa||c.title_ar||'')}</bdi></span><small>${number(c.book_count)} books</small></button>`).join('')||'<p class="subtle">No categories found. Try a shorter name or another language.</p>';
}
function renderSummary(summary) {
  state.summary=summary;
  document.querySelectorAll('[data-count]').forEach(el=>el.textContent=number(summary[el.dataset.count]));
  $('catalog-size').textContent=bytes(summary.catalog_bytes)+' of original books';
  $('original-size').textContent=bytes(summary.original_bytes)+' saved on this computer';
  $('epub-size').textContent=summary.converted?bytes(summary.epub_bytes)+' of portable EPUBs':'Ready for your updated app';
  $('unchecked-count').textContent=number(summary.unchecked)+' saved originals not yet checked';
  $('downloads-path').textContent=summary.downloads_directory; $('epubs-path').textContent=summary.epubs_directory;
  $('disk-space').textContent=bytes(summary.disk_free_bytes)+' free on your library’s drive.';
  const languageSelect=$('language');
  if(languageSelect.options.length===1) {
    languageSelect.innerHTML='<option value="">All languages</option>'+Object.entries(summary.languages).sort((a,b)=>b[1]-a[1]).map(([code,count])=>`<option value="${escapeHTML(code)}">${escapeHTML(languages[code]||code)} (${number(count)})</option>`).join('');
    languageSelect.value=state.lang;
  }
}
function modeName(mode) {return {restore:'Download + EPUB',download:'Download originals',convert:'Convert saved books',check:'Check compatibility'}[mode];}
function renderGuided() {
  if(!state.guided)return;
  const active=activeJob();
  const signature=JSON.stringify([state.guided,active,state.online]);
  if(signature===state.guidedRenderSignature)return;
  state.guidedRenderSignature=signature;
  $('guided-start').disabled=!!active||!state.online||(!state.guided.queues.download.next_count&&!state.guided.queues.download.resume_job);
  $('guided-cards').innerHTML=['download','convert'].map(kind=>{
    const q=state.guided.queues[kind], downloading=kind==='download';
    const job=active?.options.guided&&active.kind===kind?active:null;
    const percent=100*q.completed/Math.max(q.total,1);
    const count=q.next_count, resume=q.resume_job;
    const label=resume?'Review unfinished batch':count?`Review next ${count} ${downloading?'downloads':'conversions'}`:downloading?(q.completed===q.total?'All originals saved':'No new downloads ready'):'No conversions ready';
    const description=downloading?'Originals are saved automatically. Books already on disk are skipped.':'Saved originals become EPUBs automatically. Finished EPUBs are skipped.';
    let progress=resume?`Your unfinished batch has ${number(resume.counts.pending+resume.counts.failed)} books left. Review it to resume.`:count?`${number(q.ready)} ready for future batches. Each batch waits for your confirmation.`:q.needs_attention?'Remaining books need attention. Review a retry batch or use the manual controls.':downloading?'All available originals are saved.':'Download more originals to make them available for conversion.';
    if(!count&&!resume&&!q.needs_attention){
      if(q.reserved)progress='Finish your unfinished jobs to continue. Open View jobs to review them.';
      else if(q.completed===q.total)progress=downloading?'All available originals are saved.':'All available originals have an EPUB.';
    }
    if(job)progress=`Batch in progress: ${number(job.counts.done+job.counts.skipped)} of ${number(job.total)} saved, ${number(job.counts.failed)} failed.${job.active_book?' Working on #'+job.active_book.id+'.':''}`;
    return `<article class="guided-card" data-guided-card="${kind}"><div class="guided-card-heading"><h3>${downloading?'Download library':'Convert library to EPUB'}</h3>${icon(downloading?'download':'book')}</div><p class="subtle">${description}</p><div class="guided-total"><strong>${number(q.completed)}</strong><span>of ${number(q.total)} ${downloading?'originals saved':'EPUBs ready'}</span></div><div class="job-progress" role="progressbar" aria-label="${downloading?'Library download':'Library conversion'} progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(percent)}"><span style="width:${percent}%"></span></div><p class="guided-progress" data-guided-progress="${kind}">${escapeHTML(progress)}</p><div class="guided-counts">${q.waiting_download?`<span>${number(q.waiting_download)} awaiting download</span>`:''}${q.needs_attention?`<span>${number(q.needs_attention)} need attention</span>`:''}${q.reserved&&!resume?`<span>${number(q.reserved)} in unfinished jobs</span>`:''}</div><div class="guided-actions"><button class="button primary small" data-guided="${kind}" ${active||(!count&&!resume)||!state.online?'disabled':''}>${escapeHTML(label)} ${icon('arrow')}</button>${job?`<button class="button outlined small" data-job-action="pause" data-job-id="${job.id}" ${job.state==='pausing'?'disabled':''}>${job.state==='pausing'?'Pausing…':'Pause batch'}</button>`:''}${q.retryable&&!resume?`<button class="text-button" data-guided-retry="${kind}" ${active||!state.online?'disabled':''}>Review retries (${number(Math.min(q.batch_size,q.retryable))})</button>`:''}<button class="text-button" data-view="jobs">View jobs</button></div></article>`;
  }).join('');
  if(state.summary)$('guided-storage').textContent=`Saved automatically on this computer: originals in ${state.summary.downloads_directory} · EPUBs in ${state.summary.epubs_directory}`;
}
async function reviewGuided(kind, retry=false) {
  const resume=state.guided?.queues[kind]?.resume_job;
  if(!resume)return reviewBatch({mode:kind,kind,guided:true,retry});
  const request=++batchReviewRequest;
  state.plan=null;
  $('batch-title').textContent='Resume your unfinished batch';
  $('batch-description').textContent=modeName(kind)+' · Completed books remain saved.';
  $('batch-error').textContent='';$('start-batch').textContent='Confirm resume';$('start-batch').disabled=true;
  $('batch-plan').innerHTML='<p class="subtle">Checking your saved batch…</p>';showDialog('batch-dialog');
  try {
    const job=await api('/jobs/'+resume.id);
    if(!$('batch-dialog').open||request!==batchReviewRequest)return;
    const remaining=job.counts.pending+job.counts.failed;
    $('batch-plan').innerHTML=`<div class="plan-numbers"><div><b>${number(remaining)}</b><span>books remaining in this batch</span></div><div><b>${number(job.counts.done+job.counts.skipped)}</b><span>already completed</span></div></div><p class="plan-notes">Continue this saved batch first. We’ll prepare the next batch of up to 50 after it finishes. Previously failed books in this batch will also be retried.</p>`;
    state.plan={mode:kind,kind,guidedResume:job.id};syncBatchAvailability();
  }catch(error){if(request===batchReviewRequest)$('batch-error').textContent=error.message;}
}
function buildBatch(override) {
  if(override)return override;
  const mode=$('batch-mode').value;
  let options=state.all||!state.selected.size?{scope:'all',...filters(),exclude_ids:[...state.excluded]}:{scope:'ids',ids:[...state.selected]};
  const raw=$('batch-size').value.trim();
  if(raw && (!/^\d+$/.test(raw)||Number(raw)<1))throw new Error('Enter a positive batch size, or clear it to include all matches.');
  options.limit=raw?Number(raw):null;
  options.convert_epub=mode==='restore';
  if($('skip-completed').checked)options[mode==='restore'||mode==='convert'?'skip_converted':mode==='check'?'skip_checked':'skip_downloaded']=true;
  return {mode,kind:mode==='convert'?'convert':mode==='check'?'check':'download',options};
}
async function reviewBatch(override) {
  const request=++batchReviewRequest;
  try {
    const batch=buildBatch(override);
    state.plan=null;
    $('batch-title').textContent=batch.guided?'Review your next batch':override?.entire?'Restore the entire library':'Review your batch';
    $('batch-description').textContent=modeName(batch.mode)+(batch.kind==='check'||batch.kind==='convert'?' · Only saved originals are included.':' · Original files stay unchanged.');
    $('batch-plan').innerHTML='<p class="subtle">Calculating your selection…</p>'; $('batch-error').textContent=''; $('start-batch').disabled=true;
    $('start-batch').textContent=batch.guided?'Confirm batch':'Start batch';
    showDialog('batch-dialog');
    const plan=batch.guided?await api('/guided/plan',{kind:batch.kind,retry:batch.retry}):await api('/library/plan',{...batch.options,kind:batch.kind});
    if(!$('batch-dialog').open||request!==batchReviewRequest)return;
    state.plan={...batch,plan};
    if(batch.guided){$('batch-title').textContent=`Confirm ${batch.retry?'retry of':'next'} ${number(plan.books)} ${batch.kind==='download'?'downloads':'conversions'}`;$('batch-description').textContent='Chosen automatically from your whole library. The next batch will wait for a new confirmation.';}
    $('batch-plan').innerHTML=`<div class="plan-numbers"><div><b>${number(plan.books)}</b><span>books in this batch</span></div><div><b>${batch.kind==='download'?bytes(plan.missing_ghm_bytes):'Local files'}</b><span>${batch.kind==='download'?'of originals to download':'no originals will be downloaded'}</span></div></div><div class="plan-notes">${number(plan.existing_files_to_verify)} original files already saved. ${bytes(plan.disk_free_bytes)} free on this drive.<br>${batch.mode==='restore'?'EPUBs are created after each download and require additional space.':batch.mode==='check'?'The reader checks the downloaded container and records whether its text is compatible. No EPUB is created.':batch.mode==='convert'?'Compatible local originals are exported to the EPUB folder.':'Each saved file includes a checksum receipt.'}</div><ul class="plan-samples">${plan.sample.map(b=>`<li><bdi>${escapeHTML(b.title)}</bdi><span>#${b.id}</span></li>`).join('')}</ul>${plan.books>plan.sample.length?`<p class="subtle">…and ${number(plan.books-plan.sample.length)} more books.</p>`:''}`;
    syncBatchAvailability();
  } catch(error) { if(request===batchReviewRequest)$('batch-error').textContent=error.message==='No books match the selection'?'No eligible books remain in this selection. Change the filters or turn off “Skip completed”.':error.message; }
}
async function startBatch() {
  if(!state.plan)return;
  state.startingBatch=true;
  $('start-batch').disabled=true; $('batch-error').textContent='';
  try {
    const batch=state.plan, endpoint=batch.kind==='convert'?'/conversions':batch.kind==='check'?'/checks':'/jobs';
    const job=batch.guidedResume?await api(`/jobs/${batch.guidedResume}/resume`,{}):batch.guided?await api('/guided/start',{token:batch.plan.token}):await api(endpoint,batch.options);
    if(batch.guided||batch.guidedResume)state.followGuided=job.id;
    $('batch-dialog').close();state.selected.clear();resetAllSelection();
    toast(`${modeName(batch.mode)} started for ${number(job.total)} books. Your progress is saved automatically.`);
    await refreshJobs();setView('jobs');poll();
  } catch(error) { $('batch-error').textContent=error.message; $('start-batch').disabled=false; }
  finally {state.startingBatch=false;syncBatchAvailability();}
}
function jobLabel(job) { return job.kind==='check'?'Compatibility check':job.kind==='convert'?'EPUB conversion':job.options.convert_epub?'Download + EPUB':'Original book download'; }
function jobState(job) { return {running:'In progress',pausing:'Pausing…',paused:'Paused',interrupted:'Ready to resume',completed:'Completed',completed_with_errors:'Completed with errors',failed:'Needs attention'}[job.state]||job.state; }
function renderJobs() {
  syncBatchAvailability();
  renderGuided();
  $('job-count').textContent=number(state.jobs.length);
  const active=activeJob();
  $('sidebar-job').hidden=!active;
  if(active) {
    const done=active.counts.done+active.counts.skipped;
    $('sidebar-job').innerHTML=`<b>${escapeHTML(jobLabel(active))}</b><p>${number(done)} of ${number(active.total)} books</p><div class="mini-progress"><span style="width:${100*(done+active.counts.failed)/active.total}%"></span></div>`;
  }
  $('job-list').innerHTML=state.jobs.length?state.jobs.map(job=>{
    const c=job.counts, done=c.done+c.skipped, percent=100*(done+c.failed)/Math.max(job.total,1);
    const stateClass=job.state==='completed'?'converted':job.state==='completed_with_errors'||job.state==='failed'?'failed':'convertible';
    const action=job.state==='running'?`<button class="button outlined small" data-job-action="pause" data-job-id="${job.id}">${icon('pause')}Pause</button>`:['paused','interrupted','failed','completed_with_errors'].includes(job.state)?`<button class="button outlined small" data-job-action="resume" data-job-id="${job.id}" ${active?'disabled':''}>${icon('play')}${job.state==='completed_with_errors'?'Retry failures':'Resume'}</button>`:'';
    return `<article class="job-card"><div class="job-card-header"><div><h3>${escapeHTML(jobLabel(job))} · ${number(job.total)} books</h3><div class="job-meta">${escapeHTML(new Date(job.created).toLocaleString())} · ${escapeHTML(job.options.q?`“${job.options.q}”`:'')}${job.options.lang?' · '+escapeHTML(languages[job.options.lang]||job.options.lang):''}<br>Job ${job.id.slice(0,12)}</div></div><span class="status-badge ${stateClass}">${escapeHTML(jobState(job))}</span></div><div class="job-progress" role="progressbar" aria-label="Batch progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(percent)}"><span style="width:${percent}%"></span></div><div class="job-counts"><span><b>${number(c.done)}</b> completed</span><span><b>${number(c.skipped)}</b> already saved</span><span><b>${number(c.pending)}</b> remaining</span><span><b>${number(c.failed)}</b> failed</span></div>${job.active_book?`<div class="job-active">Working on <bdi>${escapeHTML(job.active_book.title)}</bdi> · #${job.active_book.id}</div>`:''}${job.error?`<div class="job-error">${escapeHTML(job.error)}</div>`:''}<div class="job-actions">${action}<button class="button ghost small" data-job-results="${job.id}">View results</button>${c.failed?`<button class="button ghost small" data-job-failures="${job.id}">Review ${number(c.failed)} failures</button>`:''}</div></article>`;
  }).join(''):`<div class="empty-state"><b>Your first batch starts a new chapter.</b>Select books in the library and review a batch to begin.<br><button class="button outlined small" data-back-library>Browse library</button></div>`;
}
async function refreshJobs() { const [data,guided]=await Promise.all([api('/jobs'),api('/guided')]);state.jobs=data.jobs;state.guided=guided;renderJobs(); }
async function jobAction(id, action, button) {
  button.disabled=true;
  try { await api(`/jobs/${id}/${action}`,{});await refreshJobs();toast(action==='pause'?'Pausing after the current transfer settles. Completed files stay saved.':'Job resumed. Completed books are preserved.'); }
  catch(error) { toast(error.message,true);button.disabled=false; }
}
async function showJobResults(id, failed=false, page=1) {
  try {
    const data=await api(`/jobs/${id}/items?${searchParams({status:failed?'failed':null,page,page_size:30})}`);
    $('detail-id').textContent=failed?'BATCH FAILURES':'BATCH RESULTS';
    $('detail-content').innerHTML=`<h2 class="detail-title">${failed?'Books needing attention':'Every book in this batch'}</h2><p class="subtle">${number(data.total)} results. Retrying a job only repeats pending or failed books.</p>${data.items.map(item=>`<div class="result-row"><button class="text-button" data-detail="${item.book_id}">Book #${item.book_id}</button><span class="status-badge ${item.status==='failed'?'failed':'convertible'}">${escapeHTML(item.status)}</span>${item.error?`<pre>${escapeHTML(item.error)}</pre>`:''}${item.result?.epub?.path?`<div class="result-links"><a href="/epubs/${item.book_id}.epub" download>Save EPUB ${icon('download')}</a></div>`:''}</div>`).join('')||'<p class="subtle">No results in this view.</p>'}<div class="dialog-actions">${page>1?`<button class="button outlined small" data-result-page="${page-1}" data-result-id="${id}" data-result-failed="${failed}">Previous</button>`:''}${page*30<data.total?`<button class="button outlined small" data-result-page="${page+1}" data-result-id="${id}" data-result-failed="${failed}">Next results</button>`:''}</div>`;
    showDialog('detail-dialog');
  }catch(error){toast(error.message,true);}
}
async function showBook(id) {
  $('detail-id').textContent='BOOK #'+id;$('detail-content').innerHTML='<p class="subtle">Loading book details…</p>';showDialog('detail-dialog');
  try {
    const book=await api('/books/'+id), local=book.local;
    if(!$('detail-dialog').open)return;
    $('detail-content').innerHTML=`<h2 class="detail-title" dir="auto">${escapeHTML(book.title)}</h2><p class="detail-author" dir="auto">${escapeHTML(book.author||'Unknown author')}</p>${statusBadge(local)}<dl class="detail-grid"><div><dt>Book ID</dt><dd>${id}</dd></div><div><dt>Language</dt><dd>${escapeHTML(languages[book.lang]||book.lang)}</dd></div><div><dt>Original size</dt><dd>${bytes(book.file_size)}</dd></div><div><dt>EPUB size</dt><dd>${local.epub_available?bytes(local.epub_bytes):'Not created yet'}</dd></div><div><dt>Version</dt><dd>${escapeHTML(book.version||'—')}</dd></div><div><dt>Publisher</dt><dd dir="auto">${escapeHTML(book.publisher||'—')}</dd></div></dl><h3>Categories</h3><p class="subtle" dir="auto">${escapeHTML(book.categories.map(c=>c.title+(c.title_fa?' · '+c.title_fa:'')).join(' / ')||'Uncategorized')}</p><div class="detail-status"><strong>${local.can_convert===true?'Compatible with the recovered reader':local.can_convert===false?'This container could not be read':'Format not checked yet'}</strong><p>${escapeHTML(statusHelp[local.status])}</p>${local.checked_at?`<p>Last checked: ${escapeHTML(new Date(local.checked_at).toLocaleString())}</p>`:''}</div><div class="detail-actions">${local.epub_available?`<a class="button primary" href="${local.epub_url}" download>Save EPUB ${icon('download')}</a>`:''}${local.downloaded?`<a class="button outlined" href="${local.ghm_url}" download>Save original ${icon('download')}</a><button class="button outlined" data-check-book="${id}">Check format</button><button class="button outlined" data-convert-book="${id}">Convert to EPUB</button>`:`<button class="button primary" data-restore-book="${id}">Download + EPUB ${icon('arrow')}</button>`}</div>${local.missing_media_count?`<div class="notice">${number(local.missing_media_count)} media references were not bundled or supported. Their placeholders and a list are kept in the conversion receipt.</div>`:''}${local.error?`<details class="help-details"><summary>Last error · technical details</summary><pre>${escapeHTML(local.error)}</pre></details>`:''}${local.epub_available?`<a class="text-button" href="/receipts/${id}.json?type=epub" download>Save conversion receipt</a>`:''}`;
  }catch(error){$('detail-content').innerHTML=`<p class="error-text">${escapeHTML(error.message)}</p>`;}
}
function resetFilters() { state.q='';state.category='';state.lang='';state.filter='all';state.page=1;resetAllSelection();$('search').value='';$('category').value='';$('language').value='';$('book-state').value='all';setView('library','all'); }
function connected(online) { state.online=online;$('connection-dot').classList.toggle('offline',!online);$('connection-label').textContent=online?'Local library connected':'Service disconnected'; }
async function poll() {
  if(state.polling)return;state.polling=true;
  try {
    const [summary,jobs,guided]=await Promise.all([api('/library/summary'),api('/jobs'),api('/guided')]);
    connected(true);renderSummary(summary);state.jobs=jobs.jobs;state.guided=guided;renderJobs();
    const finished=state.jobs.find(j=>j.id===state.followGuided&&['completed','completed_with_errors','failed'].includes(j.state));
    if(finished){state.followGuided=null;toast('Batch finished. Review the next batch when you’re ready. Failed books are kept for retry.');}
    const signature=JSON.stringify(jobs.jobs.map(j=>[j.id,j.state,j.counts,j.active_book?.id]));
    if(signature!==state.jobSignature){state.jobSignature=signature;if(state.view==='library')await loadBooks();}
  }catch(error){if(state.online)toast('The local service disconnected. Restart the library launcher to reconnect.',true);connected(false);renderGuided();}
  finally{state.polling=false;}
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button,a');if(!button)return;
  if(button.matches('[data-close]')){button.closest('dialog').close();return;}
  if(button.dataset.guided){reviewGuided(button.dataset.guided);return;}
  if(button.dataset.guidedRetry){reviewGuided(button.dataset.guidedRetry,true);return;}
  if(button.dataset.view){setView(button.dataset.view,button.dataset.state);return;}
  if(button.hasAttribute('data-reset-filters')){resetFilters();return;}
  if(button.hasAttribute('data-back-library')){setView('library');return;}
  if(button.dataset.pickCategory){state.category=button.dataset.pickCategory;state.page=1;resetAllSelection();$('category').value=state.category;$('category-dialog').close();setView('library');return;}
  if(button.dataset.detail){showBook(Number(button.dataset.detail));return;}
  for(const [attribute,mode,kind] of [['restoreBook','restore','download'],['convertBook','convert','convert'],['checkBook','check','check']]) {
    if(button.dataset[attribute]){reviewBatch({mode,kind,options:{scope:'ids',ids:[Number(button.dataset[attribute])],convert_epub:mode==='restore'}});return;}
  }
  if(button.dataset.jobAction){jobAction(button.dataset.jobId,button.dataset.jobAction,button);return;}
  if(button.dataset.jobResults){showJobResults(button.dataset.jobResults);return;}
  if(button.dataset.jobFailures){showJobResults(button.dataset.jobFailures,true);return;}
  if(button.dataset.resultPage){showJobResults(button.dataset.resultId,button.dataset.resultFailed==='true',Number(button.dataset.resultPage));return;}
  if(button.dataset.openFolder){api('/open-folder',{folder:button.dataset.openFolder}).then(()=>toast('Opened your saved files folder.')).catch(e=>toast(e.message,true));}
});
$('book-rows').addEventListener('change',event=>{const input=event.target.closest('[data-select-book]');if(!input)return;const id=Number(input.dataset.selectBook);if(state.all){input.checked?state.excluded.delete(id):state.excluded.add(id);}else{input.checked?state.selected.add(id):state.selected.delete(id);}renderSelection();});
$('select-page').addEventListener('change',event=>{state.books.forEach(book=>{const id=Number(book.id);if(state.all){event.target.checked?state.excluded.delete(id):state.excluded.add(id);}else{event.target.checked?state.selected.add(id):state.selected.delete(id);}});renderSelection();});
$('select-matching').onclick=()=>{state.all=true;state.selected.clear();state.excluded.clear();renderSelection();};
$('clear-selection').onclick=()=>{state.selected.clear();resetAllSelection();renderSelection();};
let searchTimer;$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.q=$('search').value.trim();state.page=1;resetAllSelection();loadBooks();},300);});
for(const [id,key] of [['category','category'],['language','lang'],['book-state','filter']])$(id).addEventListener('change',()=>{state[key]=$(id).value;state.page=1;resetAllSelection();setView('library');});
$('page-size').onchange=()=>{state.pageSize=Number($('page-size').value);state.page=1;loadBooks();};
$('previous-page').onclick=()=>{if(state.page>1){state.page--;loadBooks();}};
$('next-page').onclick=()=>{if(state.page*state.pageSize<state.total){state.page++;loadBooks();}};
$('review-batch').onclick=()=>reviewBatch();$('start-batch').onclick=startBatch;
$('guided-start').onclick=()=>reviewGuided('download');
$('restore-all').onclick=()=>reviewBatch({mode:'restore',kind:'download',entire:true,options:{scope:'all',convert_epub:true,skip_converted:true}});
$('clear-filters').onclick=resetFilters;
$('find-category').onclick=()=>{$('category-search').value='';renderCategorySearch();showDialog('category-dialog');$('category-search').focus();};
$('category-search').oninput=renderCategorySearch;
$('jobs-shortcut').onclick=()=>setView('jobs');$('sidebar-job').onclick=()=>setView('jobs');
$('refresh-jobs').onclick=()=>refreshJobs().catch(e=>toast(e.message,true));
$('storage-button').onclick=$('open-files').onclick=()=>showDialog('storage-dialog');
$('refresh-catalog').onclick=async()=>{const button=$('refresh-catalog');button.disabled=true;toast('Refreshing catalog and categories…');try{const result=await api('/catalog/refresh',{});await loadCategories();await poll();await loadBooks();toast(`Catalog refreshed: ${number(result.books)} books.`);}catch(error){toast(error.message,true);}finally{button.disabled=false;}};
document.addEventListener('keydown',event=>{if(event.key==='/'&&!document.querySelector('dialog[open]')&&!['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)){event.preventDefault();$('search').focus();}});
(async()=>{
  $('search').value=state.q;$('book-state').value=state.filter;
  try{const info=await api('/health');connected(true);if(!info.conversion_runtime){$('runtime-notice').hidden=false;$('runtime-notice').textContent='Downloads are available. To enable compatibility checks and EPUB conversion, run setup.cmd on Windows or ./setup.sh --app-only on macOS/Linux, then reopen the library.';}await loadCategories();await poll();setView(state.view);}
  catch(error){connected(false);$('book-rows').innerHTML=`<tr><td colspan="7" class="empty-state"><b>Your library service is not ready.</b>${escapeHTML(error.message)}<br>Start library.cmd on Windows or ./library.sh on macOS/Linux, then reload this page.</td></tr>`;}
  setInterval(poll,3500);
})();
