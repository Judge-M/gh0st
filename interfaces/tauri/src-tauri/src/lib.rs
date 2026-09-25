use reqwest::{Client, Method, Url};
use serde_json::{json, Value};
use std::time::Duration;

async fn local_request(
    port: u16,
    token: &str,
    method: Method,
    path: &str,
    body: Option<Value>,
) -> Result<Value, String> {
    if port == 0 {
        return Err("Choose a valid local API port".into());
    }
    let client = Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|error| error.to_string())?;
    let url = format!("http://127.0.0.1:{port}{path}");
    let mut request = client.request(method, url);
    if !token.is_empty() {
        request = request.bearer_auth(token);
    }
    if let Some(payload) = body {
        request = request.json(&payload);
    }
    let response = request.send().await.map_err(|error| error.to_string())?;
    let status = response.status();
    let payload: Value = response.json().await.map_err(|error| error.to_string())?;
    if status.is_success() {
        Ok(payload)
    } else {
        Err(payload
            .get("detail")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| format!("Local API returned {status}: {payload}")))
    }
}

fn query_path(path: &str, key: &str, value: &str) -> Result<String, String> {
    let mut url = Url::parse(&format!("http://127.0.0.1{path}"))
        .map_err(|error| error.to_string())?;
    url.query_pairs_mut().append_pair(key, value);
    Ok(format!("{}?{}", url.path(), url.query().unwrap_or("")))
}

#[tauri::command]
async fn health(port: u16, token: String) -> Result<Value, String> {
    local_request(port, &token, Method::GET, "/v1/health", None).await
}

#[tauri::command]
async fn tasks(port: u16, token: String) -> Result<Value, String> {
    local_request(port, &token, Method::GET, "/v1/tasks?limit=50", None).await
}

#[tauri::command]
async fn task_status(port: u16, token: String, task_id: String) -> Result<Value, String> {
    let path = query_path("/v1/status", "task_id", &task_id)?;
    local_request(port, &token, Method::GET, &path, None).await
}

#[tauri::command]
async fn dispatch_task(
    port: u16,
    token: String,
    prompt: String,
    source_repo: String,
    max_budget_usd: f64,
    max_budget_tokens: u64,
) -> Result<Value, String> {
    if prompt.trim().is_empty() || prompt.len() > 8000 {
        return Err("Enter a prompt of 1 to 8000 characters".into());
    }
    if !max_budget_usd.is_finite() || max_budget_usd <= 0.0 || max_budget_tokens == 0 {
        return Err("Task budgets must be positive".into());
    }
    if source_repo.trim().is_empty() {
        return Err("Choose a local repository for this task".into());
    }
    local_request(
        port,
        &token,
        Method::POST,
        "/v1/run",
        Some(json!({
            "prompt": prompt.trim(),
            "source_repo": source_repo.trim(),
            "max_budget_usd": max_budget_usd,
            "max_budget_tokens": max_budget_tokens,
        })),
    )
    .await
}

#[tauri::command]
async fn decide_grant(
    port: u16,
    token: String,
    grant_id: String,
    approved: bool,
    operator_id: String,
) -> Result<Value, String> {
    if grant_id.is_empty()
        || !grant_id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '_' || character == '-')
    {
        return Err("Invalid grant ID".into());
    }
    if operator_id.trim().is_empty() {
        return Err("Enter an operator name".into());
    }
    local_request(
        port,
        &token,
        Method::POST,
        &format!("/v1/grants/{grant_id}/decision"),
        Some(json!({"approved": approved, "operator_id": operator_id.trim()})),
    )
    .await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            health,
            tasks,
            task_status,
            dispatch_task,
            decide_grant
        ])
        .run(tauri::generate_context!())
        .expect("Could not start gh0st desktop");
}
