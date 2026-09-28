/* Payments page: direct payments (recharge, bill, send money).
 * Submitting a form only PREPARES a request: AgentGuard checks policy, the
 * wallet key and behavioural risk, then shows the review. Nothing is issued,
 * held or paid until the user clicks Authorize. Every value shown comes from
 * a backend response. */
(function () {
    const $ = (id) => document.getElementById(id);
    let current = null;   // the request being reviewed
    let busy = false;

    function switchTab(name) {
        if (!$('tab-' + name)) return;
        document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'content-' + name));
        $('payment-result').hidden = true;
    }
    function showForms(on) {
        $('forms').hidden = !on;
        $('review-panel').hidden = on;
        document.querySelector('.tabs').hidden = !on;
    }
    function setUrl(requestId) {
        const u = new URL(location.href);
        if (requestId) u.searchParams.set('request', requestId); else u.searchParams.delete('request');
        ['operator', 'mobile_number', 'plan_amount', 'consumer_number', 'provider', 'amount', 'recipient_upi', 'purpose', 'repeat']
            .forEach(k => u.searchParams.delete(k));
        history.replaceState(null, '', u);
    }
    document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

    $('btn-shopping-go').addEventListener('click', (e) => {
        e.preventDefault();
        const instruction = $('shop-instruction').value.trim() || 'Buy groceries for me under ₹3,000';
        location.href = `/agent?instruction=${encodeURIComponent(instruction)}`;
    });

    $('form-recharge').addEventListener('submit', (e) => {
        e.preventDefault();
        const [amount, desc] = document.querySelector('input[name="plan"]:checked').value.split('|');
        prepare('/product/payments/recharge', {
            mobile_number: $('rc-mobile').value.trim(), operator: $('rc-operator').value,
            plan_amount: parseFloat(amount), plan_description: desc,
        }, 'btn-recharge-submit');
    });
    $('form-bill').addEventListener('submit', (e) => {
        e.preventDefault();
        prepare('/product/payments/bill', {
            consumer_number: $('bill-consumer').value.trim(), provider: $('bill-provider').value,
            amount: parseFloat($('bill-amount').value),
        }, 'btn-bill-submit');
    });
    $('form-send').addEventListener('submit', (e) => {
        e.preventDefault();
        prepare('/product/payments/send-money', {
            recipient_upi: $('send-upi').value.trim(), amount: parseFloat($('send-amount').value),
            purpose: $('send-purpose').value.trim() || 'Transfer',
        }, 'btn-send-submit');
    });

    async function prepare(url, body, btnId) {
        if (busy) return;
        busy = true;
        const btn = $(btnId);
        const label = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Checking policy, identity and risk';
        $('payment-result').hidden = true;
        const r = await postJSON(url, body);
        busy = false;
        btn.disabled = false;
        btn.textContent = label;
        const req = r.data && (r.data.request || (r.data.request_id ? r.data : null));
        if (r.ok && req && req.status === 'AWAITING_AUTHORIZATION') return showReview(req);
        showOutcome(req, r);
    }

    function showReview(req) {
        current = req;
        setUrl(req.request_id);
        showForms(false);
        $('review-panel').innerHTML = directReviewHTML(req);
        $('review-panel').querySelector('[data-authorize]').onclick = authorize;
        $('review-panel').querySelector('[data-cancel]').onclick = cancel;
        $('payment-result').hidden = true;
        $('review-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
    }

    async function authorize() {
        if (busy || !current) return;
        busy = true;
        const btn = $('review-panel').querySelector('[data-authorize]');
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Authorizing — Drunix Reserve and Commit take a few seconds';
        $('review-panel').querySelector('[data-cancel]').disabled = true;
        const r = await postJSON(`/product/payments/requests/${encodeURIComponent(current.request_id)}/authorize`);
        busy = false;
        const req = r.data && (r.data.request || (r.data.request_id ? r.data : null));
        if (req && req.status === 'AWAITING_AUTHORIZATION') {   // e.g. Drunix unreachable at commit: hold kept, retry allowed
            showReview(req);
            showToast(r.message || 'Not completed — you can authorize again.', 'error');
            return;
        }
        showOutcome(req, r);
    }

    async function cancel() {
        if (busy || !current) return;
        busy = true;
        const r = await postJSON(`/product/payments/requests/${encodeURIComponent(current.request_id)}/cancel`);
        busy = false;
        const req = r.data && (r.data.request || (r.data.request_id ? r.data : null));
        showOutcome(req, r);
    }

    async function showOutcome(req, r) {
        current = null;
        showForms(true);
        const box = $('payment-result');
        if (req && req.status === 'COMPLETED') {
            const d = await apiCall(`/product/transactions/${encodeURIComponent(req.request_id)}`);
            setUrl(req.request_id);
            box.className = 'payment-result result-success';
            box.innerHTML = `<div class="panel-flat stack">
                <span class="badge ok">Payment completed · authorized by you</span>
                <div class="review-amount">${fmtINR(req.amount)}</div>
                ${d.ok ? receiptHTML(d.data) : ''}
                <div class="row-gap"><a class="btn btn-secondary btn-sm" href="/transactions/${encodeURIComponent(req.request_id)}">Full receipt</a>
                    <a class="link" href="/activity">Activity</a></div></div>`;
        } else {
            setUrl(null);
            const blocked = req && (req.status === 'BLOCKED' || req.status === 'REVIEW_REQUIRED');
            const d = r.data || {};
            const title = !req ? 'Payment not completed' : req.status === 'CANCELLED' ? 'Cancelled — nothing was paid'
                : blocked ? 'Blocked by risk controls' : req.status === 'EXPIRED' ? 'Request expired' : 'Payment not completed';
            box.className = 'payment-result result-error';
            box.innerHTML = `<div class="panel-flat stack">
                <span class="badge ${req && req.status === 'CANCELLED' ? '' : 'bad'}">${escapeHtml(title)}</span>
                ${r.ok ? '' : `<p class="h4">${escapeHtml(r.message)}</p>`}
                ${d.risk_level ? `<p class="meta">Risk ${escapeHtml(d.risk_level)} · score ${Number(d.anomaly_score).toFixed(4)}${(d.reasons || []).length ? ' · ' + d.reasons.map(escapeHtml).join('; ') : ''}</p>` : ''}
                <p class="meta">No money moved.${req ? ` Request ${escapeHtml(req.request_id)}.` : ''}</p>
                ${req ? `<div class="row-gap"><a class="link" href="/transactions/${encodeURIComponent(req.request_id)}">Details</a></div>` : ''}</div>`;
        }
        box.hidden = false;
        box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }

    // ── Boot: resume a pending request, or prefill a repeated one ───────
    const params = new URLSearchParams(location.search);
    const initial = params.get('tab');
    if (initial) switchTab(initial);
    const prefill = {
        mobile_number: 'rc-mobile', operator: 'rc-operator', consumer_number: 'bill-consumer', provider: 'bill-provider',
        recipient_upi: 'send-upi', purpose: 'send-purpose',
    };
    Object.entries(prefill).forEach(([k, id]) => { if (params.get(k)) $(id).value = params.get(k); });
    if (params.get('amount')) { ($('content-bills').classList.contains('active') ? $('bill-amount') : $('send-amount')).value = params.get('amount'); }
    if (params.get('plan_amount')) {
        const opt = [...document.querySelectorAll('input[name="plan"]')].find(i => i.value.split('|')[0] === String(Number(params.get('plan_amount'))));
        if (opt) opt.checked = true;
    }
    if (params.get('repeat')) showToast('Pre-filled from an earlier payment. Edit anything, then review — this creates a new request.', 'success');
    if (params.get('request')) {
        apiCall(`/product/payments/requests/${encodeURIComponent(params.get('request'))}`).then(r => {
            if (!r.ok) { setUrl(null); return; }
            if (r.data.status === 'AWAITING_AUTHORIZATION') showReview(r.data);
            else showOutcome(r.data, r);
        });
    }
})();
