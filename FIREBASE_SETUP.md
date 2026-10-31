# Make accounts and history permanent (Firebase) — 5 minutes, one time

Why: Render's free plan erases the bot's local database on every deploy/restart, so accounts and
trade history vanished. With this set up, every account, setting, trade and action is also saved in
your Firebase project (swiftchain-827f2) and restored automatically whenever the server restarts.
Your **web config (apiKey etc.) is not enough** for a server to write safely — the server needs a
private key that only you can create. That is the only secret you have to handle.

## 1. Rules (important)
The bot keeps everything under ONE node, `synthbot`, and never touches other nodes in the project.
Its server key bypasses rules, so **your existing rules keep working unchanged** and nothing of your
other apps breaks. BUT: a rule granted at the top (e.g. `".read": "auth != null"`) cascades down and
cannot be taken back lower, so any signed-in user of your other apps could also read `synthbot`
(password hashes, trade history). Best: replace the top-level grants with per-node rules for your
other apps and leave `synthbot` unlisted (denied). If you can list your other top-level nodes, ask for
the rewritten rules. If this project has only the bot, use:
Firebase console → **Build → Realtime Database → Rules**. Replace everything with:

    {
      "rules": {
        ".read": false,
        ".write": false
      }
    }

Click **Publish**. (The bot's server key bypasses these rules; nobody else, including anyone who has
your public web config, can read the password hashes or trade history.)
If your rules currently say `".read": true` / `".write": true` (test mode), anyone on the internet
can read and change your data. Fix this first.

## 2. Create the server key
Firebase console → ⚙ **Project settings → Service accounts → Generate new private key**.
A `.json` file downloads. Keep it private: do NOT put it in the project folder or git.

## 3. Give the key to Render
Convert the file to one line of text (PowerShell, change the path):

    [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\dell\Downloads\swiftchain-827f2-firebase-adminsdk.json"))

Copy the long output. In Render → your service → **Environment → Add Environment Variable**:

    FIREBASE_SERVICE_ACCOUNT  =  <the long text>

Save. Render redeploys.
(Pasting the raw JSON contents also works. Optional: SESSION_DAYS, default 3650.)

## 4. Check it works
* Render → Logs should say: `Firebase restore done: ... rows restored from the cloud ...`
* Open `https://synthetic-bot-ts1x.onrender.com/api/cloud` while logged in:
  `"enabled": true, "restored": true`.
* Firebase console → Realtime Database → Data shows `users`, `settings`, `trades`, `activity`.
* Real test: Render → Manual Deploy (wipes the local file) → log in again with the same
  username/password; your history is still there.

## Notes
* Accounts created BEFORE this was switched on and already wiped by a redeploy cannot be recovered.
  Register once more; from then on it is permanent.
* If Firebase is unreachable at start-up the bot still runs, and sign-ups are paused until the
  cloud copy is restored (so a new account can never overwrite an old one).
* Market candle data is not copied (it is rebuilt from Deriv); accounts, settings, every trade and
  every action are.
