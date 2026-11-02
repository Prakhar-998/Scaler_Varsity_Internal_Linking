/**
 * Varsity internal linker: "run now" trigger for the MasterSheet's Apps Script.
 *
 * Paste this file into the MasterSheet's Apps Script project (Extensions -> Apps Script -> + -> Script).
 * It only sends a signal to GitHub; it reads and writes nothing in the sheet.
 *
 * Setup (once):
 *   1. GitHub -> Settings -> Developer settings -> Fine-grained tokens -> Generate new token
 *      Repository access: only Scaler_Varsity_Internal_Linking
 *      Repository permissions: Contents = Read and write   (needed to send a repository_dispatch)
 *   2. Apps Script -> Project Settings -> Script properties:
 *      GITHUB_TOKEN = <that token>
 *      GITHUB_REPO  = Prakhar-998/Scaler_Varsity_Internal_Linking
 *   3. Run testInternalLinkerTrigger() once (authorise the URL fetch), then check GitHub ->
 *      Actions -> Internal linker for a run started by "repository_dispatch".
 *
 * Use: right after your script writes Draft Links, call triggerInternalLinker([id1, id2, ...])
 * ONCE with all the new post IDs (one call per batch, not one per row).
 *
 * It never throws: if GitHub can't be reached, the 30-minute schedule still picks the drafts up.
 */
function triggerInternalLinker(postIds) {
  var ids = [].concat(postIds).map(String).filter(function (x) { return /^\d+$/.test(x); });
  if (!ids.length) return false;
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('GITHUB_TOKEN');
  var repo = props.getProperty('GITHUB_REPO');
  if (!token || !repo) {
    console.warn('Internal linker trigger: GITHUB_TOKEN / GITHUB_REPO script properties not set');
    return false;
  }
  try {
    var res = UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify({ event_type: 'draft-created', client_payload: { post_ids: ids.join(',') } }),
      muteHttpExceptions: true
    });
    var ok = res.getResponseCode() === 204;
    if (!ok) console.warn('Internal linker trigger failed: ' + res.getResponseCode() + ' ' + res.getContentText());
    return ok;
  } catch (e) {
    console.warn('Internal linker trigger error: ' + e);
    return false;
  }
}

/** Sends a test signal for the test draft. The run only links drafts that are Ready to Publish. */
function testInternalLinkerTrigger() {
  Logger.log(triggerInternalLinker([1573]) ? 'Sent: check GitHub Actions' : 'Not sent: see the log');
}

/** Helper: pull the post ID out of a Draft Link like .../post.php?post=1940&action=edit */
function postIdFromDraftLink(link) {
  var m = String(link || '').match(/[?&]post=(\d+)/);
  return m ? m[1] : null;
}
