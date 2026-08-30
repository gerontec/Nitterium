<?php
/**
 * gateway.php - transparent front end for Nitter profile pages.
 *
 * Fetches the page from the instance. If a real timeline comes back it is
 * passed through unchanged. If Nitter returns nothing usable (rate limit, empty
 * timeline despite HTTP 200, backend gone), the same view is rendered from
 * wagodb.nitter_posts. No switching required.
 */

const BACKEND   = "http://127.0.0.1:9497";
const TIMEOUT_S = 8;

$db_host = "localhost";
$db_user = "gh";
$db_pass = getenv("NITTER_DB_PASS") ?: "";
$db_name = "wagodb";

// The app loads its feed as /<user1>,<user2> - allow comma lists
$user  = (string)($_GET["u"] ?? "");
$users = array_values(array_filter(explode(",", $user), static fn($u) =>
    preg_match("~^[A-Za-z0-9_]{1,15}$~", $u) === 1));
if (!$users) {
    http_response_code(404);
    exit;
}
$title = "@" . implode(", @", $users);

// Pass the query string on without our own u= (e.g. ?cursor=... when paging)
$params = $_GET;
unset($params["u"]);
$qs  = $params ? "?" . http_build_query($params) : "";
$url = BACKEND . "/" . implode(",", array_map("rawurlencode", $users)) . $qs;

$fwd = [];
foreach (["HTTP_COOKIE" => "Cookie", "HTTP_USER_AGENT" => "User-Agent",
          "HTTP_ACCEPT_LANGUAGE" => "Accept-Language"] as $k => $h) {
    if (!empty($_SERVER[$k])) {
        $fwd[] = "$h: " . $_SERVER[$k];
    }
}

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HEADER         => true,
    CURLOPT_TIMEOUT        => TIMEOUT_S,
    CURLOPT_HTTPHEADER     => $fwd,
]);
$raw      = curl_exec($ch);
$code     = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
$hdr_size = (int)curl_getinfo($ch, CURLINFO_HEADER_SIZE);
curl_close($ch);

$head = $raw === false ? "" : substr($raw, 0, $hdr_size);
$body = $raw === false ? "" : substr($raw, $hdr_size);

// Usable = HTTP 200 with at least one real tweet in the timeline
$usable = ($code === 200 && str_contains($body, "timeline-item"));

if ($usable) {
    foreach (explode("\r\n", $head) as $line) {
        if (preg_match("~^(Content-Type|Set-Cookie|Cache-Control|Last-Modified):~i", $line)) {
            header($line, false);
        }
    }
    http_response_code(200);
    echo $body;
    exit;
}

// ---- Archive from the database -----------------------------------------------

$rows = [];
try {
    $pdo = new PDO("mysql:host=$db_host;dbname=$db_name;charset=utf8mb4", $db_user, $db_pass,
                   [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $in = implode(",", array_fill(0, count($users), "?"));
    $st = $pdo->prepare(
        "SELECT title, content, link, published_at, author, account
           FROM nitter_posts
          WHERE account IN ($in)
          ORDER BY published_at DESC
          LIMIT 60"
    );
    $st->execute($users);
    $rows = $st->fetchAll(PDO::FETCH_ASSOC);
} catch (PDOException $e) {
    $rows = [];
}

// Nothing in the archive: show the instance's original response instead
if (!$rows) {
    if ($raw !== false && $body !== "") {
        foreach (explode("\r\n", $head) as $line) {
            if (preg_match("~^(Content-Type|Set-Cookie):~i", $line)) {
                header($line, false);
            }
        }
        http_response_code($code ?: 502);
        echo $body;
        exit;
    }
    http_response_code(503);
    header("Content-Type: text/html; charset=utf-8");
    echo "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
         "<link rel=\"stylesheet\" href=\"/css/style.css\"></head><body><div class=\"container\">",
         "<p>@", htmlspecialchars($user), " ist gerade weder ueber X noch im Archiv erreichbar.</p>",
         "</div></body></html>";
    exit;
}

$newest = (string)($rows[0]["published_at"] ?? "");
$out  = "<div class=\"archive-note\">Archiv-Ansicht &ndash; X liefert gerade nichts. "
      . count($rows) . " gespeicherte Posts von " . htmlspecialchars($title)
      . ", neuester vom " . htmlspecialchars($newest) . ".</div>";

foreach ($rows as $r) {
    $acc  = (string)($r["account"] ?? $users[0]);
    $text = trim((string)($r["title"] !== "" ? $r["title"] : $r["content"]));
    $link = (string)$r["link"];
    $date = $r["published_at"] ? date("d.m.Y H:i", strtotime((string)$r["published_at"])) : "";
    $out .= "<div class=\"timeline-item\"><div class=\"tweet-body\">"
          . "<div class=\"tweet-header\"><div class=\"fullname-and-username\">"
          . "<a class=\"fullname\" href=\"/" . rawurlencode($acc) . "\">"
          . htmlspecialchars((string)($r["author"] !== "" ? $r["author"] : $acc)) . "</a>"
          . "<a class=\"username\" href=\"/" . rawurlencode($acc) . "\">@"
          . htmlspecialchars($acc) . "</a></div>"
          . "<span class=\"tweet-date\"><a href=\"" . htmlspecialchars($link) . "\">" . $date
          . "</a></span></div>"
          . "<div class=\"tweet-content\">" . nl2br(htmlspecialchars($text)) . "</div>"
          . "</div></div>";
}

http_response_code(200);
header("Content-Type: text/html; charset=utf-8");
header("Cache-Control: no-store");
echo "<!DOCTYPE html><html lang=\"de\"><head><meta charset=\"utf-8\">",
     "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
     "<title>", htmlspecialchars($title), "</title>",
     "<link rel=\"stylesheet\" href=\"/css/style.css\">",
     "<style>.archive-note{padding:.6em 1em;margin:0 0 .4em;border-left:4px solid #ff7a18;",
     "background:rgba(255,122,24,.12);font-size:.9em}.timeline-item{padding:.6em 1em}",
     ".tweet-date{float:right;font-size:.85em;opacity:.7}",
     ".tweet-content{white-space:pre-wrap;margin-top:.3em}</style></head><body>",
     "<div class=\"container\"><div class=\"timeline-container\">", $out,
     "</div></div></body></html>";
