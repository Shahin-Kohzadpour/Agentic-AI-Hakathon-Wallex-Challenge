async function refresh() {
  try {
    const response = await fetch('/api/status', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('دسترسی به وضعیت امکان‌پذیر نیست');
    const state = await response.json();
    const states = {authentication_failed: 'خطای احراز هویت والکس', configuration_required: 'تنظیمات نیاز به تکمیل دارد',
                    connected_orders_disarmed: 'متصل؛ ارسال سفارش غیرفعال', live_monitoring: 'پایش واقعی بازار',
                    liquidating_to_irr: 'در حال تسویه', settled_irr: 'تسویه تأیید شد',
                    settlement_pending_residuals: 'تسویه ناقص؛ دارایی باقی مانده', blocked: 'اجرا متوقف شده'};
    document.getElementById('state').textContent = states[state.state] || state.state || 'نامشخص';
    const armed = state.orders_enabled && state.connected && state.ready;
    document.getElementById('orders').textContent = armed ? 'فعال' : 'غیرفعال';
    document.getElementById('orders-help').textContent = armed
      ? 'معامله فقط پس از تأیید شروط استراتژی و محدودیت سرمایه انجام می‌شود'
      : 'تا رفع مانع و فعال‌سازی، معامله و تسویه خودکار اجرا نمی‌شود';
    const errors = {wallex_http_401: 'والکس کلید یا دسترسی این حساب را نپذیرفته است.',
                    account_scope_required: 'نوع حساب والکس باید مشخص شود.',
                    credit_client_id_required: 'شناسه حساب اعتبار معاملاتی لازم است.',
                    final_liquidation_scope_confirmation_required: 'دامنه تسویه نهایی هنوز تأیید نشده است.'};
    document.getElementById('message').textContent = state.connected
      ? 'موجودی تأییدشده از API والکس؛ آخرین همگام‌سازی: ' + new Date(state.last_account_sync * 1000).toLocaleString('fa-IR')
      : 'اتصال تأیید نشده است. ' + (errors[state.error] || state.error || '');
    const target = document.getElementById('balances');
    target.replaceChildren();
    if (state.connected) {
      Object.entries(state.balances || {}).filter(([, b]) => Number(b.total) > 0).forEach(([asset, b]) => {
        const row = document.createElement('tr');
        [asset, b.total, b.free, b.locked].forEach(value => {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.appendChild(cell);
        });
        target.appendChild(row);
      });
    }
    const fields = ['orders_enabled', 'open_orders', 'position', 'cash_irr', 'entry_blocker',
                    'settlement_blockers', 'residuals', 'calibration', 'error'];
    document.getElementById('details').textContent = JSON.stringify(
      Object.fromEntries(fields.filter(k => state[k] !== undefined).map(k => [k, state[k]])), null, 2);
  } catch (error) {
    document.getElementById('message').textContent = error.message;
  }
}
refresh();
setInterval(refresh, 10000);
