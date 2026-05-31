/**
 * Instagram自動投稿ワークフローを定期的に起動するトリガー（Google Apps Script）
 *
 * 目的：
 *   GitHub内蔵のスケジュール(cron)は遅延・間引きが多く、予定時刻どおりに動かない。
 *   そこで、信頼性の高い Apps Script の時間トリガーから GitHub のワークフローを
 *   起動し、post_reel.py に「予定時刻を過ぎた投稿」を処理させる。
 *
 * セットアップ手順：
 *   1. 投稿管理スプレッドシートを開く → 拡張機能 → Apps Script
 *   2. このコードを貼り付けて保存
 *   3. プロジェクトの設定（歯車）→ スクリプト プロパティ →
 *        プロパティ名: GITHUB_TOKEN  値: 発行したFine-grained PAT
 *   4. setupTrigger() を一度だけ実行（5分ごとの時間トリガーを作成）
 *      ※初回実行時に承認ダイアログが出るので許可する
 *
 * 動作確認：
 *   triggerInstagramWorkflow() を手動実行 → 実行ログに「起動OK (204)」が出ればOK。
 */

const GITHUB_OWNER = 'f-macb-88';
const GITHUB_REPO = 'instagram-auto-post';
const WORKFLOW_FILE = 'post.yml';
const BRANCH = 'main';

/** GitHub のワークフローを1回起動する。5分ごとのトリガーから呼ばれる。 */
function triggerInstagramWorkflow() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    throw new Error('スクリプトプロパティ GITHUB_TOKEN が未設定です。');
  }
  const url = 'https://api.github.com/repos/' + GITHUB_OWNER + '/' + GITHUB_REPO +
              '/actions/workflows/' + WORKFLOW_FILE + '/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: BRANCH }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code === 204) {
    Logger.log('起動OK (204)');
  } else {
    Logger.log('起動失敗: ' + code + ' / ' + res.getContentText());
  }
}

/** 5分ごとの時間トリガーを作成する（重複作成を防ぐため既存の同名トリガーは削除）。一度だけ実行。 */
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'triggerInstagramWorkflow') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('triggerInstagramWorkflow')
    .timeBased()
    .everyMinutes(5)
    .create();
  Logger.log('5分ごとのトリガーを作成しました。');
}
