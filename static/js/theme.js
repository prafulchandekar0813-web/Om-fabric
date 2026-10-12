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
