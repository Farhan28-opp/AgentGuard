/* AgentGuard Security Center — technical view. Everything rendered here is
 * read from the backend (/product/security/summary); nothing is synthesised. */
(function () {
    const POLL_MS = 2000;
    const $ = (id) => document.getElementById(id);
    let lastJson = '';
    let capsById = {};

    const shortId = (id) => escapeHtml(String(id || '').slice(0, 8)) + '…';
    const time = (iso) => new Date(iso).toLocaleTimeString('en-IN', {hour12: false});

    async function refresh() {
        const r = await apiCall('/product/security/summary');
        if (!r.ok) {
            $('sc-updated').textContent = 'Update failed: ' + r.message;
            $('sc-updated').style.color = 'var(--danger)';
            $('sc-live').className = 'dot-live bad';
            return;
        }
        $('sc-updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN');
        $('sc-updated').style.color = '';
        $('sc-live').className = 'dot-live';
        const json = JSON.stringify(r.data);
        if (json === lastJson) return;
        lastJson = json;
        const d = r.data;
        renderSummary(d.summary);
        renderPolicy(d.policy);
        renderDistribution(d.summary, d.policy);
        renderOrders(d.orders);
        renderCrypto(d.crypto);
        renderRisk(d.risk);
        renderTree(d.capabilities);
        renderReservations(d.reservations);
        renderAgents(d.agents);
        renderPayments(d.payments);
        renderEvents(d.events);
    }

    function renderSummary(s) {
        $('stat-agents').textContent = s.active_agents ?? 0;
        $('stat-capabilities').textContent = s.active_capabilities ?? 0;
        $('stat-reservations').textContent = s.active_reservations ?? 0;
        $('stat-reserved').textContent = fmtINR(s.reserved_authority);
        $('stat-committed').textContent = fmtINR(s.committed_authority);
        $('stat-contained').textContent = s.containments ?? 0;
        $('stat-revoked').textContent = s.revoked_capabilities ?? 0;
    }

    function renderPolicy(p) {
        if (!p) return;
        const agents = p.agents.map(a => `<div class="kv-row"><span>${escapeHtml(a.role)}</span><span>
            <span class="mono">${escapeHtml(a.agent_identifier || '—')}</span>
            <b class="${a.status === 'active' ? 'ok-text' : 'bad-text'}">${escapeHtml(a.status)}</b>
            ${a.standing_authority !== null ? ' · ' + fmtINR(a.standing_authority) : (a.note ? ' · ' + escapeHtml(a.note) : '')}</span></div>`).join('');
        $('policy-panel').innerHTML = `
            <div class="kv-row"><span>Overall authority</span><span>${fmtINR(p.overall_authority)} · ${escapeHtml(p.enforcement.overall_authority)}</span></div>
            <div class="kv-row"><span>Per purchase</span><span>${fmtINR(p.per_transaction_limit)}</span></div>
            <div class="kv-row"><span>Categories</span><span>${p.allowed_categories.map(escapeHtml).join(', ')}</span></div>
            <div class="kv-row"><span>Merchants</span><span>${p.allowed_merchants.map(escapeHtml).join(', ')}</span></div>
            <div class="kv-row"><span>Approval</span><span>${escapeHtml(p.approval_mode)}${p.approval_mode === 'above_threshold' ? ' > ' + fmtINR(p.approval_threshold) : ''}</span></div>
            <div class="kv-row"><span>Main Agent pool</span><span>${fmtINR(p.standing.main_unallocated)} unallocated · ${fmtINR(p.standing.main_delegated)} delegated</span></div>
            ${agents}`;
    }

    // Authority distribution — a view of numbers already in the summary (no new data).
    function renderDistribution(s, p) {
        if (!p || !p.standing) return;
        const total = Number(p.overall_authority) || 0;
        const un = Number(p.standing.main_unallocated) || 0;
        const del = Number(p.standing.main_delegated) || 0;
        const pct = (v) => total > 0 ? Math.max(0, Math.min(100, v / total * 100)) : 0;
        $('dist-panel').innerHTML = `
            <div class="w-grid">
                <div>
                    <div class="w-sub"><span>Main Agent standing authority</span><span class="meta">${fmtINR(total)} mandate</span></div>
                    <div class="dist"><div class="dist-bar" role="img" aria-label="Main Agent: ${fmtINR(un)} unallocated, ${fmtINR(del)} delegated">
                        <span class="d-unalloc" style="width:${pct(un)}%"></span><span class="d-deleg" style="width:${pct(del)}%"></span></div></div>
                    <div class="dist-legend">
                        <div><span><i style="background:var(--ink)"></i>Unallocated with the Main Agent</span><span class="num">${fmtINR(un)}</span></div>
                        <div><span><i style="background:#8fa3cf"></i>Delegated to sub-agents</span><span class="num">${fmtINR(del)}</span></div>
                    </div>
                </div>
                <div>
                    <div class="w-sub"><span>Holds and payments</span><span class="meta">all capabilities</span></div>
                    <div class="dist-legend" style="margin-top:0;border-top:0">
                        <div><span><i style="background:var(--warn)"></i>Reserved — held, not yet paid</span><span class="num">${fmtINR(s.reserved_authority)}</span></div>
                        <div><span><i style="background:var(--ok)"></i>Committed — paid</span><span class="num">${fmtINR(s.committed_authority)}</span></div>
                        <div><span><i style="background:var(--line-strong)"></i>Active capabilities · revoked</span><span class="num">${s.active_capabilities ?? 0} · ${s.revoked_capabilities ?? 0}</span></div>
                        <div><span><i style="background:var(--bad)"></i>Containments</span><span class="num">${s.containments ?? 0}</span></div>
                    </div>
                </div>
            </div>`;
    }

    function renderOrders(rows) {
        $('orders-table').innerHTML = (rows || []).map(o => `
            <tr><td><a href="/orders/${encodeURIComponent(o.order_number)}">${escapeHtml(o.order_number)}</a></td>
                <td>${escapeHtml(o.merchant)}</td><td>${fmtINR(o.total)}</td>
                <td>${o.approval === 'user' ? 'user' : escapeHtml(o.approval)}</td><td class="mono">${time(o.created_at)}</td></tr>`).join('')
            || '<tr><td colspan="5" class="muted">No orders yet.</td></tr>';
    }

    function renderCrypto(c) {
        const ops = Object.entries(c.verified_signed_requests || {}).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(' · ') || 'none yet';
        const grants = c.grant_signatures_invalid
            ? `<span class="bad-text">${c.grant_signatures_valid} valid · ${c.grant_signatures_invalid} INVALID</span>`
            : `<span class="ok-text">${c.grant_signatures_valid}/${c.grant_signatures_checked} valid</span>`;
        $('crypto-panel').innerHTML = `
            <div class="kv-row"><span>Algorithm</span><span>${escapeHtml(c.algorithm)} (per-agent keypairs)</span></div>
            <div class="kv-row"><span>Agents with registered public key</span><span>${c.agents_with_registered_keys} / ${c.agents_total}</span></div>
            <div class="kv-row"><span>Verified signed requests</span><span>${c.verified_signed_requests_total} (${ops})</span></div>
            <div class="kv-row"><span>Rejected signed requests</span><span class="${c.rejected_signed_requests ? 'bad-text' : ''}">${c.rejected_signed_requests}</span></div>
            <div class="kv-row"><span>Capability grant signatures (re-verified now)</span><span>${grants}</span></div>
            <p class="fine-print">Verified = anti-replay nonce stored, which only happens after signature, timestamp window and payload hash all check out.</p>`;
    }

    function renderRisk(r) {
        const rows = (r.recent || []).map(e => `
            <div class="risk-row">
                <span class="mono">${time(e.timestamp)}</span>
                <b class="${e.level === 'HIGH' ? 'bad-text' : e.level === 'MEDIUM' ? 'warn-text' : 'ok-text'}">${escapeHtml(e.level || e.event_type)}</b>
                ${e.anomaly_score !== null && e.anomaly_score !== undefined ? `score ${Number(e.anomaly_score).toFixed(4)}` : ''}
                ${e.amount ? '· ' + fmtINR(e.amount) : ''}
                ${e.simulation ? '<span class="sim-tag">SIMULATION</span>' : ''}
                ${(e.reasons || []).length ? `<div class="muted" >${e.reasons.map(escapeHtml).join('; ')}</div>` : ''}
            </div>`).join('');
        $('risk-panel').innerHTML = `
            <div class="kv">
                <div class="kv-row"><span>Model</span><span>${escapeHtml(r.model)}</span></div>
                <div class="kv-row"><span>Thresholds (anomaly score)</span><span>MEDIUM ≥ ${r.thresholds.medium} · HIGH ≥ ${r.thresholds.high}</span></div>
                <div class="kv-row"><span>Normal hours feature</span><span>${escapeHtml(r.normal_hours)}</span></div>
                <div class="kv-row"><span>Order of checks</span><span>signature → hard authority rules → model</span></div>
            </div>
            <div class="mt-3">${rows || '<div class="muted" >No risk evaluations yet.</div>'}</div>`;
    }

    function renderTree(caps) {
        const container = $('capability-tree');
        capsById = Object.fromEntries(caps.map(c => [c.id, c]));
        if (!caps.length) { container.innerHTML = '<div class="muted" >No capabilities yet. Run the AI Agent or Initialize the demo.</div>'; return; }
        const hasKids = new Set(caps.filter(c => c.parent_id).map(c => c.parent_id));
        // Delegation trees first (standing agent authority, Security Lab), then single grants.
        const roots = caps.filter(c => !c.parent_id)
            .sort((a, b) => (hasKids.has(b.id) - hasKids.has(a.id)) || (b.status === 'ACTIVE') - (a.status === 'ACTIVE'))
            .slice(0, 12);
        const html = [];
        const walk = (cap, depth) => {
            let cls = 'status-active';
            if (cap.status === 'REVOKED') cls = 'status-revoked';
            else if (cap.status === 'EXHAUSTED') cls = 'status-committed';
            else if (Number(cap.reserved_authority) > 0) cls = 'status-reserved';
            html.push(`
                <div class="tree-node" data-cap="${escapeHtml(cap.id)}" style="--depth:${Math.min(depth, 4)}" tabindex="0" role="button" aria-label="Capability ${escapeHtml(cap.agent_identifier)} details">
                    <div class="node-header">
                        <span class="node-agent">${escapeHtml(cap.agent_identifier)}</span>
                        <span class="node-status ${cls}">${escapeHtml(cap.status)}</span>
                    </div>
                    <div class="node-auth">
                        <span>Total ${fmtINR(cap.total_authority)}</span>
                        <span>Unallocated ${fmtINR(cap.unallocated_authority)}</span>
                        <span class="warn-text">Reserved ${fmtINR(cap.reserved_authority)}</span>
                        <span >Committed ${fmtINR(cap.committed_authority)}</span>
                        <span class="${cap.grant_signature_valid ? 'ok-text' : 'bad-text'}">${cap.grant_signature_valid ? 'grant ✓' : 'grant ✗'}</span>
                    </div>
                </div>`);
            caps.filter(c => c.parent_id === cap.id).forEach(ch => walk(ch, depth + 1));
        };
        roots.forEach(r => walk(r, 0));
        container.innerHTML = html.join('') + (caps.filter(c => !c.parent_id).length > 12 ? '<div class="fine-print">Showing the 12 most recent mandates.</div>' : '');
        container.querySelectorAll('[data-cap]').forEach(n => {
            n.addEventListener('click', () => openModal(capsById[n.dataset.cap]));
            n.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(capsById[n.dataset.cap]); } });
        });
    }

    function renderReservations(rows) {
        $('reservations-table').innerHTML = rows.slice(0, 25).map(r => `
            <tr><td class="mono">${shortId(r.id)}</td>
                <td>${escapeHtml(r.merchant)} ${r.simulation ? '<span class="sim-tag">SIM</span>' : ''}</td>
                <td>${fmtINR(r.amount)}</td>
                <td class="${r.status === 'RESERVED' ? 'warn-text' : r.status === 'COMMITTED' ? 'ok-text' : 'muted'}">${escapeHtml(r.status)}</td>
                <td class="mono">${time(r.expires_at)}</td></tr>`).join('')
            || '<tr><td colspan="5" class="muted">No reservations yet.</td></tr>';
    }

    function renderAgents(rows) {
        $('agents-table').innerHTML = rows.map(a => `
            <tr><td>${escapeHtml(a.identifier)}</td>
                <td><span class="step-tag">${escapeHtml(a.type)}</span></td>
                <td class="${a.status === 'active' ? 'ok-text' : 'bad-text'}">${escapeHtml(a.status)}</td>
                <td class="mono muted">${escapeHtml(a.public_key_fingerprint)}</td></tr>`).join('')
            || '<tr><td colspan="4" class="muted">No agents yet.</td></tr>';
    }

    function renderPayments(rows) {
        $('payments-table').innerHTML = rows.slice(0, 20).map(p => `
            <tr><td class="mono">${shortId(p.id)}</td><td>${escapeHtml(p.merchant)}</td>
                <td>${fmtINR(p.amount)}</td><td class="mono">${escapeHtml(p.utr_reference)}</td>
                <td class="ok-text">${escapeHtml(p.status)}</td>
                <td class="mono">${time(p.created_at)}</td></tr>`).join('')
            || '<tr><td colspan="6" class="muted">No payments yet.</td></tr>';
    }

    function renderEvents(events) {
        const box = $('event-feed');
        if (!events.length) { box.innerHTML = '<div class="meta">No events yet.</div>'; return; }
        box.innerHTML = events.map(e => {
            const t = e.event_type;
            let cls = 'info';
            if (/FAIL|REJECT|HIGH_RISK|REVOKE|INVALID|BLOCKED|VIOLATION/.test(t)) cls = 'high-risk';
            else if (/SUCCESS|COMMITTED|AUTHORIZED$|WINNER/.test(t)) cls = 'success';
            else if (/RESERVED|RACE|SIMULATION|EXPIRED|CANCELLED|REQUIRED/.test(t)) cls = 'warning';
            return `<div class="event-item">
                <div class="event-time">${time(e.timestamp)}</div>
                <div class="event-type ${cls}">${escapeHtml(t)}${e.payload && e.payload.simulation ? ' <span class="sim-tag">SIM</span>' : ''}</div>
                <div class="event-actor">${escapeHtml(e.actor)}${e.amount ? ' · ' + fmtINR(e.amount) : ''}</div>
                ${e.payload && Object.keys(e.payload).length ? `<div class="event-payload">${escapeHtml(JSON.stringify(e.payload))}</div>` : ''}
            </div>`;
        }).join('');
    }

    function openModal(cap) {
        if (!cap) return;
        const sig = cap.grant_signature
            ? `<span class="${cap.grant_signature_valid ? 'signature-valid' : 'signature-invalid'}">${cap.grant_signature_valid ? '✓ Verified against issuer public key' : '✗ Does not verify against the current issuer key'}</span>
               <br><span class="mono muted" >${escapeHtml(cap.grant_signature)}</span>`
            : '<span class="signature-invalid">✗ No signature</span>';
        const row = (k, v) => `<div class="detail-row"><span class="detail-label">${k}</span><span class="detail-value">${v}</span></div>`;
        // Provenance: walk parent links (backend data) up to the root grant.
        const chain = [];
        let node = cap;
        while (node) { chain.unshift(node); node = node.parent_id ? capsById[node.parent_id] : null; }
        const ancestry = chain.map(c => `${escapeHtml(c.agent_identifier)} <span class="muted">(${fmtINR(c.total_authority)}, ${escapeHtml(c.status)}, issued by ${escapeHtml(c.issuer_identifier)})</span>`).join('<br>↓ ');
        $('modal-body').innerHTML =
            row('Capability ID', `<span class="mono">${escapeHtml(cap.id)}</span>`) +
            row('Issued to', escapeHtml(cap.agent_identifier)) +
            row('Issued by', escapeHtml(cap.issuer_identifier)) +
            row('Status', escapeHtml(cap.status)) +
            row('Total', fmtINR(cap.total_authority)) +
            row('Unallocated', fmtINR(cap.unallocated_authority)) +
            row('Reserved', fmtINR(cap.reserved_authority)) +
            row('Committed', fmtINR(cap.committed_authority)) +
            row('Category scope', escapeHtml(cap.category)) +
            row('Delegation depth', escapeHtml(cap.delegation_depth)) +
            `<div class="detail-row" data-col><span class="detail-label" >Provenance (root → this grant)</span><div >mandate<br>↓ ${ancestry}</div></div>` +
            `<div class="detail-row" data-col>
                <span class="detail-label" >Grant attestation</span><div>${sig}</div></div>`;
        $('cap-modal').classList.add('active');
    }
    $('modal-close').addEventListener('click', () => $('cap-modal').classList.remove('active'));
    $('cap-modal').addEventListener('click', (e) => { if (e.target.id === 'cap-modal') $('cap-modal').classList.remove('active'); });

    // ── Demo scenarios ───────────────────────────────────────────────
    const TITLES = {
        'initialize': 'Security Lab initialized', 'normal-payment': 'Normal payment',
        'policy-violation': 'Over-limit request (hard policy rule)', 'concurrent-race': 'Concurrent race',
        'behavioural-anomaly': 'Behavioural Risk Simulation (controlled)', 'tamper-request': 'Tampered requests', 'reset': 'Reset',
    };

    function summarize(action, d) {
        switch (action) {
            case 'normal-payment': return `Paid ${escapeHtml(d.utr_reference)} · risk ${escapeHtml(d.risk.level)} (${Number(d.risk.anomaly_score).toFixed(4)})`;
            case 'policy-violation': return `Requested ${fmtINR(d.requested)} with ${fmtINR(d.available)} available → ${escapeHtml(d.error)} from the ${escapeHtml(d.layer)}. The ML model was not consulted.`;
            case 'concurrent-race': return d.results.map(r => `Worker ${r.worker}: ${escapeHtml(r.status)}${r.error ? ' (' + escapeHtml(r.error) + ')' : ''}`).join(' · ') + ` for ${fmtINR(d.attempted_amount)} each`;
            case 'behavioural-anomaly': return d.status === 'contained'
                ? `Signature ✓ · authority ✓ · IsolationForest ${escapeHtml(d.risk.level)} (${Number(d.risk.anomaly_score).toFixed(4)}) → capability ${escapeHtml(d.containment.capability_status)}, ${d.containment.released_reservations} holds released, next request ${d.follow_up_request.blocked ? 'blocked (' + escapeHtml(d.follow_up_request.error) + ')' : 'NOT blocked'}. Synthetic history: ${d.simulated_history.holds} holds.`
                : `Model returned ${escapeHtml(d.risk.level)} — not contained; simulated holds removed.`;
            case 'tamper-request': return d.attempts.map(a => `${escapeHtml(a.attempt.replace(/_/g, ' '))}: ${escapeHtml(a.status)} (${escapeHtml(a.error || '')})`).join(' · ');
            default: return escapeHtml(d.status || 'done');
        }
    }

    document.querySelectorAll('[data-demo]').forEach(btn => btn.addEventListener('click', async () => {
        const action = btn.dataset.demo;
        if (action === 'reset' && !confirm('Reset demo data? This deletes all demo-user tasks, orders, agents, capabilities and audit events, then re-provisions a clean demo user. Non-demo data is not touched.')) return;
        document.querySelectorAll('[data-demo]').forEach(b => b.disabled = true);
        const r = await postJSON(`/demo/${action}`);
        document.querySelectorAll('[data-demo]').forEach(b => b.disabled = false);
        const box = $('demo-result');
        box.hidden = false;
        if (r.ok) {
            box.innerHTML = `<h4>${escapeHtml(TITLES[action])}</h4><div>${summarize(action, r.data)}</div>
                <details><summary class="muted" >Raw response</summary><pre>${escapeHtml(JSON.stringify(r.data, null, 2))}</pre></details>`;
        } else {
            box.innerHTML = `<h4 class="bad-text">${escapeHtml(TITLES[action])}: rejected (HTTP ${r.status})</h4><div>${escapeHtml(r.message)}</div>`;
        }
        lastJson = '';
        refresh();
    }));

    // Drunix enforcement status (read-only link to the Drunix Ledger page).
    async function drunixChip() {
        const r = await apiCall('/drunix/status');
        const el = $('sc-drunix');
        if (!r.ok) { el.innerHTML = 'Drunix ledger · <span class="mono">status unavailable</span>'; return; }
        const s = r.data;
        const on = s.mode === 'enforce';
        el.innerHTML = `<span class="dot-live ${on && s.connected ? '' : on ? 'bad' : 'off'}"></span>Drunix · <span class="mono">${on ? 'ENFORCE' : 'OFF'}${on ? (s.connected ? ' · connected' : ' · not connected') : ''}</span>`;
    }
    drunixChip();
    setInterval(() => { if (!document.hidden) drunixChip(); }, 15000);

    refresh().then(() => {
        const focus = new URLSearchParams(location.search).get('capability');
        if (focus && capsById[focus]) openModal(capsById[focus]);
    });
    setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
})();
