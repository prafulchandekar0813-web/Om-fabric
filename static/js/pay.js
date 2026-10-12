// Opens Razorpay's secure payment window and sends the result to our server for verification.
(function () {
  const el = document.getElementById('pay');
  if (!el) return;
  const btn = document.getElementById('pay-btn'), msg = document.getElementById('pay-msg');
  function start() {
    if (typeof Razorpay === 'undefined') { msg.textContent = 'Could not load the payment window. Check your connection and press “Pay now”.'; return; }
    const rzp = new Razorpay({
      key: el.dataset.key, amount: Number(el.dataset.amount), currency: 'INR',
      name: el.dataset.shop, description: el.dataset.desc, order_id: el.dataset.order,
      prefill: { name: el.dataset.name, email: el.dataset.email, contact: el.dataset.phone },
      theme: { color: '#22221f' },
      handler: function (r) {
        msg.textContent = 'Confirming your payment…';
        document.getElementById('rz_payment_id').value = r.razorpay_payment_id;
        document.getElementById('rz_order_id').value = r.razorpay_order_id;
        document.getElementById('rz_signature').value = r.razorpay_signature;
        document.getElementById('verify').submit();
      },
      modal: { ondismiss: function () { msg.textContent = 'Payment not completed yet. Press “Pay now” to try again.'; } }
    });
    rzp.on('payment.failed', function (r) {
      msg.textContent = 'Payment failed' + (r.error && r.error.description ? ': ' + r.error.description : '') + '. You can try again.';
    });
    rzp.open();
  }
  btn.addEventListener('click', start);
  window.addEventListener('load', start);
})();
