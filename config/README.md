# Local configuration

No actual user profile catalog or credentials were exported. The application
requires all seven existing stable IDs for live checks. Inspect the intended
Chrome profiles on the target machine and replace every placeholder directory
in the example before installing it as local configuration.

```bash
cp config/logged-in-profiles.example.json config/logged-in-profiles.json
mkdir -p 'config/routine task profiles'
cp 'config/routine task profiles/logged-in-profiles.example.json' 'config/routine task profiles/logged-in-profiles.json'
```

Edit both resulting files so the same ID has the same `chrome_directory` in
both. Use Chrome's actual directory name from `chrome://version`, such as
`Profile 5`; do not put the display label or a guessed directory there. Each ID
must have a different directory. Preserve exactly these seven IDs:

```text
ortemksl_personal
ortemksl_finance
sokr4r3
kysil_work
loggedin_1
loggedin_2
loggedin_3
```

Sign into ChatGPT, Gemini and browser Copilot in each intended Chrome profile
yourself. Do not copy someone else's Chrome profile directory or session.
The names are inherited stable selectors; bind them to the user's intended
profiles only with their authorization. They are not account credentials.

The launcher defaults to the normal macOS Chrome executable and user-data
directory. A locally created `config/.env` can specify `BROWSER_CMD` and
`CHROME_USER_DATA_DIR` if required. `CDP_URL` defaults to
`http://127.0.0.1:9222`; if changed, export it before starting `run.sh`. Keep the
debugging endpoint on loopback.

Optional supported Copilot saved-account sign-in reads
`config/copilot-sign-in.json` with this shape:

```json
{
  "schema_version": 1,
  "account_emails": {
    "loggedin_1": "replace-with-the-users-account@example.invalid"
  }
}
```

Supply only real user-approved account identifiers for configured profiles.
Passwords, MFA codes and tokens do not belong in this file. Missing bindings
leave sign-in for operator attention. Do not add them to Git.

`launch_logged_in.sh` and `profile_anchor.html` are program files. Run live
checks through the panel's reviewed Start, not by launching this script manually.
New Start/Refetch closes and reopens Chrome under the existing shared guard;
the selected profiles must already contain their intended login sessions.
