# Nitterium für nitter.heissa.de — App und Serverseite

Eigener Build von [kaleedtc/Nitterium](https://github.com/kaleedtc/Nitterium)
(Kotlin/Jetpack Compose, WebView-Wrapper um eine Nitter-Instanz) plus die
Serverkonfiguration, die die Instanz auf heissa.de am Leben hält, seit X das
kostenlose Polling gestrichen hat.

Ersetzt die eingestellte F-Droid-App `com.plexer0.nitter`, deren Quellcode nicht
mehr verfügbar ist.

---

## 1. App

### Änderungen gegenüber Upstream

| Datei | Änderung |
|---|---|
| `app/src/main/res/values/strings.xml` | `nitter_heissa_de_url` = `https://nitter.heissa.de` |
| `ui/feature/settings/SettingsViewModel.kt` | eigene Instanz steht als erste in der Auswahl, Fallback beim Entfernen |
| `data/repository/UserPreferencesRepository.kt` | Default-Instanz + Default-Tab `Feed` |
| `ui/feature/settings/SettingsContract.kt`, `MainViewModel.kt`, `ui/NitteriumApp.kt` | Default-Tab `Feed` |
| `data/repository/SubscriptionRepository.kt` | Erststart legt die Abos `SZwanglos` und `carol_herzog` an |
| `data/repository/UserPreferencesRepository.kt`, `ui/common/NitterWebView.kt`, `ui/feature/settings/*`, `MainActivity.kt` | **Zugangsschlüssel je Instanz** — siehe unten |
| `app/build.gradle.kts` | `lint { checkReleaseBuilds = false }` — AGP-Lint stürzt beim Analysieren ab (UAST/`AsyncExecutionService`-Bug) und blockiert sonst den Release-Build |

Die App lädt ihren Feed als `https://nitter.heissa.de/<user1>,<user2>` (Nitters
Mehrfach-Timeline), Profile als `/<user>`. Pull-to-Refresh ist ein
`webView.reload()` — es holt live von der Instanz, es gibt **keinen** Weg, aus
der App den DB-Poller auf dem Server auszulösen.

### Bauen

```bash
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
JAVA_HOME=/pfad/zu/jdk21 ./gradlew assembleRelease
zipalign -f -p 4 app/build/outputs/apk/release/app-release-unsigned.apk nitterium-heissa.apk
apksigner sign --ks <keystore> nitterium-heissa.apk
```

Voraussetzungen: **echtes JDK 21** — ein JRE reicht nicht (`javac` fehlt, Gradle
bricht mit „Toolchain … does not provide the required capabilities:
[JAVA_COMPILER]" ab). Android SDK; `compileSdk 37` lädt AGP selbst nach,
`cmdline-tools` sind dafür nicht nötig, aber `~/Android/Sdk/licenses` muss
existieren.


### Zugangsschlüssel je Instanz

Die Tweet-Detailseiten der eigenen Instanz sind für Fremde gesperrt (siehe
2.3). Eine Adressliste im vhost trägt aber nur, solange das Telefon im WLAN
hängt: im Mobilfunk und nach jedem Präfixwechsel der FritzBox fällt die App
heraus und bekommt auf dem eigenen Host ein 403.

Deshalb weist sich die App aus. In den Einstellungen steht unter der
Instanz-URL ein Feld **Instance access key (optional)**; der Wert wird je
**Host** gespeichert (`instance_keys`, JSON `{host: schlüssel}`) und hängt als
` Nitterium/<schlüssel>` am User-Agent — nur an genau diese Instanz, nie an
eine andere. Ohne Schlüssel verhält sich die App exakt wie vorher.

Serverseite, eine Zeile im `LocationMatch`:

```apache
Require expr "%{HTTP_USER_AGENT} =~ m#Nitterium/<schlüssel>#"
```

**Eintragen ohne Tippen.** 32 Zeichen auf dem Telefon abzutippen geht schief —
gemessen: aus `b208675f926a…` wurde `b8675f92a64c…`. Die App nimmt den
Schlüssel deshalb auch über einen Verweis entgegen:

```
nitterium://instance?url=https%3A%2F%2Fnitter.heissa.de&key=<schlüssel>
```

Als QR-Code gedruckt reicht die normale Kamera-App — **keine
Kamerabibliothek und keine Kameraberechtigung in Nitterium**. Die App
bestätigt mit „Access key saved for <host>". Zum Erzeugen genügt Pythons
`qrcode`; per adb geht es auch direkt:

```bash
adb shell "am start -a android.intent.action.VIEW -d 'nitterium://instance?url=…&key=…'"
```

Die Anführungszeichen gehören **um den ganzen Befehl**: `adb shell` reicht die
Argumente an die Shell des Telefons weiter und verliert dabei die eigenen
Quotes — ein nacktes `&` wird dort zum Hintergrund-Operator, `key=…` fällt weg,
und ein leerer Schlüssel löscht den Eintrag.

### Binary

`bin/nitterium-heissa.apk` — Release, mit eigenem Schlüssel signiert
(SHA-256 `09c4e95e94d1a05f8c37710f7d1e0cad3f997610feb58f3826bd88731f38e3a4`,
`CN=gerontec, O=heissa.de`). Der Keystore liegt **nicht** im Repo
(`~/.android/nitterium-release.jks`); ohne ihn lässt sich kein Update über die
installierte App drüberinstallieren.

### Netz

`nitter.heissa.de` ist dual-stack (AAAA + A). Android nimmt per Happy Eyeballs
IPv6 und fällt selbsttätig auf IPv4 zurück — in der App ist dafür nichts
hardcodiert.

---

## 2. Serverseite (heissa.de)

Alles unter `server/` ist die bereinigte Fassung der laufenden Konfiguration
(Zugangsdaten und private Adressen durch Platzhalter ersetzt).

### 2.1 Poll-Budget: 30 Requests pro Tag

Die Instanz fährt mit **einem** Session-Token (`~/nitter/sessions.jsonl`,
Refresh alle 8 h). Seit dem Wegfall des kostenlosen Zugangs läuft
`nitter_poll.py` nur noch mit versetzten Zeiten und kleinen Batches:

| Zeit | Job | Requests/Lauf | Läufe/Tag | Summe |
|---|---|---|---|---|
| :10 alle 6 h | `--following SZwanglos --batch 3` | 3 | 4 | 12 |
| :25 alle 6 h | `--user carol_herzog` | 1 | 4 | 4 |
| :40 alle 6 h | `--user ZentraleV` | 1 | 4 | 4 |
| :55 alle 6 h | `--user SHomburg` | 1 | 4 | 4 |
| :10 um 6 und 18 Uhr | `--following Impf_Info --batch 3` | 3 | 2 | 6 |

Zwischen zwei Läufen liegen mindestens 15 Minuten, innerhalb eines Laufs
pausiert der Poller 1,5–4,5 s je Account. Frequenz nie ohne Not erhöhen — die
Sperre träfe den Account hinter dem Token.

Historie holt man **nicht** über die API, sondern per Playwright über x.com mit
der eingeloggten Browser-Session (`zentralev_backfill.py`, `carol_backfill.py`;
14-Tage-Zeitschnitt, bricht erst nach 3 alten Posts ab — angepinnte alte Tweets
lösen sonst einen Fehlabbruch aus).

### 2.2 Healthcheck: nicht auf eine Tweet-Seite zeigen lassen

Der Docker-Healthcheck rief alle 30 s `/Jack/status/20` ab. Tweet-Detailseiten
brauchen `ConversationTimeline`, und genau dieser Endpoint ist für eine
Gratis-Session dauerhaft leer: **2.880 vergebliche API-Versuche pro Tag**,
~400 Log-Zeilen pro Stunde, rund um die Uhr. Die Seite selbst kam aus dem
Redis-Cache und lieferte 200, deshalb galt der Container als „healthy".

Ziel ist jetzt eine gecachte RSS-Seite:

```yaml
healthcheck:
  test: wget -nv --tries=1 --spider http://127.0.0.1:8080/SZwanglos/rss || exit 1
```

### 2.3 Fremdzugriffe: nur das sperren, was einen Poll auslöst

Gemessen: 77 Abrufe von Tweet-Detailseiten in 5,5 Minuten von **77
verschiedenen IPs**, jede IP genau einmal, jede Tweet-ID genau einmal — ein
Proxy-Pool, der eine alphabetische Accountliste durchgeht. Jeder dieser
Requests ist zwangsläufig ein Cache-Miss und damit ein Poll auf unsere Session.
Per-IP-Regeln greifen dagegen prinzipiell nicht.

Deshalb sperrt der vhost genau einen Pfad für Fremde und lässt alles andere
offen (`server/nitter-le-ssl.conf.example`):

```apache
<LocationMatch "^/[A-Za-z0-9_]+/status/[0-9]+">
    Require ip 127.0.0.1 ::1 <eigene Adressen, WireGuard, LANs>
</LocationMatch>
```

* gesperrt: nur `/<user>/status/<id>` — der einzige Pfad, der pro Abruf zu X geht
* offen für alle: Profile, Timelines, RSS, Suche, Bilder, Statik — alles, was
  aus dem Redis-Cache kommt und X nicht anfasst
* Wirkung: 19 Scraper-Requests/Minute laufen auf 403, im Nitter-Log **0**
  API-Versuche (vorher ~400/h)

**Falle:** `Require ip` braucht `mod_authz_host`. War es nicht geladen, scheitert
der Reload mit „Unknown Authz provider: ip" und Apache bleibt unten — also
`a2enmod authz_host` vorher, `apachectl configtest` danach.

Als Auffangnetz für einzelne Dauerläufer läuft zusätzlich die fail2ban-Jail
`nitter-scrape` (`server/fail2ban-*`): 30 Tweet-Seiten in 10 Minuten pro IP,
dann 1 h Sperre, eskalierend bis 1 Tag; eigene Netze in `ignoreip`. Sie braucht
den Zugriffs-Log `/var/log/apache2/nitter_access.log`, den beide Nitter-vhosts
schreiben.

### 2.4 Archiv-Gateway: live und Archiv ohne Umschalten

`server/gateway.php` liegt auf dem Server unter
`/var/www/nitter_archive/gateway.php` und ist den Profil- und Feed-Pfaden
vorgeschaltet:

```apache
ProxyPass /_archive !
ProxyPassMatch "^/(?!about$|settings$|search$|explore$|css$|js$|pic$|fonts$)[A-Za-z0-9_][A-Za-z0-9_,]{0,120}/?$" !
RewriteRule "^/(?!…)([A-Za-z0-9_][A-Za-z0-9_,]{0,120})/?$" /_archive/gateway.php?u=$1 [QSA,PT,L]
```

Ablauf je Aufruf:

1. Gateway holt die Seite von `127.0.0.1:9497` und reicht Cookie, User-Agent und
   Accept-Language weiter.
2. Kommt **HTTP 200 mit `timeline-item`** zurück, wird die Antwort unverändert
   durchgereicht — live, ohne Umweg.
3. Sonst (429, leere Zeitleiste trotz 200, Backend weg) rendert das Gateway
   dieselbe Ansicht aus `wagodb.nitter_posts` im Nitter-Markup, mit
   Nitter-Stylesheet und einem Hinweisstreifen, **HTTP 200**.
4. Ist auch im Archiv nichts, wird die Originalantwort der Instanz gezeigt.

Der Inhaltstest ist der Punkt: wenn die API ganz stirbt, antwortet Nitter mit
200 und leerer Zeitleiste — ein Fallback über `ErrorDocument` würde dann nie
auslösen.

Komma-Listen sind abgedeckt, weil die App ihren Feed als `/<user1>,<user2>`
lädt; das Archiv mischt dann beide Accounts nach Zeit.

Getestet mit gestopptem Container:

```
/SZwanglos                 → HTTP 200, 19 Posts aus dem Archiv
/SZwanglos,carol_herzog    → HTTP 200, 26 Posts gemischt, Hinweisstreifen
danach wieder live         → HTTP 200, Passthrough ohne Hinweis
```

Datenbankzugang: `gateway.php` erwartet das Passwort in `NITTER_DB_PASS`
(auf dem Server steht es direkt in der Datei, die hier abgelegte Fassung ist
bereinigt). Gelesen wird `wagodb.nitter_posts`, Spalte `account`.

---

## 3. Was im Log steht

Ohne bezahlte API sind Timelines und Profile in Ordnung — `UserTweets` taucht
im Nitter-Log nie als Fehler auf. Kaputt sind die Thread-Ansichten:
`no sessions available for API: …/ConversationTimeline`. Die Abstürze
(`SIGSEGV: Illegal storage access`, 27 in einer Woche, in Schüben mit 2–7
Sofort-Neustarts) sind der Grund, wenn die Instanz „weg" wirkt — keine Sperre,
kein 401/403.
