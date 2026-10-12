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
