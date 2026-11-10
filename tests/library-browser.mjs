// End-to-end QA against an isolated sample library. Requires the project's Playwright/Chrome.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = require('../.tools/node_modules/playwright-core');
const base = process.argv[2] || 'http://127.0.0.1:8766';
const chrome = process.env.REVIVE_CHROME || (process.platform === 'win32'
  ? ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(file=>fs.existsSync(file))
  : process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/google-chrome');
assert(chrome, 'Install Chrome/Edge or set REVIVE_CHROME to your browser executable.');
const output = path.resolve('analysis');
fs.mkdirSync(output, {recursive:true});
const browser = await chromium.launch({headless:true, executablePath:chrome});
const page = await browser.newPage({viewport:{width:1512,height:1100}, acceptDownloads:true});
const errors = [];
page.on('pageerror', error=>errors.push(error.message));
async function waitForJob(kind) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const {jobs} = await (await fetch(base+'/jobs')).json();
    const job = jobs.find(j=>j.kind === kind);
    if (job && ['completed','completed_with_errors','failed'].includes(job.state)) {
      assert.equal(job.state,'completed',JSON.stringify(job.failures));
      return job;
    }
    await new Promise(resolve=>setTimeout(resolve,400));
  }
  throw new Error('Job did not settle: '+kind);
}
async function search(text) {
  await page.locator('#search').fill(text);
  await page.waitForTimeout(450);
  await page.waitForFunction(()=>document.querySelector('#table-card').getAttribute('aria-busy')==='false');
}
try {
  await page.goto(base);
  await page.locator('[data-select-book]').first().waitFor();
  assert.equal(await page.locator('[data-select-book]').count(),50);
  const health=await (await fetch(base+'/health')).json();
  const fullCount=health.catalog_books;
  assert(fullCount>20000);
  await page.screenshot({path:path.join(output,'library-desktop.png')});
  console.log('PASS: desktop app, full catalog, pagination and local status');

  await Promise.all([
    page.waitForResponse(r=>r.url().includes('/books?')&&r.url().includes('category=1956')),
    page.locator('#category').selectOption('1956'),
  ]);
  const expected=await (await fetch(base+'/books?category=1956&page_size=1')).json();
  await page.waitForFunction(total=>document.querySelector('#result-summary').textContent.startsWith(new Intl.NumberFormat().format(total)),expected.total);
  assert(expected.total>0 && expected.total<fullCount);
  await page.locator('#category').selectOption('');
  await search('منهاج الامام الحسين');
  await page.locator('[data-select-book="5992"]').waitFor();
  await page.locator('#book-rows button[data-detail="5992"]').first().click();
  await page.locator('#detail-dialog[open]').waitFor();
  const downloadEvent=page.waitForEvent('download');
  await page.getByRole('link',{name:'Save EPUB',exact:true}).click();
  const download=await downloadEvent;
  assert(download.suggestedFilename().endsWith('.epub'));
  await download.saveAs(path.join(output,'browser-saved-5992.epub'));
  await page.getByRole('button',{name:'Close book details'}).click();
  console.log('PASS: category descendants, normalized Arabic/Persian search, native EPUB save');

  await search('4696');
  await page.locator('#book-rows button[data-detail="4696"]').first().click();
  await page.getByRole('button',{name:'Check format',exact:true}).click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  await page.locator('#start-batch').click();
  await waitForJob('check');
  await page.locator('[data-state="convertible"]').click();
  await search('4696');
  await page.locator('[data-select-book="4696"]').waitFor({timeout:15000});
  await page.locator('[data-select-book="4696"]').check();
  await page.locator('#batch-mode').selectOption('convert');
  await page.locator('#review-batch').click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  await page.locator('#start-batch').click();
  await waitForJob('convert');
  const converted=await (await fetch(base+'/books/4696')).json();
  assert.equal(converted.local.status,'converted');
  console.log('PASS: compatibility check → ready to convert → successful EPUB job');

  await page.locator('[data-state="all"]').click();
  await search('');
  await page.locator('#select-page').check();
  assert.match(await page.locator('#selection-count').textContent(),/50 books selected/);
  await page.locator('#select-matching').click();
  assert.match(await page.locator('#selection-count').textContent(),new RegExp(new Intl.NumberFormat().format(fullCount)));
  const first=page.locator('[data-select-book]').first();
  const excludedId=Number(await first.getAttribute('data-select-book'));
  await first.uncheck();
  await page.locator('#batch-mode').selectOption('download');
  const planResponse=page.waitForResponse(r=>r.url().endsWith('/library/plan'));
  await page.locator('#review-batch').click();
  const plan=await (await planResponse).json();
  assert.equal(plan.books,50);
  assert(plan.selection.exclude_ids.includes(excludedId));
  await page.getByRole('button',{name:'Close batch preview'}).click();
  await page.locator('#restore-all').click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  assert.match(await page.locator('#batch-title').textContent(),/entire library/);
  const before=await (await fetch(base+'/jobs')).json();
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  const after=await (await fetch(base+'/jobs')).json();
  assert.equal(before.jobs.length,after.jobs.length);
  console.log('PASS: page/all selection, exclusions, bounded batch preview; full backup preview causes no download');

  if(process.argv.includes('--live')) {
    await page.locator('#clear-selection').click();
    await search('4977');
    await page.locator('#book-rows button[data-detail="4977"]').first().click();
    await page.getByRole('button',{name:'Download + EPUB',exact:true}).click();
    await page.locator('#start-batch:not([disabled])').waitFor();
    await page.locator('#start-batch').click();
    await waitForJob('download');
    const book=await (await fetch(base+'/books/4977')).json();
    assert.equal(book.local.status,'converted');
    console.log('PASS: live GUI download + EPUB for a new small book (4977)');
  }
  await page.setViewportSize({width:390,height:844});
  await page.locator('#jobs-shortcut').click();
  assert(await page.locator('#jobs-view').isVisible());
  await page.locator('[data-state="all"]').click();
  await page.waitForTimeout(600);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Mobile viewport overflow');
  await page.screenshot({path:path.join(output,'library-mobile.png')});
  assert.deepEqual(errors,[],'Browser JavaScript errors');
  console.log('PASS: mobile library and jobs, no page overflow or browser errors');
} finally {await browser.close();}
