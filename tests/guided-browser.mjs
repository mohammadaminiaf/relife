// Run against `python tests/serve_guided_fixture.py`, never a live catalog.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require('../.tools/node_modules/playwright-core');
const base=process.argv[2]||'http://127.0.0.1:8768';
const chrome=process.env.REVIVE_CHROME||(process.platform==='win32'
  ? ['C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p))
  : process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'/usr/bin/google-chrome');
const get=async endpoint=>(await fetch(base+endpoint)).json();
assert.equal((await get('/health')).catalog_books,105,'Use the isolated 105-book fixture.');
assert.equal((await get('/jobs')).jobs.length,0,'Start a fresh test fixture.');
const browser=await chromium.launch({headless:true,executablePath:chrome});
const page=await browser.newPage({viewport:{width:1512,height:1200},acceptDownloads:true});
const errors=[];
page.on('pageerror',error=>errors.push(error.message));
const card=kind=>page.locator(`[data-guided-card="${kind}"]`);
async function settled(id) {
  const end=Date.now()+60000;
  while(Date.now()<end) {
    const job=await get('/jobs/'+id);
    if(['completed','completed_with_errors','failed','paused'].includes(job.state))return job;
    await new Promise(resolve=>setTimeout(resolve,150));
  }
  throw new Error('Job did not settle: '+id);
}
async function review(kind,count) {
  const button=card(kind).locator('[data-guided]');
  await button.filter({hasText:`Review next ${count} `}).waitFor();
  await button.click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  assert.match(await page.locator('#batch-title').textContent(),new RegExp(`next ${count} `));
}
async function confirm() {
  const response=page.waitForResponse(r=>r.url().endsWith('/guided/start')&&r.request().method()==='POST');
  await page.locator('#start-batch').click();
  const job=await (await response).json();
  assert(job.id,JSON.stringify(job));
  return job;
}
async function batch(kind,count) {
  await review(kind,count);
  const job=await confirm();
  assert.equal(job.total,count);
  assert.equal((await settled(job.id)).state,'completed');
  return job;
}
try {
  await page.goto(base);
  await card('download').locator('[data-guided]').waitFor();
  await page.locator('#search').fill('Book 105');
  await page.waitForTimeout(500);
  await page.locator('#guided-start').click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  assert.match(await page.locator('#batch-title').textContent(),/next 50 downloads/);
  assert.match(await page.locator('#batch-plan').textContent(),/Book 1/);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal((await get('/jobs')).jobs.length,0);
  console.log('PASS: whole-library next 50, independent of search; cancel starts no work');

  await batch('download',50);
  await page.waitForTimeout(4000);
  assert.equal((await get('/jobs')).jobs.length,1);
  assert.equal((await get('/guided')).queues.download.completed,50);
  await page.reload();
  await card('download').locator('[data-guided]').filter({hasText:'Review next 50 downloads'}).waitFor();
  fs.mkdirSync('analysis',{recursive:true});
  await page.screenshot({path:path.resolve('analysis/guided-desktop.png')});
  console.log('PASS: 50 saved originals, progress survives reload, next batch waits for confirmation');

  await batch('convert',50);
  assert.equal((await get('/guided')).queues.convert.completed,50);
  console.log('PASS: conversion finds the next 50 saved originals and saves real EPUBs');

  await review('download',50);
  const interrupted=await confirm();
  const pause=card('download').getByRole('button',{name:'Pause batch',exact:true});
  await pause.click();
  assert.equal((await settled(interrupted.id)).state,'paused');
  await page.reload();
  await card('download').getByRole('button',{name:'Review unfinished batch'}).click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  const resumeResponse=page.waitForResponse(r=>r.url().endsWith(`/jobs/${interrupted.id}/resume`)&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Confirm resume',exact:true}).click();
  assert((await resumeResponse).ok());
  assert.equal((await settled(interrupted.id)).state,'completed');
  assert.equal((await get('/guided')).queues.download.completed,100);
  console.log('PASS: pause, reload, confirmation and resume preserve the original 50-book batch');

  await batch('download',5);
  await batch('convert',50);
  await batch('convert',5);
  const queues=(await get('/guided')).queues;
  assert.equal(queues.download.completed,105);
  assert.equal(queues.convert.completed,105);
  assert.equal(queues.download.next_count,0);
  assert.equal(queues.convert.next_count,0);
  console.log('PASS: download and conversion queues finish in 50/50/5 batches');

  await page.locator('[data-state="all"]').click();
  await page.locator('#search').fill('Book 1');
  await page.waitForTimeout(500);
  await page.locator('[data-select-book="1"]').check();
  await page.locator('#batch-mode').selectOption('check');
  await page.locator('#skip-completed').uncheck();
  await page.locator('#review-batch').click();
  await page.locator('#start-batch:not([disabled])').waitFor();
  const manualResponse=page.waitForResponse(r=>r.url().endsWith('/checks')&&r.request().method()==='POST');
  await page.locator('#start-batch').click();
  const manual=await (await manualResponse).json();
  assert.equal(manual.total,1);
  assert.equal((await settled(manual.id)).state,'completed');
  await page.locator('[data-state="all"]').click();
  await page.locator('#book-rows button[data-detail="1"]').first().click();
  const saved=page.waitForEvent('download');
  await page.getByRole('link',{name:'Save EPUB',exact:true}).click();
  assert((await saved).suggestedFilename().endsWith('.epub'));
  await page.getByRole('button',{name:'Close book details'}).click();
  console.log('PASS: manual selection, compatibility checks and individual EPUB saves remain available');

  await page.setViewportSize({width:390,height:844});
  await card('download').scrollIntoViewIfNeeded();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile page overflows');
  await page.screenshot({path:path.resolve('analysis/guided-mobile.png')});
  assert.deepEqual(errors,[]);
  console.log('PASS: responsive queue cards, no browser JavaScript errors');
} finally {await browser.close();}
