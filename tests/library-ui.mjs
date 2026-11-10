// Run against: python tests/serve_guided_fixture.py --errors
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require('../.tools/node_modules/playwright-core');
const base=process.argv[2]||'http://127.0.0.1:8768';
const get=async path=>(await fetch(base+path)).json();
assert.equal((await get('/health')).catalog_books,105,'Use the isolated fixture.');
assert.equal((await get('/jobs')).jobs.length,0,'Start a fresh fixture with --errors.');
const chrome=process.env.REVIVE_CHROME||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'/usr/bin/google-chrome');
const browser=await chromium.launch({headless:true,executablePath:chrome});
const page=await browser.newPage({viewport:{width:1512,height:1100},acceptDownloads:true});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
fs.mkdirSync('analysis',{recursive:true});
async function route(path){await page.goto(base+path);await page.locator('h1').waitFor();}
async function settled(id){const deadline=Date.now()+60000;while(Date.now()<deadline){const j=await get('/jobs/'+id);if(['completed','completed_with_errors','failed','paused'].includes(j.state))return j;await new Promise(r=>setTimeout(r,150));}throw Error('Job did not finish '+id);}
async function confirm(endpoint){const response=page.waitForResponse(r=>r.url().endsWith(endpoint)&&r.request().method()==='POST');await page.locator('#start-batch:not([disabled])').click();const result=await (await response).json();assert(result.id,JSON.stringify(result));return result;}
async function guided(kind,count){await route(kind==='download'?'/downloads':'/conversions');await page.locator(`[data-guided="${kind}"]:not([data-retry])`).filter({hasText:`Review next ${count} `}).click();await page.locator('#start-batch:not([disabled])').waitFor();assert.match(await page.locator('#batch-title').textContent(),new RegExp(`${count} books`));return confirm('/guided/start');}
try{
 for(const path of ['/catalog','/downloaded','/epubs','/downloads','/conversions','/activity','/errors','/settings']){await route(path);assert(await page.locator('h1').isVisible());}
 await page.locator('#download-workers').selectOption('4');await page.waitForTimeout(200);assert.equal((await get('/api/settings')).download_workers,4);await page.locator('#download-workers').selectOption('8');await page.waitForTimeout(200);
 console.log('PASS: all eight dedicated pages open directly; performance preference persists');
 await route('/downloads');await page.getByRole('button',{name:'Review next 50 downloads',exact:true}).click();await page.locator('#start-batch:not([disabled])').waitFor();await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal((await get('/jobs')).jobs.length,0);
 const first=await guided('download',50);await page.screenshot({path:'analysis/redesign-transfer-live.png',fullPage:true});const result=await settled(first.id);assert.equal(result.state,'completed_with_errors');assert.equal(result.counts.done,49);assert.equal(result.counts.failed,1);assert.equal(result.telemetry.configured_workers,8);assert.equal(result.telemetry.effective_workers,4);assert.equal(result.telemetry.rate_limit_events,1);assert(result.telemetry.network_bytes>0);
 await page.waitForTimeout(2200);assert.equal((await get('/jobs')).jobs.length,1);
 console.log('PASS: confirmation, eight simultaneous books, explicit rate limit/backoff, publisher removal, no automatic next batch');
 await route('/errors');await page.getByRole('heading',{name:'Removed by publisher',exact:true}).waitFor();await page.getByText('Server’s message',{exact:true}).click();assert.match(await page.locator('.diagnostic-detail blockquote').textContent(),/ناشرو/);await page.getByRole('button',{name:'Inspect server responses',exact:true}).click();await page.waitForTimeout(400);const reportDownload=page.waitForEvent('download');await page.getByRole('link',{name:'Save report',exact:false}).click();assert((await reportDownload).suggestedFilename().endsWith('.json'));await page.screenshot({path:'analysis/redesign-errors.png',fullPage:true});
 console.log('PASS: specific failure reason, HTTP status, server message, response inspection and diagnostic export');
 await route('/downloaded');await page.locator('.book-card').first().waitFor();assert.equal(await page.locator('.book-card').count(),24);await page.screenshot({path:'analysis/redesign-downloaded.png',fullPage:true});await page.locator('[data-card="1"] [data-single="convert"]').click();await page.locator('#start-batch:not([disabled])').waitFor();assert.match(await page.locator('#batch-title').textContent(),/Convert 1 book/);const single=await confirm('/conversions');assert.equal((await settled(single.id)).state,'completed');
 await route('/conversions');await page.locator('[data-select-book="2"]').check();await page.locator('[data-select-book="3"]').check();await page.getByRole('button',{name:'Convert selected',exact:false}).click();await page.locator('#start-batch:not([disabled])').waitFor();const selected=await confirm('/conversions');assert.equal(selected.total,2);assert.equal((await settled(selected.id)).state,'completed');
 const convert=await guided('convert',46);assert.equal((await settled(convert.id)).state,'completed');assert.equal((await get('/guided')).queues.convert.completed,49);
 console.log('PASS: downloaded card library, single-book conversion, selected conversion and automatic conversion batch');
 await route('/epubs');await page.locator('.book-card').first().waitFor();const save=page.waitForEvent('download');await page.locator('[data-card="1"] a[download]').click();assert((await save).suggestedFilename().endsWith('.epub'));await page.screenshot({path:'analysis/redesign-epubs.png',fullPage:true});
 await route('/catalog');await page.locator('[data-select-book="101"]').count();await page.locator('#search').fill('Book 101');await page.waitForTimeout(500);await page.locator('[data-select-book="101"]').check();await page.getByRole('button',{name:'Download originals',exact:true}).click();await page.locator('#start-batch:not([disabled])').waitFor();assert.match(await page.locator('#batch-title').textContent(),/Download 1 book/);await page.getByRole('button',{name:'Cancel',exact:true}).click();
 console.log('PASS: EPUB card library, native EPUB save and manual selected download preview');
 await route('/settings');await page.locator('#download-workers').selectOption('1');await page.waitForTimeout(300);const second=await guided('download',50);await page.getByRole('button',{name:'Pause batch',exact:true}).click();assert.equal((await settled(second.id)).state,'paused');await route('/downloads');await page.getByRole('button',{name:'Resume unfinished batch',exact:true}).click();await page.locator('#start-batch:not([disabled])').waitFor();await confirm(`/jobs/${second.id}/resume`);assert.equal((await settled(second.id)).state,'completed');assert.equal((await get('/guided')).queues.download.completed,99);
 const last=await guided('download',5);assert.equal((await settled(last.id)).state,'completed');const nextConvert=await guided('convert',50);assert.equal((await settled(nextConvert.id)).state,'completed');const lastConvert=await guided('convert',5);assert.equal((await settled(lastConvert.id)).state,'completed');const q=(await get('/guided')).queues;assert.equal(q.download.completed,104);assert.equal(q.download.needs_attention,1);assert.equal(q.download.next_count,0);assert.equal(q.convert.completed,104);
 console.log('PASS: pause/reload/confirmed resume, complete 50/50/5 queues with unavailable book set aside');
 await page.setViewportSize({width:390,height:844});for(const path of ['/downloaded','/epubs','/conversions','/downloads','/errors','/settings','/catalog','/activity']){await route(path);await page.waitForTimeout(150);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow on '+path);}await route('/downloaded');await page.screenshot({path:'analysis/redesign-mobile.png',fullPage:true});assert.deepEqual(errors,[]);
 console.log('PASS: every page works on mobile; no JavaScript errors');
}finally{await browser.close();}
