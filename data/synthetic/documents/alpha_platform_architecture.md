---
document_id: DOC-ALPHA-ARCH-V1
tenant_id: tenant-alpha
title: Alpha Plattformarchitektur v1
classification: architecture
access_level: internal
version: v1
department: operations
created_at: 2026-07-20
valid_from: 2026-07-20
---
Synthetisches Dokument. Im deterministischen Standardmodus wird als Datenspeicher ein lokales dateibasiertes Repository für Dokumente und synthetische CRM-Datensätze genutzt; eine externe Datenbank ist dafür nicht erforderlich. PostgreSQL 16 ist im Docker-Profil als relationaler Zieladapter bereitgestellt. Qdrant 1.15 ist als Vektordatenbank-Zieladapter bereitgestellt. Der aktuelle lokale Retrieval-Kern bleibt bewusst dateibasiert, damit Tests ohne externe Dienste reproduzierbar sind. Es werden keine produktiven Zugangsdaten gespeichert.
