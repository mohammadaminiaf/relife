// Read-only browser smoke checks against the full local catalog.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require('../.tools/node_modules/playwright-core');
const base=process.argv[2]||'http://127.0.0.1:8765';
const chrome=process.env.REVIVE_CHROME||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'/usr/bin/google-chrome');
const get=async p=>(await fetch(base+p)).json();
const health=await get('/health');assert(health.catalog_books>20000);
const before=(await get('/jobs')).jobs.length;
const browser=await chromium.launch({headless:true,executablePath:chrome});
const page=await browser.newPage({viewport:{width:1512,height:1100},acceptDownloads:true});
const errors=[];page.on('pageerror',e=>errors.push(e.message));fs.mkdirSync('analysis',{recursive:true});
try{
 await page.goto(base+'/catalog');await page.locator('[data-select-book]').first().waitFor();assert.equal(await page.locator('[data-select-book]').count(),24);
 await page.locator('#search').fill('Quran');await page.waitForTimeout(600);await page.locator('#book-results[aria-busy="false"]').waitFor();assert((await page.locator('#result-summary').textContent()).includes('matching'));
 const summary=await get('/library/summary');
 for(const [path,key] of [['downloaded','downloaded'],['epubs','converted']]){await page.goto(base+'/'+path);if(summary[key])await page.locator('.book-card').first().waitFor();else await page.locator('.empty-state').waitFor();await page.screenshot({path:`analysis/library-${path}-v3.png`,fullPage:false});}
 await page.goto(base+'/downloads');await page.locator('[data-guided="download"]:not([data-retry])').waitFor();await page.screenshot({path:'analysis/library-downloads-v3.png',fullPage:false});
 await page.locator('[data-guided="download"]').first().click();await page.locator('#start-batch:not([disabled])').waitFor();await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal((await get('/jobs')).jobs.length,before);
 for(const path of ['conversions','activity','errors','settings']){await page.goto(base+'/'+path);await page.locator('h1').waitFor();if(path==='errors'){await page.locator('#issues-list .issue-card,#issues-list .empty-state').first().waitFor();await page.screenshot({path:'analysis/library-issues-v3.png',fullPage:false});}}
 await page.setViewportSize({width:390,height:844});await page.goto(base+'/downloaded');await page.locator('.book-card,.empty-state').first().waitFor();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:'analysis/library-mobile-v3.png',fullPage:false});assert.deepEqual(errors,[]);
 console.log('PASS: full catalog, search, saved and EPUB cards, all routes, confirmation without downloads, mobile and no JavaScript errors');
}finally{await browser.close();}
