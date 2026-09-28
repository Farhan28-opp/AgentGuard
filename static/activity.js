/* Activity: backend orders + audit events as a transaction history. Nothing is fabricated. */
(function () {
    const $ = (id) => document.getElementById(id);
    let events = [];
    let filter = 'all';

    const GROUP = {
        payment: /PAYMENT_(SUCCESS|AUTHORIZED|FAILED|REJECTED|AUTO_APPROVED|AUTHORIZATION_REQUIRED)|ORDER_CREATED|RESERVATION_(COMMITTED|RELEASED)|AUTHORITY_RESERVED|RESERVATIONS_EXPIRED|AUTHORIZATION_EXPIRED/,
        security: /SIGNATURE|SIGNED_|RISK_|HIGH_RISK|CONTAINMENT|REVOKED|SUSPENDED|BLOCKED|POLICY_|KEY_ROTATED|SIMULATION|VIOLATION/,
    };
    const groupOf = (e) => GROUP.security.test(e.raw_event) ? 'security' : GROUP.payment.test(e.raw_event) ? 'payment' : 'agent';
    const BADGE = {success: 'ok', danger: 'bad', warning: 'warn', pending: 'info', info: ''};
    const STATUS = {success: 'Completed', danger: 'Blocked', warning: 'Attention', pending: 'Pending', info: 'Recorded'};

    function who(e) {
        const p = e.payload || {};
        return p.merchant || p.to || p.agent || e.actor;
    }
    function detail(e) {
        const p = e.payload || {};
        const bits = [];
        if (/^SIGNED_/.test(e.raw_event)) bits.push('Signature verified · Ed25519');
        if (p.level) bits.push(`Risk ${p.level}${p.anomaly_score !== undefined ? ' · ' + Number(p.anomaly_score).toFixed(4) : ''}`);
        if (p.order) bits.push('Order ' + p.order);
        if (p.utr) bits.push('Ref ' + p.utr);
        if (p.reason) bits.push(p.reason);
        if (p.error && !p.level) bits.push(p.error);
        if (p.released_reservations !== undefined) bits.push(`${p.released_reservations} holds released`);
        return bits.join(' · ');
    }

    function render() {
        const box = $('activity-container');
        const rows = events.filter(e => filter === 'all' || groupOf(e) === filter);
        if (!rows.length) {
            box.innerHTML = `<div class="empty"><p class="h4">No recent activity</p>
                <p class="muted">Your completed payments and agent actions will appear here.</p></div>`;
            return;
        }
        const tech = $('show-tech').checked;
        // Presentation only: group the backend rows by calendar day.
        const dayKey = (d) => d.toLocaleDateString('en-IN', {year: 'numeric', month: '2-digit', day: '2-digit'});
        const today = dayKey(new Date());
        const yesterday = dayKey(new Date(Date.now() - 864e5));
        const counts = {};
        rows.forEach(e => { const k = dayKey(new Date(e.timestamp)); counts[k] = (counts[k] || 0) + 1; });
        let lastDay = null;
        box.innerHTML = rows.map(e => {
            const d = new Date(e.timestamp);
            const sub = detail(e);
            const k = dayKey(d);
            let head = '';
            if (k !== lastDay) {
                lastDay = k;
                const label = k === today ? 'Today' : k === yesterday ? 'Yesterday'
                    : d.toLocaleDateString('en-IN', {weekday: 'long', day: 'numeric', month: 'long'});
                head = `<div class="tx-group"><span>${label}</span><span class="meta">${counts[k]} ${counts[k] === 1 ? 'entry' : 'entries'}</span></div>`;
            }
            return head + `<div class="tx-row">
                <span class="date">${d.toLocaleDateString('en-IN', {day: '2-digit', month: 'short'})}<br>${d.toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit', second: '2-digit'})}</span>
                <span><span class="what">${escapeHtml(e.label)}</span>${e.simulation ? ' <span class="tag">Simulation</span>' : ''}
                    ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ''}</span>
                <span class="who">${escapeHtml(who(e))}</span>
                <span class="amt">${e.amount ? fmtINR(e.amount) : ''}</span>
                <span class="st"><span class="badge ${BADGE[e.kind] || ''}">${STATUS[e.kind] || 'Recorded'}</span></span>
                ${tech ? `<span class="tech">${escapeHtml(e.raw_event)} · ${escapeHtml(e.actor)} · ${escapeHtml(JSON.stringify(e.payload))}</span>` : ''}
            </div>`;
        }).join('');
    }

    // ── Transactions: selection, detail, repeat, hide ───────────────
    let txKind = '';
    let selected = null;
    let txRows = [];

    async function loadTransactions() {
        const qs = new URLSearchParams({limit: '50'});
        if (txKind) qs.set('kind', txKind);
        if ($('show-hidden').checked) qs.set('include_hidden', 'true');
        const r = await apiCall('/product/transactions?' + qs);
        const tb = $('tx-list');
        if (!r.ok) { tb.innerHTML = `<tr><td colspan="5" class="bad-text">Couldn't load transactions: ${escapeHtml(r.message)}</td></tr>`; return; }
        txRows = r.data.transactions;
        const STATE = {COMPLETED: 'ok', FAILED: 'bad', PAYMENT_FAILED: 'bad', BLOCKED: 'bad', CONTAINED: 'bad',
            REVIEW_REQUIRED: 'warn', EXPIRED: 'warn', AWAITING_AUTHORIZATION: 'info', CREATED: 'info'};
        tb.innerHTML = txRows.length ? txRows.map(t => {
            const d = new Date(t.created_at);
            return `<tr class="clickable ${t.id === selected ? 'selected' : ''} ${t.hidden ? 'is-hidden' : ''}" data-id="${escapeHtml(t.id)}" tabindex="0" aria-selected="${t.id === selected}">
                <td class="meta num">${d.toLocaleDateString('en-IN', {day: '2-digit', month: 'short'})} ${d.toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit'})}</td>
                <td>${escapeHtml(t.type_label)}${t.hidden ? ' <span class="tag">Hidden</span>' : ''}<div class="meta">${escapeHtml(t.description || '')}</div></td>
                <td>${escapeHtml(t.merchant || '—')}</td>
                <td class="r num strong">${t.amount ? fmtINR(t.amount) : '—'}</td>
                <td class="r"><span class="badge ${STATE[t.status] || ''}">${escapeHtml(t.status_label)}</span></td></tr>`;
        }).join('') : `<tr><td colspan="5"><div class="empty"><p class="h4">No transactions yet</p>
            <p class="muted">Pay a bill or <a href="/agent">ask your agent</a> to buy something.</p></div></td></tr>`;
        tb.querySelectorAll('tr[data-id]').forEach(tr => {
            tr.addEventListener('click', () => select(tr.dataset.id));
            tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(tr.dataset.id); } });
        });
    }

    async function select(id) {
        selected = id;
        document.querySelectorAll('#tx-list tr[data-id]').forEach(tr => {
            tr.classList.toggle('selected', tr.dataset.id === id); tr.setAttribute('aria-selected', tr.dataset.id === id);
        });
        const box = $('tx-detail');
        box.innerHTML = '<span class="loading"><span class="spinner"></span>Loading</span>';
        const r = await apiCall('/product/transactions/' + encodeURIComponent(id));
        if (!r.ok) { box.innerHTML = `<div class="notice bad">${escapeHtml(r.message)}</div>`; return; }
        const d = r.data;
        const pending = d.status === 'AWAITING_AUTHORIZATION' || d.status === 'CREATED';
        box.innerHTML = `<div class="eyebrow">${escapeHtml(d.flow_label)}</div>
            <p class="h3 mt-2">${escapeHtml(d.type_label)}</p>
            <div class="mt-4">${receiptHTML(d)}</div>
            <div class="row-gap mt-5">
                <a class="btn btn-secondary btn-sm" href="/transactions/${encodeURIComponent(d.id)}">Full receipt</a>
                ${pending ? `<a class="btn btn-primary btn-sm" href="${escapeHtml(d.open_url)}">Continue</a>` : ''}
                ${d.can_repeat ? `<button class="btn btn-secondary btn-sm" type="button" id="btn-repeat">${d.can_retry ? 'Edit &amp; Retry' : 'Repeat &amp; Edit'}</button>` : ''}
                <button class="btn btn-quiet btn-sm" type="button" id="btn-hide">${d.hidden ? 'Restore to Activity' : 'Hide from Activity'}</button>
            </div>
            <p class="meta mt-3">${d.can_repeat ? 'Repeat &amp; Edit starts a new request pre-filled from this one; this transaction is not changed. ' : ''}Hiding never deletes the payment or its audit records.</p>`;
        if ($('btn-repeat')) $('btn-repeat').onclick = () => repeat(d);
        $('btn-hide').onclick = () => toggleHidden(d);
    }

    async function repeat(d) {
        const r = await postJSON(`/product/transactions/${encodeURIComponent(d.id)}/repeat`);
        if (!r.ok) return showToast(r.message, 'error');
        if (r.data.kind === 'agent') { location.href = `/agent?task=${encodeURIComponent(r.data.task_id)}`; return; }
        const qs = new URLSearchParams({tab: r.data.tab, repeat: '1'});
        Object.entries(r.data.prefill || {}).forEach(([k, v]) => { if (v !== null && v !== '' && k !== 'plan_description') qs.set(k, v); });
        location.href = '/payments?' + qs;
    }

    async function toggleHidden(d) {
        const action = d.hidden ? 'unhide' : 'hide';
        const r = await postJSON(`/product/transactions/${encodeURIComponent(d.id)}/${action}`);
        if (!r.ok) return showToast(r.message, 'error');
        showToast(d.hidden ? 'Restored to Activity.' : 'Hidden from Activity. The payment and its audit records are kept.', 'success');
        if (!d.hidden && !$('show-hidden').checked) {
            selected = null;
            $('tx-detail').innerHTML = '<div class="eyebrow">Transaction</div><p class="muted mt-2">Hidden. Tick “Show hidden” to see it again.</p>';
        }
        await loadTransactions();
        if (selected) select(selected);
    }

    document.querySelectorAll('#tx-filters .chip').forEach(c => c.addEventListener('click', () => {
        txKind = c.dataset.kind;
        document.querySelectorAll('#tx-filters .chip').forEach(x => x.classList.toggle('active', x === c));
        loadTransactions();
    }));
    $('show-hidden').addEventListener('change', loadTransactions);

    async function load() {
        loadTransactions();
        const r = await apiCall('/product/activity?limit=80');
        if (!r.ok) {
            $('activity-container').innerHTML = `<div class="notice bad">Couldn't load activity: ${escapeHtml(r.message)}</div>`;
            return;
        }
        events = r.data;
        render();
        $('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN');
    }

    document.querySelectorAll('#filters .chip').forEach(c => c.addEventListener('click', () => {
        filter = c.dataset.filter;
        document.querySelectorAll('#filters .chip').forEach(x => x.classList.toggle('active', x === c));
        render();
    }));
    $('show-tech').addEventListener('change', render);
    $('btn-refresh').addEventListener('click', load);
    load();
    setInterval(() => { if (!document.hidden) load(); }, 5000);
})();
