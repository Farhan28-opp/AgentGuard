/* Unified receipt (GET /product/transactions/{id}). Fields the backend does
 * not have are shown as "Not available"; nothing is invented. */
(async function () {
    const box = document.getElementById('txr');
    const r = await apiCall('/product/transactions/' + encodeURIComponent(box.dataset.id));
    if (!r.ok) {
        box.innerHTML = `<div class="empty"><div class="eyebrow">Receipt</div><h1 class="h2">Transaction not found</h1>
            <p class="muted">${escapeHtml(r.message)}</p><p class="mt-5"><a class="btn btn-secondary" href="/activity">Activity</a></p></div>`;
        return;
    }
    const d = r.data;
    const steps = (d.steps || []).map(s => `<li class="${s.status === 'failed' ? 'failed' : s.status === 'pending' ? 'pending' : ''}">
        <span class="dot"></span><div><div class="t">${escapeHtml(s.name)}</div><div class="d">${escapeHtml(s.detail)}</div></div></li>`).join('');
    box.innerHTML = `
        <div class="receipt-top">
            <div>
                <div class="eyebrow">${escapeHtml(d.flow_label)}</div>
                <h1 class="h1 mt-3">${escapeHtml(d.type_label)}</h1>
                <p class="meta mt-2">${escapeHtml(d.description || '')}</p>
            </div>
            <div class="ta-r">
                <span class="badge ${d.status === 'COMPLETED' ? 'ok' : ['FAILED', 'PAYMENT_FAILED', 'BLOCKED', 'CONTAINED'].includes(d.status) ? 'bad' : 'warn'}">${escapeHtml(d.status_label)}</span>
                <div class="review-amount">${d.amount ? fmtINR(d.amount) : '—'}</div>
            </div>
        </div>
        <div class="cols mt-6">
            <section class="col-7">
                <h2 class="eyebrow mb-3">Receipt</h2>
                ${receiptHTML(d)}
            </section>
            <section class="col-5">
                <h2 class="eyebrow mb-3">What happened, in order</h2>
                <ul class="log">${steps || '<li class="meta">No steps recorded.</li>'}</ul>
            </section>
        </div>
        <div class="row-gap mt-7">
            <a class="btn btn-primary" href="/activity">Activity</a>
            ${d.order ? `<a class="btn btn-secondary" href="/orders/${encodeURIComponent(d.order.order_number)}">Order and items</a>` : ''}
            ${d.capability_id ? `<a class="btn btn-secondary" href="/security?capability=${encodeURIComponent(d.capability_id)}">Security trail</a>` : ''}
            <a class="btn btn-secondary" href="/drunix">Drunix Ledger</a>
        </div>
        <p class="meta mt-5">Prototype: simulated payment rail${d.kind === 'agent' ? ' and simulated marketplace' : ''}. No real money moved.</p>`;
})();
