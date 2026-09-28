/* AgentGuard Product JS — shared utilities for consumer pages.
 * No page fabricates events: everything rendered comes from backend responses. */

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtINR(amount) {
    const n = Number(amount);
    if (!isFinite(n)) return '₹—';
    return '₹' + n.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${type}`;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.className = 'toast'; }, 4000);
}

/* fetch wrapper: resolves {ok, status, data}; never throws on HTTP errors,
 * only on network failure. Turns FastAPI/AgentGuard error bodies into text. */
async function apiCall(url, options = {}) {
    let resp;
    try {
        resp = await fetch(url, options);
    } catch (err) {
        return {ok: false, status: 0, data: null, message: 'Network error — is the AgentGuard server running?'};
    }
    let data = null;
    try { data = await resp.json(); } catch (_) { /* non-JSON */ }
    return {ok: resp.ok, status: resp.status, data, message: resp.ok ? '' : errorText(data, resp.status)};
}

function errorText(data, status) {
    if (!data) return `Request failed (HTTP ${status}).`;
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
        return data.detail.map(d => `${(d.loc || []).slice(-1)[0] || 'input'}: ${d.msg}`).join('; ');
    }
    if (data.error) return data.error;
    return `Request failed (HTTP ${status}).`;
}

function postJSON(url, body) {
    return apiCall(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body),
    });
}

/* ── Shared payment presentation (direct payments, Activity, receipts) ──────
 * One review renderer for recharge / bill / send money and one receipt
 * renderer for every transaction. Every value comes from the backend; a value
 * the backend does not have is shown as "Not available", never invented. */

const NA = '<span class="muted">Not available</span>';

function fmtWhen(iso) {
    if (!iso) return NA;
    return escapeHtml(new Date(iso).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'medium'}));
}

function txLabel(tx) {
    if (!tx || !tx.tx_id) return NA;
    return `<span class="mono" title="${escapeHtml(tx.tx_id)}">${escapeHtml(tx.tx_id.slice(0, 18))}…</span>`
        + (tx.block_number != null ? ` · block ${escapeHtml(tx.block_number)}` : ' · recovered')
        + (tx.status ? ` · ${escapeHtml(tx.status)}` : '');
}

function checkCell(k, v, state) {
    const CHECK = '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5 10 17 19 7.5"/></svg>';
    const DOT = '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/></svg>';
    return `<div class="check-cell"><div class="k">${escapeHtml(k)}</div>
        <div class="v ${state}">${state === 'ok' ? CHECK : DOT}${escapeHtml(v)}</div></div>`;
}

/* Review screen for a prepared direct payment (status AWAITING_AUTHORIZATION). */
function directReviewHTML(req) {
    const a = req.authorization || {};
    const c = a.checks || {};
    const risk = a.risk || {};
    const dx = a.drunix || {};
    const riskState = risk.action === 'ALLOW' ? 'ok' : risk.action === 'REVIEW' ? 'warn' : 'bad';
    const dxState = dx.mode === 'off' ? 'warn' : dx.connected ? 'ok' : 'bad';
    return `
    <div class="review">
        <div class="eyebrow">Authorize payment · ${escapeHtml(a.flow_label || 'Direct payment')}</div>
        <div class="review-amount">${fmtINR(a.amount)}</div>
        <p class="h3">${escapeHtml(a.type_label || '')} · ${escapeHtml(a.merchant || '')}</p>
        <p class="muted body-sm mt-2">${escapeHtml(a.description || '')}. Nothing has been issued, held or paid yet.</p>
        <div class="kv mt-6">
            <div class="kv-row"><span>Payment type</span><span>${escapeHtml(a.type_label || '')} <span class="tag">Direct · authorized by you</span></span></div>
            <div class="kv-row"><span>Paid to</span><span>${escapeHtml(a.merchant || '')} · ${escapeHtml(a.payee || '')} <span class="tag">Simulated</span></span></div>
            <div class="kv-row"><span>Amount</span><span class="num">${fmtINR(a.amount)} ${escapeHtml(a.currency || 'INR')}</span></div>
            <div class="kv-row"><span>Requested by</span><span>${escapeHtml((a.actor || {}).label || '')} · <span class="mono">${escapeHtml((a.actor || {}).agent || '')}</span> <span class="meta">key ${escapeHtml((a.actor || {}).key_fingerprint || '')}</span></span></div>
            <div class="kv-row"><span>Policy</span><span>${escapeHtml((a.policy || {}).detail || '')}</span></div>
            <div class="kv-row"><span>Authority</span><span>${escapeHtml((a.authority || {}).detail || '')}</span></div>
            <div class="kv-row"><span>Risk (pre-check)</span><span>${escapeHtml(risk.level || '')} / ${escapeHtml(risk.action || '')} · score ${Number(risk.anomaly_score).toFixed(4)} <span class="meta">${escapeHtml(risk.engine || '')}</span>${(risk.reasons || []).length ? `<div class="meta">${risk.reasons.map(escapeHtml).join('; ')}</div>` : ''}</span></div>
            <div class="kv-row"><span>Drunix</span><span>${escapeHtml(dx.status || '')} · <span class="meta">${escapeHtml(dx.detail || '')}</span></span></div>
            <div class="kv-row"><span>Reservation</span><span>${escapeHtml(a.reservation_status || '')}</span></div>
            <div class="kv-row"><span>Request expires</span><span class="mono">${fmtWhen(a.expires_at)}</span></div>
        </div>
        <div class="checks">
            ${checkCell('Identity', c.identity || '—', c.identity === 'VERIFIED' ? 'ok' : 'bad')}
            ${checkCell('Policy', c.policy || '—', c.policy === 'WITHIN LIMIT' ? 'ok' : 'bad')}
            ${checkCell('Risk', `${risk.level || ''} · ${c.risk || ''}`, riskState)}
            ${checkCell('Authority', c.authority || '—', 'warn')}
            ${c.drunix ? checkCell('Drunix', c.drunix, dxState) : ''}
            ${checkCell('Reservation', c.reservation || '—', c.reservation === 'RESERVED' ? 'ok' : 'warn')}
        </div>
        <details class="mt-4" open>
            <summary class="link">What happens when you authorize</summary>
            <ol class="pay-flow mt-3">${(a.after_confirmation || []).map(s => `<li><span>${escapeHtml(s)}</span></li>`).join('')}</ol>
        </details>
        <div class="review-actions">
            <button class="btn btn-primary btn-lg" type="button" data-authorize>Authorize ${fmtINR(a.amount)}</button>
            <button class="btn btn-secondary btn-lg" type="button" data-cancel>Cancel</button>
        </div>
        <p class="meta mt-3">Simulated payment rail — no real money moves. Request ${escapeHtml(req.request_id)}.</p>
    </div>`;
}

/* Unified receipt for any transaction (GET /product/transactions/{id}). */
function receiptHTML(d) {
    const dx = d.drunix || {};
    const risk = d.risk || null;
    const actor = d.actor || null;
    const row = (k, v) => `<div class="kv-row"><span>${k}</span><span>${v == null || v === '' ? NA : v}</span></div>`;
    const esc = (v) => v == null ? null : escapeHtml(v);
    const final = {COMPLETED: 'ok', CANCELLED: '', EXPIRED: 'warn', FAILED: 'bad', PAYMENT_FAILED: 'bad',
        REVIEW_REQUIRED: 'warn', BLOCKED: 'bad', CONTAINED: 'bad', AWAITING_AUTHORIZATION: 'warn'}[d.status] ?? '';
    const enforced = dx.mode === 'enforce';
    const dxRows = [
        ['Drunix mode', esc(dx.mode === 'enforce' ? 'ENFORCE' : dx.mode === 'off' ? 'OFF (not enforced on Drunix)' : dx.mode)],
        ...(d.kind === 'direct'
            ? [['Drunix RegisterMandate', enforced ? txLabel(dx.register_mandate) : null],
               ['Drunix RegisterRootCapability', enforced ? txLabel(dx.register_capability) : null]]
            : [['Drunix Delegate', enforced ? txLabel(dx.delegate) : null]]),
        ['Drunix Reserve', enforced ? txLabel(dx.reserve) : null],
        ['Drunix Commit', enforced ? txLabel(dx.commit) : null],
        ...(dx.return_unused ? [['Drunix ReturnUnused', txLabel(dx.return_unused)]] : []),
        ...(dx.release ? [['Drunix Release', txLabel(dx.release)]] : []),
    ];
    return `
    <div class="kv">
        ${row('Type', `${esc(d.type_label)} <span class="tag">${d.kind === 'direct' ? 'Direct · authorized by you' : 'Agent-delegated'}</span>`)}
        ${row('Provider / merchant', d.merchant ? `${esc(d.merchant)} <span class="tag">Simulated</span>` : null)}
        ${d.payee ? row('Payee', esc(d.payee)) : ''}
        ${row('Amount', d.amount ? `<span class="num">${fmtINR(d.amount)}</span>` : null)}
        ${d.kind === 'agent' ? row('Budget', d.budget ? `<span class="num">${fmtINR(d.budget)}</span>` : null) : ''}
        ${row('Timestamp', fmtWhen(d.timestamp))}
        ${row('Final state', `<span class="badge ${final}">${esc(d.status_label)}</span>`)}
        ${row('Payment ID', d.payment_id ? `<span class="mono">${esc(d.payment_id)}</span>` : null)}
        ${row('UTR / reference', d.utr_reference ? `<span class="mono">${esc(d.utr_reference)}</span>` : null)}
        ${row('Payment rail', 'Simulated rail — no real money moved')}
        ${row(d.kind === 'direct' ? 'Payment actor' : 'Agent', actor ? `${esc(actor.role)} · <span class="mono">${esc(actor.identifier)}</span> <span class="meta">key ${esc(actor.key_fingerprint)}</span>` : null)}
        ${row('Signature', esc(d.signature))}
        ${row('Risk', risk ? `${esc(risk.level)} / ${esc(risk.action)} · score ${Number(risk.anomaly_score).toFixed(4)} <span class="meta">${esc(risk.stage || '')}</span>${(risk.reasons || []).length ? `<div class="meta">${risk.reasons.map(escapeHtml).join('; ')}</div>` : ''}` : null)}
        ${row('Policy', esc(d.policy))}
        ${row('Capability ID', d.capability_id ? `<span class="mono">${esc(d.capability_id)}</span>` : null)}
        ${row('Reservation ID', d.reservation_id ? `<span class="mono">${esc(d.reservation_id)}</span>` : null)}
        ${d.order ? row('Order', `<a href="/orders/${encodeURIComponent(d.order.order_number)}">${esc(d.order.order_number)}</a> · ${d.order.item_count} items`) : ''}
        ${dxRows.map(([k, v]) => row(k, v)).join('')}
    </div>
    ${d.error ? `<div class="notice bad mt-4">${esc(d.error.error)}: ${esc(d.error.detail)}</div>` : ''}`;
}
