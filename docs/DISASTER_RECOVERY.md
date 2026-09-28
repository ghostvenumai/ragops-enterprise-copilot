# Disaster recovery

The contract is create, verify, isolate, restore and validate (see
[BACKUP_RESTORE.md](BACKUP_RESTORE.md)). RPO follows the configured backup interval; no
product-level RPO, RTO or retention target is published. The `backup_restore` RC gate
measures the drill (backup duration, restore duration and the snapshot-to-completion gap)
on synthetic data only; those durations are measurements, not a contractual RTO.
