/**
 * Shared backend for the AI Canvas Prompt Library: thumbs up/down votes and
 * student-submitted prompts, stored in the Google Sheet this script is attached to.
 *
 * Setup (once): paste this into Extensions → Apps Script of a new Google Sheet,
 * then Deploy → New deployment → Web app, Execute as: Me, Who has access: Anyone.
 * The web app URL goes into the prompt library build (see build_prompt_library.py).
 *
 * Sheets (created automatically on first use):
 *   Votes       — one row per (voter, prompt), so a browser can change or remove its
 *                 vote but never vote twice on the same prompt.
 *   Submissions — student prompts. They appear in the library only after you type
 *                 "yes" in the approved column. Delete a row to remove a prompt.
 */

const VOTES = 'Votes';
const SUBMISSIONS = 'Submissions';
const HEADERS = {
  [VOTES]: ['updated_at', 'prompt_id', 'voter', 'vote'],
  [SUBMISSIONS]: ['submitted_at', 'id', 'approved', 'title', 'prompt', 'area', 'expect', 'voter'],
};
const PROMPT_ID = /^(P|S)\d{3,4}$/;
const VOTER_ID = /^[a-z0-9-]{16,40}$/;
const MAX_SUBMISSIONS_PER_VOTER = 10;
// Only needed when the script isn't attached to the sheet (a standalone Apps Script project):
// the ID is the long string in the sheet's URL between /d/ and /edit.
const SPREADSHEET_ID = '';

function sheet_(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet() || SpreadsheetApp.openById(SPREADSHEET_ID);
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(HEADERS[name]);
    sh.setFrozenRows(1);
  }
  return sh;
}

function rows_(name) {
  return sheet_(name).getDataRange().getValues().slice(1);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function clean_(value, max) {
  return String(value || '').replace(/[\u0000-\u0008\u000b-\u001f]/g, '').trim().slice(0, max);
}

// GET → { counts: { P054: { up, down }, ... }, community: [ { id, title, prompt, area, expect }, ... ] }
function doGet() {
  const counts = {};
  rows_(VOTES).forEach(([, id, , vote]) => {
    if (vote !== 'up' && vote !== 'down') return;
    const c = counts[id] || (counts[id] = { up: 0, down: 0 });
    c[vote] += 1;
  });
  const community = rows_(SUBMISSIONS)
    .filter(r => String(r[2]).trim().toLowerCase() === 'yes')
    .map(([, id, , title, prompt, area, expect]) => ({ id, title, prompt, area, expect }));
  return json_({ counts, community });
}

// POST body is sent as text/plain to avoid a CORS preflight. Two actions:
//   { "action": "vote",   "id": "P054", "voter": "...", "vote": "up" | "down" | "clear" }
//   { "action": "submit", "voter": "...", "prompt": "...", "title": "...", "area": "...", "expect": "..." }
function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: 'bad json' }); }
  if (!body || !VOTER_ID.test(body.voter || '')) return json_({ ok: false, error: 'invalid voter' });

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    return body.action === 'submit' ? submit_(body) : vote_(body);
  } finally {
    lock.releaseLock();
  }
}

function vote_({ id, voter, vote }) {
  if (!PROMPT_ID.test(id || '') || ['up', 'down', 'clear'].indexOf(vote) < 0) return json_({ ok: false, error: 'invalid vote' });
  const sh = sheet_(VOTES);
  const rows = sh.getDataRange().getValues();
  let rowIndex = -1;
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][1] === id && rows[i][2] === voter) { rowIndex = i + 1; break; }
  }
  if (vote === 'clear') {
    if (rowIndex > 0) sh.deleteRow(rowIndex);
  } else if (rowIndex > 0) {
    sh.getRange(rowIndex, 1, 1, 4).setValues([[new Date(), id, voter, vote]]);
  } else {
    sh.appendRow([new Date(), id, voter, vote]);
  }
  return json_({ ok: true });
}

function submit_(body) {
  const prompt = clean_(body.prompt, 600);
  if (prompt.length < 10) return json_({ ok: false, error: 'prompt too short' });
  const sh = sheet_(SUBMISSIONS);
  const rows = sh.getDataRange().getValues().slice(1);
  if (rows.filter(r => r[7] === body.voter).length >= MAX_SUBMISSIONS_PER_VOTER) {
    return json_({ ok: false, error: 'submission limit reached' });
  }
  // Next number after the highest existing id, so deleting a row never reuses an id (and its votes)
  const last = rows.reduce((m, r) => Math.max(m, parseInt(String(r[1]).slice(1), 10) || 0), 0);
  const id = 'S' + String(last + 1).padStart(3, '0');
  sh.appendRow([new Date(), id, '', clean_(body.title, 80), prompt, clean_(body.area, 40), clean_(body.expect, 200), body.voter]);
  return json_({ ok: true, id });
}
