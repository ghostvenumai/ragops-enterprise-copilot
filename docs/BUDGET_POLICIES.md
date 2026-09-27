# Budget policies

Budgets use EUR explicitly, monthly period boundaries and Decimal arithmetic. At 80% a warning is emitted; at 100% optimize mode requests a cheaper compliant route; hard-limit mode denies at the configured hard threshold when no compliant route remains. Capability, high-risk and tenant model policy always take precedence.

Thresholds are inclusive: reaching 80% warns, reaching 100% routes cheaper and reaching the hard threshold denies. Preflight and reservation apply the same boundary, so a reservation that would bring committed plus active reserved spend exactly to the hard limit is denied. A hard-limit budget without an explicit hard threshold fails closed at 100% of the budget amount in both paths.
