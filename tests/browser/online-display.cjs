const fs=require('fs'),assert=require('assert/strict');
const {chromium}=require('playwright');
const root=process.cwd(),staticDir=root+'/apps/api/agenthub_rt/static';
fs.mkdirSync(root+'/var/timeout-check',{recursive:true});
const errors=[],requests=[],mutations=[];
const profile={agents:{},workspace:'E:\\example',workspace_access:'workspace-write',execution_timeout_seconds:0};
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const ctx=await browser.newContext({viewport:{width:1550,height:1050},permissions:['clipboard-read','clipboard-write']});
  const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());requests.push(u.href);let response={};
   if(['/chat','/settings','/ui'].includes(u.pathname))return route.fulfill({contentType:'text/html',body:fs.readFileSync(staticDir+'/'+({'/ui':'index'}[u.pathname]||u.pathname.slice(1))+'.html','utf8')});
   if(u.pathname.startsWith('/assets/display.'))return route.fulfill({contentType:u.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(staticDir+'/'+u.pathname.slice(8),'utf8')});
   if(u.pathname==='/assets/app-icon.png')return route.fulfill({contentType:'image/png',body:fs.readFileSync(staticDir+'/brand/app-icon-256.png')});
   if(u.pathname==='/api/client-config')response={desktop:true,development_token:''};
   else if(u.pathname==='/api/desktop/settings') {if(req.method()==='POST'){Object.assign(profile,req.postDataJSON());mutations.push(req.postDataJSON());response={enabled_agents:[]};}else response=profile;}
   else if(u.pathname==='/api/adapter')response={agent_ids:['codex'],name:'test',execution_admission:{allowed:false},supervised_admission:{allowed:true}};
   else if(u.pathname==='/api/conversations')response={conversations:[]};
   else if(req.method()==='DELETE'){mutations.push({delete:u.pathname});response={deleted:true};}
   else if(u.pathname.includes('models'))response={models:[]};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(response)});
  });
  const base='http://127.0.0.1:18219';
  await page.goto(base+'/settings');await page.waitForFunction(()=>document.querySelector('#workspace').value.length>0);
  assert.equal(await page.locator('#codex_timeout_minutes').inputValue(),'0');
  await page.locator('[data-theme-picker]').selectOption('light');
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'light');
  assert.match(await page.locator('.permission-guide').textContent(),/命令前缀留空/);
  await page.locator('#confirm').check();await page.locator('#save').click();
  await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('已保存'));
  assert.equal(profile.execution_timeout_seconds,0);
  await page.reload();await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('[data-theme-picker]').inputValue(),'light');
  await page.goto(base+'/chat');await page.waitForLoadState('networkidle');
  const md='# 验收结果\n\n**完成** 项目检查\n\n- 读取材料\n- 核验表格\n\n| 项目 | 状态 |\n| --- | --- |\n| 目录 | 已读取 |\n\n```python\nprint("OK")\n```\n\n```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 120"><rect x="4" y="4" width="390" height="110" rx="12" fill="#e7efff" stroke="#275dce"/><text x="30" y="65" font-size="22">读取 → 核验 → 输出</text></svg>\n```\n\n<script>window.pwned=1</script>\n[bad](javascript:alert(1))\n![外部图片](https://invalid.example/private.png)';
  await page.evaluate(text=>{S.cid='fixture';S.messages=[{message_id:100,role:'assistant',text,result:{},created_at:new Date().toISOString()}];S.timelineSignature=null;render();},md);
  assert.equal(await page.locator('.markdown h1').textContent(),'验收结果');
  assert.equal(await page.locator('.markdown table tbody tr').count(),1);
  assert.equal(await page.locator('.markdown pre code').first().textContent(),'print("OK")');
  assert.equal(await page.locator('.svg-preview svg text').textContent(),'读取 → 核验 → 输出');
  assert.equal(await page.evaluate(()=>window.pwned),undefined);
  assert.equal(await page.locator('.markdown img,.markdown script').count(),0);
  await page.getByRole('button',{name:'复制回复',exact:true}).click();
  assert.equal((await page.evaluate(()=>navigator.clipboard.readText())).replace(/\r\n/g,'\n'),md);
  await page.getByRole('button',{name:'查看原文',exact:true}).click();
  await page.waitForFunction(text=>document.querySelector('.msg.assistant .bubble').textContent===text,md);
  assert.equal(await page.locator('.msg.assistant .bubble').textContent(),md);
  await page.getByRole('button',{name:'格式化显示',exact:true}).click();
  await page.locator('.markdown h1').waitFor();
  await page.screenshot({path:root+'/var/timeout-check/online-light.png',fullPage:true});
  await page.setViewportSize({width:760,height:900});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:root+'/var/timeout-check/online-mobile.png',fullPage:true});
  await page.setViewportSize({width:1550,height:1050});
  await page.evaluate(()=>{
    const malicious='<svg xmlns="http://www.w3.org/2000/svg" onload="window.pwned=1"><script>window.pwned=1</script><foreignObject><iframe src="https://invalid.example"/></foreignObject><image href="https://invalid.example/a"/><rect fill="url(https://invalid.example/b)" onclick="alert(1)"/><use href="javascript:alert(1)"/></svg>';
    const clean=AppDisplay.safeSVG(malicious);document.querySelector('#timeline').append(clean);
  });
  assert.equal(await page.locator('svg script,svg foreignObject,svg image,svg [onload],svg [onclick],svg [href^="javascript"],svg [fill^="url(http"]').count(),0);
  assert.equal(requests.filter(x=>!x.startsWith(base)).length,0);
  await page.locator('[data-theme-picker]').selectOption('dark');
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'dark');
  await page.evaluate(()=>{S.convs=[{conversation_id:'fixture',title:'测试对话',created_at:new Date().toISOString()}];renderConvs();});
  page.once('dialog',async d=>d.dismiss());await page.getByRole('button',{name:'删除',exact:true}).click();
  assert.equal(mutations.filter(x=>x.delete).length,0);
  page.once('dialog',async d=>{assert.match(d.message(),/项目文件不删除/);await d.accept();});
  await page.getByRole('button',{name:'删除',exact:true}).click();
  await page.waitForFunction(()=>S.cid===null);
  assert.deepEqual(mutations.find(x=>x.delete),{delete:'/api/conversations/fixture'});
  assert.deepEqual(errors,[]);
  const report={themes:['system','light','dark'],theme_persisted:true,default_unlimited:true,permission_guide:true,markdown_table_code:true,svg_preview:true,svg_script_and_external_resources_removed:true,original_copy_unchanged:true,delete_requires_confirmation:true,browser_errors:errors,real_model_calls:0};
  fs.writeFileSync(root+'/var/timeout-check/online-ui-check.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
