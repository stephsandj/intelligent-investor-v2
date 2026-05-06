// ═══════════════════ State ═══════════════════
var _users = [];
var _filteredUsers = [];
var _currentPage = 'dashboard';
var _adminInfo = null;

// ═══════════════════ API helper ══════════════
async function _api(method, path, body) {
  try {
    var opts = { method: method, credentials: 'include', headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    var r = await fetch(path, opts);
    var data;
    try { data = await r.json(); } catch(_) { data = {}; }
    return { ok: r.ok, status: r.status, data: data };
  } catch(e) {
    return { ok: false, status: 0, data: { error: e.message } };
  }
}

// ═══════════════════ Auth ════════════════════
async function doLogin() {
  var email    = document.getElementById('login-email').value.trim();
  var password = document.getElementById('login-password').value;
  var errEl    = document.getElementById('login-error');
  errEl.textContent = '';
  if (!email || !password) { errEl.textContent = 'Email and password are required'; return; }
  var r = await _api('POST', '/admin/api/login', { email: email, password: password });
  if (!r.ok) { errEl.textContent = r.data.error || 'Login failed'; return; }
  _adminInfo = r.data;
  _showApp(_adminInfo);
  loadDashboard();
  _startInactivityTimer();
}

function _showApp(info) {
  document.getElementById('admin-name').textContent = info.full_name || info.email || 'Admin';
  document.getElementById('admin-role').textContent  = info.role || 'superadmin';
  document.getElementById('admin-avatar').textContent = ((info.full_name || info.email || 'A')[0] || 'A').toUpperCase();
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('app').classList.add('visible');
}

async function doLogout() {
  _stopInactivityTimer();
  await _api('POST', '/admin/api/logout');
  location.reload();
}

async function checkSession() {
  var r = await _api('GET', '/admin/api/me');
  if (r.ok) { _adminInfo = r.data; _showApp(_adminInfo); loadDashboard(); _startInactivityTimer(); }
}

// ═══════════════════ Inactivity timeout (15 min) ══════════════════
var _inactivityTimer  = null;
var _inactivityWarn   = null;
var INACTIVITY_MS     = 15 * 60 * 1000;
var INACTIVITY_WARN   = 14 * 60 * 1000;

function _startInactivityTimer() {
  _resetInactivityTimer();
  ['mousemove','mousedown','keydown','touchstart','scroll','click'].forEach(function(evt) {
    document.addEventListener(evt, _resetInactivityTimer, { passive: true });
  });
}

function _stopInactivityTimer() {
  clearTimeout(_inactivityTimer);
  clearTimeout(_inactivityWarn);
  ['mousemove','mousedown','keydown','touchstart','scroll','click'].forEach(function(evt) {
    document.removeEventListener(evt, _resetInactivityTimer);
  });
}

function _resetInactivityTimer() {
  clearTimeout(_inactivityTimer);
  clearTimeout(_inactivityWarn);
  _inactivityWarn = setTimeout(function() {
    showToast('⚠️ Session expires in 1 minute due to inactivity', 'error');
  }, INACTIVITY_WARN);
  _inactivityTimer = setTimeout(async function() {
    _stopInactivityTimer();
    showToast('Session expired — you have been signed out', 'error');
    await new Promise(function(r) { setTimeout(r, 1500); });
    await _api('POST', '/admin/api/logout');
    location.reload();
  }, INACTIVITY_MS);
}

// ═══════════════════ Navigation ══════════════
function showPage(name) {
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.nav-item').forEach(function(n) { n.classList.remove('active'); });
  document.getElementById('page-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  var titles = {
    dashboard:  'Dashboard',
    users:      'Users',
    plans:      'Plans',
    audit:      'Audit Log',
    monitoring: 'Monitoring',
  };
  document.getElementById('page-title').textContent = titles[name] || name;

  if (_currentPage === 'monitoring' && name !== 'monitoring') {
    _stopMonAutoRefresh();
  }

  _currentPage = name;
  if (name === 'users')      loadUsers();
  if (name === 'plans')      loadPlans();
  if (name === 'audit')      loadAuditLog();
  if (name === 'dashboard')  loadDashboard();
  if (name === 'monitoring') { loadMonitoring(); _startMonAutoRefresh(); }
}

function refreshCurrentPage() { showPage(_currentPage); }

// ═══════════════════ Dashboard ═══════════════
async function loadDashboard() {
  var r = await _api('GET', '/admin/api/stats');
  if (!r.ok) { showToast('Failed to load stats', 'error'); return; }
  var d = r.data;
  document.getElementById('stat-total').textContent  = d.total_users != null ? d.total_users : '—';
  document.getElementById('stat-active').textContent = d.active_subscriptions != null ? d.active_subscriptions : '—';
  document.getElementById('stat-trials').textContent = d.active_trials != null ? d.active_trials : '—';
  document.getElementById('stat-mrr').textContent    = d.mrr_usd != null ? '$' + Number(d.mrr_usd).toLocaleString() : '—';
  document.getElementById('stat-runs').textContent   = d.runs_today != null ? d.runs_today : '—';
  document.getElementById('stat-as-of').textContent  = d.as_of ? 'as of ' + new Date(d.as_of).toLocaleTimeString() : '';

  var breakdown = d.active_subscriptions_by_plan || {};
  var planOrder = [
    { key: 'trial',      label: 'Free Trial',  color: 'var(--muted)' },
    { key: 'starter',    label: 'Starter',     color: 'var(--blue)' },
    { key: 'pro',        label: 'Pro',         color: 'var(--green)' },
    { key: 'advanced',   label: 'Advanced',    color: 'var(--orange)' },
    { key: 'analyst',    label: 'Analyst',     color: 'var(--purple)' },
    { key: 'enterprise', label: 'Enterprise',  color: 'var(--gold)' },
  ];
  document.getElementById('plan-breakdown').innerHTML = planOrder.map(function(p) {
    return '<div class="plan-card"><div><div style="font-weight:600;color:' + p.color + '">' + p.label + '</div></div>'
      + '<div style="font-size:1.6rem;font-weight:700;color:' + p.color + '">' + (breakdown[p.key] != null ? breakdown[p.key] : 0) + '</div></div>';
  }).join('');

  var uResp = await _api('GET', '/admin/api/users');
  if (uResp.ok) {
    var recent = (uResp.data.users || []).slice(0, 8);
    document.getElementById('recent-users-wrap').innerHTML = _buildSimpleUsersTable(recent);
  }
}

function _buildSimpleUsersTable(users) {
  if (!users.length) return '<div style="padding:20px;color:var(--muted)">No users yet</div>';
  return '<table><thead><tr><th>Email</th><th>Plan</th><th>Status</th><th>Created</th></tr></thead><tbody>'
    + users.map(function(u) {
      return '<tr><td>' + esc(u.email) + '</td><td>' + planBadge(u.plan_name) + '</td>'
        + '<td><span class="status-' + (u.status||'') + '">' + (u.status||'—') + '</span></td>'
        + '<td style="color:var(--muted);font-size:.82rem">' + (u.created_at ? new Date(u.created_at).toLocaleDateString() : '—') + '</td></tr>';
    }).join('') + '</tbody></table>';
}

// ═══════════════════ Users ════════════════════
async function loadUsers() {
  var tbody = document.getElementById('users-tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:32px">Loading…</td></tr>';
  var r = await _api('GET', '/admin/api/users');
  if (!r.ok) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--red)">Failed to load users</td></tr>';
    return;
  }
  _users = r.data.users || [];
  _filteredUsers = _users.slice();
  renderUsers();
}

function filterUsers() {
  var q        = (document.getElementById('user-search').value || '').toLowerCase();
  var plan     = (document.getElementById('col-filter-plan')     ? document.getElementById('col-filter-plan').value     : '');
  var status   = (document.getElementById('col-filter-status')   ? document.getElementById('col-filter-status').value   : '');
  var verified = (document.getElementById('col-filter-verified') ? document.getElementById('col-filter-verified').value : '');

  _filteredUsers = _users.filter(function(u) {
    var mq = !q        || (u.email||'').toLowerCase().includes(q) || (u.full_name||'').toLowerCase().includes(q);
    var mp = !plan     || (u.plan_name||'').toLowerCase() === plan;
    var ms = !status   || (u.status||'').toLowerCase() === status;
    var mv = !verified || (verified === 'yes'
               ? (u.email_verified === true || u.email_verified === 'true')
               : (u.email_verified !== true && u.email_verified !== 'true'));
    return mq && mp && ms && mv;
  });
  renderUsers();
}

function updateColFilterStyle(sel) {
  if (sel.value) sel.classList.add('active');
  else           sel.classList.remove('active');
}

function clearAllFilters() {
  ['col-filter-plan','col-filter-status','col-filter-verified'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) { el.value = ''; el.classList.remove('active'); }
  });
  document.getElementById('user-search').value = '';
  filterUsers();
}

function renderUsers() {
  var tbody = document.getElementById('users-tbody');
  clearBulkSelection && clearBulkSelection();
  if (!_filteredUsers.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:32px">No users found</td></tr>';
    return;
  }
  tbody.innerHTML = _filteredUsers.map(function(u) {
    var uid = u.user_id;
    var emailSafe = esc(u.email);
    var isEmailVerified = u.email_verified === true || u.email_verified === 'true';
    var emailBadge = isEmailVerified
      ? '<span style="color:var(--green);font-weight:600;font-size:.75rem">✅ Verified</span>'
      : '<span style="color:var(--red);font-weight:600;font-size:.75rem">❌ Unverified</span>';
    return '<tr>'
      + '<td class="chk-col"><input type="checkbox" class="user-chk" data-uid="' + uid + '"></td>'
      + '<td><div style="font-weight:600">' + emailSafe + '</div>'
      + '<div style="color:var(--muted);font-size:.78rem">' + esc(u.full_name || '—') + '</div></td>'
      + '<td>' + planBadge(u.plan_name) + '</td>'
      + '<td><span class="status-' + (u.status||'') + '">' + (u.status||'—') + '</span></td>'
      + '<td style="text-align:center">' + emailBadge + '</td>'
      + '<td>'
      + '<div style="display:flex;align-items:center;gap:6px">'
      + '<span class="runs-count" id="run-count-' + uid + '">' + (u.daily_runs_today != null ? u.daily_runs_today : 0) + '</span>'
      + '<button class="btn btn-green btn-sm" data-action="set-runs" data-uid="' + uid + '" data-email="' + emailSafe + '" data-runs="' + (u.daily_runs_today||0) + '" title="Set daily run count">⚡ Set</button>'
      + '</div></td>'
      + '<td style="color:var(--muted);font-size:.82rem">' + (u.created_at ? new Date(u.created_at).toLocaleDateString() : '—') + '</td>'
      + '<td><div style="display:flex;gap:5px">'
      + '<button class="btn btn-outline btn-sm" data-action="edit" data-user="' + esc(JSON.stringify(u)) + '" title="Edit user">✏️</button>'
      + '<button class="btn btn-outline btn-sm" data-action="extend-trial" data-uid="' + uid + '" title="Extend trial">⏱</button>'
      + '<button class="btn btn-danger btn-sm" data-action="delete" data-uid="' + uid + '" data-email="' + emailSafe + '" title="Delete user">🗑</button>'
      + '</div></td>'
      + '</tr>';
  }).join('');
}

// ═══════════════════ Plans ════════════════════
async function loadPlans() {
  var tbody = document.getElementById('plans-tbody');
  tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted)">Loading…</td></tr>';
  var r = await _api('GET', '/admin/api/plans');
  if (!r.ok) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--red)">Failed to load plans</td></tr>';
    return;
  }
  var plans = r.data.plans || [];
  var order = ['trial','starter','pro','advanced','analyst','enterprise'];
  plans.sort(function(a,b) { return order.indexOf(a.name) - order.indexOf(b.name); });

  tbody.innerHTML = plans.map(function(p) {
    var f = p.features || {};
    var etfBond = (f.etf && f.bond) ? 'ETF+Bond' : f.etf ? 'ETF only' : '<span class="dash">—</span>';
    if (f.all_modes && f.etf && f.bond) etfBond = 'All';
    var price  = p.price_monthly ? '$' + p.price_monthly + '/mo' : '<span style="color:var(--muted)">Free</span>';
    var runs   = p.runs_per_day == null ? '<span style="color:var(--gold)">∞</span>' : p.runs_per_day;
    var picks  = p.max_ai_picks != null ? p.max_ai_picks : 5;
    var pdf    = p.max_pdf_history == null ? '<span style="color:var(--gold)">∞</span>' : p.max_pdf_history;
    var single = f.single_ticker ? '<span class="check" style="font-size:.78rem">→ Daily limit</span>' : '<span class="dash">—</span>';
    var emailV = f.email ? '<span class="check">✓</span>' : '<span class="dash">—</span>';
    var agent  = f.agent_logs ? '<span class="check">✓ Full</span>' : '<span class="cross">✗</span>';
    return '<tr>'
      + '<td style="text-align:left">' + planBadge(p.name) + ' <strong>' + esc(p.display_name) + '</strong></td>'
      + '<td>' + price + '</td>'
      + '<td style="font-weight:700">' + runs + '</td>'
      + '<td>' + single + '</td>'
      + '<td>' + picks + '</td>'
      + '<td>' + pdf + '</td>'
      + '<td>' + etfBond + '</td>'
      + '<td>' + emailV + '</td>'
      + '<td>' + agent + '</td>'
      + '</tr>';
  }).join('');
}

// ═══════════════════ Audit Log ═══════════════
async function loadAuditLog() {
  var tbody = document.getElementById('audit-tbody');
  tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted)">Loading…</td></tr>';
  var r = await _api('GET', '/admin/api/audit-log');
  if (!r.ok) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--red)">Failed to load audit log</td></tr>';
    return;
  }
  var entries = r.data.audit_log || [];
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:32px">No audit entries</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(function(e) {
    return '<tr>'
      + '<td style="color:var(--muted);white-space:nowrap;font-size:.8rem">' + (e.created_at ? new Date(e.created_at).toLocaleString() : '—') + '</td>'
      + '<td><code style="background:var(--bg3);padding:2px 6px;border-radius:4px;font-size:.78rem">' + esc(e.action) + '</code></td>'
      + '<td style="font-size:.82rem">' + esc(e.target_email || (e.target_user_id ? e.target_user_id.slice(0,8)+'…' : '—')) + '</td>'
      + '<td style="color:var(--muted);font-size:.8rem">' + esc(e.notes || '—') + '</td>'
      + '</tr>';
  }).join('');
}

// ═══════════════════ Modals ══════════════════
var _editingUser = null;

function openEditModalRaw(jsonStr) {
  var u = JSON.parse(jsonStr);
  _editingUser = u;
  document.getElementById('eu-uid').value    = u.user_id || '';
  document.getElementById('eu-name').value   = u.full_name || '';
  document.getElementById('eu-email').value  = u.email || '';
  document.getElementById('eu-password').value = '';
  document.getElementById('eu-plan').value   = String(u.plan_id || 1);
  document.getElementById('eu-status').value = u.status || 'active';
  document.getElementById('eu-msg').textContent = '';
  updateEmailVerificationDisplay();
  document.getElementById('edit-user-modal').classList.remove('hidden');
}

function updateEmailVerificationDisplay() {
  if (!_editingUser) return;
  var isVerified = _editingUser.email_verified === true || _editingUser.email_verified === 'true';
  var statusBadge = document.getElementById('eu-email-status-badge');
  var btn = document.getElementById('eu-email-btn');

  if (isVerified) {
    statusBadge.textContent = '✅ Verified';
    statusBadge.style.color = 'var(--green)';
    btn.textContent = 'Mark as Unverified';
    btn.style.color = 'var(--red)';
  } else {
    statusBadge.textContent = '❌ Unverified';
    statusBadge.style.color = 'var(--red)';
    btn.textContent = 'Mark as Verified';
    btn.style.color = 'var(--green)';
  }
}

async function toggleEmailVerification() {
  if (!_editingUser) return;
  var userId = _editingUser.user_id;
  var isCurrentlyVerified = _editingUser.email_verified === true || _editingUser.email_verified === 'true';
  var endpoint = isCurrentlyVerified ? '/admin/users/' + userId + '/unverify-email' : '/admin/users/' + userId + '/verify-email';

  var btn = document.getElementById('eu-email-btn');
  btn.disabled = true;
  var originalText = btn.textContent;
  btn.textContent = 'Updating…';

  var r = await _api('POST', endpoint);

  btn.disabled = false;
  btn.textContent = originalText;

  if (!r.ok) {
    showToast(r.data.error || 'Failed to update email verification status', 'error');
    return;
  }

  _editingUser.email_verified = !isCurrentlyVerified;
  updateEmailVerificationDisplay();
  showToast('Email marked as ' + (!isCurrentlyVerified ? 'verified' : 'unverified'), 'success');
}

async function saveUserEdit() {
  var uid    = document.getElementById('eu-uid').value;
  var name   = document.getElementById('eu-name').value.trim();
  var email  = document.getElementById('eu-email').value.trim();
  var pw     = document.getElementById('eu-password').value;
  var planId = parseInt(document.getElementById('eu-plan').value);
  var status = document.getElementById('eu-status').value;
  var msg    = document.getElementById('eu-msg');
  msg.textContent = '';

  var updates = {};
  if (name)  updates.full_name = name;
  if (email) updates.email     = email;
  if (pw)    updates.password  = pw;

  if (Object.keys(updates).length) {
    var r = await _api('PUT', '/admin/api/users/' + uid, updates);
    if (!r.ok) { msg.className = 'modal-error'; msg.textContent = r.data.error || 'Update failed'; return; }
  }

  var rp = await _api('POST', '/admin/api/users/' + uid + '/plan', { plan_id: planId, billing_cycle: 'monthly' });
  if (!rp.ok) { msg.className = 'modal-error'; msg.textContent = rp.data.error || 'Plan update failed'; return; }

  var rs = await _api('POST', '/admin/api/users/' + uid + '/status', { status: status });
  if (!rs.ok) { msg.className = 'modal-error'; msg.textContent = rs.data.error || 'Status update failed'; return; }

  closeModal('edit-user-modal');
  showToast('User updated successfully', 'success');
  loadUsers();
}

function openSetRunsModal(uid, email, current) {
  document.getElementById('sr-uid').value      = uid;
  document.getElementById('sr-email').textContent = email;
  document.getElementById('sr-count').value    = current;
  document.getElementById('sr-msg').textContent = '';
  document.getElementById('set-runs-modal').classList.remove('hidden');
  setTimeout(function() { document.getElementById('sr-count').focus(); document.getElementById('sr-count').select(); }, 100);
}

async function saveRunCount() {
  var uid   = document.getElementById('sr-uid').value;
  var count = parseInt(document.getElementById('sr-count').value);
  var msg   = document.getElementById('sr-msg');
  if (isNaN(count) || count < 0) { msg.className = 'modal-error'; msg.textContent = 'Enter a valid non-negative number'; return; }

  var r = await _api('POST', '/admin/api/users/' + uid + '/set-runs', { count: count });
  if (!r.ok) { msg.className = 'modal-error'; msg.textContent = r.data.error || 'Failed'; return; }

  var userRow = _users.find(function(u) { return u.user_id === uid; });
  if (userRow) userRow.daily_runs_today = count;
  var countEl = document.getElementById('run-count-' + uid);
  if (countEl) countEl.textContent = count;

  closeModal('set-runs-modal');
  showToast('Daily run count set to ' + count, 'success');
}

function openCreateUserModal() {
  document.getElementById('cu-email').value    = '';
  document.getElementById('cu-name').value     = '';
  document.getElementById('cu-password').value = '';
  document.getElementById('cu-plan').value     = '1';
  document.getElementById('cu-verify').checked = true;
  document.getElementById('cu-msg').textContent = '';
  document.getElementById('create-user-modal').classList.remove('hidden');
}

async function createUser() {
  var email  = document.getElementById('cu-email').value.trim();
  var name   = document.getElementById('cu-name').value.trim();
  var pw     = document.getElementById('cu-password').value;
  var planId = parseInt(document.getElementById('cu-plan').value);
  var verify = document.getElementById('cu-verify').checked;
  var msg    = document.getElementById('cu-msg');
  if (!email) { msg.className = 'modal-error'; msg.textContent = 'Email is required'; return; }

  var body = { email: email, plan_id: planId, verify_email: verify };
  if (name) body.full_name = name;
  if (pw)   body.password  = pw;

  var r = await _api('POST', '/admin/api/users', body);
  if (!r.ok) { msg.className = 'modal-error'; msg.textContent = r.data.error || 'Failed to create user'; return; }

  closeModal('create-user-modal');
  showToast('User ' + email + ' created successfully', 'success');
  loadUsers();
}

function openExtendTrialModal(uid) {
  document.getElementById('et-uid').value   = uid;
  document.getElementById('et-days').value  = '7';
  document.getElementById('et-msg').textContent = '';
  document.getElementById('extend-trial-modal').classList.remove('hidden');
}

async function doExtendTrial() {
  var uid  = document.getElementById('et-uid').value;
  var days = parseInt(document.getElementById('et-days').value);
  var msg  = document.getElementById('et-msg');
  if (!days || days < 1) { msg.className = 'modal-error'; msg.textContent = 'Enter a valid number of days'; return; }

  var r = await _api('POST', '/admin/api/users/' + uid + '/extend-trial', { days: days });
  if (!r.ok) { msg.className = 'modal-error'; msg.textContent = r.data.error || 'Failed'; return; }

  closeModal('extend-trial-modal');
  showToast('Trial extended by ' + days + ' day(s)', 'success');
  loadUsers();
}

// ── Bulk select / delete ─────────────────────────────────────────────────────
function toggleSelectAll(cb) {
  document.querySelectorAll('.user-chk[data-uid]').forEach(function(c) { c.checked = cb.checked; });
  updateBulkBar();
}

function updateBulkBar() {
  var checked = document.querySelectorAll('.user-chk[data-uid]:checked').length;
  var total   = document.querySelectorAll('.user-chk[data-uid]').length;
  var bar     = document.getElementById('bulk-bar');
  var countEl = document.getElementById('bulk-count');
  var chkAll  = document.getElementById('chk-all');
  if (checked > 0) {
    bar.classList.add('visible');
    countEl.textContent = checked + ' of ' + total + ' selected';
    chkAll.indeterminate = checked > 0 && checked < total;
    chkAll.checked = checked === total;
  } else {
    bar.classList.remove('visible');
    chkAll.indeterminate = false;
    chkAll.checked = false;
  }
}

function clearBulkSelection() {
  document.querySelectorAll('.user-chk').forEach(function(c) { c.checked = false; c.indeterminate = false; });
  document.getElementById('bulk-bar').classList.remove('visible');
}

async function bulkDeleteSelected() {
  var checked = Array.from(document.querySelectorAll('.user-chk[data-uid]:checked'));
  if (!checked.length) return;
  var count = checked.length;
  var ids   = checked.map(function(c) { return c.dataset.uid; });
  showConfirm(
    '🗑',
    'Delete ' + count + ' user' + (count > 1 ? 's' : '') + '?',
    'This will <strong>permanently remove</strong> ' + count + ' user' + (count > 1 ? 's' : '') + ' and all their data.<br>This action <strong>cannot be undone</strong>.',
    'Delete ' + count + ' User' + (count > 1 ? 's' : ''),
    async function() {
      var r = await _api('POST', '/admin/api/users/bulk-delete', { user_ids: ids });
      if (!r.ok) { showToast(r.data.error || 'Bulk delete failed', 'error'); return; }
      showToast((r.data.deleted || count) + ' user' + ((r.data.deleted || count) > 1 ? 's' : '') + ' deleted', 'success');
      clearBulkSelection();
      loadUsers();
    }
  );
}

function confirmDelete(uid, email) {
  showConfirm(
    '⚠️',
    'Delete user?',
    'Permanently delete <strong>' + email + '</strong> and all their data?<br>This action <strong>cannot be undone</strong>.',
    'Delete User',
    function() {
      _api('DELETE', '/admin/api/users/' + uid).then(function(r) {
        if (!r.ok) { showToast(r.data.error || 'Delete failed', 'error'); return; }
        showToast('User ' + email + ' deleted', 'success');
        loadUsers();
      });
    }
  );
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}

// ═══════════════════ Helpers ══════════════════
function planBadge(name) {
  var n = (name || '').toLowerCase();
  var classMap = { trial:'badge-trial', starter:'badge-starter', pro:'badge-pro',
    advanced:'badge-advanced', analyst:'badge-analyst', enterprise:'badge-enterprise' };
  var labelMap = { trial:'Trial', starter:'Starter', pro:'Pro',
    advanced:'Advanced', analyst:'Analyst', enterprise:'Enterprise' };
  var cls   = classMap[n] || 'badge-trial';
  var label = labelMap[n] || (name || '—');
  return '<span class="badge ' + cls + '">' + label + '</span>';
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showToast(msg, type) {
  var el = document.getElementById('toast');
  el.textContent = (type === 'success' ? '✅ ' : '❌ ') + msg;
  el.className = 'show ' + (type || '');
  clearTimeout(el._t);
  el._t = setTimeout(function() { el.classList.remove('show'); }, 3500);
}

// ── In-app confirm dialog ─────────────────────────────────────────────────────
var _confirmCallback = null;

function showConfirm(icon, title, msg, confirmLabel, onConfirm) {
  document.getElementById('confirm-icon').textContent    = icon;
  document.getElementById('confirm-title').textContent   = title;
  document.getElementById('confirm-msg').innerHTML       = msg;
  document.getElementById('confirm-ok-btn').textContent  = confirmLabel || 'Confirm';
  _confirmCallback = onConfirm;
  document.getElementById('confirm-overlay').classList.remove('hidden');
}

function _confirmOk() {
  document.getElementById('confirm-overlay').classList.add('hidden');
  if (typeof _confirmCallback === 'function') _confirmCallback();
  _confirmCallback = null;
}

function _confirmCancel() {
  document.getElementById('confirm-overlay').classList.add('hidden');
  _confirmCallback = null;
}

// ═══════════════════ Monitoring Charts ══════════════════════════════════
var _monRange              = '24h';
var _monCharts             = {};
var _monAutoRefreshInterval = null;

function setMonRange(range) {
  _monRange = range;
  ['24h', '7d', '30d'].forEach(function(r) {
    var btn = document.getElementById('range-' + r);
    if (btn) btn.classList.toggle('active', r === range);
  });
  loadMonitoring();
}

async function loadMonitoring() {
  var r = await _api('GET', '/admin/api/metrics?range=' + _monRange);
  if (!r.ok) { showToast('Failed to load monitoring metrics', 'error'); return; }

  var series = r.data.series || [];
  var latest = r.data.latest || {};

  var cpuEl   = document.getElementById('mon-cur-cpu');
  var ramEl   = document.getElementById('mon-cur-ram');
  var ramGbEl = document.getElementById('mon-cur-ram-gb');
  var connEl  = document.getElementById('mon-cur-conn');
  var usersEl = document.getElementById('mon-cur-users');
  if (cpuEl)   cpuEl.textContent   = latest.cpu_pct   != null ? latest.cpu_pct.toFixed(1)   + '%' : '—';
  if (ramEl)   ramEl.textContent   = latest.ram_pct   != null ? latest.ram_pct.toFixed(1)   + '%' : '—';
  if (ramGbEl && latest.ram_used_mb && latest.ram_total_mb) {
    ramGbEl.textContent = (latest.ram_used_mb / 1024).toFixed(1) + ' / '
                        + (latest.ram_total_mb / 1024).toFixed(1) + ' GB';
  } else if (ramGbEl) {
    ramGbEl.textContent = '—';
  }
  if (connEl)  connEl.textContent  = latest.http_connections   != null ? latest.http_connections   : '—';
  if (usersEl) usersEl.textContent = latest.active_users_today != null ? latest.active_users_today : '—';

  var luEl = document.getElementById('mon-last-update');
  if (luEl) luEl.textContent = ' · updated ' + new Date().toLocaleTimeString();

  var emptyEl = document.getElementById('mon-empty-state');
  var gridEl  = document.getElementById('mon-charts-grid');
  if (!series.length) {
    if (emptyEl) emptyEl.style.display = 'block';
    if (gridEl)  gridEl.style.display  = 'none';
    ['cpu','ram','conn','users'].forEach(function(k) {
      var el = document.getElementById('mon-peak-' + k);
      if (el) el.textContent = '—';
    });
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';
  if (gridEl)  gridEl.style.display  = '';

  function _peak(key) {
    return Math.max.apply(null, series.map(function(p) { return +(p[key] || 0); }));
  }
  var peakCpu   = _peak('cpu_pct');
  var peakRam   = _peak('ram_pct');
  var peakConn  = _peak('http_connections');
  var peakUsers = _peak('active_users_today');

  var pkCpu   = document.getElementById('mon-peak-cpu');
  var pkRam   = document.getElementById('mon-peak-ram');
  var pkConn  = document.getElementById('mon-peak-conn');
  var pkUsers = document.getElementById('mon-peak-users');
  if (pkCpu)   pkCpu.textContent   = 'Peak: ' + peakCpu.toFixed(1)   + '%';
  if (pkRam)   pkRam.textContent   = 'Peak: ' + peakRam.toFixed(1)   + '%';
  if (pkConn)  pkConn.textContent  = 'Peak: ' + Math.round(peakConn);
  if (pkUsers) pkUsers.textContent = 'Peak: ' + Math.round(peakUsers);

  var labels = series.map(function(p) {
    var d = new Date(p.ts);
    if (_monRange === '7d') {
      return d.toLocaleDateString([], {month:'short', day:'numeric'})
             + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    } else if (_monRange === '30d') {
      return d.toLocaleDateString([], {month:'short', day:'numeric'});
    } else {
      return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    }
  });

  _renderMonChart('chart-cpu',   labels, series.map(function(p) { return p.cpu_pct; }),
    'CPU %',        '#3fb950', 'rgba(63,185,80,0.12)',   100);
  _renderMonChart('chart-ram',   labels, series.map(function(p) { return p.ram_pct; }),
    'RAM %',        '#58a6ff', 'rgba(88,166,255,0.12)',  100);
  _renderMonChart('chart-conn',  labels, series.map(function(p) { return p.http_connections; }),
    'Connections',  '#f0a500', 'rgba(240,165,0,0.12)',   null);
  _renderMonChart('chart-users', labels, series.map(function(p) { return p.active_users_today; }),
    'Active Users', '#d2a8ff', 'rgba(210,168,255,0.12)', null);
}

function _renderMonChart(canvasId, labels, dataPoints, label, borderColor, bgColor, yMax) {
  var canvas = document.getElementById(canvasId);
  if (!canvas) return;
  if (_monCharts[canvasId]) {
    try { _monCharts[canvasId].destroy(); } catch(_) {}
    _monCharts[canvasId] = null;
  }
  var ctx = canvas.getContext('2d');
  var yConfig = {
    grid:   { color: 'rgba(48,54,61,0.7)', drawBorder: false },
    ticks:  { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 5 },
    border: { display: false },
    beginAtZero: true,
  };
  if (yMax != null) { yConfig.max = yMax; yConfig.suggestedMax = yMax; }

  _monCharts[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: label, data: dataPoints,
        borderColor: borderColor, backgroundColor: bgColor,
        borderWidth: 1.5, fill: true, tension: 0.35,
        pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: borderColor,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(22,27,34,0.96)', borderColor: '#30363d', borderWidth: 1,
          titleColor: '#8b949e', bodyColor: '#e6edf3',
          titleFont: { size: 11 }, bodyFont: { size: 12, weight: 'bold' },
          padding: 10,
          callbacks: {
            title: function(items) { return items.length ? items[0].label : ''; },
            label: function(item) {
              var v = item.raw;
              if (v == null) return label + ': —';
              if (label.includes('%')) return label + ': ' + (+v).toFixed(1) + '%';
              return label + ': ' + Math.round(v);
            },
          },
        },
      },
      scales: {
        x: {
          type: 'category',
          grid:   { color: 'rgba(48,54,61,0.5)', drawBorder: false },
          ticks:  { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 7, maxRotation: 0, autoSkip: true },
          border: { display: false },
        },
        y: yConfig,
      },
    },
  });
}

function _startMonAutoRefresh() {
  _stopMonAutoRefresh();
  _monAutoRefreshInterval = setInterval(function() {
    if (_currentPage === 'monitoring') loadMonitoring();
  }, 60000);
}

function _stopMonAutoRefresh() {
  if (_monAutoRefreshInterval) {
    clearInterval(_monAutoRefreshInterval);
    _monAutoRefreshInterval = null;
  }
}

// ═══════════════════ Event listeners (replaces all inline onclick/onchange/oninput) ═══════════════════

// Login
document.getElementById('login-btn').addEventListener('click', doLogin);
document.getElementById('login-email').addEventListener('keydown', function(e) { if (e.key === 'Enter') doLogin(); });
document.getElementById('login-password').addEventListener('keydown', function(e) { if (e.key === 'Enter') doLogin(); });

// Sidebar nav
document.getElementById('nav-dashboard').addEventListener('click', function() { showPage('dashboard'); });
document.getElementById('nav-users').addEventListener('click', function() { showPage('users'); });
document.getElementById('nav-plans').addEventListener('click', function() { showPage('plans'); });
document.getElementById('nav-audit').addEventListener('click', function() { showPage('audit'); });
document.getElementById('nav-monitoring').addEventListener('click', function() { showPage('monitoring'); });

// Topbar
document.getElementById('logout-btn').addEventListener('click', doLogout);
document.getElementById('refresh-btn').addEventListener('click', refreshCurrentPage);
document.getElementById('header-new-user-btn').addEventListener('click', function() { showPage('users'); openCreateUserModal(); });

// Users page controls
document.getElementById('user-search').addEventListener('input', filterUsers);
document.getElementById('clear-filters-btn').addEventListener('click', clearAllFilters);
document.getElementById('new-user-btn').addEventListener('click', openCreateUserModal);
document.getElementById('bulk-delete-btn').addEventListener('click', bulkDeleteSelected);
document.getElementById('clear-bulk-btn').addEventListener('click', clearBulkSelection);
document.getElementById('chk-all').addEventListener('change', function() { toggleSelectAll(this); });
document.getElementById('col-filter-plan').addEventListener('change', function() { filterUsers(); updateColFilterStyle(this); });
document.getElementById('col-filter-status').addEventListener('change', function() { filterUsers(); updateColFilterStyle(this); });
document.getElementById('col-filter-verified').addEventListener('change', function() { filterUsers(); updateColFilterStyle(this); });

// User table — event delegation replaces per-row onclick/onchange attributes
document.getElementById('users-tbody').addEventListener('click', function(e) {
  var btn = e.target.closest('button[data-action]');
  if (!btn) return;
  var a = btn.dataset.action, uid = btn.dataset.uid, email = btn.dataset.email;
  if (a === 'set-runs')      openSetRunsModal(uid, email, parseInt(btn.dataset.runs) || 0);
  else if (a === 'edit')     openEditModalRaw(btn.dataset.user);
  else if (a === 'extend-trial') openExtendTrialModal(uid);
  else if (a === 'delete')   confirmDelete(uid, email);
});
document.getElementById('users-tbody').addEventListener('change', function(e) {
  if (e.target.classList.contains('user-chk')) updateBulkBar();
});

// Monitoring range buttons
document.getElementById('range-24h').addEventListener('click', function() { setMonRange('24h'); });
document.getElementById('range-7d').addEventListener('click', function() { setMonRange('7d'); });
document.getElementById('range-30d').addEventListener('click', function() { setMonRange('30d'); });

// Edit user modal
document.getElementById('eu-email-btn').addEventListener('click', toggleEmailVerification);
document.getElementById('eu-cancel-btn').addEventListener('click', function() { closeModal('edit-user-modal'); });
document.getElementById('eu-save-btn').addEventListener('click', saveUserEdit);

// Set runs modal
document.getElementById('sr-cancel-btn').addEventListener('click', function() { closeModal('set-runs-modal'); });
document.getElementById('sr-save-btn').addEventListener('click', saveRunCount);

// Create user modal
document.getElementById('cu-cancel-btn').addEventListener('click', function() { closeModal('create-user-modal'); });
document.getElementById('cu-save-btn').addEventListener('click', createUser);

// Extend trial modal
document.getElementById('et-cancel-btn').addEventListener('click', function() { closeModal('extend-trial-modal'); });
document.getElementById('et-save-btn').addEventListener('click', doExtendTrial);

// Confirm overlay
document.getElementById('confirm-overlay').addEventListener('click', function(e) { if (e.target === this) _confirmCancel(); });
document.getElementById('confirm-cancel-btn').addEventListener('click', _confirmCancel);
document.getElementById('confirm-ok-btn').addEventListener('click', _confirmOk);

// All modal overlays — close on background click
document.querySelectorAll('.modal-overlay').forEach(function(el) {
  el.addEventListener('click', function(e) { if (e.target === el) el.classList.add('hidden'); });
});

// ESC key dismisses confirm dialog
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') _confirmCancel(); });

// Bootstrap — check existing session on page load
checkSession();
