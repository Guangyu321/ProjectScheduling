// Uses isolated data and child services; never operates the user's active batch.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const python = process.env.PYTHON || 'python';
const port = () => new Promise(resolve => {const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p));});});
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
async function ready(url) {for(let i=0;i<100;i++){try{if((await fetch(url)).ok)return;}catch{}await delay(100);}throw new Error('Service not ready: '+url);}
(async()=>{
 const out=path.join(root,'.runtime','flexible-verification',Date.now().toString());fs.mkdirSync(out,{recursive:true});
 const apiPort=await port(), frontPort=await port();
 const apiRoot=`http://127.0.0.1:${apiPort}`, frontRoot=`http://127.0.0.1:${frontPort}`;
 const children=[];let browser;
 function start(args){const p=spawn(python,args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});p.stderr.on('data',s=>process.stderr.write(s));children.push(p);return p;}
 try{
  start(['backend/run.py','--port',String(apiPort),'--data',path.join(out,'data'),'--frontend-origin',frontRoot]);
  start(['frontend/serve.py','--port',String(frontPort),'--api-base',apiRoot]);
  await Promise.all([ready(apiRoot+'/api/v1/state'),ready(frontRoot)]);
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(frontRoot);
  await page.waitForFunction(()=>document.getElementById('connection').textContent==='已连接');
  await page.locator('[data-view=composer]').click({timeout:4000});
  await page.locator('#recipe-name').fill('自由组合验收');
  await page.locator('#execution-mode').selectOption('serial');
  // Remove the fixed pipeline and build detection → shaking → detection.
  while(await page.locator('[data-op-action=remove]').count())await page.locator('[data-op-action=remove]').first().click();
  await page.locator('[data-add-capability="fluorescence.measure"]').click();
  await page.locator('[data-add-capability="plate.shake"]').click();
  await page.locator('[data-op-action=duplicate]').first().click();
  await page.locator('[data-op-action=down]').nth(1).click();
  const labels=await page.locator('.operation-card h3').allTextContents();
  assert.match(labels[0],/荧光检测/);assert.match(labels[1],/振荡/);assert.match(labels[2],/荧光检测/);
  await page.locator('[data-op-field=method_id]').first().fill('CUSTOM_READ');
  await page.locator('[data-op-field=device_id]').first().selectOption('reader_1');
  await page.locator('[data-op-field=location]').first().selectOption('reader_1.slot1');
  await page.locator('#preview-recipe').click();
  await page.waitForFunction(()=>document.getElementById('preview-status').textContent.includes('预演通过'));
  assert.match(await page.locator('#preview-status').innerText(),/14/);
  await page.screenshot({path:path.join(out,'composer-desktop.png'),fullPage:true});
  await page.locator('#save-recipe').click();
  await page.waitForFunction(()=>document.getElementById('recipe-status').textContent.includes('已保存'));
  await page.reload();
  await page.waitForFunction(()=>document.getElementById('connection').textContent==='已连接');
  await page.locator('[data-view=composer]').click();
  assert.equal(await page.locator('#recipe-name').inputValue(),'自由组合验收');
  assert.equal(await page.locator('.operation-card').count(),3);
  // Invalid parameter must invalidate the old preview and prevent saving a revision.
  await page.locator('[data-op-field=estimated_duration_s]').first().fill('0');
  await page.locator('#preview-recipe').click();
  await page.waitForFunction(()=>document.getElementById('preview-status').textContent.includes('时长'));
  await page.locator('[data-op-field=estimated_duration_s]').first().fill('5');
  await page.locator('#save-recipe').click();
  await page.waitForFunction(()=>document.getElementById('recipe-status').textContent.includes('v2'));
  await page.locator('#run-recipe').click();
  assert.match(await page.locator('#new-summary').innerText(),/自由组合验收/);
  await page.locator('#new-form button[type=submit]').click();
  await page.waitForFunction(()=>document.getElementById('stat-state').textContent==='运行中');
  await page.locator('[data-view=overview]').click();
  await page.locator('#pause').click();
  await page.waitForFunction(()=>document.getElementById('stat-state').textContent==='已暂停');
  await page.locator('#resume').click();
  await page.waitForFunction(()=>document.getElementById('stat-state').textContent==='已完成',null,{timeout:60000});
  assert.match(await page.locator('#stat-progress').innerText(),/14\s*\/\s*14/);
  const state=await (await fetch(apiRoot+'/api/v1/state')).json();
  assert.equal(state.workflow.recipe.name,'自由组合验收');
  assert.equal(state.workflow.recipe.version,2);
  assert.equal(state.workflow.steps.filter(s=>s.kind==='process')[0].parameters.method_id,'CUSTOM_READ');
  assert.equal(state.workflow.recipe.operations[0].device_id,'reader_1');
  assert.equal(state.positions.plate_02,'fridge_1.slot2');
  await page.locator('[data-view=history]').click();
  await page.waitForFunction(()=>document.getElementById('history-rows').textContent.includes('自由组合验收'));
  await page.locator('[data-view=composer]').click();
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(out,'composer-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const downloadReady=page.waitForEvent('download');
  await page.locator('#export-recipe').click();
  const download=await downloadReady, exportedPath=path.join(out,'exported-recipe.json');await download.saveAs(exportedPath);
  const exported=JSON.parse(fs.readFileSync(exportedPath,'utf8'));
  assert.equal(exported.operations.length,3);assert.equal(exported.operations[0].method_id,'CUSTOM_READ');
  await page.locator('#blank-recipe').click();
  assert.equal(await page.locator('.operation-card').count(),0);
  await page.locator('#recipe-file').setInputFiles(exportedPath);
  await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('流程已导入'));
  assert.equal(await page.locator('.operation-card').count(),3);
  assert.equal(await page.locator('[data-op-field=method_id]').first().inputValue(),'CUSTOM_READ');
  assert.deepEqual(errors,[]);
  if(process.env.RUN_LEGACY_SMOKE==='1')await new Promise((resolve,reject)=>{
   const child=spawn(process.execPath,[path.join(__dirname,'browser-smoke.cjs')],{cwd:root,windowsHide:true,stdio:'inherit',env:{...process.env,CONSOLE_URL:frontRoot}});
   child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(new Error('Legacy browser checks failed: '+code)));
  });
  console.log(JSON.stringify({status:'passed',screenshots:out,steps:state.steps.length,checks:['compose','reorder','duplicate','device','preview','validation','save','reload','version','run','pause','resume','complete','history','mobile','export','import']}));
 }finally{
  if(browser)await browser.close();
  await Promise.all(children.map(p=>new Promise(resolve=>{if(p.exitCode!==null)return resolve();p.once('exit',resolve);p.kill();})));
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
