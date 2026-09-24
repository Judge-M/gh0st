/**
 * gh0st VS Code Extension (extension.js)
 * Connects VS Code to the local gh0st REST API daemon (http://localhost:8383)
 *
 * Features:
 * - gh0st.run: Execute prompts directly from editor command palette or selection
 * - gh0st.status: Display task progress, token/cost spend, and trusted claims
 * - Sidebar Webview Provider for interactive task queue & budget monitoring
 */

const vscode = require('vscode');
const http = require('http');

const DEFAULT_API_BASE = 'http://127.0.0.1:8383';

/**
 * Helper function to send HTTP requests to gh0st local daemon
 */
function makeApiRequest(method, path, body = null) {
    return new Promise((resolve, reject) => {
        const settings = vscode.workspace.getConfiguration('gh0st');
        const url = new URL(path, settings.get('apiBase', DEFAULT_API_BASE));
        const options = {
            hostname: url.hostname,
            port: url.port,
            path: url.pathname + url.search,
            method: method,
            headers: {
                'Content-Type': 'application/json',
            }
        };
        const apiToken = settings.get('apiToken', '');
        if (apiToken) options.headers.Authorization = `Bearer ${apiToken}`;

        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (res.statusCode >= 200 && res.statusCode < 300) {
                        resolve(parsed);
                    } else {
                reject(new Error(parsed.detail || parsed.error || `HTTP ${res.statusCode}`));
                    }
                } catch (e) {
                    reject(new Error(`Invalid API response (${res.statusCode})`));
                }
            });
        });

        req.on('error', (err) => reject(err));
        if (body) {
            req.write(JSON.stringify(body));
        }
        req.end();
    });
}

/**
 * Activates the gh0st extension
 */
function activate(context) {
    console.log('👻 gh0st extension active!');

    // Status Bar Item
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.text = "$(ghost) gh0st: Ready";
    statusBar.tooltip = "Click to inspect gh0st daemon status";
    statusBar.command = 'gh0st.status';
    statusBar.show();
    context.subscriptions.push(statusBar);

    // Command 1: gh0st.run - Run prompt or active editor selection
    let runDisposable = vscode.commands.registerCommand('gh0st.run', async () => {
        const editor = vscode.window.activeTextEditor;
        let selectedText = editor ? editor.document.getText(editor.selection) : '';

        let prompt = selectedText.trim();
        if (!prompt) {
            prompt = await vscode.window.showInputBox({
                prompt: "Enter task prompt for gh0st System-1 Router",
                placeHolder: "e.g., Refactor authentication middleware to use JWT tokens"
            });
        }

        if (!prompt) return;

        statusBar.text = "$(sync~spin) gh0st: Routing...";

        try {
            const response = await makeApiRequest('POST', '/v1/run', {
                prompt: prompt,
                    max_budget_tokens: 50000,
                    max_budget_usd: 3.50
            });

            vscode.window.showInformationMessage(
                `gh0st Task Started: ${response.task_id} (${response.decision.selected_capability}, Conf: ${response.decision.confidence_score})`
            );

            statusBar.text = `$(gear~spin) gh0st: Running (${response.task_id})`;

            // Poll status
            pollTaskStatus(response.task_id, statusBar);
        } catch (err) {
            vscode.window.showErrorMessage(`gh0st Error: ${err.message}. Is the daemon running on localhost:8383?`);
            statusBar.text = "$(error) gh0st: Offline";
        }
    });

    // Command 2: gh0st.status - View status
    let statusDisposable = vscode.commands.registerCommand('gh0st.status', async () => {
        const taskId = await vscode.window.showInputBox({
            prompt: "Enter Task ID to query",
            placeHolder: "e.g., task_12345"
        });

        if (!taskId) return;

        try {
            const data = await makeApiRequest('GET', `/v1/status?task_id=${encodeURIComponent(taskId)}`);
            const msg = `Task ${data.task_id} [${data.status}]: Used ${data.quota.tokens_consumed} tokens ($${data.quota.cost_usd_consumed.toFixed(4)} USD). Pending Tickets: ${data.pending_tickets_count}`;
            vscode.window.showInformationMessage(msg);
        } catch (err) {
            vscode.window.showErrorMessage(`gh0st Query Error: ${err.message}`);
        }
    });

    context.subscriptions.push(runDisposable);
    context.subscriptions.push(statusDisposable);
}

function pollTaskStatus(taskId, statusBar) {
    const interval = setInterval(async () => {
        try {
            const data = await makeApiRequest('GET', `/v1/status?task_id=${encodeURIComponent(taskId)}`);
            if (['completed', 'failed', 'halted'].includes(data.status)) {
                clearInterval(interval);
                statusBar.text = `$(check) gh0st: ${data.status} (${data.quota.cost_usd_consumed.toFixed(4)} USD)`;
                vscode.window.showInformationMessage(`gh0st Task ${taskId}: ${data.status}. Cost: $${data.quota.cost_usd_consumed.toFixed(4)} USD.`);
            } else {
                statusBar.text = `$(sync~spin) gh0st: ${data.pending_tickets_count} pending ($${data.quota.cost_usd_consumed.toFixed(3)})`;
            }
        } catch (e) {
            clearInterval(interval);
            statusBar.text = "$(warning) gh0st: Disconnected";
        }
    }, 2000);
}

function deactivate() {}

module.exports = { activate, deactivate };
