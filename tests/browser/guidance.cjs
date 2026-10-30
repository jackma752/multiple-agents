// Offline browser contract check against an authored/simulated pipeline fixture.
const fs=require('fs'),assert=require('assert/strict');
const {chromium}=require('playwright');
const root=process.cwd(),dir=root+'/apps/api/agenthub_rt/static';
const fixture=JSON.parse(fs.readFileSync(root+'/var/guidance-check/browser-fixture.json','utf8'));
const mutations=[],errors=[],requests=[];
let project={version:0,selected_candidate:'',notes:{}};
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());requests.push(u.href);
   if(u.pathname==='/papers')return route.fulfill({contentType:'text/html',body:fs.readFileSync(dir+'/papers.html')});
   if(u.pathname.startsWith('/assets/katex/'))return route.fulfill({contentType:u.pathname.endsWith('.js')?'text/javascript':u.pathname.endsWith('.css')?'text/css':'font/woff2',body:fs.readFileSync(dir+'/vendor/katex/'+u.pathname.slice('/assets/katex/'.length))});
   if(u.pathname.startsWith('/assets/display.'))return route.fulfill({contentType:u.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(dir+'/'+u.pathname.slice(8))});
   if(u.pathname==='/assets/app-icon.png')return route.fulfill({contentType:'image/png',body:fs.readFileSync(dir+'/brand/app-icon-256.png')});
   let response={};
   if(u.pathname==='/api/client-config')response={desktop:false,development_token:'offline-fixture'};
   else if(u.pathname==='/api/adapter')response={agent_ids:['claude-code','deepseek']};
   else if(u.pathname==='/api/papers')response={tasks:[]};
   else if(u.pathname.endsWith('/project')){
     if(req.method()==='PUT'){const data=req.postDataJSON();mutations.push(data);project={version:project.version+1,selected_candidate:data.selected_candidate,notes:data.notes};}
     response=project;
   }else if(u.pathname.startsWith('/api/papers/'))response=fixture;
   return route.fulfill({contentType:'application/json',body:JSON.stringify(response)});
  });
  const base='http://127.0.0.1:18571';await page.goto(base+'/papers');
  await page.waitForFunction(()=>document.querySelector('#f_main')?.options.length>0);
  assert.equal(await page.locator('#f_goal').inputValue(),'project');
  const request=await page.evaluate(()=>formBody().request);
  assert.equal(request.core_papers,1);assert.equal(request.repair_rounds,1);
  await page.evaluate(v=>{S.current=v.task_id;S.view=v;render(true);},fixture);
  await page.getByText('先读懂，再看数学推导',{exact:true}).waitFor();
  await page.getByText('相邻步距离 · step1',{exact:true}).click();
  assert.equal(await page.locator('#guidanceBox .katex').count(),2);
  assert(await page.getByText('推导经模型审阅（非形式化证明）',{exact:true}).count());
  assert(await page.getByText('第一周任务',{exact:true}).count());
  await page.getByText('选择方案并记录论文进度',{exact:true}).click();
  const editor=page.locator('[data-project-editor]');await editor.waitFor();
  await editor.locator('textarea').nth(1).fill('检查几何级数尾项并尝试反例。');
  await page.waitForTimeout(2200);assert.equal(await editor.locator('textarea').nth(1).inputValue(),'检查几何级数尾项并尝试反例。');
  await page.getByText('保存项目新版本',{exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-project-editor]'));
  assert.equal(mutations[0].notes.proof_notes,'检查几何级数尾项并尝试反例。');
  await page.screenshot({path:root+'/var/guidance-check/guidance-light.png',fullPage:true});
  await page.locator('[data-theme-picker]').selectOption('dark');
  await page.setViewportSize({width:760,height:950});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:root+'/var/guidance-check/guidance-mobile.png',fullPage:true});
  assert.equal(requests.filter(u=>!u.startsWith(base)).length,0);assert.deepEqual(errors,[]);
  const report={default_project_mode:true,formula_rendered:true,local_assets_only:true,versioned_project_form:true,polling_preserves_edit:true,mobile_no_horizontal_overflow:true,browser_errors:errors,real_model_calls:0};
  fs.writeFileSync(root+'/var/guidance-check/browser-report.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
