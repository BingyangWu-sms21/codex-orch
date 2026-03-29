from __future__ import annotations

import asyncio
import json
import os
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

import typer
import yaml

from codex_orch.assistant import AssistantWorkerService, CodexCliAssistantBackend
from codex_orch.api.app import DEFAULT_WEB_PORT, serve
from codex_orch.domain import (
    ApprovalMode,
    ConfidenceLevel,
    ControlActionKind,
    ControlActionStatus,
    ControlActor,
    DecisionKind,
    DependencyKind,
    ManualGateStatus,
    ProjectSpec,
    RequestKind,
    RequestPriority,
    ResolutionKind,
    TaskSpec,
    TaskStatus,
)
from codex_orch.runner import CodexExecRunner
from codex_orch.scheduler import RunService
from codex_orch.skills import (
    export_builtin_skill,
    install_builtin_skill,
    list_builtin_skills,
)
from codex_orch.store import AssistantRequestRecord, ManualGateRecord, ProjectStore
from codex_orch.task_pool import TaskPoolService

app = typer.Typer(help="File-backed task orchestrator for Codex CLI.")
project_app = typer.Typer(help="Initialize and inspect program directories.")
task_app = typer.Typer(help="CRUD operations for tasks.")
edge_app = typer.Typer(help="Manage dependency edges.")
preset_app = typer.Typer(help="Preview and apply presets.")
run_app = typer.Typer(help="Create, resume, and inspect runs.")
graph_app = typer.Typer(help="Display the task graph.")
inspect_app = typer.Typer(help="Inspect tasks and runs.")
assistant_app = typer.Typer(help="Assistant protocol helper commands.")
assistant_request_app = typer.Typer(help="Manage assistant requests.")
assistant_action_app = typer.Typer(help="Manage assistant control actions.")
manual_gate_app = typer.Typer(help="Manage human manual gates.")
skill_app = typer.Typer(help="Export bundled Codex skills.")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(edge_app, name="edge")
app.add_typer(preset_app, name="preset")
app.add_typer(run_app, name="run")
app.add_typer(graph_app, name="graph")
app.add_typer(inspect_app, name="inspect")
assistant_app.add_typer(assistant_request_app, name="request")
assistant_app.add_typer(assistant_action_app, name="action")
app.add_typer(assistant_app, name="assistant")
app.add_typer(manual_gate_app, name="manual-gate")
app.add_typer(skill_app, name="skill")


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(package_version("codex-orch"))
    except PackageNotFoundError:
        typer.echo("unknown")
    raise typer.Exit()


@app.callback()
def app_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show codex-orch version and exit.",
    ),
) -> None:
    del version


def _store(program_dir: Path) -> ProjectStore:
    return ProjectStore(program_dir, global_root=_resolve_global_root())


def _task_pool(program_dir: Path) -> TaskPoolService:
    return TaskPoolService(_store(program_dir))


def _run_service(program_dir: Path) -> RunService:
    return RunService(_store(program_dir), CodexExecRunner())


def _assistant_worker_service(program_dir: Path) -> AssistantWorkerService:
    store = _store(program_dir)
    return AssistantWorkerService(
        store,
        backend=CodexCliAssistantBackend(),
        run_service=RunService(store, CodexExecRunner()),
    )


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _parse_key_values(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise typer.BadParameter(f"expected key=value, got {entry}")
        key, value = entry.split("=", maxsplit=1)
        parsed[key.strip()] = value.strip()
    return parsed


def _resolve_program_dir(program_dir: Path | None) -> Path:
    if program_dir is not None:
        return program_dir
    raw = os.environ.get("CODEX_ORCH_PROGRAM_DIR")
    if raw:
        return Path(raw)
    raise typer.BadParameter("program directory is required or set CODEX_ORCH_PROGRAM_DIR")


def _resolve_global_root() -> Path | None:
    raw = os.environ.get("CODEX_ORCH_GLOBAL_ROOT")
    if not raw:
        return None
    return Path(raw)


def _resolve_run_id(run_id: str | None) -> str:
    if run_id is not None:
        return run_id
    raw = os.environ.get("CODEX_ORCH_RUN_ID")
    if raw:
        return raw
    raise typer.BadParameter("run id is required or set CODEX_ORCH_RUN_ID")


def _resolve_task_id(task_id: str | None) -> str:
    if task_id is not None:
        return task_id
    raw = os.environ.get("CODEX_ORCH_TASK_ID")
    if raw:
        return raw
    raise typer.BadParameter("task id is required or set CODEX_ORCH_TASK_ID")


def _read_text_input(value: str | None, value_file: Path | None, *, field: str) -> str:
    if value is not None and value_file is not None:
        raise typer.BadParameter(f"{field} and {field}-file are mutually exclusive")
    if value_file is not None:
        return value_file.read_text(encoding="utf-8").strip()
    if value is not None:
        return value.strip()
    raise typer.BadParameter(f"{field} or {field}-file is required")


def _parse_json_payload(
    pairs: list[str] | None,
    payload_file: Path | None,
) -> dict[str, object]:
    if pairs and payload_file is not None:
        raise typer.BadParameter("payload and payload-file are mutually exclusive")
    if payload_file is not None:
        payload = json.loads(payload_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise typer.BadParameter("payload file must contain a JSON object")
        return {str(key): value for key, value in payload.items()}
    if not pairs:
        return {}
    parsed = _parse_key_values(pairs)
    return {key: value for key, value in parsed.items()}


def _assistant_record_payload(record: AssistantRequestRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": record.run_id,
        "task_id": record.task_id,
        "node_dir": str(record.node_dir),
        "request": record.request.model_dump(mode="json"),
        "response": None
        if record.response is None
        else record.response.model_dump(mode="json"),
        "control_action": None
        if record.control_action is None
        else record.control_action.model_dump(mode="json"),
    }
    return payload


def _manual_gate_record_payload(record: ManualGateRecord) -> dict[str, object]:
    return {
        "run_id": record.run_id,
        "task_id": record.task_id,
        "node_dir": str(record.node_dir),
        "manual_gate": record.gate.model_dump(mode="json"),
        "human_request": None
        if record.human_request is None
        else record.human_request.model_dump(mode="json"),
        "human_response": None
        if record.human_response is None
        else record.human_response.model_dump(mode="json"),
    }


@project_app.command("init")
def project_init(
    program_dir: Path,
    name: str,
    workspace: Path,
    description: str = "",
    default_agent: str = "default",
    default_assistant_profile: str | None = None,
    default_sandbox: str = "workspace-write",
    max_concurrency: int = 2,
) -> None:
    store = _store(program_dir)
    project = ProjectSpec(
        name=name,
        workspace=str(workspace.resolve()),
        description=description,
        default_agent=default_agent,
        default_assistant_profile=default_assistant_profile,
        default_sandbox=default_sandbox,
        max_concurrency=max_concurrency,
    )
    store.save_project(project)
    typer.echo(f"Initialized program at {store.paths.root}")


@task_app.command("list")
def task_list(
    program_dir: Path,
    status: list[TaskStatus] | None = typer.Option(None),
    label: str | None = None,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    pool = _task_pool(program_dir)
    statuses = set(status) if status is not None else None
    tasks = pool.list_tasks(statuses=statuses, label=label)
    if as_json:
        _print_json([task.model_dump(mode="json") for task in tasks])
        return
    for task in tasks:
        typer.echo(f"{task.id}\t{task.status.value}\t{task.agent}\t{task.title}")


@task_app.command("show")
def task_show(program_dir: Path, task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    task = _task_pool(program_dir).get_task(task_id)
    if as_json:
        _print_json(task.model_dump(mode="json"))
        return
    typer.echo(yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False))


@task_app.command("add")
def task_add(program_dir: Path, spec: Path) -> None:
    store = _store(program_dir)
    payload = yaml.safe_load(spec.read_text(encoding="utf-8"))
    task = TaskSpec.model_validate(payload)
    TaskPoolService(store).save_task(task)
    typer.echo(f"Added task {task.id}")


@task_app.command("update")
def task_update(program_dir: Path, task_id: str, spec: Path) -> None:
    store = _store(program_dir)
    existing = store.get_task(task_id)
    payload = yaml.safe_load(spec.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("task spec must be a YAML object")
    merged = existing.model_dump(mode="json")
    merged.update(payload)
    task = TaskSpec.model_validate(merged)
    TaskPoolService(store).save_task(task)
    if task.id != existing.id:
        store.delete_task(existing.id)
    typer.echo(f"Updated task {task.id}")


@task_app.command("delete")
def task_delete(program_dir: Path, task_id: str) -> None:
    _task_pool(program_dir).delete_task(task_id)
    typer.echo(f"Deleted task {task_id}")


@edge_app.command("list")
def edge_list(program_dir: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    edges = _task_pool(program_dir).list_edges()
    payload = [
        {
            "source": edge.source,
            "target": edge.target,
            "kind": edge.kind,
            "consume": list(edge.consume),
        }
        for edge in edges
    ]
    if as_json:
        _print_json(payload)
        return
    for edge in payload:
        typer.echo(
            f"{edge['source']} -> {edge['target']} [{edge['kind']}] consume={edge['consume']}"
        )


@edge_app.command("add")
def edge_add(
    program_dir: Path,
    source: str,
    target: str,
    kind: DependencyKind,
    consume: list[str] | None = typer.Option(None),
) -> None:
    store = _store(program_dir)
    store.add_edge(
        source_task_id=source,
        target_task_id=target,
        kind=kind,
        consume=[] if consume is None else consume,
    )
    TaskPoolService(store).validate_graph()
    typer.echo(f"Added {kind.value} edge {source} -> {target}")


@edge_app.command("remove")
def edge_remove(
    program_dir: Path,
    source: str,
    target: str,
    kind: DependencyKind,
) -> None:
    store = _store(program_dir)
    store.remove_edge(source_task_id=source, target_task_id=target, kind=kind)
    TaskPoolService(store).validate_graph()
    typer.echo(f"Removed {kind.value} edge {source} -> {target}")


@preset_app.command("list")
def preset_list(program_dir: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    presets = _store(program_dir).list_presets()
    payload = {
        preset_id: {
            "source": resolved.source,
            "preset": resolved.preset.model_dump(mode="json"),
        }
        for preset_id, resolved in presets.items()
    }
    if as_json:
        _print_json(payload)
        return
    for preset_id, resolved in presets.items():
        typer.echo(f"{preset_id}\t{resolved.source}\t{resolved.preset.title}")


@preset_app.command("preview")
def preset_preview(
    program_dir: Path,
    preset_id: str,
    var: list[str] | None = typer.Option(None),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    tasks = _task_pool(program_dir).preview_preset(
        preset_id,
        _parse_key_values([] if var is None else var),
    )
    payload = [task.model_dump(mode="json") for task in tasks]
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@preset_app.command("apply")
def preset_apply(
    program_dir: Path,
    preset_id: str,
    var: list[str] | None = typer.Option(None),
    overwrite: bool = False,
) -> None:
    tasks = _task_pool(program_dir).apply_preset(
        preset_id,
        _parse_key_values([] if var is None else var),
        overwrite=overwrite,
    )
    typer.echo(f"Applied preset {preset_id}: {', '.join(task.id for task in tasks)}")


@run_app.command("start")
def run_start(
    program_dir: Path,
    root: list[str] | None = typer.Option(None),
    label: list[str] | None = typer.Option(None),
    input_value: list[str] | None = typer.Option(None, "--input"),
    wait: bool = True,
) -> None:
    service = _run_service(program_dir)
    root_values = [] if root is None else root
    label_values = [] if label is None else label
    input_values = [] if input_value is None else input_value
    if wait:
        snapshot = asyncio.run(
            service.start_run(
                roots=root_values,
                labels=label_values,
                user_inputs=_parse_key_values(input_values),
            )
        )
    else:
        snapshot = service.create_snapshot(
            roots=root_values,
            labels=label_values,
            user_inputs=_parse_key_values(input_values),
        )
    typer.echo(snapshot.id)


@run_app.command("resume")
def run_resume(program_dir: Path, run_id: str) -> None:
    snapshot = asyncio.run(_run_service(program_dir).resume_run(run_id))
    typer.echo(snapshot.id)


@run_app.command("reconcile")
def run_reconcile(
    program_dir: Path,
    run_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    snapshot = asyncio.run(_run_service(program_dir).reconcile_run(run_id))
    if as_json:
        _print_json(snapshot.model_dump(mode="json"))
        return
    typer.echo(snapshot.id)


@run_app.command("abort")
def run_abort(
    program_dir: Path,
    run_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    snapshot = asyncio.run(_run_service(program_dir).abort_run(run_id))
    if as_json:
        _print_json(snapshot.model_dump(mode="json"))
        return
    typer.echo(snapshot.id)


@run_app.command("list")
def run_list(program_dir: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    runs = _store(program_dir).list_runs()
    if as_json:
        _print_json([snapshot.model_dump(mode="json") for snapshot in runs])
        return
    for snapshot in runs:
        prefect_suffix = (
            ""
            if snapshot.prefect_flow_run_id is None
            else f"\tprefect={snapshot.prefect_flow_run_id}"
        )
        typer.echo(
            f"{snapshot.id}\t{snapshot.status.value}\troots={','.join(snapshot.roots)}{prefect_suffix}"
        )


@run_app.command("show")
def run_show(program_dir: Path, run_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    snapshot = _store(program_dir).get_run(run_id)
    if as_json:
        _print_json(snapshot.model_dump(mode="json"))
        return
    typer.echo(yaml.safe_dump(snapshot.model_dump(mode="json"), sort_keys=False))


@assistant_request_app.command("create")
def assistant_request_create(
    program_dir: Path | None = typer.Option(None, "--program-dir"),
    run_id: str | None = typer.Option(None, "--run-id"),
    task_id: str | None = typer.Option(None, "--task-id"),
    kind: RequestKind = typer.Option(..., "--kind"),
    decision_kind: DecisionKind = typer.Option(..., "--decision-kind"),
    question: str | None = typer.Option(None, "--question"),
    question_file: Path | None = typer.Option(None, "--question-file"),
    option: list[str] | None = typer.Option(None, "--option"),
    artifact: list[str] | None = typer.Option(None, "--artifact"),
    requested_action: list[ControlActionKind] | None = typer.Option(
        None,
        "--requested-action",
    ),
    priority: RequestPriority = typer.Option(RequestPriority.NORMAL, "--priority"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    resolved_program_dir = _resolve_program_dir(program_dir)
    try:
        request = _store(resolved_program_dir).create_assistant_request(
            run_id=_resolve_run_id(run_id),
            task_id=_resolve_task_id(task_id),
            request_kind=kind,
            question=_read_text_input(question, question_file, field="question"),
            decision_kind=decision_kind,
            options=[] if option is None else option,
            context_artifacts=[] if artifact is None else artifact,
            requested_control_actions=[]
            if requested_action is None
            else requested_action,
            priority=priority,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _print_json(request.model_dump(mode="json"))
        return
    typer.echo(request.request_id)


@assistant_request_app.command("list")
def assistant_request_list(
    program_dir: Path,
    run_id: str | None = typer.Option(None, "--run-id"),
    unresolved_only: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    records = _store(program_dir).list_assistant_requests(
        run_id=run_id,
        unresolved_only=unresolved_only,
    )
    if as_json:
        _print_json([_assistant_record_payload(record) for record in records])
        return
    for record in records:
        resolution = "-" if record.response is None else record.response.resolution_kind.value
        typer.echo(
            f"{record.request.request_id}\trun={record.run_id}\ttask={record.task_id}\tresolution={resolution}"
        )


@assistant_request_app.command("show")
def assistant_request_show(
    program_dir: Path,
    request_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    record = _store(program_dir).find_assistant_request(request_id)
    payload = _assistant_record_payload(record)
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@assistant_app.command("worker")
def assistant_worker(
    program_dir: Path,
    once: bool = typer.Option(False, "--once"),
    poll_interval_sec: float = typer.Option(5.0, "--poll-interval-sec"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    worker = _assistant_worker_service(program_dir)
    if once:
        stats = worker.run_once()
        payload = {
            "scanned": stats.scanned,
            "processed": stats.processed,
            "auto_replied": stats.auto_replied,
            "handed_off": stats.handed_off,
            "skipped_no_profile": stats.skipped_no_profile,
            "failed": stats.failed,
        }
        if as_json:
            _print_json(payload)
            return
        typer.echo(
            " ".join(f"{key}={value}" for key, value in payload.items())
        )
        return
    worker.serve_forever(poll_interval_sec=poll_interval_sec)


@assistant_app.command("respond")
def assistant_respond(
    program_dir: Path,
    request_id: str,
    resolution_kind: ResolutionKind = typer.Option(..., "--resolution-kind"),
    answer: str | None = typer.Option(None, "--answer"),
    answer_file: Path | None = typer.Option(None, "--answer-file"),
    rationale: str | None = typer.Option(None, "--rationale"),
    rationale_file: Path | None = typer.Option(None, "--rationale-file"),
    confidence: ConfidenceLevel = typer.Option(ConfidenceLevel.MEDIUM, "--confidence"),
    citation: list[str] | None = typer.Option(None, "--citation"),
    guidance_update: list[str] | None = typer.Option(None, "--guidance-update"),
    proposed_action: list[ControlActionKind] | None = typer.Option(
        None,
        "--proposed-action",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    response = _store(program_dir).save_assistant_response_by_request_id(
        request_id,
        resolution_kind=resolution_kind,
        answer=_read_text_input(answer, answer_file, field="answer"),
        rationale=_read_text_input(rationale, rationale_file, field="rationale"),
        confidence=confidence,
        citations=[] if citation is None else citation,
        proposed_guidance_updates=[]
        if guidance_update is None
        else guidance_update,
        proposed_control_actions=[]
        if proposed_action is None
        else proposed_action,
    )
    if as_json:
        _print_json(response.model_dump(mode="json"))
        return
    typer.echo(response.request_id)


@assistant_action_app.command("create")
def assistant_action_create(
    program_dir: Path,
    request_id: str,
    action_kind: ControlActionKind = typer.Option(..., "--action-kind"),
    requested_by: ControlActor = typer.Option(ControlActor.ASSISTANT, "--requested-by"),
    target_kind: str | None = typer.Option(None, "--target-kind"),
    target_path: str | None = typer.Option(None, "--target-path"),
    payload: list[str] | None = typer.Option(None, "--payload"),
    payload_file: Path | None = typer.Option(None, "--payload-file"),
    reason: str = typer.Option(..., "--reason"),
    approval_mode: ApprovalMode = typer.Option(
        ApprovalMode.MANUAL_REQUIRED,
        "--approval-mode",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    action = _store(program_dir).save_assistant_control_action_by_request_id(
        request_id,
        requested_by=requested_by,
        action_kind=action_kind,
        target_kind=target_kind,
        target_path=target_path,
        payload=_parse_json_payload(payload, payload_file),
        reason=reason.strip(),
        approval_mode=approval_mode,
    )
    if as_json:
        _print_json(action.model_dump(mode="json"))
        return
    typer.echo(action.action_id)


@assistant_action_app.command("status")
def assistant_action_status(
    program_dir: Path,
    request_id: str,
    status: ControlActionStatus = typer.Option(..., "--status"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    action = _store(program_dir).update_assistant_control_action_status(
        request_id,
        status,
    )
    if as_json:
        _print_json(action.model_dump(mode="json"))
        return
    typer.echo(action.status.value)


@skill_app.command("list")
def skill_list(as_json: bool = typer.Option(False, "--json")) -> None:
    skills = list_builtin_skills()
    payload = [
        {
            "id": skill.skill_id,
            "description": skill.description,
        }
        for skill in skills
    ]
    if as_json:
        _print_json(payload)
        return
    for skill in payload:
        typer.echo(f"{skill['id']}\t{skill['description']}")


@skill_app.command("export")
def skill_export(
    skill_id: str,
    destination_dir: Path,
    overwrite: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    exported_path = export_builtin_skill(
        skill_id,
        destination_dir,
        overwrite=overwrite,
    )
    payload = {"skill_id": skill_id, "exported_path": str(exported_path)}
    if as_json:
        _print_json(payload)
        return
    typer.echo(str(exported_path))


@skill_app.command("install")
def skill_install(
    skill_id: str,
    repo_dir: Path | None = typer.Option(None, "--repo-dir"),
    user_scope: bool = typer.Option(False, "--user"),
    overwrite: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    installed_path = install_builtin_skill(
        skill_id,
        repo_dir=repo_dir,
        user_scope=user_scope,
        overwrite=overwrite,
    )
    payload = {"skill_id": skill_id, "installed_path": str(installed_path)}
    if as_json:
        _print_json(payload)
        return
    typer.echo(str(installed_path))


@manual_gate_app.command("list")
def manual_gate_list(
    program_dir: Path,
    run_id: str | None = typer.Option(None, "--run-id"),
    unresolved_only: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    records = _store(program_dir).list_manual_gates(
        run_id=run_id,
        unresolved_only=unresolved_only,
    )
    if as_json:
        _print_json([_manual_gate_record_payload(record) for record in records])
        return
    for record in records:
        typer.echo(
            f"{record.gate.gate_id}\trun={record.run_id}\ttask={record.task_id}\tstatus={record.gate.status.value}"
        )


@manual_gate_app.command("show")
def manual_gate_show(
    program_dir: Path,
    gate_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    record = _store(program_dir).find_manual_gate_by_gate_id(gate_id)
    payload = _manual_gate_record_payload(record)
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@manual_gate_app.command("respond")
def manual_gate_respond(
    program_dir: Path,
    gate_id: str,
    answer: str | None = typer.Option(None, "--answer"),
    answer_file: Path | None = typer.Option(None, "--answer-file"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    record = _store(program_dir).find_manual_gate_by_gate_id(gate_id)
    response = _store(program_dir).save_human_response_by_request_id(
        record.gate.request_id,
        answer=_read_text_input(answer, answer_file, field="answer"),
    )
    if as_json:
        _print_json(response.model_dump(mode="json"))
        return
    typer.echo(response.request_id)


@manual_gate_app.command("approve")
def manual_gate_approve(
    program_dir: Path,
    gate_id: str,
    resume: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    store = _store(program_dir)
    record = store.find_manual_gate_by_gate_id(gate_id)
    gate = store.update_manual_gate_status_by_request_id(
        record.gate.request_id,
        ManualGateStatus.APPROVED,
    )
    snapshot = None
    if resume:
        snapshot = asyncio.run(_run_service(program_dir).resume_run(record.run_id))
        gate = _store(program_dir).find_manual_gate_by_gate_id(gate_id).gate
    payload: dict[str, object] = {"manual_gate": gate.model_dump(mode="json")}
    if snapshot is not None:
        payload["run"] = snapshot.model_dump(mode="json")
    if as_json:
        _print_json(payload)
        return
    typer.echo(gate.status.value)


@manual_gate_app.command("reject")
def manual_gate_reject(
    program_dir: Path,
    gate_id: str,
    refresh_run: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    store = _store(program_dir)
    record = store.find_manual_gate_by_gate_id(gate_id)
    gate = store.update_manual_gate_status_by_request_id(
        record.gate.request_id,
        ManualGateStatus.REJECTED,
    )
    snapshot = None
    if refresh_run:
        snapshot = asyncio.run(_run_service(program_dir).resume_run(record.run_id))
        gate = _store(program_dir).find_manual_gate_by_gate_id(gate_id).gate
    payload: dict[str, object] = {"manual_gate": gate.model_dump(mode="json")}
    if snapshot is not None:
        payload["run"] = snapshot.model_dump(mode="json")
    if as_json:
        _print_json(payload)
        return
    typer.echo(gate.status.value)


@graph_app.command("show")
def graph_show(program_dir: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    tasks = _task_pool(program_dir).list_tasks()
    edges = _task_pool(program_dir).list_edges()
    payload = {
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "edges": [
            {
                "source": edge.source,
                "target": edge.target,
                "kind": edge.kind,
                "consume": list(edge.consume),
            }
            for edge in edges
        ],
    }
    if as_json:
        _print_json(payload)
        return
    for edge in payload["edges"]:
        typer.echo(
            f"{edge['source']} -> {edge['target']} [{edge['kind']}]"
        )


@inspect_app.command("task")
def inspect_task(program_dir: Path, task_id: str) -> None:
    task_show(program_dir, task_id, as_json=False)


@inspect_app.command("run")
def inspect_run(program_dir: Path, run_id: str) -> None:
    run_show(program_dir, run_id, as_json=False)


@app.command("web")
def web(
    program_dir: Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_WEB_PORT,
) -> None:
    serve(program_dir, host=host, port=port)


def main() -> None:
    app()
