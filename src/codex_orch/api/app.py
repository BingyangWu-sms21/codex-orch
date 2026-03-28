from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

import uvicorn
import yaml
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from codex_orch.domain import (
    ApprovalMode,
    ConfidenceLevel,
    ControlActionKind,
    ControlActionStatus,
    ControlActor,
    DependencyKind,
    ManualGateStatus,
    RequestKind,
    ResolutionKind,
    RunSnapshot,
    TaskSpec,
    TaskStatus,
)
from codex_orch.runner import CodexExecRunner, TaskRunner
from codex_orch.scheduler import RunService
from codex_orch.store import ProjectStore
from codex_orch.task_pool import TaskPoolService

DEFAULT_WEB_PORT = 38473


def create_app(
    program_dir: Path,
    *,
    global_root: Path | None = None,
    runner: TaskRunner | None = None,
) -> FastAPI:
    store = ProjectStore(program_dir, global_root=global_root)
    task_pool = TaskPoolService(store)
    run_service = RunService(store, CodexExecRunner() if runner is None else runner)

    app = FastAPI(title="codex-orch")
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parent.parent / "web" / "templates")
    )
    static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def render(
        request: Request,
        template_name: str,
        context: dict[str, object],
    ) -> HTMLResponse:
        base_context: dict[str, object] = {
            "request": request,
            "program_dir": str(store.paths.root),
            "project": store.load_project() if store.paths.project_file.exists() else None,
            "prefect_ui_url": os.environ.get("PREFECT_UI_URL"),
            "task_statuses": [status.value for status in TaskStatus],
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template_name, base_context)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/tasks", status_code=303)

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request) -> HTMLResponse:
        tasks = task_pool.list_tasks()
        return render(
            request,
            "tasks.html",
            {
                "tasks": tasks,
                "compose_example": _compose_example(),
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str) -> HTMLResponse:
        task = task_pool.get_task(task_id)
        incoming_edges = [
            edge for edge in task_pool.list_edges() if edge.target == task.id
        ]
        return render(
            request,
            "task_detail.html",
            {
                "task": task,
                "task_yaml": _task_to_yaml(task),
                "compose_text": _compose_to_yaml(task),
                "incoming_edges": incoming_edges,
                "all_tasks": task_pool.list_tasks(),
            },
        )

    @app.post("/tasks", response_class=HTMLResponse)
    async def create_task(request: Request) -> Response:
        form = await request.form()
        try:
            task = _task_from_form(form, store=store)
            task_pool.save_task(task)
        except ValueError as exc:
            return render(
                request,
                "tasks.html",
                {
                    "tasks": task_pool.list_tasks(),
                    "compose_example": _compose_example(),
                    "error": str(exc),
                },
            )
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    @app.post("/tasks/{task_id}", response_class=HTMLResponse)
    async def update_task(request: Request, task_id: str) -> Response:
        existing = task_pool.get_task(task_id)
        form = await request.form()
        try:
            task = _task_from_form(form, store=store, existing=existing)
            task_pool.save_task(task)
            if task.id != existing.id:
                store.delete_task(existing.id)
        except ValueError as exc:
            return render(
                request,
                "task_detail.html",
                {
                    "task": existing,
                    "task_yaml": _task_to_yaml(existing),
                    "compose_text": _compose_to_yaml(existing),
                    "incoming_edges": [
                        edge
                        for edge in task_pool.list_edges()
                        if edge.target == existing.id
                    ],
                    "all_tasks": task_pool.list_tasks(),
                    "error": str(exc),
                },
            )
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    @app.post("/tasks/{task_id}/delete")
    async def delete_task(task_id: str) -> RedirectResponse:
        task_pool.delete_task(task_id)
        return RedirectResponse(url="/tasks", status_code=303)

    @app.post("/edges")
    async def add_edge(request: Request) -> RedirectResponse:
        form = await request.form()
        source = str(form.get("source", "")).strip()
        target = str(form.get("target", "")).strip()
        kind = DependencyKind(str(form.get("kind", DependencyKind.ORDER.value)))
        consume = _split_lines(str(form.get("consume", "")))
        store.add_edge(
            source_task_id=source,
            target_task_id=target,
            kind=kind,
            consume=consume,
        )
        task_pool.validate_graph()
        return RedirectResponse(url=f"/tasks/{target}", status_code=303)

    @app.post("/edges/delete")
    async def delete_edge(request: Request) -> RedirectResponse:
        form = await request.form()
        source = str(form.get("source", "")).strip()
        target = str(form.get("target", "")).strip()
        kind = DependencyKind(str(form.get("kind", DependencyKind.ORDER.value)))
        store.remove_edge(
            source_task_id=source,
            target_task_id=target,
            kind=kind,
        )
        task_pool.validate_graph()
        return RedirectResponse(url=f"/tasks/{target}", status_code=303)

    @app.get("/board", response_class=HTMLResponse)
    async def board_page(request: Request) -> HTMLResponse:
        columns: dict[str, list[TaskSpec]] = defaultdict(list)
        for task in task_pool.list_tasks():
            columns[task.status.value].append(task)
        return render(request, "board.html", {"columns": dict(columns)})

    @app.get("/graph", response_class=HTMLResponse)
    async def graph_page(request: Request) -> HTMLResponse:
        tasks = task_pool.list_tasks()
        edges = task_pool.list_edges()
        nodes_payload = [
            {"id": task.id, "label": task.title, "status": task.status.value}
            for task in tasks
        ]
        edges_payload = [
            {
                "from": edge.source,
                "to": edge.target,
                "label": edge.kind,
                "consume": list(edge.consume),
            }
            for edge in edges
        ]
        return render(
            request,
            "graph.html",
            {
                "nodes_json": json.dumps(nodes_payload),
                "edges_json": json.dumps(edges_payload),
            },
        )

    @app.get("/presets", response_class=HTMLResponse)
    async def presets_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "presets.html",
            {
                "presets": store.list_presets(),
                "preview_tasks": None,
                "preview_tasks_json": None,
            },
        )

    @app.post("/presets/preview", response_class=HTMLResponse)
    async def preview_preset(request: Request) -> HTMLResponse:
        form = await request.form()
        preset_id = str(form.get("preset_id", "")).strip()
        values = _parse_key_values(str(form.get("variables", "")))
        try:
            preview_tasks = task_pool.preview_preset(preset_id, values)
            error = None
        except ValueError as exc:
            preview_tasks = None
            error = str(exc)
        return render(
            request,
            "presets.html",
            {
                "presets": store.list_presets(),
                "preview_tasks": preview_tasks,
                "preview_tasks_json": None
                if preview_tasks is None
                else json.dumps(
                    [task.model_dump(mode="json") for task in preview_tasks],
                    indent=2,
                    sort_keys=True,
                ),
                "selected_preset_id": preset_id,
                "variables_text": str(form.get("variables", "")),
                "error": error,
            },
        )

    @app.post("/presets/apply")
    async def apply_preset(request: Request) -> RedirectResponse:
        form = await request.form()
        preset_id = str(form.get("preset_id", "")).strip()
        values = _parse_key_values(str(form.get("variables", "")))
        overwrite = str(form.get("overwrite", "")).lower() == "on"
        task_pool.apply_preset(preset_id, values, overwrite=overwrite)
        return RedirectResponse(url="/tasks", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        runs = store.list_runs()
        return render(
            request,
            "runs.html",
            {
                "runs": runs,
                "run_activity": {
                    run.id: _run_activity_summary(store, run)
                    for run in runs
                },
                "tasks": task_pool.list_tasks(),
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str) -> HTMLResponse:
        snapshot = store.get_run(run_id)
        return render(
            request,
            "run_detail.html",
            {
                "run": snapshot,
                "node_dirs": {
                    node_id: str(store.get_node_dir(run_id, node_id))
                    for node_id in snapshot.nodes
                },
                "node_runtimes": {
                    node_id: store.maybe_get_runtime(run_id, node_id)
                    for node_id in snapshot.nodes
                },
            },
        )

    @app.get("/assistant", response_class=HTMLResponse)
    async def assistant_page(request: Request) -> HTMLResponse:
        manual_gate_by_request = {
            record.gate.request_id: record for record in store.list_manual_gates()
        }
        return render(
            request,
            "assistant.html",
            {
                "assistant_requests": store.list_assistant_requests(),
                "manual_gate_by_request": manual_gate_by_request,
                "resolution_kinds": [kind.value for kind in ResolutionKind],
                "confidence_levels": [level.value for level in ConfidenceLevel],
                "control_action_kinds": [kind.value for kind in ControlActionKind],
                "control_action_statuses": [status.value for status in ControlActionStatus],
                "control_actors": [actor.value for actor in ControlActor],
                "approval_modes": [mode.value for mode in ApprovalMode],
            },
        )

    @app.post("/assistant/respond")
    async def assistant_respond(request: Request) -> RedirectResponse:
        form = await request.form()
        store.save_assistant_response_by_request_id(
            str(form.get("request_id", "")).strip(),
            resolution_kind=ResolutionKind(
                str(form.get("resolution_kind", ResolutionKind.AUTO_REPLY.value))
            ),
            answer=str(form.get("answer", "")).strip(),
            rationale=str(form.get("rationale", "")).strip(),
            confidence=ConfidenceLevel(
                str(form.get("confidence", ConfidenceLevel.MEDIUM.value))
            ),
            citations=_split_lines(str(form.get("citations", ""))),
            proposed_guidance_updates=_split_lines(
                str(form.get("guidance_updates", ""))
            ),
            proposed_control_actions=[
                ControlActionKind(value)
                for value in _split_lines(str(form.get("proposed_actions", "")))
            ],
        )
        return RedirectResponse(url="/assistant", status_code=303)

    @app.post("/assistant/actions")
    async def assistant_action_create(request: Request) -> RedirectResponse:
        form = await request.form()
        store.save_assistant_control_action_by_request_id(
            str(form.get("request_id", "")).strip(),
            requested_by=ControlActor(
                str(form.get("requested_by", ControlActor.ASSISTANT.value))
            ),
            action_kind=ControlActionKind(str(form.get("action_kind", ""))),
            target_kind=_nullable(str(form.get("target_kind", "")).strip()),
            target_path=_nullable(str(form.get("target_path", "")).strip()),
            payload=_parse_json_or_key_values(str(form.get("payload", ""))),
            reason=str(form.get("reason", "")).strip(),
            approval_mode=ApprovalMode(
                str(form.get("approval_mode", ApprovalMode.MANUAL_REQUIRED.value))
            ),
        )
        return RedirectResponse(url="/assistant", status_code=303)

    @app.post("/assistant/actions/status")
    async def assistant_action_status(request: Request) -> RedirectResponse:
        form = await request.form()
        store.update_assistant_control_action_status(
            str(form.get("request_id", "")).strip(),
            ControlActionStatus(str(form.get("status", ""))),
        )
        return RedirectResponse(url="/assistant", status_code=303)

    @app.get("/manual-gates", response_class=HTMLResponse)
    async def manual_gates_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "manual_gates.html",
            {
                "manual_gates": store.list_manual_gates(),
            },
        )

    @app.get("/manual-gates/{gate_id}", response_class=HTMLResponse)
    async def manual_gate_detail(request: Request, gate_id: str) -> HTMLResponse:
        record = store.find_manual_gate_by_gate_id(gate_id)
        return render(
            request,
            "manual_gate_detail.html",
            {
                "gate_record": record,
                "manual_gate_statuses": [status.value for status in ManualGateStatus],
            },
        )

    @app.post("/manual-gates/{gate_id}/respond")
    async def manual_gate_respond(request: Request, gate_id: str) -> RedirectResponse:
        form = await request.form()
        record = store.find_manual_gate_by_gate_id(gate_id)
        store.save_human_response_by_request_id(
            record.gate.request_id,
            answer=str(form.get("answer", "")).strip(),
        )
        return RedirectResponse(url=f"/manual-gates/{gate_id}", status_code=303)

    @app.post("/manual-gates/{gate_id}/approve")
    async def manual_gate_approve(
        gate_id: str,
        background_tasks: BackgroundTasks,
    ) -> RedirectResponse:
        record = store.find_manual_gate_by_gate_id(gate_id)
        store.update_manual_gate_status_by_request_id(
            record.gate.request_id,
            ManualGateStatus.APPROVED,
        )
        background_tasks.add_task(_resume_run_sync, run_service, record.run_id)
        return RedirectResponse(url=f"/runs/{record.run_id}", status_code=303)

    @app.post("/manual-gates/{gate_id}/reject")
    async def manual_gate_reject(
        gate_id: str,
        background_tasks: BackgroundTasks,
    ) -> RedirectResponse:
        record = store.find_manual_gate_by_gate_id(gate_id)
        store.update_manual_gate_status_by_request_id(
            record.gate.request_id,
            ManualGateStatus.REJECTED,
        )
        background_tasks.add_task(_resume_run_sync, run_service, record.run_id)
        return RedirectResponse(url=f"/runs/{record.run_id}", status_code=303)

    @app.post("/runs")
    async def start_run(request: Request, background_tasks: BackgroundTasks) -> Response:
        form = await request.form()
        roots = _split_csv(str(form.get("roots", "")))
        labels = _split_csv(str(form.get("labels", "")))
        user_inputs = _parse_key_values(str(form.get("inputs", "")))
        try:
            snapshot = run_service.create_snapshot(
                roots=roots,
                labels=labels,
                user_inputs=user_inputs,
            )
        except ValueError as exc:
            return render(
                request,
                "runs.html",
                {
                    "runs": store.list_runs(),
                    "tasks": task_pool.list_tasks(),
                    "error": str(exc),
                },
            )
        background_tasks.add_task(_run_snapshot_sync, run_service, snapshot.id)
        return RedirectResponse(url=f"/runs/{snapshot.id}", status_code=303)

    @app.post("/runs/{run_id}/resume")
    async def resume_run(
        run_id: str,
        background_tasks: BackgroundTasks,
    ) -> RedirectResponse:
        background_tasks.add_task(_resume_run_sync, run_service, run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/reconcile")
    async def reconcile_run(run_id: str) -> RedirectResponse:
        await run_service.reconcile_run(run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/abort")
    async def abort_run(run_id: str) -> RedirectResponse:
        await run_service.abort_run(run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    return app


def serve(
    program_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_WEB_PORT,
) -> None:
    uvicorn.run(create_app(program_dir), host=host, port=port)


def _run_snapshot_sync(run_service: RunService, run_id: str) -> None:
    asyncio.run(run_service.run_snapshot(run_id))


def _resume_run_sync(run_service: RunService, run_id: str) -> None:
    asyncio.run(run_service.resume_run(run_id))


def _run_activity_summary(
    store: ProjectStore,
    snapshot: RunSnapshot,
) -> dict[str, object]:
    running_nodes: list[str] = []
    last_progress_at: str | None = None
    last_event_summary: str | None = None
    for node_id, node in snapshot.nodes.items():
        runtime = store.maybe_get_runtime(snapshot.id, node_id)
        if runtime is None:
            continue
        if node.status.value == "running":
            running_nodes.append(node_id)
        if runtime.last_progress_at is not None and (
            last_progress_at is None or runtime.last_progress_at > last_progress_at
        ):
            last_progress_at = runtime.last_progress_at
            last_event_summary = runtime.last_event_summary
    return {
        "running_nodes": running_nodes,
        "last_progress_at": last_progress_at,
        "last_event_summary": last_event_summary,
    }


def _task_from_form(
    form: Mapping[str, object],
    *,
    store: ProjectStore,
    existing: TaskSpec | None = None,
) -> TaskSpec:
    project = store.load_project()
    task_id = _form_value(form, "id", existing.id if existing is not None else "")
    title = _form_value(form, "title", existing.title if existing is not None else "")
    agent = _form_value(
        form,
        "agent",
        existing.agent if existing is not None else project.default_agent,
    )
    status_raw = _form_value(
        form,
        "status",
        existing.status.value if existing is not None else TaskStatus.DRAFT.value,
    )
    description = _form_value(
        form,
        "description",
        existing.description if existing is not None else "",
    )
    compose_text = _form_value(
        form,
        "compose",
        _compose_to_yaml(existing) if existing is not None else "",
    )
    compose_payload = yaml.safe_load(compose_text) if compose_text.strip() else []
    if compose_payload is None:
        compose_payload = []
    publish = _split_lines(_form_value(form, "publish", "final.md"))
    labels = _split_csv(_form_value(form, "labels", ""))
    model = _nullable(_form_value(form, "model", existing.model if existing is not None and existing.model is not None else ""))
    sandbox = _nullable(
        _form_value(
            form,
            "sandbox",
            existing.sandbox if existing is not None and existing.sandbox is not None else project.default_sandbox,
        )
    )
    workspace = _nullable(
        _form_value(
            form,
            "workspace",
            existing.workspace if existing is not None and existing.workspace is not None else "",
        )
    )
    extra_writable_roots = _split_lines(
        _form_value(
            form,
            "extra_writable_roots",
            ""
            if existing is None
            else "\n".join(existing.extra_writable_roots),
        )
    )
    result_schema = _nullable(
        _form_value(
            form,
            "result_schema",
            existing.result_schema if existing is not None and existing.result_schema is not None else "",
        )
    )

    return TaskSpec(
        id=task_id,
        title=title,
        agent=agent,
        status=TaskStatus(status_raw),
        description=description,
        labels=labels,
        depends_on=[] if existing is None else existing.depends_on,
        compose=compose_payload,
        publish=publish,
        model=model,
        sandbox=sandbox,
        workspace=workspace,
        extra_writable_roots=extra_writable_roots,
        result_schema=result_schema,
    )


def _task_to_yaml(task: TaskSpec) -> str:
    return yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False)


def _compose_to_yaml(task: TaskSpec | None) -> str:
    if task is None:
        return _compose_example()
    payload = [step.model_dump(mode="json", exclude_none=True) for step in task.compose]
    return yaml.safe_dump(payload, sort_keys=False)


def _compose_example() -> str:
    return yaml.safe_dump(
        [
            {"kind": "file", "path": "prompts/analyze.md"},
            {"kind": "user_input", "key": "brief"},
        ],
        sort_keys=False,
    )


def _form_value(form: Mapping[str, object], key: str, default: str) -> str:
    value = form.get(key, default)
    return str(value).strip()


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _split_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        value = chunk.strip()
        if value:
            lines.append(value)
    return lines


def _parse_key_values(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError(f"expected key=value line, got {stripped}")
        key, value = stripped.split("=", maxsplit=1)
        pairs[key.strip()] = value.strip()
    return pairs


def _parse_json_or_key_values(raw: str) -> dict[str, object]:
    stripped = raw.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        return {str(key): value for key, value in payload.items()}
    parsed = _parse_key_values(stripped)
    return {key: value for key, value in parsed.items()}


def _nullable(value: str) -> str | None:
    return value or None
