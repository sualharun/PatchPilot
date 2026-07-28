from __future__ import annotations

import re
from pathlib import Path

from agent.application.dto import AgentRunResult, ExecuteRunCommand, ExecutionSettings
from agent.application.ports.outbound import (
    ArtifactWriter,
    IssueTrackerGateway,
    LLMGateway,
    ProjectEnvironmentDetector,
    RepositoryToolFactory,
    RepositoryTools,
    RepositoryWorkspace,
    RunRepository,
    SecretRedactor,
    WorkspaceManager,
)
from agent.application.services.tool_executor import ToolCallExecutor
from agent.domain.entities import IssueContext, RunState
from agent.domain.enums import RunStatus
from agent.domain.services import compute_run_metrics, normalize_max_iterations
from agent.domain.value_objects import CommandResult, IssueRef


class ExecuteEngineeringRunHandler:
    """Application service that coordinates one autonomous engineering run."""

    def __init__(
        self,
        *,
        settings: ExecutionSettings,
        github: IssueTrackerGateway,
        llm: LLMGateway,
        runs: RunRepository,
        workspaces: WorkspaceManager,
        tool_factory: RepositoryToolFactory,
        environments: ProjectEnvironmentDetector,
        artifacts: ArtifactWriter,
        redactor: SecretRedactor,
    ) -> None:
        self._settings = settings
        self._github = github
        self._llm = llm
        self._runs = runs
        self._workspaces = workspaces
        self._tool_factory = tool_factory
        self._environments = environments
        self._artifacts = artifacts
        self._redactor = redactor

    def execute(self, command: ExecuteRunCommand) -> AgentRunResult:
        issue = command.issue
        if not isinstance(issue, IssueRef):
            raise TypeError("ExecuteRunCommand.issue must be an IssueRef")
        issue_context = self._github.fetch_issue(issue)
        workspace = self._workspaces.clone(self._github.clone_url(issue), issue)
        settings = self._environments.resolve(workspace.path, self._settings)
        logs_path = self._logs_path(settings.logs_dir, issue, workspace)
        state = RunState()
        run_id = command.run_id or self._runs.start_run(
            {
                "issue_url": issue.url,
                "repo": issue.full_name,
                "branch": workspace.branch,
                "model": command.model,
                "started_at": "",
            }
        )
        if command.run_id is not None:
            self._runs.update_run(run_id, status=RunStatus.RUNNING.value, branch=workspace.branch, model=command.model)

        try:
            result = self._run_workspace(
                workspace=workspace,
                issue_context=issue_context,
                model=command.model,
                max_iterations=normalize_max_iterations(command.max_iterations),
                open_pr=command.open_pr,
                settings=settings,
                state=state,
                logs_path=logs_path,
            )
            self._runs.finish_run(
                run_id,
                result.status,
                iterations=state.iterations,
                commands=state.commands,
                tool_calls=state.tool_calls,
                patches=state.patches,
                test_results=state.test_results,
                token_usage=state.usage,
                estimated_cost_usd=state.usage.get("estimated_cost_usd"),
                pr_url=result.pr_url,
                summary=result.summary,
                logs_path=str(result.logs_path),
                leased_until=None,
                worker_id=None,
            )
            return result
        except Exception as exc:
            state.summary = f"Agent error: {exc}"
            self._runs.finish_run(
                run_id,
                RunStatus.AGENT_ERROR.value,
                iterations=state.iterations,
                commands=state.commands,
                tool_calls=state.tool_calls,
                patches=state.patches,
                test_results=state.test_results,
                token_usage=state.usage,
                estimated_cost_usd=state.usage.get("estimated_cost_usd"),
                leased_until=None,
                worker_id=None,
                last_error=self._redactor.redact(str(exc)),
            )
            raise
        finally:
            if not self._artifacts.exists(logs_path):
                self._write_artifact(
                    logs_path,
                    issue_context,
                    workspace,
                    command.model,
                    RunStatus.AGENT_ERROR.value,
                    state.summary,
                    state,
                    workspace.head_sha(),
                )

    def _run_workspace(
        self,
        *,
        workspace: RepositoryWorkspace,
        issue_context: IssueContext,
        model: str,
        max_iterations: int,
        open_pr: bool,
        settings: ExecutionSettings,
        state: RunState,
        logs_path: Path,
    ) -> AgentRunResult:
        tools = self._tool_factory.create(workspace.path, settings)
        tool_executor = ToolCallExecutor(tools, self._redactor)
        if not self._run_setup(tools, settings, state):
            state.summary = "Dependency installation failed before code edits."
            self._write_artifact(
                logs_path,
                issue_context,
                workspace,
                model,
                RunStatus.SETUP_FAILED.value,
                state.summary,
                state,
                workspace.head_sha(),
            )
            return self._result(
                RunStatus.SETUP_FAILED,
                state.summary,
                list(settings.test_commands),
                False,
                workspace,
                None,
                logs_path,
            )

        last_test_output = ""
        for iteration in range(1, max_iterations + 1):
            state.iterations = iteration
            prompt = self._build_prompt(issue_context, workspace.path, tools, last_test_output, iteration, max_iterations)
            if hasattr(self._llm, "propose_fix_with_tools"):
                decision = self._llm.propose_fix_with_tools(  # type: ignore[attr-defined]
                    model=model,
                    prompt=prompt,
                    tool_executor=tool_executor,
                )
            else:
                decision = self._llm.propose_fix(model=model, prompt=prompt)
            state.summary = decision.summary
            if decision.usage:
                state.usage = decision.usage
            state.tool_calls.extend(decision.tool_results or [])
            for call in decision.tool_calls or []:
                call_result = tool_executor.execute(call)
                state.tool_calls.append(tool_executor.as_log(call_result) | {"rationale": call.rationale})
                if not call_result.ok:
                    last_test_output = f"Tool call failed: {call_result.name}\n{call_result.output}"
                    break
            diff_after_tools = tools.git_diff()
            if diff_after_tools.strip():
                state.patches.append(self._redactor.redact(diff_after_tools))
            else:
                for patch in decision.patches:
                    tools.apply_patch(patch)
                    state.patches.append(self._redactor.redact(patch))

            tests_passed, last_test_output = self._run_tests(tools, settings, state)
            if tests_passed:
                return self._complete_success(
                    workspace,
                    issue_context,
                    model,
                    open_pr,
                    settings,
                    state,
                    logs_path,
                )

        summary = self._change_summary(workspace, state) or "Tests are still failing after all iterations."
        state.summary = summary
        self._write_artifact(
            logs_path,
            issue_context,
            workspace,
            model,
            RunStatus.FAILED_TESTS.value,
            summary,
            state,
            workspace.head_sha(),
        )
        return self._result(
            RunStatus.FAILED_TESTS,
            summary,
            list(settings.test_commands),
            False,
            workspace,
            None,
            logs_path,
        )

    def _run_setup(self, tools: RepositoryTools, settings: ExecutionSettings, state: RunState) -> bool:
        for command in settings.install_commands:
            result = tools.run_command_in_sandbox(command, timeout_seconds=settings.sandbox.install_timeout_seconds)
            self._record_command(state, result, phase="setup")
            if not result.ok:
                return False
        return True

    def _run_tests(
        self,
        tools: RepositoryTools,
        settings: ExecutionSettings,
        state: RunState,
    ) -> tuple[bool, str]:
        outputs: list[str] = []
        passed = True
        for command in settings.test_commands:
            result = tools.run_command_in_sandbox(command, timeout_seconds=settings.sandbox.test_timeout_seconds)
            self._record_command(state, result, phase="test")
            state.test_results.append(
                {
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "runtime_seconds": result.runtime_seconds,
                    "timed_out": result.timed_out,
                }
            )
            outputs.append(f"$ {command}\n{result.stdout}\n{result.stderr}")
            if not result.ok:
                passed = False
                break
        return passed, self._redactor.redact("\n\n".join(outputs))[-20_000:]

    def _complete_success(
        self,
        workspace: RepositoryWorkspace,
        issue_context: IssueContext,
        model: str,
        open_pr: bool,
        settings: ExecutionSettings,
        state: RunState,
        logs_path: Path,
    ) -> AgentRunResult:
        workspace.commit_all(f"Fix issue #{issue_context.ref.number}")
        pr_url = None
        status = RunStatus.SUCCESS
        if open_pr:
            workspace.push()
            pr_url = self._github.create_draft_pr(
                issue_context.ref,
                workspace.branch,
                f"Fix #{issue_context.ref.number}: {issue_context.title}",
                self._pr_body(issue_context, state, list(settings.test_commands)),
            )
            status = RunStatus.PR_OPENED
        summary = self._change_summary(workspace, state)
        state.summary = summary
        self._write_artifact(
            logs_path,
            issue_context,
            workspace,
            model,
            status.value,
            summary,
            state,
            workspace.head_sha(),
        )
        return self._result(status, summary, list(settings.test_commands), True, workspace, pr_url, logs_path)

    def _build_prompt(
        self,
        issue_context: IssueContext,
        repo_path: Path,
        tools: RepositoryTools,
        last_test_output: str,
        iteration: int,
        max_iterations: int,
    ) -> str:
        return (
            "You are editing a Python repository. Produce minimal unified git patches only.\n"
            f"Iteration {iteration} of {max_iterations}.\n\n"
            f"{_issue_prompt_context(issue_context)}\n\n"
            f"{self._environments.summarize(repo_path)}\n\n"
            f"Relevant file context:\n{self._relevant_file_context(issue_context, tools)}\n\n"
            f"Current git status:\n{tools.git_status() or '(clean)'}\n\n"
            f"Current diff:\n{tools.git_diff() or '(none)'}\n\n"
            f"Latest test output:\n{last_test_output or '(tests not run yet)'}\n"
        )

    def _relevant_file_context(self, issue_context: IssueContext, tools: RepositoryTools) -> str:
        issue_text = "\n".join([issue_context.title, issue_context.body, *issue_context.comments])
        files = set(tools.list_files())
        mentioned = {
            match for match in re.findall(r"[\w./-]+\.py", issue_text) if match in files and not match.startswith(".")
        }
        selected = list(sorted(mentioned))[:8]
        if not selected:
            selected = [path for path in tools.list_files() if path.endswith(".py") and path.startswith("tests/")][:4]
            selected.extend(path for path in tools.list_files() if path.endswith(".py") and not path.startswith("tests/"))
            selected = selected[:8]
        chunks = []
        for path in selected:
            try:
                chunks.append(f"--- {path} ---\n{tools.read_file(path, max_bytes=16_000)}")
            except UnicodeDecodeError:
                continue
        return "\n\n".join(chunks) if chunks else "(no Python files selected)"

    def _record_command(self, state: RunState, result: CommandResult, phase: str) -> None:
        state.commands.append(
            {
                "phase": phase,
                "command": result.command,
                "stdout": self._redactor.redact(result.stdout),
                "stderr": self._redactor.redact(result.stderr),
                "exit_code": result.exit_code,
                "runtime_seconds": result.runtime_seconds,
                "timed_out": result.timed_out,
            }
        )

    def _write_artifact(
        self,
        path: Path,
        issue_context: IssueContext,
        workspace: RepositoryWorkspace,
        model: str,
        status: str,
        summary: str,
        state: RunState,
        commit_sha: str | None,
    ) -> None:
        self._artifacts.write(
            path,
            {
                "schema_version": 1,
                "issue_url": issue_context.ref.url,
                "repo": issue_context.ref.full_name,
                "branch": workspace.branch,
                "model": model,
                "repo_path": str(workspace.path),
                "commit_sha": commit_sha,
                "status": status,
                "summary": summary,
                "iterations": state.iterations,
                "commands": state.commands,
                "tool_calls": state.tool_calls,
                "patches": state.patches,
                "test_results": state.test_results,
                "usage": state.usage,
                "metrics": compute_run_metrics(state.commands, state.patches, state.iterations, state.usage),
            },
        )

    def _change_summary(self, workspace: RepositoryWorkspace, state: RunState) -> str:
        status = workspace.status().strip()
        diff = workspace.diff().strip()
        if not status and not diff:
            return state.summary or "No code changes were produced."
        return (state.summary + "\n\n" if state.summary else "") + f"Git status:\n{status}\n\nDiff:\n{diff[:8000]}"

    def _pr_body(self, issue_context: IssueContext, state: RunState, test_commands: list[str]) -> str:
        tests = "\n".join(f"- `{command}`" for command in test_commands)
        return (
            f"Draft PR generated for {issue_context.ref.url}.\n\n"
            f"Summary:\n{state.summary}\n\n"
            f"Tests:\n{tests}"
        )

    def _logs_path(self, logs_dir: Path, issue: IssueRef, workspace: RepositoryWorkspace) -> Path:
        return logs_dir / f"issue-{issue.number}-{workspace.branch.replace('/', '-')}.json"

    def _result(
        self,
        status: RunStatus,
        summary: str,
        tests_run: list[str],
        tests_passed: bool,
        workspace: RepositoryWorkspace,
        pr_url: str | None,
        logs_path: Path,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status.value,
            summary=summary,
            tests_run=tests_run,
            tests_passed=tests_passed,
            branch=workspace.branch,
            pr_url=pr_url,
            logs_path=logs_path,
            repo_path=workspace.path,
        )


def _issue_prompt_context(context: IssueContext) -> str:
    comments = "\n\n".join(f"Comment {index + 1}:\n{comment}" for index, comment in enumerate(context.comments))
    labels = ", ".join(context.labels) if context.labels else "none"
    prs = ", ".join(context.linked_prs) if context.linked_prs else "none found"
    return (
        f"Issue: {context.ref.url}\nTitle: {context.title}\nLabels: {labels}\nLinked PRs: {prs}\n\n"
        f"Body:\n{context.body or '(empty)'}\n\nComments:\n{comments or '(none)'}"
    )
