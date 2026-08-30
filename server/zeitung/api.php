<?php
/**
 * ZeitungApi - maschinenlesbarer Zugang zum Zeitungsarchiv.
 *
 * suche.php zeigt die Treffer als Webseite. Fuer eine KI ist das unbrauchbar:
 * sie muesste HTML zerlegen, bekaeme genau einen Ausschnitt je Ausgabe und
 * saehe nicht, was ueberhaupt vorhanden ist. Diese Datei liefert dieselbe
 * Suche als JSON, mit mehreren Fundstellen je Ausgabe, waehlbarem Umfeld und
 * dem Volltext einer Ausgabe auf Anfrage.
 *
 * Grundlage sind die .txt-Dateien neben den PDFs - der Text liegt also schon
 * vor, es wird nichts erst umgewandelt.
 *
 * Einstieg ohne Parameter: Manifest mit allen Endpunkten.
 *
 *   ?view=manifest   Selbstbeschreibung (Default)
 *   ?view=ausgaben   Welche Ausgaben liegen vor
 *   ?view=suche      Volltextsuche ueber alle Ausgaben
 *   ?view=text       Volltext einer Ausgabe, seitenweise abrufbar
 *   ?view=trauer     Traueranzeigen aus der Datenbank
 *   ?view=openapi    OpenAPI-3.1-Beschreibung dieser Schnittstelle
 *
 * Suche:
 *   &q=Begriff              mehrere Woerter = alle muessen vorkommen
 *   &q="genauer Wortlaut"   Anfuehrungszeichen halten die Wortfolge zusammen
 *   &von=YYYY-MM-DD &bis=   Zeitraum ueber das Ausgabedatum
 *   &limit=N                hoechstens N Ausgaben (1..200, Vorgabe 25)
 *   &treffer=N              hoechstens N Fundstellen je Ausgabe (1..20, Vorgabe 3)
 *   &kontext=N              Zeichen um die Fundstelle (40..1200, Vorgabe 300)
 *
 * Volltext:
 *   &ausgabe=YYYY-MM-DD     welche Ausgabe
 *   &ab=N &zeichen=N        Ausschnitt ab Zeichen N (fuer lange Ausgaben)
 *
 * &format=md liefert manifest, suche und text als Markdown statt JSON.
 */

declare(strict_types=1);

const ARCHIV      = __DIR__;
const BLATT       = 'toelzer-kurier';
const MAX_AUSGABE = 200;
const MAX_TREFFER = 20;
const MAX_KONTEXT = 1200;
const MAX_ZEICHEN = 120000;   // Obergrenze fuer view=text in einem Abruf

// ---------------------------------------------------------------------------

function ausgaben(): array
{
    $liste = [];
    foreach (glob(ARCHIV . '/' . BLATT . '-*.txt') as $txt) {
        $name = basename($txt, '.txt');
        if (!preg_match('/(\d{4}-\d{2}-\d{2})$/', $name, $m)) continue;
        $pdf = ARCHIV . '/' . $name . '.pdf';
        $liste[$m[1]] = [
            'datum'    => $m[1],
            'zeichen'  => (int)filesize($txt),
            'txt'      => $name . '.txt',
            'pdf'      => is_file($pdf) ? $name . '.pdf' : null,
            'pdf_mb'   => is_file($pdf) ? round(filesize($pdf) / 1048576, 1) : null,
        ];
    }
    krsort($liste);
    return $liste;
}

/** Zerlegt die Anfrage: "in Anfuehrungszeichen" bleibt zusammen. */
function begriffe(string $q): array
{
    preg_match_all('/"([^"]+)"|(\S+)/u', $q, $m, PREG_SET_ORDER);
    $out = [];
    foreach ($m as $t) {
        $w = trim($t[1] !== '' ? $t[1] : ($t[2] ?? ''));
        if ($w !== '') $out[] = $w;
    }
    return $out;
}

/** Fundstellen eines Begriffs im Text, mit Umfeld. */
function fundstellen(string $text, string $wort, int $max, int $kontext): array
{
    $treffer = [];
    $pos = 0;
    while (count($treffer) < $max && ($pos = stripos($text, $wort, $pos)) !== false) {
        $start = max(0, $pos - (int)($kontext / 2));
        $stueck = substr($text, $start, $kontext);
        // an Wortgrenzen abschneiden, damit kein halbes Wort am Rand steht
        if ($start > 0 && ($s = strpos($stueck, ' ')) !== false) $stueck = substr($stueck, $s + 1);
        if (($e = strrpos($stueck, ' ')) !== false && $start + $kontext < strlen($text)) {
            $stueck = substr($stueck, 0, $e);
        }
        $treffer[] = [
            'position' => $pos,
            'text'     => trim(preg_replace('/\s+/', ' ', $stueck)),
        ];
        $pos += max(1, strlen($wort));
    }
    return $treffer;
}

function suche(string $q, string $von, string $bis, int $limit,
               int $proAusgabe, int $kontext): array
{
    $worte = begriffe($q);
    if (!$worte) return ['fehler' => 'kein Suchbegriff (&q=)'];

    $funde = [];
    $durchsucht = 0;
    $gefunden = 0;
    $summe = 0;
    foreach (ausgaben() as $datum => $a) {
        if ($von !== '' && $datum < $von) continue;
        if ($bis !== '' && $datum > $bis) continue;
        $text = @file_get_contents(ARCHIV . '/' . $a['txt']);
        if ($text === false) continue;
        $durchsucht++;

        // alle Begriffe muessen vorkommen
        $zaehler = [];
        foreach ($worte as $w) {
            $n = substr_count(strtolower($text), strtolower($w));
            if ($n === 0) { $zaehler = []; break; }
            $zaehler[$w] = $n;
        }
        if (!$zaehler) continue;

        // Immer der ganze Bestand: sonst waere die Gesamtzahl eine
        // Untertreibung, sobald limit greift. Begrenzt wird nur die Ausgabe.
        $eintrag = [
            'datum'      => $datum,
            'treffer'    => array_sum($zaehler),
            'je_begriff' => $zaehler,
            'pdf'        => $a['pdf'],
        ];
        $gefunden++;
        $summe += $eintrag['treffer'];
        if (count($funde) < $limit) {
            $eintrag['fundstellen'] =
                fundstellen($text, $worte[0], $proAusgabe, $kontext);
            $funde[] = $eintrag;
        }
    }

    return [
        'suche'           => $q,
        'begriffe'        => $worte,
        'zeitraum'        => ['von' => $von ?: null, 'bis' => $bis ?: null],
        'durchsucht'      => $durchsucht,
        'ausgaben_gesamt' => $gefunden,
        'ausgaben'        => count($funde),
        'summe'           => $summe,
        'gekuerzt'        => $gefunden > count($funde),
        'ergebnis'        => $funde,
    ];
}

function volltext(string $datum, int $ab, int $zeichen): array
{
    $alle = ausgaben();
    if (!isset($alle[$datum])) {
        return ['fehler' => "keine Ausgabe vom $datum",
                'vorhanden' => array_slice(array_keys($alle), 0, 10)];
    }
    $pfad = ARCHIV . '/' . $alle[$datum]['txt'];
    $gesamt = (int)filesize($pfad);
    $zeichen = max(500, min(MAX_ZEICHEN, $zeichen));
    $ab = max(0, min($ab, $gesamt));
    $fh = fopen($pfad, 'rb');
    fseek($fh, $ab);
    $stueck = (string)fread($fh, $zeichen);
    fclose($fh);
    return [
        'datum'   => $datum,
        'gesamt'  => $gesamt,
        'ab'      => $ab,
        'gelesen' => strlen($stueck),
        'weiter'  => ($ab + strlen($stueck) < $gesamt)
                     ? ($ab + strlen($stueck)) : null,
        'pdf'     => $alle[$datum]['pdf'],
        'text'    => $stueck,
    ];
}

function trauer(string $von, string $bis): array
{
    try {
        // Zugangsdaten aus /etc/heissa-db.ini (0640 root:www-data), nicht
        // im Quelltext - diese Datei liegt in einem oeffentlichen Repo.
        $z = @parse_ini_file('/etc/heissa-db.ini', true)['wagodb'] ?? [];
        $pdo = new PDO(
            'mysql:host=' . ($z['host'] ?? 'localhost')
            . ';dbname=' . ($z['database'] ?? 'wagodb') . ';charset=utf8mb4',
            $z['user'] ?? 'gh', $z['password'] ?? '',
            [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
        $st = $pdo->prepare(
            "SELECT vorname, nachname, geburtsname, geboren, gestorben, ort,
                    beruf, trauernde, ausgabe
               FROM traueranzeigen
              WHERE gestorben BETWEEN :von AND :bis AND nachname IS NOT NULL
              ORDER BY gestorben DESC, nachname, vorname");
        $st->execute([':von' => $von, ':bis' => $bis]);
        $zeilen = $st->fetchAll(PDO::FETCH_ASSOC);
        return ['zeitraum' => ['von' => $von, 'bis' => $bis],
                'anzahl' => count($zeilen), 'anzeigen' => $zeilen];
    } catch (Exception $e) {
        return ['fehler' => $e->getMessage()];
    }
}

function manifest(): array
{
    $a = ausgaben();
    $daten = array_keys($a);
    $u = 'https://web2.heissa.de/zeitung/api.php';
    return [
        'name'    => 'ZeitungApi',
        'zweck'   => 'Volltextsuche im Zeitungsarchiv, maschinenlesbar.',
        'blatt'   => 'Toelzer Kurier',
        'bestand' => [
            'ausgaben' => count($a),
            'von'      => $daten ? end($daten) : null,
            'bis'      => $daten ? $daten[0] : null,
            'hinweis'  => 'Der Text stammt aus den .txt-Dateien neben den PDFs. '
                        . 'Es ist maschinell gelesener Zeitungssatz: Spalten '
                        . 'laufen ineinander, Trennstriche und Namen koennen '
                        . 'verfaelscht sein. Ein fehlender Treffer bedeutet '
                        . 'nicht sicher, dass der Begriff nicht im Blatt stand.',
        ],
        'endpunkte' => [
            ['view' => 'manifest', 'url' => $u, 'zweck' => 'Diese Selbstbeschreibung.'],
            ['view' => 'ausgaben', 'url' => "$u?view=ausgaben",
             'zweck' => 'Welche Ausgaben vorliegen, mit Groesse und PDF-Namen.'],
            ['view' => 'suche', 'url' => "$u?view=suche&q=Lenggries",
             'zweck' => 'Volltextsuche. Mehrere Woerter = alle muessen vorkommen, '
                      . 'Anfuehrungszeichen halten eine Wortfolge zusammen.',
             'parameter' => ['q', 'von', 'bis', 'limit', 'treffer', 'kontext']],
            ['view' => 'text', 'url' => "$u?view=text&ausgabe=" . ($daten[0] ?? ''),
             'zweck' => 'Volltext einer Ausgabe, ueber &ab= seitenweise weiter.',
             'parameter' => ['ausgabe', 'ab', 'zeichen']],
            ['view' => 'trauer', 'url' => "$u?view=trauer&von=2026-08-01&bis=2026-08-28",
             'zweck' => 'Traueranzeigen aus der Tabelle traueranzeigen.',
             'parameter' => ['von', 'bis']],
            ['view' => 'openapi', 'url' => "$u?view=openapi",
             'zweck' => 'OpenAPI-3.1-Beschreibung.'],
        ],
        'menschlich' => 'https://web2.heissa.de/zeitung/suche.php',
        'formate'    => ['json' => 'Vorgabe', 'md' => '&format=md fuer manifest, suche und text'],
    ];
}

function openapi(): array
{
    $p = fn(string $n, string $b, string $t = 'string') => [
        'name' => $n, 'in' => 'query', 'description' => $b,
        'schema' => ['type' => $t]];
    return [
        'openapi' => '3.1.0',
        'info' => ['title' => 'ZeitungApi', 'version' => '1.0',
                   'description' => 'Volltextsuche im Archiv des Toelzer Kurier.'],
        'servers' => [['url' => 'https://web2.heissa.de/zeitung']],
        'paths' => ['/api.php' => ['get' => [
            'summary' => 'Alle Sichten ueber &view=',
            'parameters' => [
                $p('view', 'manifest | ausgaben | suche | text | trauer | openapi'),
                $p('q', 'Suchbegriff, mehrere Woerter = UND'),
                $p('von', 'Ausgabedatum ab YYYY-MM-DD'),
                $p('bis', 'Ausgabedatum bis YYYY-MM-DD'),
                $p('ausgabe', 'Datum der Ausgabe fuer view=text'),
                $p('ab', 'Zeichenposition fuer view=text', 'integer'),
                $p('limit', 'Hoechstzahl Ausgaben', 'integer'),
                $p('treffer', 'Fundstellen je Ausgabe', 'integer'),
                $p('kontext', 'Zeichen um die Fundstelle', 'integer'),
                $p('format', 'json oder md'),
            ],
            'responses' => ['200' => ['description' => 'JSON, bei format=md Markdown']],
        ]]],
    ];
}

// --- Markdown ---------------------------------------------------------------

function mdSuche(array $r): string
{
    if (isset($r['fehler'])) return "# Fehler\n\n" . $r['fehler'] . "\n";
    $s = "# Suche: {$r['suche']}\n\n"
       . "{$r['ausgaben_gesamt']} von {$r['durchsucht']} Ausgaben enthalten den "
       . "Begriff, zusammen {$r['summe']} Fundstellen"
       . ($r['gekuerzt'] ? ", gezeigt werden die {$r['ausgaben']} neuesten" : "")
       . ".\n\n";
    foreach ($r['ergebnis'] as $e) {
        $s .= "## {$e['datum']} ({$e['treffer']} Treffer)\n\n";
        foreach ($e['fundstellen'] as $f) $s .= "> …{$f['text']}…\n\n";
        if ($e['pdf']) $s .= "PDF: https://web2.heissa.de/zeitung/{$e['pdf']}\n\n";
    }
    return $s;
}

function mdText(array $r): string
{
    if (isset($r['fehler'])) return "# Fehler\n\n" . $r['fehler'] . "\n";
    return "# Ausgabe {$r['datum']}\n\nZeichen {$r['ab']}–"
         . ($r['ab'] + $r['gelesen']) . " von {$r['gesamt']}"
         . ($r['weiter'] ? " (weiter mit &ab={$r['weiter']})" : "")
         . "\n\n" . $r['text'] . "\n";
}

function mdManifest(array $m): string
{
    $s = "# {$m['name']}\n\n{$m['zweck']}\n\n"
       . "Blatt: {$m['blatt']} — {$m['bestand']['ausgaben']} Ausgaben "
       . "({$m['bestand']['von']} bis {$m['bestand']['bis']})\n\n"
       . "{$m['bestand']['hinweis']}\n\n## Endpunkte\n\n";
    foreach ($m['endpunkte'] as $e) $s .= "- **{$e['view']}** — {$e['zweck']}\n  {$e['url']}\n";
    return $s;
}

// --- Ausgabe ----------------------------------------------------------------

function out(array $d): never
{
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: *');
    echo json_encode($d, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE
                       | JSON_UNESCAPED_SLASHES);
    exit;
}

function outText(string $s): never
{
    header('Content-Type: text/markdown; charset=utf-8');
    header('Access-Control-Allow-Origin: *');
    echo $s;
    exit;
}

$view   = strtolower((string)($_GET['view'] ?? 'manifest'));
$format = strtolower((string)($_GET['format'] ?? 'json'));
$datum  = fn(string $k) => preg_match('/^\d{4}-\d{2}-\d{2}$/', (string)($_GET[$k] ?? ''))
                           ? (string)$_GET[$k] : '';

switch ($view) {
    case 'openapi':
        out(openapi());

    case 'ausgaben':
        $a = ausgaben();
        out(['anzahl' => count($a), 'ausgaben' => array_values($a)]);

    case 'suche':
        $r = suche(
            trim((string)($_GET['q'] ?? '')),
            $datum('von'), $datum('bis'),
            max(1, min(MAX_AUSGABE, (int)($_GET['limit'] ?? 25))),
            max(1, min(MAX_TREFFER, (int)($_GET['treffer'] ?? 3))),
            max(40, min(MAX_KONTEXT, (int)($_GET['kontext'] ?? 300))));
        $format === 'md' ? outText(mdSuche($r)) : out($r);

    case 'text':
        $r = volltext($datum('ausgabe'), (int)($_GET['ab'] ?? 0),
                      (int)($_GET['zeichen'] ?? 20000));
        $format === 'md' ? outText(mdText($r)) : out($r);

    case 'trauer':
        out(trauer($datum('von') ?: date('Y-m-d', strtotime('-30 days')),
                   $datum('bis') ?: date('Y-m-d')));

    default:
        $m = manifest();
        $format === 'md' ? outText(mdManifest($m)) : out($m);
}
