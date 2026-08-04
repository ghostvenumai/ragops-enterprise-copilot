# Privacy

- Data in the repository is synthetic.
- Real customer data, secrets, production logs, and private documents are not
  allowed.
- PII detectors mask email addresses, phone numbers, and IBAN-like identifiers.
- Audit logs store request metadata, not full confidential source bodies.
- Retention concept: evaluation and demo evidence is local and may be removed
  with documented cleanup procedures. Production retention would require tenant
  policy mapping and legal review.
- Deletion concept: document deletion is disabled in the portfolio API demo to
  avoid accidental destructive behavior. A production adapter would implement
  tombstones, lineage invalidation, and vector-store deletion.
