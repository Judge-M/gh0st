# Mobile build and release checks

`interfaces/mobile/eas.json` defines the existing `preview` build commands as internal builds. Android preview produces an installable APK; iOS preview uses internal distribution. The `production` profile creates store-ready artifacts. EAS Build credentials and store submission remain under the account owner's control.

## Build locally or with EAS

From `interfaces/mobile`, install the EAS CLI (`npm install --global eas-cli`) and authenticate to the intended Expo account. Link the project to that account when EAS prompts. Then build previews:

```bash
eas build --platform android --profile preview
eas build --platform ios --profile preview
```

Before public distribution, create production artifacts and submit them only after the owner has reviewed the store listing and release metadata:

```bash
eas build --platform all --profile production
```

Do not commit Apple certificates, provisioning profiles, Android signing keys, EAS tokens, or service-account keys. EAS can manage signing credentials in the account; alternatively, configure credentials in the account's secret storage.

## Required device validation

Install each preview on a physical iOS and Android device and verify:

1. Pair by scanning the daemon's one-time QR code; the UI shows the expected device and secure storage reports success.
2. Force-close and reopen the mobile app; the saved pairing reconnects without rescanning.
3. Suspend and resume the app and change network connectivity; reconnect uses bounded backoff and recovers without sending plaintext payloads.
4. Dispatch a low-budget task and confirm status and quota updates arrive over the encrypted relay.
5. Create a grant request and verify approve/reject updates the local ledger only after an explicit tap.
6. Revoke the phone pairing from the daemon; the phone clears its credential and stops reconnecting.
7. Restart the broker and daemon; the daemon restores active pair registrations and the phone reconnects.
8. Confirm QR secrets, relay tokens, and API credentials do not appear in logs, screenshots, or crash output.

## Expo Push setup

Push alerts use Expo's push service and contain only a generic title/body. Pairing alone does not request notification permission. The app asks after the operator taps **Enable push alerts**. After opt-in, the Expo push token is stored in the phone's secure store and sent to the paired daemon inside the encrypted relay message; the daemon holds it in memory only. The phone re-registers the token on reconnect or daemon restart. Forgetting the pairing deletes the saved token. No prompts, task IDs, grant details, or approval controls are sent in push payloads.

Before push can be enabled, link this app to the owner's Expo project from `interfaces/mobile` with `eas init`, then set the project's UUID as `EXPO_PUBLIC_EAS_PROJECT_ID` for local and EAS builds. `app.config.js` injects it as `extra.eas.projectId`; without it, the phone shows push setup as unavailable instead of offering a button that cannot work. Configure Android FCM and iOS APNs credentials in Expo's project credentials. Keep credentials and EAS access tokens out of Git. Push delivery is best effort; use the app's encrypted relay status view as the source of truth.

The app source and automated pairing-crypto tests are in place, but these device checks and actual EAS builds still require the owner's Expo, Apple, and Google accounts/devices.
