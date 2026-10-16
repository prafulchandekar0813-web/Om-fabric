document.addEventListener('DOMContentLoaded', () => {
  // Mobile navigation (same behaviour as the Shopify theme)
  const b = document.querySelector('.menu-toggle'), n = document.querySelector('#MainNav');
  if (b && n) b.addEventListener('click', () => {
    const open = b.getAttribute('aria-expanded') === 'true';
    b.setAttribute('aria-expanded', String(!open));
    n.classList.toggle('is-open', !open);
    b.setAttribute('aria-label', open ? 'Open navigation' : 'Close navigation');
  });

  // Update the displayed price when a different variant is chosen
  const sel = document.querySelector('#variant'), price = document.querySelector('.product-detail .price');
  if (sel && price) sel.addEventListener('change', () => {
    const o = sel.options[sel.selectedIndex];
    if (o && o.dataset.price) price.textContent = o.dataset.price;
  });
});

// Checkout: show the matching payment note and update the total when a COD fee applies
document.addEventListener('DOMContentLoaded', () => {
  const pay = document.getElementById('payment');
  if (!pay) return;
  const notes = document.querySelectorAll('#payment-note [data-for]');
  const fee = document.getElementById('cod-fee-row'), total = document.getElementById('grand-total');
  const rupees = n => '₹' + Number(n).toLocaleString('en-IN');
  const update = () => {
    notes.forEach(n => n.hidden = n.dataset.for !== pay.value);
    if (fee && total) {
      const amt = Number(fee.dataset.fee), cod = pay.value === 'cod' && amt > 0;
      fee.hidden = !cod;
      total.textContent = rupees(Number(total.dataset.base) + (cod ? amt : 0));
    }
  };
  pay.addEventListener('change', update);
  update();
});

// Checkout (guests): remember the "email me a reminder" choice — only if they tick the box
document.addEventListener('DOMContentLoaded', () => {
  const box = document.getElementById('remind'), email = document.getElementById('email');
  const tokenEl = document.querySelector('input[name=csrf_token]');
  if (!box || !email || !tokenEl) return;
  let last = box.checked ? '' : '0';
  const send = () => {
    const consent = box.checked && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim());
    const key = consent ? '1:' + email.value.trim().toLowerCase() : '0';
    if (key === last) return;
    last = key;
    fetch('/cart/remind', { method: 'POST', credentials: 'same-origin',
      body: new URLSearchParams({ csrf_token: tokenEl.value, consent: consent ? '1' : '0', email: email.value.trim() }) }).catch(() => {});
  };
  box.addEventListener('change', send);
  email.addEventListener('change', send);
  email.addEventListener('blur', send);
});
