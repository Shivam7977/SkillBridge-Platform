// Browser-side profile validators (education/marks, work experience). Run:  node tests/js/profile_validators.test.js
// Needs only Node. It reads the real code out of templates/profile.html, so it can never drift from the page.
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', '..', 'templates', 'profile.html'), 'utf8');
let js = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n;\n');
js = js.replace(/\{\{[\s\S]*?\}\}/g, 'null').replace(/\{%[\s\S]*?%\}/g, '');       // drop Jinja tags

const slice = (start, end) => { const s = js.indexOf(start), e = js.indexOf(end, s); if (s < 0 || e < 0) throw new Error('not found: ' + start); return js.slice(s, e); };
const code = slice('const RULES', 'function blockBadGradeKeys') + slice('function blockBadGradeKeys', 'function sync()')
  + slice('function validateEduEntries()', '\nfunction refreshEduEntryView') + slice('function validateExpEntries()', '\nfunction refreshExpEntryView');

const CY = new Date().getFullYear();
let toasts, marked;
const field = (value = '', extra = {}) => ({ value, checked: false, style: { display: 'block' }, focus() {}, ...extra });
const entry = map => ({ querySelector: sel => map[sel] || null });
function run(kind, entries) {
  toasts = []; marked = new Set();
  const doc = { querySelectorAll: s => (s === '.edu-entry' && kind === 'edu') || (s === '.exp-entry' && kind === 'exp') ? entries : [] };
  const api = new Function('document', 'showToast', 'markFieldError', 'clearFieldError',
    code + '; return { validateEduEntries, validateExpEntries, blockBadExpKeys, blockBadGradeKeys, yearsLabel };')(
    doc, m => toasts.push(m), e => marked.add(e), e => marked.delete(e));
  return { api, ok: (kind === 'edu' ? api.validateEduEntries : api.validateExpEntries)() };
}
const edu = (o = {}) => entry({
  '.edu-institution': field(o.inst ?? 'VIVA Institute'), '.edu-degree': field(o.deg ?? 'B.E.'), '.edu-field': field(o.field ?? 'Computer Engineering'),
  '.edu-grade-value': field(o.grade ?? '8.82'), '.edu-grade-type': field(o.type ?? 'CGPA'),
  '.edu-from': field(o.from ?? '2023'), '.edu-to': field(o.to ?? '2027'), '.edu-current': { checked: !!o.current },
});
const job = (o = {}) => entry({
  '.exp-company': field(o.co ?? 'Acme'), '.exp-role': field(o.role ?? 'ML Intern'), '.exp-from': field(o.from ?? '2025'),
  '.exp-to': field(o.to ?? String(CY)), '.exp-current': { checked: !!o.current }, '.exp-bullets': field(o.bullets ?? 'Built a model\nShipped API'),
});

// [name, kind, entries, should the form be accepted?, text the error must contain]
const cases = [
  ['education: normal, graduating 2027', 'edu', [edu()], true],
  ['education: CGPA 0 is allowed', 'edu', [edu({ grade: '0' })], true],
  ['education: CGPA 10 is allowed', 'edu', [edu({ grade: '10' })], true],
  ['education: Percentage 100 is allowed', 'edu', [edu({ grade: '100', type: 'Percentage' })], true],
  ['education: currently studying ignores To Year', 'edu', [edu({ current: true, to: '' })], true],
  ['education: no entries is fine', 'edu', [], true],
  ['education: CGPA 11', 'edu', [edu({ grade: '11' })], false, 'between 0 and 10'],
  ['education: CGPA 10.01', 'edu', [edu({ grade: '10.01' })], false, 'between 0 and 10'],
  ['education: CGPA -1', 'edu', [edu({ grade: '-1' })], false, 'cannot be negative'],
  ['education: CGPA 8.777', 'edu', [edu({ grade: '8.777' })], false, '2 decimal'],
  ['education: grade is text', 'edu', [edu({ grade: 'abc' })], false, 'must be a number'],
  ['education: Percentage 105', 'edu', [edu({ grade: '105', type: 'Percentage' })], false, 'between 0 and 100'],
  ['education: From Year 1975', 'edu', [edu({ from: '1975' })], false, 'From Year must be between 1980'],
  ['education: From Year in the future', 'edu', [edu({ from: String(CY + 1) })], false, 'From Year'],
  ['education: To Year before From Year', 'edu', [edu({ from: '2023', to: '2020' })], false, 'To Year must be between 2023'],
  ['education: To Year far in the future', 'edu', [edu({ to: String(CY + 20) })], false, 'To Year'],
  ['education: institution 1 character', 'edu', [edu({ inst: 'A' })], false, 'at least 2'],
  ['education: second entry is bad', 'edu', [edu(), edu({ grade: '12' })], false, 'Education #2'],
  ['education: 11 entries', 'edu', Array.from({ length: 11 }, () => edu()), false, 'at most 10'],
  ['work: normal', 'exp', [job()], true],
  ['work: currently working ignores To Year', 'exp', [job({ current: true, to: '' })], true],
  ['work: To Year in the future', 'exp', [job({ to: String(CY + 1) })], false, 'To Year'],
  ['work: From Year 1979', 'exp', [job({ from: '1979' })], false, 'From Year'],
  ['work: role empty', 'exp', [job({ role: '' })], false, 'Role is required'],
  ['work: 11 achievements', 'exp', [job({ bullets: Array.from({ length: 11 }, (_, i) => 'item ' + i).join('\n') })], false, 'at most 10'],
  ['work: achievement of 301 characters', 'exp', [job({ bullets: 'x'.repeat(301) })], false, 'at most 300'],
];

let failed = 0;
for (const [name, kind, entries, expected, fragment] of cases) {
  const { ok } = run(kind, entries);
  const message = toasts[0] || '';
  const good = ok === expected && (!fragment || message.toLowerCase().includes(fragment.toLowerCase()));
  if (!good) failed++;
  console.log((good ? 'PASS  ' : 'FAIL  ') + name + (message ? '  ->  ' + message : ''));
}

// small helpers
const { api } = run('edu', []);
const check = (name, actual, expected) => { const good = actual === expected; if (!good) failed++; console.log((good ? 'PASS  ' : 'FAIL  ') + name); };
check('key filter blocks "-" in the experience box', api.blockBadExpKeys({ key: '-' }), false);
check('key filter blocks "e" in the experience box', api.blockBadExpKeys({ key: 'e' }), false);
check('key filter allows digits', api.blockBadExpKeys({ key: '5' }), true);
check('grade box allows a decimal point', api.blockBadGradeKeys({ key: '.' }), true);
check('grade box blocks "-"', api.blockBadGradeKeys({ key: '-' }), false);
check('"1 Year" is singular', api.yearsLabel(1), '1 Year');
check('"3 Years" is plural', api.yearsLabel(3), '3 Years');

console.log(failed ? `\n${failed} FAILED` : `\nAll ${cases.length + 7} checks passed`);
process.exit(failed ? 1 : 0);
