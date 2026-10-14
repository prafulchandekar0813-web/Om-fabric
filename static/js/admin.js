document.addEventListener('DOMContentLoaded', () => {
  // Confirm before destructive actions: <form data-confirm="…"> or <button data-confirm-btn="…">
  document.addEventListener('submit', e => {
    const f = e.target, btn = e.submitter;
    const msg = (btn && btn.dataset.confirmBtn) || f.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Product form: add another variant row
  const add = document.getElementById('add-variant'), box = document.getElementById('variants');
  if (add && box) add.addEventListener('click', () => {
    const row = box.lastElementChild.cloneNode(true);
    row.querySelectorAll('input').forEach(i => { i.value = ''; });
    row.querySelectorAll('select').forEach(s => { s.selectedIndex = 0; });
    box.appendChild(row);
    row.querySelector('input[name=v_title]').focus();
  });
});
