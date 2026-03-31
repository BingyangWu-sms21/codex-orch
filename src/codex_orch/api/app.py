from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import uvicorn
import yaml
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from codex_orch.domain import RunRecord, TaskKind, TaskSpec, TaskStatus
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
            "task_statuses": [status.value for status in TaskStatus],
            "assistant_roles": list(store.list_assistant_roles().values()),
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
                "control_example": _control_example(),
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
                "control_text": _control_to_yaml(task),
                "assistant_hints_text": _assistant_hints_to_yaml(task),
                "interaction_policy_text": _interaction_policy_to_yaml(task),
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
                    "control_example": _control_example(),
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
                    "control_text": _control_to_yaml(existing),
                    "assistant_hints_text": _assistant_hints_to_yaml(existing),
                    "interaction_policy_text": _interaction_policy_to_yaml(existing),
                    "incoming_edges": [
                        edge for edge in task_pool.list_edges() if edge.target == existing.id
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
    async def create_edge(request: Request) -> RedirectResponse:
        form = await request.form()
        source = _form_value(form, "source", "")
        target = _form_value(form, "target", "")
        kind = _form_value(form, "kind", "order")
        scope_alias = _nullable(_form_value(form, "scope_alias", ""))
        consume = _split_lines(_form_value(form, "consume", ""))
        from codex_orch.domain import DependencyKind  # local import to keep module surface small

        store.add_edge(
            source_task_id=source,
            target_task_id=target,
            kind=DependencyKind(kind),
            consume=consume,
            as_=scope_alias,
        )
        task_pool.validate_graph()
        return RedirectResponse(url=f"/tasks/{target}", status_code=303)

    @app.post("/edges/delete")
    async def delete_edge(request: Request) -> RedirectResponse:
        form = await request.form()
        source = _form_value(form, "source", "")
        target = _form_value(form, "target", "")
        kind = _form_value(form, "kind", "order")
        from codex_orch.domain import DependencyKind  # local import to keep module surface small

        store.remove_edge(
            source_task_id=source,
            target_task_id=target,
            kind=DependencyKind(kind),
        )
        task_pool.validate_graph()
        return RedirectResponse(url=f"/tasks/{target}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        runs = store.list_runs()
        return render(
            request,
            "runs.html",
            {
                "runs": runs,
                "tasks": task_pool.list_tasks(),
                "run_activity": {
                    run.id: _run_activity_summary(store, run) for run in runs
                },
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str) -> HTMLResponse:
        run = store.get_run(run_id)
        return render(
            request,
            "run_detail.html",
            {
                "run": run,
                "instance_runtimes": {
                    instance_id: (
                        None
                        if instance.attempt == 0
                        else store.maybe_get_attempt_runtime(run.id, instance_id, instance.attempt)
                    )
                    for instance_id, instance in run.instances.items()
                },
                "instance_dirs": {
                    instance_id: str(store.get_instance_dir(run.id, instance_id))
                    for instance_id in run.instances
                },
            },
        )

    @app.post("/runs", response_class=HTMLResponse)
    async def create_run(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        form = await request.form()
        roots = _split_csv(_form_value(form, "roots", ""))
        labels = _split_csv(_form_value(form, "labels", ""))
        user_inputs = _parse_key_values(str(form.get("inputs", "")))
        try:
            run = run_service.create_snapshot(
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
                    "run_activity": {
                        item.id: _run_activity_summary(store, item)
                        for item in store.list_runs()
                    },
                    "error": str(exc),
                },
            )
        background_tasks.add_task(_run_snapshot_sync, run_service, run.id)
        return RedirectResponse(url=f"/runs/{run.id}", status_code=303)

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
    run: RunRecord,
) -> dict[str, object]:
    running_instances: list[str] = []
    last_progress_at: str | None = None
    last_event_summary: str | None = None
    for instance_id, instance in run.instances.items():
        if instance.attempt == 0:
            continue
        runtime = store.maybe_get_attempt_runtime(run.id, instance_id, instance.attempt)
        if runtime is None:
            continue
        if instance.status.value == "running":
            running_instances.append(instance_id)
        if runtime.last_progress_at is not None and (
            last_progress_at is None or runtime.last_progress_at > last_progress_at
        ):
            last_progress_at = runtime.last_progress_at
            last_event_summary = runtime.last_event_summary
    return {
        "running_instances": running_instances,
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
    kind_raw = _form_value(
        form,
        "kind",
        existing.kind.value if existing is not None else TaskKind.WORK.value,
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
    control_text = _form_value(
        form,
        "control",
        _control_to_yaml(existing),
    )
    control_payload = yaml.safe_load(control_text) if control_text.strip() else None
    if control_payload == {}:
        control_payload = None
    publish = _split_lines(_form_value(form, "publish", "final.md"))
    labels = _split_csv(_form_value(form, "labels", ""))
    assistant_hints_text = _form_value(
        form,
        "assistant_hints",
        _assistant_hints_to_yaml(existing),
    )
    interaction_policy_text = _form_value(
        form,
        "interaction_policy",
        _interaction_policy_to_yaml(existing),
    )
    assistant_hints = (
        yaml.safe_load(assistant_hints_text) if assistant_hints_text.strip() else {}
    )
    if assistant_hints is None:
        assistant_hints = {}
    interaction_policy = (
        yaml.safe_load(interaction_policy_text)
        if interaction_policy_text.strip()
        else {}
    )
    if interaction_policy is None:
        interaction_policy = {}
    model = _nullable(
        _form_value(
            form,
            "model",
            existing.model if existing is not None and existing.model is not None else "",
        )
    )
    sandbox = _nullable(
        _form_value(
            form,
            "sandbox",
            existing.sandbox
            if existing is not None and existing.sandbox is not None
            else project.default_sandbox,
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
        kind=TaskKind(kind_raw),
        status=TaskStatus(status_raw),
        description=description,
        labels=labels,
        depends_on=[] if existing is None else existing.depends_on,
        compose=compose_payload,
        publish=publish,
        control=control_payload,
        assistant_hints=assistant_hints,
        interaction_policy=interaction_policy,
        model=model,
        sandbox=sandbox,
        workspace=workspace,
        extra_writable_roots=extra_writable_roots,
        result_schema=result_schema,
    )


def _task_to_yaml(task: TaskSpec) -> str:
    return yaml.safe_dump(task.model_dump(mode="json", by_alias=True), sort_keys=False)


def _compose_to_yaml(task: TaskSpec | None) -> str:
    if task is None:
        return _compose_example()
    payload = [
        step.model_dump(mode="json", exclude_none=True, by_alias=True) for step in task.compose
    ]
    return yaml.safe_dump(payload, sort_keys=False)


def _control_to_yaml(task: TaskSpec | None) -> str:
    if task is None or task.control is None:
        return yaml.safe_dump({}, sort_keys=False)
    payload = task.control.model_dump(mode="json", exclude_none=True, by_alias=True)
    return yaml.safe_dump(payload, sort_keys=False)


def _assistant_hints_to_yaml(task: TaskSpec | None) -> str:
    if task is None:
        return yaml.safe_dump({}, sort_keys=False)
    payload = task.assistant_hints.model_dump(mode="json", exclude_defaults=True)
    return yaml.safe_dump(payload, sort_keys=False)


def _interaction_policy_to_yaml(task: TaskSpec | None) -> str:
    if task is None:
        return yaml.safe_dump({}, sort_keys=False)
    payload = task.interaction_policy.model_dump(mode="json", exclude_defaults=True)
    return yaml.safe_dump(payload, sort_keys=False)


def _compose_example() -> str:
    return yaml.safe_dump(
        [
            {"kind": "file", "path": "prompts/analyze.md"},
            {"kind": "ref", "ref": "inputs.brief"},
        ],
        sort_keys=False,
    )


def _control_example() -> str:
    return yaml.safe_dump(
        {
            "mode": "route",
            "routes": [
                {
                    "label": "done",
                    "targets": ["publish_summary"],
                }
            ]
        },
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


def _nullable(value: str) -> str | None:
    return value or None
