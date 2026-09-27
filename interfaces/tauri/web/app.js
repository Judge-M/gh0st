const invoke = window.__TAURI__?.core?.invoke;
const byId = id => document.getElementById(id);
let selectedTask = null;
let poller = null;

function stopPolling() {
  if (poller !== null) clearInterval(poller);
  poller = null;
}

function localSettings() {
  const port = Number(byId('port').value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Choose a valid API port.');
  return { port, token: byId('token').value };
}

async function call(command, parameters = {}) {
  if (!invoke) throw new Error('Open this interface in the gh0st desktop app.');
  return invoke(command, { ...localSettings(), ...parameters });
}

function setMessage(message) {
  byId('message').textContent = String(message);
}

function makeElement(tag, content, className = '') {
  const element = document.createElement(tag);
  element.textContent = String(content);
  if (className) element.className = className;
  return element;
}

async function refreshTasks() {
  const response = await call('tasks');
  const container = byId('tasks');
  container.replaceChildren();
  if (!response.tasks.length) container.append(makeElement('p', 'No tasks yet.'));
  for (const task of response.tasks) {
    const button = makeElement('button', task.description, 'task');
    button.append(makeElement('span', `${task.status} · ${task.task_id}`));
    button.addEventListener('click', () => selectTask(task.task_id));
    container.append(button);
  }
}

function renderQuota(quota) {
  const container = byId('quota');
  container.replaceChildren();
  for (const [label, value] of [
    ['Tokens remaining', quota.remaining_tokens],
    ['USD remaining', `$${Number(quota.remaining_usd).toFixed(4)}`],
    ['Tokens consumed', quota.tokens_consumed],
    ['USD consumed', `$${Number(quota.cost_usd_consumed).toFixed(4)}`],
    ['Tokens uncertain', quota.tokens_uncertain],
    ['USD uncertain', `$${Number(quota.cost_usd_uncertain).toFixed(4)}`],
  ]) {
    container.append(makeElement('dt', label), makeElement('dd', value));
  }
}

async function decideGrant(grant, approved) {
  const operatorId = byId('operator').value.trim();
  if (!operatorId) { setMessage('Enter an operator name before deciding a grant.'); return; }
  const verb = approved ? 'approve' : 'reject';
  if (!window.confirm(`Record a decision to ${verb} ${grant.action_type} (${grant.grant_id})?`)) return;
  try {
    await call('decide_grant', { grantId: grant.grant_id, approved, operatorId });
    setMessage(`Grant ${grant.grant_id} ${approved ? 'approved' : 'rejected'}.`);
    await refreshSelectedTask();
  } catch (error) {
    setMessage(error);
  }
}

function renderGrants(grants) {
  const container = byId('grants');
  container.replaceChildren(makeElement('h3', 'External action grants'));
  if (!grants.length) container.append(makeElement('p', 'No grant requests.'));
  for (const grant of grants) {
    const block = makeElement('div', '', 'grant');
    block.append(makeElement('strong', `${grant.action_type} · ${grant.status}`));
    block.append(makeElement('pre', JSON.stringify(grant.details, null, 2)));
    if (grant.status === 'pending') {
      const approve = makeElement('button', 'Approve');
      const reject = makeElement('button', 'Reject', 'reject');
      approve.addEventListener('click', () => decideGrant(grant, true));
      reject.addEventListener('click', () => decideGrant(grant, false));
      block.append(approve, reject);
    }
    container.append(block);
  }
}

function renderTickets(tickets) {
  const container = byId('tickets');
  container.replaceChildren(makeElement('h3', 'Tickets and reports'));
  if (!tickets.length) container.append(makeElement('p', 'No tickets.'));
  for (const ticket of tickets) {
    const block = makeElement('div', '', 'ticket');
    block.append(makeElement('strong', `${ticket.capability} · ${ticket.status}`));
    block.append(makeElement('p', ticket.ticket_id));
    if (ticket.report) {
      block.append(makeElement('pre', JSON.stringify({
        diff: ticket.report.diff,
        commands_executed: ticket.report.commands_executed,
        test_results: ticket.report.test_results,
        unresolved_failures: ticket.report.unresolved_failures,
      }, null, 2)));
    }
    container.append(block);
  }
}

async function refreshSelectedTask() {
  if (!selectedTask) return;
  const data = await call('task_status', { taskId: selectedTask });
  byId('task-heading').textContent = `${data.task_id} · ${data.status}`;
  renderQuota(data.quota);
  renderGrants(data.grant_requests);
  renderTickets(data.tickets);
  if (['completed', 'failed', 'halted'].includes(data.status)) stopPolling();
}

async function selectTask(taskId) {
  selectedTask = taskId;
  stopPolling();
  poller = setInterval(() => {
    refreshSelectedTask().catch(error => setMessage(error));
  }, 3000);
  try { await refreshSelectedTask(); } catch (error) { setMessage(error); }
}

window.addEventListener('pagehide', stopPolling);

async function connect() {
  try {
    await call('health');
    byId('connection').textContent = 'Local daemon connected';
    byId('connection').classList.add('connected');
    await refreshTasks();
    await refreshSelectedTask();
    setMessage('');
  } catch (error) {
    byId('connection').textContent = 'Local daemon unavailable';
    byId('connection').classList.remove('connected');
    setMessage(error);
  }
}

byId('refresh').addEventListener('click', connect);
byId('refresh-tasks').addEventListener('click', () => refreshTasks().catch(setMessage));
byId('dispatch').addEventListener('click', async () => {
  try {
    const result = await call('dispatch_task', {
      prompt: byId('prompt').value.trim(),
      sourceRepo: byId('source-repo').value.trim(),
      maxBudgetUsd: Number(byId('budget-usd').value),
      maxBudgetTokens: Number(byId('budget-tokens').value),
    });
    setMessage(`Task ${result.task_id} dispatched.`);
    byId('prompt').value = '';
    await refreshTasks();
    await selectTask(result.task_id);
  } catch (error) {
    setMessage(error);
  }
});

void connect();
