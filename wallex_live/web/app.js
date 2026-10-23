async function refresh() {
  try {
    const response = await fetch('/api/status', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('دسترسی به وضعیت امکان‌پذیر نیست');
    const state = await response.json();
    document.getElementById('state').textContent = state.state || 'نامشخص';
    document.getElementById('message').textContent = state.connected
      ? 'موجودی تأییدشده از API والکس؛ آخرین همگام‌سازی: ' + new Date(state.last_account_sync * 1000).toLocaleString('fa-IR')
      : 'اتصال تأیید نشده است. ' + (state.error || '');
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
