from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codex_orch.domain import (
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
)
from codex_orch.runner.base import NodeExecutionRequest, NodeExecutionResult


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class _RuntimeClockState:
    started_at_monotonic: float
    last_progress_at_monotonic: float


class _RuntimeTracker:
    def __init__(
        self,
        *,
        path: Path,
        cwd: Path,
        project_workspace_dir: Path,
        command: list[str],
        sandbox: str,
        writable_roots: tuple[Path, ...],
        wall_timeout_sec: float | None,
        idle_timeout_sec: float | None,
    ) -> None:
        sanitized_command = list(command)
        if sanitized_command:
            sanitized_command[-1] = "<prompt omitted; see prompt.md>"
        self._path = path
        self._lock = asyncio.Lock()
        now_monotonic = asyncio.get_running_loop().time()
        self._clock_state = _RuntimeClockState(
            started_at_monotonic=now_monotonic,
            last_progress_at_monotonic=now_monotonic,
        )
        self._runtime = NodeExecutionRuntime(
            cwd=str(cwd),
            project_workspace_dir=str(project_workspace_dir),
            command=sanitized_command,
            sandbox=sandbox,
            writable_roots=[str(root) for root in writable_roots],
            wall_timeout_sec=wall_timeout_sec,
            idle_timeout_sec=idle_timeout_sec,
        )

    async def set_pid(self, pid: int | None) -> None:
        async with self._lock:
            self._runtime.pid = pid
            self._write_unlocked()

    async def note_stdout(self, text: str) -> None:
        event_summary = _summarize_event(text)
        now_iso = _utc_now_iso()
        now_monotonic = asyncio.get_running_loop().time()
        async with self._lock:
            self._runtime.last_stdout_at = now_iso
            self._runtime.last_progress_at = now_iso
            self._runtime.stdout_line_count += 1
            if event_summary is not None:
                self._runtime.last_event_at = now_iso
                self._runtime.last_event_summary = event_summary
            self._clock_state = _RuntimeClockState(
                started_at_monotonic=self._clock_state.started_at_monotonic,
                last_progress_at_monotonic=now_monotonic,
            )
            self._write_unlocked()

    async def note_stderr(self, text: str) -> None:
        del text
        now_iso = _utc_now_iso()
        now_monotonic = asyncio.get_running_loop().time()
        async with self._lock:
            self._runtime.last_stderr_at = now_iso
            self._runtime.last_progress_at = now_iso
            self._runtime.stderr_line_count += 1
            self._clock_state = _RuntimeClockState(
                started_at_monotonic=self._clock_state.started_at_monotonic,
                last_progress_at_monotonic=now_monotonic,
            )
            self._write_unlocked()

    async def mark_termination_reason(
        self,
        reason: NodeExecutionTerminationReason,
    ) -> None:
        async with self._lock:
            self._runtime.termination_reason = reason
            self._write_unlocked()

    async def finish(
        self,
        *,
        return_code: int,
        termination_reason: NodeExecutionTerminationReason,
    ) -> None:
        async with self._lock:
            self._runtime.return_code = return_code
            self._runtime.finished_at = _utc_now_iso()
            self._runtime.termination_reason = termination_reason
            self._write_unlocked()

    async def clock_state(self) -> _RuntimeClockState:
        async with self._lock:
            return _RuntimeClockState(
                started_at_monotonic=self._clock_state.started_at_monotonic,
                last_progress_at_monotonic=self._clock_state.last_progress_at_monotonic,
            )

    def _write_unlocked(self) -> None:
        payload = json.dumps(
            self._runtime.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        self._path.write_text(payload + "\n", encoding="utf-8")


def _summarize_event(raw_line: str) -> str | None:
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return _truncate(raw_line.strip(), 160)

    event_type = payload.get("type")
    item = payload.get("item")
    if not isinstance(event_type, str):
        return None
    if not isinstance(item, dict):
        return event_type

    item_type = item.get("type")
    if not isinstance(item_type, str):
        return event_type
    status = item.get("status")
    if item_type == "command_execution":
        command = item.get("command")
        command_preview = command if isinstance(command, str) else ""
        status_text = status if isinstance(status, str) else "-"
        return f"{event_type}:{item_type}:{status_text}:{_truncate(command_preview, 120)}"
    if item_type == "agent_message":
        text = item.get("text")
        text_preview = text if isinstance(text, str) else ""
        return f"{event_type}:{item_type}:{_truncate(text_preview, 120)}"
    status_text = status if isinstance(status, str) else "-"
    return f"{event_type}:{item_type}:{status_text}"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    buffer = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        newline_index = buffer.find(b"\n")
        while newline_index >= 0:
            line = bytes(buffer[: newline_index + 1])
            del buffer[: newline_index + 1]
            yield line.decode("utf-8", errors="replace")
            newline_index = buffer.find(b"\n")
    if buffer:
        yield bytes(buffer).decode("utf-8", errors="replace")


class CodexExecRunner:
    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        request.instance_dir.mkdir(parents=True, exist_ok=True)
        request.attempt_dir.mkdir(parents=True, exist_ok=True)
        (request.instance_dir / "published").mkdir(parents=True, exist_ok=True)
        (request.attempt_dir / "scratch").mkdir(parents=True, exist_ok=True)
        (request.attempt_dir / "prompt.md").write_text(
            request.prompt,
            encoding="utf-8",
        )

        sandbox = self._sandbox(request)
        writable_roots = self._runtime_writable_roots(request, sandbox)
        command = self._build_command(request)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(request.workspace_dir),
                env=self._build_environment(request),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return NodeExecutionResult(
                success=False,
                return_code=127,
                final_message="",
                session_id=request.resume_session_id,
                error=str(exc),
            )

        events_path = request.attempt_dir / "events.jsonl"
        stderr_path = request.attempt_dir / "stderr.log"
        runtime_path = request.attempt_dir / "runtime.json"
        runtime_tracker = _RuntimeTracker(
            path=runtime_path,
            cwd=request.workspace_dir,
            project_workspace_dir=request.project_workspace_dir,
            command=command,
            sandbox=sandbox,
            writable_roots=writable_roots,
            wall_timeout_sec=request.project.node_wall_timeout_sec,
            idle_timeout_sec=request.project.node_idle_timeout_sec,
        )
        await runtime_tracker.set_pid(process.pid)
        stdin_task = asyncio.create_task(
            self._write_prompt_stdin(process, request.prompt)
        )
        stdout_task = asyncio.create_task(
            self._capture_stdout(process, events_path, runtime_tracker)
        )
        stderr_task = asyncio.create_task(
            self._capture_stderr(process, stderr_path, runtime_tracker)
        )
        watchdog_task = asyncio.create_task(
            self._watch_process(
                process,
                runtime_tracker,
                wall_timeout_sec=request.project.node_wall_timeout_sec,
                idle_timeout_sec=request.project.node_idle_timeout_sec,
                terminate_grace_sec=request.project.node_terminate_grace_sec,
            )
        )

        return_code = await process.wait()
        await stdin_task
        watchdog_reason = await watchdog_task
        final_message, session_id = await stdout_task
        stderr_output = await stderr_task
        termination_reason = self._resolve_termination_reason(
            return_code,
            watchdog_reason,
        )
        await runtime_tracker.finish(
            return_code=return_code,
            termination_reason=termination_reason,
        )

        resolved_session_id = session_id or request.resume_session_id
        if resolved_session_id is not None:
            (request.instance_dir / "session.json").write_text(
                json.dumps({"session_id": resolved_session_id}, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

        (request.attempt_dir / "final.md").write_text(final_message, encoding="utf-8")
        self._maybe_write_result_json(request, final_message)

        return NodeExecutionResult(
            success=termination_reason is NodeExecutionTerminationReason.COMPLETED,
            return_code=return_code,
            final_message=final_message,
            session_id=resolved_session_id,
            error=self._build_error(stderr_output, termination_reason),
            termination_reason=termination_reason,
        )

    def _build_command(self, request: NodeExecutionRequest) -> list[str]:
        if request.resume_session_id is not None:
            command = [
                "codex",
                "exec",
                "resume",
                request.resume_session_id,
                "--json",
                "--skip-git-repo-check",
            ]
            model = request.task.model or request.project.default_model
            if model is not None:
                command.extend(["--model", model])
            if self._sandbox(request) == "workspace-write":
                command.append("--full-auto")
            elif self._sandbox(request) == "danger-full-access":
                command.append("--dangerously-bypass-approvals-and-sandbox")
            command.append("-")
            return command

        sandbox = self._sandbox(request)
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(request.workspace_dir),
        ]
        if sandbox == "read-only":
            if request.extra_writable_roots:
                raise ValueError("extra writable roots require a writable sandbox")
            command.extend(["--sandbox", "read-only"])
        elif sandbox == "workspace-write":
            command.append("--full-auto")
            for writable_root in self._command_writable_roots(request):
                command.extend(["--add-dir", str(writable_root)])
        elif sandbox == "danger-full-access":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", sandbox])
            for writable_root in self._command_writable_roots(request):
                command.extend(["--add-dir", str(writable_root)])

        model = request.task.model or request.project.default_model
        if model is not None:
            command.extend(["--model", model])
        if request.task.result_schema is not None:
            schema_path = request.program_dir / request.task.result_schema
            command.extend(["--output-schema", str(schema_path)])
        command.append("-")
        return command

    def _build_environment(self, request: NodeExecutionRequest) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "CODEX_ORCH_PROGRAM_DIR": str(request.program_dir),
                "CODEX_ORCH_RUN_ID": request.run_id,
                "CODEX_ORCH_INSTANCE_ID": request.instance_id,
                "CODEX_ORCH_TASK_ID": request.task.id,
                "CODEX_ORCH_INSTANCE_DIR": str(request.instance_dir),
                "CODEX_ORCH_ATTEMPT_DIR": str(request.attempt_dir),
                "CODEX_ORCH_PROJECT_WORKSPACE_DIR": str(request.project_workspace_dir),
                "CODEX_ORCH_WORKSPACE_DIR": str(request.workspace_dir),
            }
        )
        return env

    def _sandbox(self, request: NodeExecutionRequest) -> str:
        return request.task.sandbox or request.project.default_sandbox

    def _command_writable_roots(self, request: NodeExecutionRequest) -> tuple[Path, ...]:
        candidates = [request.attempt_dir]
        candidates.extend(request.extra_writable_roots)
        deduped: list[Path] = []
        seen: set[str] = set()
        workspace_dir = str(request.workspace_dir)
        for path in candidates:
            normalized = str(path)
            if normalized == workspace_dir or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return tuple(deduped)

    def _runtime_writable_roots(
        self,
        request: NodeExecutionRequest,
        sandbox: str,
    ) -> tuple[Path, ...]:
        if sandbox in {"read-only", "danger-full-access"}:
            return tuple()
        return (request.workspace_dir, *self._command_writable_roots(request))

    async def _write_prompt_stdin(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
    ) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except BrokenPipeError:
                return

    async def _capture_stdout(
        self,
        process: asyncio.subprocess.Process,
        path: Path,
        runtime_tracker: _RuntimeTracker,
    ) -> tuple[str, str | None]:
        if process.stdout is None:
            return "", None

        final_message = ""
        session_id: str | None = None
        with path.open("w", encoding="utf-8") as handle:
            async for text in _iter_stream_lines(process.stdout):
                handle.write(text)
                handle.flush()
                await runtime_tracker.note_stdout(text)
                final_message = self._extract_agent_message(text, final_message)
                session_id = self._extract_session_id(text, session_id)
        return final_message, session_id

    async def _capture_stderr(
        self,
        process: asyncio.subprocess.Process,
        path: Path,
        runtime_tracker: _RuntimeTracker,
    ) -> str:
        if process.stderr is None:
            return ""

        chunks: list[str] = []
        with path.open("w", encoding="utf-8") as handle:
            async for text in _iter_stream_lines(process.stderr):
                handle.write(text)
                handle.flush()
                await runtime_tracker.note_stderr(text)
                chunks.append(text)
        return "".join(chunks)

    async def _watch_process(
        self,
        process: asyncio.subprocess.Process,
        runtime_tracker: _RuntimeTracker,
        *,
        wall_timeout_sec: float | None,
        idle_timeout_sec: float | None,
        terminate_grace_sec: float,
    ) -> NodeExecutionTerminationReason | None:
        if wall_timeout_sec is None and idle_timeout_sec is None:
            await process.wait()
            return None

        loop = asyncio.get_running_loop()
        while True:
            if process.returncode is not None:
                return None
            clock_state = await runtime_tracker.clock_state()
            now_monotonic = loop.time()
            if (
                wall_timeout_sec is not None
                and now_monotonic - clock_state.started_at_monotonic >= wall_timeout_sec
            ):
                await self._terminate_process(
                    process,
                    runtime_tracker,
                    reason=NodeExecutionTerminationReason.WALL_TIMEOUT,
                    terminate_grace_sec=terminate_grace_sec,
                )
                return NodeExecutionTerminationReason.WALL_TIMEOUT
            if (
                idle_timeout_sec is not None
                and now_monotonic - clock_state.last_progress_at_monotonic
                >= idle_timeout_sec
            ):
                await self._terminate_process(
                    process,
                    runtime_tracker,
                    reason=NodeExecutionTerminationReason.IDLE_TIMEOUT,
                    terminate_grace_sec=terminate_grace_sec,
                )
                return NodeExecutionTerminationReason.IDLE_TIMEOUT
            await asyncio.sleep(1)

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        runtime_tracker: _RuntimeTracker,
        *,
        reason: NodeExecutionTerminationReason,
        terminate_grace_sec: float,
    ) -> None:
        if process.returncode is not None:
            return
        await runtime_tracker.mark_termination_reason(reason)
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=terminate_grace_sec)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def _resolve_termination_reason(
        self,
        return_code: int,
        watchdog_reason: NodeExecutionTerminationReason | None,
    ) -> NodeExecutionTerminationReason:
        if watchdog_reason is not None:
            return watchdog_reason
        if return_code == 0:
            return NodeExecutionTerminationReason.COMPLETED
        if return_code < 0:
            return NodeExecutionTerminationReason.TERMINATED
        return NodeExecutionTerminationReason.NONZERO_EXIT

    def _build_error(
        self,
        stderr_output: str,
        termination_reason: NodeExecutionTerminationReason,
    ) -> str | None:
        if termination_reason is NodeExecutionTerminationReason.COMPLETED:
            return None
        if termination_reason is NodeExecutionTerminationReason.WALL_TIMEOUT:
            return "codex exec exceeded wall timeout"
        if termination_reason is NodeExecutionTerminationReason.IDLE_TIMEOUT:
            return "codex exec exceeded idle timeout"
        if termination_reason is NodeExecutionTerminationReason.TERMINATED:
            return stderr_output.strip() or "codex exec terminated"
        return stderr_output.strip() or "codex exec failed"

    def _extract_agent_message(self, raw_line: str, current: str) -> str:
        stripped = raw_line.strip()
        if not stripped:
            return current
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return current

        if payload.get("type") != "item.completed":
            return current
        item = payload.get("item")
        if not isinstance(item, dict):
            return current
        if item.get("type") != "agent_message":
            return current
        text = item.get("text")
        if isinstance(text, str):
            return text
        return current

    def _maybe_write_result_json(
        self,
        request: NodeExecutionRequest,
        final_message: str,
    ) -> None:
        if (
            request.task.kind.value != "controller"
            and request.task.result_schema is None
            and "result.json" not in request.task.publish
        ):
            return
        try:
            payload = json.loads(final_message)
        except json.JSONDecodeError:
            return
        result_path = request.attempt_dir / "result.json"
        result_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _extract_session_id(self, raw_line: str, current: str | None) -> str | None:
        stripped = raw_line.strip()
        if not stripped:
            return current
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return current
        extracted = self._find_session_id(payload)
        return extracted or current

    def _find_session_id(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in (
                "session_id",
                "sessionId",
                "conversation_id",
                "conversationId",
                "thread_id",
                "threadId",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            for value in payload.values():
                found = self._find_session_id(value)
                if found is not None:
                    return found
        if isinstance(payload, list):
            for value in payload:
                found = self._find_session_id(value)
                if found is not None:
                    return found
        return None
