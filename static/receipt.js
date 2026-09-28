/* Receipt: order + authority trail from GET /product/orders/{ref}. */
(async function () {
    const box = document.getElementById('receipt');
    const r = await apiCall('/product/orders/' + encodeURIComponent(box.dataset.ref));
    if (!r.ok) {
        box.innerHTML = `<div class="empty"><div class="eyebrow">Order</div><h1 class="h2">Order not found</h1>
            <p class="muted">${escapeHtml(r.message)}</p><p class="mt-5"><a class="btn btn-secondary" href="/">Back to Home</a></p></div>`;
        return;
    }
    const o = r.data, a = o.authority || {};
    function drunixTrail(d) {
        if (!d || !d.enforced) {
            return `<h2 class="eyebrow mb-3 mt-6">Drunix ledger</h2>
                <p class="meta">Not enforced on Drunix for this order (DRUNIX_MODE was off).</p>`;
        }
        const tx = (label, t) => t ? `<div class="kv-row"><span>${label}</span><span class="mono" title="${escapeHtml(t.tx_id)}">${escapeHtml(t.tx_id.slice(0, 20))}… · ${t.block_number != null ? 'block ' + t.block_number : 'recovered'} · ${escapeHtml(t.status)}</span></div>` : '';
        return `<h2 class="eyebrow mb-3 mt-6">Drunix ledger enforcement</h2>
            <div class="kv">
                ${tx('Delegate', d.delegate)}${tx('Reserve', d.reserve)}${tx('Commit', d.commit)}${tx('Return unused', d.return_unused)}
            </div>
            <p class="meta mt-2">Committed VALID on Drunix by the agentauth chaincode before the payment was recorded.</p>`;
    }
    const when = new Date(o.created_at).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'medium'});
    box.innerHTML = `
        <div class="receipt-top">
            <div>
                <div class="eyebrow">Order confirmation</div>
                <h1 class="h1 mt-3">Order ${escapeHtml(o.order_number)}</h1>
                <p class="meta mt-2">${escapeHtml(when)} · ${escapeHtml(o.merchant)} (simulated merchant)</p>
            </div>
            <div class="ta-r">
                <span class="badge ok">${escapeHtml(o.status === 'CONFIRMED' ? 'Confirmed' : o.status)}</span>
                <div class="review-amount">${fmtINR(o.total)}</div>
            </div>
        </div>
        <div class="cols mt-6">
            <section class="col-7">
                <h2 class="eyebrow mb-3">Items</h2>
                <div class="table-wrap"><table class="table">
                    <thead><tr><th>Item</th><th class="r">Qty</th><th class="r">Price</th><th class="r">Amount</th></tr></thead>
                    <tbody>${o.items.map(i => `<tr><td>${escapeHtml(i.name)}</td><td class="r num">${i.quantity}</td><td class="r num">${fmtINR(i.unit_price)}</td><td class="r num">${fmtINR(i.line_total)}</td></tr>`).join('')}</tbody>
                </table></div>
                <div class="kv mt-4 no-top">
                    <div class="kv-row"><span>Subtotal · ${o.item_count} items</span><span class="num">${fmtINR(o.subtotal)}</span></div>
                    <div class="kv-row"><span>Delivery</span><span class="num">${Number(o.delivery_fee) === 0 ? 'Free' : fmtINR(o.delivery_fee)}</span></div>
                    <div class="kv-row total"><span>Total</span><span class="num">${fmtINR(o.total)}</span></div>
                </div>
            </section>
            <section class="col-5">
                <h2 class="eyebrow mb-3">Payment and authority</h2>
                <div class="kv">
                    <div class="kv-row"><span>Payment reference</span><span class="mono">${escapeHtml(o.utr_reference)}</span></div>
                    <div class="kv-row"><span>Payment rail</span><span>Simulated</span></div>
                    <div class="kv-row"><span>Approved by</span><span>${o.approval === 'user' ? 'You' : escapeHtml(o.approval)}</span></div>
                    <div class="kv-row"><span>Agent used</span><span class="mono">${escapeHtml(o.agent)}</span></div>
                    <div class="kv-row"><span>Agent authority</span><span class="num">${fmtINR(a.agent_authority)}</span></div>
                    <div class="kv-row"><span>Authority consumed</span><span class="num">${fmtINR(a.consumed)}</span></div>
                    <div class="kv-row"><span>Returned to Main Agent</span><span class="num">${fmtINR(a.returned_to_main_agent)}</span></div>
                    <div class="kv-row"><span>Main Agent remaining</span><span class="num">${fmtINR(a.main_agent_remaining_at_payment)}</span></div>
                    <div class="kv-row"><span>Signed operations</span><span>${(o.security_trail.signed_operations || []).map(escapeHtml).join(' → ') || '—'}</span></div>
                    <div class="kv-row"><span>Capability</span><span class="mono">${escapeHtml(o.security_trail.capability_id)}</span></div>
                    <div class="kv-row"><span>Reservation</span><span class="mono">${escapeHtml(o.security_trail.reservation_id)}</span></div>
                </div>
                ${drunixTrail(o.drunix)}
            </section>
        </div>
        <div class="row-gap mt-7">
            <a class="btn btn-primary" href="/activity">View Activity</a>
            <a class="btn btn-secondary" href="/security?capability=${encodeURIComponent(o.security_trail.capability_id)}">View Security Trail</a>
            <a class="btn btn-secondary" href="/">Back to Home</a>
        </div>
        <p class="meta mt-5">Prototype: controlled simulated marketplace and simulated payment rail. No real money moved.</p>`;
})();
