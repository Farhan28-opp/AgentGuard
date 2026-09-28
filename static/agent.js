/* AgentGuard — AI Agent workspace.
 * Every number, merchant, product, check and status shown here comes from a
 * backend response. This file only renders and sends user actions. */
(function () {
    const $ = (id) => document.getElementById(id);
    let task = null;          // latest task payload from the backend
    let quotes = null;        // latest comparison (from POST /compare)
    let recommendation = null;
    let catalogState = {q: '', category: ''};
    let categories = [];
    let lastCatalog = null;
    let busy = false;
    let countdownTimer = null;

    const STATUS = {
        CREATED: ['Shopping', 'info'], RUNNING: ['Working', 'info'],
        AWAITING_AUTHORIZATION: ['Awaiting authorization', 'warn'], AUTHORIZED: ['Authorizing', 'warn'],
        COMPLETED: ['Paid · order confirmed', 'ok'], CANCELLED: ['Cancelled', ''],
        EXPIRED: ['Expired', 'warn'], FAILED: ['Failed', 'bad'], PAYMENT_FAILED: ['Payment failed', 'bad'],
        REVIEW_REQUIRED: ['Held for review', 'warn'], CONTAINED: ['Contained', 'bad'],
    };
    const ARROW = '<svg class="icon arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
    const CHECK = '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5 10 17 19 7.5"/></svg>';
    const CROSS = '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';

    function showTask(on) { $('phase-task').hidden = !on; $('phase-empty').hidden = on; }
    function setUrl(id) {
        const u = new URL(location.href);
        if (id) u.searchParams.set('task', id); else u.searchParams.delete('task');
        u.searchParams.delete('instruction');
        history.replaceState(null, '', u);
    }

    // ── Policy ───────────────────────────────────────────────────────
    async function loadPolicy() {
        const r = await apiCall('/product/policy');
        if (!r.ok) { $('policy-summary').textContent = 'Policy unavailable: ' + r.message; return; }
        renderPolicy(r.data);
    }

    function renderPolicy(p) {
        const mode = p.options.approval_modes.find(m => m.id === p.approval_mode);
        const cats = p.options.categories.filter(c => p.allowed_categories.includes(c.id)).map(c => c.label);
        const mers = p.options.merchants.filter(m => p.allowed_merchants.includes(m.id)).map(m => m.label);
        $('auth-per-txn').innerHTML = `${fmtINR(p.per_transaction_limit)} <small>/ transaction</small>`;
        $('auth-meta').textContent = `Overall ${fmtINR(p.overall_authority)} · ${fmtINR(p.standing.main_unallocated)} unallocated with the Main Agent`;
        $('policy-summary').textContent = `${mode ? mode.label : p.approval_mode}${p.approval_mode === 'above_threshold' ? ' (' + fmtINR(p.approval_threshold) + ')' : ''} · ${cats.join(', ')} · ${mers.join(', ')}`;
        const purchase = p.agents.find(a => a.role === 'Purchase Agent');
        const suspended = purchase && purchase.status !== 'active';
        $('policy-notice').innerHTML = suspended ? `<div class="notice bad mt-4">Purchase Agent <b>${escapeHtml(purchase.agent_identifier)}</b> is ${escapeHtml(purchase.status)} after a containment; its key is no longer accepted.
            <div class="mt-3"><button class="btn btn-secondary btn-sm" type="button" id="btn-replace">Replace Purchase Agent</button></div></div>` : '';
        $('policy-body').innerHTML = `
            <form id="policy-form" class="policy-grid">
                <div class="field"><label class="label" for="pf-overall">Overall authority (₹)</label>
                    <input class="input num" id="pf-overall" name="overall_authority" type="number" min="500" max="100000" step="1" value="${Number(p.overall_authority)}">
                    <span class="hint">${escapeHtml(p.enforcement.overall_authority)}</span></div>
                <div class="field"><label class="label" for="pf-txn">Per-purchase / per-payment limit (₹)</label>
                    <input class="input num" id="pf-txn" name="per_transaction_limit" type="number" min="100" step="1" value="${Number(p.per_transaction_limit)}">
                    <span class="hint">${escapeHtml(p.enforcement.per_transaction_limit)}</span></div>
                <fieldset class="field"><legend class="label">Allowed categories</legend>
                    ${p.options.categories.map(c => `<label class="check"><input type="checkbox" name="cat" value="${escapeHtml(c.id)}" ${p.allowed_categories.includes(c.id) ? 'checked' : ''}> ${escapeHtml(c.label)}</label>`).join('')}
                    <span class="hint">${escapeHtml(p.enforcement.allowed_categories)}</span></fieldset>
                <fieldset class="field"><legend class="label">Allowed merchants</legend>
                    ${p.options.merchants.map(m => `<label class="check"><input type="checkbox" name="mer" value="${escapeHtml(m.id)}" ${p.allowed_merchants.includes(m.id) ? 'checked' : ''}> ${escapeHtml(m.label)}</label>`).join('')}
                    <span class="hint">${escapeHtml(p.enforcement.allowed_merchants)}</span></fieldset>
                <div class="field"><label class="label" for="pf-mode">Approval mode</label>
                    <select class="select" id="pf-mode" name="approval_mode">${p.options.approval_modes.map(m => `<option value="${m.id}" ${m.id === p.approval_mode ? 'selected' : ''}>${escapeHtml(m.label)}</option>`).join('')}</select>
                    <span class="hint">${escapeHtml(p.enforcement.approval_mode)}</span></div>
                <div class="field"><label class="label" for="pf-thr">Ask me above (₹)</label>
                    <input class="input num" id="pf-thr" name="approval_threshold" type="number" min="0" step="1" value="${Number(p.approval_threshold)}">
                    <span class="hint">Used by “Ask me above a threshold”</span></div>
                <div class="full row-gap">
                    <button class="btn btn-primary" type="submit">Save policy</button>
                    <span class="hint">Changing overall authority or merchants re-issues the Main Agent's signed authority. Mandate valid until ${p.standing.mandate_valid_until ? new Date(p.standing.mandate_valid_until).toLocaleDateString('en-IN') : '—'}.</span>
                </div>
            </form>`;
        $('policy-form').addEventListener('submit', savePolicy);
        if ($('btn-replace')) $('btn-replace').onclick = replaceAgent;
    }

    // Opens the existing policy editor on the field that limits this purchase.
    function openPolicyEditor(fieldId) {
        $('policy-body').hidden = false;
        $('btn-policy-toggle').textContent = 'Close';
        $('btn-policy-toggle').setAttribute('aria-expanded', 'true');
        document.querySelector('.authority-box').scrollIntoView({behavior: 'smooth', block: 'start'});
        const f = $(fieldId); if (f) { f.focus(); f.select && f.select(); }
    }

    $('btn-policy-toggle').addEventListener('click', () => {
        const body = $('policy-body');
        body.hidden = !body.hidden;
        $('btn-policy-toggle').textContent = body.hidden ? 'Edit policy' : 'Close';
        $('btn-policy-toggle').setAttribute('aria-expanded', String(!body.hidden));
    });

    async function savePolicy(e) {
        e.preventDefault();
        const f = e.target;
        const body = {
            overall_authority: Number(f.overall_authority.value),
            per_transaction_limit: Number(f.per_transaction_limit.value),
            allowed_categories: [...f.querySelectorAll('input[name=cat]:checked')].map(i => i.value),
            allowed_merchants: [...f.querySelectorAll('input[name=mer]:checked')].map(i => i.value),
            approval_mode: f.approval_mode.value,
            approval_threshold: Number(f.approval_threshold.value),
        };
        const r = await apiCall('/product/policy', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        if (!r.ok) return showToast(r.message, 'error');
        renderPolicy(r.data);
        $('policy-body').hidden = false;
        showToast('Policy saved.', 'success');
        if (task) refreshTask();
    }

    async function replaceAgent() {
        const r = await postJSON('/product/agents/purchase/replace');
        if (!r.ok) return showToast(r.message, 'error');
        renderPolicy(r.data);
        showToast(`New Purchase Agent: ${r.data.replaced_with}`, 'success');
        if (task) refreshTask();
    }

    // ── Ask ──────────────────────────────────────────────────────────
    document.querySelectorAll('[data-instruction]').forEach(c =>
        c.addEventListener('click', () => { $('instruction').value = c.dataset.instruction; $('instruction').focus(); }));
    $('task-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        if (busy) return;
        busy = true;
        $('phase-error').hidden = true;
        $('btn-start').disabled = true;
        const r = await postJSON('/product/agent/tasks', {instruction: $('instruction').value.trim()});
        busy = false;
        $('btn-start').disabled = false;
        if (!r.ok) { $('error-detail').textContent = r.message; $('phase-error').hidden = false; return; }
        quotes = recommendation = null;
        catalogState = {q: '', category: r.data.category};
        lastCatalog = null;
        setUrl(r.data.task_id);
        render(r.data);
        loadCatalog();
        $('phase-task').scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    $('btn-new').onclick = () => {
        stopCountdown(); task = null; quotes = recommendation = null; setUrl(null); showTask(false);
        renderPlan(null); $('hierarchy').innerHTML = '<p class="meta">Loads with a task.</p>'; $('steps-list').innerHTML = '';
        loadPolicy(); window.scrollTo({top: 0, behavior: 'smooth'}); $('instruction').focus();
    };

    // ── Actions ─────────────────────────────────────────────────────
    async function act(method, path, body) {
        if (busy || !task) return null;
        busy = true;
        if (path.startsWith('/cart')) { quotes = null; recommendation = null; }  // contents changed → re-compare
        document.querySelectorAll('#task-main button').forEach(b => b.disabled = true);
        const opts = {method, headers: {'Content-Type': 'application/json'}};
        if (body !== undefined) opts.body = JSON.stringify(body);
        const r = await apiCall(`/product/agent/tasks/${task.task_id}${path}`, opts);
        busy = false;
        if (r.ok && r.data && r.data.task_id) render(r.data);
        else if (r.data && r.data.task) { render(r.data.task); showToast(r.message, 'error'); }
        else { document.querySelectorAll('#task-main button').forEach(b => b.disabled = false); if (!r.ok) showToast(r.message, 'error'); }
        return r;
    }

    async function refreshTask() {
        if (!task) return;
        const r = await apiCall(`/product/agent/tasks/${task.task_id}`);
        if (r.ok) render(r.data);
    }

    // ── Rendering ───────────────────────────────────────────────────
    function render(t) {
        task = t;
        showTask(true);
        $('task-label').textContent = `Task ${t.task_id} · ${t.category_label}`;
        $('task-title').textContent = t.instruction;
        $('task-sub').textContent = `Budget ${fmtINR(t.budget_inr)} · Purchase Agent limit ${fmtINR(t.policy.purchase_limit)} (lowest of budget, per-transaction limit and remaining authority)`;
        const [txt, kind] = STATUS[t.status] || [t.status, ''];
        $('task-status').textContent = txt;
        $('task-status').className = 'badge ' + kind;
        renderMain(t);
        renderPlan(t);
        renderHierarchy(t.authority);
        renderSteps(t.steps || []);
    }

    // Plan: a view of the backend task state (no state is invented here).
    function renderPlan(t) {
        const has = (k) => t && (t.steps || []).some(s => s.key === k);
        const failedEnd = t && ['CANCELLED', 'EXPIRED', 'FAILED', 'PAYMENT_FAILED', 'REVIEW_REQUIRED', 'CONTAINED'].includes(t.status);
        const selected = t && t.cart && t.cart.selected_merchant;
        const reserved = has('reserved');
        const items = [
            ['Request received', t ? 'done' : 'todo', t ? 'Parsed' : ''],
            ['Search', has('search') ? 'done' : 'todo', has('search') ? 'Marketplace searched' : ''],
            ['Evaluate', (selected || reserved) ? 'done' : (t && t.status === 'CREATED' ? 'current' : 'todo'),
                selected ? 'Platform chosen' : (t && t.status === 'CREATED' ? 'Build cart · compare' : '')],
            ['Prepare payment', reserved ? 'done' : (t && t.status === 'RUNNING' ? 'current' : (failedEnd && !reserved && t.status !== 'CANCELLED' ? 'failed' : 'todo')),
                reserved ? 'Signed · checked · held' : ''],
            ['Await authorization', !t ? 'todo' : t.status === 'COMPLETED' ? 'done' : t.status === 'AWAITING_AUTHORIZATION' || t.status === 'AUTHORIZED' ? 'current' : failedEnd && reserved ? 'failed' : 'todo',
                !t ? '' : t.status === 'COMPLETED' ? 'Authorized' : t.status === 'AWAITING_AUTHORIZATION' ? 'Your decision' : failedEnd && reserved ? (STATUS[t.status] || [t.status])[0] : ''],
        ];
        $('plan').innerHTML = items.map(([name, st, note], i) =>
            `<li class="${st}"><span class="n">${String(i + 1).padStart(2, '0')}</span><span>${name}</span><span class="state">${escapeHtml(note)}</span></li>`).join('');
    }

    function renderMain(t) {
        stopCountdown();
        const main = $('task-main');
        if (t.status === 'CREATED') return renderShopping(t);
        if (t.status === 'AWAITING_AUTHORIZATION' && t.authorization) return renderReview(t);
        if (t.status === 'COMPLETED' && t.order) {
            const o = t.order, r = t.result;
            main.innerHTML = `
            <div class="review">
                <span class="badge ok">Order confirmed</span>
                <div class="review-amount">${fmtINR(o.total)}</div>
                <p class="h3">Order ${escapeHtml(o.order_number)} · ${escapeHtml(o.merchant)}</p>
                <div class="kv mt-5">
                    <div class="kv-row"><span>Items</span><span class="num">${o.item_count}</span></div>
                    <div class="kv-row"><span>Approval</span><span>${r.approval === 'user' ? 'Authorized by you' : escapeHtml(r.approval)}</span></div>
                    <div class="kv-row"><span>Agent authority</span><span class="num">${fmtINR(r.agent_authority)}</span></div>
                    <div class="kv-row"><span>Used</span><span class="num">${fmtINR(r.authority_consumed)}</span></div>
                    <div class="kv-row"><span>Returned to Main Agent</span><span class="num">${fmtINR(r.authority_returned)}</span></div>
                    <div class="kv-row"><span>Main Agent remaining</span><span class="num">${fmtINR(r.main_agent_remaining)}</span></div>
                    <div class="kv-row"><span>Payment reference</span><span class="mono">${escapeHtml(r.utr_reference)}</span></div>
                    <div class="kv-row"><span>Payment rail</span><span>Simulated</span></div>
                    ${r.drunix && r.drunix.reserve ? `<div class="kv-row"><span>Drunix Reserve</span><span class="mono" title="${escapeHtml(r.drunix.reserve.tx_id)}">${escapeHtml(r.drunix.reserve.tx_id.slice(0, 16))}… · block ${r.drunix.reserve.block_number}</span></div>` : ''}
                    ${r.drunix && r.drunix.commit ? `<div class="kv-row"><span>Drunix Commit</span><span class="mono" title="${escapeHtml(r.drunix.commit.tx_id)}">${escapeHtml(r.drunix.commit.tx_id.slice(0, 16))}… · ${r.drunix.commit.block_number != null ? 'block ' + r.drunix.commit.block_number : 'recovered'}</span></div>` : ''}
                </div>
                <div class="review-actions"><a class="btn btn-primary" href="/orders/${encodeURIComponent(o.order_number)}">View receipt ${ARROW}</a></div>
            </div>`;
            return;
        }
        if (t.status === 'CONTAINED') return renderContained(t);
        const msg = {
            CANCELLED: ['Task cancelled', 'Any hold was released and unused authority returned to the Main Agent. No payment was made.'],
            EXPIRED: ['Authorization window expired', 'The hold expired, was released, and the authority returned. No payment was made.'],
            PAYMENT_FAILED: ['Payment failed', 'The payment was not settled (the simulated rail or the Drunix ledger refused it). The hold was released; nothing was committed and no order was created.'],
            REVIEW_REQUIRED: ['Held for review', 'The behavioural model scored this request MEDIUM. Nothing was reserved.'],
            FAILED: ['Checkout failed', 'See the agent log.'],
            RUNNING: ['Working…', 'The agents are processing this task.'],
            AUTHORIZED: ['Authorizing…', 'Settling the payment.'],
        }[t.status] || [t.status, ''];
        main.innerHTML = `<div class="empty"><p class="h3">${msg[0]}</p><p class="muted mt-2">${escapeHtml(msg[1])}</p>
            ${t.error ? `<p class="meta mt-3">${escapeHtml(t.error.error)}: ${escapeHtml(t.error.detail)}</p>` : ''}</div>`;
        if (t.status === 'RUNNING' || t.status === 'AUTHORIZED') setTimeout(refreshTask, 1500);
    }

    // Shopping: marketplace + cart + platform choice
    function renderShopping(t) {
        const c = t.cart;
        const est = c.estimate;
        const limit = Number(c.purchase_limit);
        const estTotal = est ? Number(est.total) : 0;
        const pct = limit > 0 ? Math.min(100, Math.round(estTotal / limit * 100)) : 0;
        const selected = c.selected_quote;
        const pNode = (t.authority && t.authority.nodes || []).find(n => n.role === 'purchase');
        const suspended = pNode && pNode.agent_status !== 'active';
        $('task-main').innerHTML = `
        ${suspended ? `<div class="notice bad mb-5">Purchase Agent <b>${escapeHtml(pNode.agent_identifier)}</b> is ${escapeHtml(pNode.agent_status)} after a containment. Checkout is blocked until you replace it.
            <div class="mt-3"><button class="btn btn-secondary btn-sm" type="button" id="btn-replace3">Replace Purchase Agent</button></div></div>` : ''}
        ${t.error && !suspended ? `<div class="notice warn mb-5">${escapeHtml(t.error.detail)}</div>` : ''}
        ${t.budget_notice ? `<div class="notice warn mb-5" id="budget-notice"><b>Your budget is above what your policy lets one purchase spend.</b>
            ${escapeHtml(t.budget_notice.message)}
            <div class="mt-3"><button class="btn btn-secondary btn-sm" type="button" id="btn-edit-limit">${t.budget_notice.binding === 'per_purchase_limit' ? 'Edit per-purchase limit' : 'Edit overall authority'}</button></div></div>` : ''}
        <div class="shop">
            <section>
                <div class="split"><h3 class="h4">Marketplace</h3><span class="meta">Controlled simulated marketplace</span></div>
                <form id="search-form" class="inline-form mt-3">
                    <input class="input" id="search-q" placeholder="Search products — milk, atta, soap" value="${escapeHtml(catalogState.q)}">
                    <button class="btn btn-secondary" type="submit">Search</button>
                </form>
                <div class="chips mt-3" id="cat-chips"></div>
                <div id="results" class="results"><p class="meta mt-4"><span class="loading"><span class="spinner"></span>Loading products</span></p></div>
            </section>
            <section>
                <div class="split"><h3 class="h4">Cart</h3>${c.items.length ? '<button class="link link-quiet" type="button" id="btn-clear">Clear</button>' : ''}</div>
                <div class="list mt-3">
                ${c.items.length ? c.items.map(i => `
                    <div class="cart-row">
                        <div>${escapeHtml(i.name)} <span class="meta">${escapeHtml(i.unit)}</span>
                            ${i.category_allowed ? '' : '<div class="meta bad-text">Category not allowed by your policy</div>'}</div>
                        <div class="row-gap">
                            <span class="qty">
                                <button type="button" data-qty="${escapeHtml(i.product_id)}" data-n="${i.quantity - 1}" aria-label="Decrease">−</button>
                                <span>${i.quantity}</span>
                                <button type="button" data-qty="${escapeHtml(i.product_id)}" data-n="${i.quantity + 1}" aria-label="Increase">+</button>
                            </span>
                            <button type="button" class="link link-quiet" data-qty="${escapeHtml(i.product_id)}" data-n="0">Remove</button>
                        </div>
                    </div>`).join('') : '<p class="meta cart-empty">Your cart is empty. Add products from the marketplace.</p>'}
                </div>
                ${est ? `
                <div class="kv mt-4 no-top">
                    <p class="meta">${selected ? 'At your selected platform' : 'Best available simulated option'}: <b>${escapeHtml(est.merchant)}</b></p>
                    <div class="kv-row"><span>Subtotal</span><span class="num">${fmtINR(est.subtotal)}</span></div>
                    <div class="kv-row"><span>Delivery fee</span><span class="num">${Number(est.delivery_fee) === 0 ? 'Free' : fmtINR(est.delivery_fee)}</span></div>
                    <div class="kv-row total"><span>Total</span><span class="num">${fmtINR(est.total)}</span></div>
                </div>
                <div class="meter"><span style="width:${pct}%" class="${estTotal > limit ? 'over' : ''}"></span></div>
                <p class="meta mt-2">${fmtINR(est.total)} of the ${fmtINR(limit)} Purchase Agent limit ·
                    ${Number(t.budget_inr) - estTotal >= 0 ? `${fmtINR(Number(t.budget_inr) - estTotal)} left of your ${fmtINR(t.budget_inr)} budget` : `<span class="bad-text">${fmtINR(estTotal - Number(t.budget_inr))} over your ${fmtINR(t.budget_inr)} budget</span>`}</p>` :
                (c.items.length ? '<div class="notice warn mt-4">No simulated merchant can fulfil this cart within your policy and limit.</div>' : '')}
                ${c.items.length ? `<button class="btn btn-secondary btn-block mt-4" type="button" id="btn-compare">Compare platforms</button>` : ''}
            </section>
        </div>
        <div id="compare-box"></div>`;
        $('search-form').onsubmit = (e) => { e.preventDefault(); catalogState.q = $('search-q').value.trim(); loadCatalog(); };
        renderCategoryChips();
        document.querySelectorAll('[data-qty]').forEach(b => b.onclick = () =>
            act('POST', '/cart/items', {product_id: b.dataset.qty, quantity: Math.max(0, Number(b.dataset.n)), mode: 'set'}));
        if ($('btn-clear')) $('btn-clear').onclick = () => act('DELETE', '/cart/items');
        if ($('btn-compare')) $('btn-compare').onclick = compare;
        if ($('btn-replace3')) $('btn-replace3').onclick = replaceAgent;
        if ($('btn-edit-limit')) $('btn-edit-limit').onclick = () => openPolicyEditor(t.budget_notice.binding === 'per_purchase_limit' ? 'pf-txn' : 'pf-overall');
        if (selected || quotes) renderCompare();
        loadCatalog(true);
    }

    function renderCategoryChips() {
        const box = $('cat-chips'); if (!box) return;
        const all = [{id: '', label: 'All'}].concat(categories);
        const allowed = task ? task.policy.allowed_categories : [];
        box.innerHTML = all.map(c => `<button type="button" class="chip ${catalogState.category === c.id ? 'active' : ''} ${c.id && !allowed.includes(c.id) ? 'blocked' : ''}" data-cat="${escapeHtml(c.id)}"
            ${c.id && !allowed.includes(c.id) ? 'title="Not allowed by your agent policy"' : ''}>${escapeHtml(c.label)}</button>`).join('');
        box.querySelectorAll('[data-cat]').forEach(b => b.onclick = () => { catalogState.category = b.dataset.cat; renderCategoryChips(); loadCatalog(); });
    }

    async function loadCatalog(useCache) {
        if (useCache && lastCatalog) return renderResults(lastCatalog);
        const params = new URLSearchParams();
        if (catalogState.q) params.set('q', catalogState.q);
        if (catalogState.category) params.set('category', catalogState.category);
        const r = await apiCall('/product/catalog?' + params);
        if (!r.ok) { if ($('results')) $('results').innerHTML = `<div class="notice bad mt-4">${escapeHtml(r.message)}</div>`; return; }
        categories = r.data.categories;
        lastCatalog = r.data.products;
        renderCategoryChips();
        renderResults(lastCatalog);
    }

    function renderResults(products) {
        const box = $('results'); if (!box) return;
        if (!products.length) { box.innerHTML = '<div class="empty"><p class="h4">No products match</p><p class="muted">Try another search or category.</p></div>'; return; }
        const allowed = task ? task.policy.allowed_categories : [];
        box.innerHTML = products.map(p => {
            const inStock = p.offers.filter(o => o.stock > 0);
            const blocked = !allowed.includes(p.category);
            return `<div class="product-row">
                <div>
                    <div class="product-name">${escapeHtml(p.name)}</div>
                    <div class="meta">${escapeHtml(p.brand || '')} · ${escapeHtml(p.unit)} · ${escapeHtml(p.category_label)}${blocked ? ' · <span class="bad-text">not allowed by policy</span>' : ''}</div>
                    <div class="offers">${p.offers.map(o => `<span class="${o.stock > 0 ? '' : 'oos'}">${escapeHtml(o.merchant)} <span class="num">${fmtINR(o.price)}</span>${o.stock > 0 ? (o.stock <= 3 ? ` · ${o.stock} left` : '') : ' · out of stock'}</span>`).join('')}</div>
                </div>
                <div class="product-buy">
                    <span class="strong num">${p.from_price ? 'from ' + fmtINR(p.from_price) : 'Unavailable'}</span>
                    <span class="row-gap"><input type="number" min="1" max="50" value="1" class="input input-qty" aria-label="Quantity for ${escapeHtml(p.name)}" id="q-${escapeHtml(p.product_id)}">
                    <button class="btn btn-secondary btn-sm" type="button" data-add="${escapeHtml(p.product_id)}" ${inStock.length && !blocked ? '' : 'disabled'}>Add</button></span>
                </div></div>`;
        }).join('');
        box.querySelectorAll('[data-add]').forEach(b => b.onclick = () => {
            const q = Number(document.getElementById('q-' + b.dataset.add).value) || 1;
            act('POST', '/cart/items', {product_id: b.dataset.add, quantity: q, mode: 'add'});
        });
    }

    async function compare() {
        if (busy) return;
        busy = true;
        const r = await postJSON(`/product/agent/tasks/${task.task_id}/compare`);
        busy = false;
        if (!r.ok) return showToast(r.message, 'error');
        quotes = r.data.quotes; recommendation = r.data.recommendation;
        await refreshTask();
        renderCompare();
        $('compare-box').scrollIntoView({behavior: 'smooth', block: 'start'});
    }

    function renderCompare() {
        const box = $('compare-box'); if (!box || !task) return;
        const c = task.cart;
        const rows = quotes || (c.selected_quote ? [c.selected_quote] : []);
        box.innerHTML = `
        <section class="mt-7">
            <div class="section-head"><h3 class="h3">Choose a platform</h3><span class="meta">Merchant Optimization Agent · simulated merchants</span></div>
            ${recommendation ? `<div class="notice info mb-5"><b>Merchant Optimization Agent:</b> ${escapeHtml(recommendation.reason)}
                <div class="meta mt-2">Deterministic comparison of the simulated merchants' listed prices, fees and delivery times — no merchant is contacted or negotiated with.</div></div>` : ''}
            <div class="table-wrap"><table class="table quotes">
                <thead><tr><th>Platform</th><th class="r">Subtotal</th><th class="r">Delivery</th><th class="r">Total</th><th class="r">ETA</th><th></th></tr></thead>
                <tbody>${rows.map(q => `
                <tr class="quote-row ${q.eligible ? '' : 'ineligible'} ${c.selected_merchant === q.merchant_id ? 'selected' : ''}">
                    <td><span class="strong">${escapeHtml(q.merchant)}</span> <span class="tag">Simulated</span>
                        <div class="meta">${escapeHtml(q.tagline || '')}</div>
                        ${q.problems.length ? `<div class="problems">${q.problems.map(escapeHtml).join(' · ')}</div>` : ''}</td>
                    <td class="r num">${q.available ? fmtINR(q.subtotal) : '—'}</td>
                    <td class="r num">${q.available ? (Number(q.delivery_fee) === 0 ? 'Free' : fmtINR(q.delivery_fee)) : '—'}</td>
                    <td class="r num strong">${q.available ? fmtINR(q.total) : '—'}</td>
                    <td class="r num">${q.delivery_minutes} min</td>
                    <td class="r">${c.selected_merchant === q.merchant_id ? '<span class="badge ok">Selected</span>' :
                        `<button class="btn btn-secondary btn-sm" type="button" data-pick="${escapeHtml(q.merchant_id)}" ${q.eligible ? '' : 'disabled'}>Choose</button>`}</td>
                </tr>`).join('')}</tbody>
            </table></div>
            <div class="row-gap mt-4">
                ${quotes ? '' : '<button class="btn btn-secondary" type="button" id="btn-compare2">Compare all platforms</button>'}
                <button class="btn btn-secondary" type="button" id="btn-agent-decide">Let Agent Decide</button>
            </div>
            ${c.selected_merchant ? `
            <div class="panel-flat mt-6 selected-box">
                <div class="split">
                    <div><div class="eyebrow">Selected platform</div>
                        <p class="h4 mt-2">${escapeHtml(c.selected_quote ? c.selected_quote.merchant : c.selected_merchant)}</p>
                        <p class="muted body-sm mt-1">${escapeHtml(c.selection_reason || '')}</p></div>
                    <button class="btn btn-primary btn-lg" type="button" id="btn-execute">Prepare payment ${c.selected_quote ? '· ' + fmtINR(c.selected_quote.total) : ''} ${ARROW}</button>
                </div>
                <p class="meta mt-3">Policy check → Main Agent delegates bounded authority → Purchase Agent signs → AgentGuard verifies → risk check → amount held (with Drunix enforcement on, the Delegate and the Reserve must be VALID on Drunix).
                    ${task.policy.approval_mode === 'always' ? 'You authorize before payment.' : 'Your approval mode may let the agent settle automatically.'}</p>
            </div>` : ''}
        </section>`;
        box.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => act('POST', '/merchant', {mode: 'user', merchant_id: b.dataset.pick}));
        if ($('btn-agent-decide')) $('btn-agent-decide').onclick = () => act('POST', '/merchant', {mode: 'agent'});
        if ($('btn-compare2')) $('btn-compare2').onclick = compare;
        if ($('btn-execute')) $('btn-execute').onclick = async () => {
            $('btn-execute').innerHTML = '<span class="spinner"></span> Agents are preparing';
            const r = await act('POST', '/execute');
            if (r && r.ok && r.data.status === 'COMPLETED') showToast('Settled automatically under your agent policy.', 'success');
        };
    }

    // Review: PAYMENT REQUEST — banking confirmation
    function renderReview(t) {
        const a = t.authorization;
        const cell = (k, v, good, warn) => `<div class="check-cell"><div class="k">${k}</div>
            <div class="v ${good ? 'ok' : warn ? 'warn' : 'bad'}">${good ? CHECK : CROSS}${escapeHtml(v)}</div></div>`;
        $('task-main').innerHTML = `
        ${t.error ? `<div class="notice bad mb-5">${escapeHtml(t.error.error)}: ${escapeHtml(t.error.detail)}</div>` : ''}
        <div class="review">
            <div class="eyebrow">Payment request</div>
            <div class="review-amount">${fmtINR(a.total)}</div>
            <p class="h3">to ${escapeHtml(a.merchant)} <span class="tag">Simulated merchant</span></p>
            <p class="muted body-sm mt-2">The amount is held, not paid. Authorizing lets the Purchase Agent settle it.</p>

            <div class="kv mt-6">
                <div class="kv-row"><span>Merchant</span><span>${escapeHtml(a.merchant)} · ~${a.delivery_minutes} min delivery</span></div>
                <div class="kv-row"><span>Amount</span><span class="num">${fmtINR(a.total)}</span></div>
                <div class="kv-row"><span>Authority</span><span class="num">${fmtINR(a.agent_authority)} delegated to <span class="mono">${escapeHtml(a.agent)}</span></span></div>
                <div class="kv-row"><span>Remaining</span><span class="num">${fmtINR(a.remaining_after_payment)} · returned to the Main Agent after payment</span></div>
                <div class="kv-row"><span>Hold expires in</span><span class="mono countdown" id="countdown">—</span></div>
            </div>

            <div class="checks">
                ${cell('Identity', a.checks.identity, a.checks.identity === 'VERIFIED')}
                ${cell('Signature', a.checks.signature, a.checks.signature === 'VERIFIED')}
                ${cell('Policy', a.checks.policy, a.checks.policy === 'WITHIN LIMIT')}
                ${cell('Risk', a.risk.level + ' · ' + a.checks.risk, a.checks.risk === 'ALLOW', a.checks.risk === 'REVIEW')}
                ${cell('Reservation', a.checks.reservation, a.checks.reservation === 'RESERVED')}
                ${a.checks.drunix ? cell('Drunix', a.checks.drunix, a.checks.drunix.startsWith('VALID'), a.checks.drunix === 'DISABLED') : ''}
            </div>
            ${a.drunix && a.drunix.reserve ? `<p class="meta">Held on the Drunix ledger by agentauth.Reserve · tx <span class="mono" title="${escapeHtml(a.drunix.reserve.tx_id)}">${escapeHtml(a.drunix.reserve.tx_id.slice(0, 16))}…</span> · block ${a.drunix.reserve.block_number}. Authorizing submits agentauth.Commit; the payment is recorded only after Drunix commits it VALID.</p>` : ''}
            <p class="meta">Identity key ${escapeHtml(a.agent_key_fingerprint)} · risk score ${Number(a.risk.anomaly_score).toFixed(4)} (${escapeHtml(a.risk.engine)})</p>

            <details class="mt-5">
                <summary class="link">Items (${a.lines.reduce((n, l) => n + l.quantity, 0)})</summary>
                <div class="table-wrap mt-3"><table class="table">
                    <thead><tr><th>Item</th><th class="r">Qty</th><th class="r">Price</th><th class="r">Amount</th></tr></thead>
                    <tbody>${a.lines.map(l => `<tr><td>${escapeHtml(l.name)} <span class="meta">${escapeHtml(l.unit || '')}</span></td><td class="r num">${l.quantity}</td><td class="r num">${fmtINR(l.unit_price)}</td><td class="r num">${fmtINR(l.line_total)}</td></tr>`).join('')}</tbody>
                </table></div>
                <div class="kv mt-3 no-top">
                    <div class="kv-row"><span>Subtotal</span><span class="num">${fmtINR(a.subtotal)}</span></div>
                    <div class="kv-row"><span>Delivery fee</span><span class="num">${Number(a.delivery_fee) === 0 ? 'Free' : fmtINR(a.delivery_fee)}</span></div>
                    <div class="kv-row total"><span>Total</span><span class="num">${fmtINR(a.total)}</span></div>
                </div>
            </details>

            <div class="review-actions">
                <button class="btn btn-primary btn-lg" type="button" id="btn-authorize">Authorize ${fmtINR(a.total)}</button>
                <button class="btn btn-danger btn-lg" type="button" id="btn-cancel">Cancel and release hold</button>
            </div>
            <p class="meta mt-3">Simulated payment rail — no real money moves.</p>

            <details class="mt-6 sim-details">
                <summary class="link link-quiet">Security demo: Behavioural Risk Simulation on this agent</summary>
                <p class="body-sm muted mt-3">Controlled Security Simulation. AgentGuard injects a clearly labelled burst of synthetic rapid holds on
                    this Purchase Agent's capability; these are not real history. The agent then sends one genuine signed request for most of its
                    remaining authority, at an allowed merchant it has never used. The real IsolationForest decides. On HIGH, the capability
                    is revoked, all holds (including this payment) are released, and the agent is suspended.</p>
                <button class="btn btn-secondary btn-sm mt-3" type="button" id="btn-sim">Run simulation</button>
            </details>
        </div>`;
        $('btn-authorize').onclick = () => { $('btn-authorize').innerHTML = '<span class="spinner"></span> Authorizing'; act('POST', '/authorize-payment'); };
        $('btn-cancel').onclick = () => act('POST', '/cancel');
        $('btn-sim').onclick = async () => {
            if (!confirm('Run the controlled Behavioural Risk Simulation? If the model returns HIGH, this agent is contained.')) return;
            const r = await act('POST', '/trigger-anomaly');
            if (r && r.ok) showToast(r.data.simulation_status === 'contained' ? 'HIGH risk — agent contained.' :
                `Model returned ${r.data.risk_level || '—'}; not contained.`, r.data.simulation_status === 'contained' ? 'error' : 'warning');
        };
        startCountdown(a.expires_at);
    }

    function renderContained(t) {
        const s = t.simulation;
        $('task-main').innerHTML = `
        <div class="review">
            <span class="badge bad">Agent contained</span>
            <p class="h2 mt-4">The Purchase Agent's behaviour was not valid, so its authority was withdrawn.</p>
            ${s ? `<p class="meta mt-3">${escapeHtml(s.label)} — synthetic behaviour, not real customer history.</p>` : ''}
            ${s && s.risk ? `<div class="kv mt-6">
                <div class="kv-row"><span>Signature</span><span class="ok-text">Verified</span></div>
                <div class="kv-row"><span>Authority</span><span class="ok-text">Within limits</span></div>
                <div class="kv-row"><span>Simulated history</span><span>${s.simulated_history.holds} rapid holds · ${fmtINR(s.simulated_history.total)}</span></div>
                <div class="kv-row"><span>Live request</span><span>${fmtINR(s.live_request.amount)} at ${escapeHtml(s.live_request.merchant)}</span></div>
                <div class="kv-row"><span>IsolationForest</span><span class="bad-text strong">${escapeHtml(s.risk.level)} · ${Number(s.risk.anomaly_score).toFixed(4)}</span></div>
                <div class="kv-row"><span>Reasons</span><span>${(s.risk.reasons || []).map(escapeHtml).join('<br>')}</span></div>
                <div class="kv-row"><span>Revocation</span><span>Capability ${escapeHtml(s.containment.capability_status)} · ${s.containment.released_reservations} holds released (${fmtINR(s.containment.released_amount)})</span></div>
                <div class="kv-row"><span>Agent</span><span class="mono">${escapeHtml(s.containment.agent.agent)} · ${escapeHtml(s.containment.agent.agent_status)}</span></div>
                <div class="kv-row"><span>Next request</span><span>${s.follow_up_request.blocked ? 'Blocked — ' + escapeHtml(s.follow_up_request.error) : 'Not blocked'}</span></div>
            </div>` : `<p class="muted mt-3">${escapeHtml((t.error || {}).detail || '')}</p>`}
            <div class="review-actions">
                <button class="btn btn-primary" type="button" id="btn-replace2">Replace Purchase Agent</button>
                <a class="btn btn-secondary" href="/security">Open Security Center ${ARROW}</a>
            </div>
        </div>`;
        $('btn-replace2').onclick = replaceAgent;
    }

    function renderHierarchy(a) {
        if (!a || !a.nodes) { $('hierarchy').innerHTML = ''; return; }
        const row = (n, child) => {
            const st = n.status || (n.agent_status === 'active' ? 'NO GRANT' : n.agent_status.toUpperCase());
            const cls = n.status === 'ACTIVE' ? 'ok' : n.status === 'EXHAUSTED' ? '' : (!n.status && n.agent_status === 'active') ? '' : 'bad';
            return `<tr>
                <td class="${child ? 'indent' : ''}"><span class="role">${escapeHtml(n.label)}</span>
                    <div class="meta mono">${escapeHtml(n.agent_identifier || '')}${n.agent_status && n.agent_status !== 'active' ? ' · ' + escapeHtml(n.agent_status) : ''}</div></td>
                <td class="r num">${n.total !== undefined ? fmtINR(n.total) : '—'}
                    ${n.total !== undefined && (Number(n.reserved) > 0 || Number(n.committed) > 0) ? `<div class="meta">held ${fmtINR(n.reserved)} · spent ${fmtINR(n.committed)}</div>` : ''}</td>
                <td class="r"><span class="badge ${cls} plain">${escapeHtml(st)}</span></td></tr>`;
        };
        const by = Object.fromEntries(a.nodes.map(n => [n.role, n]));
        $('hierarchy').innerHTML = `
            <p class="meta">Your mandate: <b class="num">${fmtINR(a.mandate_total)}</b></p>
            <table class="table auth-table mt-2"><tbody>
                ${by.main ? row(by.main, false) : ''}${['search', 'negotiation', 'purchase'].filter(k => by[k]).map(k => row(by[k], true)).join('')}
            </tbody></table>
            <p class="meta mt-2 ${a.conservation_ok ? 'ok-text' : 'bad-text'}">${a.conservation_ok ? 'Authority conserved across the hierarchy' : 'Conservation check failed'}</p>`;
    }

    function renderSteps(steps) {
        $('steps-list').innerHTML = steps.slice().reverse().map(s => {
            const cls = s.status === 'completed' ? (s.key && s.key.startsWith('drunix') ? 'drunix' : '') : s.status === 'failed' ? 'failed' : 'pending';
            const tags = [];
            if (s.signing_verified) tags.push('<span class="tag">Ed25519 verified</span>');
            if (s.risk_level) tags.push(`<span class="tag">Risk ${escapeHtml(s.risk_level)} · ${Number(s.anomaly_score).toFixed(4)}</span>`);
            if (s.authority !== undefined) tags.push(`<span class="tag">${fmtINR(s.authority)}</span>`);
            if (s.drunix_tx) tags.push(`<span class="tag tag-drunix" title="${escapeHtml(s.drunix_tx)}">Drunix tx ${escapeHtml(s.drunix_tx.slice(0, 12))}…${s.drunix_block != null ? ' · block ' + s.drunix_block : ''}</span>`);
            if (s.drunix_code) tags.push(`<span class="tag tag-drunix-bad">Drunix: ${escapeHtml(s.drunix_code)}</span>`);
            return `<li class="${cls}"><span class="dot"></span><div><div class="t">${escapeHtml(s.name)}</div>
                <div class="d">${escapeHtml(s.detail)}</div>${tags.length ? `<div class="tags">${tags.join('')}</div>` : ''}</div></li>`;
        }).join('');
    }

    function startCountdown(expiresAt) {
        const end = new Date(expiresAt).getTime();
        const tick = () => {
            const el = $('countdown'); if (!el) return stopCountdown();
            const left = Math.max(0, Math.round((end - Date.now()) / 1000));
            el.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
            el.classList.toggle('urgent', left <= 20);
            if (left <= 0) { stopCountdown(); setTimeout(refreshTask, 1500); }
        };
        tick();
        countdownTimer = setInterval(tick, 1000);
    }
    function stopCountdown() { if (countdownTimer) clearInterval(countdownTimer); countdownTimer = null; }

    // ── Boot ─────────────────────────────────────────────────────────
    const params = new URLSearchParams(location.search);
    if (params.get('instruction')) $('instruction').value = params.get('instruction');
    loadPolicy();
    renderPlan(null);
    if (params.get('task')) {
        apiCall(`/product/agent/tasks/${encodeURIComponent(params.get('task'))}`).then(r => {
            if (r.ok) { catalogState.category = r.data.category; render(r.data); loadCatalog(); }
            else { setUrl(null); showTask(false); }
        });
    } else {
        showTask(false);
    }
})();
