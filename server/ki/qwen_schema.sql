-- Wissensbasis des Modells. Alles unter dem Praefix qwen_, damit die
-- gewachsene wagodb unberuehrt bleibt und der Bestand des Modells jederzeit
-- als Ganzes sichtbar, sicherbar und loeschbar ist.

-- Gemerkte Aussagen. Der Volltextindex auf aussage+thema macht das
-- Nachschlagen schnell, ohne dass ueber Millionen Zeilen gescannt wird.
CREATE TABLE IF NOT EXISTS qwen_wissen (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  thema       VARCHAR(120)  NOT NULL,
  aussage     TEXT          NOT NULL,
  quelle      VARCHAR(500)  NULL,
  vertrauen   TINYINT       NOT NULL DEFAULT 2,   -- 1 vermutet, 2 belegt, 3 geprueft
  gueltig_ab  DATE          NULL,
  gueltig_bis DATE          NULL,
  angelegt_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  geprueft_at DATETIME      NULL,
  fingerprint CHAR(32) AS (MD5(CONCAT(thema,'|',LEFT(aussage,400)))) STORED,
  UNIQUE KEY uq_fingerprint (fingerprint),
  KEY idx_thema (thema),
  FULLTEXT KEY ft_wissen (thema, aussage)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Abgerufene Seiten. Spart den zweiten Abruf derselben Adresse und macht
-- nachvollziehbar, worauf sich eine Aussage stuetzte.
CREATE TABLE IF NOT EXISTS qwen_seiten (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  url        VARCHAR(700)  NOT NULL,
  url_hash   CHAR(32)      AS (MD5(url)) STORED,
  titel      VARCHAR(500)  NULL,
  text       MEDIUMTEXT    NULL,
  zeichen    INT           NOT NULL DEFAULT 0,
  status     SMALLINT      NULL,
  geholt_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_url (url_hash),
  KEY idx_geholt (geholt_at),
  FULLTEXT KEY ft_seiten (titel, text)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Suchlaeufe mit ihren Treffern, damit dieselbe Frage nicht zweimal ins Netz geht.
CREATE TABLE IF NOT EXISTS qwen_suchen (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  begriff    VARCHAR(300)  NOT NULL,
  begriff_h  CHAR(32)      AS (MD5(LOWER(begriff))) STORED,
  treffer    TEXT          NULL,          -- JSON: Titel und Adressen
  anzahl     SMALLINT      NOT NULL DEFAULT 0,
  gesucht_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_begriff (begriff_h),
  KEY idx_gesucht (gesucht_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Verknuepfung Aussage -> belegende Seite.
CREATE TABLE IF NOT EXISTS qwen_belege (
  wissen_id BIGINT NOT NULL,
  seiten_id BIGINT NOT NULL,
  stelle    TEXT   NULL,
  PRIMARY KEY (wissen_id, seiten_id),
  KEY idx_seite (seiten_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SHOW TABLES LIKE 'qwen\_%';
