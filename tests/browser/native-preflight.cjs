// Execute the actual composer availability/render/send functions, offline.
// No third-party browser runtime or running user Hub is used.
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('apps/api/agenthub_rt/static/chat.html','utf8');
function extract(name) {
  const match = html.match(new RegExp('(?:async )?function '+name+'\\([^]*?\\n}'));
  assert.ok(match, name);
  return match[0];
}
const option = {disabled:false};
const fields = {
  agent:{value:'claude-code'}, mode:{value:'strict'}, text:{value:'synthetic task'},
  nativeAgents:{value:'disabled',disabled:false,querySelector:()=>option},
  nativeHint:{hidden:true,textContent:''},
};
const state = {usageConv:null};
let dispatches=0, renders=0, notifications=[];
const context = vm.createContext({S:state,$:id=>fields[id],
  toast:text=>notifications.push(text),renderComposer:()=>renders++,
  guarded:async()=>{dispatches++;},console});
vm.runInContext(['nativeChoiceProblem','nativeCallProblem','renderNativeChoice','send','runApproved','approveAndRun'].map(extract).join('\n'),context);
function render(){vm.runInContext('renderNativeChoice()',context);}
function saved(changes={}) {
  state.usageConv={root_task:{root_task_id:'synthetic-root'},budget:{policy:{
    collaboration_enabled:true,max_subagents_concurrent:1,max_depth:1,
    token_budget_total:null,model_budgets:{},...changes}}};
}
(async()=>{
  render(); assert.equal(option.disabled,true); assert.equal(fields.nativeHint.hidden,false);
  saved({collaboration_enabled:false}); fields.nativeAgents.value='approved'; render();
  assert.equal(option.disabled,true); assert.equal(fields.nativeAgents.value,'disabled');
  assert.equal(fields.mode.value,'strict'); assert.match(fields.nativeHint.textContent,/协作未开启/);
  saved(); render();
  assert.equal(option.disabled,false); assert.equal(fields.nativeAgents.value,'disabled');
  assert.equal(fields.nativeHint.hidden,true); // No automatic opt-in when enabled.
  for(const changes of [{max_depth:0},{max_subagents_concurrent:0},
      {token_budget_total:1000},{model_budgets:{synthetic:{total:1000}}}]) {
    saved(changes); fields.nativeAgents.value='approved'; render();
    assert.equal(option.disabled,true); assert.equal(fields.nativeAgents.value,'disabled');
  }
  saved({collaboration_enabled:false}); fields.nativeAgents.value='approved';
  await context.send({preventDefault(){}});
  assert.equal(dispatches,0); assert.equal(renders,1); assert.equal(notifications.length,1);
  fields.mode.value=''; // Legacy card uses its own mode, not the composer mode.
  await context.runApproved({id:'legacy',native_subagents:'approved',agent_id:'claude-code',execution_mode:'strict'});
  await context.approveAndRun({id:'legacy',native_subagents:'approved',agent_id:'claude-code',execution_mode:'strict'});
  assert.equal(dispatches,0);
  fields.mode.value='strict';
  fields.nativeAgents.value='disabled';
  await context.send({preventDefault(){}});
  assert.equal(dispatches,1); // Ordinary strict message needs no collaboration.
  saved(); fields.nativeAgents.value='approved';
  await context.send({preventDefault(){}});
  assert.equal(dispatches,2);
  fields.agent.value='codex'; render();
  assert.equal(fields.nativeAgents.disabled,true); assert.equal(fields.nativeAgents.value,'disabled');
  console.log(JSON.stringify({passed:true,policy_cases:9,invalid_requests_dispatched:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
