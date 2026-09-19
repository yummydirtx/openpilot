# Phone identity and authentication experiment

Observed September 18, 2026: **stock Google DHU 2.0 accepts the phone identity
imported from Android Auto 17.6.663454-release**. The local Python sender completes
TLS 1.2, receives Android Auto authentication status 0, and exchanges encrypted
service-discovery messages. This resolves the receiver's rejection of our
self-signed certificate in the local experiment. Subsequent [video experiments](video.md)
also pass. **September 19 update:** the actual Mazda accepts this identity, and
strict reverse verification of the Mazda certificate also passes on the comma's
OpenSSL 3.0.13. See the [in-car experiment](in-car.md). The DHU-specific date-format
failure described below remains a separate local-emulator issue.

## What changed

The head unit checks the phone certificate's identity and issuer after TLS
negotiation. Giving a self-signed certificate the correct `CarService` subject
does not make it trusted. AACS's published identity expired in 2022.

Android Auto's full APK contains a current CarService certificate and an
obfuscated matching key. The historical recovery approach is described by its
implementers in [AACS issue 15](https://github.com/tomasz-grobelny/AACS/issues/15#issuecomment-1302991900).
We inspected the current package and reproduced that approach locally; the
certificate chains to its embedded Google Automotive Link CA and its RSA public
key matches the recovered private key.

No physical Android phone was used. The existing Pixel_9 API 36 emulator was
booted read-only without saving snapshots, inspected, then stopped. Its preloaded
Android Auto package was only version `1.2.551000-stub`, so it did not contain the
projection implementation. Its verified Google signing certificate supplied a
local baseline for authenticating the full downloaded APK. Static inspection on
the Mac then sufficed; installing or running the full app was unnecessary.

The final runtime still needs only the comma and car. APK inspection, jadx,
Android SDK tools, and the Android emulator are development tools.

## Artifact provenance

The full bundle came from the
[Android Auto package download](https://apkpure.net/android-auto/com.google.android.projection.gearhead/download)
on September 18, 2026. This is a third-party distribution source; trust was checked
cryptographically against the Google-signed SDK stub, not inferred from its label.
The download page is mutable. The importer accepts only the exact base APK below.

| Artifact | Observed value |
| --- | --- |
| Package/version | `com.google.android.projection.gearhead`, `17.6.663454-release`, code `176663454` |
| XAPK SHA-256 | `fa09bb4ad486dc2ad47d182ada467eef46f4db8b64ee49b8f90a0d384fd7cd3e` |
| Base APK SHA-256 | `d9cdcf6db33056a2c586182c0b93fbd75768260426134f231848186067603078` |
| Verified APK signer SHA-256 | `1ca8dcc0bed3cbd872d2cb791200c0292ca9975768a82d676b8b424fb65b5295` |
| Projection certificate SHA-256 (DER) | `2badd925f1b52b257db2d5b52c83ffd4d446f616327e8f8254fd660dc20c381d` |
| Projection subject | `O=CarService,L=Mountain View,ST=California,C=US` |
| Projection issuer | Google Automotive Link |
| Projection validity | July 4, 2014 through December 23, 2026, 22:48:29 UTC |
| Embedded root expiry | June 5, 2044 |

The APK signing identity and projection TLS identity serve different purposes.
The first authenticates the downloaded software; the second is presented to DHU
or the car. Neither establishes comma USB compatibility.

## Reproduce the import

Run from the repository root. The base APK is already in this Mac's ignored cache.
To recover it from the recorded XAPK, extract the member
`com.google.android.projection.gearhead.apk` as `android-auto-base.apk`. The repo
contains no APK, decompiled Google code, certificates, or private keys.

Development dependencies used: Python 3.14.7, `cryptography` 50.0.1, jadx 1.5.6,
and Android SDK build-tools 36.0.0 `apksigner`. Only the importer needs
`cryptography` and jadx; the session probe uses Python's standard library.

```sh
python3 -m tools.android_auto.import_identity \
  --apk .cache/automaxxing/android-auto-base.apk \
  --apksigner "$HOME/Library/Android/sdk/build-tools/36.0.0/apksigner"
```

The importer verifies the pinned APK hash and signer, decompiles just the two
identity classes into a temporary directory, recovers the key, verifies its match
and the certificate's issuer/validity, and creates
`.cache/automaxxing/imported-identity/`. It prints metadata only. The directory is
mode 0700 and PEM files mode 0600. It refuses to overwrite an existing output;
reuse the existing identity or supply a fresh `--output` directory.

For this inspected build, `jcf` is the provider and `jch` holds the root and byte
arrays. There is a consequential decompiler trap: jadx's enhanced-for rendering
of `tna.ba` caches an input byte, but the DEX instruction reads that byte inside
the inner loop. Since input and output alias during seven mixing rounds, those
forms are not equivalent. The importer preserves the bytecode's behavior;
successful padding, RSA key match, CA signature, and actual DHU acceptance all
validate the recovered result. Names, constants, and layout may change in later
builds. Inspect them before changing the pinned hash.

## Repeat the working receiver-authentication test

```sh
python3 -m tools.android_auto.auth_probe \
  --cert .cache/automaxxing/imported-identity/phone-cert.pem \
  --key .cache/automaxxing/imported-identity/phone-key.pem \
  --output .cache/automaxxing/auth-imported
```

Observed exit code: **0**. Result flags:

```json
{
  "tls_established": true,
  "authentication_complete": true,
  "head_unit_verified": false,
  "service_discovery_complete": true,
  "video_tested": false
}
```

Evidence: `.cache/automaxxing/auth-imported/events.jsonl` and `dhu.log`.
DHU reports `Verify returned: ok`. The event log records the successful auth
status and a 592-byte encrypted discovery response advertising seven channels;
channel 2 contains a video configuration. Unknown nested configuration bytes are
preserved as hex rather than assigned guessed meanings. This authentication-only
diagnostic stops before opening video; the separate [video sender](video.md)
continues through projection. Logs are overwritten on rerun; use a new output
directory to retain them.

The default `auth_probe` without credentials remains a negative control and
should exit 2 with the original self-signed rejection. TLS completion alone must
never be counted as receiver acceptance.

## Remaining direction of authentication

The working test proves **DHU authenticates our sender**. By default, this
loopback prototype does not request or validate the head unit's certificate;
the output explicitly reports `head_unit_verified: false`.

Adding the following option requests a client certificate and verifies it against
the imported CA, failing closed on validation errors:

```sh
--ca .cache/automaxxing/imported-identity/root-cert.pem
```

With this Mac's OpenSSL 3.6.4, both stock DHU 2.0 and 2.1 fail that check with
`format error in certificate's notBefore field`. No certificate date normalization,
trust-store change, system-clock change, or DHU patch was applied. This is a
distinct limitation from the original phone-certificate rejection. Investigate
the receiver certificate and TLS-library compatibility before treating this
prototype as a mutually authenticated runtime; test the Mazda certificate too.

Additional evidence: `.cache/automaxxing/auth-mutual/` and
`.cache/automaxxing/auth-mutual-2.1/`. The beta DHU tested was
`2.1-mac-arm64`, build `2022-12-15-495527557`, from Google's catalog:
[desktop-head-unit-darwin-aarch64_r02.1.zip](https://dl.google.com/android/repository/desktop-head-unit-darwin-aarch64_r02.1.zip),
7,572,204 bytes, verified catalog SHA-1
`edd9d9389ac511d5f28e47cdfbf54bbcd7294d7f`. The default probe still uses stable 2.0.

## Next decisions

1. The accepted identity now supports local channel/focus negotiation and moving
   H.264, demonstrated in the [video runbook](video.md). Continue with live rendering.
2. Tomorrow, establish actual comma USB transport and repeat authentication and
   discovery against the Mazda. Do not mark G2 passed from this loopback test.
3. Resolve sender-side head-unit verification before relying on the runtime beyond
   the isolated experiment.
4. Keep certificate provisioning separate from code and record expiry. Refresh
   before December 23, 2026; this identity is a working hackathon provision, not a
   permanent certificate strategy.
