-- Handy queries. Run with:  sqlite3 data/rumble-chat-chart.db
-- Most take a stream id; :sid below means "paste an id from the streams table".

-- What has been captured
SELECT id, title, first_seen, ended_at, peak_watching FROM streams ORDER BY first_seen DESC;

-- The id of the most recent stream, for reuse below
SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1;

-- ── who said what ────────────────────────────────────────────────────────────
SELECT created_on, username, text
FROM messages
WHERE stream_id = (SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1)
ORDER BY created_on, captured_at;

-- Most active chatters
SELECT username, COUNT(*) AS messages
FROM messages
WHERE stream_id = (SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1)
GROUP BY username
ORDER BY messages DESC
LIMIT 25;

-- Everything one person said, across every stream
SELECT s.title, m.created_on, m.text
FROM messages m JOIN streams s ON s.id = m.stream_id
WHERE m.username = 'SOME_USER'
ORDER BY m.created_on;

-- ── donations (rants) ────────────────────────────────────────────────────────
SELECT created_on, username, amount_cents / 100.0 AS dollars, text
FROM rants
WHERE stream_id = (SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1)
ORDER BY created_on;

-- Leaderboard for one stream
SELECT username, SUM(amount_cents) / 100.0 AS dollars, COUNT(*) AS rants
FROM rants
WHERE stream_id = (SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1)
GROUP BY username
ORDER BY dollars DESC;

-- Rant revenue per stream
SELECT s.first_seen, s.title, COUNT(r.msg_id) AS rants, COALESCE(SUM(r.amount_cents), 0) / 100.0 AS dollars
FROM streams s LEFT JOIN rants r ON r.stream_id = s.id
GROUP BY s.id
ORDER BY s.first_seen DESC;

-- ── subscribers and followers ───────────────────────────────────────────────
SELECT occurred_on, username, amount_cents / 100.0 AS dollars
FROM events
WHERE kind = 'subscriber'
  AND stream_id = (SELECT id FROM streams ORDER BY first_seen DESC LIMIT 1)
ORDER BY occurred_on;

-- Follower and subscriber counts over time (sampled every poll)
SELECT captured_at, followers_total, subscribers_total
FROM totals
ORDER BY captured_at DESC
LIMIT 100;

-- ── data quality ────────────────────────────────────────────────────────────
-- Polls where chat may have outrun the interval
SELECT captured_at, stream_id, window_messages, new_messages, overlap
FROM polls
WHERE suspect_gap = 1
ORDER BY captured_at DESC;

-- Recent failures
SELECT captured_at, http_status, error FROM polls
WHERE error IS NOT NULL
ORDER BY captured_at DESC LIMIT 20;
