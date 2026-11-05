/**
 * LinkerStatus.gs  (new file next to Code.gs)
 *
 * Shows the internal linker's result for every drafted article in an "Internal Linking" column,
 * so the team can see which articles have been linked, are waiting, or need a look.
 *
 * Reads:  the linker's control sheet (Processed + Link Checks tabs). The linker itself never
 *         writes to the MasterSheet; this script copies its results across.
 * Writes: ONLY the "Internal Linking" column (and its header cell once, if that cell is empty).
 *         Nothing else in the MasterSheet is touched.
 *
 * Setup (once):
 *   1. Project Settings -> Script properties -> add
 *        LINKER_CONTROL_SHEET_ID = 1n_pYi_Y3jeyMSzr6wlB1Yla4OY54NeImKPNs8rLO4X8
 *   2. Run syncLinkerStatusNow once from the editor (approve access to the control sheet).
 *   3. Varsity -> Admin -> Turn on internal-link status (every 15 min).
 */

const LINKER_STATUS = {
  HEADER: 'Internal Linking',
  EVERY_MINUTES: 15,          // 1, 5, 10, 15 or 30
  PROCESSED_TAB: 'Processed',
  CHECKS_TAB: 'Link Checks'
};

/* ---------- Scheduling and menu entry points ---------- */

function turnOnLinkerStatusSync() {
  turnOffLinkerStatusSync(true);
  ScriptApp.newTrigger('syncLinkerStatusAuto').timeBased().everyMinutes(LINKER_STATUS.EVERY_MINUTES).create();
  SpreadsheetApp.getActive().toast('On. The Internal Linking column refreshes every ' + LINKER_STATUS.EVERY_MINUTES + ' minutes.', 'Varsity', 8);
}

function turnOffLinkerStatusSync(quiet) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncLinkerStatusAuto') ScriptApp.deleteTrigger(t);
  });
  if (quiet !== true) SpreadsheetApp.getActive().toast('Internal-link status refresh is off.', 'Varsity', 6);
}

function syncLinkerStatusAuto() { runLinkerStatus_(false); }
function syncLinkerStatusNow() { runLinkerStatus_(true); }

/* ---------- The sync ---------- */

function runLinkerStatus_(manual) {
  const ss = SpreadsheetApp.getActive();
  const id = (PropertiesService.getScriptProperties().getProperty('LINKER_CONTROL_SHEET_ID') || '').trim();
  if (!id) {
    if (manual) SpreadsheetApp.getUi().alert('Add the script property LINKER_CONTROL_SHEET_ID first (Project Settings -> Script properties).');
    return;
  }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(manual ? 10000 : 60000)) {
    if (manual) ss.toast('Another run is in progress. Try again in a minute.', 'Varsity', 6);
    return;
  }
  let updated = 0;
  try {
    const results = readLinkerResults_(id);
    ss.getSheets().forEach(function (sheet) { updated += writeStatusColumn_(sheet, results); });
  } catch (e) {
    if (manual) SpreadsheetApp.getUi().alert('Could not refresh the internal-link status: ' + e.message);
    else console.warn('Linker status: ' + e.message);
    return;
  } finally {
    lock.releaseLock();
  }
  if (manual) ss.toast(updated ? updated + ' status cell(s) updated.' : 'Internal-link status is up to date.', 'Varsity', 6);
}

// { postId: { status, links, dry, mode, runAt, notes, kept, added } } from the control sheet.
function readLinkerResults_(controlId) {
  const book = SpreadsheetApp.openById(controlId);
  const out = {};
  const rows = function (tab) {
    const sh = book.getSheetByName(tab);
    if (!sh || sh.getLastRow() < 2) return [];
    const vals = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getValues();
    const head = vals[0].map(function (h) { return String(h).trim().toLowerCase(); });
    return vals.slice(1).map(function (r) {
      const o = {};
      head.forEach(function (h, i) { o[h] = r[i]; });
      return o;
    });
  };
  const isTrue = function (v) { return v === true || String(v).toUpperCase() === 'TRUE'; };

  rows(LINKER_STATUS.PROCESSED_TAB).forEach(function (r) {
    const pid = String(r.post_id || '').trim();
    if (!/^\d+$/.test(pid)) return;
    const cur = { status: String(r.status || ''), links: +r.links_added || 0, dry: isTrue(r.dry_run),
                  mode: String(r.mode || 'draft'), runAt: String(r.run_at || ''), notes: String(r.notes || '') };
    const prev = out[pid];
    // A live result always beats a dry run; otherwise the newest row wins.
    if (!prev || (prev.dry && !cur.dry) || (prev.dry === cur.dry && cur.runAt >= prev.runAt)) out[pid] = cur;
  });
  rows(LINKER_STATUS.CHECKS_TAB).forEach(function (r) {
    const pid = String(r.post_id || '').trim();
    if (out[pid]) { out[pid].kept = +r.links_still_present || 0; out[pid].added = +r.links_added || 0; }
  });
  return out;
}

function statusText_(res, published) {
  if (!res) return published ? 'Published before linking (backfill can add links)' : 'Waiting for the linker';
  const day = /^(\d{4})-(\d{2})-(\d{2})/.exec(res.runAt);
  const when = day ? ' · ' + (+day[3]) + ' ' + ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][+day[2] - 1] : '';
  const how = res.mode === 'backfill' ? ' (after publishing)' : '';
  switch (res.status) {
    case 'linked':
      if (res.dry) return 'Dry run: ' + res.links + ' link(s) proposed, not applied' + when;
      return 'Linked: ' + res.links + ' link(s)' + how + when +
             (res.added ? ' · ' + res.kept + '/' + res.added + ' kept after publishing' : '');
    case 'no_links':
      return (res.dry ? 'Dry run: no suitable links' : 'Checked: no suitable links') + when;
    case 'skipped':
      return /publish/i.test(res.notes) ? 'Published before linking (backfill can add links)' : 'Skipped' + when;
    case 'retry_later':
      return 'Edited while linking, will retry' + when;
    case 'error':
      return 'Error, will retry automatically' + when;
    default:
      return res.status + when;
  }
}

// Fills the Internal Linking column on one tab. Returns the number of cells changed.
function writeStatusColumn_(sheet, results) {
  const lastCol = sheet.getLastColumn(), lastRow = sheet.getLastRow();
  if (lastCol < 1 || lastRow < 2) return 0;
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(function (h) { return String(h).trim().toLowerCase(); });
  const col = function (name) { const i = headers.indexOf(name.toLowerCase()); return i === -1 ? 0 : i + 1; };
  const dCol = col('Draft Link');
  if (!dCol) return 0;                                    // not a publishing tab

  let sCol = col(LINKER_STATUS.HEADER);
  if (!sCol) {                                            // add the header once, after the last header
    let last = 0;
    headers.forEach(function (h, i) { if (h !== '') last = i + 1; });
    const cell = sheet.getRange(1, last + 1);
    if (cell.getValue() !== '') return 0;
    styleHeaderLike_(sheet, dCol, last + 1);
    cell.setValue(LINKER_STATUS.HEADER);
    if (sheet.getColumnWidth(last + 1) < 200) sheet.setColumnWidth(last + 1, 260);
    sCol = last + 1;
  }

  const n = lastRow - 1;
  const drafts = sheet.getRange(2, dCol, n, 1).getValues();
  const lCol = col('Live Link'), pCol = col('Publishing Status');
  const lives = lCol ? sheet.getRange(2, lCol, n, 1).getValues() : null;
  const pubs = pCol ? sheet.getRange(2, pCol, n, 1).getValues() : null;
  const range = sheet.getRange(2, sCol, n, 1);
  const current = range.getValues();
  let changed = 0;
  const next = current.map(function (row, i) {
    const m = /[?&](?:post|p)=(\d+)/.exec(String(drafts[i][0]));
    if (!m) return row;                                   // no draft yet: leave the cell as it is
    const published = (lives && String(lives[i][0]).trim() !== '') ||
                      (pubs && String(pubs[i][0]).trim().toLowerCase() === 'published');
    const text = statusText_(results[m[1]], published);
    if (String(row[0]) !== text) { changed++; return [text]; }
    return row;
  });
  if (changed) range.setValues(next);
  return changed;
}
