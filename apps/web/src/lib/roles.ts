/** UI hints only: FastAPI decides every request. These mirror REVIEW_ROLES and admin checks. */
export const REVIEW_ROLES = ["admin", "compliance"] as const;

export function canReview(roles: readonly string[]): boolean {
  return roles.some((role) => (REVIEW_ROLES as readonly string[]).includes(role));
}

export function isAdmin(roles: readonly string[]): boolean {
  return roles.includes("admin");
}

export function canManageIngestion(roles: readonly string[]): boolean {
  return isAdmin(roles);
}
