// Run against: python tests/serve_guided_fixture.py --count 5205
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require('../.tools/node_modules/playwright-core');
const base=process.argv[2]||'http://127.0.0.1:8768';
const get=async p=>(await fetch(base+p)).json();
assert.equal((await get('/health')).catalog_books,5205,'Use a fresh --count 5205 fixture.');
assert.equal((await get('/jobs')).jobs.length,0);
const chrome=process.env.REVIVE_CHROME||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'/usr/bin/google-chrome');
const browser=await chromium.launch({headless:true,executablePath:chrome});
const page=await browser.newPage({viewport:{width:1400,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
async function choose(size){const response=page.waitForResponse(r=>r.url().endsWith('/api/settings')&&r.request().method()==='POST');await page.locator('#download-batch-size').selectOption(String(size));assert((await response).ok());await page.waitForFunction(n=>document.querySelector('[data-guided="download"]:not([data-retry])')?.textContent.includes(new Intl.NumberFormat().format(n)),size);}
try{
 await page.goto(base+'/downloads');await page.locator('#download-batch-size').waitFor();assert.deepEqual(await page.locator('#download-batch-size option').evaluateAll(es=>es.map(e=>e.value)),['50','100','200','1000','5000']);
 for(const size of [100,200,1000,5000]){await choose(size);await page.locator('[data-guided="download"]:not([data-retry])').click();await page.locator('#start-batch:not([disabled])').waitFor();assert((await page.locator('#batch-title').textContent()).includes(new Intl.NumberFormat().format(size)+' books'));await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal((await get('/jobs')).jobs.length,0);}
 console.log('PASS: 100/200/1,000/5,000 choices create exact previews, and cancel starts no downloads');
 await page.reload();await page.locator('#download-batch-size').waitFor();assert.equal(await page.locator('#download-batch-size').inputValue(),'5000');assert.equal((await get('/api/settings')).download_workers,8);
 await page.goto(base+'/settings');await page.locator('#download-batch-size').waitFor();assert.equal(await page.locator('#download-batch-size').inputValue(),'5000');await page.locator('#download-batch-size').selectOption('200');await page.waitForTimeout(300);assert.equal((await get('/api/settings')).download_batch_size,200);
 console.log('PASS: preference survives reload, Settings shares it, and concurrency remains independent');
 await page.goto(base+'/downloads');await choose(100);await page.locator('[data-guided="download"]:not([data-retry])').click();await page.locator('#start-batch:not([disabled])').waitFor();const started=page.waitForResponse(r=>r.url().endsWith('/guided/start')&&r.request().method()==='POST');await page.locator('#start-batch').click();const job=await (await started).json();assert.equal(job.total,100);assert.equal(job.options.batch_size,100);assert.equal(job.options.download_workers,8);
 const deadline=Date.now()+30000;let current;while(Date.now()<deadline){current=await get('/jobs/'+job.id);if(current.state==='completed')break;await new Promise(r=>setTimeout(r,150));}assert.equal(current.state,'completed');assert.equal(current.counts.done,100);assert.equal((await get('/jobs')).jobs.length,1);assert.equal((await get('/guided')).queues.download.next_count,100);assert.equal((await get('/guided')).queues.convert.batch_size,50);
 await page.reload();await page.locator('#download-batch-size').waitFor();await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);
 console.log('PASS: confirmed 100-book batch, next group waiting, conversions stay at 50, mobile layout and no JS errors');
}finally{await browser.close();}
