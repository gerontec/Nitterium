# Nitterium — Build für nitter.heissa.de

Fork von [kaleedtc/Nitterium](https://github.com/kaleedtc/Nitterium) (Kotlin/Jetpack Compose,
WebView-Wrapper um eine Nitter-Instanz). Ersetzt die eingestellte F-Droid-App
`com.plexer0.nitter`, deren Quellcode nicht mehr verfügbar ist.

## Änderungen gegenüber Upstream

| Datei | Änderung |
|---|---|
| `app/src/main/res/values/strings.xml` | `nitter_heissa_de_url` = `https://nitter.heissa.de` |
| `ui/feature/settings/SettingsViewModel.kt` | eigene Instanz steht als erste in der Auswahl, Fallback beim Entfernen |
| `data/repository/UserPreferencesRepository.kt` | Default-Instanz + Default-Tab `Feed` |
| `ui/feature/settings/SettingsContract.kt`, `MainViewModel.kt`, `ui/NitteriumApp.kt` | Default-Tab `Feed` |
| `data/repository/SubscriptionRepository.kt` | Erststart legt die Abos `SZwanglos` und `carol_herzog` an |
| `app/build.gradle.kts` | `lint { checkReleaseBuilds = false }` — AGP-Lint stürzt beim Analysieren ab (UAST-Bug) |

`nitter.heissa.de` ist dual-stack: AAAA `2607:f1c0:f081:e500::2`, A `74.208.77.214`.
Android nimmt per Happy Eyeballs IPv6 und fällt automatisch auf IPv4 zurück.

## Bauen

```bash
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
JAVA_HOME=/pfad/zu/jdk21 ./gradlew assembleRelease
zipalign -f -p 4 app/build/outputs/apk/release/app-release-unsigned.apk nitterium-heissa.apk
apksigner sign --ks <keystore> nitterium-heissa.apk
```

Voraussetzungen: JDK 21 (kein JRE — `javac` wird gebraucht), Android SDK,
compileSdk 37 lädt AGP selbst nach.

## Binary

`bin/nitterium-heissa.apk` — Release, mit eigenem Schlüssel signiert
(SHA-256 `09c4e95e94d1a05f8c37710f7d1e0cad3f997610feb58f3826bd88731f38e3a4`,
CN=gerontec, O=heissa.de). Der Keystore liegt **nicht** im Repo:
`~/.android/nitterium-release.jks`. Ohne ihn lässt sich kein Update über die
installierte App drüber installieren.
