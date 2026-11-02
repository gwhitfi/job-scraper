document.addEventListener('click', async event => {
  const button = event.target.closest('.mark, .save-note, .employer-action');
  if (!button) return;
  const card = button.closest('[data-key]');
  const status = card.querySelector('.save-status');
  const employer = button.classList.contains('employer-action');
  const note = button.classList.contains('save-note');
  const field = note ? 'note' : button.dataset.field;
  const value = note ? card.querySelector('textarea').value : button.getAttribute('aria-pressed') !== 'true';
  button.disabled = true;
  status.textContent = 'Saving…';
  try {
    const response = await fetch(employer ? '/api/employer' : '/api/mark?jev=v6', {
      method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'jobscraper'},
      body:JSON.stringify(employer
        ? {employer:button.dataset.employer,action:button.dataset.action}
        : {key:card.dataset.key,field,value,page:'/jev'})
    });
    if (!response.ok) throw new Error(await response.text());
    if (note) status.textContent = 'Note saved.';
    else location.reload();
  } catch (error) { status.textContent = 'Could not save: ' + error.message; }
  finally { button.disabled = false; }
});
