from __future__ import annotations

import asyncio
import json
import os
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

import typer
import yaml

from codex_orch.assistant import (
    AssistantRoleRouter,
    AssistantWorkerService,
    CodexCliAssistantBackend,
)
from codex_orch.assistant_docs import install_assistant_operating_model
from codex_orch.api.app import DEFAULT_WEB_PORT, serve
from codex_orch.domain import (
    AssistantUpdateKind,
    AssistantUpdateStatus,
    ConfidenceLevel,
    DecisionKind,
    DependencyKind,
    InterruptAudience,
    InterruptReplyKind,
    ProjectSpec,
    RequestKind,
    RequestPriority,
    TaskSpec,
    TaskStatus,
)
from codex_orch.input_values import JsonValue, parse_json_input_override
from codex_orch.runner import CodexExecRunner
from codex_orch.scheduler import RunService
from codex_orch.skills import (
    export_builtin_skill,
    install_builtin_skill,
    list_builtin_skills,
)
from codex_orch.store import InterruptRecord, ProjectStore
from codex_orch.task_pool import ProgramValidationIssue, TaskPoolService

app = typer.Typer(help="File-backed task orchestrator for Codex CLI.")
project_app = typer.Typer(help="Initialize and inspect program directories.")
task_app = typer.Typer(help="CRUD operations for tasks.")
edge_app = typer.Typer(help="Manage dependency edges.")
preset_app = typer.Typer(help="Preview and apply presets.")
run_app = typer.Typer(help="Create, resume, and inspect runs.")
graph_app = typer.Typer(help="Display the task graph.")
inspect_app = typer.Typer(help="Inspect tasks and runs.")
interrupt_app = typer.Typer(help="Create and inspect runtime interrupts.")
inbox_app = typer.Typer(help="Operate the runtime inbox and assistant worker.")
proposal_app = typer.Typer(help="Inspect and mark recorded assistant update proposals.")
assistant_doc_app = typer.Typer(help="Install shared assistant operating model docs.")
skill_app = typer.Typer(help="Export bundled Codex skills.")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(edge_app, name="edge")
app.add_typer(preset_app, name="preset")
app.add_typer(run_app, name="run")
app.add_typer(graph_app, name="graph")
app.add_typer(inspect_app, name="inspect")
app.add_typer(interrupt_app, name="interrupt")
app.add_typer(inbox_app, name="inbox")
app.add_typer(proposal_app, name="proposal")
app.add_typer(assistant_doc_app, name="assistant-doc")
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


def _assistant_router(program_dir: Path) -> AssistantRoleRouter:
    return AssistantRoleRouter(_store(program_dir))


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _parse_key_value_entry(entry: str, *, option_name: str) -> tuple[str, str]:
    if "=" not in entry:
        raise typer.BadParameter(f"expected key=value for {option_name}, got {entry}")
    key, value = entry.split("=", maxsplit=1)
    normalized_key = key.strip()
    if not normalized_key:
        raise typer.BadParameter(f"{option_name} key must not be empty")
    return normalized_key, value.strip()


def _parse_key_values(values: list[str], *, option_name: str = "--input") -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in values:
        key, value = _parse_key_value_entry(entry, option_name=option_name)
        parsed[key] = value
    return parsed


def _parse_json_key_values(values: list[str]) -> dict[str, JsonValue]:
    parsed: dict[str, JsonValue] = {}
    for entry in values:
        key, raw_value = _parse_key_value_entry(entry, option_name="--input-json")
        try:
            parsed[key] = parse_json_input_override(
                raw_value,
                field_name=f"--input-json {key}",
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return parsed


def _parse_json_file_key_values(values: list[str]) -> dict[str, JsonValue]:
    parsed: dict[str, JsonValue] = {}
    for entry in values:
        key, raw_path = _parse_key_value_entry(entry, option_name="--input-json-file")
        path = Path(raw_path)
        try:
            parsed[key] = parse_json_input_override(
                path.read_text(encoding="utf-8"),
                field_name=f"--input-json-file {key}",
            )
        except OSError as exc:
            raise typer.BadParameter(
                f"--input-json-file {key} could not read {path}: {exc}"
            ) from exc
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return parsed


def _parse_user_input_overrides(
    *,
    string_values: list[str],
    json_values: list[str],
    json_file_values: list[str],
) -> dict[str, JsonValue]:
    merged: dict[str, JsonValue] = {}
    merged.update(_parse_key_values(string_values, option_name="--input"))
    merged.update(_parse_json_key_values(json_values))
    merged.update(_parse_json_file_key_values(json_file_values))
    return merged


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


def _resolve_instance_id(instance_id: str | None) -> str:
    if instance_id is not None:
        return instance_id
    raw = os.environ.get("CODEX_ORCH_INSTANCE_ID")
    if raw:
        return raw
    raise typer.BadParameter(
        "instance id is required or set CODEX_ORCH_INSTANCE_ID"
    )


def _read_text_input(value: str | None, value_file: Path | None, *, field: str) -> str:
    if value is not None and value_file is not None:
        raise typer.BadParameter(f"{field} and {field}-file are mutually exclusive")
    if value_file is not None:
        return value_file.read_text(encoding="utf-8").strip()
    if value is not None:
        return value.strip()
    raise typer.BadParameter(f"{field} or {field}-file is required")


def _interrupt_record_payload(record: InterruptRecord) -> dict[str, object]:
    return {
        "run_id": record.run_id,
        "instance_id": record.instance_id,
        "task_id": record.task_id,
        "interrupt": record.interrupt.model_dump(mode="json"),
        "reply": None if record.reply is None else record.reply.model_dump(mode="json"),
    }


def _proposal_payload(record) -> dict[str, object]:
    return record.model_dump(mode="json")


def _print_validation_issue(issue: ProgramValidationIssue) -> None:
    typer.echo(
        f"{issue.severity.upper()}: [{issue.code}] {issue.location}: {issue.message}",
        err=True,
    )
    if issue.reference_url is not None:
        typer.echo(f"  reference: {issue.reference_url}", err=True)


def _emit_validation_warnings(report) -> None:
    for issue in report.warnings:
        _print_validation_issue(issue)


def _validation_warning_text(report) -> str | None:
    if not report.warnings:
        return None
    return " | ".join(issue.message for issue in report.warnings)


@project_app.command("init")
def project_init(
    program_dir: Path,
    name: str,
    workspace: Path,
    description: str = "",
    default_agent: str = "default",
    default_sandbox: str = "workspace-write",
    max_concurrency: int = 2,
) -> None:
    store = _store(program_dir)
    project = ProjectSpec(
        name=name,
        workspace=str(workspace.resolve()),
        description=description,
        default_agent=default_agent,
        default_sandbox=default_sandbox,
        max_concurrency=max_concurrency,
    )
    store.save_project(project)
    if not store.get_assistant_operating_model_path().exists():
        install_assistant_operating_model(store.paths.root, overwrite=False)
    typer.echo(f"Initialized program at {store.paths.root}")


@project_app.command("validate")
def project_validate(
    program_dir: Path,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    report = _task_pool(program_dir).validate_program()
    payload = {
        **report.to_dict(),
        "program_dir": str(program_dir.resolve()),
    }
    if as_json:
        _print_json(payload)
    else:
        for issue in report.errors:
            _print_validation_issue(issue)
        for issue in report.warnings:
            _print_validation_issue(issue)
        if report.ok:
            typer.echo(f"Validated program at {payload['program_dir']}")
    if report.errors:
        raise typer.Exit(code=2)
    if report.warnings:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@assistant_doc_app.command("install")
def assistant_doc_install(
    program_dir: Path,
    overwrite: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    installed_path = install_assistant_operating_model(
        program_dir,
        overwrite=overwrite,
    )
    payload = {"installed_path": str(installed_path)}
    if as_json:
        _print_json(payload)
        return
    typer.echo(str(installed_path))


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
        _print_json(task.model_dump(mode="json", by_alias=True))
        return
    typer.echo(yaml.safe_dump(task.model_dump(mode="json", by_alias=True), sort_keys=False))


@task_app.command("add")
def task_add(program_dir: Path, spec: Path) -> None:
    store = _store(program_dir)
    payload = yaml.safe_load(spec.read_text(encoding="utf-8"))
    task = TaskSpec.model_validate(payload)
    pool = TaskPoolService(store)
    pool.save_task(task)
    _emit_validation_warnings(pool.validate_program())
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
    pool = TaskPoolService(store)
    pool.save_task(task)
    if task.id != existing.id:
        store.delete_task(existing.id)
    _emit_validation_warnings(pool.validate_program())
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
            "scope": edge.scope,
            "consume": list(edge.consume),
        }
        for edge in edges
    ]
    if as_json:
        _print_json(payload)
        return
    for edge in payload:
        typer.echo(
            f"{edge['source']} -> {edge['target']} [{edge['kind']}] scope={edge['scope']} consume={edge['consume']}"
        )


@edge_app.command("add")
def edge_add(
    program_dir: Path,
    source: str,
    target: str,
    kind: DependencyKind,
    scope_alias: str | None = typer.Option(None, "--scope-alias"),
    consume: list[str] | None = typer.Option(None),
) -> None:
    store = _store(program_dir)
    store.add_edge(
        source_task_id=source,
        target_task_id=target,
        kind=kind,
        consume=[] if consume is None else consume,
        as_=scope_alias,
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
    input_json: list[str] | None = typer.Option(None, "--input-json"),
    input_json_file: list[str] | None = typer.Option(None, "--input-json-file"),
    wait: bool = True,
) -> None:
    report = _task_pool(program_dir).validate_program()
    if report.errors:
        raise typer.BadParameter(report.errors[0].message)
    _emit_validation_warnings(report)
    service = _run_service(program_dir)
    root_values = [] if root is None else root
    label_values = [] if label is None else label
    input_values = [] if input_value is None else input_value
    input_json_values = [] if input_json is None else input_json
    input_json_file_values = [] if input_json_file is None else input_json_file
    user_inputs = _parse_user_input_overrides(
        string_values=input_values,
        json_values=input_json_values,
        json_file_values=input_json_file_values,
    )
    if wait:
        run = asyncio.run(
            service.start_run(
                roots=root_values,
                labels=label_values,
                user_inputs=user_inputs,
            )
        )
    else:
        run = service.create_snapshot(
            roots=root_values,
            labels=label_values,
            user_inputs=user_inputs,
        )
    typer.echo(run.id)


@run_app.command("resume")
def run_resume(program_dir: Path, run_id: str) -> None:
    run = asyncio.run(_run_service(program_dir).resume_run(run_id))
    typer.echo(run.id)


@run_app.command("reconcile")
def run_reconcile(
    program_dir: Path,
    run_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run = asyncio.run(_run_service(program_dir).reconcile_run(run_id))
    if as_json:
        _print_json(run.model_dump(mode="json"))
        return
    typer.echo(run.id)


@run_app.command("abort")
def run_abort(
    program_dir: Path,
    run_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    run = asyncio.run(_run_service(program_dir).abort_run(run_id))
    if as_json:
        _print_json(run.model_dump(mode="json"))
        return
    typer.echo(run.id)


@run_app.command("list")
def run_list(program_dir: Path, as_json: bool = typer.Option(False, "--json")) -> None:
    runs = _store(program_dir).list_runs()
    if as_json:
        _print_json([run.model_dump(mode="json") for run in runs])
        return
    for run in runs:
        typer.echo(
            f"{run.id}\t{run.status.value}\troots={','.join(run.roots)}\tinstances={len(run.instances)}"
        )


@run_app.command("show")
def run_show(program_dir: Path, run_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    run = _store(program_dir).get_run(run_id)
    if as_json:
        _print_json(run.model_dump(mode="json"))
        return
    typer.echo(yaml.safe_dump(run.model_dump(mode="json"), sort_keys=False))


@interrupt_app.command("create")
def interrupt_create(
    program_dir: Path | None = typer.Option(None, "--program-dir"),
    run_id: str | None = typer.Option(None, "--run-id"),
    instance_id: str | None = typer.Option(None, "--instance-id"),
    task_id: str | None = typer.Option(None, "--task-id"),
    audience: InterruptAudience = typer.Option(..., "--audience"),
    kind: RequestKind = typer.Option(..., "--kind"),
    decision_kind: DecisionKind | None = typer.Option(None, "--decision-kind"),
    question: str | None = typer.Option(None, "--question"),
    question_file: Path | None = typer.Option(None, "--question-file"),
    target_role: str | None = typer.Option(None, "--target-role"),
    option: list[str] | None = typer.Option(None, "--option"),
    artifact: list[str] | None = typer.Option(None, "--artifact"),
    reply_schema: str | None = typer.Option(None, "--reply-schema"),
    priority: RequestPriority = typer.Option(RequestPriority.NORMAL, "--priority"),
    blocking: bool = typer.Option(True, "--blocking/--non-blocking"),
    metadata: list[str] | None = typer.Option(None, "--metadata"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    resolved_program_dir = _resolve_program_dir(program_dir)
    resolved_run_id = _resolve_run_id(run_id)
    resolved_instance_id = _resolve_instance_id(instance_id)
    resolved_task_id = _resolve_task_id(task_id)
    store = _store(resolved_program_dir)
    try:
        run = store.get_run(resolved_run_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if resolved_instance_id not in run.instances:
        raise typer.BadParameter(
            f"instance {resolved_instance_id} does not exist in run {resolved_run_id}"
        )
    instance = run.instances[resolved_instance_id]
    if instance.task_id != resolved_task_id:
        raise typer.BadParameter(
            f"task id {resolved_task_id} does not match instance task {instance.task_id}"
        )
    router = AssistantRoleRouter(store)
    requested_target_role_id: str | None = None
    recommended_target_role_id: str | None = None
    resolved_target_role_id: str | None = None
    target_resolution_reason: str | None = None
    try:
        if audience is InterruptAudience.ASSISTANT:
            recommendation, resolution = router.resolve_assistant_target(
                run_id=resolved_run_id,
                task_id=resolved_task_id,
                request_kind=kind,
                decision_kind=decision_kind,
                requested_target_role_id=target_role,
            )
            requested_target_role_id = resolution.requested_target_role_id
            recommended_target_role_id = recommendation.recommended_target_role_id
            resolved_target_role_id = resolution.resolved_target_role_id
            target_resolution_reason = resolution.target_resolution_reason
        else:
            if target_role is not None:
                raise typer.BadParameter("--target-role is only valid for assistant interrupts")
            router.validate_human_interrupt_allowed(
                run_id=resolved_run_id,
                task_id=resolved_task_id,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        interrupt = store.create_interrupt(
            run_id=resolved_run_id,
            instance_id=resolved_instance_id,
            audience=audience,
            blocking=blocking,
            request_kind=kind,
            question=_read_text_input(question, question_file, field="question"),
            decision_kind=decision_kind,
            options=[] if option is None else option,
            context_artifacts=[] if artifact is None else artifact,
            reply_schema=reply_schema,
            priority=priority,
            requested_target_role_id=requested_target_role_id,
            recommended_target_role_id=recommended_target_role_id,
            resolved_target_role_id=resolved_target_role_id,
            target_resolution_reason=target_resolution_reason,
            metadata={}
            if metadata is None
            else {key: value for key, value in _parse_key_values(metadata).items()},
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _print_json(interrupt.model_dump(mode="json"))
        return
    typer.echo(interrupt.interrupt_id)


@interrupt_app.command("recommend")
def interrupt_recommend(
    program_dir: Path | None = typer.Option(None, "--program-dir"),
    run_id: str | None = typer.Option(None, "--run-id"),
    task_id: str | None = typer.Option(None, "--task-id"),
    audience: InterruptAudience = typer.Option(..., "--audience"),
    kind: RequestKind = typer.Option(..., "--kind"),
    decision_kind: DecisionKind | None = typer.Option(None, "--decision-kind"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    resolved_program_dir = _resolve_program_dir(program_dir)
    resolved_run_id = _resolve_run_id(run_id)
    resolved_task_id = _resolve_task_id(task_id)
    router = _assistant_router(resolved_program_dir)
    if audience is InterruptAudience.HUMAN:
        allowed = True
        reason = None
        try:
            router.validate_human_interrupt_allowed(
                run_id=resolved_run_id,
                task_id=resolved_task_id,
            )
        except ValueError as exc:
            allowed = False
            reason = str(exc)
        payload = {
            "audience": audience.value,
            "request_kind": kind.value,
            "decision_kind": None if decision_kind is None else decision_kind.value,
            "allow_human": allowed,
            "reason": reason,
        }
        if as_json:
            _print_json(payload)
            return
        typer.echo(yaml.safe_dump(payload, sort_keys=False))
        return

    try:
        recommendation = router.recommend(
            run_id=resolved_run_id,
            task_id=resolved_task_id,
            request_kind=kind,
            decision_kind=decision_kind,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "audience": audience.value,
        "request_kind": kind.value,
        "decision_kind": None if decision_kind is None else decision_kind.value,
        "allowed_assistant_roles": list(recommendation.allowed_role_ids),
        "ranked_role_ids": list(recommendation.ranked_role_ids),
        "recommended_target_role_id": recommendation.recommended_target_role_id,
        "target_resolution_reason": recommendation.recommendation_reason,
        "allow_human": recommendation.allow_human,
    }
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@interrupt_app.command("list")
def interrupt_list(
    program_dir: Path,
    run_id: str | None = typer.Option(None, "--run-id"),
    audience: InterruptAudience | None = typer.Option(None, "--audience"),
    unresolved_only: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    records = _store(program_dir).list_interrupts(
        run_id=run_id,
        audience=audience,
        unresolved_only=unresolved_only,
    )
    if as_json:
        _print_json([_interrupt_record_payload(record) for record in records])
        return
    for record in records:
        typer.echo(
            f"{record.interrupt.interrupt_id}\trun={record.run_id}\tinstance={record.instance_id}\taudience={record.interrupt.audience.value}\tstatus={record.interrupt.status.value}"
        )


@interrupt_app.command("show")
def interrupt_show(
    program_dir: Path,
    interrupt_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    record = _store(program_dir).find_interrupt(interrupt_id)
    payload = _interrupt_record_payload(record)
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@inbox_app.command("list")
def inbox_list(
    program_dir: Path,
    audience: InterruptAudience | None = typer.Option(None, "--audience"),
    unresolved_only: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    interrupt_list(
        program_dir=program_dir,
        run_id=None,
        audience=audience,
        unresolved_only=unresolved_only,
        as_json=as_json,
    )


@inbox_app.command("show")
def inbox_show(
    program_dir: Path,
    interrupt_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    interrupt_show(program_dir=program_dir, interrupt_id=interrupt_id, as_json=as_json)


@inbox_app.command("reply")
def inbox_reply(
    program_dir: Path,
    interrupt_id: str,
    text: str | None = typer.Option(None, "--text"),
    text_file: Path | None = typer.Option(None, "--text-file"),
    reply_kind: InterruptReplyKind = typer.Option(
        InterruptReplyKind.ANSWER,
        "--reply-kind",
    ),
    rationale: str | None = typer.Option(None, "--rationale"),
    confidence: ConfidenceLevel | None = typer.Option(None, "--confidence"),
    citation: list[str] | None = typer.Option(None, "--citation"),
    payload_json: str | None = typer.Option(None, "--payload-json"),
    payload_file: Path | None = typer.Option(None, "--payload-file"),
    resume: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    store = _store(program_dir)
    router = AssistantRoleRouter(store)
    record = store.find_interrupt(interrupt_id)
    body = _read_text_input(text, text_file, field="text")
    if payload_json is not None and payload_file is not None:
        raise typer.BadParameter("--payload-json and --payload-file are mutually exclusive")
    reply_payload: dict[str, object] | None = None
    if payload_json is not None:
        try:
            parsed_payload = parse_json_input_override(
                payload_json,
                field_name="--payload-json",
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not isinstance(parsed_payload, dict):
            raise typer.BadParameter("--payload-json must be a JSON object")
        reply_payload = parsed_payload
    elif payload_file is not None:
        try:
            parsed_payload = parse_json_input_override(
                payload_file.read_text(encoding="utf-8"),
                field_name="--payload-file",
            )
        except OSError as exc:
            raise typer.BadParameter(
                f"--payload-file could not read {payload_file}: {exc}"
            ) from exc
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not isinstance(parsed_payload, dict):
            raise typer.BadParameter("--payload-file must contain a JSON object")
        reply_payload = parsed_payload
    if record.interrupt.audience is InterruptAudience.HUMAN and reply_kind is not InterruptReplyKind.ANSWER:
        raise typer.BadParameter("human inbox replies must use reply-kind=answer")
    try:
        reply = store.save_interrupt_reply(
            interrupt_id,
            audience=record.interrupt.audience,
            reply_kind=reply_kind,
            text=body,
            payload=reply_payload,
            rationale=rationale,
            confidence=confidence,
            citations=[] if citation is None else citation,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if (
        record.interrupt.audience is InterruptAudience.ASSISTANT
        and reply_kind is InterruptReplyKind.HANDOFF_TO_HUMAN
    ):
        try:
            router.validate_human_interrupt_allowed(
                run_id=record.run_id,
                task_id=record.task_id,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        store.create_interrupt(
            run_id=record.run_id,
            instance_id=record.instance_id,
            audience=InterruptAudience.HUMAN,
            blocking=record.interrupt.blocking,
            request_kind=record.interrupt.request_kind,
            question=record.interrupt.question,
            decision_kind=record.interrupt.decision_kind,
            options=list(record.interrupt.options),
            context_artifacts=list(record.interrupt.context_artifacts),
            reply_schema=record.interrupt.reply_schema,
            priority=record.interrupt.priority,
            requested_target_role_id=None,
            recommended_target_role_id=None,
            resolved_target_role_id=None,
            target_resolution_reason=None,
            metadata={
                "assistant_summary": body,
                "assistant_rationale": rationale or "",
                "assistant_citations": [] if citation is None else citation,
            },
        )
    if resume:
        asyncio.run(_run_service(program_dir).resume_run(record.run_id))
    if as_json:
        _print_json(reply.model_dump(mode="json"))
        return
    typer.echo(reply.interrupt_id)


@inbox_app.command("worker")
def inbox_worker(
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
            "skipped_no_role": stats.skipped_no_role,
            "failed": stats.failed,
        }
        if as_json:
            _print_json(payload)
            return
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items()))
        return
    worker.serve_forever(poll_interval_sec=poll_interval_sec)


@proposal_app.command("list")
def proposal_list(
    program_dir: Path,
    run_id: str | None = typer.Option(None, "--run-id"),
    status: AssistantUpdateStatus | None = typer.Option(None, "--status"),
    kind: AssistantUpdateKind | None = typer.Option(None, "--kind"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    records = _store(program_dir).list_proposals(run_id=run_id, status=status)
    if kind is not None:
        records = [record for record in records if record.proposal.kind is kind]
    if as_json:
        _print_json([_proposal_payload(record) for record in records])
        return
    for record in records:
        typer.echo(
            f"{record.proposal_id}\trun={record.run_id}\tstatus={record.status.value}\tkind={record.proposal.kind.value}\ttarget={record.target_file_path}"
        )


@proposal_app.command("show")
def proposal_show(
    program_dir: Path,
    proposal_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    record = _store(program_dir).find_proposal(proposal_id)
    payload = _proposal_payload(record)
    if as_json:
        _print_json(payload)
        return
    typer.echo(yaml.safe_dump(payload, sort_keys=False))


@proposal_app.command("mark")
def proposal_mark(
    program_dir: Path,
    proposal_id: str,
    status: AssistantUpdateStatus = typer.Option(..., "--status"),
    note: str | None = typer.Option(None, "--note"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    try:
        updated = _store(program_dir).mark_proposal_status(
            proposal_id,
            status=status,
            note=note,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        _print_json(updated.model_dump(mode="json"))
        return
    typer.echo(updated.proposal_id)


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
        typer.echo(f"{edge['source']} -> {edge['target']} [{edge['kind']}]")


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
