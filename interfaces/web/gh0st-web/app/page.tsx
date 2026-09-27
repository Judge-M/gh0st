"use client"

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react"
import { Button } from "@/components/ui/button"

type Task = {
  task_id: string
  description: string
  status: string
  created_at: string
  updated_at: string
}

type Report = {
  status: string
  output: Record<string, unknown>
  commands_executed: Array<Record<string, unknown>>
  test_results: Array<Record<string, unknown>>
  unresolved_failures: string[]
  diff: string
  error?: string | null
}

type TaskStatus = {
  task_id: string
  status: string
  quota: { remaining_tokens: number; remaining_usd: number }
  grant_requests: Array<{
    grant_id: string
    action: string
    status: string
    details: Record<string, unknown>
  }>
  tickets: Array<{
    ticket_id: string
    status: string
    capability: string
    report: Report | null
  }>
}

type Health = {
  status: string
  default_classifier: string
  gateway_target: string
  models: Array<{
    name: string
    gateway_model: string
    quality: string
    placement: string
  }>
}

const strategyNames: Record<string, string> = {
  smart: "Smart routing",
  quality: "Quality first",
  speed: "Fastest response",
  cost: "Cost aware",
  local: "Local models",
}

function Icon({ name, className = "" }: { name: string; className?: string }) {
  const shared = {
    className,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true as const,
  }
  const paths: Record<string, ReactNode> = {
    ghost: (
      <>
        <path d="M9 10h.01M15 10h.01" />
        <path d="M5 20v-9a7 7 0 0 1 14 0v9l-3-2-4 2-4-2-3 2Z" />
        <path d="M9 15c1.5 1 4.5 1 6 0" />
      </>
    ),
    plus: (
      <>
        <path d="M12 5v14M5 12h14" />
      </>
    ),
    search: (
      <>
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-4-4" />
      </>
    ),
    settings: (
      <>
        <path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z" />
        <path d="m19.4 15 .1.1a1.7 1.7 0 0 1-2.4 2.4l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a1.7 1.7 0 0 1-3.4 0v-.2a1.7 1.7 0 0 0-2.9-1.2l-.1.1a1.7 1.7 0 0 1-2.4-2.4l.1-.1a1.7 1.7 0 0 0-1.2-2.9H4a1.7 1.7 0 0 1 0-3.4h.2a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a1.7 1.7 0 0 1 2.4-2.4l.1.1a1.7 1.7 0 0 0 2.9-1.2V2a1.7 1.7 0 0 1 3.4 0v.2a1.7 1.7 0 0 0 2.9 1.2l.1-.1a1.7 1.7 0 0 1 2.4 2.4l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.2a1.7 1.7 0 0 1 0 3.4h-.2a1.7 1.7 0 0 0-1.2 2.9Z" />
      </>
    ),
    chevron: <path d="m9 18 6-6-6-6" />,
    arrow: (
      <>
        <path d="M7 17 17 7M7 7h10v10" />
      </>
    ),
    terminal: (
      <>
        <path d="m4 17 6-5-6-5" />
        <path d="M12 19h8" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    lock: (
      <>
        <rect x="4" y="10" width="16" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 1 1 8 0v3" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5" />
        <path d="M4 17v-5h5" />
        <path d="M5.6 9a7 7 0 0 1 11.8-2L20 12M4 12l2.6 5a7 7 0 0 0 11.8-2" />
      </>
    ),
    spark: (
      <>
        <path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" />
        <path d="m19 14 .9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9L19 14Z" />
      </>
    ),
  }
  return <svg {...shared}>{paths[name] ?? paths.spark}</svg>
}

function shortId(value: string) {
  return value.replace(/^task_/, "").slice(0, 8)
}

function timeAgo(value: string) {
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - new Date(value).getTime()) / 1000)
  )
  if (seconds < 60) return "just now"
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return new Date(value).toLocaleDateString()
}

function statusStyle(status: string) {
  if (["completed", "success"].includes(status))
    return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
  if (["failed", "halted"].includes(status))
    return "bg-rose-500/10 text-rose-700 dark:text-rose-300"
  if (["active", "running", "dispatched"].includes(status))
    return "bg-primary/10 text-primary"
  return "bg-muted text-muted-foreground"
}

export default function Page() {
  const [apiToken, setApiToken] = useState("")
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedId, setSelectedId] = useState("")
  const [detail, setDetail] = useState<TaskStatus | null>(null)
  const [prompt, setPrompt] = useState("")
  const [strategy, setStrategy] = useState("smart")
  const [model, setModel] = useState("")
  const [sourceRepo, setSourceRepo] = useState("")
  const [budget, setBudget] = useState("5")
  const [tokens, setTokens] = useState("100000")
  const [operator, setOperator] = useState("local-operator")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [connectionError, setConnectionError] = useState("")
  const [notice, setNotice] = useState("")
  const [filter, setFilter] = useState("all")

  const request = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      const headers = new Headers(init.headers)
      if (apiToken) headers.set("Authorization", `Bearer ${apiToken}`)
      if (init.body && !headers.has("Content-Type"))
        headers.set("Content-Type", "application/json")
      const response = await fetch(path, {
        ...init,
        headers,
        cache: "no-store",
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok)
        throw new Error(body.detail || `Request failed (${response.status})`)
      return body as T
    },
    [apiToken]
  )

  const loadTasks = useCallback(async () => {
    const [healthData, taskData] = await Promise.all([
      request<Health>("/v1/health"),
      request<{ tasks: Task[] }>("/v1/tasks?limit=50"),
    ])
    setHealth(healthData)
    setTasks(taskData.tasks)
    setConnectionError("")
    return taskData.tasks
  }, [request])

  const loadDetail = useCallback(
    async (taskId: string) => {
      if (!taskId) {
        setDetail(null)
        return
      }
      setDetail(
        await request<TaskStatus>(
          `/v1/status?task_id=${encodeURIComponent(taskId)}`
        )
      )
    },
    [request]
  )

  useEffect(() => {
    let alive = true
    const initial = async () => {
      try {
        const [healthData, taskData] = await Promise.all([
          request<Health>("/v1/health"),
          request<{ tasks: Task[] }>("/v1/tasks?limit=50"),
        ])
        if (!alive) return
        setHealth(healthData)
        setTasks(taskData.tasks)
        setConnectionError("")
        if (taskData.tasks.length) setSelectedId(taskData.tasks[0].task_id)
      } catch (caught) {
        if (alive)
          setConnectionError(
            caught instanceof Error
              ? caught.message
              : "Could not connect to gh0st"
          )
      }
    }
    void initial()
    return () => {
      alive = false
    }
  }, [request])

  useEffect(() => {
    if (!selectedId) {
      return
    }
    let alive = true
    const update = async () => {
      try {
        const data = await request<TaskStatus>(
          `/v1/status?task_id=${encodeURIComponent(selectedId)}`
        )
        if (alive) {
          setDetail(data)
          setConnectionError("")
        }
      } catch (caught) {
        if (alive)
          setConnectionError(
            caught instanceof Error ? caught.message : "Could not load task"
          )
      }
    }
    void update()
    const interval = window.setInterval(update, 2000)
    return () => {
      alive = false
      window.clearInterval(interval)
    }
  }, [selectedId, request])

  const visibleTasks = useMemo(
    () => tasks.filter((task) => filter === "all" || task.status === filter),
    [tasks, filter]
  )
  const report =
    detail?.tickets
      .map((ticket) => ticket.report)
      .find((item) => item !== null) ?? null
  const completedCount = tasks.filter(
    (task) => task.status === "completed"
  ).length
  const activeCount = tasks.filter((task) => task.status === "active").length
  const pendingGrants =
    detail?.grant_requests.filter((grant) => grant.status === "pending") ?? []

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!prompt.trim()) return
    setBusy(true)
    setError("")
    setNotice("")
    try {
      const result = await request<{
        task_id: string
        decision: {
          selected_capability: string
          selected_model: string
          selected_routing_strategy: string
        }
      }>("/v1/run", {
        method: "POST",
        body: JSON.stringify({
          prompt: prompt.trim(),
          max_budget_usd: Number(budget),
          max_budget_tokens: Number(tokens),
          routing_strategy: strategy,
          model: model.trim() || null,
          source_repo: sourceRepo.trim() || null,
        }),
      })
      setPrompt("")
      setSelectedId(result.task_id)
      setNotice(
        `Task started · ${result.decision.selected_capability} · ${result.decision.selected_model}`
      )
      await loadTasks()
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not start task"
      )
    } finally {
      setBusy(false)
    }
  }

  async function decideGrant(grantId: string, approved: boolean) {
    try {
      await request(`/v1/grants/${encodeURIComponent(grantId)}/decision`, {
        method: "POST",
        body: JSON.stringify({
          approved,
          operator_id: operator.trim() || "local-operator",
        }),
      })
      setNotice(approved ? "Grant approved" : "Grant rejected")
      await loadDetail(selectedId)
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not record grant decision"
      )
    }
  }

  return (
    <div className="min-h-svh bg-background text-foreground">
      <div className="mx-auto flex min-h-svh max-w-[1600px]">
        <aside className="hidden w-[248px] shrink-0 border-r border-sidebar-border bg-sidebar px-4 py-5 text-sidebar-foreground md:flex md:flex-col">
          <div className="flex items-center gap-3 px-2">
            <div className="flex size-9 items-center justify-center rounded-xl bg-sidebar-primary text-sidebar-primary-foreground">
              <Icon name="ghost" className="size-5" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight">gh0st</div>
              <div className="text-xs text-muted-foreground">
                Local workspace
              </div>
            </div>
          </div>
          <Button
            className="mt-8 h-9 w-full justify-start gap-2 rounded-lg text-sm"
            onClick={() => document.getElementById("task-prompt")?.focus()}
          >
            <Icon name="plus" className="size-4" /> New task
          </Button>
          <div className="mt-8 flex items-center justify-between px-2 text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">
            <span>Workspace</span>
            <span>{tasks.length}</span>
          </div>
          <nav className="mt-3 space-y-1">
            {[
              { id: "all", label: "All tasks" },
              { id: "active", label: "In progress" },
              { id: "completed", label: "Completed" },
            ].map((item) => (
              <button
                key={item.id}
                onClick={() => setFilter(item.id)}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition-colors ${filter === item.id ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground" : "text-muted-foreground hover:bg-sidebar-accent/70 hover:text-sidebar-foreground"}`}
              >
                <span>{item.label}</span>
                <span className="text-xs">
                  {item.id === "all"
                    ? tasks.length
                    : tasks.filter((task) => task.status === item.id).length}
                </span>
              </button>
            ))}
          </nav>
          <div className="mt-8 px-2 text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">
            Recent
          </div>
          <div className="mt-3 flex-1 space-y-1 overflow-y-auto">
            {visibleTasks.slice(0, 20).map((task) => (
              <button
                key={task.task_id}
                onClick={() => setSelectedId(task.task_id)}
                className={`w-full rounded-lg px-3 py-2.5 text-left transition-colors ${selectedId === task.task_id ? "bg-sidebar-accent text-sidebar-accent-foreground" : "hover:bg-sidebar-accent/70"}`}
              >
                <div className="truncate text-xs font-medium">
                  {task.description || "Untitled task"}
                </div>
                <div className="mt-1.5 flex items-center justify-between text-[10px] text-muted-foreground">
                  <span>{shortId(task.task_id)}</span>
                  <span>{timeAgo(task.updated_at)}</span>
                </div>
              </button>
            ))}
            {!visibleTasks.length && (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                No tasks yet
              </p>
            )}
          </div>
          <div className="mt-4 rounded-xl border border-sidebar-border bg-background/50 p-3">
            <div className="flex items-center gap-2 text-xs font-medium">
              <span
                className={`size-2 rounded-full ${health?.status === "ok" ? "bg-emerald-500" : "bg-amber-500"}`}
              />
              {health?.status === "ok"
                ? "Daemon connected"
                : "Connect to daemon"}
            </div>
            <div className="mt-1 truncate text-[10px] text-muted-foreground">
              {health?.default_classifier
                ? `${health.default_classifier} router`
                : "Local API · 127.0.0.1:8383"}
            </div>
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-border/70 bg-background/90 px-4 backdrop-blur md:px-8">
            <div className="flex items-center gap-2 text-sm">
              <span className="font-semibold">Control room</span>
              <Icon name="chevron" className="size-3 text-muted-foreground" />
              <span className="text-muted-foreground">Overview</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="hidden items-center gap-2 rounded-full border border-border px-3 py-1.5 text-xs sm:flex">
                <span
                  className={`size-1.5 rounded-full ${health?.status === "ok" ? "bg-emerald-500" : "bg-amber-500"}`}
                />
                {health?.status === "ok"
                  ? "Local runtime online"
                  : "Runtime disconnected"}
              </div>
              <Button
                variant="outline"
                size="icon"
                aria-label="Refresh tasks"
                onClick={() =>
                  void loadTasks().catch((caught) =>
                    setConnectionError(caught instanceof Error ? caught.message : "Could not refresh gh0st")
                  )
                }
              >
                <Icon name="refresh" className="size-4" />
              </Button>
            </div>
          </header>

          <div className="mx-auto max-w-6xl px-4 py-7 md:px-8 md:py-10">
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div>
                <div className="text-xs font-medium tracking-[0.18em] text-primary uppercase">
                  Private by default · local-first
                </div>
                <h1 className="mt-2 text-3xl font-semibold tracking-tight md:text-4xl">
                  Good work starts here.
                </h1>
                <p className="mt-2 max-w-xl text-sm text-muted-foreground">
                  Give gh0st a task. It works inside an isolated workspace and
                  reports back with changes and test results.
                </p>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Icon name="lock" className="size-3.5" /> API token stays in
                this tab
              </div>
            </div>

            {error && (
              <div
                role="alert"
                className="mt-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive"
              >
                {error}
              </div>
            )}
            {connectionError && (
              <div
                role="status"
                className="mt-6 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-800 dark:text-amber-200"
              >
                Local connection issue: {connectionError}
              </div>
            )}
            {!error && notice && (
              <div role="status" className="mt-6 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300">
                {notice}
              </div>
            )}

            <div className="mt-7 grid gap-3 sm:grid-cols-3">
              <Metric
                label="Total tasks"
                value={String(tasks.length)}
                caption="Saved in the local ledger"
                icon="terminal"
              />
              <Metric
                label="In progress"
                value={String(activeCount)}
                caption="Running in task workspaces"
                icon="clock"
              />
              <Metric
                label="Completed"
                value={String(completedCount)}
                caption="Reports saved to the ledger"
                icon="check"
              />
            </div>

            <div className="mt-7 grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
              <section className="rounded-xl border border-border bg-card shadow-sm">
                <div className="border-b border-border px-5 py-4">
                  <div className="flex items-center gap-2">
                    <Icon name="spark" className="size-4 text-primary" />
                    <h2 className="text-sm font-semibold">Start a task</h2>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    One focused job at a time. Review the diff and report when
                    it finishes.
                  </p>
                </div>
                <form onSubmit={submitTask} className="p-5">
                  <label htmlFor="task-prompt" className="text-xs font-medium">
                    What should gh0st do?
                  </label>
                  <textarea
                    id="task-prompt"
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    placeholder="Describe a change, ask for an investigation, or request tests…"
                    className="mt-2 min-h-36 w-full resize-y rounded-lg border border-input bg-background px-3 py-3 text-sm leading-relaxed transition outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25"
                    required
                  />
                  <div className="mt-4 grid gap-3 sm:grid-cols-2">
                    <label className="text-xs font-medium">
                      Routing scheme
                      <select
                        value={strategy}
                        onChange={(event) => setStrategy(event.target.value)}
                        className="mt-1.5 block h-9 w-full rounded-md border border-input bg-background px-2.5 text-sm font-normal outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25"
                      >
                        {Object.entries(strategyNames).map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="text-xs font-medium">
                      Provider / model override{" "}
                      <input
                        list="configured-models"
                        value={model}
                        onChange={(event) => setModel(event.target.value)}
                        placeholder="Leave blank for dynamic routing"
                        className="mt-1.5 block h-9 w-full rounded-md border border-input bg-background px-2.5 text-sm font-normal outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25"
                      />
                      <datalist id="configured-models">
                        {health?.models?.map((target) => (
                          <option key={target.name} value={target.name}>
                            {target.gateway_model} · {target.quality} ·{" "}
                            {target.placement}
                          </option>
                        ))}
                      </datalist>
                      <span className="mt-1 block text-[10px] font-normal text-muted-foreground">
                        Choose a configured catalog model or leave automatic
                        routing on.
                      </span>
                    </label>
                  </div>
                  {health?.models?.length === 0 && (
                    <p className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                      Configure GH0ST_MODEL_CATALOG or GH0ST_DEFAULT_MODEL and a
                      provider key on this machine before dispatching tasks.
                    </p>
                  )}
                  <details className="group mt-4 rounded-lg border border-border/70 px-3 py-2.5">
                    <summary className="cursor-pointer list-none text-xs font-medium text-muted-foreground">
                      Budget and connection settings
                      <span className="float-right transition-transform group-open:rotate-90">
                        ›
                      </span>
                    </summary>
                    <div className="mt-3 grid gap-3 sm:grid-cols-2">
                      <label className="text-xs">
                        Max spend (USD)
                        <input
                          type="number"
                          min="0.01"
                          step="0.01"
                          value={budget}
                          onChange={(event) => setBudget(event.target.value)}
                          className="mt-1 block h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                        />
                      </label>
                      <label className="text-xs">
                        Token cap
                        <input
                          type="number"
                          min="1"
                          step="1"
                          value={tokens}
                          onChange={(event) => setTokens(event.target.value)}
                          className="mt-1 block h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                        />
                      </label>
                      <label className="text-xs sm:col-span-2">
                        Source repository
                        <input
                          value={sourceRepo}
                          onChange={(event) =>
                            setSourceRepo(event.target.value)
                          }
                          placeholder="Optional local repository path or clone URL"
                          className="mt-1 block h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                        />
                      </label>
                      <label className="text-xs sm:col-span-2">
                        Local API token{" "}
                        <input
                          type="password"
                          autoComplete="off"
                          value={apiToken}
                          onChange={(event) => setApiToken(event.target.value)}
                          placeholder="Optional; kept in memory for this tab"
                          className="mt-1 block h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                        />
                      </label>
                      <label className="text-xs sm:col-span-2">
                        Operator label for grant decisions
                        <input
                          value={operator}
                          onChange={(event) => setOperator(event.target.value)}
                          className="mt-1 block h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                        />
                      </label>
                    </div>
                  </details>
                  <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                    <span className="text-[11px] text-muted-foreground">
                      Workers can edit and test inside their isolated workspace.
                    </span>
                    <Button
                      type="submit"
                      disabled={busy || !prompt.trim()}
                      className="h-9 gap-2 rounded-lg px-4 text-sm"
                    >
                      <Icon name="spark" className="size-4" />
                      {busy ? "Starting…" : "Run task"}
                    </Button>
                  </div>
                </form>
              </section>

              <section className="rounded-xl border border-border bg-card shadow-sm">
                <div className="flex items-center justify-between border-b border-border px-5 py-4">
                  <div>
                    <h2 className="text-sm font-semibold">Task report</h2>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {detail
                        ? `Task ${shortId(detail.task_id)}`
                        : "Select a task to inspect progress"}
                    </p>
                  </div>
                  {detail && (
                    <span
                      className={`rounded-full px-2.5 py-1 text-[10px] font-semibold tracking-wide uppercase ${statusStyle(detail.status)}`}
                    >
                      {detail.status}
                    </span>
                  )}
                </div>
                {!detail ? (
                  <div className="flex min-h-60 flex-col items-center justify-center px-6 text-center">
                    <div className="flex size-11 items-center justify-center rounded-xl bg-muted text-muted-foreground">
                      <Icon name="terminal" className="size-5" />
                    </div>
                    <p className="mt-3 text-sm font-medium">No task selected</p>
                    <p className="mt-1 max-w-xs text-xs leading-relaxed text-muted-foreground">
                      Start a task or pick one from the recent list to see its
                      status and report.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-5 p-5">
                    <div className="grid grid-cols-2 gap-3">
                      <Quota
                        label="Tokens remaining"
                        value={detail.quota.remaining_tokens.toLocaleString()}
                      />
                      <Quota
                        label="Budget remaining"
                        value={`$${detail.quota.remaining_usd.toFixed(2)}`}
                      />
                    </div>
                    {pendingGrants.map((grant) => (
                      <div
                        key={grant.grant_id}
                        className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3"
                      >
                        <div className="text-xs font-semibold">
                          Approval needed
                        </div>
                        <p className="mt-1 text-xs break-all text-muted-foreground">
                          {grant.action}
                        </p>
                        <div className="mt-3 flex gap-2">
                          <Button
                            size="sm"
                            onClick={() =>
                              void decideGrant(grant.grant_id, true)
                            }
                          >
                            Approve
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void decideGrant(grant.grant_id, false)
                            }
                          >
                            Reject
                          </Button>
                        </div>
                      </div>
                    ))}
                    {report ? (
                      <>
                        <div>
                          <div className="text-[10px] font-semibold tracking-widest text-muted-foreground uppercase">
                            Worker summary
                          </div>
                          <p className="mt-2 text-sm leading-relaxed whitespace-pre-wrap">
                            {String(
                              report.output?.content ??
                                report.error ??
                                "Task finished; report has no summary text."
                            )}
                          </p>
                        </div>
                        {report.test_results?.length > 0 && (
                          <div>
                            <div className="text-[10px] font-semibold tracking-widest text-muted-foreground uppercase">
                              Checks
                            </div>
                            <div className="mt-2 space-y-2">
                              {report.test_results.map((result, index) => (
                                <div
                                  key={index}
                                  className="rounded-md bg-muted/60 px-3 py-2 text-xs"
                                >
                                  <div className="font-medium">
                                    {String(
                                      result.command ??
                                        result.name ??
                                        "Test command"
                                    )}
                                  </div>
                                  <div className="mt-1 text-muted-foreground">
                                    {String(
                                      result.result ??
                                        result.status ??
                                        JSON.stringify(result)
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {report.diff && (
                          <details className="rounded-lg border border-border">
                            <summary className="cursor-pointer px-3 py-2.5 text-xs font-medium">
                              View change summary
                            </summary>
                            <pre className="max-h-56 overflow-auto border-t border-border bg-muted/40 p-3 text-[11px] leading-relaxed">
                              {report.diff}
                            </pre>
                          </details>
                        )}
                        {report.unresolved_failures?.length > 0 && (
                          <div className="rounded-lg border border-destructive/25 bg-destructive/5 p-3 text-xs text-destructive">
                            <div className="font-semibold">
                              Unresolved failures
                            </div>
                            <ul className="mt-2 list-inside list-disc">
                              {report.unresolved_failures.map(
                                (failure, index) => (
                                  <li key={index}>{failure}</li>
                                )
                              )}
                            </ul>
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center">
                        <div className="mx-auto flex size-9 items-center justify-center rounded-full bg-primary/10 text-primary">
                          <Icon
                            name={
                              detail.status === "active" ? "clock" : "terminal"
                            }
                            className="size-4"
                          />
                        </div>
                        <p className="mt-3 text-sm font-medium">
                          {detail.status === "active"
                            ? "Worker is on it"
                            : "No report yet"}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          The task report will appear here as soon as it is
                          saved.
                        </p>
                      </div>
                    )}
                    <div className="border-t border-border pt-3 text-[10px] text-muted-foreground">
                      {detail.tickets.length} ticket
                      {detail.tickets.length === 1 ? "" : "s"} · updates every 2
                      seconds · stored in your local ledger
                    </div>
                  </div>
                )}
              </section>
            </div>
            <footer className="mt-8 flex flex-wrap items-center justify-between gap-2 border-t border-border/70 py-4 text-[10px] text-muted-foreground">
              <span>
                gh0st runs locally. External actions require an explicit grant.
              </span>
              <span>
                {health?.gateway_target ??
                  "Gateway configured by local runtime"}
              </span>
            </footer>
          </div>
        </main>
      </div>
    </div>
  )
}

function Metric({
  label,
  value,
  caption,
  icon,
}: {
  label: string
  value: string
  caption: string
  icon: string
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Icon name={icon} className="size-4 text-muted-foreground" />
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 text-[10px] text-muted-foreground">{caption}</div>
    </div>
  )
}

function Quota({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-muted/60 px-3 py-2.5">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-semibold tabular-nums">{value}</div>
    </div>
  )
}
