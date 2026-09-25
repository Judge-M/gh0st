const assert = require('node:assert/strict');
const http = require('node:http');
const Module = require('node:module');

async function run() {
    const requests = [];
    const server = http.createServer(async (req, res) => {
        let body = '';
        for await (const chunk of req) body += chunk;
        requests.push({ method: req.method, path: req.url, authorization: req.headers.authorization,
            body: body ? JSON.parse(body) : null });
        res.setHeader('Content-Type', 'application/json');
        if (req.url === '/v1/tasks?limit=50') {
            res.end(JSON.stringify({ tasks: [{ task_id: 'task_1', description: 'Fix parser', status: 'active' }] }));
        } else if (req.url === '/v1/status?task_id=task_1') {
            res.end(JSON.stringify({ task_id: 'task_1', status: 'active', grant_requests: [{
                task_id: 'task_1', grant_id: 'grant_1', action_type: 'git_push',
                status: 'pending', details: { ref: 'main' },
            }], tickets: [] }));
        } else if (req.url === '/v1/run') {
            res.statusCode = 202;
            res.end(JSON.stringify({ task_id: 'task_1' }));
        } else if (req.url === '/v1/grants/grant_1/decision') {
            res.end(JSON.stringify({ grant_id: 'grant_1', status: 'approved' }));
        } else {
            res.statusCode = 404;
            res.end(JSON.stringify({ detail: 'Unknown path' }));
        }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const commands = new Map();
    let treeProvider;
    const folder = { uri: { scheme: 'file', fsPath: '/source/repository' } };
    const settings = {
        apiBase: `http://127.0.0.1:${server.address().port}`,
        maxBudgetUsd: 3, maxBudgetTokens: 4000, operatorName: 'Operator',
    };
    class EventEmitter {
        constructor() { this.event = () => {}; }
        fire() {}
    }
    class TreeItem {
        constructor(label, collapsibleState) { this.label = label; this.collapsibleState = collapsibleState; }
    }
    const vscode = {
        EventEmitter, TreeItem, ThemeIcon: class { constructor(id) { this.id = id; } },
        TreeItemCollapsibleState: { Collapsed: 1 }, StatusBarAlignment: { Right: 1 },
        workspace: {
            getConfiguration: () => ({ get: (key, fallback) => settings[key] ?? fallback }),
            getWorkspaceFolder: () => folder, workspaceFolders: [folder],
        },
        window: {
            activeTextEditor: { document: { uri: folder.uri, getText: () => '' }, selection: {} },
            createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
            createStatusBarItem: () => ({ show() {}, dispose() {} }),
            createTreeView: (_id, options) => { treeProvider = options.treeDataProvider; return { dispose() {} }; },
            showInputBox: async () => 'Fix parser',
            showWarningMessage: async (_message, _options, choice) => choice,
            showInformationMessage: () => {}, showErrorMessage: message => { throw new Error(message); },
        },
        commands: { registerCommand: (name, handler) => {
            commands.set(name, handler);
            return { dispose() {} };
        } },
    };
    const originalLoad = Module._load;
    Module._load = function(name, parent, isMain) {
        if (name === 'vscode') return vscode;
        return originalLoad.call(this, name, parent, isMain);
    };
    let extension;
    try {
        extension = require('../extension');
    } finally {
        Module._load = originalLoad;
    }
    const context = {
        subscriptions: [],
        secrets: { get: async () => 'local-secret', store: async () => {}, delete: async () => {} },
    };
    try {
        extension.activate(context);
        const tasks = await treeProvider.getChildren();
        assert.equal(tasks[0].task_id, 'task_1');
        const children = await treeProvider.getChildren(tasks[0]);
        assert.equal(children[0].grant_id, 'grant_1');
        await commands.get('gh0st.run')();
        const run = requests.find(request => request.path === '/v1/run');
        assert.equal(run.body.source_repo, '/source/repository');
        assert.equal(run.body.max_budget_usd, 3);
        assert.equal(run.body.max_budget_tokens, 4000);
        await commands.get('gh0st.approveGrant')(children[0]);
        const decision = requests.find(request => request.path === '/v1/grants/grant_1/decision');
        assert.deepEqual(decision.body, { approved: true, operator_id: 'Operator' });
        assert.ok(requests.every(request => request.authorization === 'Bearer local-secret'));
        process.stdout.write('VS Code API flow passed\n');
    } finally {
        for (const subscription of context.subscriptions) subscription.dispose();
        await new Promise(resolve => server.close(resolve));
    }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
