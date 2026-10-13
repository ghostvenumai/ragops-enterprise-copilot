"use client";

import * as React from "react";

export interface ClientSession {
  name: string | null;
  email: string | null;
  tenantId: string;
  roles: string[];
  csrfToken: string;
}

const SessionContext = React.createContext<ClientSession | null>(null);

export function SessionProvider({ value, children }: { value: ClientSession; children: React.ReactNode }) {
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): ClientSession {
  const value = React.useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}
