const vscode = require('vscode');
const http = require('http');

const DEFAULT_API_BASE = 'http://127.0.0.1:8383';
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'halted']);

function localApiUrl(path) {
    const base = vscode.workspace.getConfiguration('gh0st').get('apiBase', DEFAULT_API_BASE);
    const url = new URL(path, base.endsWith('/') ? base : `${base}/`);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
        throw new Error('gh0st API URL must be a loopback HTTP address');
    }
    return url;
}

async function makeApiRequest(context, method, path, body = null) {
    const url = localApiUrl(path);
    const settings = vscode.workspace.getConfiguration('gh0st');
    const token = await context.secrets.get('gh0st.apiToken') || settings.get('apiToken', '');
    return new Promise((resolve, reject) => {
        const headers = { 'Accept': 'application/json' };
        if (body !== null) headers['Content-Type'] = 'application/json';
        if (token) headers.Authorization = `Bearer ${token}`;
        const req = http.request(url, { method, headers, timeout: 10000 }, (res) => {
            let data = '';
            res.setEncoding('utf8');
            res.on('data', chunk => {
                data += chunk;
                if (data.length > 2_000_000) req.destroy(new Error('gh0st API response is too large'));
            });
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (res.statusCode >= 200 && res.statusCode < 300) resolve(parsed);
                    else reject(new Error(String(parsed.detail || parsed.error || `HTTP ${res.statusCode}`)));
                } catch (error) {
                    reject(error instanceof SyntaxError ? new Error(`Invalid API response (${res.statusCode})`) : error);
                }
            });
        });
        req.on('timeout', () => req.destroy(new Error('gh0st API timed out')));
        req.on('error', reject);
        if (body !== null) req.write(JSON.stringify(body));
        req.end();
    });
}

class TaskTreeProvider {
    constructor(context) {
        this.context = context;
        this.changed = new vscode.EventEmitter();
        this.onDidChangeTreeData = this.changed.event;
    }

    refresh() {
        this.changed.fire();
    }

    async getChildren(element) {
        if (!element) {
            const result = await makeApiRequest(this.context, 'GET', '/v1/tasks?limit=50');
            return result.tasks.map(task => ({ kind: 'task', ...task }));
        }
        if (element.kind !== 'task') return [];
        const status = await makeApiRequest(this.context, 'GET',
            `/v1/status?task_id=${encodeURIComponent(element.task_id)}`);
        const grants = status.grant_requests
            .filter(grant => grant.status === 'pending')
            .map(grant => ({ kind: 'grant', ...grant }));
        const tickets = status.tickets.map(ticket => ({
            kind: 'ticket', task_id: element.task_id, ...ticket,
        }));
        return [...grants, ...tickets];
    }

    getTreeItem(element) {
        if (element.kind === 'task') {
            const label = element.description.length > 75
                ? `${element.description.slice(0, 72)}...` : element.description;
            const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Collapsed);
            item.description = `${element.status} · ${element.task_id}`;
            item.tooltip = `${element.task_id}\n${element.description}\n${element.status}`;
            item.contextValue = 'gh0stTask';
            item.iconPath = new vscode.ThemeIcon(TERMINAL_STATUSES.has(element.status) ? 'check' : 'sync');
            item.command = { command: 'gh0st.status', title: 'Show Task Status', arguments: [element] };
            return item;
        }
        if (element.kind === 'grant') {
            const item = new vscode.TreeItem(`Grant: ${element.action_type}`);
            item.description = element.grant_id;
            item.tooltip = JSON.stringify(element.details, null, 2);
            item.contextValue = 'gh0stGrant';
            item.iconPath = new vscode.ThemeIcon('shield');
            item.command = { command: 'gh0st.inspectGrant', title: 'Inspect Grant', arguments: [element] };
            return item;
        }
        const item = new vscode.TreeItem(element.capability || element.ticket_id);
        item.description = element.status;
        item.tooltip = `${element.ticket_id}\n${element.status}`;
        item.iconPath = new vscode.ThemeIcon('terminal');
        return item;
    }
}

function activeWorkspaceFolder() {
    const editor = vscode.window.activeTextEditor;
    if (editor) {
        const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
        if (folder) return folder;
    }
    const folders = vscode.workspace.workspaceFolders || [];
    return folders.length === 1 ? folders[0] : null;
}

function activate(context) {
    const output = vscode.window.createOutputChannel('gh0st');
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.text = '$(terminal) gh0st';
    statusBar.tooltip = 'Show gh0st tasks';
    statusBar.command = 'gh0st.refresh';
    statusBar.show();
    const provider = new TaskTreeProvider(context);
    const view = vscode.window.createTreeView('gh0st.tasks', { treeDataProvider: provider });
    const pollers = new Map();
    context.subscriptions.push(output, statusBar, view,
        { dispose: () => { for (const timer of pollers.values()) clearInterval(timer); } });

    async function refreshTask(taskId) {
        const data = await makeApiRequest(context, 'GET',
            `/v1/status?task_id=${encodeURIComponent(taskId)}`);
        provider.refresh();
        statusBar.text = `$(terminal) gh0st: ${data.status}`;
        if (TERMINAL_STATUSES.has(data.status)) {
            clearInterval(pollers.get(taskId));
            pollers.delete(taskId);
        }
        return data;
    }

    context.subscriptions.push(vscode.commands.registerCommand('gh0st.refresh', () => provider.refresh()));
    context.subscriptions.push(vscode.commands.registerCommand('gh0st.setApiToken', async () => {
        const token = await vscode.window.showInputBox({ prompt: 'gh0st local API token', password: true,
            ignoreFocusOut: true });
        if (token === undefined) return;
        if (token) await context.secrets.store('gh0st.apiToken', token);
        else await context.secrets.delete('gh0st.apiToken');
        provider.refresh();
    }));

    context.subscriptions.push(vscode.commands.registerCommand('gh0st.run', async () => {
        const folder = activeWorkspaceFolder();
        if (!folder || folder.uri.scheme !== 'file') {
            vscode.window.showErrorMessage('Open one local repository workspace before running gh0st.');
            return;
        }
        const editor = vscode.window.activeTextEditor;
        const selection = editor ? editor.document.getText(editor.selection).trim() : '';
        const prompt = selection || await vscode.window.showInputBox({
            prompt: 'Task for gh0st in the active repository', ignoreFocusOut: true,
        });
        if (!prompt || !prompt.trim()) return;
        const settings = vscode.workspace.getConfiguration('gh0st');
        const budgetUsd = settings.get('maxBudgetUsd', 5);
        const budgetTokens = settings.get('maxBudgetTokens', 100000);
        if (!(budgetUsd > 0) || !(budgetTokens > 0)) {
            vscode.window.showErrorMessage('Set positive gh0st task budgets in VS Code settings.');
            return;
        }
        try {
            const response = await makeApiRequest(context, 'POST', '/v1/run', {
                prompt: prompt.trim(), source_repo: folder.uri.fsPath,
                max_budget_usd: budgetUsd, max_budget_tokens: budgetTokens,
            });
            provider.refresh();
            statusBar.text = `$(sync~spin) gh0st: ${response.task_id}`;
            vscode.window.showInformationMessage(`gh0st task started: ${response.task_id}`);
            const timer = setInterval(() => {
                void refreshTask(response.task_id).catch(error => {
                    clearInterval(timer);
                    pollers.delete(response.task_id);
                    statusBar.text = '$(warning) gh0st: disconnected';
                    output.appendLine(`Status check failed: ${error.message}`);
                });
            }, 2000);
            pollers.set(response.task_id, timer);
        } catch (error) {
            vscode.window.showErrorMessage(`gh0st: ${error.message}`);
            statusBar.text = '$(warning) gh0st: disconnected';
        }
    }));

    context.subscriptions.push(vscode.commands.registerCommand('gh0st.status', async (task) => {
        let taskId = task && task.task_id;
        try {
            if (!taskId) {
                const result = await makeApiRequest(context, 'GET', '/v1/tasks?limit=50');
                const chosen = await vscode.window.showQuickPick(result.tasks.map(item => ({
                    label: item.description.slice(0, 80), description: `${item.status} · ${item.task_id}`,
                    taskId: item.task_id,
                })), { placeHolder: 'Choose a gh0st task' });
                if (!chosen) return;
                taskId = chosen.taskId;
            }
            const data = await refreshTask(taskId);
            output.appendLine(JSON.stringify(data, null, 2));
            output.show(true);
        } catch (error) {
            vscode.window.showErrorMessage(`gh0st: ${error.message}`);
        }
    }));

    context.subscriptions.push(vscode.commands.registerCommand('gh0st.inspectGrant', async (grant) => {
        if (!grant || grant.kind !== 'grant') return;
        output.appendLine(JSON.stringify(grant, null, 2));
        output.show(true);
    }));

    async function decideGrant(grant, approved) {
        if (!grant || grant.kind !== 'grant' || grant.status !== 'pending') return;
        const decision = approved ? 'Approve' : 'Reject';
        const confirmed = await vscode.window.showWarningMessage(
            `${decision} ${grant.action_type} grant ${grant.grant_id}? This records a ledger decision.`,
            { modal: true }, decision);
        if (confirmed !== decision) return;
        const operator = vscode.workspace.getConfiguration('gh0st').get('operatorName', '') ||
            await vscode.window.showInputBox({ prompt: 'Your name for the grant ledger', ignoreFocusOut: true });
        if (!operator || !operator.trim()) return;
        try {
            await makeApiRequest(context, 'POST',
                `/v1/grants/${encodeURIComponent(grant.grant_id)}/decision`,
                { approved, operator_id: operator.trim() });
            provider.refresh();
            vscode.window.showInformationMessage(`Grant ${grant.grant_id} ${approved ? 'approved' : 'rejected'}.`);
        } catch (error) {
            vscode.window.showErrorMessage(`gh0st: ${error.message}`);
        }
    }

    context.subscriptions.push(vscode.commands.registerCommand('gh0st.approveGrant', grant => decideGrant(grant, true)));
    context.subscriptions.push(vscode.commands.registerCommand('gh0st.rejectGrant', grant => decideGrant(grant, false)));
}

function deactivate() {}

module.exports = { activate, deactivate };
