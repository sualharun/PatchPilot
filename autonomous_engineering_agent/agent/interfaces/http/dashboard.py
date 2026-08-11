from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent.application.commands.dashboard import (
    AccountError,
    ChangePasswordCommand,
    CompleteOnboardingCommand,
    ConnectGitHubAccountCommand,
    CreateAccountCommand,
    RecordAuditCommand,
    SendVerificationEmailCommand,
    SignupError,
    UpdateEmailCommand,
    VerifyEmailCommand,
)
from agent.application.commands.handle_github_app_webhook import (
    GitHubAppWebhookCommand,
    GitHubAppWebhookSettings,
    HandleGitHubAppWebhookHandler,
)
from agent.application.commands.handle_pr_webhook import (
    EnqueuePullRequestAnalysisCommand,
    EnqueuePullRequestAnalysisHandler,
)
from agent.application.commands.queue_run import QueueRunCommand
from agent.application.queries.dashboard import calculate_run_stats
from agent.application.services.passwords import verify_password
from agent.bootstrap import build_application
from agent.domain.services import parse_issue_ref
from agent.infrastructure.clock import SystemClock
from agent.infrastructure.config.settings import load_config
from agent.infrastructure.kafka import KafkaPRJobProducer
from agent.infrastructure.llm.pricing import estimate_cost_usd
from agent.infrastructure.llm.verify import verify_provider_key
from agent.infrastructure.observability import init_sentry
from agent.infrastructure.security import RateLimiter, verify_github_signature
from agent.infrastructure.stripe import StripeClient, verify_stripe_signature

SESSION_COOKIE = "patchpilot_session"
SESSION_TTL_SECONDS = 60 * 60 * 12

# Per-process fixed-window limiters; see infrastructure/security/rate_limit.py for the trade-off.
_LOGIN_RATE_LIMITER = RateLimiter(max_requests=10, window_seconds=300)
_SIGNUP_RATE_LIMITER = RateLimiter(max_requests=5, window_seconds=3600)
_QUEUE_RUN_RATE_LIMITER = RateLimiter(max_requests=20, window_seconds=3600)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(limiter: RateLimiter, request: Request, *, detail: str) -> None:
    if not limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=detail)


def create_app(database_url: str | None = None) -> FastAPI:
    config = load_config()
    init_sentry(config.sentry_dsn, environment="production" if config.production else "development")
    container = build_application(database_url=database_url, config=config)
    queries = container.queries
    _seed_runtime_state(container, config)
    app = FastAPI(title="PatchPilot", docs_url=None, redoc_url=None)
    static_dir = Path(__file__).parents[2] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def require_dashboard_auth(request: Request, call_next):
        if _is_public_path(request.url.path) or _is_authenticated(request, config):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        queries.list_runs(limit=1)
        return {"status": "ready", "database": container.database_kind}

    @app.get("/api/runs")
    def list_runs(request: Request, limit: int = 50):
        workspace_id = _request_workspace_id(request, config, queries)
        return {"runs": _runs_for_display(queries, config, limit=limit, workspace_id=workspace_id)}

    @app.get("/api/stats")
    def stats(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        return _stats(_runs_for_display(queries, config, limit=250, workspace_id=workspace_id))

    @app.get("/api/audit-events")
    def audit_events(limit: int = 100):
        return {"events": _audit_events_for_display(queries, config, limit=limit)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        try:
            return queries.get_run(run_id)
        except KeyError:
            sample = next((run for run in _sample_runs() if run["id"] == run_id), None)
            if sample and config.dashboard_demo_data_enabled:
                return sample
            raise HTTPException(status_code=404, detail="run not found") from None

    @app.get("/api/artifacts")
    def list_artifacts(logs_dir: str = ".agent-logs"):
        return {"artifacts": queries.list_artifacts(logs_dir)}

    @app.get("/api/github/status")
    def github_status():
        connection = queries.github_connection()
        return {
            "oauth_configured": _github_oauth_enabled(config),
            "oauth_mock_enabled": config.github_oauth_mock_enabled,
            "app_install_url_configured": bool(config.github_app_install_url),
            "app_credentials_configured": _github_app_credentials_configured(config),
            "connected": bool(connection),
            "connection": connection,
        }

    @app.get("/api/github/repositories")
    def github_repositories():
        if not config.github_token:
            return {"repositories": [], "warning": "GITHUB_TOKEN is not configured for server-side verification"}
        return {"repositories": container.sync_github_repositories.execute()}

    @app.get("/api/github/verify")
    def github_verify(repo: str):
        if not config.github_token:
            raise HTTPException(status_code=400, detail="GITHUB_TOKEN is not configured")
        return container.verify_github_repository.execute(repo)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        if not config.github_webhook_secret:
            raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured")
        return await _handle_pull_request_webhook(
            request=request,
            secret=config.github_webhook_secret,
            config=config,
            audit_log=container.audit_log,
        )

    @app.post("/webhooks/github-app")
    async def github_app_webhook(request: Request):
        secret = config.github_app_webhook_secret or config.github_webhook_secret
        if not secret:
            raise HTTPException(status_code=500, detail="GITHUB_APP_WEBHOOK_SECRET is not configured")
        body = await request.body()
        if not verify_github_signature(
            secret=secret,
            body=body,
            signature_header=request.headers.get("X-Hub-Signature-256"),
        ):
            raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")
        event = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
        handler = HandleGitHubAppWebhookHandler(
            deliveries=container.webhook_deliveries,
            installations=container.github_app,
            queue_run=container.queue_run,
            pr_analysis=EnqueuePullRequestAnalysisHandler(
                KafkaPRJobProducer(config), container.audit_log, SystemClock()
            ),
            audit_log=container.audit_log,
            settings=GitHubAppWebhookSettings(
                default_model=config.default_model,
                open_pr=config.github_app_auto_open_pr,
                trigger_label=config.github_app_trigger_label,
                worker_max_attempts=config.worker_max_attempts,
            ),
            accounts=container.accounts,
            billing=container.billing,
        )
        try:
            result = handler.execute(
                GitHubAppWebhookCommand(event=event, delivery_id=delivery_id, payload=payload)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status_code = 200 if result.status == "duplicate" else 202
        return JSONResponse({"status": result.status, "event": event, **result.detail}, status_code=status_code)

    @app.post("/api/runs")
    def create_run(
        request: Request,
        issue: str = Form(...),
        model: str = Form(...),
        max_iterations: int = Form(5),
        open_pr: str = Form("false"),
        csrf_token: str = Form(""),
    ):
        _enforce_rate_limit(_QUEUE_RUN_RATE_LIMITER, request, detail="too many queued runs, try again later")
        _require_csrf(request, config, csrf_token)
        workspace_id = _request_workspace_id(request, config, queries)
        decision = container.billing.check_run_allowed(workspace_id)
        if not decision.allowed:
            return RedirectResponse(f"/billing?limit={_escape_attr(decision.reason)}", status_code=303)
        result = container.queue_run.execute(
            QueueRunCommand(
                issue=parse_issue_ref(issue),
                model=model,
                max_iterations=max_iterations,
                open_pr=open_pr == "true",
                requested_by=_current_user(request, config),
                workspace_id=workspace_id,
                max_attempts=config.worker_max_attempts,
            )
        )
        container.billing.record_run(workspace_id, result.run_id)
        return RedirectResponse(f"/runs/{result.run_id}", status_code=303)

    @app.post("/billing/checkout")
    def billing_checkout(request: Request, plan: str = Form(...), csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        if not config.stripe_secret_key:
            raise HTTPException(status_code=400, detail="STRIPE_SECRET_KEY is not configured")
        price_id = config.stripe_price_id_pro if plan == "pro" else config.stripe_price_id_starter
        if not price_id:
            raise HTTPException(status_code=400, detail=f"Stripe price for plan '{plan}' is not configured")
        workspace_id = _request_workspace_id(request, config, queries)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="no workspace for the current session")
        account = queries.account(_session_login(request, config))
        session = StripeClient(config.stripe_secret_key).create_checkout_session(
            price_id=price_id,
            workspace_id=workspace_id,
            customer_email=str((account.get("user") or {}).get("email") or "") or None,
            success_url=f"{config.public_base_url}/billing?checkout=success",
            cancel_url=f"{config.public_base_url}/billing?checkout=canceled",
        )
        return RedirectResponse(str(session["url"]), status_code=303)

    @app.post("/billing/portal")
    def billing_portal(request: Request, csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        if not config.stripe_secret_key:
            raise HTTPException(status_code=400, detail="STRIPE_SECRET_KEY is not configured")
        workspace_id = _request_workspace_id(request, config, queries)
        customer = container.billing.stripe_customer(workspace_id)
        if not customer:
            return RedirectResponse("/billing?portal=no_customer", status_code=303)
        session = StripeClient(config.stripe_secret_key).create_portal_session(
            customer_id=str(customer["stripe_customer_id"]),
            return_url=f"{config.public_base_url}/billing",
        )
        return RedirectResponse(str(session["url"]), status_code=303)

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        if not config.stripe_webhook_secret:
            raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET is not configured")
        body = await request.body()
        if not verify_stripe_signature(
            secret=config.stripe_webhook_secret,
            payload=body,
            signature_header=request.headers.get("Stripe-Signature"),
        ):
            raise HTTPException(status_code=401, detail="invalid Stripe webhook signature")
        try:
            event = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
        result = container.handle_stripe_webhook.execute(event)
        container.record_audit.execute(
            RecordAuditCommand(
                actor="stripe",
                event=f"billing.webhook.{event.get('type', 'unknown')}",
                target=f"workspace:{result.get('workspace_id', 'unknown')}",
                result=str(result.get("status", "processed")),
            )
        )
        return JSONResponse(result, status_code=200)

    @app.get("/", response_class=HTMLResponse)
    def hero():
        return _page(
            "PatchPilot",
            """
<main class="browser-page hero-browser">
  <section class="hero-reference">
    <div class="hero-left">
      <a class="logo-line" href="/"><span class="mark">P</span><strong>PatchPilot</strong></a>
      <div class="hero-headline">
        <h1>Autonomous<br>Python issue<br>resolution</h1>
        <p>Clone. Patch. Test. PR.</p>
        <div class="hero-actions">
          <a class="button dark" href="/runs"><span>〉_</span>View Runs</a>
          <a class="button outline" href="/demo"><span class="play-triangle"></span>Watch Demo</a>
        </div>
      </div>
      <div class="floor-grid" aria-hidden="true"></div>
    </div>
    <div class="hero-product">
      <aside class="product-sidebar">
        <div class="side-brand"><span class="mark mini">P</span><strong>PatchPilot</strong></div>
        <nav><a class="selected" href="/runs">◴ Runs</a><a href="/repositories">▱ Repositories</a><a href="/settings">⚙ Settings</a></nav>
        <div class="side-footer"><a href="/docs">▣ Docs</a><a href="/feedback">□ Feedback</a><a class="user-dot" href="/settings">Dashboard<br><small>local user</small></a></div>
      </aside>
      <section class="run-board">
        <div class="run-topbar"><a href="/runs">← Runs</a><strong>Run 8f3c2a7d</strong><em>○ Running</em><span>Started 2m 14s ago</span><a href="/github">Open in GitHub</a><a href="/runs/101">…</a></div>
        <div class="run-grid">
          <article class="issue-card span-2">
            <div class="repo-line"><span class="python-dot">●</span><strong>psf / requests</strong><em>Public</em></div>
            <h3>Issue #6448&nbsp; Fix proxy bypass for NO_PROXY with port</h3>
            <p>Requests should respect NO_PROXY when a port is included in the proxy URL.</p>
            <footer>◎ Python 3.12 &nbsp;&nbsp; ☆ 52.1k &nbsp;&nbsp; ⑂ 9.3k</footer>
          </article>
          <article class="meta-card"><span>Branch</span><strong>patchpilot/issue-6448</strong><span>Base</span><strong>main</strong><span>Commit</span><strong>a1b2c3d</strong></article>
          <article class="pr-card"><span>Draft PR</span><a href="/pull-requests">#12947</a><a href="/pull-requests" class="mini-button">Open</a><p>Ready for review</p><em>● 2/3 checks passing</em></article>
          <article class="timeline-card">
            <header><strong>Run timeline</strong><a href="/runs/101">View logs</a></header>
            <div class="timeline-row done"><strong>Clone repository</strong><span>00:04</span><small>Cloned psf/requests</small></div>
            <div class="timeline-row done"><strong>Understand issue</strong><span>00:18</span><small>Analyzed issue #6448 and related code</small></div>
            <div class="timeline-row active"><strong>Plan changes</strong><span>00:24</span><small>Identified proxy matching logic in utils.py</small></div>
            <div class="timeline-row focus"><strong>Implement patch</strong><span>01:02</span><small>Editing 2 files</small></div>
            <div class="timeline-row"><strong>Run tests (Docker)</strong><small>pytest -q</small></div>
            <div class="timeline-row"><strong>Validate & Lint</strong><small>ruff, mypy</small></div>
            <div class="timeline-row"><strong>Commit changes</strong><small>Create commit and push branch</small></div>
            <div class="timeline-row"><strong>Open Pull Request</strong><small>Create draft PR</small></div>
          </article>
          <article class="tests-card">
            <header><strong>Docker tests</strong><span>Running&nbsp; 18/28</span></header>
            <div class="progress"><i></i></div>
            <code>pytest -q</code>
            <p class="pass">● tests/test_no_proxy.py::test_no_proxy_without_port <span>0.21s</span></p>
            <p class="pass">● tests/test_no_proxy.py::test_no_proxy_with_port <span>0.18s</span></p>
            <p class="warn">● tests/test_no_proxy.py::test_no_proxy_with_auth <span>0.32s</span></p>
            <p class="idle">○ tests/test_proxies.py::test_http_proxy <span>–</span></p>
          </article>
          <article class="tools-card">
            <header><strong>LLM tool calls</strong></header>
            <p>● read_file <span>requests/utils.py (120-200)</span><time>00:28</time></p>
            <p>● search_code <span>NO_PROXY port handling</span><time>00:31</time></p>
            <p>● edit_file <span>requests/utils.py</span><time>00:43</time></p>
            <p>◉ edit_file <span>tests/test_no_proxy.py</span><time>00:55</time></p>
            <p>○ run_command <span>pytest -q</span><time>01:01</time></p>
          </article>
          <article class="pr-status-card">
            <header><strong>Draft PR status</strong></header>
            <p>Checks <b>2/3 passing</b></p><p>Conflicts <b>None</b></p><p>Reviewers <b>None requested</b></p><a href="/pull-requests">View PR ↗</a>
          </article>
        </div>
      </section>
    </div>
  </section>
</main>
""",
            nav_active="home",
            show_top_nav=False,
            body_class="home-exact",
        )

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next: str = "/runs"):
        error = request.query_params.get("error")
        if _oauth_login_enabled(config):
            github_login = '<a class="button primary full" href="/auth/github/start">Continue with GitHub</a>'
        elif config.dashboard_auth_mode != "password":
            github_login = '<p class="fine-print">GitHub OAuth is not configured. Set GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET, and GITHUB_OAUTH_CALLBACK_URL.</p>'
        else:
            github_login = ""
        password_login = (
            f"""<label>Email or username<input name="username" autocomplete="username" value="{_escape_attr(config.dashboard_username)}"></label>
    <label>Password<input name="password" type="password" autocomplete="current-password" placeholder="Dashboard password"></label>
    <button class="button primary full" type="submit">Continue</button>
    <a class="button secondary full" href="/signup">Create an account</a>"""
            if _password_login_enabled(config)
            else ""
        )
        return _page(
            "Sign in",
            f"""
<main class="auth-shell">
  <section class="auth-preview">
    <img src="/static/ui/login.png" alt="PatchPilot login UI mockup">
  </section>
  <form class="auth-panel" method="post" action="/login">
    <p class="eyebrow">Secure workspace</p>
    <h1>Sign in to PatchPilot</h1>
    <input type="hidden" name="next_path" value="{_escape_attr(next)}">
    <input type="hidden" name="csrf_token" value="{_csrf_token(request, config)}">
    {_login_error(error)}
    {'<p class="fine-print">Account created — sign in below.</p>' if request.query_params.get("created") else ""}
    {github_login}
    {password_login}
    <a class="button secondary full" href="/">Back to home</a>
    <p class="fine-print">Audit logs, scoped GitHub tokens, and Docker-isolated repo execution are built into every run.</p>
    <div class="link-row"><a href="/demo">Watch demo</a><a href="/docs">Docs</a><a href="/faq">FAQ</a><a href="/runs">Dashboard</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></div>
  </form>
</main>
""",
            nav_active="login",
        )

    @app.get("/signup", response_class=HTMLResponse)
    def signup(request: Request):
        if not _password_login_enabled(config):
            return RedirectResponse("/login", status_code=303)
        error = request.query_params.get("error")
        return _page(
            "Create account",
            f"""
<main class="auth-shell">
  <section class="auth-preview">
    <img src="/static/ui/login.png" alt="PatchPilot login UI mockup">
  </section>
  <form class="auth-panel" method="post" action="/signup">
    <p class="eyebrow">Secure workspace</p>
    <h1>Create your PatchPilot account</h1>
    <input type="hidden" name="csrf_token" value="{_csrf_token(request, config)}">
    {_signup_error(error)}
    <label>Email<input name="email" type="email" autocomplete="email" required></label>
    <label>Name<input name="name" autocomplete="name" placeholder="Optional"></label>
    <label>Password<input name="password" type="password" autocomplete="new-password" minlength="8" required></label>
    <label>Confirm password<input name="password_confirm" type="password" autocomplete="new-password" minlength="8" required></label>
    <button class="button primary full" type="submit">Create account</button>
    <a class="button secondary full" href="/login">Already have an account? Sign in</a>
    <p class="fine-print">A private workspace is created for you. Passwords are stored as salted PBKDF2 hashes.</p>
  </form>
</main>
""",
            nav_active="login",
        )

    @app.post("/signup")
    def signup_post(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        name: str = Form(""),
        csrf_token: str = Form(""),
    ):
        if not _password_login_enabled(config):
            return RedirectResponse("/login", status_code=303)
        _enforce_rate_limit(_SIGNUP_RATE_LIMITER, request, detail="too many signup attempts, try again later")
        _require_csrf(request, config, csrf_token)
        try:
            container.create_account.execute(
                CreateAccountCommand(email=email, password=password, password_confirm=password_confirm, name=name)
            )
        except SignupError as exc:
            return RedirectResponse(f"/signup?error={exc.code}", status_code=303)
        if config.dashboard_require_email_verification:
            # Soft verification: the account is usable immediately and the
            # dashboard nags until the link is clicked.
            container.send_verification_email.execute(
                SendVerificationEmailCommand(login=email.strip().lower(), base_url=config.public_base_url)
            )
        return RedirectResponse("/login?created=1", status_code=303)

    @app.get("/auth/github/start")
    def github_oauth_start(next: str = "/runs"):
        if not _oauth_login_enabled(config):
            return RedirectResponse("/login?error=github_oauth_disabled", status_code=303)
        query = urlencode(
            {
                "client_id": config.github_oauth_client_id,
                "redirect_uri": config.github_oauth_callback_url,
                "scope": "read:user user:email repo",
                "state": _sign_oauth_state(_safe_next_path(next), config),
            }
        )
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=303)

    @app.get("/auth/github/callback")
    def github_oauth_callback(code: str = "", state: str = ""):
        next_path = _verify_oauth_state(state, config) or "/runs"
        if not code or not _oauth_login_enabled(config):
            return RedirectResponse("/login?error=github_oauth_failed", status_code=303)
        if config.github_oauth_mock_enabled and code == "mock":
            access_token = "mock-oauth-token"
            token_payload = {"scope": "read:user user:email repo"}
            user_payload = {
                "id": 424242,
                "login": "mock-github-user",
                "name": "Mock GitHub User",
                "email": "mock-github-user@example.invalid",
                "avatar_url": "https://avatars.githubusercontent.com/u/424242",
            }
        else:
            token_response = requests.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": config.github_oauth_client_id,
                    "client_secret": config.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": config.github_oauth_callback_url,
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            access_token = token_payload.get("access_token")
            if not access_token:
                return RedirectResponse("/login?error=github_oauth_failed", status_code=303)
            user_response = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            user_response.raise_for_status()
            user_payload = user_response.json()
        login_name = str(user_payload.get("login") or "github-user")
        email = str(user_payload.get("email") or f"{login_name}@users.noreply.github.com")
        container.connect_github_account.execute(
            ConnectGitHubAccountCommand(
                login=login_name,
                email=email,
                token_hint=_secret_hint(access_token),
                scopes=str(token_payload.get("scope") or "read:user user:email repo"),
                github_user_id=str(user_payload["id"]) if user_payload.get("id") is not None else None,
                name=str(user_payload.get("name") or login_name),
                avatar_url=str(user_payload.get("avatar_url") or "") or None,
            )
        )
        response = RedirectResponse(
            _post_login_destination(container, queries, config, login_name, next_path), status_code=303
        )
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session(login_name, config),
            httponly=True,
            secure=config.dashboard_secure_cookies,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
        )
        return response

    @app.post("/login")
    def login_post(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next_path: str = Form("/runs"),
        csrf_token: str = Form(""),
    ):
        if not _password_login_enabled(config):
            return RedirectResponse("/login?error=invalid", status_code=303)
        _enforce_rate_limit(_LOGIN_RATE_LIMITER, request, detail="too many login attempts, try again later")
        _require_csrf(request, config, csrf_token)
        login_name = username if _valid_login(config, username, password) else _db_login(container, username, password)
        if not login_name:
            return RedirectResponse("/login?error=invalid", status_code=303)
        response = RedirectResponse(
            _post_login_destination(container, queries, config, login_name, next_path), status_code=303
        )
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session(login_name, config),
            httponly=True,
            secure=config.dashboard_secure_cookies,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
        )
        container.record_audit.execute(RecordAuditCommand(actor=login_name, event="auth.login", target="dashboard"))
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        container.record_audit.execute(
            RecordAuditCommand(actor=_current_user(request, config), event="auth.logout", target="dashboard")
        )
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        display_runs = _runs_for_display(queries, config, limit=50, workspace_id=workspace_id)
        showing_demo = _showing_demo_data(queries, config, workspace_id)
        stats_data = _stats(display_runs)
        total_runs = int(stats_data["total_runs"])
        repository_options = _run_filter_options(display_runs, "repo")
        model_options = _run_filter_options(display_runs, "model")
        return _page(
            "Runs",
            f"""
<main class="runs-exact-shell">
  <aside class="runs-rail">
    <a class="rail-brand" href="/overview"><span class="bot-mark">P</span><strong>PatchPilot</strong><small>Workspace</small></a>
    <nav>
      <a href="/overview"><span>⌂</span>Overview</a>
      <a class="active" href="/runs"><span>☷</span>Runs</a>
      <a href="/agents"><span>☵</span>Agents</a>
      <a href="/repositories"><span>⑂</span>Repositories</a>
      <a href="/issues"><span>ⓘ</span>Issues</a>
      <a href="/pull-requests"><span>⑂</span>Pull Requests</a>
      <a href="/tests"><span>✓</span>Tests</a>
      <a data-tour="settings" href="/settings"><span>⚙</span>Settings</a>
      <a href="/billing"><span>▭</span>Billing</a>
      <a href="/audit-log"><span>▤</span>Audit Log</a>
    </nav>
    <a class="connection-card rail-inline-card" href="/github"><span class="github-dot">◖</span><b>GitHub<small>Connection settings</small></b><i>⌄</i></a>
  </aside>
  <section class="runs-main">
    <header class="runs-head">
      <div><h1>Runs</h1><p>Autonomous engineering agent run history and metrics</p></div>
      <div class="head-controls">
        <a href="/github"><span class="github-dot small">◖</span>GitHub status ⌄</a>
        <a href="/settings#providers">Model: <strong>{_escape_attr(str(display_runs[0].get("model", "not set") if display_runs else "not set"))}</strong></a>
      </div>
    </header>
    {_page_tabs("runs")}
    {_demo_data_banner(showing_demo)}
    <section id="metrics" class="runs-metrics">
      <article class="metric-card metric-card-with-detail">
        <span>Runs</span>
        <strong>{stats_data["total_runs"]}</strong>
        <small>{'Queue your first issue from Overview' if total_runs == 0 else 'Persisted run history'}</small>
        {_run_recency_summary(display_runs)}
      </article>
      <article class="metric-card metric-card-with-ring">
        <span>Success rate</span>
        <strong>{stats_data["success_rate"]}%</strong>
        <small>{'No completed runs yet' if total_runs == 0 else 'success + PR statuses'}</small>
        <div class="metric-ring success-ring" style="{_success_ring_style(stats_data['success_rate'], total_runs)}"><b>{stats_data["success_rate"]}%</b></div>
      </article>
      <article class="metric-card metric-card-with-detail">
        <span>Median runtime</span>
        <strong>{_format_seconds(stats_data["median_runtime_seconds"])}</strong>
        <small>{'Waiting for Docker command data' if total_runs == 0 else 'From captured commands'}</small>
        {_runtime_range(display_runs)}
      </article>
      <article class="metric-card metric-card-with-detail">
        <span>Tracked cost</span>
        <strong>{_format_money(stats_data["cost_today_usd"])}</strong>
        <small>{'No provider usage recorded' if total_runs == 0 else 'From LLM usage'}</small>
        {_cost_summary(display_runs)}
      </article>
      <article id="failures" class="metric-card distribution">
        <span>Status distribution</span>
        <div class="metric-ring status-ring" style="{_status_ring_style(display_runs)}"></div>
        {_status_distribution(display_runs)}
        <footer>Total <b>{stats_data["total_runs"]}</b></footer>
      </article>
    </section>
    <section id="run-history" class="runs-filters" aria-label="Filter runs">
      <div id="runs-search-control" class="runs-search-control" role="search">
        <label class="sr-only" for="runs-search-field">Search field</label>
        <select id="runs-search-field" class="runs-search-field" aria-label="Search field">
          <option value="all">All fields</option>
          <option value="run">Run ID</option>
          <option value="repository">Repository</option>
          <option value="issue">Issue</option>
          <option value="branch">Branch</option>
        </select>
        <span class="runs-search-divider" aria-hidden="true"></span>
        <span class="runs-search-icon" aria-hidden="true">⌕</span>
        <label class="sr-only" for="runs-search">Search runs</label>
        <input id="runs-search" type="search" placeholder="Search all run fields" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="runs-search-suggestions">
        <button id="runs-search-clear" class="runs-search-clear" type="button" title="Clear search" aria-label="Clear search" hidden>×</button>
        <div id="runs-search-suggestions" class="runs-search-suggestions" role="listbox" aria-label="Matching runs" hidden></div>
      </div>
      <label class="runs-select" for="runs-repository"><span class="sr-only">Repository</span><select id="runs-repository"><option value="">All repositories</option>{repository_options}</select></label>
      <label class="runs-select" for="runs-status"><span class="sr-only">Status</span><select id="runs-status"><option value="">All statuses</option><option value="success">Successful</option><option value="running">Queued / running</option><option value="failed">Failed</option></select></label>
      <label class="runs-select" for="runs-model"><span class="sr-only">Model</span><select id="runs-model"><option value="">All models</option>{model_options}</select></label>
      <label class="runs-select" for="runs-range"><span class="sr-only">Time range</span><select id="runs-range"><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="all">All time</option></select></label>
      <button id="runs-reset" class="icon-button" type="button" title="Reset filters" aria-label="Reset run filters">↻</button>
    </section>
    <section class="runs-table-card">
      <table class="exact-runs-table">
        <thead><tr><th>Run</th><th>Repository</th><th>Issue</th><th>Branch</th><th>Model</th><th>Status</th><th>Tests</th><th>Runtime</th><th>Cost</th><th>PR</th></tr></thead>
        <tbody id="runs-table-body">{_run_rows(display_runs)}</tbody>
      </table>
    </section>
    <footer class="runs-pagination"><span id="runs-result-count" aria-live="polite">Showing {min(len(display_runs), 50)} run{'' if len(display_runs) == 1 else 's'}</span></footer>
  </section>
</main>
""",
            nav_active="runs",
            show_top_nav=False,
            body_class="runs-exact",
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int):
        try:
            run = queries.get_run(run_id)
            return _page(
                f"Run {run_id}",
                _db_run_detail_content(run),
                nav_active="runs",
                show_top_nav=False,
                body_class="app-dashboard",
            )
        except KeyError:
            if not config.dashboard_demo_data_enabled:
                return _page(
                    "Run not found",
                    _missing_run_content(run_id),
                    nav_active="runs",
                    show_top_nav=False,
                    body_class="app-dashboard",
                )
        return _page(
            f"Run {run_id}",
            """
<main class="detail-exact-shell">
  <aside class="detail-rail">
    <a class="rail-brand" href="/overview"><span class="bot-icon">P</span><strong>PatchPilot</strong><small>Workspace</small></a>
    <nav><a href="/overview">⌂ Dashboard</a><a class="active" href="/runs">⊕ Runs</a><a href="/issues">ⓘ Issues</a><a href="/repositories">▱ Repositories</a><a href="/agents">☞ Agents</a><a href="/settings">⚙ Settings</a><a href="/audit-log">▤ Audit Log</a></nav>
    <a class="connection-card rail-inline-card" href="/github"><span class="github-dot">◖</span><b>GitHub<small>Connection settings</small></b><i>⌄</i></a>
  </aside>
  <section class="detail-main">
    <header class="detail-top"><div><a href="/runs">Runs</a><span>/</span><strong>run_8f3c2a7d</strong></div><nav><a href="/runs/101">↻ Re-run</a><a href="/api/runs/101">⇩ Export</a><a href="/runs">⋮</a></nav></header>
    <div class="detail-title-row"><h1>Run detail <code>run_8f3c2a7d ⧉</code></h1><div><span class="complete-pill">✓ Completed</span><span>May 12, 2025 10:24 AM</span><span>◷ 8m 42s</span><span>Agent <b>gpt-4o</b></span></div></div>
    <div class="detail-layout">
      <section class="exact-timeline">
        <div class="step done"><i>✓</i><b>1</b><strong>Clone repository<small>acme/widgets</small></strong><time>00:08</time></div>
        <div class="step done"><i>✓</i><b>2</b><strong>Fetch issue<small>Issue #4821</small></strong><time>00:05</time></div>
        <div class="step done"><i>✓</i><b>3</b><strong>Inspect files<small>12 files scanned</small></strong><time>00:37</time></div>
        <div class="step done"><i>✓</i><b>4</b><strong>Plan<small>1 proposed change</small></strong><time>00:28</time></div>
        <div class="step done"><i>✓</i><b>5</b><strong>Apply patch<small>1 file modified</small></strong><time>00:19</time></div>
        <div class="step error"><i>!</i><b>6</b><strong>Run tests<small>pytest -q</small></strong><time>01:14</time></div>
        <div class="step done"><i>✓</i><b>7</b><strong>Analyze failure<small>1 failure found</small></strong><time>00:33</time></div>
        <div class="step retry"><i>↻</i><b>8</b><strong>Retry <em>(attempt 2)</em><small>Adjust patch</small></strong><time>00:55</time></div>
        <div class="step done"><i>✓</i><b>9</b><strong>Run tests<small>pytest -q</small></strong><time>00:48</time></div>
        <div class="step done"><i>✓</i><b>10</b><strong>Commit<small>chore: fix Docker tests in ...</small></strong><time>00:16</time></div>
        <div class="step done"><i>✓</i><b>11</b><strong>Draft PR<small>Create pull request</small></strong><time>00:29</time></div>
        <footer><span>Total time</span><strong>8m 42s</strong></footer>
      </section>
      <section class="detail-center">
        <article class="exact-panel issue-context"><header><h2>Issue context</h2><a href="/issues">⌁</a></header><a href="/issues">Issue #4821</a><a class="mini-button" href="/issues">Open</a><h3>Docker tests</h3><p>Docker-based tests are failing on CI due to service readiness timing. The tests assume the API is available immediately after container start, but it can take a few seconds.</p><div><span>repo: acme/widgets</span><span>label: test</span><span>priority: medium</span></div></article>
        <article class="exact-panel tool-log"><header><h2>LLM tool calls</h2><a href="/api/runs/101">View full log</a></header><table><thead><tr><th>Time</th><th>Tool</th><th>Input</th><th>Result</th></tr></thead><tbody><tr><td>10:24:31</td><td>filesystem.read</td><td>tests/test_docker.py</td><td>Success</td></tr><tr><td>10:24:32</td><td>filesystem.read</td><td>docker-compose.yml</td><td>Success</td></tr><tr><td>10:24:33</td><td>filesystem.grep</td><td>wait_for.*health</td><td>Success</td></tr><tr><td>10:24:35</td><td>thinking</td><td>Diagnose readiness issue</td><td>Success</td></tr><tr><td>10:24:37</td><td>filesystem.edit</td><td>tests/test_docker.py</td><td>Success</td></tr><tr><td>10:26:50</td><td>filesystem.edit</td><td>tests/test_docker.py</td><td>Success</td></tr></tbody></table></article>
        <article class="exact-panel command-output"><header><h2>Command output</h2></header><div class="cmd-head">$ pytest -q <a href="/api/runs/101">⧉ Copy</a></div><pre>===================== test session starts =====================
platform linux -- Python 3.11.8, pytest-8.2.2
rootdir: /workspace
collected 42 items

...............................F.............       [ 64%]
..............................................       [100%]

========================= FAILURES =========================
____________ test_healthcheck_eventual ____________
E   AssertionError: assert False
E     + where False = &lt;Response [503]&gt;.ok

-------------------- 1 failed, 41 passed in 12.34s --------------------</pre></article>
      </section>
      <section class="detail-right">
        <article class="exact-panel diff-view"><header><h2>git diff</h2><div><a href="/runs/101">Split</a><a href="/runs/101">Unified</a><a href="/api/runs/101">⧉</a></div></header><div class="file-name">tests/test_docker.py</div><pre><span>@@ -18,7 +18,16 @@ def wait_for_api(url: str, timeout: int = 5):</span>
 18      def wait_for_api(url: str, timeout: int = 5):
 19          \"\"\"Wait for the API to become available.\"\"\"
 20          deadline = time.time() + timeout
<b class="del">21  -     while time.time() &lt; deadline:</b>
<b class="del">    -         if requests.get(url).ok:</b>
<b class="add">21  +     delay = 0.5</b>
<b class="add">22  +     while time.time() &lt; deadline:</b>
<b class="add">23  +         try:</b>
<b class="add">24  +             resp = requests.get(url, timeout=2)</b>
<b class="add">25  +             if resp.ok:</b>
<b class="add">26  +                 return True</b>
<b class="add">27  +         except requests.RequestException:</b>
<b class="add">28  +             pass</b>
<b class="add">29  +         time.sleep(delay)</b></pre></article>
        <article class="exact-panel test-summary"><h2>Test result summary</h2><div class="test-row"><strong>✓ 42 passed</strong><span>0 failed&nbsp;&nbsp;&nbsp;0 skipped</span><aside><b>Duration</b>00:48</aside><aside><b>Tests</b>42</aside></div><div class="green-line"></div></article>
        <article class="exact-panel replay"><header><h2>Replay artifact</h2><a href="/api/artifacts">⇩ Download</a></header><p><span>Artifact ID</span><b>replay_8f3c2a7d_20250512_102431 ⧉</b></p><p><span>Size</span><b>24.7 MB</b></p><p><span>Created</span><b>May 12, 2025 10:24 AM</b></p><p><span>Includes</span><b>Inputs, tool calls, outputs, filesystem diff, logs</b></p></article>
        <article class="exact-panel pr-detail"><h2>PR status</h2><strong>↳ pr_opened</strong><p><a href="/pull-requests">#4825</a> chore: fix Docker tests in test_docker.py</p><a href="/github">Open on GitHub ↗</a><footer><span>Base<br><b>main</b></span><span>Head<br><b>patchpilot/run_8f3c2a7d</b></span><span>Checks<br><b>✓ All passing</b></span></footer></article>
      </section>
    </div>
  </section>
</main>
""",
            nav_active="runs",
            show_top_nav=False,
            body_class="detail-exact",
        )

    @app.get("/demo", response_class=HTMLResponse)
    def demo():
        return _page(
            "Workflow Demo",
            """
<main class="browser-page demo-browser">
  <header class="demo-nav">
    <a class="demo-brand" href="/"><span class="hex-mark">⌬</span><strong>PatchPilot</strong></a>
    <nav><a href="/docs">▱ Docs</a><a href="/security">▱ Security</a><a href="/github">◖ GitHub</a></nav>
  </header>
  <section class="demo-reference">
    <h1>Watch PatchPilot resolve an issue <span>◷ 5 min demo</span></h1>
    <div class="demo-content-grid">
      <section class="video-panel">
        <div class="video-app">
          <aside class="video-sidebar"><div class="cube">◇</div><a class="selected" href="/runs">▣ Runs</a><a href="/issues">◴ Issues</a><a href="/repositories">▤ Repositories</a><a href="/security">▧ Policies</a><a href="/settings">⚙ Settings</a></aside>
          <div class="video-main">
            <div class="run-video-head"><div><strong>Run #1287</strong><em>Completed</em><p>Triggered by webhook &nbsp; ⑂ main &nbsp; ↔ a1b2c3d</p></div><span>Started&nbsp; 2:14 PM</span><span>Duration&nbsp; ◷ 04:32</span></div>
            <div class="tabs"><b>Overview</b><span>Logs</span><span>Artifacts</span><span>Evaluation</span></div>
            <div class="overview-grid">
              <article><h3>Workflow</h3><p>● Clone repository <span>00:18</span></p><p>● Inspect issue <span>00:42</span></p><p>● Patch code <span>01:21</span></p><p>● Run Docker tests <span>01:02</span></p><p>● Open draft PR <span>00:41</span></p></article>
              <article><h3>Summary</h3><p>Files changed <b>3</b></p><p>Tests passed <b>24 / 24</b></p><p>Bench score <b>92.4 / 100</b></p><p>Token usage <b>18.7k</b></p><p>Estimated cost <b>$0.048</b></p></article>
            </div>
            <div class="logs-panel"><strong>Logs</strong><pre>14:17:03   INFO   Workflow completed successfully</pre></div>
          </div>
          <a class="large-play" href="/runs/101">▶</a>
          <div class="video-controls"><span>▶</span><strong>0:00 / 5:02</strong><i></i><span>CC</span><span>1x</span><span>⚙</span><span>⛶</span></div>
        </div>
        <div class="chapters"><span>Chapters</span><b>● 0:00 Clone repository</b><b>● 0:18 Inspect issue</b><b>● 1:00 Patch code</b><b>● 2:21 Run Docker tests</b><b>● 3:23 Open draft PR</b></div>
      </section>
      <aside class="workflow-panel">
        <h2>Workflow</h2>
        <div class="workflow-step"><span>↬</span><b>1</b><strong>Clone repository</strong><em>✓ Done</em><time>00:18⌄</time></div>
        <div class="workflow-step"><span>⌕</span><b>2</b><strong>Inspect issue</strong><em>✓ Done</em><time>00:42⌄</time></div>
        <div class="workflow-step"><span>&lt;/&gt;</span><b>3</b><strong>Patch code</strong><em>✓ Done</em><time>01:21⌄</time></div>
        <div class="workflow-step two-line"><span>▰</span><b>4</b><strong>Run Docker tests<small>24 / 24 tests passed</small></strong><em>✓ Done</em><time>01:02⌄</time></div>
        <div class="workflow-step two-line"><span>⑂</span><b>5</b><strong>Open draft PR<small>#1288 opened</small></strong><em>✓ Done</em><time>00:41⌄</time></div>
        <div class="score-strip"><div><span>Duration</span><strong>4:32</strong></div><div><span>Bench score</span><strong class="teal">92.4</strong> / 100</div><div><span>Token usage</span><strong>18.7k</strong></div></div>
        <div class="workflow-actions"><a class="button outline" href="/runs/101">▱ Replay artifact</a><a class="button teal" href="/agents#evaluations">▥ View benchmark report</a></div>
      </aside>
    </div>
    <div class="demo-info-grid">
      <article><span>▦</span><h2>Workflow</h2><p>PatchPilot autonomously analyzes the issue, modifies the code, validates with containerized tests, and opens a draft pull request.</p><a href="/overview">Learn more →</a></article>
      <article><span>⬡</span><h2>Artifacts</h2><p>Every run produces reproducible artifacts: patches, logs, test results, and PR metadata — ready for review and compliance.</p><a href="/runs">Explore runs →</a></article>
      <article><span>▥</span><h2>Evaluation</h2><p>Patch quality is measured with our benchmark suite to ensure correctness, performance, and minimal regressions.</p><a href="/agents#evaluations">View evaluation status →</a></article>
    </div>
  </section>
</main>
""",
            nav_active="demo",
            show_top_nav=False,
            body_class="demo-exact",
        )

    @app.get("/overview", response_class=HTMLResponse)
    def overview(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        stats_data = _stats(_runs_for_display(queries, config, limit=250, workspace_id=workspace_id))
        return _dashboard_page(
            "Overview",
            "Workspace health, recent autonomous fixes, and deployment readiness signals.",
            "overview",
            _overview_content(stats_data, _csrf_token(request, config)),
        )

    @app.get("/agents", response_class=HTMLResponse)
    def agents():
        return _dashboard_page(
            "Agents",
            "Configured model providers, tool policies, budgets, and sandbox profiles.",
            "agents",
            _agents_content(queries.provider_keys(), queries.eval_reports(limit=1), config),
        )

    @app.get("/repositories", response_class=HTMLResponse)
    def repositories():
        return _dashboard_page(
            "Repositories",
            "Connected Python repositories with setup detection, test commands, and agent.yaml overrides.",
            "repositories",
            _repositories_content(queries.repositories(limit=50)),
        )

    @app.get("/issues", response_class=HTMLResponse)
    def issues(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        return _dashboard_page(
            "Issues",
            "GitHub issues queued for autonomous triage, planning, patching, and PR creation.",
            "issues",
            _issues_content(queries.list_runs(limit=50, workspace_id=workspace_id)),
        )

    @app.get("/pull-requests", response_class=HTMLResponse)
    def pull_requests(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        return _dashboard_page(
            "Pull Requests",
            "Draft PRs opened by PatchPilot with summaries, checks, and reviewer handoff.",
            "pull-requests",
            _pull_requests_content(queries.list_runs(limit=50, workspace_id=workspace_id)),
        )

    @app.get("/tests", response_class=HTMLResponse)
    def tests_page(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        return _dashboard_page(
            "Tests",
            "Docker test runs, command output, retry attempts, and pass/fail history.",
            "tests",
            _tests_content(queries.test_overview(limit=100, workspace_id=workspace_id)),
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        account = queries.account(_session_login(request, config))
        return _dashboard_page(
            "Settings",
            "Workspace settings for GitHub tokens, provider keys, budgets, and safety controls.",
            "settings",
            _settings_content(
                account,
                queries.provider_keys(),
                config,
                _csrf_token(request, config),
                app_installations=container.github_app.list_installations(),
                key_test=_key_test_result(request),
            ),
        )

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        account = queries.account(_session_login(request, config))
        return _dashboard_page(
            "Account",
            "Your profile, GitHub identity, workspace membership, and session controls.",
            "settings",
            _account_content(
                account,
                _csrf_token(request, config),
                notice=_account_notice(request),
                verification_required=config.dashboard_require_email_verification,
            ),
        )

    @app.post("/account/password")
    def account_change_password(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(...),
        new_password_confirm: str = Form(...),
        csrf_token: str = Form(""),
    ):
        _enforce_rate_limit(_LOGIN_RATE_LIMITER, request, detail="too many attempts, try again later")
        _require_csrf(request, config, csrf_token)
        try:
            container.change_password.execute(
                ChangePasswordCommand(
                    login=_account_login(request, config),
                    current_password=current_password,
                    new_password=new_password,
                    new_password_confirm=new_password_confirm,
                )
            )
        except AccountError as exc:
            return RedirectResponse(f"/account?error={exc.code}", status_code=303)
        return RedirectResponse("/account?ok=password", status_code=303)

    @app.post("/account/email")
    def account_update_email(request: Request, email: str = Form(...), csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        login_name = _account_login(request, config)
        try:
            new_login = container.update_email.execute(UpdateEmailCommand(login=login_name, new_email=email))
        except AccountError as exc:
            return RedirectResponse(f"/account?error={exc.code}", status_code=303)
        response = RedirectResponse("/account?ok=email", status_code=303)
        if new_login != login_name:
            # Password accounts sign sessions with their email; re-issue the
            # cookie so the change does not log the user out.
            response.set_cookie(
                SESSION_COOKIE,
                _sign_session(new_login, config),
                httponly=True,
                secure=config.dashboard_secure_cookies,
                samesite="lax",
                max_age=SESSION_TTL_SECONDS,
            )
        return response

    @app.get("/onboarding", response_class=HTMLResponse)
    def onboarding(request: Request):
        account = queries.account(_session_login(request, config))
        checklist = _onboarding_checklist(queries, container, config, account)
        return _dashboard_page(
            "Get started",
            "A quick checklist to get PatchPilot fixing issues in your repositories.",
            "overview",
            _onboarding_content(account, checklist, _csrf_token(request, config)),
        )

    @app.post("/onboarding/skip")
    def onboarding_skip(request: Request, csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        container.complete_onboarding.execute(
            CompleteOnboardingCommand(login=_account_login(request, config), reason="skipped")
        )
        return RedirectResponse("/overview", status_code=303)

    @app.post("/onboarding/complete")
    def onboarding_complete(request: Request, csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        container.complete_onboarding.execute(
            CompleteOnboardingCommand(login=_account_login(request, config), reason="completed")
        )
        return RedirectResponse("/overview", status_code=303)

    @app.get("/github-app-setup", response_class=HTMLResponse)
    def github_app_setup(request: Request):
        installations = container.github_app.list_installations()
        return _dashboard_page(
            "GitHub App setup",
            "Install the PatchPilot GitHub App so labeled issues trigger runs automatically.",
            "github",
            _github_app_setup_content(installations, container, config, request),
        )

    @app.get("/github-app-setup/start")
    def github_app_setup_start(request: Request):
        if not config.github_app_install_url:
            return RedirectResponse("/github-app-setup?error=not_configured", status_code=303)
        query = urlencode({"state": _sign_oauth_state("/github-app-setup", config)})
        return RedirectResponse(f"{config.github_app_install_url}?{query}", status_code=303)

    @app.get("/github-app-setup/callback")
    def github_app_setup_callback(request: Request, installation_id: str = "", setup_action: str = ""):
        if not installation_id:
            return RedirectResponse("/github-app-setup?error=missing_installation", status_code=303)
        # Provisional record; the `installation` webhook overwrites it with
        # authoritative account data from GitHub.
        container.github_app.upsert_installation(
            installation_id=installation_id,
            account_login=_account_login(request, config) or "pending-webhook",
            status="active",
        )
        container.record_audit.execute(
            RecordAuditCommand(
                actor=_current_user(request, config),
                event="github_app.setup_completed",
                target="dashboard",
                metadata={"installation_id": installation_id, "setup_action": setup_action},
            )
        )
        return RedirectResponse("/github-app-setup?installed=1", status_code=303)

    @app.get("/billing", response_class=HTMLResponse)
    def billing(request: Request):
        workspace_id = _request_workspace_id(request, config, queries)
        return _dashboard_page(
            "Billing",
            "Token usage, provider costs, run budgets, and workspace billing controls.",
            "billing",
            _billing_content(
                queries.billing_overview(limit=500, workspace_id=workspace_id),
                plan_info=container.billing.overview(workspace_id),
                csrf_token=_csrf_token(request, config),
                stripe_enabled=bool(config.stripe_secret_key),
                limit_notice=request.query_params.get("limit"),
            ),
        )

    @app.get("/audit-log", response_class=HTMLResponse)
    def audit_log():
        events = _audit_events_for_display(queries, config, limit=100)
        return _dashboard_page(
            "Audit Log",
            "Immutable run events, tool calls, command logs, and security-relevant actions.",
            "audit-log",
            _audit_log_content(events),
        )

    @app.get("/github", response_class=HTMLResponse)
    def github():
        return _dashboard_page(
            "GitHub",
            "GitHub connection status, repository access, branch pushes, and draft PR permissions.",
            "github",
            _github_content(queries.github_connection(), queries.repositories(limit=20), config),
        )

    @app.get("/security", response_class=HTMLResponse)
    def security():
        return _dashboard_page(
            "Security",
            "Sandboxing, token redaction, command allowlists, Docker limits, and approval boundaries.",
            "security",
            _security_content(),
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback():
        return _dashboard_page(
            "Feedback",
            "Capture reviewer feedback and product requests from beta users.",
            "feedback",
            _feedback_content(),
        )

    @app.get("/docs", response_class=HTMLResponse)
    def docs():
        return _dashboard_page(
            "Docs",
            "Implementation docs, CLI examples, dashboard routes, and safety guidance.",
            "docs",
            _docs_content(),
        )

    @app.get("/verify-email")
    def verify_email(token: str = ""):
        verified = container.verify_email.execute(VerifyEmailCommand(token=token))
        return RedirectResponse(
            "/account?ok=verified" if verified else "/account?error=verification_invalid", status_code=303
        )

    @app.post("/account/resend-verification")
    def account_resend_verification(request: Request, csrf_token: str = Form("")):
        _enforce_rate_limit(_LOGIN_RATE_LIMITER, request, detail="too many attempts, try again later")
        _require_csrf(request, config, csrf_token)
        sent = container.send_verification_email.execute(
            SendVerificationEmailCommand(login=_account_login(request, config), base_url=config.public_base_url)
        )
        return RedirectResponse(
            "/account?ok=verification_sent" if sent else "/account?error=verification_unavailable",
            status_code=303,
        )

    @app.get("/faq", response_class=HTMLResponse)
    def faq():
        return _dashboard_page(
            "FAQ",
            "Common questions about requirements, cost, supported languages, and troubleshooting.",
            "docs",
            _faq_content(),
        )

    @app.post("/settings/test-provider-key")
    def settings_test_provider_key(request: Request, provider: str = Form(...), csrf_token: str = Form("")):
        _require_csrf(request, config, csrf_token)
        if provider not in ("openai", "anthropic"):
            return RedirectResponse("/settings?tested=unknown&status=unsupported#providers", status_code=303)
        api_key = config.openai_api_key if provider == "openai" else config.anthropic_api_key
        status = verify_provider_key(provider, api_key)
        container.record_audit.execute(
            RecordAuditCommand(
                actor=_current_user(request, config),
                event="settings.provider_key_tested",
                target=provider,
                result="success" if status == "ok" else "failure",
                metadata={"status": status},
            )
        )
        return RedirectResponse(f"/settings?tested={provider}&status={status}#providers", status_code=303)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return _page("Privacy Policy", _privacy_content(), nav_active="privacy")

    @app.get("/terms", response_class=HTMLResponse)
    def terms():
        return _page("Terms of Service", _terms_content(), nav_active="terms")

    return app


def serve_dashboard(host: str, port: int, database_url: str | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(database_url), host=host, port=port)


async def _handle_pull_request_webhook(request: Request, *, secret: str, config, audit_log) -> JSONResponse:
    body = await request.body()
    if not verify_github_signature(
        secret=secret,
        body=body,
        signature_header=request.headers.get("X-Hub-Signature-256"),
    ):
        raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")
    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if event != "pull_request":
        return JSONResponse({"status": "ignored", "event": event}, status_code=202)
    try:
        payload = json.loads(body.decode("utf-8"))
        job = EnqueuePullRequestAnalysisHandler(
            KafkaPRJobProducer(config),
            audit_log,
            SystemClock(),
        ).execute(EnqueuePullRequestAnalysisCommand(payload=payload, delivery_id=delivery_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job is None:
        return JSONResponse({"status": "ignored", "event": event}, status_code=202)
    return JSONResponse(
        {
            "status": "queued",
            "topic": config.kafka_pr_analysis_topic,
            "repo": job.full_name,
            "pr_number": job.pr_number,
        },
        status_code=202,
    )


def _page(title: str, body: str, *, nav_active: str, show_top_nav: bool = True, body_class: str = "") -> str:
    nav = (
        f"""
    <header class="top-nav">
      <a class="brand" href="/">PatchPilot</a>
      <nav>
        <a class="{_active(nav_active, 'home')}" href="/">Home</a>
        <a class="{_active(nav_active, 'runs')}" href="/runs">Runs</a>
        <a class="{_active(nav_active, 'demo')}" href="/demo">Demo</a>
        <a class="{_active(nav_active, 'login')}" href="/login">Login</a>
      </nav>
    </header>
"""
        if show_top_nav
        else ""
    )
    footer = (
        """
    <footer style="text-align:center;padding:24px;font-size:13px;opacity:0.7;">
      <a href="/faq" style="color:inherit;">FAQ</a> &middot; <a href="/privacy" style="color:inherit;">Privacy</a> &middot; <a href="/terms" style="color:inherit;">Terms</a>
    </footer>
"""
        if show_top_nav
        else ""
    )
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} · PatchPilot</title>
    <link rel="stylesheet" href="/static/app.css?v=2026071902">
    <style>{_inline_css()}</style>
  </head>
  <body class="{body_class}">
    {nav}
    {body}
    {footer}
    <script defer src="/static/app.js?v=2026071902"></script>
  </body>
</html>
"""


def _dashboard_page(title: str, description: str, nav_active: str, content: str) -> str:
    return _page(
        title,
        f"""
<main class="app-shell">
  <aside class="runs-rail">
    <a class="rail-brand" href="/overview"><span class="bot-mark">P</span><strong>PatchPilot</strong><small>Workspace</small></a>
    <nav>
      <a class="{_active(nav_active, 'overview')}" href="/overview"><span>⌂</span>Overview</a>
      <a class="{_active(nav_active, 'runs')}" href="/runs"><span>☷</span>Runs</a>
      <a class="{_active(nav_active, 'agents')}" href="/agents"><span>☵</span>Agents</a>
      <a class="{_active(nav_active, 'repositories')}" href="/repositories"><span>⑂</span>Repositories</a>
      <a class="{_active(nav_active, 'issues')}" href="/issues"><span>ⓘ</span>Issues</a>
      <a class="{_active(nav_active, 'pull-requests')}" href="/pull-requests"><span>⑂</span>Pull Requests</a>
      <a class="{_active(nav_active, 'tests')}" href="/tests"><span>✓</span>Tests</a>
      <a class="{_active(nav_active, 'settings')}" data-tour="settings" href="/settings"><span>⚙</span>Settings</a>
      <a class="{_active(nav_active, 'billing')}" href="/billing"><span>▭</span>Billing</a>
      <a class="{_active(nav_active, 'audit-log')}" href="/audit-log"><span>▤</span>Audit Log</a>
    </nav>
    <a class="connection-card rail-inline-card" href="/github"><span class="github-dot">◖</span><b>GitHub<small>Connection settings</small></b><i>⌄</i></a>
  </aside>
  <section class="app-main">
    <header class="app-head">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <nav>
        <a href="/overview#start-run"><span>+</span> Run agent</a>
        <a href="/demo"><span>▷</span> Watch demo</a>
      </nav>
    </header>
    {_page_tabs(nav_active)}
    {content}
  </section>
</main>
""",
        nav_active=nav_active,
        show_top_nav=False,
        body_class="app-dashboard",
    )


def _overview_content(stats: dict[str, float | int | str | None], csrf_token: str) -> str:
    healthy_repos = "0"
    repo_note = "Connect GitHub or queue a run"
    return f"""
<section id="workspace-metrics" class="app-metric-grid">
  <article><span>Healthy repos</span><strong>{healthy_repos}</strong><small>{repo_note}</small></article>
  <article><span>Total runs</span><strong>{stats["total_runs"]}</strong><small class="good">Persisted run history</small></article>
  <article><span>Success rate</span><strong>{stats["success_rate"]}%</strong><small>success + pr_opened</small></article>
  <article><span>Spend tracked</span><strong>${stats["cost_today_usd"]}</strong><small class="good">From token usage</small></article>
</section>
<section class="app-grid two">
  <article id="start-run" class="app-card settings-card">
    <header><h2>Start Agent Run</h2><a href="/docs">CLI docs</a></header>
    <form method="post" action="/api/runs" class="run-create-form">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <label>GitHub issue URL<input name="issue" placeholder="https://github.com/OWNER/REPO/issues/123" required></label>
      <label>Model<select name="model"><option>gpt-4o-mini</option><option>gpt-4.1-mini</option><option>gpt-4.1</option><option>claude-3-5-sonnet-latest</option></select></label>
      <label>Max iterations<input name="max_iterations" type="number" min="1" max="10" value="5"></label>
      <label>Open PR<select name="open_pr"><option value="false">No, queue local branch only</option><option value="true">Yes, open draft PR</option></select></label>
      <button class="button dark" type="submit">Queue run</button>
    </form>
  </article>
  <article id="readiness" class="app-card">
    <header><h2>Live Runs</h2><a href="/runs">View all</a></header>
    <div class="activity-list">
      <p><b>No live runs yet</b><span class="pill run">ready</span><small>Queue a GitHub issue to start the worker flow.</small></p>
    </div>
  </article>
  <article class="app-card">
    <header><h2>Readiness</h2><a href="/security">Safety model</a></header>
    <div class="readiness-list">
      <p><span>✓</span><b>Docker sandbox enforced</b><small>CPU, memory, and timeout limits enabled</small></p>
      <p><span>✓</span><b>Secrets redacted</b><small>Logs filter GitHub and model provider keys</small></p>
      <p><span>!</span><b>Provider budget alerts</b><small>Soft warning at 80% daily budget</small></p>
    </div>
  </article>
</section>
<section class="app-card">
  <header><h2>Recent Pull Requests</h2><a href="/pull-requests">Open PR queue</a></header>
  <table class="app-table"><thead><tr><th>PR</th><th>Repository</th><th>Status</th><th>Checks</th><th>Reviewer</th></tr></thead><tbody>
    <tr><td colspan="5">No draft PRs opened yet. Runs with <code>open_pr=true</code> will appear here.</td></tr>
  </tbody></table>
</section>
"""


def _agents_content(provider_rows: list[dict], evals: list[dict], config) -> str:
    providers = {key["provider"]: key for key in provider_rows}
    latest_eval = evals[0] if evals else None
    default_model = "Claude 3.5 Sonnet" if config.anthropic_api_key else "GPT-4.1" if config.openai_api_key else "not configured"
    eval_rate = f"{float(latest_eval['pass_rate']) * 100:.1f}%" if latest_eval else "no report"
    openai_status = "connected" if "openai" in providers else "missing"
    anthropic_status = "connected" if "anthropic" in providers else "missing"
    return f"""
<section id="providers" class="app-metric-grid">
  <article><span>Provider keys</span><strong>{len(providers)}</strong><small>OpenAI: {openai_status} · Anthropic: {anthropic_status}</small></article>
  <article><span>Default model</span><strong>{_escape_attr(default_model)}</strong><small>From configured provider keys</small></article>
  <article><span>Latest eval</span><strong>{_escape_attr(eval_rate)}</strong><small>{_escape_attr(str(latest_eval.get("name", "not run") if latest_eval else "run agent eval-synthetic"))}</small></article>
  <article><span>Queue policy</span><strong>{config.worker_max_attempts} attempts</strong><small>{config.worker_lease_seconds}s worker lease</small></article>
</section>
<section class="app-grid three">
  <article class="app-card agent-card"><h2>Issue Fixer</h2><p>Plans patches, edits code, and iterates on Docker test failures.</p><span class="pill ok">enabled</span><dl><dt>Model</dt><dd>{_escape_attr(default_model)}</dd><dt>Max iterations</dt><dd>configured per run</dd><dt>Tools</dt><dd>read, search, patch, sandbox</dd></dl></article>
  <article class="app-card agent-card"><h2>Worker</h2><p>Claims queued jobs with a database lease and retries failed claims.</p><span class="pill ok">enabled</span><dl><dt>Worker ID</dt><dd>{_escape_attr(config.worker_id)}</dd><dt>Lease</dt><dd>{config.worker_lease_seconds}s</dd><dt>Attempts</dt><dd>{config.worker_max_attempts}</dd></dl></article>
  <article id="evaluations" class="app-card agent-card"><h2>Evaluator</h2><p>Runs curated issue fixtures and tracks benchmark quality over time.</p><span class="pill run">on demand</span><dl><dt>Last report</dt><dd>{_escape_attr(eval_rate)}</dd><dt>Dataset</dt><dd>{_escape_attr(str(latest_eval.get("task_count", "20+") if latest_eval else "20 synthetic"))}</dd><dt>Command</dt><dd>agent eval-synthetic</dd></dl></article>
</section>
<section id="tool-policy" class="app-card">
  <header><h2>Tool Policy</h2><a href="/security">Review controls</a></header>
  <table class="app-table"><thead><tr><th>Tool</th><th>Permission</th><th>Scope</th><th>Logging</th></tr></thead><tbody>
    <tr><td>apply_patch</td><td>Allowed</td><td>Repo workspace</td><td>Full diff</td></tr>
    <tr><td>run_command</td><td>Allowlisted</td><td>Docker only</td><td>stdout, stderr, runtime</td></tr>
    <tr><td>open_pr</td><td>Flag gated</td><td>--open-pr true</td><td>PR URL + summary</td></tr>
  </tbody></table>
</section>
"""


def _repositories_content(repositories: list[dict]) -> str:
    rows = "\n".join(
        f"<tr><td>{_escape_attr(str(repo.get('full_name') or 'unknown'))}</td>"
        f"<td>{_escape_attr(str(repo.get('python_setup') or 'unknown'))}</td>"
        f"<td>{_escape_attr(str(repo.get('test_command') or 'not detected'))}</td>"
        f"<td><span class=\"pill ok\">{_escape_attr(str(repo.get('config_status') or 'detected'))}</span></td>"
        f"<td>{_escape_attr(str(repo.get('last_run_id') or 'none'))}</td></tr>"
        for repo in repositories
    ) or '<tr><td colspan="5">No repositories yet. Queue a run to populate this table.</td></tr>'
    return f"""
<section id="repositories" class="app-card">
  <header><h2>Connected Repositories</h2><a href="/github">Manage GitHub</a></header>
  <table class="app-table"><thead><tr><th>Repository</th><th>Python setup</th><th>Tests</th><th>Config</th><th>Last run</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
<section class="app-grid two">
  <article id="setup" class="app-card"><h2>Setup Detection</h2><div class="check-list"><p>✓ pyproject.toml</p><p>✓ requirements.txt</p><p>✓ setup.py</p><p>✓ Pipfile / poetry.lock</p></div></article>
  <article id="overrides" class="app-card"><h2>Recommended Overrides</h2><pre class="mini-code">install: pip install -e .[test]
test: pytest -q
timeout_seconds: 600
python_version: "3.11"</pre></article>
</section>
"""


def _issues_content(runs: list[dict]) -> str:
    rows = "\n".join(
        f"<tr><td>{_escape_attr(str(run.get('issue_url') or 'unknown'))}</td>"
        f"<td>{_escape_attr(str(run.get('repo') or 'unknown'))}</td>"
        f"<td>{_status_pill(str(run.get('status') or 'queued'))}</td>"
        f"<td>{_escape_attr(str(run.get('summary') or 'Awaiting agent analysis'))}</td>"
        f"<td><a href=\"/runs/{int(run.get('id', 0))}\">View run</a></td></tr>"
        for run in runs
    ) or '<tr><td colspan="5">No issues queued yet.</td></tr>'
    return f"""
<section id="issue-queue" class="app-card">
  <header><h2>Issue Queue</h2><a href="/runs">Start run</a></header>
  <table class="app-table"><thead><tr><th>Issue</th><th>Repository</th><th>Labels</th><th>Agent verdict</th><th>Action</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
<section id="triage" class="app-grid three">
  <article class="app-card"><h2>Triage Rules</h2><p>Python-only issues with reproducible tests are eligible for autonomous runs.</p></article>
  <article class="app-card"><h2>Linked PRs</h2><p>Issues with active human PRs are marked review-only to avoid duplicate work.</p></article>
  <article id="escalation" class="app-card"><h2>Escalation</h2><p>Security-sensitive labels require manual approval before code execution.</p></article>
</section>
"""


def _pull_requests_content(runs: list[dict]) -> str:
    def pr_cell(run: dict) -> str:
        if not run.get("pr_url"):
            return "not opened"
        return f'<a href="{_escape_attr(str(run["pr_url"]))}">Open</a>'

    rows = "\n".join(
        f"<tr><td>{pr_cell(run)}</td>"
        f"<td>{_escape_attr(str(run.get('branch') or 'not assigned'))}</td>"
        f"<td>{_escape_attr(str(run.get('summary') or 'No summary yet'))}</td>"
        f"<td>{_escape_attr(_test_summary(run))}</td>"
        f"<td>{_status_pill(str(run.get('status') or 'queued'))}</td></tr>"
        for run in runs
        if run.get("status") in {"success", "pr_opened", "failed_tests", "queued", "running"}
    ) or '<tr><td colspan="5">No PR-capable runs yet.</td></tr>'
    return f"""
<section id="pr-queue" class="app-card">
  <header><h2>Draft PR Queue</h2><a href="/github">Open GitHub settings</a></header>
  <table class="app-table"><thead><tr><th>PR</th><th>Branch</th><th>Summary</th><th>Checks</th><th>Status</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
<section class="app-grid two">
  <article class="app-card"><h2>PR Template</h2><pre class="mini-code">Summary
- Root cause
- Patch approach
- Tests run in Docker
- Residual risk</pre></article>
  <article id="handoff" class="app-card"><h2>Review Handoff</h2><p>PatchPilot leaves a concise reviewer note, full logs, replay artifact, and exact branch metadata.</p></article>
</section>
"""


def _tests_content(overview: dict) -> str:
    runs = overview["runs"]
    passed = overview["passing"]
    running = overview["running"]
    failed = overview["failed"]
    setup_failed = overview["setup_failed"]
    rows = "\n".join(
        f"<tr><td><a href=\"/runs/{int(run.get('id', 0))}\">{_escape_attr(_run_label(run))}</a></td>"
        f"<td>{_escape_attr(_last_test_command(run))}</td>"
        f"<td>{_status_pill(str(run.get('status') or 'queued'))}</td>"
        f"<td>{_format_seconds(sum(float(command.get('runtime_seconds') or 0) for command in (run.get('commands') or [])))}</td>"
        f"<td>{_escape_attr(str((run.get('commands') or [{}])[-1].get('image', 'configured image') if run.get('commands') else 'not run'))}</td></tr>"
        for run in runs[:25]
    ) or '<tr><td colspan="5">No test runs yet.</td></tr>'
    return f"""
<section id="test-metrics" class="app-metric-grid">
  <article><span>Passing</span><strong>{passed}</strong><small class="good">success + PR opened</small></article>
  <article><span>Running</span><strong>{running}</strong><small>queued or leased jobs</small></article>
  <article><span>Failed</span><strong>{failed}</strong><small>retry analysis exhausted</small></article>
  <article><span>Setup failed</span><strong>{setup_failed}</strong><small>install command review</small></article>
</section>
<section id="test-runs" class="app-card">
  <header><h2>Test Runs</h2><a href="/runs">Open latest detail</a></header>
  <table class="app-table"><thead><tr><th>Run</th><th>Command</th><th>Result</th><th>Runtime</th><th>Container</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
<section class="app-grid two">
  <article id="output" class="app-card"><h2>Command Output</h2><pre class="mini-code">$ pytest -q
No command output captured yet.
Queue a run to capture Docker stdout, stderr, exit code, and runtime.</pre></article>
  <article id="limits" class="app-card"><h2>Limits</h2><div class="check-list"><p>CPU: 2 cores</p><p>Memory: 2 GB</p><p>Install timeout: 600s</p><p>Test timeout: 900s</p></div></article>
</section>
"""


def _settings_content(
    account: dict,
    provider_rows: list[dict],
    config,
    csrf_token: str = "",
    app_installations: list[dict] | None = None,
    key_test: tuple[str, str] | None = None,
) -> str:
    github = account.get("github")
    user = account.get("user") or {}
    providers = {key["provider"]: key["key_hint"] for key in provider_rows}
    avatar = _avatar_html(user)
    active_installations = [row for row in (app_installations or []) if row.get("status") == "active"]
    if active_installations:
        first = active_installations[0]
        app_card = (
            f'<label>Installed for<input value="{_escape_attr(str(first.get("account_login") or "unknown"))}" readonly></label>'
            f'<label>Installations<input value="{len(active_installations)}" readonly></label>'
            f'<p><a href="/github-app-setup">View installation details</a></p>'
        )
        app_pill = '<span class="pill ok">installed</span>'
    else:
        app_card = (
            "<p>Not installed. New issues can only be queued manually until the app is set up.</p>"
            '<p><a class="button outline" href="/github-app-setup">Set up the GitHub App</a></p>'
        )
        app_pill = '<span class="pill run">not installed</span>'
    return f"""
<section class="app-grid two">
  <article id="account" class="app-card settings-card"><h2>Account</h2>{avatar}<label>User<input value="{_escape_attr(str(user.get('name')))}" readonly></label><label>Email<input value="{_escape_attr(str(user.get('email')))}" readonly></label><label>GitHub<input value="{_escape_attr(str((github or {}).get('login') or user.get('login') or 'not connected'))}" readonly></label><p><a href="/account">Open profile settings</a></p><form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{csrf_token}"><button class="button outline" type="submit">Sign out</button></form></article>
  <article id="providers" class="app-card settings-card">
    <h2>Provider Keys</h2>
    <p>PatchPilot needs an OpenAI or Anthropic key to generate patches. Keys are read from the server
    environment (<code>OPENAI_API_KEY</code>, <code>ANTHROPIC_API_KEY</code>) and never stored in the database —
    only the masked hint below is kept.</p>
    {_provider_key_notice(key_test)}
    <label>OpenAI API key<input value="{_escape_attr(providers.get('openai', 'not configured'))}" readonly></label>
    <p><a href="https://platform.openai.com/api-keys">Create an OpenAI key</a>
    <form method="post" action="/settings/test-provider-key"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="provider" value="openai"><button class="button outline" type="submit">Test OpenAI key</button></form></p>
    <label>Anthropic API key<input value="{_escape_attr(providers.get('anthropic', 'not configured'))}" readonly></label>
    <p><a href="https://console.anthropic.com/settings/keys">Create an Anthropic key</a>
    <form method="post" action="/settings/test-provider-key"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="provider" value="anthropic"><button class="button outline" type="submit">Test Anthropic key</button></form></p>
    <label>Artifact storage<input value="{_escape_attr(str(config.artifact_storage_dir))}" readonly></label>
    <h3>What a run costs</h3>
    <p class="fine-print">Estimates for a typical run (~50K input, ~8K output tokens). Actual cost depends on
    repository size and iteration count; every run records its own estimate.</p>
    <table class="app-table compact"><thead><tr><th>Model</th><th>Estimated cost</th></tr></thead><tbody>{_cost_example_rows()}</tbody></table>
  </article>
</section>
<section class="app-grid two">
  <article id="github-app" class="app-card settings-card"><header><h2>GitHub App</h2>{app_pill}</header>{app_card}</article>
</section>
<section class="app-grid two">
  <article id="run-policy" class="app-card settings-card"><h2>Run Policy</h2><label>Worker ID<input value="{_escape_attr(config.worker_id)}" readonly></label><label>Max attempts<input value="{config.worker_max_attempts}" readonly></label><label>Worker lease seconds<input value="{config.worker_lease_seconds}" readonly></label></article>
  <article class="app-card settings-card"><h2>Session Policy</h2><label>Auth enabled<input value="{config.dashboard_auth_enabled}" readonly></label><label>Secure cookies<input value="{config.dashboard_secure_cookies}" readonly></label><label>Demo data enabled<input value="{config.dashboard_demo_data_enabled}" readonly></label></article>
</section>
<section id="safety" class="app-card">
  <header><h2>Safety Controls</h2><a href="/security">Review security</a></header>
  <div class="check-list columns"><p>✓ Redact secrets in logs</p><p>✓ Docker-only execution</p><p>✓ Command allowlist</p><p>✓ PR creation gated</p><p>✓ Runtime limits</p><p>✓ Persist audit trail</p></div>
</section>
"""


_PROVIDER_KEY_TEST_MESSAGES = {
    "ok": ("fine-print", "key is valid and the provider accepted it."),
    "invalid": ("auth-error", "the provider rejected this key (401). Check for a typo or a revoked key."),
    "forbidden": (
        "auth-error",
        "the key authenticated but lacks permission (403). Check the project or organization it belongs to.",
    ),
    "rate_limited": (
        "fine-print",
        "the key is valid but the account is rate limited or out of credit (429). Add billing credit to run.",
    ),
    "unreachable": ("auth-error", "could not reach the provider. Check network egress from the server."),
    "not_configured": (
        "auth-error",
        "no key is set. Add it to the server environment and restart PatchPilot.",
    ),
    "unsupported": ("auth-error", "unknown provider."),
}


def _provider_key_notice(key_test: tuple[str, str] | None) -> str:
    if not key_test:
        return ""
    provider, status = key_test
    css_class, message = _PROVIDER_KEY_TEST_MESSAGES.get(status, _PROVIDER_KEY_TEST_MESSAGES["unsupported"])
    return f'<p class="{css_class}">{_escape_attr(provider)}: {message}</p>'


def _cost_example_rows() -> str:
    rows = []
    for model in ("gpt-4o-mini", "gpt-4.1", "claude-sonnet-4-5"):
        estimate = estimate_cost_usd(model, 50_000, 8_000)
        cost = estimate.get("estimated_cost_usd")
        label = f"${float(cost):.3f}" if cost is not None else "not priced"
        rows.append(f"<tr><td>{_escape_attr(model)}</td><td>{label}</td></tr>")
    return "".join(rows)


_FAQ_ITEMS = [
    (
        "What do I need to get started?",
        "A GitHub account, an OpenAI or Anthropic API key, and a repository with a test command. "
        "Docker must be available on the server so repository commands run in an isolated sandbox.",
    ),
    (
        "Is the GitHub App required?",
        "No. The app only adds automatic triggering — when an issue is labeled, a run starts by itself. "
        "You can queue every run manually from the Runs page instead. See /github-app-setup.",
    ),
    (
        "What does a run cost?",
        "You pay your LLM provider directly for tokens; PatchPilot adds no per-token markup. A typical run "
        "costs a few cents on a small model and up to a few tens of cents on a frontier model. Settings → "
        "Provider Keys shows current estimates, and every run records its own cost.",
    ),
    (
        "Which languages are supported?",
        "Python repositories are the best-supported path today: setup and test commands are detected "
        "automatically. Other languages work if you configure install and test commands in agent.yaml, "
        "since the agent edits files and runs your commands rather than parsing a specific language.",
    ),
    (
        "Can it work on private repositories?",
        "Yes. Grant the GitHub App (or your GitHub token) access to the private repositories you want covered. "
        "Code is cloned into a Docker sandbox with no outbound network during test execution and is removed "
        "when the run finishes.",
    ),
    (
        "Will it push code or open pull requests without asking?",
        "No. PatchPilot only pushes a branch and opens a draft PR when the run is started with the open-PR "
        "option enabled. Otherwise it reports the patch and test results and changes nothing on GitHub.",
    ),
    (
        "A run failed — how do I troubleshoot?",
        "Open the run's detail page: it lists every command with exit codes, the captured test output, the "
        "proposed patch, and the tool calls the agent made. Most failures are a missing dependency in the "
        "sandbox image or a test command that needs configuring for the repository.",
    ),
    (
        "How do I keep my API keys safe?",
        "Keys live in the server environment and are never written to the database — the dashboard stores only "
        "a masked hint. Logs are redacted before being persisted. See the Security page for the full model.",
    ),
]


def _faq_content() -> str:
    items = "".join(
        f'<article class="app-card"><h2>{_escape_attr(question)}</h2><p>{_escape_attr(answer)}</p></article>'
        for question, answer in _FAQ_ITEMS
    )
    return f"""
<section class="app-card">
  <h2>Frequently asked questions</h2>
  <p>New here? The step-by-step walkthrough lives in <code>docs/getting-started.md</code>, and the GitHub App
  guide in <code>docs/github-app-setup.md</code>.</p>
</section>
<section class="app-grid two">{items}</section>
<section class="app-card">
  <p>Still stuck? Open the <a href="/feedback">Feedback</a> page or check <a href="/docs">Docs</a> and
  <a href="/security">Security</a>.</p>
</section>
"""


def _avatar_html(user: dict) -> str:
    avatar_url = str(user.get("avatar_url") or "")
    if not avatar_url:
        return ""
    return (
        f'<img class="account-avatar" src="{_escape_attr(avatar_url)}" alt="GitHub avatar" '
        'width="64" height="64" style="border-radius:50%;margin-bottom:12px">'
    )


def _account_content(
    account: dict, csrf_token: str, *, notice: str = "", verification_required: bool = False
) -> str:
    user = account.get("user") or {}
    workspace = account.get("workspace") or {}
    github = account.get("github")
    connected = "connected" if github else "not connected"
    verification_banner = ""
    if verification_required and user.get("id") and not user.get("email_verified_at"):
        verification_banner = (
            '<section class="app-card"><p class="auth-error">Your email address is not verified yet. '
            "Check your inbox for the confirmation link — it expires 24 hours after it is sent.</p>"
            '<form method="post" action="/account/resend-verification">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
            '<button class="button outline" type="submit">Resend verification email</button></form></section>'
        )
    current_password_field = (
        '<label>Current password<input name="current_password" type="password" '
        'autocomplete="current-password" required></label>'
        if user.get("password_hash")
        else '<p class="fine-print">No password set yet — your session authorizes setting one.</p>'
    )
    return f"""
{notice}
{verification_banner}
<section class="app-grid two">
  <article id="profile" class="app-card settings-card">
    <h2>Profile</h2>
    {_avatar_html(user)}
    <label>Name<input value="{_escape_attr(str(user.get('name') or ''))}" readonly></label>
    <label>GitHub login<input value="{_escape_attr(str(user.get('login') or 'not connected'))}" readonly></label>
    <form method="post" action="/account/email">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      <label>Email<input name="email" type="email" value="{_escape_attr(str(user.get('email') or ''))}" required></label>
      <button class="button outline" type="submit">Update email</button>
    </form>
  </article>
  <article id="password" class="app-card settings-card">
    <h2>Password</h2>
    <form method="post" action="/account/password">
      <input type="hidden" name="csrf_token" value="{csrf_token}">
      {current_password_field}
      <label>New password<input name="new_password" type="password" autocomplete="new-password" minlength="8" required></label>
      <label>Confirm new password<input name="new_password_confirm" type="password" autocomplete="new-password" minlength="8" required></label>
      <button class="button outline" type="submit">Change password</button>
    </form>
  </article>
  <article id="workspace" class="app-card settings-card">
    <h2>Workspace</h2>
    <label>Workspace<input value="{_escape_attr(str(workspace.get('name') or 'PatchPilot'))}" readonly></label>
    <label>Slug<input value="{_escape_attr(str(workspace.get('slug') or 'default'))}" readonly></label>
    <label>GitHub connection<input value="{_escape_attr(connected)}" readonly></label>
    <form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{csrf_token}"><button class="button dark" type="submit">Sign out</button></form>
  </article>
</section>
"""


def _billing_content(
    overview: dict,
    *,
    plan_info: dict | None = None,
    csrf_token: str = "",
    stripe_enabled: bool = False,
    limit_notice: str | None = None,
) -> str:
    total_cost = overview["total_cost"]
    avg_cost = overview["average_cost"]
    by_model = overview["by_model"]
    plan_info = plan_info or {}
    plan = str(plan_info.get("plan") or "free")
    runs_used = int(plan_info.get("runs_this_month") or 0)
    run_cap = int(plan_info.get("monthly_run_cap") or 0)
    subscription_status = str(plan_info.get("subscription_status") or "none")
    notice = (
        f'<section class="app-card"><p class="auth-error">Run blocked: {_escape_attr(limit_notice)}. '
        "Upgrade your plan to queue more runs.</p></section>"
        if limit_notice
        else ""
    )
    upgrade_forms = (
        f"""
  <form method="post" action="/billing/checkout" style="display:inline"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="plan" value="starter"><button class="button outline" type="submit">Upgrade to Starter</button></form>
  <form method="post" action="/billing/checkout" style="display:inline"><input type="hidden" name="csrf_token" value="{csrf_token}"><input type="hidden" name="plan" value="pro"><button class="button dark" type="submit">Upgrade to Pro</button></form>
  <form method="post" action="/billing/portal" style="display:inline"><input type="hidden" name="csrf_token" value="{csrf_token}"><button class="button outline" type="submit">Manage subscription</button></form>
"""
        if stripe_enabled
        else '<p class="fine-print">Stripe is not configured. Set STRIPE_SECRET_KEY and price IDs to enable upgrades.</p>'
    )
    rows = "\n".join(
        f"<tr><td>{_escape_attr(model)}</td><td>{int(values['runs'])}</td><td>{int(values['tokens'])}</td>"
        f"<td>${values['cost']:.2f}</td><td>{(values['cost'] / total_cost * 100 if total_cost else 0):.1f}%</td></tr>"
        for model, values in sorted(by_model.items())
    ) or '<tr><td colspan="5">No cost data yet.</td></tr>'
    return f"""
{notice}
<section id="usage" class="app-metric-grid">
  <article><span>Plan</span><strong>{_escape_attr(plan)}</strong><small>subscription: {_escape_attr(subscription_status)}</small></article>
  <article><span>Runs this month</span><strong>{runs_used} / {run_cap}</strong><small>monthly run cap</small></article>
  <article><span>Total tracked</span><strong>${total_cost:.2f}</strong><small class="good">from persisted runs</small></article>
  <article><span>Avg/run</span><strong>${avg_cost:.2f}</strong><small>estimated provider cost</small></article>
</section>
<section id="plan" class="app-card">
  <header><h2>Subscription</h2></header>
  <p>Current plan: <strong>{_escape_attr(plan)}</strong> · {runs_used} of {run_cap} runs used this month.</p>
  {upgrade_forms}
</section>
<section id="models" class="app-card">
  <header><h2>Cost Breakdown by Model</h2></header>
  <table class="app-table"><thead><tr><th>Model</th><th>Runs</th><th>Tokens</th><th>Cost</th><th>Share</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
"""


def _audit_log_content(events: list[dict]) -> str:
    rows = "\n".join(
        f"<tr><td>{_escape_attr(str(event.get('created_at', ''))[11:19] or 'now')}</td>"
        f"<td>{_escape_attr(str(event.get('actor', 'agent')))}</td>"
        f"<td>{_escape_attr(str(event.get('event', 'event')))}</td>"
        f"<td>{_escape_attr(str(event.get('target', 'dashboard')))}</td>"
        f"<td><span class=\"pill ok\">{_escape_attr(str(event.get('result', 'success')))}</span></td></tr>"
        for event in events[:20]
    ) or '<tr><td colspan="5">No audit events yet. Login, queue a run, or receive a webhook to populate this log.</td></tr>'
    return f"""
<section id="events" class="app-card">
  <header><h2>Audit Events</h2></header>
  <table class="app-table"><thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Target</th><th>Result</th></tr></thead><tbody>
    {rows}
  </tbody></table>
</section>
<section class="app-grid two">
  <article class="app-card"><h2>Log Integrity</h2><p>Events include timestamps, tool names, sanitized inputs, results, command output references, and final run status.</p></article>
  <article class="app-card"><h2>Retention</h2><p>SQLite stores local dev runs. PostgreSQL is used in deployed environments for team history and analysis.</p></article>
</section>
"""


def _github_content(connection: dict | None, repos: list[dict], config) -> str:
    install_link = config.github_app_install_url or "https://github.com/settings/apps"
    connect_link = "/auth/github/start" if _github_oauth_enabled(config) else "/settings"
    status = "connected" if connection else "not connected"
    login = connection.get("login") if connection else "no GitHub user"
    scopes = connection.get("scopes") if connection else "configure OAuth or GitHub token"
    repo_rows = "\n".join(
        f"<tr><td>{_escape_attr(str(repo.get('full_name')))}</td><td><span class=\"pill ok\">{_escape_attr(str(repo.get('config_status') or 'detected'))}</span></td></tr>"
        for repo in repos
    ) or '<tr><td>No repositories yet</td><td><span class="pill run">queue a run</span></td></tr>'
    return f"""
<section class="app-grid two">
  <article id="connection" class="app-card"><header><h2>Connection</h2><span class="pill {'ok' if connection else 'run'}">{_escape_attr(status)}</span></header><p>Signed in as {_escape_attr(str(login))}. Scopes: {_escape_attr(str(scopes))}.</p><div class="check-list"><p>✓ Fetch issues and comments</p><p>✓ Clone repositories</p><p>✓ Push branches when enabled</p><p>✓ Open draft PRs only with --open-pr true</p></div><p><a href="{connect_link}">Connect GitHub OAuth</a> · <a href="{_escape_attr(install_link)}">Install GitHub App</a></p></article>
  <article id="repo-access" class="app-card"><h2>Repository Access</h2><table class="app-table compact"><tbody>{repo_rows}</tbody></table></article>
</section>
<section id="guardrail" class="app-card"><h2>PR Creation Guardrail</h2><p>PatchPilot never pushes or opens a PR unless the run is invoked with <code>--open-pr true</code>. Local dashboard links show simulated product flow unless credentials are configured.</p></section>
"""


_GITHUB_APP_SETUP_ERRORS = {
    "not_configured": (
        "GITHUB_APP_INSTALL_URL is not configured. Set it to your GitHub App's install page, "
        "e.g. https://github.com/apps/patchpilot/installations/new."
    ),
    "missing_installation": "GitHub did not return an installation id. Try installing again.",
}


def _github_app_setup_content(installations: list[dict], container, config, request: Request) -> str:
    notice = ""
    error = _GITHUB_APP_SETUP_ERRORS.get(request.query_params.get("error") or "")
    if error:
        notice = f'<section class="app-card"><p class="auth-error">{error}</p></section>'
    elif request.query_params.get("installed"):
        notice = (
            '<section class="app-card"><p class="fine-print">GitHub App installed. Repository details '
            "arrive with the first webhook delivery, usually within seconds.</p></section>"
        )
    active = [row for row in installations if row.get("status") == "active"]
    if active:
        rows = "".join(
            f"""<article class="app-card settings-card">
    <header><h2>@{_escape_attr(str(row.get('account_login') or 'unknown'))}</h2><span class="pill ok">{_escape_attr(str(row.get('status')))}</span></header>
    <label>Installation ID<input value="{_escape_attr(str(row.get('installation_id')))}" readonly></label>
    <label>Repositories<input value="{len(container.github_app.list_repositories(str(row.get('installation_id'))))}" readonly></label>
    <label>Installed<input value="{_escape_attr(str(row.get('installed_at') or 'unknown'))}" readonly></label>
    <p><a href="https://github.com/settings/installations/{_escape_attr(str(row.get('installation_id')))}">Manage or uninstall on GitHub</a></p>
  </article>"""
            for row in active
        )
        status_section = f'<section class="app-grid two">{rows}</section>'
        install_cta = '<a class="button outline" href="/github-app-setup/start">Install for another account</a>'
    else:
        status_section = ""
        install_cta = '<a class="button primary" href="/github-app-setup/start">Install on GitHub</a>'
    return f"""
{notice}
<section class="app-card">
  <h2>Why install the GitHub App?</h2>
  <p>Without the app, you queue every run by hand. With it, PatchPilot receives webhooks from your
  repositories and starts a run automatically when an issue is labeled
  <code>{_escape_attr(config.github_app_trigger_label)}</code>, posting the resulting draft PR back to the issue.</p>
  <div class="check-list columns">
    <p>✓ Contents: read &amp; write — clone code, push fix branches</p>
    <p>✓ Issues: read — receive issue events and comments</p>
    <p>✓ Pull requests: read &amp; write — open draft PRs</p>
    <p>✓ Metadata: read — list accessible repositories</p>
  </div>
  <p>{install_cta}</p>
  <p class="fine-print">Installation is recorded automatically via webhook — if you complete the install on
  GitHub but land back here signed out, nothing is lost. See docs/github-app-setup.md for the full guide.</p>
</section>
{status_section}
"""


def _security_content() -> str:
    return """
<section id="safety-model" class="app-metric-grid">
  <article><span>Secret leaks</span><strong>0</strong><small class="good">redaction active</small></article>
  <article><span>Sandbox</span><strong>Docker</strong><small>repo commands isolated</small></article>
  <article><span>Network</span><strong>Scoped</strong><small>GitHub + package install</small></article>
  <article><span>PR writes</span><strong>Gated</strong><small>open-pr flag required</small></article>
</section>
<section class="app-card">
  <header><h2>Safety Model</h2><a href="/docs">Read docs</a></header>
  <div class="security-grid">
    <p id="secrets"><b>Redaction</b><small>Tokens, API keys, and provider secrets are masked before logs persist.</small></p>
    <p id="sandbox"><b>Command allowlist</b><small>Only predictable setup/test commands run without confirmation.</small></p>
    <p><b>Docker cleanup</b><small>Containers are removed after each command, including timeout cases.</small></p>
    <p><b>Scoped paths</b><small>File tools reject paths outside the cloned repository workspace.</small></p>
  </div>
</section>
"""


def _feedback_content() -> str:
    return """
<section class="app-grid two">
  <article class="app-card settings-card"><h2>Send Feedback</h2><label>Category<select><option>Run quality</option><option>Dashboard UX</option><option>Security concern</option></select></label><label>Message<textarea readonly>Example: The agent should explain skipped tests more clearly.</textarea></label><a class="button dark" href="/runs">Attach latest run</a></article>
  <article class="app-card"><h2>Beta Signals</h2><div class="activity-list"><p><b>Top request</b><small>Provider-native tool calling</small></p><p><b>Next request</b><small>Cost controls per repository</small></p><p><b>Design note</b><small>Show replay artifacts directly in run detail</small></p></div></article>
</section>
"""


def _docs_content() -> str:
    return """
<section class="app-grid two">
  <article id="quickstart" class="app-card"><h2>CLI Quickstart</h2><pre class="mini-code">agent run \\
  --issue https://github.com/acme/api/issues/4821 \\
  --model claude-3-5-sonnet-latest \\
  --max-iterations 5 \\
  --open-pr false</pre></article>
  <article id="environment" class="app-card"><h2>Environment</h2><pre class="mini-code">GITHUB_TOKEN=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
DATABASE_URL=sqlite:///agent_runs.sqlite3</pre></article>
</section>
<section id="deploy" class="app-card">
  <header><h2>Deployment Checklist</h2><a href="/security">Security</a></header>
  <div class="check-list columns"><p>✓ Docker available</p><p>✓ PostgreSQL configured</p><p>✓ GitHub token scoped</p><p>✓ Provider key configured</p><p>✓ Logs directory writable</p><p>✓ Budget alerts enabled</p></div>
</section>
"""


_LEGAL_DOC_STYLE = """
<style>
  .legal-doc { max-width: 760px; margin: 0 auto; padding: 48px 24px 96px; line-height: 1.6; }
  .legal-doc h1 { font-size: 28px; margin: 0 0 4px; }
  .legal-doc .legal-updated { color: var(--muted, #777); margin: 0 0 24px; font-size: 14px; }
  .legal-doc .legal-notice { background: rgba(255, 176, 32, 0.12); border: 1px solid rgba(255, 176, 32, 0.4); border-radius: 8px; padding: 14px 16px; margin: 0 0 32px; font-size: 14px; }
  .legal-doc h2 { font-size: 18px; margin: 32px 0 10px; }
  .legal-doc p { margin: 0 0 12px; font-size: 15px; }
  .legal-doc ul { margin: 0 0 12px; padding-left: 22px; }
  .legal-doc li { margin: 0 0 6px; font-size: 15px; }
  .legal-doc a { color: inherit; }
</style>
"""


def _privacy_content() -> str:
    return f"""
{_LEGAL_DOC_STYLE}
<main class="legal-doc">
  <h1>Privacy Policy</h1>
  <p class="legal-updated">Last updated 2026-08-02</p>
  <p class="legal-notice"><strong>Draft, not legal advice.</strong> This page is a first draft written to be plain and accurate about what PatchPilot actually does today. It has not been reviewed by a lawyer. Have it checked before relying on it with real users.</p>

  <h2>What we store about you</h2>
  <p>When you sign in with GitHub OAuth, we store your GitHub user ID, login (username), and avatar URL, plus a signed session cookie used to keep you logged in. We do not receive or store your GitHub password.</p>

  <h2>What the GitHub App can access</h2>
  <p>The PatchPilot GitHub App only accesses repositories it has been explicitly installed on and granted access to -- installing it on one repository does not give it access to any others. Its permissions are scoped to what it needs to do its job: read issues, read repository metadata, write repository contents (to push a fix branch), and write pull requests (to open a draft PR).</p>

  <h2>What gets sent to a third-party LLM provider</h2>
  <p>To analyze an issue and propose a fix, PatchPilot sends relevant repository content (file contents, issue text, diffs, command output) to a third-party large language model provider -- OpenAI or Anthropic, depending on which model is configured -- for processing. That provider's own privacy policy and data-handling terms apply to what happens to that data on their side.</p>

  <h2>How long we keep run and log data</h2>
  <p>Run records (status, commands executed, test results, token usage, patches produced) and log/artifact files are retained so runs stay debuggable and auditable. As of this writing there is no automated expiry -- data is kept indefinitely unless manually deleted. If that changes, this page will be updated.</p>

  <h2>Billing</h2>
  <p>If billing is enabled, Stripe processes payments and handles your payment details directly -- PatchPilot does not receive or store your card number. We store the minimum billing metadata needed to associate your workspace with a Stripe customer and subscription (e.g. plan tier, subscription status).</p>

  <h2>Questions</h2>
  <p>This is an early private beta. If you have questions about data handling, contact the person who invited you.</p>
</main>
"""


def _terms_content() -> str:
    return f"""
{_LEGAL_DOC_STYLE}
<main class="legal-doc">
  <h1>Terms of Service</h1>
  <p class="legal-updated">Last updated 2026-08-02</p>
  <p class="legal-notice"><strong>Draft, not legal advice.</strong> This page is a first draft and has not been reviewed by a lawyer. Have it checked before relying on it with real users.</p>

  <h2>Beta software</h2>
  <p>PatchPilot is private-beta software. It autonomously edits code, runs commands in a sandboxed environment, and can open pull requests on repositories you connect. You are responsible for reviewing any pull request before merging it -- PatchPilot does not guarantee correctness.</p>

  <h2>Your responsibilities</h2>
  <ul>
    <li>Only connect repositories you have the right to grant PatchPilot access to.</li>
    <li>Review generated pull requests before merging; PatchPilot's output is not a substitute for code review.</li>
    <li>Keep any API keys and credentials you provide (GitHub, OpenAI, Anthropic, Stripe) confidential.</li>
  </ul>

  <h2>Third-party services</h2>
  <p>PatchPilot relies on third-party services -- GitHub, an LLM provider (OpenAI or Anthropic), and, if billing is enabled, Stripe -- to function. Your use of PatchPilot is also subject to those providers' own terms.</p>

  <h2>No warranty</h2>
  <p>PatchPilot is provided during a private beta on an "as is" basis, without warranty of any kind, express or implied.</p>

  <h2>Changes</h2>
  <p>These terms may change as the product changes during the beta. Material changes will be reflected on this page.</p>
</main>
"""


def _db_run_detail_content(run: dict) -> str:
    commands = run.get("commands") or []
    tool_calls = run.get("tool_calls") or []
    patches = run.get("patches") or []
    test_results = run.get("test_results") or []
    status = str(run.get("status") or "unknown")
    summary = str(run.get("summary") or _status_empty_summary(status))
    repo = str(run.get("repo") or "unknown")
    branch = str(run.get("branch") or "no branch")
    logs_href = str(run.get("logs_path") or "/api/artifacts")
    command_rows = "\n".join(
        f"<tr><td>{_escape_attr(str(command.get('phase') or 'command'))}</td>"
        f"<td>{_escape_attr(str(command.get('command') or command.get('args') or ''))}</td>"
        f"<td>{_escape_attr(str(command.get('exit_code', '')))}</td>"
        f"<td>{_format_seconds(command.get('runtime_seconds') or 0)}</td></tr>"
        for command in commands
    ) or '<tr><td colspan="4">No commands captured yet.</td></tr>'
    tool_rows = "\n".join(
        f"<tr><td>{_escape_attr(str(call.get('name') or call.get('tool') or 'tool'))}</td>"
        f"<td>{_escape_attr(str(call.get('rationale') or call.get('input') or ''))}</td>"
        f"<td>{_escape_attr(str(call.get('ok', call.get('result', 'recorded'))))}</td></tr>"
        for call in tool_calls
    ) or '<tr><td colspan="3">No tool calls captured yet.</td></tr>'
    patch_text = "\n\n".join(str(patch) for patch in patches) or _patch_empty_summary(status)
    return f"""
<main class="app-shell">
  <aside class="runs-rail">
    <a class="rail-brand" href="/overview"><span class="bot-mark">P</span><strong>PatchPilot</strong><small>Workspace</small></a>
    <nav><a href="/overview"><span>⌂</span>Overview</a><a class="active" href="/runs"><span>☷</span>Runs</a><a href="/issues"><span>ⓘ</span>Issues</a><a href="/tests"><span>✓</span>Tests</a><a href="/settings"><span>⚙</span>Settings</a></nav>
  </aside>
  <section class="app-main">
    <header class="app-head"><div><h1>Run {_escape_attr(_run_label(run))}</h1><p>{_escape_attr(str(run.get('issue_url') or 'unknown issue'))}</p></div><nav><a href="/runs">Back to runs</a><a href="/api/runs/{int(run.get('id', 0))}">Export JSON</a></nav></header>
    <nav class="platform-tabs" aria-label="Run detail sections">
      <a class="active" href="/runs/{int(run.get('id', 0))}">Overview</a>
      <a href="#commands">Logs</a>
      <a href="#patch">Artifacts</a>
      <a href="/tests">Evaluation</a>
    </nav>
    <section class="app-metric-grid run-detail-metrics">
      <article><span>Status</span><strong class="metric-status">{_status_pill(status)}</strong><small>{_escape_attr(summary)}</small></article>
      <article><span>Repository</span><strong class="repo-name">{_escape_attr(repo)}</strong><small>{_escape_attr(branch)}</small></article>
      <article><span>Tests</span><strong>{_escape_attr(_test_summary(run))}</strong><small>{len(test_results)} recorded result(s)</small></article>
      <article><span>Cost</span><strong>{_format_money(run.get('estimated_cost_usd') or 0)}</strong><small>{_escape_attr(str(run.get('model') or 'not set'))}</small></article>
    </section>
    <section class="app-grid two">
      <article id="commands" class="app-card run-table-card"><header><h2>Commands</h2><a href="{_escape_attr(logs_href)}">Logs</a></header><table class="app-table"><thead><tr><th>Phase</th><th>Command</th><th>Exit</th><th>Runtime</th></tr></thead><tbody>{command_rows}</tbody></table></article>
      <article id="tools" class="app-card"><header><h2>LLM Tool Calls</h2><a href="/api/runs/{int(run.get('id', 0))}">API</a></header><table class="app-table"><thead><tr><th>Tool</th><th>Input</th><th>Result</th></tr></thead><tbody>{tool_rows}</tbody></table></article>
    </section>
    <section id="patch" class="app-card"><header><h2>Patch</h2><a href="/runs">All runs</a></header><pre class="mini-code">{_escape_attr(patch_text)}</pre></section>
  </section>
</main>
"""


def _inline_css() -> str:
    css_path = Path(__file__).parents[2] / "static" / "app.css"
    try:
        return css_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _active(current: str, expected: str) -> str:
    return "active" if current == expected else ""


def _page_tabs(nav_active: str) -> str:
    tab_map = {
        "overview": [
            ("Home", "/overview"),
            ("Run agent", "/overview#start-run"),
            ("Readiness", "/overview#readiness"),
            ("Pull requests", "/pull-requests"),
        ],
        "runs": [
            ("Run history", "/runs#run-history"),
            ("Metrics", "/runs#metrics"),
            ("Failures", "/runs#run-history"),
        ],
        "agents": [
            ("Providers", "/agents#providers"),
            ("Tool policy", "/agents#tool-policy"),
            ("Evaluations", "/agents#evaluations"),
            ("Security", "/security"),
        ],
        "repositories": [
            ("Repositories", "/repositories#repositories"),
            ("GitHub access", "/github"),
            ("Overrides", "/repositories#overrides"),
            ("Setup detection", "/repositories#setup"),
        ],
        "issues": [
            ("Issue queue", "/issues#issue-queue"),
            ("Triage", "/issues#triage"),
            ("Escalation", "/issues#escalation"),
            ("Runs", "/runs"),
        ],
        "pull-requests": [
            ("Draft PRs", "/pull-requests#pr-queue"),
            ("Review handoff", "/pull-requests#handoff"),
            ("GitHub", "/github"),
            ("Audit", "/audit-log"),
        ],
        "tests": [
            ("Test runs", "/tests#test-runs"),
            ("Command output", "/tests#output"),
            ("Limits", "/tests#limits"),
            ("Evaluation", "/agents#evaluations"),
        ],
        "settings": [
            ("Account", "/settings#account"),
            ("Providers", "/settings#providers"),
            ("Run policy", "/settings#run-policy"),
            ("Safety", "/security"),
        ],
        "billing": [
            ("Usage", "/billing#usage"),
            ("Models", "/billing#models"),
        ],
        "audit-log": [
            ("Events", "/audit-log#events"),
            ("Security", "/security"),
        ],
        "github": [
            ("Connection", "/github#connection"),
            ("Repository access", "/github#repo-access"),
            ("PR guardrail", "/github#guardrail"),
        ],
        "security": [
            ("Safety model", "/security#safety-model"),
            ("Secrets", "/security#secrets"),
            ("Sandbox", "/security#sandbox"),
            ("Docs", "/docs"),
        ],
        "docs": [
            ("Quickstart", "/docs#quickstart"),
            ("Environment", "/docs#environment"),
            ("Deploy", "/docs#deploy"),
            ("Security", "/security"),
        ],
        "feedback": [
            ("Feedback", "/feedback"),
            ("Latest run", "/runs"),
        ],
    }
    tabs = tab_map.get(nav_active, tab_map["overview"])
    links = []
    for index, (label, href) in enumerate(tabs):
        attributes = []
        if "#" in href:
            attributes.append(f'data-section="#{_escape_attr(href.split("#", 1)[1])}"')
        if nav_active == "runs" and label in {"Run history", "Failures"}:
            run_status = "failed" if label == "Failures" else ""
            attributes.append(f'data-run-status="{run_status}"')
        extra = (" " + " ".join(attributes)) if attributes else ""
        links.append(
            f'<a class="{"active" if index == 0 else ""}" href="{href}"{extra}>{label}</a>'
        )
    return f'<nav class="platform-tabs" aria-label="{_escape_attr(nav_active)} sections">{"".join(links)}</nav>'


def _runs_for_display(queries, config, *, limit: int, workspace_id: int | None = None) -> list[dict]:
    runs = queries.list_runs(limit=limit, workspace_id=workspace_id)
    if runs or not config.dashboard_demo_data_enabled:
        return runs
    return _sample_runs()[:limit]


def _showing_demo_data(queries, config, workspace_id: int | None) -> bool:
    """True when the page is padded with sample runs instead of real history."""
    if not config.dashboard_demo_data_enabled:
        return False
    return not queries.list_runs(limit=1, workspace_id=workspace_id)


def _demo_data_banner(showing_demo: bool) -> str:
    if not showing_demo:
        return ""
    return (
        '<section class="app-card demo-data-banner"><p><span class="pill setup">demo data</span> '
        "These runs are illustrative samples, not your workspace history. Queue a real run to replace them, "
        "or set <code>DASHBOARD_DEMO_DATA_ENABLED=false</code> to always show empty states.</p></section>"
    )


def _request_workspace_id(request: Request | None, config, queries) -> int | None:
    login = _current_user(request, config) if request is not None else None
    if login in {"local-dev", "anonymous", None}:
        login = None
    workspace = queries.workspace_for_login(login)
    workspace_id = (workspace or {}).get("id")
    return int(workspace_id) if workspace_id else None


def _audit_events_for_display(queries, config, *, limit: int) -> list[dict]:
    events = queries.list_audit_events(limit=limit)
    if events or not config.dashboard_demo_data_enabled:
        return events
    return _sample_audit_events()[:limit]


def _run_filter_options(runs: list[dict], field: str) -> str:
    values = sorted({str(run.get(field) or "").strip() for run in runs if run.get(field)})
    return "".join(
        f'<option value="{_escape_attr(value)}">{_escape_attr(value)}</option>' for value in values
    )


def _run_rows(runs: list[dict]) -> str:
    if not runs:
        return '<tr data-empty-state><td colspan="10">No runs yet. Queue one from Overview.</td></tr>'
    rows = []
    for run in runs:
        commands = run.get("commands") or []
        runtime = sum(float(command.get("runtime_seconds") or 0) for command in commands)
        tests = _test_summary(run)
        pr_url = run.get("pr_url")
        pr = f'<a href="{_escape_attr(str(pr_url))}">Open ↗</a>' if pr_url else "–"
        issue = str(run.get("issue_url") or "")
        issue_label = issue.rsplit("/", 1)[-1] if issue else "queued"
        started = str(run.get("started_at") or "")[:16].replace("T", " ")
        raw_started = str(run.get("started_at") or "")
        repository = str(run.get("repo") or "unknown")
        model = str(run.get("model") or "not set")
        status = str(run.get("status") or "queued")
        branch = str(run.get("branch") or "not assigned")
        run_label = _run_label(run)
        run_href = f"/runs/{int(run.get('id', 0))}"
        issue_search = f"{issue_label} {issue}".strip()
        search_all = " ".join(
            [run_label, repository, issue_search, branch, model, status, tests]
        )
        rows.append(
            f'<tr data-repository="{_escape_attr(repository)}" data-status="{_escape_attr(status)}" '
            f'data-model="{_escape_attr(model)}" data-started-at="{_escape_attr(raw_started)}" '
            f'data-run-id="{_escape_attr(run_label)}" data-issue="{_escape_attr(issue_search)}" '
            f'data-branch="{_escape_attr(branch)}" data-search-all="{_escape_attr(search_all)}" '
            f'data-run-href="{_escape_attr(run_href)}">'
            f'<td><a href="{_escape_attr(run_href)}">{_escape_attr(run_label)}</a><small>{_escape_attr(started)}</small></td>'
            f"<td>{_escape_attr(repository)}</td>"
            f"<td>{_escape_attr(issue_label)}</td>"
            f"<td>{_escape_attr(branch)}</td>"
            f"<td>{_escape_attr(model)}</td>"
            f"<td>{_status_pill(status)}</td>"
            f"<td>{_escape_attr(tests)}</td>"
            f"<td>{_format_seconds(runtime)}</td>"
            f"<td>{_format_money(run.get('estimated_cost_usd') or 0)}</td>"
            f"<td>{pr}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _run_label(run: dict) -> str:
    if run.get("id"):
        return f"run_{int(run['id']):04d}"
    branch = str(run.get("branch") or "run")
    return branch.rsplit("-", 1)[-1][:8]


def _test_summary(run: dict) -> str:
    results = run.get("test_results") or []
    if not results:
        return "–"
    failed = sum(1 for result in results if int(result.get("exit_code") or 0) != 0)
    total = len(results)
    return f"{total - failed} passed" if failed == 0 else f"{failed} failed, {total - failed} passed"


def _status_pill(status: str) -> str:
    cls = "ok" if status in {"success", "pr_opened"} else "run" if status in {"queued", "running"} else "fail"
    return f'<span class="pill {cls}">{_escape_attr(status)}</span>'


def _run_recency_summary(runs: list[dict]) -> str:
    if not runs:
        return '<p class="metric-empty-note">Run activity will appear here.</p>'
    latest = runs[0]
    latest_label = _run_label(latest)
    latest_href = f"/runs/{int(latest.get('id', 0))}"
    started = _format_short_timestamp(latest.get("started_at"))
    return (
        '<dl class="metric-facts">'
        f'<div><dt>Latest run</dt><dd><a href="{_escape_attr(latest_href)}">{_escape_attr(latest_label)}</a></dd></div>'
        f'<div><dt>Started</dt><dd>{_escape_attr(started)}</dd></div>'
        "</dl>"
    )


def _runtime_range(runs: list[dict]) -> str:
    values = sorted(
        runtime
        for runtime in (
            sum(float(command.get("runtime_seconds") or 0) for command in (run.get("commands") or []))
            for run in runs
        )
        if runtime > 0
    )
    if not values:
        return '<p class="metric-empty-note">Runtime range will appear after the first command.</p>'
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    minimum = values[0]
    maximum = values[-1]
    marker = 50.0 if maximum == minimum else (median - minimum) / (maximum - minimum) * 100
    return (
        f'<div class="metric-range" aria-label="Runtime range from {_format_seconds(minimum)} to {_format_seconds(maximum)}">'
        f'<div class="metric-range-track" style="--marker:{marker:.2f}%"><i></i></div>'
        '<div class="metric-range-labels">'
        f'<span><small>Fastest</small>{_format_seconds(minimum)}</span>'
        f'<span><small>Slowest</small>{_format_seconds(maximum)}</span>'
        "</div></div>"
    )


def _cost_summary(runs: list[dict]) -> str:
    if not runs:
        return '<p class="metric-empty-note">Cost and token usage will appear after the first LLM call.</p>'
    total_cost = sum(float(run.get("estimated_cost_usd") or 0) for run in runs)
    total_tokens = sum(
        int((run.get("token_usage") or {}).get("total_tokens") or 0) for run in runs
    )
    return (
        '<dl class="metric-facts">'
        f'<div><dt>Average / run</dt><dd>{_format_money(total_cost / len(runs))}</dd></div>'
        f'<div><dt>Total tokens</dt><dd>{_format_compact_number(total_tokens)}</dd></div>'
        "</dl>"
    )


def _success_ring_style(success_rate: float | int | str | None, total_runs: int) -> str:
    if total_runs <= 0:
        return "--ring: conic-gradient(#e5e7eb 0 100%);"
    percent = max(0.0, min(100.0, float(success_rate or 0)))
    return f"--ring: conic-gradient(#27a85f 0 {percent}%, #e5e7eb {percent}% 100%);"


def _status_ring_style(runs: list[dict]) -> str:
    if not runs:
        return "--ring: conic-gradient(#e5e7eb 0 100%);"
    counts = {"ok": 0, "run": 0, "fail": 0, "setup": 0}
    for run in runs:
        status = str(run.get("status") or "")
        if status in {"success", "pr_opened"}:
            counts["ok"] += 1
        elif status in {"queued", "running"}:
            counts["run"] += 1
        elif status == "setup_failed":
            counts["setup"] += 1
        else:
            counts["fail"] += 1
    total = sum(counts.values()) or 1
    ok_end = counts["ok"] / total * 100
    run_end = ok_end + counts["run"] / total * 100
    fail_end = run_end + counts["fail"] / total * 100
    return (
        "--ring: conic-gradient("
        f"#27a85f 0 {ok_end:.2f}%, "
        f"#f5aa22 {ok_end:.2f}% {run_end:.2f}%, "
        f"#db3939 {run_end:.2f}% {fail_end:.2f}%, "
        f"#b8bec6 {fail_end:.2f}% 100%);"
    )


def _status_distribution(runs: list[dict]) -> str:
    counts: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        return '<ul class="empty-distribution"><li><i></i><span>No runs yet</span><b>0</b></li></ul>'
    items = []
    for status, count in sorted(counts.items()):
        color_class = (
            "green"
            if status in {"success", "pr_opened"}
            else "amber"
            if status in {"queued", "running"}
            else "gray"
            if status == "setup_failed"
            else "red"
        )
        items.append(f'<li><i class="{color_class}"></i><span>{_escape_attr(status)}</span><b>{count}</b></li>')
    return "<ul>" + "".join(items) + "</ul>"


def _format_short_timestamp(value) -> str:
    if not value:
        return "Not recorded"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:16].replace("T", " ")
    return parsed.strftime("%b %d, %H:%M").replace(" 0", " ", 1)


def _format_compact_number(value) -> str:
    amount = float(value or 0)
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}K"
    return str(int(amount))


def _format_seconds(value) -> str:
    seconds = int(float(value or 0))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _format_money(value) -> str:
    amount = float(value or 0)
    if amount == 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _status_empty_summary(status: str) -> str:
    if status == "queued":
        return "Waiting for the worker to claim this run."
    if status == "running":
        return "Worker is cloning, patching, or running Docker tests."
    return "No summary captured yet."


def _patch_empty_summary(status: str) -> str:
    if status == "queued":
        return "Patch will appear after the worker starts and the LLM edits files."
    if status == "running":
        return "Patch will appear as soon as file edits are captured."
    return "No patch generated yet."


def _is_public_path(path: str) -> bool:
    return (
        path == "/"
        or path == "/login"
        or path == "/signup"
        or path == "/auth/github/start"
        or path == "/auth/github/callback"
        or path == "/webhooks/github"
        or path == "/webhooks/github-app"
        or path == "/webhooks/stripe"
        or path == "/health"
        or path == "/ready"
        or path == "/privacy"
        or path == "/terms"
        or path == "/faq"
        or path == "/verify-email"
        or path.startswith("/static/")
    )


def _csrf_token(request: Request | None, config) -> str:
    if not config.dashboard_auth_enabled:
        return "local-dev-csrf"
    if request is None:
        return ""
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        session = "anonymous"
    return hmac.new(_session_secret(config), f"csrf:{session}".encode(), hashlib.sha256).hexdigest()


def _require_csrf(request: Request, config, submitted: str) -> None:
    if not config.dashboard_auth_enabled:
        return
    expected = _csrf_token(request, config)
    if not submitted or not hmac.compare_digest(submitted, expected):
        raise HTTPException(status_code=403, detail="invalid csrf token")


def _db_login(container, email: str, password: str) -> str | None:
    """Validate a self-service account; returns the session login or None."""
    user = container.accounts.get_user_by_email(email.strip().lower())
    if not user or not user.get("password_hash"):
        return None
    if not verify_password(password, str(user["password_hash"])):
        return None
    return str(user.get("login") or user["email"])


def _valid_login(config, username: str, password: str) -> bool:
    if not config.dashboard_auth_enabled:
        return True
    expected_password = config.dashboard_password
    if not expected_password:
        return False
    return hmac.compare_digest(username, config.dashboard_username) and hmac.compare_digest(password, expected_password)


def _is_authenticated(request: Request, config) -> bool:
    if not config.dashboard_auth_enabled:
        return True
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        return False
    user = _verify_session(session, config)
    return user is not None


def _session_login(request: Request | None, config) -> str | None:
    """The signed-in login from the session cookie, or None outside a session."""
    if request is None:
        return None
    return _verify_session(request.cookies.get(SESSION_COOKIE) or "", config)


def _current_user(request: Request, config) -> str:
    if not config.dashboard_auth_enabled:
        return "local-dev"
    session = request.cookies.get(SESSION_COOKIE)
    user = _verify_session(session or "", config)
    return user or "anonymous"


def _sign_session(username: str, config) -> str:
    issued_at = str(int(time.time()))
    payload = f"{username}:{issued_at}"
    signature = hmac.new(_session_secret(config), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def _verify_session(raw: str, config) -> str | None:
    try:
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        username, issued_at, signature = decoded.rsplit(":", 2)
        payload = f"{username}:{issued_at}"
        expected = hmac.new(_session_secret(config), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(time.time()) - int(issued_at) > SESSION_TTL_SECONDS:
            return None
        return username
    except Exception:
        return None


def _session_secret(config) -> bytes:
    secret = config.dashboard_session_secret or config.dashboard_password or "patchpilot-local-dev-secret"
    return secret.encode()


def _safe_next_path(path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        return "/runs"
    return path


def _github_oauth_enabled(config) -> bool:
    return bool(config.github_oauth_client_id and config.github_oauth_client_secret and config.github_oauth_callback_url)


def _password_login_enabled(config) -> bool:
    return config.dashboard_auth_mode in ("password", "both")


def _oauth_login_enabled(config) -> bool:
    return config.dashboard_auth_mode in ("github-oauth", "both") and _github_oauth_enabled(config)


def _github_app_credentials_configured(config) -> bool:
    has_key = bool(config.github_app_private_key or config.github_app_private_key_path)
    return bool(config.github_app_id and config.github_app_installation_id and has_key)


def _sign_oauth_state(next_path: str, config) -> str:
    payload = json.dumps({"next": _safe_next_path(next_path), "iat": int(time.time())}, separators=(",", ":"))
    signature = hmac.new(_session_secret(config), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{signature}".encode()).decode()


def _verify_oauth_state(raw: str, config) -> str | None:
    try:
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        payload, signature = decoded.rsplit(".", 1)
        expected = hmac.new(_session_secret(config), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(payload)
        if int(time.time()) - int(data.get("iat", 0)) > 600:
            return None
        return _safe_next_path(str(data.get("next") or "/runs"))
    except Exception:
        return None


def _secret_hint(value: str | None) -> str:
    if not value:
        return "not configured"
    if len(value) <= 8:
        return "configured"
    return f"{value[:4]}...{value[-4:]}"


def _seed_runtime_state(container, config) -> None:
    container.seed_runtime_state.execute(
        username=config.dashboard_username,
        openai_key_hint=_secret_hint(config.openai_api_key) if config.openai_api_key else None,
        anthropic_key_hint=_secret_hint(config.anthropic_api_key) if config.anthropic_api_key else None,
    )


def _last_test_command(run: dict) -> str:
    results = run.get("test_results") or []
    if results and isinstance(results[-1], dict):
        return str(results[-1].get("command") or "test command")
    commands = run.get("commands") or []
    if commands and isinstance(commands[-1], dict):
        return str(commands[-1].get("command") or commands[-1].get("phase") or "command")
    return "not run"


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _login_error(error: str | None) -> str:
    if error == "invalid":
        return '<p class="auth-error">Invalid username or password.</p>'
    return ""


_SIGNUP_ERRORS = {
    "invalid_email": "Enter a valid email address.",
    "password_mismatch": "The passwords do not match.",
    "weak_password": "Password must be at least 8 characters.",
    "email_exists": "An account with this email already exists. Try signing in.",
}


def _signup_error(error: str | None) -> str:
    message = _SIGNUP_ERRORS.get(error or "")
    return f'<p class="auth-error">{message}</p>' if message else ""


_ACCOUNT_ERRORS = {
    "invalid_email": "Enter a valid email address.",
    "email_exists": "Another account already uses this email.",
    "wrong_password": "The current password is incorrect.",
    "password_mismatch": "The new passwords do not match.",
    "weak_password": "The new password must be at least 8 characters.",
    "not_managed": (
        "This admin account's credentials are set via the DASHBOARD_USERNAME and "
        "DASHBOARD_PASSWORD environment variables."
    ),
    "verification_invalid": "That verification link is invalid, already used, or expired. Request a new one.",
    "verification_unavailable": (
        "Could not send a verification email. It may already be verified, or email delivery is not configured "
        "on this server."
    ),
}

_ACCOUNT_SUCCESS = {
    "password": "Password updated.",
    "email": "Email updated.",
    "verified": "Email address verified.",
    "verification_sent": "Verification email sent. Check your inbox.",
}


def _account_notice(request: Request) -> str:
    message = _ACCOUNT_ERRORS.get(request.query_params.get("error") or "")
    if message:
        return f'<section class="app-card"><p class="auth-error">{message}</p></section>'
    message = _ACCOUNT_SUCCESS.get(request.query_params.get("ok") or "")
    if message:
        return f'<section class="app-card"><p class="fine-print">{message}</p></section>'
    return ""


def _account_login(request: Request, config) -> str:
    return _session_login(request, config) or ""


def _key_test_result(request: Request) -> tuple[str, str] | None:
    provider = request.query_params.get("tested")
    status = request.query_params.get("status")
    return (provider, status) if provider and status else None


def _post_login_destination(container, queries, config, login_name: str, next_path: str) -> str:
    """Where a fresh login should land: onboarding for first-time users.

    Only the default destination is rewritten — an explicitly requested deep
    link is always honored. Env-admin sessions have no user row and are left
    alone; a workspace that already has runs predates onboarding and skips it.
    """
    destination = _safe_next_path(next_path)
    if destination != "/runs" or not config.dashboard_onboarding_enabled:
        return destination
    user = container.accounts.get_user_by_login(login_name) if login_name else None
    if not user or user.get("onboarding_completed_at"):
        return destination
    workspace = container.accounts.workspace_for_login(login_name)
    workspace_id = int(workspace["id"]) if workspace and workspace.get("id") else None
    if queries.list_runs(limit=1, workspace_id=workspace_id):
        return destination
    return "/onboarding"


def _onboarding_checklist(queries, container, config, account: dict) -> list[dict]:
    github = account.get("github")
    provider_rows = queries.provider_keys()
    configured_providers = {str(row.get("provider")) for row in provider_rows} if provider_rows else set()
    api_key_done = bool(
        config.openai_api_key or config.anthropic_api_key or configured_providers & {"openai", "anthropic"}
    )
    app_installed = bool(container.github_app.list_installations(limit=1))
    workspace_id = (account.get("workspace") or {}).get("id")
    has_runs = bool(queries.list_runs(limit=1, workspace_id=int(workspace_id) if workspace_id else None))
    github_login = str((github or {}).get("login") or "")
    return [
        {
            "title": "GitHub connection",
            "done": bool(github),
            "detail": f"Connected as @{github_login}" if github else "Sign in with GitHub to link your repositories.",
            "href": "/github",
            "action": "Connect GitHub",
        },
        {
            "title": "API key",
            "done": api_key_done,
            "detail": "An OpenAI or Anthropic key powers code generation."
            if not api_key_done
            else "Provider key configured.",
            "href": "/settings#providers",
            "action": "Configure key",
        },
        {
            "title": "GitHub App (optional)",
            "done": app_installed,
            "detail": "Install the app so new issues trigger runs automatically."
            if not app_installed
            else "App installed.",
            "href": "/github-app-setup",
            "action": "Install app",
        },
        {
            "title": "First run",
            "done": has_runs,
            "detail": "Queue a run on any GitHub issue to see PatchPilot work."
            if not has_runs
            else "First run recorded.",
            "href": "/runs",
            "action": "Start a run",
        },
    ]


def _onboarding_content(account: dict, checklist: list[dict], csrf_token: str) -> str:
    user = account.get("user") or {}
    name = str(user.get("name") or "there")
    items = "".join(
        f"""<article class="app-card settings-card">
    <header><h2>{_escape_attr(item['title'])}</h2><span class="pill {'ok' if item['done'] else 'run'}">{'done' if item['done'] else 'pending'}</span></header>
    <p>{_escape_attr(item['detail'])}</p>
    {'' if item['done'] else f'<p><a class="button outline" href="{_escape_attr(item["href"])}">{_escape_attr(item["action"])}</a></p>'}
  </article>"""
        for item in checklist
    )
    required_done = all(item["done"] for item in checklist if "(optional)" not in item["title"])
    finish = (
        f'<form method="post" action="/onboarding/complete"><input type="hidden" name="csrf_token" value="{csrf_token}">'
        '<button class="button primary" type="submit">Finish setup</button></form>'
        if required_done
        else ""
    )
    return f"""
<section class="app-card"><h2>Welcome, {_escape_attr(name)}!</h2>
<p>PatchPilot fixes GitHub issues autonomously: it plans a patch, edits code in a Docker sandbox, runs your tests, and opens a draft PR. A few steps get you to your first run.</p></section>
<section class="app-grid two">{items}</section>
<section class="app-card">
  {finish}
  <form method="post" action="/onboarding/skip"><input type="hidden" name="csrf_token" value="{csrf_token}">
  <button class="button outline" type="submit">Skip for now</button></form>
  <p class="fine-print">You can revisit this checklist any time at /onboarding. New to PatchPilot? Read the
  walkthrough in docs/getting-started.md or browse the <a href="/faq">FAQ</a>.</p>
</section>
"""


def _stats(runs: list[dict]) -> dict[str, float | int | str | None]:
    stats = calculate_run_stats(runs)
    return {
        "total_runs": stats.total_runs,
        "success_rate": stats.success_rate,
        "median_runtime_seconds": stats.median_runtime_seconds,
        "cost_today_usd": stats.total_cost_usd,
    }


def _sample_runs() -> list[dict]:
    return [
        {
            "id": 101,
            "status": "pr_opened",
            "repo": "acme/api",
            "issue_url": "https://github.com/acme/api/issues/4821",
            "branch": "agent/fix-issue-4821-20260504103000",
            "model": "gpt-4.1",
            "commands": [{"phase": "test", "runtime_seconds": 42.1, "exit_code": 0}],
            "tool_calls": [{"name": "read_file", "ok": True}, {"name": "apply_patch", "ok": True}],
            "patches": ["diff --git a/calc.py b/calc.py\n- return a - b\n+ return a + b"],
            "test_results": [{"command": "python -m pytest", "exit_code": 0}],
            "estimated_cost_usd": 0.1842,
            "started_at": "2026-05-04T10:30:00Z",
        },
        {
            "id": 100,
            "status": "failed_tests",
            "repo": "northstar/worker",
            "issue_url": "https://github.com/northstar/worker/issues/88",
            "branch": "agent/fix-issue-88-20260504091200",
            "model": "claude-3-5-sonnet-latest",
            "commands": [{"phase": "test", "runtime_seconds": 66.4, "exit_code": 1}],
            "tool_calls": [{"name": "search_text", "ok": True}],
            "patches": [],
            "test_results": [{"command": "python -m pytest", "exit_code": 1}],
            "estimated_cost_usd": 0.2311,
            "started_at": "2026-05-04T09:12:00Z",
        },
        {
            "id": 99,
            "status": "setup_failed",
            "repo": "atlas/cli",
            "issue_url": "https://github.com/atlas/cli/issues/17",
            "branch": "agent/fix-issue-17-20260504085600",
            "model": "gpt-4.1-mini",
            "commands": [{"phase": "setup", "runtime_seconds": 18.2, "exit_code": 1}],
            "tool_calls": [],
            "patches": [],
            "test_results": [],
            "estimated_cost_usd": 0.022,
            "started_at": "2026-05-04T08:56:00Z",
        },
    ]


def _sample_audit_events() -> list[dict]:
    return [
        {
            "created_at": "2026-05-04T10:26:50Z",
            "actor": "agent",
            "event": "filesystem.edit",
            "target": "tests/test_docker.py",
            "result": "success",
            "metadata": {},
        },
        {
            "created_at": "2026-05-04T10:25:21Z",
            "actor": "agent",
            "event": "sandbox.run",
            "target": "pytest -q",
            "result": "failed",
            "metadata": {},
        },
        {
            "created_at": "2026-05-04T10:24:31Z",
            "actor": "agent",
            "event": "filesystem.read",
            "target": "tests/test_docker.py",
            "result": "success",
            "metadata": {},
        },
    ]


def _missing_run_content(run_id: int) -> str:
    return f"""
<main class="app-shell">
  <aside class="runs-rail">
    <a class="rail-brand" href="/overview"><span class="bot-mark">P</span><strong>PatchPilot</strong><small>Workspace</small></a>
    <nav><a href="/overview"><span>⌂</span>Overview</a><a class="active" href="/runs"><span>☷</span>Runs</a><a href="/issues"><span>ⓘ</span>Issues</a><a href="/tests"><span>✓</span>Tests</a><a href="/settings"><span>⚙</span>Settings</a></nav>
  </aside>
  <section class="app-main">
    <header class="app-head"><div><h1>Run not found</h1><p>run_{run_id:04d} is not in the configured database.</p></div><nav><a href="/runs">Back to runs</a><a href="/overview">Queue run</a></nav></header>
    <section class="app-card"><h2>No persisted run</h2><p>This environment is showing real database-backed runs only. Queue a run from Overview or enable demo data for screenshot mode.</p></section>
  </section>
</main>
"""
