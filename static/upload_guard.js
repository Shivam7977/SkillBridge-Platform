/* Upload guard: checks file size / type in the browser BEFORE the upload starts, and shows
   a clear message instead of letting the server reject (or reset) a huge request.
   Usage: <input type="file" data-max-mb="15" accept=".zip"> + this script. */
(function () {
  'use strict';

  function formatMB(bytes) { return (bytes / 1048576).toFixed(1) + ' MB'; }

  function messageBox(input) {
    let box = input.parentElement.querySelector('.upload-guard-msg');
    if (!box) {
      box = document.createElement('div');
      box.className = 'upload-guard-msg';
      box.setAttribute('role', 'alert');
      box.style.cssText = 'display:none;margin-top:8px;padding:10px 12px;border-radius:10px;' +
        'background:#fdecea;color:#b3261e;border:1px solid #f5c2be;font-size:13px;font-weight:600;line-height:1.4;';
      input.insertAdjacentElement('afterend', box);
    }
    return box;
  }

  function allowedExtensions(input) {
    return (input.getAttribute('accept') || '').split(',')
      .map(s => s.trim().toLowerCase()).filter(s => s.startsWith('.'));
  }

  function problemFor(input, file) {
    const maxMB = parseFloat(input.dataset.maxMb || '15');
    const exts = allowedExtensions(input);
    const name = file.name || 'This file';
    if (exts.length) {
      const dot = name.lastIndexOf('.');
      const ext = dot >= 0 ? name.slice(dot).toLowerCase() : '';
      if (!exts.includes(ext)) return '"' + name + '" is not allowed. Please choose a ' + exts.join(' / ') + ' file.';
    }
    if (file.size === 0) return '"' + name + '" is empty. Please choose a different file.';
    if (file.size > maxMB * 1048576) {
      return '"' + name + '" is ' + formatMB(file.size) + ', but the maximum allowed size is ' + maxMB + ' MB. ' +
        'Tip: remove folders like node_modules, venv, .git or build files and compress again.';
    }
    return '';
  }

  function check(input) {
    const box = messageBox(input);
    const file = input.files && input.files[0];
    if (!file) { box.style.display = 'none'; return true; }
    const problem = problemFor(input, file);
    if (problem) {
      box.textContent = problem;
      box.style.display = 'block';
      input.value = '';                 // clear the bad selection so it can't be submitted
      return false;
    }
    box.style.display = 'none';
    return true;
  }

  function init() {
    document.querySelectorAll('input[type="file"][data-max-mb]').forEach(function (input) {
      input.addEventListener('change', function () { check(input); });
      const form = input.closest('form');
      if (form) {
        form.addEventListener('submit', function (e) {
          if (input.files && input.files[0] && !check(input)) e.preventDefault();
        });
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();