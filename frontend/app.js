// Dashboard logic: fetch /api/repos and /api/runs, poll for status
// No build step, no dependencies — plain ES2021.

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let repos        = [];        // [{id, name, github_repo, ...}]
let runs         = [];        // [{id, repo_id, status, current_stage, ...}]
let pollingTimer = null;      // setInterval handle when a run is in-flight
let drawerRunId  = null;      // which run the detail drawer is showing

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('form-register').addEventListener('submit', onRegisterSubmit);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.getElementById('run-detail').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeDrawer();  // click backdrop
  });

  loadAll();
});

async function loadAll() {
  await Promise.all([loadRepos(), loadRuns()]);
  ensurePolling();
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function apiFetch(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Repos
// ---------------------------------------------------------------------------
async function loadRepos() {
  try {
    repos = await apiFetch('/api/repos');
    renderRepos();
  } catch (e) {
    document.getElementById('repos-list').innerHTML =
      `<p class="muted err">Failed to load repos: ${e.message}</p>`;
  }
}

function renderRepos() {
  const el = document.getElementById('repos-list');
  if (!repos.length) {
    el.innerHTML = '<p class="muted">No repositories registered yet.</p>';
    return;
  }
  el.innerHTML = repos.map(r => `
    <div class="card" data-repo-id="${r.id}">
      <div class="card-body">
        <div class="card-title">${esc(r.name)}</div>
        <div class="card-meta">${esc(r.github_repo)} &middot; registered ${fmtDate(r.created_at)}</div>
      </div>
      <div class="card-actions">
        <button class="btn-trigger" onclick="triggerRun(${r.id})">▶ Trigger</button>
      </div>
    </div>
  `).join('');
}

async function triggerRun(repoId) {
  const btn = document.querySelector(`[data-repo-id="${repoId}"] .btn-trigger`);
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  try {
    const res = await apiFetch(`/api/repos/${repoId}/trigger`, { method: 'POST' });
    await loadRuns();
    ensurePolling();
  } catch (e) {
    alert(`Trigger failed: ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '▶ Trigger'; }
  }
}

// ---------------------------------------------------------------------------
// Register form
// ---------------------------------------------------------------------------
async function onRegisterSubmit(e) {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form));
  const msg  = document.getElementById('register-msg');
  const btn  = form.querySelector('button[type=submit]');

  btn.disabled = true;
  msg.textContent = '';
  msg.className = 'msg';

  try {
    await apiFetch('/api/repos', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    form.reset();
    msg.textContent = 'Registered!';
    msg.className = 'msg ok';
    await loadRepos();
  } catch (e) {
    msg.textContent = `Error: ${e.message}`;
    msg.className = 'msg err';
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------
async function loadRuns() {
  try {
    runs = await apiFetch('/api/runs');
    renderRuns();
    // Refresh drawer if open
    if (drawerRunId !== null) {
      const updated = runs.find(r => r.id === drawerRunId);
      if (updated) renderDrawer(updated);
    }
  } catch (e) {
    document.getElementById('runs-list').innerHTML =
      `<p class="muted">Failed to load runs: ${e.message}</p>`;
  }
}

function renderRuns() {
  const el = document.getElementById('runs-list');
  if (!runs.length) {
    el.innerHTML = '<p class="muted">No runs yet. Trigger one above.</p>';
    return;
  }
  el.innerHTML = runs.map(r => {
    const repo = repos.find(x => x.id === r.repo_id);
    const repoName = repo ? repo.name : `repo #${r.repo_id}`;
    const stage = r.current_stage || 0;
    const inFlight = r.status === 'running' || r.status === 'pending';
    return `
      <div class="card">
        <div class="card-body">
          <div class="card-title">
            Run #${r.id} &mdash; ${esc(repoName)}
            <span class="badge badge-${r.status}">${r.status}${inFlight ? '' : ''}</span>
          </div>
          <div class="card-meta">${fmtDate(r.created_at)}</div>
          <div class="stages">
            ${[1,2,3,4,5].map(n => stageDot(n, stage, r.status)).join('')}
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-detail" onclick="openDrawer(${r.id})">Details</button>
        </div>
      </div>
    `;
  }).join('');
}

function stageDot(n, currentStage, status) {
  let cls = '';
  if (n < currentStage)  cls = 'done';
  else if (n === currentStage) cls = (status === 'error' || status === 'failed') ? 'failed' : 'active';
  return `<div class="stage-dot ${cls}" title="Stage ${n}"></div>`;
}

// ---------------------------------------------------------------------------
// Polling — keep polling while any run is pending or running
// ---------------------------------------------------------------------------
function ensurePolling() {
  const hasActive = runs.some(r => r.status === 'pending' || r.status === 'running');
  if (hasActive && !pollingTimer) {
    pollingTimer = setInterval(async () => {
      await loadRuns();
      const stillActive = runs.some(r => r.status === 'pending' || r.status === 'running');
      if (!stillActive) {
        clearInterval(pollingTimer);
        pollingTimer = null;
      }
    }, 2000);
  }
}

// ---------------------------------------------------------------------------
// Detail drawer
// ---------------------------------------------------------------------------
function openDrawer(runId) {
  drawerRunId = runId;
  const run = runs.find(r => r.id === runId);
  if (run) renderDrawer(run);
  document.getElementById('run-detail').classList.remove('hidden');
}

function closeDrawer() {
  drawerRunId = null;
  document.getElementById('run-detail').classList.add('hidden');
}

function renderDrawer(run) {
  const repo = repos.find(x => x.id === run.repo_id);
  const repoName = repo ? repo.name : `repo #${run.repo_id}`;
  let html = `
    <h2 style="margin-bottom:16px;">Run #${run.id} — ${esc(repoName)}</h2>
    <div class="detail-section">
      <table class="detail-table">
        <tr><th>Status</th><td><span class="badge badge-${run.status}">${run.status}</span></td></tr>
        <tr><th>Stage</th><td>${run.current_stage ? `${run.current_stage} / 5` : '—'}</td></tr>
        <tr><th>Started</th><td>${run.started_at ? fmtDate(run.started_at) : '—'}</td></tr>
        <tr><th>Finished</th><td>${run.finished_at ? fmtDate(run.finished_at) : '—'}</td></tr>
      </table>
    </div>
  `;

  if (run.error_message) {
    html += section('Error', `<pre class="log-output">${esc(run.error_message)}</pre>`);
  }

  if (run.stage1_out) {
    const s = run.stage1_out;
    html += section('Stage 1 — Change Detection', `
      <table class="detail-table">
        <tr><th>Category</th><th>Count</th></tr>
        <tr><td>Renames</td><td>${(s.renames||[]).length}</td></tr>
        <tr><td>Removals</td><td>${(s.removals||[]).length}</td></tr>
        <tr><td>Newly required</td><td>${(s.newly_required||[]).length}</td></tr>
        <tr><td>Deprecated</td><td>${(s.deprecated||[]).length}</td></tr>
        <tr><td>Type changes</td><td>${(s.type_changes||[]).length}</td></tr>
        <tr><td><strong>Total</strong></td><td><strong>${s.total||0}</strong></td></tr>
      </table>
    `);
  }

  if (run.stage2_out) {
    const s = run.stage2_out;
    const files = Object.keys(s.by_file || {});
    html += section('Stage 2 — Codebase Scan', `
      <table class="detail-table">
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Affected sites</td><td>${s.total_affected}</td></tr>
        <tr><td>Files scanned</td><td>${files.length}</td></tr>
      </table>
      ${files.length ? `<p style="margin-top:8px;font-size:12px;color:var(--muted)">${files.map(esc).join(', ')}</p>` : ''}
    `);
  }

  if (run.stage3_out) {
    const s = run.stage3_out;
    const c = s.counts || {};
    html += section('Stage 3 — Patch Generation', `
      <table class="detail-table">
        <tr><th>Status</th><th>Count</th></tr>
        <tr><td>✅ Mechanical</td><td>${c.mechanical||0}</td></tr>
        <tr><td>🤖 LLM-assisted</td><td>${c.llm||0}</td></tr>
        <tr><td>⚠️ Human review needed</td><td>${c.human||0}</td></tr>
        <tr><td>⏭️ Skipped</td><td>${c.skipped||0}</td></tr>
      </table>
    `);
  }

  if (run.stage4_out) {
    const s = run.stage4_out;
    const badge = s.passed ? '✅ Passed' : '❌ Failed';
    html += section('Stage 4 — Test Gate', `
      <table class="detail-table">
        <tr><th>Result</th><td>${badge}</td></tr>
        <tr><th>Total</th><td>${s.total}</td></tr>
        <tr><th>Passed</th><td>${s.passed_count}</td></tr>
        <tr><th>Failed</th><td>${s.failed_count}</td></tr>
        <tr><th>Errors</th><td>${s.error_count}</td></tr>
      </table>
      ${s.stdout ? `<pre class="log-output" style="margin-top:10px">${esc(s.stdout)}</pre>` : ''}
    `);
  }

  if (run.stage5_out) {
    const s = run.stage5_out;
    html += section('Stage 5 — Pull Request', s.opened
      ? `<table class="detail-table">
           <tr><th>Status</th><td>✅ Opened</td></tr>
           <tr><th>PR #</th><td>${s.pr_number}</td></tr>
           <tr><th>Branch</th><td><code>${esc(s.branch)}</code></td></tr>
           <tr><th>Link</th><td><a class="pr-link" href="${esc(s.pr_url)}" target="_blank" rel="noopener">${esc(s.pr_url)}</a></td></tr>
         </table>`
      : `<table class="detail-table">
           <tr><th>Status</th><td>⏭️ Not opened</td></tr>
           <tr><th>Reason</th><td>${esc(s.reason)}</td></tr>
         </table>`
    );
  }

  document.getElementById('drawer-content').innerHTML = html;
}

function section(title, body) {
  return `<div class="detail-section"><h3>${title}</h3>${body}</div>`;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso.replace(' ', 'T') + 'Z').toLocaleString();
  } catch { return iso; }
}
