# Persistent deployment

The production source branch for this experiment is
[yummydirtx/openpilot:automaxxing](https://github.com/yummydirtx/openpilot/tree/automaxxing).
Both the device's checked-out branch and `UpdaterTargetBranch` must be
`automaxxing`, and its updater's `origin` must point to that fork. The upstream
Zoompilot remote can be retained separately for deliberate future merges.

## Why the launcher disappeared

After a restart, the C4 was again on clean upstream `develop` at `1662aceda`.
The Android Auto UI modifications were absent, while the persistent enable
marker and `/data/automaxxing` runtime were still present. The updater target
was also `develop` on `zoompilot/zoompilot`.

The updater stages a clean checkout, and the launch script swaps a finalized
update into `/data/openpilot` at boot. Copying experimental files over upstream
does not put them into that branch's commit history. The resulting clean
checkout can therefore lose the launcher, even without a change in commit ID.

The fix is to use the branch containing the integration as the device's actual
source and update target. No overlay that repeatedly reapplies patches and no
updater disablement are needed. The local shallow development clone needs its
missing upstream history fetched before its commits can be pushed to the fork.

## Persistent versus temporary state

| Location | Purpose | Expected after reboot |
| --- | --- | --- |
| `/data/openpilot`, branch `automaxxing` | Committed native launcher, frontend adapter, project source | Retained by updates from this branch |
| `/data/automaxxing/native-ui-enabled` | Enables the local display adapter | Present |
| `/data/automaxxing/tools/android_auto` | Deployed projection runtime and hardware encoder library | Present |
| `/data/automaxxing/deps` and private identity directory | Runtime dependencies and TLS identity | Present; credentials remain outside Git |
| `/run/systemd/system/automaxxing-display.service` | Display supervisor unit | Recreated when the user selects Android Auto |
| `/run/automaxxing-display*` | Short-lived handoff state | Reset; old requests and display leases cannot survive boot |

The launcher is the first Sunnypilot settings category even before the
supervisor starts. Its availability comes from the persistent enable marker.
Boot leaves the comma display active. Android Auto starts only after an explicit
selection, using the existing fixed launcher and passwordless sudo configuration.

The deployed runtime is currently separate from the checked-out source. Changing
runtime code still requires updating `/data/automaxxing`; selecting a newer
source commit alone does not replace its Python dependencies, identity, or
compiled hardware encoder. These external files survived the reported reboot.

## Deployment checks

1. Confirm fresh offroad state and inactive projection before changing the
   device checkout. Save the previous commit, branch, remote configuration,
   and updater target outside the checkout.
2. Push the committed project branch to the verified user-owned fork. Do not
   force-push or change its default branch.
3. Fetch and check out `automaxxing` without discarding local edits or cleaning
   the device's ignored build artifacts. Submodule revisions and vehicle-control
   source remain those of the verified baseline.
4. Point `origin` and `UpdaterTargetBranch` at the project fork/branch. Invalidate
   a finalized update from the previous target before reboot; do not delete a
   staging tree while the updater owns it.
5. Reboot normally, reconnect, and verify the branch/commit/target, launcher
   source, enable marker, native UI health, and hardware encoder selection.
6. Exercise recreation of the supervisor after reboot. It should stay in local
   mode without starting projection until the user selects Android Auto.

For a source rollback, return the device to the recorded previous remote,
branch, and updater target, then reboot normally while offroad. The hash-guarded
UI bundle rollback is for experimental file-only installations; applying it
over a committed installation would intentionally dirty that branch.

## Verified deployment — 2026-09-19

The C4 was switched from clean `develop` at `1662aceda` to committed
`automaxxing` at `c1307ebac`. Its original remote was retained as `upstream`;
`origin` now points to the fork above, with `UpdaterTargetBranch=automaxxing`.
The previous configuration and installation record are saved outside the
checkout as `/data/automaxxing/persistence-before.json` and
`/data/automaxxing/persistence-install.json`.

A normal manager-requested reboot was completed and a new boot ID verified.
After boot, the tracked checkout was clean, the branch and update target were
retained, submodule revisions were unchanged, and every expected manager
process was running, including the native UI and updater.
The updater subsequently finalized `c1307ebac` with the integration present,
confirming its next staged boot also uses the project branch.

The native handoff adapter was enabled in local mode before the temporary
supervisor unit existed. The actual Android Auto settings card passed the
offscreen launcher probe: ordinary short tap, starting/connecting/connected
indicators, failed-session retry, and cancel. The launcher's fixed startup
command successfully recreated its supervisor after reboot.

A ten-second offroad full-native-UI probe then produced 285 frames at
28.04 fps using `qcom-v4l2`, with fresh display state and no software fallback.
This was a renderer/encoder check in the room, not another Mazda display test.
Diagnostic results remain under `/data/automaxxing/persistence-*`.
The final supervisor state was `local`, with USB projection inactive and all
expected manager processes healthy.
