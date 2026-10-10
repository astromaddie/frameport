# Civilization VII directory-backed OBB repair

This manual developer utility builds a title-specific APK from an APK already imported or built by FramePort. It adds
the reviewed `frameport.civ` helper DEX and changes the single native MAIN activity in `AndroidManifest.xml`. The
helper serves only validated, pre-extracted OBB directories. It falls back to Android's original OBB service whenever
the source image, key, location, or ready marker does not match.

In the current device run, the lowercase `base.obb` alias allowed all eleven requested OBB mounts, including the
uppercase `Base.obb` image, and the earlier `PLY_Projector` missing-lookup crash did not recur. The run continued
with balanced VR frame submission beyond 90 seconds. The off-head compositor capture was dark, so headset visuals
and gameplay remain unverified.

The packager requires explicit JDK and Android SDK paths. Use JDK 17, Android API 31 (API 26 or newer is accepted),
and the matching Android build-tools `d8`. It does not download or bundle tools. From a FramePort source checkout:

```sh
PYTHONPATH=src python scripts/civilization_directory_obb/package_directory_loader.py \
  --input /path/to/frameport-import-or-build.apk \
  --output /path/to/civilization-directory-loader.apk \
  --javac /path/to/jdk-17/bin/javac \
  --android-jar /path/to/android-sdk/platforms/android-31/android.jar \
  --d8 /path/to/android-sdk/build-tools/35.0.0/d8
```

By default, signing uses the existing FramePort per-package key and configured FramePort signer. Set `FRAMEPORT_HOME` or
pass `--frameport-home` if the key is stored in a non-default FramePort data directory. The script refuses to create a
new key and verifies that the output certificate SHA-256 matches the input APK. For a matching existing key outside
FramePort, pass all five options `--apksigner`, `--keystore`, `--ks-pass`, `--ks-key-alias`, and `--key-pass`. The key
must match the input APK certificate. The output is published only after signing, certificate comparison, and ZIP
payload checks succeed; the input APK is opened read-only.

Before packaging, the utility checks that the APK package is `com.mrf.firaxis.invernessdemo` and that its sole MAIN
activity is exactly `android.app.NativeActivity`. It refuses to replace an app-defined activity, since doing so could
discard activity behavior. It also requires one existing boolean `android:hasCode` application attribute and sets it
to `true`; if that attribute is absent or not safely editable, packaging fails. The OBB footer owner remains the
distinct string `com.firaxis.invernessdemo`. The utility rejects an existing `frameport.civ` class, then compiles the
four Java runtime classes against compile-only Android stubs and the SDK jar. Those stubs never enter the new DEX.
After signing, every original non-signature entry must have the same bytes, except for the manifest edit; the only new
application payload is one `classesN.dex` entry. Existing signature metadata is replaced by the signer.

Extract each OBB with an explicit footer owner and its own root directory:

```sh
python scripts/civilization_directory_obb/extract_obb.py \
  --root /path/to/Android/obb/com.mrf.firaxis.invernessdemo \
  --owner com.firaxis.invernessdemo \
  --alias base.obb \
  /path/to/Android/obb/com.mrf.firaxis.invernessdemo/Base.obb
```

The current native code probes the literal lowercase `base.obb` and skips the Base BLP mount if that name is absent,
even when the original image is named `Base.obb`. Use `--alias base.obb` only for that case. After validating the
unencrypted OBB footer, owner, source stability, and a complete ready extraction, the utility atomically creates the
relative symlink `base.obb -> Base.obb` in the selected root. It never renames or copies the source. A matching safe
alias is reused; a regular file, directory, dangling link, unsafe link, or link to another OBB causes an error without
replacing it. The option is optional and normal extraction does not create aliases.

The extractor accepts unencrypted AOSP v1 OBB footers with a matching FAT payload only. It reads the OBB without
modifying it, rejects unsafe paths and mismatched owners, and atomically publishes app-readable files/directories plus
a source-size/mtime ready marker. A matching complete extraction is reused; stale or unsafe existing output is
refused. It walks the parsed FAT directory entries and copies their cluster chains directly, avoiding repeated
high-level pathname scans in large asset directories. Install the host-only Python dependencies in the active
FramePort environment with these pins:

```text
pyfatfs==1.1.0
fs==2.4.16
appdirs==1.4.4
six==1.17.0
setuptools==80.9.0
```

No APK, OBB, extracted game data, certificate, or signing key belongs in the repository. Device installation and
player acceptance are separate from this source/package utility.
