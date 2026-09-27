const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) {
    this.tag = tag;
    this.value = '';
    this.children = [];
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text || ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
}

async function run() {
  const elements = new Map();
  const byId = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  byId('port').value = '8383';
  byId('token').value = 'local-secret';
  byId('operator').value = 'Operator';
  byId('source-repo').value = '/source/repository';
  byId('prompt').value = 'Fix parser';
  byId('budget-usd').value = '3';
  byId('budget-tokens').value = '4000';
  const requests = [];
  const clearedIntervals = [];
  const windowListeners = {};
  let nextInterval = 1;
  const invoke = async (command, args) => {
    requests.push({ command, args });
    if (command === 'health') return { status: 'ok' };
    if (command === 'tasks') return { tasks: [{
      task_id: 'task_1', description: 'Fix parser', status: 'active',
    }] };
    if (command === 'dispatch_task') return { task_id: 'task_1' };
    if (command === 'task_status') return {
      task_id: 'task_1', status: 'active',
      quota: { remaining_tokens: 3000, remaining_usd: 2.5,
        tokens_consumed: 1000, cost_usd_consumed: 0.5,
        tokens_uncertain: 0, cost_usd_uncertain: 0 },
      grant_requests: [{ grant_id: 'grant_1', task_id: 'task_1',
        action_type: 'git_push', status: 'pending', details: { ref: 'main' } }],
      tickets: [{ ticket_id: 'ticket_1', capability: 'code_refactor', status: 'running' }],
    };
    if (command === 'decide_grant') return { status: 'approved' };
    throw new Error(`Unexpected command: ${command}`);
  };
  const context = {
    window: {
      __TAURI__: { core: { invoke } }, confirm: () => true,
      addEventListener: (name, listener) => { windowListeners[name] = listener; },
    },
    document: { getElementById: byId, createElement: tag => new Element(tag) },
    setInterval: () => nextInterval++, clearInterval: id => clearedIntervals.push(id),
    console,
  };
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  vm.runInNewContext(source, context, { filename: 'app.js' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(byId('connection').textContent, 'Local daemon connected');
  assert.equal(byId('tasks').children.length, 1);
  await byId('dispatch').listeners.click();
  const dispatch = requests.find(request => request.command === 'dispatch_task');
  assert.equal(dispatch.args.sourceRepo, '/source/repository');
  assert.equal(dispatch.args.maxBudgetUsd, 3);
  assert.equal(dispatch.args.maxBudgetTokens, 4000);
  assert.equal(dispatch.args.token, 'local-secret');
  const taskButton = byId('tasks').children[0];
  await taskButton.listeners.click();
  assert.deepEqual(clearedIntervals, [1]);
  windowListeners.pagehide();
  assert.deepEqual(clearedIntervals, [1, 2]);
  const grant = byId('grants').children[1];
  const approve = grant.children.find(child => child.tag === 'button');
  await approve.listeners.click();
  const decision = requests.find(request => request.command === 'decide_grant');
  assert.equal(decision.args.grantId, 'grant_1');
  assert.equal(decision.args.operatorId, 'Operator');
  assert.equal(decision.args.approved, true);
  process.stdout.write('Desktop control flow passed\n');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
