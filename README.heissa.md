# Nitterium for nitter.heissa.de — app and server side

Own build of [kaleedtc/Nitterium](https://github.com/kaleedtc/Nitterium)
(Kotlin/Jetpack Compose, a WebView wrapper around a Nitter instance) plus the
server configuration that keeps the instance on heissa.de alive since X removed
free polling.

Replaces the discontinued F-Droid app `com.plexer0.nitter`, whose source is no
longer available.

---

## 1. App

### Changes against upstream

| File | Change |
|---|---|
| `app/src/main/res/values/strings.xml` | `nitter_heissa_de_url` = `https://nitter.heissa.de` |
| `ui/feature/settings/SettingsViewModel.kt` | own instance comes first in the list, fallback when it is removed |
| `data/repository/UserPreferencesRepository.kt` | default instance + default tab `Feed` |
| `ui/feature/settings/SettingsContract.kt`, `MainViewModel.kt`, `ui/NitteriumApp.kt` | default tab `Feed` |
| `data/repository/SubscriptionRepository.kt` | first start seeds the first-order accounts `SZwanglos`, `carol_herzog`, `ZentraleV`, `SHomburg`, `Impf_Info` |
| `data/repository/UserPreferencesRepository.kt`, `ui/common/NitterWebView.kt`, `ui/feature/settings/*`, `MainActivity.kt` | **access key per instance** — see below |
| `app/build.gradle.kts` | `lint { checkReleaseBuilds = false }` — AGP lint crashes while analysing (UAST/`AsyncExecutionService` bug) and would otherwise block the release build |

The app loads its feed as `https://nitter.heissa.de/<user1>,<user2>` (Nitter's
multi timeline) and profiles as `/<user>`. Pull to refresh is a
`webView.reload()` — it fetches live from the instance; there is **no** way to
trigger the database poller on the server from inside the app.

### Building

```bash
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
JAVA_HOME=/path/to/jdk21 ./gradlew assembleRelease
zipalign -f -p 4 app/build/outputs/apk/release/app-release-unsigned.apk nitterium-heissa.apk
apksigner sign --ks <keystore> nitterium-heissa.apk
```

Requirements: a **real JDK 21** — a JRE is not enough (`javac` missing, Gradle
aborts with "Toolchain … does not provide the required capabilities:
[JAVA_COMPILER]"). Android SDK; `compileSdk 37` is fetched by AGP itself, so
`cmdline-tools` are not needed, but `~/Android/Sdk/licenses` has to exist.

### Access key per instance

The tweet detail pages of the instance are closed to strangers (see 2.3). An
address list in the vhost only carries as long as the phone sits in the home
WLAN: on mobile data, and after every prefix change of the router, the app drops
out and gets a 403 on its own host.

So the app identifies itself. Settings has a field below the instance URL,
**Instance access key (optional)**; the value is stored per **host**
(`instance_keys`, JSON `{host: key}`) and is appended to the user agent as
` Nitterium/<key>` — to that one instance only, never to another. Without a key
the app behaves exactly as before.

Server side, one line inside the `LocationMatch`:

```apache
Require expr "%{HTTP_USER_AGENT} =~ m#Nitterium/<key>#"
```

**Entering it without typing.** Typing 32 characters on a phone goes wrong —
measured: `b208675f926a…` came out as `b8675f92a64c…`. The app therefore also
accepts the key from a link:

```
nitterium://instance?url=https%3A%2F%2Fnitter.heissa.de&key=<key>
```

Printed as a QR code the ordinary camera app is enough — **no camera library
and no camera permission in Nitterium**. The app confirms with "Access key
saved for \<host\>". Python's `qrcode` is enough to generate it; over adb it
works directly as well:

```bash
adb shell "am start -a android.intent.action.VIEW -d 'nitterium://instance?url=…&key=…'"
```

The quotes belong **around the whole command**: `adb shell` hands its arguments
to the shell on the phone and loses the local quoting on the way — a bare `&`
becomes a background operator there, `key=…` is dropped, and an empty key
deletes the entry.

Proof from production, same phone, same mobile network, WLAN off:

```
2a00:20:…            "GET /AnwaltUlbrich/status/2093855542447902741"  403   (no key)
2a00:20:429b:f162:…  "GET /leeksmiau/status/2093914404077056392"      200   (key in the user agent)
```

### Binary

`bin/nitterium-heissa.apk` — release, signed with an own key
(SHA-256 `09c4e95e94d1a05f8c37710f7d1e0cad3f997610feb58f3826bd88731f38e3a4`,
`CN=gerontec, O=heissa.de`). The keystore is **not** in the repository
(`~/.android/nitterium-release.jks`); without it no update can be installed over
the app already on the device.

### Network

`nitter.heissa.de` is dual stack (AAAA + A). Android picks IPv6 via Happy
Eyeballs and falls back to IPv4 on its own — nothing about that is hardcoded in
the app.

---

## 2. Server side (heissa.de)

Everything under `server/` is the cleaned version of the running configuration
(credentials and private addresses replaced by placeholders).

### 2.1 Poll budget: 30 requests per day

The instance runs on **one** session token (`~/nitter/sessions.jsonl`, refreshed
every 8 h). Since free access disappeared, `nitter_poll.py` only runs on
staggered times and in small batches:

| Time | Job | Requests/run | Runs/day | Total |
|---|---|---|---|---|
| :10 every 6 h | `--following SZwanglos --batch 3` | 3 | 4 | 12 |
| :25 every 6 h | `--user carol_herzog` | 1 | 4 | 4 |
| :40 every 6 h | `--user ZentraleV` | 1 | 4 | 4 |
| :55 every 6 h | `--user SHomburg` | 1 | 4 | 4 |
| :10 at 6 and 18 | `--following Impf_Info --batch 3` | 3 | 2 | 6 |

At least 15 minutes lie between two runs, and inside a run the poller pauses
1.5–4.5 s per account. Never raise the frequency without need — a ban would hit
the account behind the token.

History is **not** fetched through the API but with Playwright over x.com using
the logged-in browser session (`zentralev_backfill.py`, `carol_backfill.py`;
14-day cut, stops only after 3 old posts — pinned old tweets would otherwise
end the run too early).

### 2.2 Health check: do not point it at a tweet page

The Docker health check requested `/Jack/status/20` every 30 s. Tweet detail
pages need `ConversationTimeline`, and that endpoint is permanently empty for a
free session: **2,880 futile API attempts per day**, ~400 log lines per hour,
around the clock. The page itself came from the Redis cache and returned 200, so
the container counted as "healthy".

The target is a cached RSS page instead:

```yaml
healthcheck:
  test: wget -nv --tries=1 --spider http://127.0.0.1:8080/SZwanglos/rss || exit 1
```

### 2.3 Foreign access: block only what triggers a poll

Measured: 77 requests to tweet detail pages in 5.5 minutes from **77 different
IPs**, every IP exactly once, every tweet ID exactly once — a proxy pool walking
an alphabetical account list. Every one of those requests is necessarily a cache
miss and therefore a poll on our session. Per-IP rules cannot catch that in
principle.

So the vhost closes exactly one path to strangers and leaves everything else
open (`server/nitter-le-ssl.conf.example`):

```apache
<LocationMatch "^/[A-Za-z0-9_]+/status/[0-9]+">
    Require ip 127.0.0.1 ::1 <own addresses, WireGuard, LANs>
    Require expr "%{HTTP_USER_AGENT} =~ m#Nitterium/<key>#"
</LocationMatch>
```

* closed: only `/<user>/status/<id>` — the single path that reaches X per request
* open to everyone: profiles, timelines, RSS, search, images, static files —
  everything served from the Redis cache that never touches X
* effect: 19 scraper requests/minute run into 403, and the Nitter log shows **0**
  API attempts (before: ~400/h)

**Trap:** `Require ip` needs `mod_authz_host`. If it was not loaded, the reload
fails with "Unknown Authz provider: ip" and Apache stays down — so `a2enmod
authz_host` first, `apachectl configtest` afterwards.

As a safety net against individual long runners, the fail2ban jail
`nitter-scrape` (`server/fail2ban-*`) runs as well: 30 tweet pages in 10 minutes
per IP, then a 1 h ban, escalating up to one day; own networks in `ignoreip`. It
needs the access log `/var/log/apache2/nitter_access.log`, which both Nitter
vhosts write.

### 2.4 Archive gateway: live and archive without switching

`server/gateway.php` sits on the server at
`/var/www/nitter_archive/gateway.php` and is placed in front of the profile and
feed paths:

```apache
ProxyPass /_archive !
ProxyPassMatch "^/(?!about$|settings$|search$|explore$|css$|js$|pic$|fonts$)[A-Za-z0-9_][A-Za-z0-9_,]{0,120}/?$" !
RewriteRule "^/(?!…)([A-Za-z0-9_][A-Za-z0-9_,]{0,120})/?$" /_archive/gateway.php?u=$1 [QSA,PT,L]
```

What happens per call:

1. The gateway fetches the page from `127.0.0.1:9497` and passes cookie, user
   agent and Accept-Language along.
2. If **HTTP 200 with `timeline-item`** comes back, the response is passed
   through unchanged — live, no detour.
3. Otherwise (429, empty timeline despite 200, backend gone) the gateway renders
   the same view from `wagodb.nitter_posts` in Nitter's markup, with Nitter's
   stylesheet and a notice strip, **HTTP 200**.
4. If the archive has nothing either, the instance's original response is shown.

The content test is the point: when the API dies completely, Nitter answers 200
with an empty timeline — a fallback via `ErrorDocument` would never fire.

Comma lists are covered because the app loads its feed as `/<user1>,<user2>`;
the archive then merges both accounts by time.

Tested with the container stopped:

```
/SZwanglos                 → HTTP 200, 19 posts from the archive
/SZwanglos,carol_herzog    → HTTP 200, 26 posts merged, notice strip
live again afterwards      → HTTP 200, passthrough without notice
```

Database access: `gateway.php` expects the password in `NITTER_DB_PASS` (on the
server it sits directly in the file; the copy stored here is cleaned). It reads
`wagodb.nitter_posts`, column `account`.

---

## 3. What the log says

Without a paid API, timelines and profiles are fine — `UserTweets` never shows
up as an error in the Nitter log. What is broken are the thread views:
`no sessions available for API: …/ConversationTimeline`. The crashes
(`SIGSEGV: Illegal storage access`, 27 in one week, in bursts with 2–7 immediate
restarts) are the reason when the instance appears to be "gone" — no ban, no
401/403.
