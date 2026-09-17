# 🏗️ Architecture, Security & Disaster Recovery

TARVeri is designed with a zero-knowledge security posture, non-blocking asynchronous I/O, automated failover circuit breakers, and continuous cloud backup.

---

## 📋 Table of Contents
1. [Zero-Knowledge Security Architecture](#zero-knowledge-security-architecture)
2. [Dual-Relay Email & Circuit Breaker](#dual-relay-email--circuit-breaker)
3. [SQLite Concurrency & WAL Checkpoint](#sqlite-concurrency--wal-checkpoint)
4. [Litestream Continuous Cloud Replication](#litestream-continuous-cloud-replication)
5. [Storage Guard & Automated Self-Healing](#storage-guard--automated-self-healing)
6. [Real-Time Telemetry with Sentry](#real-time-telemetry-with-sentry)
7. [Outage Watchdog & Self-Healing](#outage-watchdog--self-healing)

---

## 1. Zero-Knowledge Security Architecture

- **HMAC-SHA256 Blind Indexing**: Student IDs and email addresses are hashed with a salted secret (`TARVERI_ID_HASH_SECRET`). Lookups and duplicate prevention are executed purely on hashes without storing or querying raw PII.
- **Fernet AES-256 Symmetric Encryption**: Raw email strings are encrypted with AES-128-CBC + HMAC-SHA256 before disk writes.
- **Timing-Attack Resistance**: OTP validation uses `secrets.compare_digest()` for constant-time comparison.

---

## 2. Dual-Relay Email & Circuit Breaker

```mermaid
flowchart LR
    A["Verification Request"] --> B["Pre-Flight Validation"]
    B --> C{"AsyncCircuitBreaker State"}
    C -->|"CLOSED / HALF_OPEN"| D["Primary SMTP Relay<br/>(Resend / SMTP2GO)"]
    D -->|"Success"| S["Email Dispatched"]
    D -->|"Failure / Timeout"| F["Record Failure"]
    F -->|"Trips OPEN"| E["Fallback Direct SMTP<br/>(Domain Mail Server)"]
    C -->|"OPEN (Instant Failover)"| E
    E --> S
```

- **Non-Blocking `aiosmtplib`**: Delivers emails directly on the `asyncio` event loop without thread pool executor overhead.
- **`AsyncCircuitBreaker`**: If Primary SMTP fails 3 consecutive times, it trips **`OPEN`**, instantly routing subsequent verification requests to Fallback SMTP with **0ms timeout penalty**. Probes recovery in **`HALF_OPEN`** after 300s.

---

## 3. SQLite Concurrency & WAL Checkpoint

- **Write-Ahead Logging (`journal_mode=WAL`)**: Allows concurrent reads without blocking writes.
- **Busy Timeout**: Configured with `busy_timeout=15000` (15 seconds) to prevent database lock exceptions during traffic spikes.
- **Clean Checkpoints**: Truncates WAL on startup and shutdown (`PRAGMA wal_checkpoint(TRUNCATE)`).

---

## 4. Litestream Continuous Cloud Replication

TARVeri supports [Litestream](https://litestream.io) for continuous frame-by-frame SQLite streaming to Cloudflare R2 / AWS S3:

```yaml
# litestream.yml
dbs:
  - path: tarveri.db
    replicas:
      - type: s3
        bucket: $LITESTREAM_BUCKET
        path: db
        endpoint: $LITESTREAM_ENDPOINT
        access-key-id: $LITESTREAM_ACCESS_KEY_ID
        secret-access-key: $LITESTREAM_SECRET_ACCESS_KEY
        sync-interval: 10s
        retention: 720h # 30 days
        snapshot-interval: 24h
```

### Operational Modes

1. **Wrapped Execution (Automatic Sync with Bot Process)**:
   ```bash
   # Use -log-level warn to keep terminal logs clean
   litestream replicate -log-level warn -config litestream.yml -exec ".venv/bin/python tarveri_bot.py"
   ```

2. **Decoupled Tmux Panes / Windows**:
   - **Pane 1 (Discord Bot)**: `.venv/bin/python tarveri_bot.py`
   - **Pane 2 (Litestream Daemon)**: `litestream replicate -config litestream.yml`

3. **Systemd Service (24/7 Background Management)**:
   Deploy instantly for any user and directory using the universal installer:
   ```bash
   # User mode (rootless, no sudo required, runs 24/7)
   ./scripts/install_service.sh --user --with-litestream --enable-now

   # Or system-wide mode (requires sudo)
   ./scripts/install_service.sh --system --with-litestream --enable-now
   ```
   - Logs are isolated in `journalctl`:
     ```bash
     journalctl --user -u litestream -f       # Live log tail
     journalctl --user -u litestream -n 50    # Recent 50 log entries
     ```

### Disaster Recovery Commands:
- **List Snapshots**: `litestream snapshots -config litestream.yml tarveri.db`
- **List Generations**: `litestream generations -config litestream.yml tarveri.db`
- **Restore from Cloud**: `litestream restore -config litestream.yml -o tarveri.db`

---

## 5. Storage Guard & Automated Self-Healing

The `StorageGuardService` monitors SQLite database size, WAL/SHM files, local backups, and log directories against configurable thresholds (`TARVERI_MAX_STORAGE_MB`, default 500 MB).

- **Warning Threshold (80%)**: Emits Sentry warning tags and triggers preventive WAL checkpointing (`PRAGMA wal_checkpoint(TRUNCATE)`).
- **Critical Threshold (100%)**: Captures high-priority `StorageLimitExceededError` in Sentry, executes emergency self-healing (WAL truncation, audit log pruning >30 days via `prune_audit_logs()`, and backup rotation).
- **Telemetry**: Continuously reports total storage breakdown in megabytes to Sentry metrics.

---

## 6. Real-Time Telemetry with Sentry

Set `TARVERI_SENTRY_DSN` in `.env` to capture unhandled Discord interaction exceptions, database anomalies, and background task errors in real time.

Test your Sentry setup from the CLI:
```bash
python3 scripts/test_sentry.py
```

---

## 7. Outage Watchdog & Self-Healing

- **Network Probe Daemon**: Tests gateway connectivity and debounces alerts (300s window) during ISP instability.
- **Power Signal Checkpoint**: Captures `SIGPWR` (from UPS daemons) to flush SQLite WAL to disk immediately before shutdown.
- **Self-Healing on Startup**: Auto-recreates deleted roles, discovers valid review channels, and syncs member states.
