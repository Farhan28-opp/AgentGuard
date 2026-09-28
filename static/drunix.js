/* Drunix Ledger page. Everything shown comes from /drunix/* responses; no
 * status is assumed — "connected" is only shown when the bridge answered a
 * real query against the deployed chaincode. */
(function () {
    const $ = (id) => document.getElementById(id);
    const short = (s, n = 12) => s ? (s.length > n ? s.slice(0, n) + '…' : s) : '—';
    const pill = (text, cls) => `<span class="pill ${cls}">${escapeHtml(text)}</span>`;
    const OUTCOME_CLS = {VALID: 'ok', SYNCED: 'ok', RECOVERED: 'ok', REJECTED: 'bad', CHAINCODE_REJECTED: 'bad',
        CONFLICT: 'warn', MVCC_READ_CONFLICT: 'warn', INVALID_COMMIT: 'bad', DRUNIX_INVALID_COMMIT: 'bad',
        UNAVAILABLE: 'bad', DRUNIX_UNAVAILABLE: 'bad', TIMEOUT: 'warn', DRUNIX_TIMEOUT: 'warn',
        SYNC_PENDING: 'warn', NOT_ON_LEDGER: 'muted'};
    const outcome = (o) => pill(o, OUTCOME_CLS[o] || 'muted');
    const time = (iso) => new Date(iso).toLocaleTimeString('en-IN', {hour12: false});
    let scenarios = [];

    function stat(k, v, cls = '') {
        return `<div class="drunix-stat"><div class="k">${escapeHtml(k)}</div><div class="v ${cls}">${v}</div></div>`;
    }

    async function loadStatus() {
        const r = await apiCall('/drunix/status');
        if (!r.ok) { $('dx-status').innerHTML = `<div class="notice bad">${escapeHtml(r.message)}</div>`; return; }
        const s = r.data;
        const enforce = s.mode === 'enforce';
        const lat = s.latency_24h || {};
        $('dx-status').innerHTML = [
            stat('Mode', enforce ? 'ENFORCE' : 'OFF', enforce ? 'ok' : 'warn'),
            stat('Drunix network', s.connected ? 'Connected' : escapeHtml(s.network === 'reachable' ? 'Reachable' : 'Not connected'),
                s.connected ? 'ok' : 'bad'),
            stat('Bridge', escapeHtml(s.bridge === 'ok' ? 'Up' : s.bridge), s.bridge === 'ok' ? 'ok' : 'bad'),
            stat('Chaincode', escapeHtml(s.chaincode_status === 'ready'
                ? `${s.chaincode || 'agentauth'} ${(s.contract && s.contract.version) || ''}` : s.chaincode_status),
                s.chaincode_status === 'ready' ? 'ok' : 'bad'),
            stat('Channel · peer', escapeHtml(`${s.channel || '—'} · ${(s.peer_endpoint || '—').replace('dns:///', '')}`)),
            stat('Client MSP', escapeHtml(s.msp_id || '—')),
            stat('Avg commit latency (24 h)', lat.avg_ms != null ? `${Math.round(lat.avg_ms)} ms` : '—'),
            stat('VALID transactions', String((s.journal || {}).VALID || 0)),
            stat('Rejected by Drunix', String((s.journal || {}).REJECTED || 0), (s.journal || {}).REJECTED ? 'warn' : ''),
            stat('Sync pending', String(s.sync_pending), s.sync_pending ? 'warn' : 'ok'),
        ].join('');
        $('dx-status-note').textContent = s.health_error ? `${s.health_error.category || ''} ${s.health_error.message || ''}` : '';
        // Header chip: the same observed mode + connection, nothing assumed.
        $('dx-mode').innerHTML = `<span class="dot-live ${enforce && s.connected ? '' : enforce ? 'bad' : 'off'}"></span>`
            + (enforce ? `Enforcing on Drunix · ${s.connected ? 'connected' : 'not connected'}` : 'Drunix enforcement off · DRUNIX_MODE=off');
        $('dx-updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', {hour12: false});
    }

    // Only a transaction that reached a block has a ledger position. A request
    // the chaincode refused at endorsement was never ordered.
    function txCell(t) {
        if (t.block_number != null) return `${escapeHtml(short(t.tx_id, 14))} · #${t.block_number}${['VALID', 'SYNCED', 'RECOVERED'].includes(t.outcome) ? '' : ' (invalid)'}`;
        if (t.outcome === 'REJECTED' || t.outcome === 'NOT_ON_LEDGER') return '<span class="meta">refused at endorsement · not ordered</span>';
        if (t.outcome === 'RECOVERED') return escapeHtml(short(t.tx_id, 14)) + ' · recovered';
        return '—';
    }

    async function loadTransactions() {
        const r = await apiCall('/drunix/transactions?limit=30' + ($('tx-lab').checked ? '' : '&source=agentguard'));
        if (!r.ok) return;
        const rows = r.data.transactions;
        $('tx-body').innerHTML = rows.length ? rows.map(t => `<tr>
            <td class="mono small">${escapeHtml(time(t.created_at))}</td>
            <td><span class="mono">${escapeHtml(t.function)}</span>${t.source === 'security-lab' ? ' <span class="tag">lab</span>' : ''}</td>
            <td class="mono small" title="${escapeHtml(t.entity_id)}">${escapeHtml(t.entity_type)} ${escapeHtml(short(t.entity_id, 14))}</td>
            <td>${outcome(t.outcome)}</td>
            <td class="mono small" title="${escapeHtml(t.tx_id || '')}">${txCell(t)}</td>
            <td class="small">${t.code ? `<span class="mono">${escapeHtml(t.code)}</span> ` : ''}${escapeHtml(short(t.message || '', 90))}</td>
            <td class="r mono small">${t.latency_ms ?? ''}</td></tr>`).join('')
            : '<tr><td colspan="7" class="meta">No Drunix transactions yet. Set DRUNIX_MODE=enforce and run a purchase, or run the Security Lab.</td></tr>';
        const pending = rows.filter(t => t.outcome === 'SYNC_PENDING');
        const p = await apiCall('/drunix/transactions?limit=50&outcome=SYNC_PENDING');
        const list = p.ok ? p.data.transactions : pending;
        $('sync-body').innerHTML = list.length ? list.map(t => `<tr><td class="mono small">${escapeHtml(time(t.created_at))}</td>
            <td class="mono">${escapeHtml(t.function)}</td><td class="mono small">${escapeHtml(short(t.entity_id, 18))}</td>
            <td class="mono">${t.attempts}</td><td class="small">${escapeHtml(t.category || '')} ${escapeHtml(short(t.message || '', 80))}</td></tr>`).join('')
            : '<tr><td colspan="5" class="meta">Nothing pending — every restrictive operation is confirmed on Drunix.</td></tr>';
    }

    // ── Latest agent payment, traced through every check ──────────────
    // Read-only view of existing endpoints: the latest order, its receipt
    // (authority + Drunix trail) and its task record (checks + risk).
    let chainRef = null;
    async function loadChain() {
        const box = $('dx-chain');
        const r = await apiCall('/product/orders?limit=1');
        if (!r.ok) { box.innerHTML = `<div class="notice bad">${escapeHtml(r.message)}</div>`; return; }
        if (!r.data.length) {
            chainRef = null;
            box.innerHTML = `<div class="empty" style="padding:8px 0"><p class="h4">No agent payment yet</p>
                <p class="muted mt-2">Ask the AI Agent to buy something. Its request, checks, Drunix reserve and commit, and receipt will be traced here.</p>
                <p class="mt-4"><a class="btn btn-secondary btn-sm" href="/agent">Open AI Agent</a></p></div>`;
            $('dx-chain-links').innerHTML = '';
            return;
        }
        const o = r.data[0];
        if (o.order_number === chainRef) return;
        const [od, tk] = await Promise.all([
            apiCall('/product/orders/' + encodeURIComponent(o.order_number)),
            o.task_id ? apiCall('/product/agent/tasks/' + encodeURIComponent(o.task_id)) : Promise.resolve({ok: false}),
        ]);
        if (!od.ok) { box.innerHTML = `<div class="notice bad">${escapeHtml(od.message)}</div>`; return; }
        chainRef = o.order_number;
        renderChain(od.data, tk.ok ? tk.data : null);
    }

    function renderChain(o, t) {
        const a = (t && t.authorization) || {};
        const checks = a.checks || {};
        const risk = a.risk || (t && t.result && t.result.risk) || {};
        const au = o.authority || {};
        const dx = o.drunix || {};
        const RISK = {ALLOW: 'ok', REVIEW: 'warn', CONTAIN: 'bad'};
        const na = '<span class="pill muted">not recorded</span>';
        const txLine = (tx) => tx ? `tx <span class="mono" title="${escapeHtml(tx.tx_id)}">${escapeHtml(short(tx.tx_id, 16))}</span>`
            + (tx.block_number != null ? ` · block ${tx.block_number}` : ' · recovered') + (tx.latency_ms != null ? ` · ${tx.latency_ms} ms` : '') : '';
        const offLedger = '<span class="pill muted">NOT ON LEDGER</span>';
        const step = (layer, what, detail, status, dxStep) => `<li class="chain-step${dxStep ? ' dx-step' : ''}">
            <span class="who-layer${dxStep ? ' dx' : ''}">${layer}</span>
            <span class="what">${what}${detail ? `<span class="detail">${detail}</span>` : ''}</span>
            ${status}</li>`;
        const signed = (o.security_trail && o.security_trail.signed_operations) || [];
        const approval = o.approval === 'user' ? 'Authorized by you' : `Approved by ${escapeHtml(o.approval)}`;
        const steps = [
            step('Agent', 'Payment request', `${t ? '“' + escapeHtml(t.instruction) + '” · ' : ''}signed by <span class="mono">${escapeHtml(o.agent || '—')}</span>`,
                signed.length ? '<span class="pill ok">SIGNED</span>' : na),
            step('AgentGuard', 'Identity verification', a.agent_key_fingerprint ? `Ed25519 key <span class="mono">${escapeHtml(a.agent_key_fingerprint)}</span> · signature, nonce, timestamp, payload hash` : 'Ed25519 signature, nonce, timestamp, payload hash',
                checks.identity ? `<span class="pill ${checks.identity === 'VERIFIED' && checks.signature === 'VERIFIED' ? 'ok' : 'bad'}">${escapeHtml(checks.signature || checks.identity)}</span>` : na),
            step('AgentGuard', 'Authority verification', `${fmtINR(au.agent_authority)} delegated to the Purchase Agent · capability <span class="mono">${escapeHtml(short(o.security_trail && o.security_trail.capability_id, 10))}</span>`
                + (dx.delegate ? `<br>Drunix Delegate ${txLine(dx.delegate)}` : ''),
                dx.delegate ? '<span class="pill ok">VALID</span>' : (au.agent_authority ? '<span class="pill ok">DELEGATED</span>' : na)),
            step('AgentGuard', 'Policy validation', 'Amount, merchant and category checked against your agent policy',
                checks.policy ? `<span class="pill ${checks.policy === 'WITHIN LIMIT' ? 'ok' : 'bad'}">${escapeHtml(checks.policy)}</span>` : na),
            step('AgentGuard', 'Risk assessment', risk.level ? `IsolationForest ${escapeHtml(risk.level)} · score ${Number(risk.anomaly_score).toFixed(4)}${risk.engine ? ' · ' + escapeHtml(risk.engine) : ''}` : 'Behavioural model',
                risk.action ? `<span class="pill ${RISK[risk.action] || 'muted'}">${escapeHtml(risk.action)}</span>` : na),
            step('Drunix', 'Authority reservation', (dx.reserve ? `agentauth.Reserve ${txLine(dx.reserve)}` : 'Held in AgentGuard only — not enforced on Drunix for this order')
                + ` · reservation <span class="mono">${escapeHtml(short(o.security_trail && o.security_trail.reservation_id, 10))}</span>`,
                dx.reserve ? `<span class="pill ok">${escapeHtml(dx.reserve.status)}</span>` : offLedger, true),
            step('You', 'User authorization', approval, `<span class="pill ok">${o.approval === 'user' ? 'AUTHORIZED' : 'AUTO-APPROVED'}</span>`),
            step('Drunix', 'Commitment', dx.commit ? `agentauth.Commit ${txLine(dx.commit)} · the payment is recorded only after this is VALID` : 'Committed in AgentGuard only — not enforced on Drunix for this order',
                dx.commit ? `<span class="pill ok">${escapeHtml(dx.commit.status)}</span>` : offLedger, true),
            step('AgentGuard', 'Payment execution', `${fmtINR(o.total)} to ${escapeHtml(o.merchant)} · UTR <span class="mono">${escapeHtml(o.utr_reference || '—')}</span> · simulated rail`,
                o.payment_status ? `<span class="pill ok">${escapeHtml(o.payment_status)}</span>` : na),
            step('AgentGuard', 'Receipt and audit', `Order <a href="/orders/${encodeURIComponent(o.order_number)}">${escapeHtml(o.order_number)}</a> · signed operations ${signed.map(escapeHtml).join(' → ') || '—'}`
                + (dx.return_unused ? `<br>Unused authority returned · Drunix ReturnUnused ${txLine(dx.return_unused)}` : ''),
                `<span class="pill ok">${escapeHtml(o.status)}</span>`),
        ];
        const when = new Date(o.created_at).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'});
        $('dx-chain').innerHTML = `
            <div class="chain-summary">
                <div><div class="k">Amount</div><div class="v num">${fmtINR(o.total)}</div></div>
                <div><div class="k">Merchant</div><div class="v sm">${escapeHtml(o.merchant)} <span class="tag">Simulated</span></div></div>
                <div><div class="k">Agent</div><div class="v sm mono">${escapeHtml(o.agent || '—')}</div></div>
                <div><div class="k">Drunix enforcement</div><div class="v sm">${dx.enforced ? '<span class="badge ok">Reserve + Commit VALID</span>' : '<span class="badge plain">Not enforced (mode off)</span>'}</div></div>
            </div>
            <ol class="chain">${steps.join('')}</ol>
            <p class="meta mt-3">${escapeHtml(when)} · order ${escapeHtml(o.order_number)}. Simulated payment rail — no real money moved.</p>`;
        $('dx-chain-links').innerHTML = `<a class="link" href="/orders/${encodeURIComponent(o.order_number)}">Receipt</a>
            <a class="link" href="/security?capability=${encodeURIComponent(o.security_trail.capability_id)}">Security trail</a>`;
    }

    // ── Latest direct payment (recharge / bill / send money) ───────────
    let directRef = null;
    async function loadDirect() {
        const box = $('dx-direct');
        const r = await apiCall('/product/transactions?kind=direct&limit=20');
        if (!r.ok) { box.innerHTML = `<div class="notice bad">${escapeHtml(r.message)}</div>`; return; }
        const t = r.data.transactions.find(x => x.status === 'COMPLETED');
        if (!t) {
            directRef = null;
            box.innerHTML = `<p class="muted">No completed direct payment yet. <a href="/payments">Pay a bill or recharge</a> — its single-use
                mandate, Drunix Reserve and Commit will be traced here.</p>`;
            $('dx-direct-links').innerHTML = '';
            return;
        }
        if (t.id === directRef) return;
        const d = await apiCall('/product/transactions/' + encodeURIComponent(t.id));
        if (!d.ok) { box.innerHTML = `<div class="notice bad">${escapeHtml(d.message)}</div>`; return; }
        directRef = t.id;
        box.innerHTML = receiptHTML(d.data);
        $('dx-direct-links').innerHTML = `<a class="link" href="/transactions/${encodeURIComponent(t.id)}">Receipt</a>`;
    }

    const fmtVals = (o) => o ? Object.entries(o).map(([k, v]) => `${k} ${v}`).join(' · ') : '—';
    async function loadReconciliation() {
        const r = await apiCall('/drunix/reconciliation?limit=8');
        if (!r.ok) { $('rc-body').innerHTML = `<tr><td colspan="5" class="meta">${escapeHtml(r.message)}</td></tr>`; return; }
        const cls = {MATCH: 'ok', MISMATCH: 'bad', NOT_ON_LEDGER: 'muted', UNREACHABLE: 'bad'};
        $('rc-body').innerHTML = r.data.rows.length ? r.data.rows.map(x => `<tr>
            <td>${escapeHtml(x.kind)}</td>
            <td class="mono small" title="${escapeHtml(x.id)}">${escapeHtml(short(x.id, 10))} <span class="meta">${escapeHtml(short(x.label, 28))}</span></td>
            <td>${pill(x.state, cls[x.state] || 'muted')}${x.diff && x.diff.length ? ` <span class="small bad-text">${escapeHtml(x.diff.join(', '))}</span>` : ''}</td>
            <td class="mono small">${escapeHtml(fmtVals(x.postgres))}</td>
            <td class="mono small">${escapeHtml(fmtVals(x.drunix))}</td></tr>`).join('')
            : '<tr><td colspan="5" class="meta">Nothing to reconcile yet.</td></tr>';
    }

    function renderScenario(sc, result, running) {
        const el = $('lab-' + sc.id);
        const steps = result && result.steps ? `<ol class="result">${result.steps.map(s => `<li>${outcome(s.outcome)}
            <span class="mono">${escapeHtml(s.function)}</span> ${escapeHtml(s.note || '')}
            ${s.code ? `<br><span class="mono small bad-text">${escapeHtml(s.code)}</span> <span class="small">${escapeHtml(short(s.message || '', 140))}</span>` : ''}
            ${s.block_number != null ? `<br><span class="mono small meta">tx ${escapeHtml(short(s.tx_id || '', 18))} · block ${s.block_number}${s.outcome === 'VALID' ? '' : ' (committed as invalid)'}</span>`
                : s.outcome === 'CHAINCODE_REJECTED' ? '<br><span class="small meta">refused by the endorsing peers — never ordered</span>' : ''}</li>`).join('')}</ol>` : '';
        const pools = result && result.pool_before ? `<p class="mono small meta">pool before: ${escapeHtml(fmtVals(result.pool_before))}<br>pool after: ${escapeHtml(fmtVals(result.pool_after))}</p>` : '';
        el.innerHTML = `<div class="split"><b>${escapeHtml(sc.title)}</b>
                <button class="btn btn-sm" type="button" data-run="${sc.id}" ${running ? 'disabled' : ''}>${running ? 'Running on Drunix…' : 'Run attack'}</button></div>
            <p class="small"><b>Attack:</b> ${escapeHtml(sc.attack)}</p>
            <p class="small"><b>Expected:</b> ${escapeHtml(sc.expected)}</p>
            ${result && result.verdict ? `<p class="verdict ${result.verdict === 'PASS' ? 'ok' : 'bad'}">${result.verdict === 'PASS' ? '✓ Drunix blocked the attack' : '✗ Unexpected result'} — ${escapeHtml(result.explanation)}</p>` : ''}
            ${result && result.error ? `<p class="bad-text small">${escapeHtml(result.error)}</p>` : ''}
            ${steps}${pools}`;
        el.querySelector('[data-run]').onclick = () => run(sc);
    }

    async function run(sc) {
        renderScenario(sc, null, true);
        const r = await postJSON('/drunix/lab/' + sc.id);
        renderScenario(sc, r.ok ? r.data : {error: r.message}, false);
        loadTransactions(); loadStatus();
        return r.ok && r.data.verdict === 'PASS';
    }

    async function loadScenarios() {
        const r = await apiCall('/drunix/lab/scenarios');
        if (!r.ok) return;
        scenarios = r.data.scenarios;
        $('lab-grid').innerHTML = scenarios.map(sc => `<div class="lab-card" id="lab-${sc.id}"></div>`).join('');
        scenarios.forEach(sc => renderScenario(sc, null, false));
    }

    $('lab-run-all').onclick = async () => {
        $('lab-run-all').disabled = true;
        let passed = 0;
        for (const sc of scenarios) { if (await run(sc)) passed++; }
        $('lab-run-all').disabled = false;
        showToast(`${passed}/${scenarios.length} attacks blocked by Drunix`, passed === scenarios.length ? 'success' : 'error');
    };
    $('lab-setup').onclick = async () => {
        $('lab-setup').disabled = true;
        const r = await postJSON('/drunix/lab/setup');
        $('lab-setup').disabled = false;
        showToast(r.ok && r.data.ready ? 'Lab authority created on Drunix' : (r.message || 'Lab setup failed'), r.ok && r.data.ready ? 'success' : 'error');
        loadTransactions();
    };
    $('rc-refresh').onclick = loadReconciliation;
    $('sync-retry').onclick = async () => {
        const r = await postJSON('/drunix/sync/retry');
        showToast(r.ok ? `Retried ${r.data.attempted}: ${r.data.synced} synced, ${r.data.still_pending} still pending` : r.message,
            r.ok ? 'success' : 'error');
        loadTransactions();
    };
    $('tx-lab').onchange = loadTransactions;

    loadScenarios();
    loadStatus(); loadTransactions(); loadReconciliation(); loadChain(); loadDirect();
    setInterval(() => { loadStatus(); loadTransactions(); }, 4000);
    setInterval(() => { if (!document.hidden) { loadChain(); loadDirect(); } }, 10000);
    setInterval(loadReconciliation, 15000);
})();
